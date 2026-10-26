/* Arinst analyzer personality: ties the sweep logic to the C5 radio + USB. GPL-3.0-or-later. */
#pragma once
#include <stdbool.h>

/* Handle one received command line (already '\0'-terminated, CR/LF trimmed).
 * Returns true if it was an Arinst command this module consumed. Runs the sweep
 * and writes the reply synchronously on the calling (command) task. */
bool arinst_service(const char *line);

/* dBFS->dBm calibration offset. The model works on floor-relative levels, so this
 * mainly sets the absolute scale; refine with widscan58's powercal. */
void arinst_set_cal(float offset_db);
float arinst_get_cal(void);
