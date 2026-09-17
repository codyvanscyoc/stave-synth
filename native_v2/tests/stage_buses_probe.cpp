#include "stave/stage_buses.hpp"
#include <algorithm>
#include <cmath>
namespace {
bool configuration(const double* v, unsigned n, stave::StageBusConfig& config) {
    if (!v || n != 23) return false;
    for (unsigned i = 0; i < n; ++i) if (!std::isfinite(v[i])) return false;
    for (unsigned i : {4u, 5u, 6u, 12u, 13u, 15u, 16u, 19u}) if (v[i] != 0 && v[i] != 1) return false;
    if (v[14] < 0 || v[14] > 4095 || std::floor(v[14]) != v[14]) return false;
    auto& p = config.pad;
    p.cutoff=v[0]; p.resonance=v[1]; p.range_min=v[2]; p.range_max=v[3]; p.slope24=v[4];
    p.shared1=v[5]; p.shared2=v[6]; p.independent1=v[7]; p.independent2=v[8]; p.highpass=v[9];
    p.send1=v[10]; p.send2=v[11]; p.bypass1=v[12]; p.bypass2=v[13]; p.haas_samples=static_cast<unsigned>(v[14]);
    p.shimmer=v[15]; p.shimmer_high=v[16]; p.shimmer_mix=v[17]; p.shimmer_send=v[18];
    config.room={v[19] != 0, v[20], v[21], v[22]};
    return config.valid();
}
}
extern "C" {
void* buses_create(unsigned frames) noexcept {
    try { return new stave::StageBuses(frames); } catch (...) { return nullptr; }
}
void buses_delete(void* p) { delete static_cast<stave::StageBuses*>(p); }
int buses_configure(void* p, const double* v, unsigned n) {
    stave::StageBusConfig config;
    return p && configuration(v,n,config) && static_cast<stave::StageBuses*>(p)->configure(config);
}
int buses_process(void* p, const double* const* input, unsigned flags) {
    if (!p || !input || flags > 7) return 0;
    std::array<const double*,7> stems{};
    for (unsigned c = 0; c < 7; ++c) stems[c] = input[c];
    return static_cast<stave::StageBuses*>(p)->process_block(stems,
        {(flags&1)!=0, (flags&2)!=0, (flags&4)!=0});
}
const double* buses_stem(void* p, unsigned c) { return p ? static_cast<stave::StageBuses*>(p)->stem(c) : nullptr; }
void buses_state(void* p, double* dest) {
    if (p && dest) { auto state = static_cast<stave::StageBuses*>(p)->state(); std::copy(state.begin(),state.end(),dest); }
}
void buses_clear(void* p) { if (p) static_cast<stave::StageBuses*>(p)->clear(); }
void buses_stop(void* p) { if (p) static_cast<stave::StageBuses*>(p)->stop(); }
}
