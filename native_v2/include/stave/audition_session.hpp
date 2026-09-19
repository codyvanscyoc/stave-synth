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
    DelayTime, DelayLowcut, DelayHighcut, RecordStart, RecordStop,
    Transpose, PianoOctave, Octave1, Octave2, Pan1, Pan2, Detune, Spread,
    PianoLowcut, PianoVelocity, Osc1Reverb, Osc2Reverb, PianoDelay,
    FilterSlope, PianoFilter, Bpm, DelayDivision, Count
};
inline constexpr std::array<const char*,unsigned(AuditionControl::Count)> audition_names{
    "piano","osc1","osc2","cutoff","wet","master","wave1","wave2","attack","release",
    "resonance","piano_room","piano_reverb","delay_wet","delay_feedback","shimmer",
    "shimmer_mix","reverb","freeze","release_all","piano_tone",
    "bed_level","bed_key","bed_rise","bed_rise_cutoff","bed_mellow","bed_mellow_cutoff","bed_fade","bed_release",
    "attack1","decay1","sustain1","release1","attack2","decay2","sustain2","release2","envelope_link",
    "master_low","master_mid","master_high","master_lowcut","master_lowcut_hz",
    "piano_room_size","piano_room_damp","reverb_decay","reverb_predelay","reverb_lowcut","reverb_highcut","reverb_damp",
    "delay_time","delay_lowcut","delay_highcut","record_start","record_stop",
    "transpose","piano_octave","octave1","octave2","pan1","pan2","detune","spread",
    "piano_lowcut","piano_velocity","osc1_reverb","osc2_reverb","piano_delay",
    "filter_slope","piano_filter","bpm","delay_division"};
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
    case AuditionControl::Transpose: return v>=-24&&v<=24&&v==std::floor(v);
    case AuditionControl::PianoOctave: case AuditionControl::Octave1: case AuditionControl::Octave2:
        return v>=-3&&v<=3&&v==std::floor(v);
    case AuditionControl::Pan1: case AuditionControl::Pan2: return v>=-1&&v<=1;
    case AuditionControl::PianoLowcut: return v>=20&&v<=500;
    case AuditionControl::PianoVelocity: return v>=1&&v<=4;
    case AuditionControl::Bpm: return v>=40&&v<=240;
    case AuditionControl::DelayDivision: return v>=0&&v<=8&&v==std::floor(v);
    case AuditionControl::Reverb: return v>=0&&v<=6&&v==std::floor(v);
    case AuditionControl::Shimmer: case AuditionControl::Freeze: case AuditionControl::ReleaseAll:
    case AuditionControl::EnvelopeLink: case AuditionControl::MasterLowcut:
    case AuditionControl::FilterSlope: case AuditionControl::PianoFilter:
    case AuditionControl::BedMellow: case AuditionControl::BedFade: case AuditionControl::BedRelease:
    case AuditionControl::RecordStart: case AuditionControl::RecordStop:
        return v==0||v==1;
    case AuditionControl::DelayFeedback: return v>=0&&v<=.99;
    case AuditionControl::Count: return false;
    default: return unsigned(c)<unsigned(AuditionControl::Count)&&v>=0&&v<=1;
    }
}
inline bool audition_mappable(AuditionControl c) noexcept {
    using C=AuditionControl;
    return c!=C::ReleaseAll&&c!=C::BedKey&&c!=C::BedFade&&c!=C::BedRelease&&
           c!=C::RecordStart&&c!=C::RecordStop&&c!=C::ReverbLowcut&&c!=C::ReverbHighcut&&
           c!=C::DelayLowcut&&c!=C::DelayHighcut&&c!=C::PianoLowcut&&
           c!=C::PianoVelocity&&c!=C::FilterSlope&&c!=C::PianoFilter&&c!=C::Count;
}
inline double audition_midi_value(AuditionControl c,unsigned raw) noexcept {
    using C=AuditionControl; double lo=0,hi=1,step=.01;
    switch(c) {
    case C::Cutoff: lo=20; hi=20000; step=1; break;
    case C::Wave1: case C::Wave2: lo=0; hi=4; step=1; break;
    case C::Attack: case C::Attack1: case C::Attack2: lo=0; hi=10000; step=1; break;
    case C::Decay1: case C::Decay2: lo=0; hi=20000; step=1; break;
    case C::Sustain1: case C::Sustain2: lo=0; hi=100; step=.1; break;
    case C::Release: case C::Release1: case C::Release2: lo=0; hi=30000; step=1; break;
    case C::Resonance: lo=.5; hi=10; step=.01; break;
    case C::Reverb: lo=0; hi=6; step=1; break;
    case C::DelayFeedback: case C::PianoRoomDamp: case C::ReverbDamp: hi=.99; break;
    case C::BedRise: hi=60; step=.5; break;
    case C::BedRiseCutoff: lo=200; hi=20000; step=1; break;
    case C::BedMellowCutoff: lo=100; hi=8000; step=1; break;
    case C::MasterLow: case C::MasterMid: case C::MasterHigh: lo=-6; hi=6; step=.1; break;
    case C::MasterLowcutHz: lo=20; hi=200; step=1; break;
    case C::ReverbDecay: hi=30; step=.1; break;
    case C::ReverbPredelay: hi=150; step=1; break;
    case C::ReverbLowcut: case C::ReverbHighcut: lo=20; hi=20000; step=1; break;
    case C::DelayTime: lo=1; hi=1000; step=1; break;
    case C::DelayLowcut: lo=20; hi=1000; step=1; break;
    case C::DelayHighcut: lo=500; hi=20000; step=1; break;
    case C::Transpose: lo=-24; hi=24; step=1; break;
    case C::PianoOctave: case C::Octave1: case C::Octave2: lo=-3; hi=3; step=1; break;
    case C::Pan1: case C::Pan2: lo=-1; hi=1; step=.01; break;
    case C::PianoLowcut: lo=20; hi=500; step=1; break;
    case C::PianoVelocity: lo=1; hi=4; step=.01; break;
    case C::Bpm: lo=40; hi=240; step=1; break;
    case C::DelayDivision: lo=0; hi=8; step=1; break;
    case C::Shimmer: case C::Freeze: case C::EnvelopeLink: case C::MasterLowcut: case C::BedMellow:
    case C::FilterSlope: case C::PianoFilter:
        step=1; break;
    default: break;
    }
    const double value=lo+(hi-lo)*std::min(raw,127u)/127.;
    return std::clamp(lo+std::round((value-lo)/step)*step,lo,hi);
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
        for(auto& mapped:cc_map_) mapped.store(-1,std::memory_order_relaxed);
        for(auto& raw:mapped_raw_) raw.store(-1,std::memory_order_relaxed);
        for(auto& serial:mapped_serial_) serial.store(0,std::memory_order_relaxed);
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
        if((c.control==AuditionControl::RecordStart||c.control==AuditionControl::RecordStop)&&!capture_) return false;
        if(c.control==AuditionControl::RecordStart&&capture_->end()!=CaptureEnd::Idle) return false;
        if(c.control==AuditionControl::RecordStop&&capture_->end()!=CaptureEnd::Open) return false;
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
    std::uint64_t midi_cc_serial() const noexcept { return midi_cc_serial_.load(std::memory_order_relaxed); }
    unsigned midi_cc() const noexcept { return midi_cc_.load(std::memory_order_relaxed); }
    unsigned midi_cc_value() const noexcept { return midi_cc_value_.load(std::memory_order_relaxed); }
    std::uint64_t midi_apply_serial() const noexcept { return midi_apply_serial_.load(std::memory_order_relaxed); }
    int midi_apply_control() const noexcept { return midi_apply_control_.load(std::memory_order_relaxed); }
    unsigned midi_apply_value() const noexcept { return midi_apply_value_.load(std::memory_order_relaxed); }
    int midi_mapped_raw(unsigned control) const noexcept {
        return control<unsigned(AuditionControl::Count)?mapped_raw_[control].load(std::memory_order_relaxed):-1;
    }
    std::uint64_t midi_mapped_serial(unsigned control) const noexcept {
        return control<unsigned(AuditionControl::Count)?mapped_serial_[control].load(std::memory_order_acquire):0;
    }
    bool map_cc(unsigned cc,AuditionControl control) noexcept {
        if(cc>=128||cc==64||cc==66||cc==120||cc==123||!audition_mappable(control)) return false;
        for(auto& mapped:cc_map_) if(mapped.load(std::memory_order_relaxed)==int(control))
            mapped.store(-1,std::memory_order_release);
        const int old=cc_map_[cc].exchange(int(control),std::memory_order_acq_rel);
        if(old>=0&&old<int(AuditionControl::Count)) {
            mapped_raw_[unsigned(old)].store(-1,std::memory_order_relaxed);
            mapped_serial_[unsigned(old)].store(0,std::memory_order_release);
        }
        mapped_raw_[unsigned(control)].store(-1,std::memory_order_relaxed);
        mapped_serial_[unsigned(control)].store(0,std::memory_order_release); return true;
    }
    unsigned bed_mask() const noexcept { return bed_mask_; } // immutable before publication
    unsigned active_beds() const noexcept { return active_beds_.load(std::memory_order_relaxed); }
    int bed_key() const noexcept { return bed_key_.load(std::memory_order_relaxed); }
    CaptureEnd capture_end() const noexcept { return capture_?capture_->end():CaptureEnd::Idle; }
    std::uint64_t capture_frames() const noexcept { return capture_?capture_->frames_written():0; }
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
        std::array<double,unsigned(AuditionControl::Count)> mapped_values{};
        std::array<bool,unsigned(AuditionControl::Count)> mapped_seen{};
        for(unsigned i=0;i<count;++i) {
            const auto& event=events[i];
            if((event.bytes[0]&0xf0)==0xb0) {
                const unsigned cc=event.bytes[1],raw=event.bytes[2];
                midi_cc_.store(cc,std::memory_order_relaxed); midi_cc_value_.store(raw,std::memory_order_relaxed);
                midi_cc_serial_.fetch_add(1,std::memory_order_release);
                const int mapped=cc_map_[cc].load(std::memory_order_acquire);
                if(mapped>=0&&mapped<int(AuditionControl::Count)) {
                    const auto control=static_cast<AuditionControl>(mapped);
                    mapped_values[unsigned(control)]=audition_midi_value(control,raw); mapped_seen[unsigned(control)]=true;
                    mapped_raw_[unsigned(control)].store(int(raw),std::memory_order_relaxed);
                    midi_apply_control_.store(mapped,std::memory_order_relaxed); midi_apply_value_.store(raw,std::memory_order_relaxed);
                    const auto serial=midi_apply_serial_.fetch_add(1,std::memory_order_relaxed)+1;
                    mapped_serial_[unsigned(control)].store(serial,std::memory_order_release);
                }
            }
            if(!midi(events[i])) { request_stop(AuditionFault::Engine); return silence(l,r,frames); }
            if(events[i].offset) quantized_.fetch_add(1,std::memory_order_relaxed);
        }
        for(unsigned c=0;c<unsigned(AuditionControl::Count);++c) if(mapped_seen[c]&&
           !apply({0,static_cast<AuditionControl>(c),mapped_values[c]})) {
            request_stop(AuditionFault::Engine); return silence(l,r,frames);
        }
        if(fault()!=AuditionFault::None||!graph_.render_block()) {
            request_stop(AuditionFault::Engine); return silence(l,r,frames);
        }
        full_scale_.store(graph_.stats().full_scale_piano_samples,std::memory_order_relaxed);
        active_beds_.store(graph_.active_beds(),std::memory_order_relaxed);
        // Dedicated pad-source tap includes piano/synth/effects but excludes
        // an existing sampled bed and global master. Recorder failure remains
        // separate from instrument health: a stalled disk cannot stop playing.
        if(capture_&&capture_->active())
            capture_->push(graph_.pad_recording_tap(0),graph_.pad_recording_tap(1),frames);
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
            // Learned CCs were coalesced and applied earlier in this callback.
            if(cc_map_[unsigned(note)].load(std::memory_order_acquire)>=0) return true;
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
        case C::RecordStart: return v==0||capture_->start();
        case C::RecordStop: if(v!=0) capture_->request_stop(); return true;
        case C::Transpose: config_.transpose=int(v); break;
        case C::PianoOctave: config_.piano_octave=int(v); break;
        case C::Octave1: config_.octave1=int(v); break;
        case C::Octave2: config_.octave2=int(v); break;
        case C::Pan1: config_.pan1=v; break;
        case C::Pan2: config_.pan2=v; break;
        case C::Detune: config_.detune=v; break;
        case C::Spread: config_.spread=v; break;
        case C::PianoLowcut: config_.piano.lowcut_hz=v; break;
        case C::PianoVelocity: config_.piano_velocity_curve=v; break;
        case C::Osc1Reverb: config_.buses.pad.send1=v; break;
        case C::Osc2Reverb: config_.buses.pad.send2=v; break;
        case C::PianoDelay: config_.piano_delay_send=v; break;
        case C::FilterSlope: config_.buses.pad.slope24=v!=0; break;
        case C::PianoFilter: config_.piano_filter=v!=0; break;
        case C::Bpm: config_.motion.bpm=v; config_.modulation.bpm=v; break;
        case C::DelayDivision: config_.delay.division=static_cast<DelayDivision>(unsigned(v)); break;
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
        if(capture_&&capture_->active()) capture_->finish(CaptureEnd::EngineStopped);
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
    std::array<std::atomic<int>,128> cc_map_{};
    std::array<std::atomic<int>,unsigned(AuditionControl::Count)> mapped_raw_{};
    std::array<std::atomic<std::uint64_t>,unsigned(AuditionControl::Count)> mapped_serial_{};
    std::atomic<std::uint64_t> midi_cc_serial_{0},midi_apply_serial_{0};
    std::atomic<unsigned> midi_cc_{0},midi_cc_value_{0},midi_apply_value_{0};
    std::atomic<int> midi_apply_control_{-1};
};
} // namespace stave
