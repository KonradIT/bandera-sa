"""Live web UI: one radio thread runs the Scanner, browsers follow it over Server-Sent Events.

    GET  /             the page (bandera/static/index.html)
    GET  /events       SSE stream: 'state', 'sweep', 'status'
    GET  /api/state    current state as JSON
    POST /api/config   {"band": [lo, hi], "w": 16, "gain": 60, "auto_gain": false, "detect_wifi": false,
                        "whole_window": false}
    POST /api/control  {"op": "start" | "pause" | "clear" | "record", "on": bool, "tag": str}

Only the radio thread touches the Scanner and the B210; HTTP threads queue commands for it.
Standard library only.
"""
from __future__ import annotations

import collections
import json
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .scanner import ScanConfig, Scanner

STATIC = Path(__file__).resolve().parent / "static"
RECENT_SWEEPS = 150  # replayed to a newly connected browser so its waterfall is not empty
KEEPALIVE_S = 15.0


def _sse(kind: str, data) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


class Hub:
    """Fan-out of SSE messages to every connected browser."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients: list[queue.Queue] = []
        self.state: dict | None = None
        self.recent: collections.deque = collections.deque(maxlen=RECENT_SWEEPS)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=RECENT_SWEEPS + 100)
        with self.lock:
            if self.state is not None:
                q.put_nowait(_sse("state", self.state))
            for msg in self.recent:
                q.put_nowait(msg)
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, kind: str, data) -> None:
        msg = _sse(kind, data)
        with self.lock:
            if kind == "state":
                self.state = data
            elif kind == "sweep":
                self.recent.append(msg)
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:  # a stalled browser: drop it, EventSource will reconnect
                    self.clients.remove(q)

    def reset_recent(self) -> None:
        with self.lock:
            self.recent.clear()


class Runner(threading.Thread):
    """Owns the Scanner. Applies queued UI commands between sweeps."""

    def __init__(self, scanner: Scanner, hub: Hub, mode: str, rec_dir: Path, log_path: Path | None = None):
        super().__init__(name="radio", daemon=True)
        self.scanner, self.hub, self.mode, self.rec_dir = scanner, hub, mode, rec_dir
        self.cmds: queue.Queue = queue.Queue()
        self.running = True
        self.stopping = threading.Event()
        self.rec_files: list[str] = []
        self.log = None
        if log_path is not None:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            self.log = open(log_path, "a", encoding="utf-8")

    def _write_log(self, kind: str, data: dict) -> None:
        if self.log is not None:
            self.log.write(json.dumps({"kind": kind, **{k: v for k, v in data.items() if k != "rel"}}) + "\n")
            self.log.flush()

    def post(self, cmd: dict) -> None:
        self.cmds.put(cmd)

    def stop(self) -> None:
        self.stopping.set()
        self.cmds.put({"op": "noop"})

    def status(self, msg: str, level: str = "info") -> None:
        print(f"[{level}] {msg}")
        data = {"t": round(time.time(), 3), "level": level, "msg": msg}
        self.hub.publish("status", data)
        self._write_log("status", data)

    def publish_state(self) -> None:
        st = self.scanner.state()
        st.update(mode=self.mode, running=self.running, rec_files=self.rec_files)
        self.hub.publish("state", st)

    def _apply(self, cmd: dict) -> None:
        op = cmd.get("op")
        if op == "config":
            self.status("applying settings..." + (" (auto-gain sweeps first)" if cmd["config"].auto_gain else ""))
            self.scanner.configure(cmd["config"])
            self.hub.reset_recent()
            c = self.scanner.config
            self.status(f"band {c.band[0]:.0f}-{c.band[1]:.0f} MHz, W={c.w}, "
                        f"windows {', '.join(ws.window.label() for ws in self.scanner.windows)}")
        elif op == "start":
            self.running = True
        elif op == "pause":
            self.running = False
        elif op == "clear":
            self.scanner.clear()
            self.status("tracks and sweep buffers cleared")
        elif op == "record":
            if cmd.get("on"):
                self.rec_files = self.scanner.start_recording(self.rec_dir, cmd.get("tag", ""))
                self.status("recording to " + ", ".join(Path(f).name for f in self.rec_files))
            else:
                done = self.scanner.stop_recording()
                self.rec_files = []
                self.status("recording stopped: " + ("; ".join(done) or "nothing recorded"))
        else:
            return
        self.publish_state()

    def run(self) -> None:
        self.publish_state()
        while not self.stopping.is_set():
            try:
                while True:
                    self._apply(self.cmds.get_nowait())
            except queue.Empty:
                pass
            except Exception as ex:  # bad command or radio error while applying it
                self.status(f"{type(ex).__name__}: {ex}", "error")
                self.publish_state()
            if not self.running:
                try:
                    self._apply(self.cmds.get(timeout=0.25))
                except queue.Empty:
                    pass
                continue
            try:
                ev = self.scanner.step()
            except EOFError:
                self.running = False
                self.status("end of recording", "warn")
                self.publish_state()
                continue
            except Exception as ex:
                self.running = False
                self.status(f"scan stopped: {type(ex).__name__}: {ex}", "error")
                self.publish_state()
                continue
            self.hub.publish("sweep", ev)
            self._write_log("sweep", ev)
        for f in self.scanner.close():
            print("recorded", f)
        if self.log is not None:
            self.log.close()


def make_handler(hub: Hub, runner: Runner):
    class Handler(BaseHTTPRequestHandler):
        server_version = "bandera"

        def log_message(self, fmt, *args):  # keep the console for scan status
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/events":
                self._events()
            elif path == "/api/state":
                self._json(200, hub.state or {})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/api/config":
                    cur = runner.scanner.config
                    cfg = ScanConfig(tuple(body.get("band", cur.band)), body.get("w", cur.w),
                                     body.get("gain", cur.gain), body.get("auto_gain", cur.auto_gain),
                                     body.get("detect_wifi", cur.detect_wifi),
                                     body.get("whole_window", cur.whole_window)).validated()
                    runner.post({"op": "config", "config": cfg})
                elif self.path == "/api/control":
                    op = body.get("op")
                    if op not in ("start", "pause", "clear", "record"):
                        raise ValueError(f"unknown op {op!r}")
                    tag = "".join(ch for ch in str(body.get("tag", "")) if ch.isalnum() or ch in "-_.")[:60]
                    runner.post({"op": op, "on": bool(body.get("on")), "tag": tag})
                else:
                    return self._send(404, b"not found", "text/plain")
                self._json(202, {"ok": True})
            except (ValueError, TypeError) as ex:
                self._json(400, {"ok": False, "error": str(ex)})

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = hub.subscribe()
            try:
                while True:
                    try:
                        msg = q.get(timeout=KEEPALIVE_S)
                    except queue.Empty:
                        msg = b": keepalive\n\n"
                    self.wfile.write(msg)
                    self.wfile.flush()
            except OSError:  # browser went away
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def lan_addresses() -> list[str]:
    try:
        return sorted({ip for *_, (ip, *_) in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                       if not ip.startswith("127.")})
    except OSError:
        return []


def serve(scanner: Scanner, hub: Hub, host: str, port: int, mode: str, rec_dir: Path,
          log_path: Path | None = None) -> None:
    runner = Runner(scanner, hub, mode, rec_dir, log_path)
    if log_path is not None:
        print(f"detection log: {log_path}")
    scanner.status = runner.status
    httpd = ThreadingHTTPServer((host, port), make_handler(hub, runner))
    httpd.daemon_threads = True
    runner.start()
    port = httpd.server_address[1]
    print(f"bandera web UI: http://127.0.0.1:{port}/")
    if host in ("0.0.0.0", ""):
        for ip in lan_addresses():
            print(f"  on the LAN:   http://{ip}:{port}/   (no authentication: anyone on this network can control the scan)")
    print("Ctrl+C to stop")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        runner.stop()
        runner.join(timeout=10)
