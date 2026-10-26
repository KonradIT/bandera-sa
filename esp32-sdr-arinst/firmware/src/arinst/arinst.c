/* Arinst analyzer personality for the ESP32-C5. GPL-3.0-or-later.
 *
 * Drives widscan58's radio + psd to answer Arinst `scn20` sweeps, so the Frontera
 * app (via the modded bridge that accepts the C5's USB id) sees a Frontera/Arinst SA.
 * The sweep math and packet format live in arinst_sweep.c (host-tested).
 */
#include "arinst.h"

#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "arinst_sweep.h"
#include "psd.h"
#include "radio.h"
#include "scanner.h"
#include "link.h"

#define ARINST_RATE_CODE 0       /* 80 MS/s keeps adjacent-hop carriers out of Nyquist aliases. */
#define ARINST_USABLE_FRAC 0.40  /* Retain the flat inner +/-16 MHz of the analog filter. */
#define ARINST_NFFT 1024         /* PSD_MAX_FFT on the C5 */
#define ARINST_SEGMENTS 4        /* capture = NFFT*SEGMENTS samples, Welch-averaged */
#define ARINST_SETTLE_US 100
#define ARINST_DC_GUARD_BINS 3
/* The C5's direct-conversion RX is blind within ~+-7 MHz of the LO (DC offset / LO
 * leakage); the planner serves every point from a good sideband outside this. */
#define ARINST_DC_DEAD_HZ 7e6

static float s_cal_db = -30.0f;  /* placeholder; floor-relative detection is insensitive to it */

/* Big sweep buffers live on the heap: static BSS here would collide with the reserved
 * RF capture SRAM bank (widscan58 sram_guard.ld: "RF dump bank overlaps BSS"). */
static float *s_dbm;             /* [ARINST_MAX_POINTS] */
static arinst_hop_t *s_hops;     /* [ARINST_MAX_HOPS] */
static uint8_t *s_pkt;           /* [2*ARINST_MAX_POINTS + 64] */

static int s_points;
static bool bufs_ready(int points) {
    if (s_points >= points) return true;
    heap_caps_free(s_dbm);
    heap_caps_free(s_hops);
    heap_caps_free(s_pkt);
    s_points = 0;
    s_dbm = heap_caps_malloc(sizeof(float) * points, MALLOC_CAP_8BIT);
    int nhops = points < ARINST_MAX_HOPS ? points : ARINST_MAX_HOPS;
    s_hops = heap_caps_malloc(sizeof(arinst_hop_t) * nhops, MALLOC_CAP_8BIT);
    s_pkt = heap_caps_malloc(2 * points + 64, MALLOC_CAP_8BIT);
    if (s_dbm && s_hops && s_pkt) { s_points = points; return true; }
    /* A partial allocation must never make the next request look ready. */
    heap_caps_free(s_dbm); s_dbm = NULL;
    heap_caps_free(s_hops); s_hops = NULL;
    heap_caps_free(s_pkt); s_pkt = NULL;
    return false;
}

void arinst_set_cal(float offset_db) { s_cal_db = offset_db; }
float arinst_get_cal(void) { return s_cal_db; }

static void write_str(const char *s) { link_text(s); }

/* One sweep: tune/capture/psd per hop, fill dBm, emit the Arinst packet. */
void arinst_scan(const arinst_req_t *req) {
    if (!bufs_ready(req->points)) { write_str("error memory\r\n"); return; }
    for (int i = 0; i < req->points; i++) s_dbm[i] = ARINST_FLOOR_DBM;

    arinst_hop_t *hops = s_hops;
    int covered = 0;
    int nhops = arinst_plan(req, (double)radio_caps()->rates_hz[ARINST_RATE_CODE], ARINST_USABLE_FRAC,
                            ARINST_DC_DEAD_HZ, ARINST_C5_BANDS, ARINST_C5_NBANDS, hops,
                            req->points < ARINST_MAX_HOPS ? req->points : ARINST_MAX_HOPS, &covered);

    int64_t t0 = esp_timer_get_time();
    double rate = (double)radio_caps()->rates_hz[ARINST_RATE_CODE];
    unsigned nsamp = ARINST_NFFT * ARINST_SEGMENTS;
    if (nsamp > radio_caps()->max_samples) nsamp = radio_caps()->max_samples;

    int band = -1;
    for (int h = 0; h < nhops; h++) {
        uint32_t lo_khz = (uint32_t)((hops[h].lo_hz + 500) / 1000); /* whole kHz; PHY takes whole MHz */
        int next_band = lo_khz < 3000000 ? 0 : 1;
        if (band != next_band) {
            /* Full per-LO DC calibration is ~45 ms and thrashes a 16-entry
             * cache on a wide sweep. Calibrate once per band; the RX gain
             * table still rebuilds for EVERY LO, retaining frequency tracking. */
            radio_set_recal(RADIO_RECAL_ONCE, next_band == 0 ? 2450000 : 5787000);
            band = next_band;
        }
        if (radio_tune(lo_khz) != ESP_OK) continue;
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
    size_t len = arinst_build_packet(req, s_dbm, elapsed_ms, s_pkt, 2 * s_points + 64);
    if (len) link_raw(s_pkt, len);
}

bool arinst_service(const char *line) {
    switch (arinst_command(line)) {
    case 's': {
        arinst_req_t req = arinst_parse(line);
        if (req.ok) {
            scan_request_t scan = {.type = SCAN_REQ_ARINST, .arinst = req,
                                   .completion_task = xTaskGetCurrentTaskHandle()};
            if (!scanner_request(&scan)) write_str("error busy\r\n");
            else ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        }
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
