/* Host test for the pure Arinst sweep logic. GPL-3.0-or-later.
 * Build: gcc -I../src/arinst -o t test_arinst_sweep.c ../src/arinst/arinst_sweep.c -lm && ./t
 *
 * Simulates the C5 radio: a synthetic spectrum with a strong analog carrier at 5843 MHz.
 * Runs the full plan->capture->fill->packet path, then parses the packet exactly as the
 * Frontera app (c8/l.java) and pyarinst do, and checks the carrier and floor come back right.
 */
#include "arinst_sweep.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define RATE 40000000.0
#define USABLE 0.8
#define NFFT 1024
/* psd returns large integer power codes and a tiny scale so code*scale is normalised
 * (full-scale tone -> 1.0). Model that: full scale = 2^30 counts. */
#define FULLSCALE_CODE 1073741824.0 /* 2^30 */
#define TONE_SCALE (1.0 / FULLSCALE_CODE)

static int failures;
static void check(int ok, const char *what) {
    printf("  %s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) failures++;
}

/* True analog FPV carrier: ~ -40 dBm around 5843 MHz, noise floor -100 dBm. */
static double true_dbm(double f_hz) {
    double mhz = f_hz / 1e6;
    if (fabs(mhz - 5843.0) <= 2.0) return -40.0;
    if (fabs(mhz - 5843.0) <= 8.0) return -62.0; /* video shoulders */
    return -100.0;
}

/* Fake psd for one LO: fill nfft linear power bins from true_dbm, mark a DC guard invalid.
 * cal_offset is the dBFS->dBm shift we will undo in arinst_fill_hop, so invert it here. */
static void fake_capture(uint64_t lo_hz, float cal_offset_db, uint32_t *power, uint8_t *valid) {
    double bin_hz = RATE / NFFT;
    int guard = 3;
    for (int k = 0; k < NFFT; k++) {
        int kk = k <= NFFT / 2 ? k : k - NFFT;      /* signed bin */
        double f = (double)lo_hz + kk * bin_hz;
        double dbfs = true_dbm(f) - cal_offset_db;  /* dBm -> dBFS (inverse of fill's +offset) */
        double code = pow(10.0, dbfs / 10.0) * FULLSCALE_CODE; /* linear power in psd counts */
        power[k] = code > 4294967295.0 ? 4294967295u : (uint32_t)(code + 0.5);
        valid[k] = (abs(kk) <= guard) ? 0 : 1;      /* DC guard near the LO */
    }
}

/* Parse the Arinst packet like the app: header, big-endian words to FF FF, dBm=(800-(w&0x7ff))/10. */
static int parse_packet(const uint8_t *pkt, size_t len, double *start_mhz, int *idx, float *dbm, int maxn) {
    const char *s = (const char *)pkt;
    const char *h = strstr(s, "scn20 ");
    if (!h) return -1;
    unsigned long long start;
    int index;
    if (sscanf(h, "scn20 %llu %d", &start, &index) != 2) return -1;
    *start_mhz = start / 1e6;
    *idx = index;
    const char *nl = strstr(h, "\r\n");
    if (!nl) return -1;
    size_t bstart = (size_t)(nl - s) + 2;
    /* find FF FF */
    size_t c = 0;
    for (size_t i = bstart; i + 1 < len; i++)
        if (pkt[i] == 0xFF && pkt[i + 1] == 0xFF) { c = i; break; }
    if (!c) return -1;
    int n = (int)((c - bstart) / 2);
    if (n > maxn) n = maxn;
    for (int i = 0; i < n; i++) {
        int w = (pkt[bstart + 2 * i] << 8) | pkt[bstart + 2 * i + 1];
        dbm[i] = (float)((800 - (w & 0x7FF)) / 10.0);
    }
    return n;
}

int main(void) {
    const float CAL = -22.0f; /* arbitrary dBFS->dBm offset, must round-trip */

    printf("parse\n");
    arinst_req_t req = arinst_parse("scn20 4200000000 5997000000 3000000 200 20 10700000 10000 7");
    check(req.ok, "scn20 parsed");
    check(req.points == 600, "600 points");
    check(req.index == 7, "index 7");
    check(arinst_command("scn20 1 2 3") == 's' && arinst_command("test") == 't'
          && arinst_command("gon 1") == 'g' && arinst_command("scf 1 2") == 'f', "command classify");

    printf("plan (4200-5997 MHz, only 5150-5955 locks)\n");
    arinst_hop_t hops[ARINST_MAX_HOPS];
    int covered = 0;
    int nhops = arinst_plan(&req, RATE, USABLE, ARINST_C5_BANDS, ARINST_C5_NBANDS,
                            hops, ARINST_MAX_HOPS, &covered);
    check(nhops > 0 && nhops < ARINST_MAX_HOPS, "some hops planned");
    /* 5150..5955 = 805 MHz / (40*0.8=32 MHz) ~ 26 hops; covered ~ (5955-5150)/3 ~ 268 pts */
    check(covered > 240 && covered < 290, "covered point count sane");
    for (int h = 0; h < nhops; h++) {
        check(hops[h].count > 0, "hop non-empty");
        uint64_t f0 = req.start_hz + (uint64_t)hops[h].first * req.step_hz;
        check(f0 >= 5150000000ULL && f0 <= 5955000000ULL, "hop points in the 5 GHz band");
    }

    printf("fill + packet\n");
    static float dbm[ARINST_MAX_POINTS];
    for (int i = 0; i < req.points; i++) dbm[i] = ARINST_FLOOR_DBM; /* floor outside bands */
    uint32_t power[NFFT];
    uint8_t valid[NFFT];
    for (int h = 0; h < nhops; h++) {
        fake_capture(hops[h].lo_hz, CAL, power, valid);
        arinst_fill_hop(&req, &hops[h], RATE, NFFT, power, TONE_SCALE, valid, CAL, dbm);
    }
    static uint8_t pkt[2 * ARINST_MAX_POINTS + 64];
    size_t len = arinst_build_packet(&req, dbm, 123, pkt, sizeof(pkt));
    check(len > (size_t)req.points * 2, "packet built");

    printf("round-trip through the app parser\n");
    double start_mhz;
    int idx, n;
    static float got[ARINST_MAX_POINTS];
    n = parse_packet(pkt, len, &start_mhz, &idx, got, ARINST_MAX_POINTS);
    check(n == 600, "parser read 600 points");
    check(fabs(start_mhz - 4200.0) < 1e-6, "start 4200 MHz");
    check(idx == 7, "index preserved");

    int kcar = (int)llround((5843e6 - 4200e6) / 3e6);
    printf("    carrier bin %d (%.0f MHz) = %.1f dBm (expect ~ -40)\n", kcar, 4200.0 + 3.0 * kcar, got[kcar]);
    check(fabs(got[kcar] - (-40.0)) <= 1.5, "carrier ~ -40 dBm at 5843 MHz");

    int kfloor_low = (int)llround((4500e6 - 4200e6) / 3e6); /* below the 5 GHz band: uncovered */
    check(got[kfloor_low] <= -100.0, "uncovered 4500 MHz at floor");

    int kfloor_in = (int)llround((5500e6 - 4200e6) / 3e6);  /* in band, away from carrier */
    printf("    in-band floor at 5500 MHz = %.1f dBm (expect ~ -100)\n", got[kfloor_in]);
    check(fabs(got[kfloor_in] - (-100.0)) <= 1.5, "in-band noise floor ~ -100 dBm");

    /* word/round-trip exactness on a few values */
    check(arinst_word(-40.0f) == (uint16_t)(800 + 400), "word(-40) = 1200");
    check(arinst_word(80.0f) == 0, "word clamps high");
    check(arinst_word(-124.8f) == 2047, "word clamps low");

    printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
    return failures ? 1 : 0;
}
