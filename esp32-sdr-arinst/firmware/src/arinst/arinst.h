/* Arinst analyzer personality: ties the sweep logic to the C5 radio + USB. GPL-3.0-or-later. */
#pragma once
#include <stdbool.h>
#include "arinst_sweep.h"

/* Handle one received command line (already '\0'-terminated, CR/LF trimmed).
 * Returns true if it was an Arinst command this module consumed. Sweeps are
 * queued to the scanner, the sole owner of the radio and PSD scratch buffers.
 * Waits for completion so a subsequent command cannot overtake the scan reply. */
bool arinst_service(const char *line);

/* Execute a queued sweep, on the scanner task only. */
void arinst_scan(const arinst_req_t *req);

/* dBFS->dBm calibration offset. The model works on floor-relative levels, so this
 * mainly sets the absolute scale; refine with widscan58's powercal. */
void arinst_set_cal(float offset_db);
float arinst_get_cal(void);
