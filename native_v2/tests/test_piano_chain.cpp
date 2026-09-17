#include "stave/piano_chain.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>

namespace {
bool count_new = false;
unsigned allocations = 0;
void* allocate(std::size_t size) {
    if (count_new) ++allocations;
    if (auto* result = std::malloc(size ? size : 1)) return result;
    throw std::bad_alloc();
}
void check(bool pass) { if (!pass) std::abort(); }
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

int main() {
    bool rejected = false;
    try { stave::PianoChain invalid(48000, 513); } catch (...) { rejected = true; }
    check(rejected);
    stave::PianoChain chain;
    stave::PianoChainConfig config;
    std::array<double, 513> input_l{}, input_r{}, output_l{}, output_r{};
    input_l[0] = .5; input_r[0] = -.25;
    output_l.fill(17); output_r.fill(18);
    const auto initial_state = chain.state();
    check(!chain.process_block(nullptr, input_r.data(), output_l.data(), output_r.data(), 512));
    check(!chain.process_block(input_l.data(), input_r.data(), output_l.data(), output_l.data() + 1, 512));
    check(!chain.process_block(input_l.data(), input_r.data(), output_l.data(), output_r.data(), 513));
    check(!chain.process_block(input_l.data(), input_r.data(), output_l.data(), output_r.data(), 0));
    check(chain.state() == initial_state && output_l[0] == 17 && output_l[512] == 17 && output_r[512] == 18);
    auto invalid = config; invalid.comp_drive_db = std::numeric_limits<double>::infinity();
    check(!chain.configure(invalid) && chain.state() == initial_state);
    check(chain.process_block(input_l.data(), input_r.data(), output_l.data(), output_r.data(), 512));
    count_new = true;
    void* calibration = ::operator new(1);
    count_new = false;
    ::operator delete(calibration); check(allocations == 1); allocations = 0;
    count_new = true;
    config.comp_enabled = config.brightness_enabled = true;
    config.tremolo_depth = .4; config.tremolo_hz = 5.5;
    check(chain.configure(config));
    for (unsigned i = 0; i < 2000; ++i) {
        config.volume = (i % 100) / 100.0;
        config.eq[1].enabled = i % 2;
        check(chain.configure(config));
        check(chain.process_block(input_l.data(), input_r.data(), output_l.data(), output_r.data(), 512));
        for (unsigned j = 0; j < 512; ++j) check(std::isfinite(output_l[j]) && std::isfinite(output_r[j]));
    }
    chain.clear();
    count_new = false;
    check(allocations == 0);
    check(output_l[512] == 17 && output_r[512] == 18);
    // Exact in-place input/output is supported because source is copied first.
    check(chain.process_block(input_l.data(), input_r.data(), input_l.data(), input_r.data(), 512));
    // Supported parameter extremes and short final blocks remain finite.
    config.volume = 1; config.lowcut_hz = 2000; config.highcut_hz = 200;
    config.comp_enabled = config.brightness_enabled = true;
    config.comp_threshold_db = -40; config.comp_ratio = 20; config.comp_attack_ms = .5;
    config.comp_release_ms = 5; config.comp_drive_db = 12; config.comp_makeup_db = 24;
    config.comp_knee_db = 0; config.brightness_amount = 1; config.velocity_target = 0;
    config.tremolo_hz = 20; config.tremolo_depth = 1;
    for (auto& band : config.eq) { band.frequency = 20000; band.gain_db = 18; band.q = .1; }
    check(chain.configure(config));
    for (unsigned frames : {1u, 37u, 256u, 512u}) {
        check(chain.process_block(input_l.data(), input_r.data(), output_l.data(), output_r.data(), frames));
        for (unsigned i = 0; i < frames; ++i) check(std::isfinite(output_l[i]) && std::isfinite(output_r[i]));
    }
    input_l[3] = std::numeric_limits<double>::quiet_NaN();
    check(!chain.process_block(input_l.data(), input_r.data(), output_l.data(), output_r.data(), 512));
    check(!chain.healthy());
    for (unsigned j = 0; j < 512; ++j) check(output_l[j] == 0 && output_r[j] == 0);
    chain.clear(); check(!chain.healthy());
    check(!chain.configure(config));
    std::puts("PASS: piano chain bounds, transactional configuration, finite/fault silence, in-place, C++ new/new[] probe");
}
