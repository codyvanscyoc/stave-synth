#pragma once
#include "stave/stage_lowpass.hpp"
#include <array>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <vector>

namespace stave {
// Prepared immutable native-rate audio. Construct/load/destroy OFF the audio
// thread. No WAV decoder, resampler, file I/O or live asset swap in this class.
// The loop overlap is baked once with the original equal-power law, removing
// per-sample trigonometry from performance. No pitch shift: each slot is the
// recorded concert key, independent of keyboard transposition and pedals.
class PreparedBed final {
public:
    static constexpr std::size_t max_bytes=32*1024*1024;
    PreparedBed(const double* left,const double* right,std::size_t frames,unsigned sample_rate=48000) {
        if(sample_rate!=48000||!left||!right||frames<4||frames>max_bytes/sizeof(Frame))
            throw std::invalid_argument("Bed requires bounded stereo48k PCM, at least4 frames");
        audio_.resize(frames);
        for(std::size_t i=0;i<frames;++i) {
            if(!std::isfinite(left[i])||!std::isfinite(right[i]))
                throw std::invalid_argument("Nonfinite bed PCM");
            audio_[i]={left[i],right[i]};
        }
        overlap_=std::max(std::size_t(1),std::min(std::size_t(24000),frames/4));
        const auto start=frames-overlap_;
        for(std::size_t j=0;j<overlap_;++j) {
            const double angle=double(j)/double(overlap_)*(3.14159265358979323846*.5);
            const double a=std::cos(angle),b=std::sin(angle);
            for(unsigned c=0;c<2;++c) {
                audio_[start+j][c]=audio_[start+j][c]*a+audio_[j][c]*b;
                if(!std::isfinite(audio_[start+j][c])) throw std::invalid_argument("Bed overlap overflow");
            }
        }
    }
    PreparedBed(const PreparedBed&)=delete;
    PreparedBed& operator=(const PreparedBed&)=delete;
    using Frame=std::array<double,2>;
    const Frame& frame(std::size_t i) const noexcept { return audio_[i]; }
    std::size_t frames() const noexcept { return audio_.size(); }
    std::size_t overlap() const noexcept { return overlap_; }
    std::size_t bytes() const noexcept { return audio_.size()*sizeof(Frame); }
private:
    std::vector<Frame> audio_;
    std::size_t overlap_{};
};

// Fixed12-slot bank port of SamplePlayer's loop,4s AR and optional rise sweep.
// Install ONLY before seal(); all following performance operations are one
// audio owner's bounded, allocation-free calls. Assets are never retired in
// process(). Live load/record handoff is a separate, still-required integration.
class SampledBed final {
public:
    static constexpr unsigned slots=12;
    static constexpr std::size_t max_bytes=128*1024*1024;
    explicit SampledBed(std::size_t byte_budget=max_bytes):budget_(byte_budget) {
        if(!budget_||budget_>max_bytes) throw std::invalid_argument("Invalid bed bank memory budget");
    }
    bool install(unsigned slot,std::unique_ptr<const PreparedBed>& asset) noexcept {
        if(sealed_||slot>=slots) return false;
        const auto old=assets_[slot]?assets_[slot]->bytes():0;
        const auto next=asset?asset->bytes():0;
        if(next>budget_-(bytes_-old)) return false;
        bytes_=bytes_-old+next;
        assets_[slot].swap(asset); // old asset returned to non-audio caller
        return true;
    }
    void seal() noexcept { sealed_=true; }
    std::size_t bytes() const noexcept { return bytes_; }
    bool loaded(unsigned slot) const noexcept { return slot<slots&&bool(assets_[slot]); }
    bool healthy() const noexcept { return !fault_; }
    unsigned active() const noexcept {
        unsigned count=0; for(const auto& v:voices_) count+=v.active; return count;
    }
    bool trigger(unsigned slot,double rise_seconds=0,double open_hz=3000) noexcept {
        if(!sealed_||fault_||!loaded(slot)||!std::isfinite(rise_seconds)||rise_seconds<0||rise_seconds>60||
           !std::isfinite(open_hz)||open_hz<200||open_hz>20000) return false;
        for(unsigned i=0;i<slots;++i) if(i!=slot) release(voices_[i]);
        auto& v=voices_[slot];
        if(!v.active) { v.position=0; v.envelope=0; }
        v.active=true; v.target=1; v.rise_seconds=rise_seconds; v.open_hz=open_hz;
        v.rise_frames=0; v.rising=v.filtered=rise_seconds>0;
        if(v.rising) {
            v.envelope=0;
            for(auto& f:v.filter) { f={}; f.tune(200,.707); }
        }
        return true;
    }
    void release_all() noexcept { for(auto& v:voices_) release(v); }
    void stop() noexcept { for(auto& v:voices_) v=Voice{}; } // keeps immutable bank
    // Adds to caller-owned stereo buffers, at most512 frames. Invalid arguments
    // do not advance state or touch output. Caller composes master/limiter next.
    bool process(double* left,double* right,unsigned n) noexcept {
        if(!sealed_||fault_||!left||!right||n==0||n>512) return false;
        const auto a=reinterpret_cast<std::uintptr_t>(left),b=reinterpret_cast<std::uintptr_t>(right);
        if((a<b?b-a:a-b)<n*sizeof(double)) return false;
        for(unsigned i=0;i<n;++i) if(!std::isfinite(left[i])||!std::isfinite(right[i])) return fail(left,right,n);
        for(unsigned slot=0;slot<slots;++slot) {
            auto& v=voices_[slot]; if(!v.active) continue;
            const auto& audio=*assets_[slot];
            double start=v.envelope,end=v.envelope;
            if(v.rising&&v.target>0) {
                start=rise_envelope(double(v.rise_frames)/48000,v.rise_seconds);
                // Saturate the clock after the rise/decay has settled.
                const auto limit=std::uint64_t(std::ceil((v.rise_seconds+.8)*48000))+512;
                v.rise_frames=std::min(v.rise_frames+n,limit);
                end=rise_envelope(double(v.rise_frames)/48000,v.rise_seconds);
            } else {
                const double step=double(n)/(4*48000);
                if(v.target>start) end=std::min(v.target,start+step);
                else if(v.target<start) end=std::max(v.target,start-step);
                // Preserve original final-block gate; not a redesigned envelope.
                if(v.target<.001&&end<=.0001) { v=Voice{}; continue; }
            }
            v.envelope=end;
            if(v.filtered) {
                const double time=double(v.rise_frames>=n?v.rise_frames-n:0)/48000;
                double cutoff=300;
                if(time<v.rise_seconds) cutoff=200*std::pow(v.open_hz/200,time/v.rise_seconds);
                else if(time<v.rise_seconds+.8) cutoff=v.open_hz*std::pow(300/v.open_hz,(time-v.rise_seconds)/.8);
                for(auto& f:v.filter) f.tune(cutoff,.707);
            }
            const double step=n>1?(end-start)/double(n-1):0;
            for(unsigned i=0;i<n;++i) {
                const auto& sample=audio.frame(v.position);
                const double gain=n>1&&i+1==n?end:start+i*step;
                double l=sample[0],r=sample[1];
                if(v.filtered) { l=v.filter[0].tick(l); r=v.filter[1].tick(r); }
                left[i]+=l*gain; right[i]+=r*gain;
                if(++v.position==audio.frames()) v.position=audio.overlap();
            }
        }
        for(unsigned i=0;i<n;++i) if(!std::isfinite(left[i])||!std::isfinite(right[i])) return fail(left,right,n);
        return true;
    }
private:
    struct Voice {
        std::size_t position{};
        double envelope{},target{},rise_seconds{},open_hz{3000};
        std::uint64_t rise_frames{};
        bool active{},rising{},filtered{};
        std::array<StageLowpass,2> filter{};
    };
    static void release(Voice& v) noexcept { v.target=0; v.rising=false; }
    static double rise_envelope(double t,double rise) noexcept {
        if(t<rise) return t/rise;
        if(t<rise+.8) return 1-.45*((t-rise)/.8);
        return .55;
    }
    bool fail(double* l,double* r,unsigned n) noexcept {
        fault_=true; stop(); std::fill_n(l,n,0); std::fill_n(r,n,0); return false;
    }
    std::array<std::unique_ptr<const PreparedBed>,slots> assets_{};
    std::array<Voice,slots> voices_{};
    std::size_t bytes_{};
    const std::size_t budget_;
    bool sealed_{},fault_{};
};
} // namespace stave
