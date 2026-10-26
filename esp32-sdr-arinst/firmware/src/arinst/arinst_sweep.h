/* Arinst SSA protocol + sweep planning for the ESP32-C5 radio. GPL-3.0-or-later.
 *
 * Pure logic, no ESP-IDF or hardware dependencies, so it builds and is tested on
 * the host (see firmware/test/test_arinst_sweep.c). The hardware glue in
 * arinst.c drives radio_tune/radio_capture/psd_compute and calls these.
 *
 * The Frontera app (and the real Arinst) speak this over serial:
 *   request  "scn20 {startHz} {stopHz} {stepHz} 200 {samples} 10700000 {att} {idx}\r\n"
 *   reply    "scn20 {startHz} {idx}\r\n" + big-endian 16-bit words, one per point,
 *            dBm = (800 - (word & 0x7FF)) / 10, then FF FF, then "{elapsedMs}\r\ncomplete\r\n"
 * Verified byte-for-byte against the app's parser (c8/l.java) and pyarinst.
 *
 * The C5's PLL only locks near 2.4 and 5 GHz, so points outside those bands are
 * returned at a floor level; drones live in the covered bands.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#define ARINST_MAX_POINTS 2048
#define ARINST_MAX_HOPS 128
#define ARINST_FLOOR_DBM (-110.0f)

/* A covered tuning band, inclusive, in Hz. */
typedef struct { uint64_t lo_hz, hi_hz; } arinst_band_t;

/* The C5's usable receive bands (radio_c5.c: 5 GHz is the FPV range, plus 2.4 GHz). */
extern const arinst_band_t ARINST_C5_BANDS[];
extern const int ARINST_C5_NBANDS;

typedef struct {
    uint64_t start_hz, stop_hz, step_hz;
    int points;       /* (stop-start)/step + 1, clamped to ARINST_MAX_POINTS */
    int index;        /* sequence index echoed back */
    bool ok;
} arinst_req_t;

/* One LO capture covering a contiguous run of output points. */
typedef struct {
    uint64_t lo_hz;   /* tune here (whole MHz applied by the radio) */
    int first, count; /* output indices [first, first+count) this hop supplies */
} arinst_hop_t;

/* Parse a "scn20"/"scn22" request line. Returns req.ok=false on malformed input. */
arinst_req_t arinst_parse(const char *line);

/* Classify a line's first token: 0 none, 's' scn20/scn22, 't' test, 'g' gon/goff, 'f' scf, 'a' sga. */
char arinst_command(const char *line);

/* Plan the LO hops that cover the request's points which fall in a locked band.
 * rate_hz/usable_frac set each capture's usable span (usable = rate*frac, centred on LO).
 * Points outside every band get no hop (the caller leaves them at the floor).
 * Returns the number of hops (<= ARINST_MAX_HOPS); *covered = points a hop will fill. */
int arinst_plan(const arinst_req_t *req, double rate_hz, double usable_frac,
                const arinst_band_t *bands, int nbands, arinst_hop_t *hops, int max_hops, int *covered);

/* For one hop, map each of its output points to the nearest usable FFT bin and
 * write dBm into out[]. power[] holds nfft linear powers in natural FFT order
 * (bin 0 = DC = LO); a full-scale tone's bin equals tone_scale. valid[] (or NULL)
 * marks usable bins (0 near DC / out of band). dbm = 10*log10(power*tone_scale) + cal_offset.
 * Points whose nearest bin is invalid are left untouched (caller pre-fills the floor). */
void arinst_fill_hop(const arinst_req_t *req, const arinst_hop_t *hop, double rate_hz, int nfft,
                     const uint32_t *power, double tone_scale, const uint8_t *valid,
                     float cal_offset_db, float *out);

/* dBFS of one linear power code: 10*log10(power*tone_scale), a full-scale tone = 0 dBFS. */
float arinst_dbfs(uint32_t power_code, double tone_scale);

/* Build the Arinst reply packet into buf (needs 2*points + 48 bytes). Returns length. */
size_t arinst_build_packet(const arinst_req_t *req, const float *dbm, int elapsed_ms,
                           uint8_t *buf, size_t cap);

/* 16-bit word for one dBm value: clamp(round(800 - dbm*10), 0, 2047). */
uint16_t arinst_word(float dbm);
