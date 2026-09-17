// Test-only C ABI for a pinned Python differential oracle. Not a production IPC.
#include "stave/piano_chain.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>

extern "C" {
void* piano_chain_create(std::uint32_t rate, std::uint32_t frames) noexcept {
    try { return new stave::PianoChain(rate, frames); } catch (...) { return nullptr; }
}
void piano_chain_delete(void* pointer) noexcept { delete static_cast<stave::PianoChain*>(pointer); }
int piano_chain_configure(void* pointer, const double* values, std::uint32_t count) noexcept {
    if (!pointer || !values || count != 33) return 0;
    for (unsigned i = 0; i < count; ++i) if (!std::isfinite(values[i])) return 0;
    for (unsigned i : {3u, 12u, 20u, 24u, 28u, 32u}) if (values[i] != 0 && values[i] != 1) return 0;
    stave::PianoChainConfig config;
    config.volume = values[0]; config.lowcut_hz = values[1]; config.highcut_hz = values[2];
    config.comp_enabled = values[3] != 0; config.comp_threshold_db = values[4]; config.comp_ratio = values[5];
    config.comp_attack_ms = values[6]; config.comp_release_ms = values[7]; config.comp_makeup_db = values[8];
    config.comp_knee_db = values[9]; config.comp_drive_db = values[10]; config.comp_wet = values[11];
    config.brightness_enabled = values[12] != 0; config.brightness_amount = values[13];
    config.velocity_target = values[14]; config.tremolo_hz = values[15]; config.tremolo_depth = values[16];
    for (unsigned i = 0; i < 4; ++i) config.eq[i] = {values[17 + 4*i], values[18 + 4*i],
                                                   values[19 + 4*i], values[20 + 4*i] != 0};
    return static_cast<stave::PianoChain*>(pointer)->configure(config);
}
int piano_chain_process(void* pointer, const double* l, const double* r,
                        double* out_l, double* out_r, std::uint32_t frames) noexcept {
    if (!pointer) return 0;
    return static_cast<stave::PianoChain*>(pointer)->process_block(l, r, out_l, out_r, frames);
}
void piano_chain_state(void* pointer, double* values) noexcept {
    if (pointer && values) {
        const auto state = static_cast<stave::PianoChain*>(pointer)->state();
        std::copy(state.begin(), state.end(), values);
    }
}
void piano_chain_clear(void* pointer) noexcept { if (pointer) static_cast<stave::PianoChain*>(pointer)->clear(); }
int piano_chain_healthy(void* pointer) noexcept { return pointer && static_cast<stave::PianoChain*>(pointer)->healthy(); }
}
