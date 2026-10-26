/* Host test for the pure Arinst sweep logic. GPL-3.0-or-later.
 * Build: gcc -I../src/arinst -o t test_arinst_sweep.c ../src/arinst/arinst_sweep.c -lm && ./t
 *
 * Simulates the C5 radio: a synthetic spectrum with a strong analog carrier at 5843 MHz.
 * Runs the full plan->capture->fill->packet path, then parses the packet exactly as the
 * Frontera app (c8/l.java) and pyarinst do, and checks the carrier and floor come back right.
 */
#include "arinst_sweep.h"

#include <math.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
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
 * psd_compute conjugates the raw inverted I/Q, then FFT-shifts the output:
 * bin N/2+kk holds physical LO+kk*bin. Verified by raw hardware captures at three LOs.
 * cal_offset is the dBFS->dBm shift we will undo in arinst_fill_hop, so invert it here. */
static void fake_capture(uint64_t lo_hz, float cal_offset_db, uint32_t *power, uint8_t *valid) {
    double bin_hz = RATE / NFFT;
    int guard = 3;
    for (int k = 0; k < NFFT; k++) {
        int kk = k - NFFT / 2;                    /* psd_compute returns FFT-shifted bins */
        double f = (double)lo_hz + kk * bin_hz;
        double dbfs = true_dbm(f) - cal_offset_db;  /* dBm -> dBFS (inverse of fill's +offset) */
        double code = pow(10.0, dbfs / 10.0) * FULLSCALE_CODE; /* linear power in psd counts */
        power[k] = code > 4294967295.0 ? 4294967295u : (uint32_t)(code + 0.5);
        valid[k] = abs(kk) > guard && fabs(kk * bin_hz) <= RATE * USABLE / 2;
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
    check(arinst_command("scn200 1 2 3") == 0 && arinst_command("testing") == 0,
          "command names require a token boundary");
    check(!arinst_parse(NULL).ok && !arinst_parse("scn20 1 2 1").ok,
          "missing request fields rejected");
    check(!arinst_parse("scn20 -1 2 1 200 20 10700000 10000 0").ok &&
          !arinst_parse("scn20 0 18446744073709551616 1 200 20 10700000 10000 0").ok,
          "negative and overflowing integers rejected");
    check(!arinst_parse("scn20 1 2 0 200 20 10700000 10000 0").ok &&
          !arinst_parse("scn20 2 1 1 200 20 10700000 10000 0").ok &&
          !arinst_parse("scn20 0 2048 1 200 20 10700000 10000 0").ok &&
          !arinst_parse("scn20 1 2 1 200 20 10700000 10000 0 garbage").ok,
          "invalid range, oversized grid and trailing input rejected");
    arinst_req_t one = arinst_parse("scn22 5805000000 5805000000 3000000 200 1 10700000 10000 4");
    check(one.ok && one.points == 1 && one.tracking && one.index == 4, "single-point tracking request");

    printf("plan (4200-5997 MHz, only 5150-5955 locks)\n");
    const double DEAD = 7e6; /* C5 DC/LO-leakage dead zone radius */
    arinst_hop_t hops[ARINST_MAX_HOPS];
    int covered = 0;
    int nhops = arinst_plan(&req, RATE, USABLE, DEAD, ARINST_C5_BANDS, ARINST_C5_NBANDS,
                            hops, ARINST_MAX_HOPS, &covered);
    check(nhops > 0 && nhops < ARINST_MAX_HOPS, "some hops planned");
    /* 5150..5955 = 805 MHz, good sideband ~ (16-1-7)=8 MHz/hop -> ~100 hops;
     * every in-band point is covered: (5955-5151)/3 + 1 = 269 pts. */
    check(covered > 240 && covered < 290, "covered point count sane");
    int plan_ok = 1, total = 0;
    for (int h = 0; h < nhops; h++) {
        plan_ok &= hops[h].count > 0 && hops[h].lo_hz % 1000000 == 0;
        uint64_t f0 = req.start_hz + (uint64_t)hops[h].first * req.step_hz;
        plan_ok &= f0 >= 5150000000ULL && f0 <= 5955000000ULL;
        double aoff = fabs((double)f0 - (double)hops[h].lo_hz);
        plan_ok &= aoff >= DEAD && aoff <= RATE * USABLE / 2.0 - 1e6;
        for (int j = 0; j < hops[h].count; j++) {
            double f = req.start_hz + (uint64_t)(hops[h].first + j) * req.step_hz;
            double off = fabs(f - hops[h].lo_hz);
            plan_ok &= off >= DEAD && off <= RATE * USABLE / 2.0 - 1e6;
            total++;
        }
    }
    check(plan_ok && total == 269 && covered == total, "all 269 supported points use whole-MHz LOs and valid offsets");

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
    check(!memcmp(pkt, "\r\nscn20 4200000000 7\r\n", 22), "official leading CRLF and exact header");
    uint8_t exact[2 * ARINST_MAX_POINTS + 64];
    memset(exact, 0xa5, sizeof(exact));
    check(arinst_build_packet(&req, dbm, 123, exact, len) == len && exact[len] == 0xa5,
          "exact-capacity packet has no trailing NUL write");
    memset(exact, 0xa5, sizeof(exact));
    check(arinst_build_packet(&req, dbm, 123, exact, len - 1) == 0 && exact[0] == 0xa5,
          "short packet buffer rejected without writing");
    check(arinst_build_packet(&one, dbm, INT_MAX, exact, sizeof(exact)) > 0 &&
          !memcmp(exact, "\r\nscn22 ", 8), "tracking reply echoes tag and accepts longest elapsed field");

    printf("round-trip through the app parser\n");
    double start_mhz = 0;
    int idx = -1, n;
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
    check(arinst_word(NAN) == arinst_word(ARINST_FLOOR_DBM) && arinst_word(-1e30f) == 2047,
          "nonfinite and extreme levels encode safely");

    /* Independent PSD layout fixture: psd_compute places DC at N/2 and, after
     * conjugating the C5 raw I/Q, a tone above the LO at a higher output index.
     * These explicit indices catch both the old natural-order and sign bugs. */
    printf("independent FFT-shifted PSD fixture at 40 and 80 MS/s\n");
    arinst_req_t two = arinst_parse("scn20 5790000000 5810000000 20000000 200 20 10700000 10000 0");
    arinst_hop_t fixed = {.lo_hz = 5800000000ULL, .first = 0, .count = 2};
    for (int r = 0; r < 2; r++) {
        memset(power, 0, sizeof(power));
        memset(valid, 0, sizeof(valid));
        int delta = r ? 128 : 256;
        power[512 - delta] = 1073742;  /* -30 dBFS at 5790 MHz */
        power[512 + delta] = 10737418; /* -20 dBFS at 5810 MHz */
        valid[512 - delta] = valid[512 + delta] = 1;
        float levels[2] = {-110, -110};
        arinst_fill_hop(&two, &fixed, r ? 80e6 : 40e6, NFFT, power, TONE_SCALE, valid, -22, levels);
        check(fabs(levels[0] + 52) < .01 && fabs(levels[1] + 42) < .01,
              r ? "80 MS/s shifted indices and RF orientation" : "40 MS/s shifted indices and RF orientation");
    }
    arinst_req_t low = arinst_parse("scn20 2200000000 3997000000 3000000 200 20 10700000 10000 0");
    int low_hops = arinst_plan(&low, 80e6, .4, DEAD, ARINST_C5_BANDS, ARINST_C5_NBANDS,
                              hops, ARINST_MAX_HOPS, &covered);
    check(low_hops > 0 && covered == 34, "2.4 GHz window covers all 34 supported points");

    printf("S3 band limits\n");
    int s3_hops = arinst_plan(&low, 80e6, .4, DEAD, ARINST_S3_BANDS, ARINST_S3_NBANDS,
                             hops, ARINST_MAX_HOPS, &covered);
    check(s3_hops == low_hops && covered == 34, "S3 covers all 34 supported 2.4 GHz points");
    int s3_plan_ok = 1;
    for (int h = 0; h < s3_hops; h++)
        s3_plan_ok &= hops[h].lo_hz >= 2400000000ULL && hops[h].lo_hz <= 2500000000ULL;
    check(s3_plan_ok, "S3 LOs stay inside the 2.4 GHz band");
    check(arinst_plan(&req, 80e6, .4, DEAD, ARINST_S3_BANDS, ARINST_S3_NBANDS,
                      hops, ARINST_MAX_HOPS, &covered) == 0 && covered == 0,
          "S3 never tunes the 4200-5997 MHz request");
    for (int i = 0; i < req.points; i++) dbm[i] = ARINST_FLOOR_DBM;
    len = arinst_build_packet(&req, dbm, 0, pkt, sizeof(pkt));
    n = parse_packet(pkt, len, &start_mhz, &idx, got, ARINST_MAX_POINTS);
    int s3_floor_ok = n == 600;
    for (int i = 0; i < n; i++) s3_floor_ok &= got[i] == ARINST_FLOOR_DBM;
    check(s3_floor_ok, "unsupported S3 5 GHz request still returns 600 floor points");

    arinst_req_t discovery = arinst_parse("scn20 100000000 200000000 10000000 200 1 10700000 10000 0");
    check(discovery.ok && discovery.points == 11 &&
          arinst_plan(&discovery, 80e6, .4, DEAD, ARINST_C5_BANDS, ARINST_C5_NBANDS,
                      hops, ARINST_MAX_HOPS, &covered) == 0 && covered == 0,
          "app discovery grid succeeds with 11 floor points");
    check(arinst_plan(&low, 10e6, .4, DEAD, ARINST_C5_BANDS, ARINST_C5_NBANDS,
                      hops, ARINST_MAX_HOPS, &covered) == 0,
          "invalid passband geometry does not create masked hops");

    printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
    return failures ? 1 : 0;
}
