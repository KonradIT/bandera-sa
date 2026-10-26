/* Arinst protocol + sweep planning (pure logic). GPL-3.0-or-later. See arinst_sweep.h. */
#include "arinst_sweep.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The C5 locks the PLL near 2.4 and 5 GHz. 5 GHz range matches widscan58's proven FPV
 * sweep (R1..R8 and margins); 2.4 GHz covers the ISM band. */
const arinst_band_t ARINST_C5_BANDS[] = {
    {2400000000ULL, 2500000000ULL},
    {5150000000ULL, 5955000000ULL},
};
const int ARINST_C5_NBANDS = (int)(sizeof(ARINST_C5_BANDS) / sizeof(ARINST_C5_BANDS[0]));

char arinst_command(const char *line) {
    if (!line) return 0;
    while (*line == ' ' || *line == '\r' || *line == '\n') line++;
    if (!strncmp(line, "scn20", 5) || !strncmp(line, "scn22", 5)) return 's';
    if (!strncmp(line, "test", 4)) return 't';
    if (!strncmp(line, "gon", 3) || !strncmp(line, "goff", 4) || !strncmp(line, "gof", 3)) return 'g';
    if (!strncmp(line, "scf", 3)) return 'f';
    if (!strncmp(line, "sga", 3)) return 'a';
    return 0;
}

arinst_req_t arinst_parse(const char *line) {
    arinst_req_t r;
    memset(&r, 0, sizeof(r));
    /* "scn20 start stop step 200 samples 10700000 att idx" */
    char tag[8];
    unsigned long long start = 0, stop = 0, step = 0;
    long c200 = 0, samples = 0, ref = 0, att = 0, idx = 0;
    int got = sscanf(line, "%7s %llu %llu %llu %ld %ld %ld %ld %ld",
                     tag, &start, &stop, &step, &c200, &samples, &ref, &att, &idx);
    if (got < 4 || step == 0 || stop <= start) return r;
    r.start_hz = start;
    r.stop_hz = stop;
    r.step_hz = step;
    r.index = (got >= 9) ? (int)idx : 0;
    long long pts = (long long)((stop - start) / step) + 1;
    if (pts < 1) return r;
    if (pts > ARINST_MAX_POINTS) pts = ARINST_MAX_POINTS;
    r.points = (int)pts;
    r.ok = true;
    return r;
}

static bool in_any_band(uint64_t f, const arinst_band_t *bands, int nbands) {
    for (int i = 0; i < nbands; i++)
        if (f >= bands[i].lo_hz && f <= bands[i].hi_hz) return true;
    return false;
}

int arinst_plan(const arinst_req_t *req, double rate_hz, double usable_frac,
                const arinst_band_t *bands, int nbands, arinst_hop_t *hops, int max_hops, int *covered) {
    int nhops = 0, cov = 0;
    double usable = rate_hz * usable_frac;      /* span one capture serves, centred on the LO */
    int i = 0;
    while (i < req->points && nhops < max_hops) {
        uint64_t f = req->start_hz + (uint64_t)i * req->step_hz;
        if (!in_any_band(f, bands, nbands)) { i++; continue; }
        /* Start a hop centred so this first covered point sits half a usable-span in. */
        uint64_t lo = f + (uint64_t)(usable / 2.0) - (uint64_t)req->step_hz;
        double win_lo = (double)lo - usable / 2.0, win_hi = (double)lo + usable / 2.0;
        arinst_hop_t *h = &hops[nhops];
        h->lo_hz = lo;
        h->first = i;
        h->count = 0;
        /* Absorb every consecutive in-band point that still falls in this window. */
        while (i < req->points) {
            uint64_t g = req->start_hz + (uint64_t)i * req->step_hz;
            if (!in_any_band(g, bands, nbands)) break;
            if ((double)g > win_hi) break;
            if ((double)g < win_lo) break;
            h->count++;
            cov++;
            i++;
        }
        if (h->count > 0) nhops++;
        else i++; /* safety: never stall */
    }
    if (covered) *covered = cov;
    return nhops;
}

float arinst_dbfs(uint32_t power_code, double tone_scale) {
    double lin = (double)power_code * tone_scale;
    return lin > 0.0 ? (float)(10.0 * log10(lin)) : ARINST_FLOOR_DBM;
}

void arinst_fill_hop(const arinst_req_t *req, const arinst_hop_t *hop, double rate_hz, int nfft,
                     const uint32_t *power, double tone_scale, const uint8_t *valid,
                     float cal_offset_db, float *out) {
    double bin_hz = rate_hz / (double)nfft;
    for (int j = 0; j < hop->count; j++) {
        int idx = hop->first + j;
        uint64_t f = req->start_hz + (uint64_t)idx * req->step_hz;
        double off = (double)f - (double)hop->lo_hz;         /* baseband offset from the LO */
        int k = (int)lround(off / bin_hz);                  /* natural FFT bin (negative wraps) */
        if (k < 0) k += nfft;
        if (k < 0 || k >= nfft) continue;
        if (valid && !valid[k]) continue;                   /* DC guard / out-of-band bin */
        out[idx] = arinst_dbfs(power[k], tone_scale) + cal_offset_db;
    }
}

uint16_t arinst_word(float dbm) {
    long w = lround(800.0 - (double)dbm * 10.0);
    if (w < 0) w = 0;
    if (w > 2047) w = 2047;
    return (uint16_t)w;
}

size_t arinst_build_packet(const arinst_req_t *req, const float *dbm, int elapsed_ms,
                           uint8_t *buf, size_t cap) {
    int n = snprintf((char *)buf, cap, "scn20 %llu %d\r\n",
                     (unsigned long long)req->start_hz, req->index);
    size_t len = (size_t)n;
    if (len + (size_t)req->points * 2 + 16 > cap) return 0;
    for (int i = 0; i < req->points; i++) {
        uint16_t w = arinst_word(dbm[i]);
        buf[len++] = (uint8_t)(w >> 8);     /* MSB first, as the app parser expects */
        buf[len++] = (uint8_t)(w & 0xFF);
    }
    buf[len++] = 0xFF;
    buf[len++] = 0xFF;
    len += (size_t)snprintf((char *)buf + len, cap - len, "%d\r\ncomplete\r\n", elapsed_ms);
    return len;
}
