#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace stave {
struct OutputConfig {
    double volume{.85};
    bool btl{}; // Explicit legacy mono/inverted-R adapter, never auto-detected.
    bool valid() const noexcept { return std::isfinite(volume)&&volume>=0&&volume<=1; }
};
// Offline owner for the original bridge's *successful audio-block* path.
// No ring, JACK, underrun/recovery policy or hardware gain. Recorder tap is
// float32 PRE volume/BTL, as before. One owner; fixed 48k/256 or512.
class StageOutput final {
public:
    explicit StageOutput(unsigned frames=512):frames_(frames) {
        if(frames!=256&&frames!=512) throw std::invalid_argument("Output requires 256/512 frames");
    }
    bool configure(const OutputConfig& p) noexcept {
        if(!healthy()||!p.valid()) return false;
        config_=p; return true;
    }
    bool process(const std::array<const double*,2>& input) noexcept {
        for(const auto* p:input) {
            if(!p) return false;
            for(const auto* outputs:{&tap_,&pcm_}) for(const auto& c:*outputs) {
                const auto a=reinterpret_cast<std::uintptr_t>(p),b=reinterpret_cast<std::uintptr_t>(c.data());
                if(a<b?b-a<frames_*sizeof(double):a-b<frames_*sizeof(float)) return false;
            }
        }
        if(!healthy()) { silence(); return false; }
        // Refuse poisonous audio before advancing gain or exporting any part
        // of the block. Unlike v1's per-sample NaN repair this is terminal.
        for(const auto* p:input) for(unsigned i=0;i<frames_;++i)
            if(!std::isfinite(p[i])||std::abs(p[i])>std::numeric_limits<float>::max()) {
                fault_=true; silence(); return false;
            }
        const float target=static_cast<float>(config_.volume),alpha=1.0f-.99584f;
        peak_=0;
        for(unsigned i=0;i<frames_;++i) {
            tap_[0][i]=static_cast<float>(input[0][i]); tap_[1][i]=static_cast<float>(input[1][i]);
            volume_+=alpha*(target-volume_);
            float l=tap_[0][i]*volume_,r=tap_[1][i]*volume_;
            if(l>1) l=1; if(l< -1) l=-1;
            if(r>1) r=1; if(r< -1) r=-1;
            if(config_.btl) { const float mono=(l+r)*.5f; pcm_[0][i]=mono; pcm_[1][i]=-mono; }
            else { pcm_[0][i]=l; pcm_[1][i]=r; }
            const float a=l<0?-l:l,b=r<0?-r:r;
            if(a>peak_) peak_=a;
            if(b>peak_) peak_=b;
        }
        return true;
    }
    const float* channel(unsigned c) const noexcept { return c<2?pcm_[c].data():nullptr; }
    const float* recording_tap(unsigned c) const noexcept { return c<2?tap_[c].data():nullptr; }
    float volume() const noexcept { return volume_; }
    float peak() const noexcept { return peak_; } // Matches pre-BTL bridge meter.
    unsigned block_frames() const noexcept { return frames_; }
    bool healthy() const noexcept { return !stopped_&&!fault_; }
    void stop() noexcept { stopped_=true; silence(); }
private:
    void silence() noexcept { for(auto& c:tap_) c.fill(0); for(auto& c:pcm_) c.fill(0); peak_=0; }
    unsigned frames_; OutputConfig config_{}; float volume_{.85f},peak_{}; bool stopped_{},fault_{};
    std::array<std::array<float,512>,2> tap_{},pcm_{};
};
} // namespace stave
