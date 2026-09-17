#pragma once
#include "stave/block_envelope.hpp"
#include "stave/stage_notes.hpp"
#include <array>
#include <limits>

namespace stave {

class StageVoiceSink {
public:
    virtual ~StageVoiceSink() = default;
    // Adapter owns phase/LFO policy. These are NOT implemented by this allocator.
    virtual void key_trigger() noexcept = 0;
    virtual void start_slot(unsigned slot) noexcept = 0;
    virtual void clear_slot(unsigned slot) noexcept = 0;
    virtual void gate(unsigned slot, std::uint8_t note, double osc1,
                      double osc2, double shimmer_weight) noexcept = 0;
};

// Fixed-capacity v1.2 Faust voice ownership/envelope contract, not an audio
// backend. One owner; configure and events happen outside begin/end_block.
// Call begin_block once at the reference cadence, render DSP, then end_block.
// Do NOT advance envelopes again for MIDI-sliced source renders in that block.
class StageVoices final {
public:
    static constexpr unsigned capacity = 12;
    struct Voice {
        BlockEnvelope env1{}, env2{};
        std::uint8_t note{};
        double velocity{};
        LayerWeights weights{};
        std::uint64_t age{};
        bool retire{};
    };
    explicit StageVoices(StageVoiceSink& sink, std::uint32_t rate = 48000,
                         unsigned limit = capacity)
        : sink_(sink), limit_(limit), free_count_(limit) {
        if (limit == 0 || limit > capacity || rate < 8000 || rate > 192000)
            throw std::invalid_argument("Invalid stage voice capacity/rate");
        for (unsigned i = 0; i < capacity; ++i) {
            voices_[i].env1 = BlockEnvelope(rate);
            voices_[i].env2 = BlockEnvelope(rate);
            free_[i] = i;
        }
    }
    bool configure(EnvelopeConfig first, EnvelopeConfig second) noexcept {
        if (in_block_ || !first.valid() || !second.valid()) return false;
        for (auto& voice : voices_) {
            voice.env1.configure(first); voice.env2.configure(second);
        }
        return true;
    }
    bool note_on(int note, double velocity, LayerWeights weights = {}) noexcept {
        if (in_block_ || note < 0 || note > 127 || !std::isfinite(velocity) ||
            velocity < 0 || velocity > 1 || !weights.valid()) return false;
        // Renormalize age ordering before integer rollover, without allocation.
        // Stable rank also preserves list-order tie behavior (ages are unique).
        if (age_ == std::numeric_limits<std::uint64_t>::max()) {
            std::array<std::uint64_t, capacity> ranks{};
            for (unsigned i = 0; i < count_; ++i)
                for (unsigned j = 0; j < count_; ++j)
                    ranks[i] += voices_[order_[j]].age < voices_[order_[i]].age;
            for (unsigned i = 0; i < count_; ++i) voices_[order_[i]].age = ranks[i];
            age_ = count_;
        }
        sink_.key_trigger();
        for (unsigned i = 0; i < count_; ++i) {
            auto& voice = voices_[order_[i]];
            if (voice.note == note && voice.env1.stage() != BlockEnvelope::Stage::Release &&
                voice.env2.stage() != BlockEnvelope::Stage::Release) {
                voice.env1.trigger(); voice.env2.trigger();
                voice.velocity = velocity; voice.age = age_++;
                return true; // Existing latched weights and phases stay intact.
            }
        }
        unsigned slot;
        if (count_ == limit_) {
            unsigned victim = 0;
            bool found_release = false;
            double quietest = 0;
            for (unsigned i = 0; i < count_; ++i) {
                const auto& voice = voices_[order_[i]];
                if (voice.env1.stage() == BlockEnvelope::Stage::Release) {
                    const auto level = std::max(voice.env1.level(), voice.env2.level());
                    if (!found_release || level < quietest) {
                        victim = i; quietest = level; found_release = true;
                    }
                }
            }
            if (!found_release) for (unsigned i = 1; i < count_; ++i)
                if (voices_[order_[i]].age < voices_[order_[victim]].age) victim = i;
            slot = order_[victim];
            sink_.clear_slot(slot);
            erase(victim);
            ++steals_;
        } else {
            slot = free_[0];
            for (unsigned i = 1; i < free_count_; ++i) free_[i - 1] = free_[i];
            --free_count_;
        }
        auto& voice = voices_[slot];
        voice.env1.clear(); voice.env2.clear();
        voice.env1.trigger(); voice.env2.trigger();
        voice.note = static_cast<std::uint8_t>(note);
        voice.velocity = velocity; voice.weights = weights; voice.age = age_++;
        voice.retire = false;
        sink_.start_slot(slot);
        order_[count_++] = slot;
        return true;
    }
    bool note_off(int note) noexcept {
        if (in_block_ || note < 0 || note > 127) return false;
        for (unsigned i = 0; i < count_; ++i) {
            auto& voice = voices_[order_[i]];
            if (voice.note == note && voice.env1.stage() != BlockEnvelope::Stage::Release) {
                voice.env1.release(); voice.env2.release();
            }
        }
        return true;
    }
    bool release_all() noexcept {
        if (in_block_) return false;
        for (unsigned i = 0; i < count_; ++i) {
            voices_[order_[i]].env1.release(); voices_[order_[i]].env2.release();
        }
        return true;
    }
    bool begin_block(std::uint32_t frames, bool skip_gates = false) noexcept {
        if (in_block_ || frames == 0 || frames > 4096) return false;
        in_block_ = true;
        for (unsigned i = 0; i < count_; ++i) {
            const auto slot = order_[i];
            auto& voice = voices_[slot];
            voice.retire = !voice.env1.active() && !voice.env2.active();
            if (voice.retire) continue;
            const auto first = endpoint(voice.env1, frames);
            const auto second = endpoint(voice.env2, frames);
            if (!skip_gates) sink_.gate(slot, voice.note,
                first * voice.velocity * voice.weights.osc1,
                second * voice.velocity * voice.weights.osc2, voice.weights.shimmer);
        }
        return true;
    }
    bool end_block() noexcept {
        if (!in_block_) return false;
        for (unsigned i = 0; i < count_;) {
            const auto slot = order_[i];
            if (voices_[slot].retire) {
                // Slot retirement/recycling follows render. The DSP owner
                // separately clears inactive slot gates BEFORE compute, as
                // v1 does; this cleanup is not a substitute for that pass.
                sink_.clear_slot(slot);
                free_[free_count_++] = slot;
                erase(i);
            } else ++i;
        }
        in_block_ = false;
        return true;
    }
    unsigned size() const noexcept { return count_; }
    unsigned slot_at(unsigned index) const noexcept { return index < count_ ? order_[index] : capacity; }
    const Voice* voice_at(unsigned index) const noexcept {
        return index < count_ ? &voices_[order_[index]] : nullptr;
    }
    std::uint64_t steals() const noexcept { return steals_; }
private:
    static double endpoint(BlockEnvelope& envelope, std::uint32_t frames) noexcept {
        // Original render's SUSTAIN shortcut does NOT update level. A live
        // sustain edit therefore changes gate but not the subsequent release
        // starting level. Preserve that quirk until an intentional sound change.
        if (envelope.stage() == BlockEnvelope::Stage::Sustain)
            return envelope.config().sustain_percent / 100;
        double value = 0;
        envelope.advance(frames, value);
        return value;
    }
    void erase(unsigned index) noexcept {
        for (unsigned i = index + 1; i < count_; ++i) order_[i - 1] = order_[i];
        --count_;
    }
    StageVoiceSink& sink_;
    std::array<Voice, capacity> voices_{};
    std::array<unsigned, capacity> order_{}, free_{};
    unsigned limit_, free_count_, count_{};
    std::uint64_t age_{}, steals_{};
    bool in_block_{};
};
} // namespace stave
