#include "stave/block_envelope.hpp"
#include "stave/stage_notes.hpp"
#include "stave/stage_voices.hpp"
#include <array>
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
struct Sink final : stave::StageNoteSink {
    unsigned osc_ons{}, osc_offs{}, piano_ons{}, piano_offs{}, resets{};
    double velocity{};
    void osc_on(std::uint8_t, double, stave::LayerWeights) noexcept override { ++osc_ons; }
    void osc_off(std::uint8_t) noexcept override { ++osc_offs; }
    void piano_on(std::uint8_t, double value) noexcept override { ++piano_ons; velocity = value; }
    void piano_off(std::uint8_t) noexcept override { ++piano_offs; }
    void all_notes_off() noexcept override { ++resets; }
};
struct VoiceSink final : stave::StageVoiceSink {
    unsigned calls{}, active_gates{};
    void key_trigger() noexcept override { ++calls; }
    void start_slot(unsigned slot) noexcept override { check(slot < 12); ++calls; }
    void clear_slot(unsigned slot) noexcept override { check(slot < 12); ++calls; }
    void gate(unsigned slot, std::uint8_t note, double a, double b, double w) noexcept override {
        check(slot < 12 && note < 128 && std::isfinite(a) && std::isfinite(b) && std::isfinite(w));
        ++calls; ++active_gates;
    }
};
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }

int main() {
    Sink sink;
    stave::StageNotes notes(sink);
    stave::BlockEnvelope envelope;
    VoiceSink voice_sink;
    stave::StageVoices voices(voice_sink);
    bool threw = false;
    try { stave::BlockEnvelope invalid(0); } catch (...) { threw = true; }
    check(threw);
    threw = false;
    try { stave::StageVoices invalid(voice_sink, 48000, 13); } catch (...) { threw = true; }
    check(threw);
    count_new = true;
    void* calibration = ::operator new(1);
    count_new = false;
    ::operator delete(calibration);
    check(allocations == 1); allocations = 0;
    count_new = true;
    check(!notes.configure(25, 0, 10));
    check(!notes.configure(0, 4, 10));
    check(!notes.configure(0, 0, 0));
    check(!notes.note_on(-1, 100)); check(!notes.note_on(128, 100));
    check(!notes.note_on(60, 128)); check(!notes.note_off(128));
    check(!notes.note_on(60, 100, {1, 1, 1, std::numeric_limits<double>::quiet_NaN()}));
    check(!voices.note_on(-1, 1)); check(!voices.note_off(128));
    check(!voices.note_on(60, std::numeric_limits<double>::quiet_NaN()));
    check(!voices.note_on(60, 1, {1, 2, 1, 1}));
    check(!voices.end_block()); check(!voices.begin_block(0)); check(!voices.begin_block(4097));
    check(!voices.configure({-1, 0, 0, 0}, {}));
    check(voices.size() == 0 && voice_sink.calls == 0 && voices.voice_at(12) == nullptr);
    check(voices.note_on(60, 1)); check(voices.begin_block(512));
    const auto calls = voice_sink.calls;
    check(!voices.begin_block(512) && !voices.note_on(61, 1) && !voices.note_off(60));
    check(!voices.release_all() && !voices.configure({}, {}));
    check(voice_sink.calls == calls && voices.size() == 1);
    check(voices.end_block());
    check(notes.note_on(60, 100, {0, 0, 0, .5}));
    check(sink.osc_ons == 0 && sink.piano_ons == 1 && sink.velocity == 100.0 / 127 * .5);
    notes.all_notes_off();
    check(!notes.sustain_down() && !notes.sostenuto_down());
    for (const auto& key : notes.keys()) check(!key.mapped && !key.physical && !key.captured && !key.sustained);
    for (int iteration = 0; iteration < 100; ++iteration) {
        envelope.trigger();
        double endpoint = 0;
        for (int block = 0; block < 50; ++block) check(envelope.advance(512, endpoint));
        envelope.release();
        check(envelope.advance(256, endpoint));
        for (int key = 0; key < 128; ++key) check(notes.note_on(key, 100));
        notes.sostenuto(true); notes.sustain(true);
        for (int key = 0; key < 128; ++key) check(notes.note_off(key));
        notes.sostenuto(false); notes.sustain(false);
        for (const auto& key : notes.keys()) check(!key.mapped);
        check(voices.configure({}, {0, 10, 10, 200}));
        for (int key = 36; key < 72; ++key) {
            check(voices.note_on(key, .7));
            check(voices.begin_block(256)); check(voices.end_block());
        }
        check(voices.size() == 12);
        std::array<bool, 12> slots{};
        for (unsigned i = 0; i < voices.size(); ++i) {
            const auto slot = voices.slot_at(i);
            check(slot < 12 && !slots[slot]); slots[slot] = true;
        }
        check(voices.configure({0, 0, 0, 0}, {0, 0, 0, 0}));
        check(voices.release_all());
        check(voices.begin_block(512)); check(voices.end_block());
        check(voices.begin_block(512)); check(voices.end_block());
        check(voices.size() == 0);
    }
    count_new = false;
    check(allocations == 0);
    check(voices.steals() > 0 && voice_sink.active_gates > 0);
    std::puts("PASS: component guards, split suppression, full-key release, voice lifecycle/steal/recycle, no C++ new/new[] in envelope/note/voice owners");
}
