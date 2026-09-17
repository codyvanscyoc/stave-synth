#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

namespace stave {

struct LayerWeights {
    double osc1{1}, osc2{1}, shimmer{1}, piano{1};
    bool valid() const noexcept {
        for (double x : {osc1, osc2, shimmer, piano})
            if (!std::isfinite(x) || x < 0 || x > 1) return false;
        return true;
    }
};

class StageNoteSink {
public:
    virtual ~StageNoteSink() = default;
    virtual void osc_on(std::uint8_t note, double velocity, LayerWeights weights) noexcept = 0;
    virtual void osc_off(std::uint8_t note) noexcept = 0;
    virtual void piano_on(std::uint8_t note, double velocity) noexcept = 0;
    virtual void piano_off(std::uint8_t note) noexcept = 0;
    virtual void all_notes_off() noexcept = 0;
};

// Native copy of v1.2 raw-key ownership: one collapsed MIDI key space, as in
// JackEngine, not M1's independent-channel repeated-note-count experiment.
// This runs on the single event/audio owner. The sink must be bounded, noexcept,
// and retain/reveal its own errors. No queue, device, UI, Fluid CC64 or allocation.
// Pedals delay note-offs; sostenuto captures only on its rising edge.
class StageNotes final {
public:
    struct Key {
        bool mapped{}, physical{}, sustained{}, captured{};
        std::uint8_t osc_note{}, piano_note{};
    };
    explicit StageNotes(StageNoteSink& sink) noexcept : sink_(sink) {}
    bool configure(int transpose, int piano_octave, int minimum_velocity) noexcept {
        if (transpose < -24 || transpose > 24 || piano_octave < -3 || piano_octave > 3 ||
            minimum_velocity < 1 || minimum_velocity > 127) return false;
        transpose_ = transpose; piano_octave_ = piano_octave; minimum_velocity_ = minimum_velocity;
        return true;
    }
    bool note_on(int raw, int velocity, LayerWeights weights = {}) noexcept {
        if (raw < 0 || raw > 127 || velocity < 0 || velocity > 127 || !weights.valid()) return false;
        if (velocity == 0) return note_off(raw);
        if (velocity < minimum_velocity_) return true;
        auto& key = keys_[static_cast<unsigned>(raw)];
        const auto osc = static_cast<std::uint8_t>(std::clamp(raw + transpose_, 0, 127));
        const auto piano = static_cast<std::uint8_t>(std::clamp(int(osc) + piano_octave_ * 12, 0, 127));
        if (key.mapped) {
            if (key.osc_note != osc) sink_.osc_off(key.osc_note);
            if (key.piano_note != piano) sink_.piano_off(key.piano_note);
        }
        key.mapped = key.physical = true;
        key.sustained = false;
        key.osc_note = osc; key.piano_note = piano;
        const double normalized = std::min(1.0, velocity / 127.0);
        if (weights.osc1 > 0 || weights.osc2 > 0 || weights.shimmer > 0)
            sink_.osc_on(osc, normalized, weights);
        if (weights.piano > 0) sink_.piano_on(piano, normalized * weights.piano);
        return true;
    }
    bool note_off(int raw) noexcept {
        if (raw < 0 || raw > 127) return false;
        auto& key = keys_[static_cast<unsigned>(raw)];
        key.physical = false;
        if (!key.mapped) return true;
        if (sustain_) key.sustained = true;
        else if (!(sostenuto_ && key.captured)) release(key);
        return true;
    }
    void sustain(bool down) noexcept {
        sustain_ = down;
        if (!down) for (auto& key : keys_) {
            if (key.sustained && !key.physical && !(sostenuto_ && key.captured) && key.mapped) release(key);
            key.sustained = false;
        }
    }
    void sostenuto(bool down) noexcept {
        if (down) {
            if (!sostenuto_) {
                sostenuto_ = true;
                for (auto& key : keys_) key.captured = key.physical;
            }
        } else {
            sostenuto_ = false;
            for (auto& key : keys_) {
                if (key.captured && !key.physical && key.mapped) {
                    if (sustain_) key.sustained = true;
                    else release(key);
                }
                key.captured = false;
            }
        }
    }
    void all_notes_off() noexcept {
        keys_ = {};
        sustain_ = sostenuto_ = false;
        sink_.all_notes_off();
    }
    const std::array<Key, 128>& keys() const noexcept { return keys_; }
    bool sustain_down() const noexcept { return sustain_; }
    bool sostenuto_down() const noexcept { return sostenuto_; }
private:
    void release(Key& key) noexcept {
        sink_.osc_off(key.osc_note); sink_.piano_off(key.piano_note);
        key.mapped = false;
        // Preserve raw physical/captured bits until their own release paths.
    }
    StageNoteSink& sink_;
    std::array<Key, 128> keys_{};
    int transpose_{}, piano_octave_{}, minimum_velocity_{10};
    bool sustain_{}, sostenuto_{};
};
} // namespace stave
