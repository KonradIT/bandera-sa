/* Arinst analyzer personality for the ESP32-C5. GPL-3.0-or-later.
 *
 * Drives widscan58's radio + psd to answer Arinst `scn20` sweeps, so the Frontera
 * app (via the modded bridge that accepts the C5's USB id) sees a Frontera/Arinst SA.
 * The sweep math and packet format live in arinst_sweep.c (host-tested).
 */
#include "arinst.h"

#include <math.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "arinst_sweep.h"
#include "psd.h"
#include "radio.h"

#define ARINST_RATE_CODE 1       /* caps.rates_hz[1] = 40 MS/s: ~32 MHz usable, good SNR */
#define ARINST_USABLE_FRAC 0.80
#define ARINST_NFFT 1024         /* PSD_MAX_FFT on the C5 */
#define ARINST_SEGMENTS 4        /* capture = NFFT*SEGMENTS samples, Welch-averaged */
#define ARINST_SETTLE_US 100
#define ARINST_DC_GUARD_BINS 3

static float s_cal_db = -30.0f;  /* placeholder; floor-relative detection is insensitive to it */

/* Big sweep buffers live on the heap: static BSS here would collide with the reserved
 * RF capture SRAM bank (widscan58 sram_guard.ld: "RF dump bank overlaps BSS"). */
static float *s_dbm;             /* [ARINST_MAX_POINTS] */
static arinst_hop_t *s_hops;     /* [ARINST_MAX_HOPS] */
static uint8_t *s_pkt;           /* [2*ARINST_MAX_POINTS + 64] */

static bool bufs_ready(void) {
    if (s_dbm) return true;
    s_dbm = heap_caps_malloc(sizeof(float) * ARINST_MAX_POINTS, MALLOC_CAP_8BIT);
    s_hops = heap_caps_malloc(sizeof(arinst_hop_t) * ARINST_MAX_HOPS, MALLOC_CAP_8BIT);
    s_pkt = heap_caps_malloc(2 * ARINST_MAX_POINTS + 64, MALLOC_CAP_8BIT);
    return s_dbm && s_hops && s_pkt;
}

void arinst_set_cal(float offset_db) { s_cal_db = offset_db; }
float arinst_get_cal(void) { return s_cal_db; }

static void write_all(const uint8_t *data, size_t len) {
    size_t sent = 0;
    while (sent < len) {
        int n = usb_serial_jtag_write_bytes(data + sent, len - sent, pdMS_TO_TICKS(1000));
        if (n <= 0) break; /* host went away */
        sent += (size_t)n;
    }
}

static void write_str(const char *s) { write_all((const uint8_t *)s, strlen(s)); }

/* One sweep: tune/capture/psd per hop, fill dBm, emit the Arinst packet. */
static void do_scan(const arinst_req_t *req) {
    if (!bufs_ready()) return;
    for (int i = 0; i < req->points; i++) s_dbm[i] = ARINST_FLOOR_DBM;

    arinst_hop_t *hops = s_hops;
    int covered = 0;
    int nhops = arinst_plan(req, (double)radio_caps()->rates_hz[ARINST_RATE_CODE], ARINST_USABLE_FRAC,
                            ARINST_C5_BANDS, ARINST_C5_NBANDS, hops, ARINST_MAX_HOPS, &covered);

    int64_t t0 = esp_timer_get_time();
    double rate = (double)radio_caps()->rates_hz[ARINST_RATE_CODE];
    unsigned nsamp = ARINST_NFFT * ARINST_SEGMENTS;
    if (nsamp > radio_caps()->max_samples) nsamp = radio_caps()->max_samples;

    for (int h = 0; h < nhops; h++) {
        uint32_t lo_khz = (uint32_t)((hops[h].lo_hz + 500) / 1000); /* whole kHz; PHY takes whole MHz */
        radio_tune(lo_khz);
        const uint32_t *words = NULL;
        uint32_t cap_us = 0;
        if (radio_capture(nsamp, ARINST_RATE_CODE, ARINST_SETTLE_US, &words, &cap_us) != ESP_OK || !words)
            continue;
        psd_result_t psd;
        if (psd_compute(words, nsamp, ARINST_NFFT, ARINST_SEGMENTS, (float)rate, true,
                        (float)(rate * ARINST_USABLE_FRAC / 2.0), ARINST_DC_GUARD_BINS, NULL, &psd) != ESP_OK)
            continue;
        arinst_fill_hop(req, &hops[h], rate, ARINST_NFFT, psd.p, psd.scale, psd.valid, s_cal_db, s_dbm);
    }

    int elapsed_ms = (int)((esp_timer_get_time() - t0) / 1000);
    size_t len = arinst_build_packet(req, s_dbm, elapsed_ms, s_pkt, 2 * ARINST_MAX_POINTS + 64);
    if (len) write_all(s_pkt, len);
}

bool arinst_service(const char *line) {
    switch (arinst_command(line)) {
    case 's': {
        arinst_req_t req = arinst_parse(line);
        if (req.ok) do_scan(&req);
        return true;
    }
    case 't':
        /* addr 0 = analyzer online (c8/l.java test branch: "test <addr> complete <gen>") */
        write_str("test 0 complete 0\r\n");
        return true;
    case 'g': /* no generator hardware: the app treats a missing reply as "failed", which is fine */
    case 'f':
    case 'a':
        return true;
    default:
        return false;
    }
}
