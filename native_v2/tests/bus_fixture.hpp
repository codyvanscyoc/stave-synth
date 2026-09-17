#pragma once
#include "stave/stage_buses.hpp"
#include <cmath>
namespace stave_fixture {
inline bool bus_configuration(const double* v, unsigned n, stave::StageBusConfig& config) {
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
