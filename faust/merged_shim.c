/* Phase 4 merged shim (FAUST_PORT_PLAN.md, Design A).
 *
 * ONE cffi call per block replaces the Python sandwich between the Faust
 * osc bank and the Faust pad bus:
 *
 *     computeStaveOscBank  →  f32→f64 widen (exact)  →  computeStavePadBus
 *
 * This file is plain C — NOT a Faust module, and deliberately NOT part of
 * jack_bridge.c (that file is audio-callback territory; this runs on the
 * Python render thread). It links against nothing: the compute-function
 * pointers AND both dsp instance pointers arrive from Python, so the same
 * shim works with the full 24-slot bank or the 12-slot lite variant
 * (whichever .so faust_osc_bank.py selected via LOW_RAM_MODE).
 *
 * Buffer contract (all owned by stave_synth/faust_merged.py, persistent,
 * zero-alloc on the hot path):
 *   bank_out : 5 float32 rows  — the bank's native output (osc1 L/R,
 *              osc2 L/R, shimmer mono)
 *   bus_in   : 5 float64 rows  — conversion target; Python reads these
 *              rows after the call as the block's osc_out (pre-filter
 *              taps for the LFO-split / FX-bypass magnitude ratios)
 *   bus_out  : 9 float64 rows  — the pad bus outputs (pad_bus.dsp header)
 *
 * The widening cast float→double is exact (no rounding), bit-identical to
 * the np.copyto(..., casting="unsafe") it replaces.
 */

typedef void (*stave_bank_compute_t)(void *dsp, int count,
                                     float **inputs, float **outputs);
typedef void (*stave_bus_compute_t)(void *dsp, int count,
                                    double **inputs, double **outputs);

#define STAVE_BANK_CH 5

void stave_merged_compute(void *bank_fn, void *bank_dsp,
                          void *bus_fn, void *bus_dsp,
                          int count,
                          float **bank_out, double **bus_in,
                          double **bus_out)
{
    /* The bank has zero inputs — its generated compute never dereferences
     * the inputs array, so NULL is safe (mirrors the wrapper's float*[0]). */
    ((stave_bank_compute_t)bank_fn)(bank_dsp, count, (float **)0, bank_out);

    for (int c = 0; c < STAVE_BANK_CH; c++) {
        const float *restrict s = bank_out[c];
        double *restrict d = bus_in[c];
        for (int i = 0; i < count; i++)
            d[i] = (double)s[i];
    }

    ((stave_bus_compute_t)bus_fn)(bus_dsp, count, bus_in, bus_out);
}
