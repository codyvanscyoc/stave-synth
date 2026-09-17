#include "stave/stage_buses.hpp"
#include <algorithm>
#include "bus_fixture.hpp"
extern "C" {
void* buses_create(unsigned frames) noexcept {
    try { return new stave::StageBuses(frames); } catch (...) { return nullptr; }
}
void buses_delete(void* p) { delete static_cast<stave::StageBuses*>(p); }
int buses_configure(void* p, const double* v, unsigned n) {
    stave::StageBusConfig config;
    return p && stave_fixture::bus_configuration(v,n,config) && static_cast<stave::StageBuses*>(p)->configure(config);
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
