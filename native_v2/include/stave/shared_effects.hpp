#pragma once
#include <array>
#include <cstdint>
#include <memory>

namespace stave {
// FREE followed by the eight existing musical subdivisions.
enum class DelayDivision { Free, Half, DottedQuarter, Quarter, QuarterTriplet,
                           DottedEighth, Eighth, EighthTriplet, Sixteenth };
struct DelayConfig {
    bool enabled{}, oblivion{}, aurora{};
    DelayDivision division{DelayDivision::Quarter}, reverse_division{DelayDivision::Free};
    double bpm{120}, milliseconds{375}, offset_ms{}, rate{1};
    double feedback{.35}, wet{}, motion{1}, lowcut{20}, highcut{18000};
    double drive{}, width{1}, mod_rate{.5}, mod_depth{};
    double reverse{}, reverse_ms{500}, reverse_feedback{}, aurora_seconds{5};
    bool valid() const noexcept;
};
// Single owner, fixed 48k/256 or512; all native state/scratch prepared at
// construction. Explicit optional external send: its dry contribution is
// subtracted after the original float32 boundary, preserving v1 rounding.
class StageDelay final {
public:
    explicit StageDelay(std::uint32_t frames=512);
    ~StageDelay();
    StageDelay(const StageDelay&)=delete;
    StageDelay& operator=(const StageDelay&)=delete;
    bool configure(const DelayConfig&) noexcept;
    bool process(const std::array<const double*,2>& dry,
                 const std::array<const double*,2>* external=nullptr) noexcept;
    const double* channel(unsigned) const noexcept;
    void clear() noexcept; // Retains configuration; not terminal-fault recovery.
    void stop() noexcept;
    bool healthy() const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

enum class ReverbType { Wash, Hall, Room, Plate, Bloom, Drone, Ghost };
enum class ReverbControl { Decay, LowCut, HighCut, Damp, Shimmer, Noise, Predelay };
// Wet-only original three-backend dispatcher. Controls are ordered operations:
// set_type applies its preset only on a type change, then explicit setters can
// override it. No loading, backend allocation or logging in these operations.
// Freeze here is REVERB freeze, not the independent sampled-bed feature.
class SharedReverb final {
public:
    explicit SharedReverb(std::uint32_t frames=512);
    ~SharedReverb();
    SharedReverb(const SharedReverb&)=delete;
    SharedReverb& operator=(const SharedReverb&)=delete;
    bool set_type(ReverbType) noexcept;
    bool set(ReverbControl,double) noexcept;
    bool freeze(bool) noexcept;
    bool process(const std::array<const double*,2>& input) noexcept;
    const double* channel(unsigned) const noexcept;
    void panic() noexcept; // Unfreeze, restore desired controls, clear all tanks.
    void stop() noexcept;
    bool healthy() const noexcept;
    ReverbType type() const noexcept;
    bool frozen() const noexcept;
    int capture_remaining() const noexcept;
    // Owner-only diagnostics. Missing optimized-out zones report NaN.
    double zone(unsigned backend,const char* label) const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace stave
