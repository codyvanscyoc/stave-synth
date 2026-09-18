#pragma once
#include "stave/stage_instrument.hpp"
#include "stave/recording_capture.hpp"
#include <atomic>
#include <cstring>

namespace stave {
// Deliberately small audition protocol, NOT the saved-preset/UI schema. Only
// absolute controls cross this SPSC queue; MIDI stays on the audio owner.
enum class AuditionControl : unsigned {
    Piano, Osc1, Osc2, Cutoff, Wet, Master, Wave1, Wave2, Attack, Release,
    Resonance, PianoRoom, PianoReverb, DelayWet, DelayFeedback, Shimmer,
    ShimmerMix, Reverb, Freeze, ReleaseAll, PianoTone,
    BedLevel, BedKey, BedRise, BedRiseCutoff, BedMellow, BedMellowCutoff, BedFade, BedRelease,
    Attack1, Decay1, Sustain1, Release1, Attack2, Decay2, Sustain2, Release2, EnvelopeLink,
    MasterLow, MasterMid, MasterHigh, MasterLowcut, MasterLowcutHz,
    PianoRoomSize, PianoRoomDamp, ReverbDecay, ReverbPredelay, ReverbLowcut, ReverbHighcut, ReverbDamp,
    DelayTime, DelayLowcut, DelayHighcut, Count
};
inline constexpr std::array<const char*,unsigned(AuditionControl::Count)> audition_names{
    "piano","osc1","osc2","cutoff","wet","master","wave1","wave2","attack","release",
    "resonance","piano_room","piano_reverb","delay_wet","delay_feedback","shimmer",
    "shimmer_mix","reverb","freeze","release_all","piano_tone",
    "bed_level","bed_key","bed_rise","bed_rise_cutoff","bed_mellow","bed_mellow_cutoff","bed_fade","bed_release",
    "attack1","decay1","sustain1","release1","attack2","decay2","sustain2","release2","envelope_link",
    "master_low","master_mid","master_high","master_lowcut","master_lowcut_hz",
    "piano_room_size","piano_room_damp","reverb_decay","reverb_predelay","reverb_lowcut","reverb_highcut","reverb_damp",
    "delay_time","delay_lowcut","delay_highcut"};
inline bool audition_value_valid(AuditionControl c,double v) noexcept {
    if(!std::isfinite(v)) return false;
    switch(c) {
    case AuditionControl::Cutoff: return v>=20&&v<=20000;
    case AuditionControl::Attack: case AuditionControl::Attack1: case AuditionControl::Attack2: return v>=0&&v<=10000;
    case AuditionControl::Release: case AuditionControl::Release1: case AuditionControl::Release2: return v>=0&&v<=30000;
    case AuditionControl::Decay1: case AuditionControl::Decay2: return v>=0&&v<=20000;
    case AuditionControl::Sustain1: case AuditionControl::Sustain2: return v>=0&&v<=100;
    case AuditionControl::MasterLow: case AuditionControl::MasterMid: case AuditionControl::MasterHigh: return v>=-6&&v<=6;
    case AuditionControl::MasterLowcutHz: return v>=20&&v<=200;
    case AuditionControl::ReverbDecay: return v>=0&&v<=30;
    case AuditionControl::ReverbPredelay: return v>=0&&v<=150;
    case AuditionControl::ReverbLowcut: case AuditionControl::ReverbHighcut: return v>=20&&v<=20000;
    case AuditionControl::DelayTime: return v>=1&&v<=1000;
    case AuditionControl::DelayLowcut: return v>=20&&v<=1000;
    case AuditionControl::DelayHighcut: return v>=500&&v<=20000;
    case AuditionControl::PianoRoomDamp: case AuditionControl::ReverbDamp: return v>=0&&v<=.99;
    case AuditionControl::Resonance: return v>=.5&&v<=10;
    case AuditionControl::BedKey: return v>=0&&v<=11&&v==std::floor(v);
    case AuditionControl::BedRise: return v>=0&&v<=60;
    case AuditionControl::BedRiseCutoff: return v>=200&&v<=20000;
    case AuditionControl::BedMellowCutoff: return v>=100&&v<=8000;
    case AuditionControl::Wave1: case AuditionControl::Wave2: return v>=0&&v<=4&&v==std::floor(v);
    case AuditionControl::Reverb: return v>=0&&v<=6&&v==std::floor(v);
    case AuditionControl::Shimmer: case AuditionControl::Freeze: case AuditionControl::ReleaseAll:
    case AuditionControl::EnvelopeLink: case AuditionControl::MasterLowcut:
    case AuditionControl::BedMellow: case AuditionControl::BedFade: case AuditionControl::BedRelease:
        return v==0||v==1;
    case AuditionControl::DelayFeedback: return v>=0&&v<=.99;
    case AuditionControl::Count: return false;
    default: return unsigned(c)<unsigned(AuditionControl::Count)&&v>=0&&v<=1;
    }
}
enum class AuditionFault : unsigned { None, Stop, GraphContract, MidiOverflow, InvalidMidi, Engine };
struct AuditionCommand { std::uint64_t id{}; AuditionControl control{}; double value{}; };
struct AuditionMidi { unsigned offset{},size{}; std::array<unsigned char,3> bytes{}; };

// Fixed whole-block audition owner. No sample slicing, render-ahead queue,
// persistence, ports, drivers or HTTP. Every callback's MIDI is applied in
// order at that block's start; offsets are counted but NOT sample-accurate.
// This intentional audition timing policy can advance events by <one block
// on the render timeline. It is not end-to-end keyboard/DAC latency evidence.
class AuditionSession final {
public:
    static constexpr unsigned capacity=128, controls_per_block=32, midi_limit=256;
    // Optional one-take transport must outlive this session and its callback.
    // Attaching is pre-activation only; file worker/start/stop UI is separate.
    explicit AuditionSession(StageInstrument& instrument,RecordingCapture<>* capture=nullptr):graph_(instrument),capture_(capture) {
        for(unsigned i=0;i<12;++i) if(graph_.bed_loaded(i)) bed_mask_|=1u<<i;
        config_.owned_motion=true;
        config_.fader1=config_.fader2=0; // start piano-only; no saved patch imported
        config_.output.volume=0; // always silent until explicitly raised
        config_.piano.highcut_smoothing_ms=80;
        if((capture_&&capture_->block_frames()!=graph_.block_frames())||!graph_.configure(config_)) request_stop(AuditionFault::Engine);
    }
    // One non-audio producer. Full/invalid controls are rejected, never
    // acknowledged as applied; no MIDI note-off can be lost in this queue.
    bool enqueue(AuditionCommand c) noexcept {
        if(fault()!=AuditionFault::None||!c.id||c.id<=previous_id_||!audition_value_valid(c.control,c.value)) return false;
        if(c.control>=AuditionControl::BedLevel&&c.control<=AuditionControl::BedRelease&&(!bed_mask_||
           (c.control==AuditionControl::BedKey&&!(bed_mask_&(1u<<unsigned(c.value)))))) return false;
        const auto h=head_.load(std::memory_order_relaxed),t=tail_.load(std::memory_order_acquire);
        if(h-t>=capacity) return false;
        commands_[h%capacity]=c; previous_id_=c.id;
        head_.store(h+1,std::memory_order_release); return true;
    }
    void request_stop(AuditionFault why=AuditionFault::Stop) noexcept {
        auto expected=AuditionFault::None;
        fault_.compare_exchange_strong(expected,why,std::memory_order_release,std::memory_order_relaxed);
    }
    AuditionFault fault() const noexcept { return fault_.load(std::memory_order_acquire); }
    std::uint64_t applied() const noexcept { return applied_.load(std::memory_order_acquire); }
    std::uint64_t blocks() const noexcept { return blocks_.load(std::memory_order_relaxed); }
    std::uint64_t notes() const noexcept { return notes_.load(std::memory_order_relaxed); }
    std::uint64_t unsupported_midi() const noexcept { return unsupported_.load(std::memory_order_relaxed); }
    std::uint64_t quantized_midi() const noexcept { return quantized_.load(std::memory_order_relaxed); }
    std::uint64_t piano_full_scale() const noexcept { return full_scale_.load(std::memory_order_relaxed); }
    unsigned bed_mask() const noexcept { return bed_mask_; } // immutable before publication
    unsigned active_beds() const noexcept { return active_beds_.load(std::memory_order_relaxed); }
    int bed_key() const noexcept { return bed_key_.load(std::memory_order_relaxed); }
    // Invalid output pointers leave memory untouched. Driver must obtain valid
    // JACK buffers and silence them on graph-size/rate mismatch itself.
    bool process(float* l,float* r,unsigned frames,const AuditionMidi* events,unsigned count) noexcept {
        if(!l||!r||l==r||frames!=graph_.block_frames()) { request_stop(AuditionFault::GraphContract); return false; }
        const auto a=reinterpret_cast<std::uintptr_t>(l),b=reinterpret_cast<std::uintptr_t>(r);
        if((a<b?b-a:a-b)<frames*sizeof(float)) { request_stop(AuditionFault::GraphContract); return false; }
        if(count>midi_limit) request_stop(AuditionFault::MidiOverflow);
        if(count&&!events) request_stop(AuditionFault::InvalidMidi);
        if(fault()!=AuditionFault::None) return silence(l,r,frames);
        // Validate the whole MIDI batch before mutating notes. Truncated data
        // could contain a lost release, so malformed packets fault closed.
        unsigned previous=0;
        for(unsigned i=0;i<count;++i) {
            const auto& e=events[i];
            if(!valid_midi(e,frames)||e.offset<previous) { request_stop(AuditionFault::InvalidMidi); return silence(l,r,frames); }
            previous=e.offset;
        }
        const auto h=head_.load(std::memory_order_acquire);
        auto t=tail_.load(std::memory_order_relaxed);
        for(unsigned n=0;t!=h&&n<controls_per_block;++n) {
            const auto c=commands_[t%capacity];
            if(!apply(c)) { request_stop(AuditionFault::Engine); return silence(l,r,frames); }
            ++t; tail_.store(t,std::memory_order_release);
            applied_.store(c.id,std::memory_order_release);
        }
        for(unsigned i=0;i<count;++i) {
            if(!midi(events[i])) { request_stop(AuditionFault::Engine); return silence(l,r,frames); }
            if(events[i].offset) quantized_.fetch_add(1,std::memory_order_relaxed);
        }
        if(fault()!=AuditionFault::None||!graph_.render_block()) {
            request_stop(AuditionFault::Engine); return silence(l,r,frames);
        }
        full_scale_.store(graph_.stats().full_scale_piano_samples,std::memory_order_relaxed);
        active_beds_.store(graph_.active_beds(),std::memory_order_relaxed);
        // Same pre-volume/BTL tap as v1. Recorder failures are separate from
        // instrument faults: a stalled disk must never interrupt playing.
        if(capture_) capture_->push(graph_.recording_tap(0),graph_.recording_tap(1),frames);
        std::copy_n(graph_.pcm(0),frames,l); std::copy_n(graph_.pcm(1),frames,r);
        // Legacy output smoothing starts at .85; setting its target to zero
        // is not an immediate mute. Separate startup gate prevents even dither
        // escaping before the player's first explicit nonzero master command.
        if(!audition_unmuted_) { std::fill_n(l,frames,0); std::fill_n(r,frames,0); }
        if(fault()!=AuditionFault::None) return silence(l,r,frames);
        blocks_.fetch_add(1,std::memory_order_relaxed); return true;
    }
private:
    static bool valid_midi(const AuditionMidi& e,unsigned n) noexcept {
        if(!e.size||e.offset>=n||e.bytes[0]<0x80) return false;
        const unsigned type=e.bytes[0]&0xf0;
        if(type==0xf0) return true; // system/SysEx: ignored, no data dereference
        const unsigned needed=(type==0xc0||type==0xd0)?2:3;
        return e.size==needed&&e.bytes[1]<128&&(needed==2||e.bytes[2]<128);
    }
    bool midi(const AuditionMidi& e) noexcept {
        const unsigned type=e.bytes[0]&0xf0;
        if(type==0xf0) return true; // clock/sensing/system messages not controls
        const int note=e.bytes[1],v=e.bytes[2];
        auto send=[&](StageAction a,int key,int value) { return graph_.key_command(graph_.frame_position(),a,key,value); };
        if(type==0x90) { if(v) notes_.fetch_add(1,std::memory_order_relaxed); return send(StageAction::NoteOn,note,v); }
        if(type==0x80) return send(StageAction::NoteOff,note,0);
        if(type==0xb0) {
            if(note==64||note==66) return send(note==64?StageAction::Sustain:StageAction::Sostenuto,0,v>=64);
            if(note==123) return send(StageAction::ReleaseAll,0,0);
            if(note==120) { request_stop(); return true; }
        }
        unsupported_.fetch_add(1,std::memory_order_relaxed); return true;
    }
    bool apply(const AuditionCommand& c) noexcept {
        using C=AuditionControl; const double v=c.value;
        switch(c.control) {
        case C::Reverb: return graph_.reverb_type(static_cast<ReverbType>(unsigned(v)));
        case C::Freeze: return graph_.freeze(v!=0);
        case C::ReleaseAll: return v==0||graph_.key_command(graph_.frame_position(),StageAction::ReleaseAll,0,0);
        case C::Piano: config_.piano.volume=v; break;
        case C::BedLevel: config_.bed.level=v; break;
        case C::BedMellow: config_.bed.mellow=v!=0; break;
        case C::BedMellowCutoff: config_.bed.mellow_hz=v; break;
        case C::BedRise: bed_rise_=v; return true;
        case C::BedRiseCutoff: bed_rise_cutoff_=v; return true;
        case C::BedKey:
            if(!graph_.trigger_bed(unsigned(v),bed_rise_,bed_rise_cutoff_)) return false;
            bed_key_.store(int(v),std::memory_order_relaxed); return true;
        case C::BedFade: return graph_.fade_bed(v!=0);
        case C::BedRelease:
            if(v!=0) { graph_.release_bed(); bed_key_.store(-1,std::memory_order_relaxed); }
            return true;
        // Brightness0..1 ->200..20000Hz; full bright is the auditioned default.
        case C::PianoTone: config_.piano.highcut_hz=v==1?20000:200*std::pow(100.,v); break;
        case C::Osc1: config_.fader1=v; break;
        case C::Osc2: config_.fader2=v; break;
        case C::Cutoff: config_.buses.pad.cutoff=v; break;
        case C::Wet: config_.wet=v; break;
        case C::Master: config_.output.volume=v; if(v>0) audition_unmuted_=true; break;
        case C::Wave1: config_.wave1=int(v); break;
        case C::Wave2: config_.wave2=int(v); break;
        case C::Attack: config_.env1.attack_ms=config_.env2.attack_ms=v; break;
        case C::Release: config_.env1.release_ms=config_.env2.release_ms=v; break;
        // LINK changes editing behavior only. One command changes both existing
        // envelopes before a single configure; no intermediate rendered state.
        case C::EnvelopeLink: envelope_link_=v!=0; return true;
        case C::Attack1: config_.env1.attack_ms=v; if(envelope_link_) config_.env2.attack_ms=v; break;
        case C::Decay1: config_.env1.decay_ms=v; if(envelope_link_) config_.env2.decay_ms=v; break;
        case C::Sustain1: config_.env1.sustain_percent=v; if(envelope_link_) config_.env2.sustain_percent=v; break;
        case C::Release1: config_.env1.release_ms=v; if(envelope_link_) config_.env2.release_ms=v; break;
        case C::Attack2: config_.env2.attack_ms=v; if(envelope_link_) config_.env1.attack_ms=v; break;
        case C::Decay2: config_.env2.decay_ms=v; if(envelope_link_) config_.env1.decay_ms=v; break;
        case C::Sustain2: config_.env2.sustain_percent=v; if(envelope_link_) config_.env1.sustain_percent=v; break;
        case C::Release2: config_.env2.release_ms=v; if(envelope_link_) config_.env1.release_ms=v; break;
        case C::MasterLow: config_.master.eq[0].gain=v; break;
        case C::MasterMid: config_.master.eq[1].gain=v; break;
        case C::MasterHigh: config_.master.eq[2].gain=v; break;
        case C::MasterLowcut: config_.master.highpass=v!=0; break;
        case C::MasterLowcutHz: config_.master.cutoff=v; break;
        case C::PianoRoomSize: config_.buses.room.size=v; break;
        case C::PianoRoomDamp: config_.buses.room.damp=v; break;
        case C::ReverbDecay: return graph_.reverb_control(ReverbControl::Decay,v);
        case C::ReverbPredelay: return graph_.reverb_control(ReverbControl::Predelay,v);
        case C::ReverbLowcut: return graph_.reverb_control(ReverbControl::LowCut,v);
        case C::ReverbHighcut: return graph_.reverb_control(ReverbControl::HighCut,v);
        case C::ReverbDamp: return graph_.reverb_control(ReverbControl::Damp,v);
        case C::DelayTime: config_.delay.division=DelayDivision::Free; config_.delay.milliseconds=v; break;
        case C::DelayLowcut: config_.delay.lowcut=v; break;
        case C::DelayHighcut: config_.delay.highcut=v; break;
        case C::Resonance: config_.buses.pad.resonance=v; break;
        case C::PianoRoom: config_.buses.room.wet=v; break;
        case C::PianoReverb: config_.piano_reverb_send=v; break;
        case C::DelayWet: config_.delay.wet=v; config_.delay.enabled=v>0; break;
        case C::DelayFeedback: config_.delay.feedback=v; break;
        case C::Shimmer: config_.buses.pad.shimmer=v!=0; break;
        case C::ShimmerMix: config_.buses.pad.shimmer_mix=v; break;
        case C::Count: return false;
        }
        return graph_.configure(config_);
    }
    bool silence(float* l,float* r,unsigned n) noexcept {
        if(capture_) capture_->finish(CaptureEnd::EngineStopped);
        active_beds_.store(0,std::memory_order_relaxed); bed_key_.store(-1,std::memory_order_relaxed);
        graph_.stop(); std::fill_n(l,n,0); std::fill_n(r,n,0); return false;
    }
    static_assert(std::atomic<std::uint64_t>::is_always_lock_free);
    static_assert(std::atomic<AuditionFault>::is_always_lock_free);
    StageInstrument& graph_;
    RecordingCapture<>* capture_{};
    unsigned bed_mask_{};
    double bed_rise_{},bed_rise_cutoff_{3000};
    std::atomic<unsigned> active_beds_{0};
    std::atomic<int> bed_key_{-1};
    StageInstrumentConfig config_{};
    std::array<AuditionCommand,capacity> commands_{};
    alignas(64) std::atomic<std::uint64_t> head_{0},tail_{0};
    std::uint64_t previous_id_{};
    bool audition_unmuted_{}; // audio owner only; not a live mute toggle
    bool envelope_link_{};
    std::atomic<AuditionFault> fault_{AuditionFault::None};
    std::atomic<std::uint64_t> applied_{0},blocks_{0},notes_{0},unsupported_{0},quantized_{0},full_scale_{0};
};
} // namespace stave
