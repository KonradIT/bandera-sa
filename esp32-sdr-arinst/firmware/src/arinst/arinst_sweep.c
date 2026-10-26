/* Arinst protocol + sweep planning (pure logic). GPL-3.0-or-later. See arinst_sweep.h. */
#include "arinst_sweep.h"

#include <math.h>
#include <ctype.h>
#include <errno.h>
#include <limits.h>
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
    while (isspace((unsigned char)*line)) line++;
    size_t n = strcspn(line, " \t\r\n");
    if (n == 5 && (!memcmp(line, "scn20", 5) || !memcmp(line, "scn22", 5))) return 's';
    if (n == 4 && !memcmp(line, "test", 4)) return 't';
    if ((n == 3 && (!memcmp(line, "gon", 3) || !memcmp(line, "gof", 3))) ||
        (n == 4 && !memcmp(line, "goff", 4))) return 'g';
    if (n == 3 && !memcmp(line, "scf", 3)) return 'f';
    if (n == 3 && !memcmp(line, "sga", 3)) return 'a';
    return 0;
}

arinst_req_t arinst_parse(const char *line) {
    arinst_req_t r;
    memset(&r, 0, sizeof(r));
    /* "scn20 start stop step 200 samples 10700000 att idx" */
    if (arinst_command(line) != 's') return r;
    while (isspace((unsigned char)*line)) line++;
    bool tracking = line[4] == '2';
    line += 5;
    uint64_t v[8];
    for (int i = 0; i < 8; i++) {
        while (isspace((unsigned char)*line)) line++;
        if (!isdigit((unsigned char)*line)) return r;
        char *end;
        errno = 0;
        v[i] = strtoull(line, &end, 10);
        if (errno == ERANGE || (*end && !isspace((unsigned char)*end))) return r;
        line = end;
    }
    while (isspace((unsigned char)*line)) line++;
    uint64_t start = v[0], stop = v[1], step = v[2];
    if (*line || !step || stop < start || !v[4] || v[7] > INT_MAX) return r;
    uint64_t intervals = (stop - start) / step;
    if (intervals >= ARINST_MAX_POINTS) return r; /* never silently change the requested count */
    r.start_hz = start;
    r.stop_hz = stop;
    r.step_hz = step;
    r.index = (int)v[7];
    r.tracking = tracking;
    r.points = (int)intervals + 1;
    r.ok = true;
    return r;
}

static int band_of(uint64_t f, const arinst_band_t *bands, int nbands) {
    for (int i = 0; i < nbands; i++)
        if (f >= bands[i].lo_hz && f <= bands[i].hi_hz) return i;
    return -1;
}

int arinst_plan(const arinst_req_t *req, double rate_hz, double usable_frac, double dead_hz,
                const arinst_band_t *bands, int nbands, arinst_hop_t *hops, int max_hops, int *covered) {
    int nhops = 0, cov = 0;
    if (covered) *covered = 0;
    if (!req || !req->ok || !req->step_hz || !bands || nbands <= 0 || !hops || max_hops <= 0 ||
        !isfinite(rate_hz) || !isfinite(usable_frac) || !isfinite(dead_hz) ||
        rate_hz <= 0 || usable_frac <= 0 || usable_frac > 1 || dead_hz < 0) return 0;
    double edge = rate_hz * usable_frac / 2.0;  /* usable half-span, centred on the LO */
    double hi = edge - 1e6;                     /* keep owned points one bin inside the mask */
    double dead = dead_hz;                      /* radio is blind within +-dead of the LO */
    if (hi <= dead) return 0; /* do not invent points outside the capture's valid mask */
    int i = 0;
    while (i < req->points && nhops < max_hops) {
        uint64_t f = req->start_hz + (uint64_t)i * req->step_hz;
        int b = band_of(f, bands, nbands);
        if (b < 0) { i++; continue; }           /* unlockable point: left at the floor */
        double bhi = (double)bands[b].hi_hz;
        /* Put f at a good offset, LO inside the band: use the lower sideband (LO above the
         * points) normally; near the top of the band flip to the upper sideband (LO below). */
        double lod = ((double)f + hi <= bhi) ? (double)f + hi : (double)f - dead;
        uint64_t lo = (uint64_t)(floor(lod / 1e6) * 1e6);   /* whole MHz (the PLL needs it) */
        if (lo < bands[b].lo_hz) lo = bands[b].lo_hz;
        if (lo > bands[b].hi_hz) lo = bands[b].hi_hz;
        arinst_hop_t *h = &hops[nhops];
        h->lo_hz = lo;
        h->first = i;
        h->count = 0;
        /* Absorb consecutive in-band points that land in this LO's good sideband. */
        while (i < req->points) {
            uint64_t g = req->start_hz + (uint64_t)i * req->step_hz;
            if (band_of(g, bands, nbands) != b) break;
            double aoff = fabs((double)g - (double)lo);
            if (aoff < dead || aoff > hi) break;
            h->count++;
            cov++;
            i++;
        }
        if (h->count > 0) nhops++;
        else i++; /* safety: never stall (point uncoverable after the in-band clamp) */
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
        double off = (double)f - (double)hop->lo_hz;        /* psd_compute already conjugates the RF I/Q */
        int k = nfft / 2 + (int)lround(off / bin_hz);       /* psd_compute is FFT-shifted: DC at N/2 */
        if (k < 0 || k >= nfft) continue;
        if (valid && !valid[k]) continue;                   /* DC guard / out-of-band bin */
        out[idx] = arinst_dbfs(power[k], tone_scale) + cal_offset_db;
    }
}

uint16_t arinst_word(float dbm) {
    if (!isfinite(dbm)) dbm = ARINST_FLOOR_DBM;
    if (dbm >= 80.0f) return 0;
    if (dbm <= -124.7f) return 2047;
    long w = lround(800.0 - (double)dbm * 10.0);
    if (w < 0) w = 0;
    if (w > 2047) w = 2047;
    return (uint16_t)w;
}

size_t arinst_build_packet(const arinst_req_t *req, const float *dbm, int elapsed_ms,
                           uint8_t *buf, size_t cap) {
    if (!req || !req->ok || req->points < 1 || req->points > ARINST_MAX_POINTS || !dbm || !buf) return 0;
    char head[64], tail[40];
    int n = snprintf(head, sizeof(head), "\r\nscn2%c %llu %d\r\n", req->tracking ? '2' : '0',
                     (unsigned long long)req->start_hz, req->index);
    int nt = snprintf(tail, sizeof(tail), "%d\r\ncomplete\r\n", elapsed_ms < 0 ? 0 : elapsed_ms);
    if (n < 0 || (size_t)n >= sizeof(head) || nt < 0 || (size_t)nt >= sizeof(tail)) return 0;
    size_t len = (size_t)n;
    if (len + (size_t)req->points * 2 + 2 + (size_t)nt > cap) return 0;
    memcpy(buf, head, len);
    for (int i = 0; i < req->points; i++) {
        uint16_t w = arinst_word(dbm[i]);
        buf[len++] = (uint8_t)(w >> 8);     /* MSB first, as the app parser expects */
        buf[len++] = (uint8_t)(w & 0xFF);
    }
    buf[len++] = 0xFF;
    buf[len++] = 0xFF;
    memcpy(buf + len, tail, (size_t)nt);
    len += (size_t)nt;
    return len;
}
