#include "stave/piano_room.hpp"
#include <array>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <new>

namespace {
bool counting{};
unsigned allocations{};
void check(bool value) { if (!value) std::abort(); }
void* allocate(std::size_t n) {
    if (counting) ++allocations;
    if (void* p = std::malloc(n ? n : 1)) return p;
    throw std::bad_alloc();
}
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

int main() {
    for (unsigned n : {0u, 1u, 255u, 513u}) {
        bool rejected = false;
        try { stave::PianoRoom room(n); } catch (...) { rejected = true; }
        check(rejected);
    }
    counting = true;
    void* calibration = ::operator new(1);
    counting = false; ::operator delete(calibration);
    check(allocations == 1); allocations = 0;
    for (unsigned frames : {256u, 512u}) {
        stave::PianoRoom room(frames);
        stave::PianoRoomConfig config;
        std::array<double, 513> l{}, r{}, dl{}, dr{};
        l[0] = .4; r[0] = -.2; dl.fill(17); dr.fill(18);
        const auto initial = room.current_wet();
        check(!room.process_block(nullptr, r.data(), dl.data(), dr.data(), frames));
        check(!room.process_block(l.data(), r.data(), dl.data(), dl.data() + 1, frames));
        check(!room.process_block(l.data(), r.data(), dl.data(), dr.data(), frames + 1));
        check(!room.process_block(l.data(), r.data(), dl.data(), dr.data(), 0));
        check(room.current_wet() == initial && dl[0] == 17 && dr[0] == 18);
        for (double bad : {-1., 1.01, std::numeric_limits<double>::quiet_NaN(), std::numeric_limits<double>::infinity()}) {
            config.wet = bad; check(!room.configure(config)); config = {};
            config.size = bad; check(!room.configure(config)); config = {};
            config.damp = bad; check(!room.configure(config)); config = {};
        }
        check(room.process_block(l.data(), r.data(), dl.data(), dr.data(), frames));
        counting = true;
        for (unsigned i = 0; i < 2000; ++i) {
            config.enabled = i % 17 != 0;
            config.wet = i % 7 == 0 ? 0 : (i % 101) / 100.;
            config.size = (i % 101) / 100.; config.damp = (i % 100) / 100.;
            check(room.configure(config));
            check(room.process_block(l.data(), r.data(), dl.data(), dr.data(), frames));
            if (i % 19 == 0) room.clear();
            for (unsigned j = 0; j < frames; ++j) check(std::isfinite(dl[j]) && std::isfinite(dr[j]));
        }
        counting = false;
        check(allocations == 0 && dl[512] == 17 && dr[512] == 18);
        config = {}; config.enabled = false; check(room.configure(config));
        check(room.process_block(l.data(), r.data(), l.data(), r.data(), frames));
        check(l[0] == .4 && r[0] == -.2); // exact bypass, including aliasing
        const double frozen = room.current_wet();
        config.enabled = true; config.wet = .001; check(room.configure(config));
        check(room.process_block(l.data(), r.data(), dl.data(), dr.data(), frames));
        check(room.current_wet() == frozen && dl[0] == l[0]);
        config.wet = 1; check(room.configure(config)); room.clear();
        l.fill(0); r.fill(0);
        for (unsigned i = 0; i < 100; ++i) {
            check(room.process_block(l.data(), r.data(), dl.data(), dr.data(), frames));
            for (unsigned j = 0; j < frames; ++j) check(dl[j] == 0 && dr[j] == 0);
        }
        l[5] = std::numeric_limits<double>::quiet_NaN();
        check(!room.process_block(l.data(), r.data(), dl.data(), dr.data(), frames));
        check(!room.healthy()); room.clear(); check(!room.healthy() && !room.configure(config));
        l.fill(0);
        check(!room.process_block(l.data(), r.data(), dl.data(), dr.data(), frames));
        for (unsigned j = 0; j < frames; ++j) check(dl[j] == 0 && dr[j] == 0);
        stave::PianoRoom overflow(frames);
        l[0] = std::numeric_limits<double>::max();
        check(!overflow.process_block(l.data(), r.data(), dl.data(), dr.data(), frames));
        check(!overflow.healthy());
    }
    std::puts("PASS: piano room validation, bounds, aliases, clear, terminal fault silence, calibrated C++ allocation probe");
}
