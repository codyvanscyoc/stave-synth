#pragma once
#include "stave/stage_core.hpp"
#include "stave/pad_ambience.hpp"
#include "stave/stage_master.hpp"
#include "stave/stage_output.hpp"
#include "stave/stage_splits.hpp"

namespace stave {
// Reuse the established source/room configuration without constructing a
// StageCore: PadAmbience must own the ONLY pad filter in this graph.
struct StageInstrumentConfig : StageCoreConfig {
    DelayConfig delay{};
    MasterConfig master{};
    MasterModulation modulation{};
    OutputConfig output{};
    StageSplits splits{};
    // Explicit opt-in preserves existing component fixtures. In owned mode,
    // motion owns BPM/mix/poly/master-LFO and unrounded Haas milliseconds.
    bool owned_motion{};
    MotionConfig motion{};
    FilterMotionConfig filter_motion{};
    double wet{.75},wet_gain{1},piano_reverb_send{},piano_delay_send{};
    bool wet_filter{},piano_filter{};
    PadAmbienceConfig ambience_config() const noexcept {
        auto d=delay; auto p=buses.pad;
        if(owned_motion) {
            d.bpm=motion.bpm; d.motion=motion.mix;
            p.haas_samples=static_cast<unsigned>(motion.haas_ms*.001*48000);
        }
        return {p,d,wet,wet_gain,wet_filter};
    }
    StagePatch source_patch(const PreparedSourceMix& mix) const noexcept {
        auto p=StageCoreConfig::source_patch(mix);
        if(owned_motion) for(unsigned j=0;j<2;++j) {
            const auto& l=motion.lfo[j];
            p.poly_lfo[j]={l.active&&l.poly&&l.depth*motion.mix>.001&&l.target==MotionTarget::Amp,
                // Existing faust_osc_bank.py clamps only the poly clock;
                // the global clock retains the full tempo-derived rate.
                std::clamp(l.effective_rate(motion.bpm),.05,20.),std::min(l.depth*motion.mix,.7),int(l.shape)};
        }
        return p;
    }
    bool valid() const noexcept {
        return StageCoreConfig::valid()&&motion.valid()&&filter_motion.valid()&&
            ambience_config().valid()&&master.valid()&&modulation.valid()&&output.valid()&&splits.valid()&&
            std::isfinite(piano_reverb_send)&&piano_reverb_send>=0&&piano_reverb_send<=1&&
            std::isfinite(piano_delay_send)&&piano_delay_send>=0&&piano_delay_send<=1;
    }
};
// Offline, single-owner, current-boundary commands only. Actual sources,
// piano room/soft clip/sends, pad/delay/reverb and master are owned together.
// No driver, browser protocol, sample bed or organ.
// pcm() is post master-fader/BTL float32; channel() retains PRE output-stage
// double diagnostics. Device gain/driver remain external. This is not Engine's
// sample-sliced Backend; render complete blocks, never once per event slice.
class StageInstrument final : private KeyTrigger {
public:
    StageInstrument(const std::string& font,PhaseSource& phases,unsigned frames=512,int program=0,MotionRandom* random=nullptr)
        :motion_(frames,random),filter_motion_(random),sources_(font,phases,frames,program,this),room_(frames),mix_(frames),ambience_(frames),master_(frames),output_(frames) {
        if(!configure(config_)) throw std::runtime_error("Instrument initialization failed");
    }
    bool configure(const StageInstrumentConfig& p) noexcept {
        if(!healthy()||!p.valid()) return false;
        if(p.owned_motion&&(!motion_.configure(p.motion)||!filter_motion_.configure(p.filter_motion))) { stop(); return false; }
        if(!sources_.configure(p.source_patch(prepared_))||!room_.configure(p.buses.room)||
           !mix_.configure(p.mix_config())||!ambience_.configure(p.ambience_config())||!master_.configure(p.master)||!output_.configure(p.output)) {
            stop(); return false;
        }
        config_=p; return true;
    }
    bool command(std::uint64_t frame,const StageCommand& command) noexcept {
        if(!healthy()) return false;
        const bool ok=sources_.command(frame,command);
        if(!sources_.healthy()) stop();
        return ok;
    }
    // Raw-key entry point. command() remains the explicit prepared-weight
    // diagnostic API; never apply both routing paths to the same event.
    bool key_command(std::uint64_t frame,StageAction action,int note,int value) noexcept {
        StageCommand event{action,note,value};
        if(action==StageAction::NoteOn&&value>0&&!config_.splits.weights(note,event.weights)) return false;
        return command(frame,event);
    }
    bool reverb_control(ReverbControl c,double v) noexcept { return healthy()&&ambience_.reverb_control(c,v); }
    bool reverb_type(ReverbType t) noexcept { return healthy()&&ambience_.reverb_type(t); }
    bool freeze(bool v) noexcept { return healthy()&&ambience_.freeze(v); }
    bool retrigger_bpm() noexcept { return healthy()&&master_.retrigger_bpm(); }
    bool render_block() noexcept {
        if(!healthy()) { stop(); return false; }
        prepared_=mix_.advance();
        if(!sources_.configure(config_.source_patch(prepared_))||!sources_.render_block(!prepared_.skip_voices)||
           !room_.process_block(sources_.stem(5),sources_.stem(6),piano_[0].data(),piano_[1].data(),block_frames())) {
            stop(); return false;
        }
        // Piano shared filter uses PREVIOUS pad cutoff, as the original piano
        // path renders before synth.render advances that block's smoother.
        const auto& p=config_.buses.pad;
        for(unsigned c=0;c<2;++c) {
            if(config_.piano_filter) {
                piano_filter_[c][0].tune(ambience_.current_cutoff(),p.resonance*(p.slope24?.5412/.707:1));
                if(p.slope24) piano_filter_[c][1].tune(ambience_.current_cutoff(),p.resonance*(1.3066/.707));
            }
            for(unsigned i=0;i<block_frames();++i) {
                double x=piano_[c][i];
                if(config_.piano_filter) {
                    x=piano_filter_[c][0].tick(x);
                    if(p.slope24) x=piano_filter_[c][1].tick(x);
                }
                if(std::abs(x)>.85) x=std::copysign(.85+(1-.85)*std::tanh((std::abs(x)-.85)/(1-.85)),x);
                piano_[c][i]=x;
                reverb_send_[c][i]=x*config_.piano_reverb_send;
                delay_send_[c][i]=x*config_.piano_delay_send;
            }
        }
        const std::array<const double*,2> piano{piano_[0].data(),piano_[1].data()};
        const std::array<const double*,2> rev{reverb_send_[0].data(),reverb_send_[1].data()};
        const std::array<const double*,2> delay{delay_send_[0].data(),delay_send_[1].data()};
        if(!ambience_.process({sources_.stem(0),sources_.stem(1),sources_.stem(2),sources_.stem(3),sources_.stem(4)},
            {prepared_.render_osc2,sources_.active_voices()>0,prepared_.haas_active,!prepared_.skip_voices},
            config_.piano_delay_send>.001?&delay:nullptr,config_.piano_reverb_send>.001?&rev:nullptr,
            config_.owned_motion?&motion_:nullptr,config_.owned_motion?&filter_motion_:nullptr)) { stop(); return false; }
        std::array<const double*,6> pad{};
        for(unsigned c=0;c<6;++c) pad[c]=ambience_.channel(c);
        const auto modulation=config_.owned_motion?MasterModulation{config_.motion.bpm,config_.motion.lfo[0].depth,motion_.state(0).last_a}:config_.modulation;
        if(!master_.process(pad,&piano,modulation)) { stop(); return false; }
        if(!output_.process({master_.channel(0),master_.channel(1)})) { stop(); return false; }
        return true;
    }
    void stop() noexcept {
        if(stopped_) return;
        stopped_=true; motion_.stop(); filter_motion_.stop(); sources_.stop(); ambience_.stop(); master_.stop(); output_.stop();
        for(auto& c:piano_) c.fill(0);
    }
    bool healthy() const noexcept { return !stopped_&&motion_.healthy()&&filter_motion_.healthy()&&sources_.healthy()&&room_.healthy()&&ambience_.healthy()&&master_.healthy()&&output_.healthy(); }
    MotionState motion_state(unsigned j) const noexcept { return motion_.state(j); }
    std::array<double,3> filter_motion_state() const noexcept { return filter_motion_.state(); }
    const double* channel(unsigned c) const noexcept { return master_.channel(c); }
    const float* pcm(unsigned c) const noexcept { return output_.channel(c); }
    const float* recording_tap(unsigned c) const noexcept { return output_.recording_tap(c); }
#ifdef STAVE_OFFLINE_TRACE
    const double* trace(unsigned c) const noexcept { return ambience_.trace(c); }
#endif
    // Diagnostics: master0/1, pad mixed/dry/FX2..7, prepared piano8/9.
    const double* stem(unsigned c) const noexcept {
        if(c<2) return channel(c);
        if(c<8) return ambience_.channel(c-2);
        return c<10?piano_[c-8].data():nullptr;
    }
    std::uint64_t frame_position() const noexcept { return sources_.frame_position(); }
    unsigned block_frames() const noexcept { return sources_.block_frames(); }
    unsigned active_voices() const noexcept { return sources_.active_voices(); }
    const StageSourceStats& stats() const noexcept { return sources_.stats(); }
    const short* raw_piano() const noexcept { return sources_.raw_piano(); }
    const PreparedSourceMix& prepared_mix() const noexcept { return prepared_; }
private:
    void oscillator_key_trigger() noexcept override { if(config_.owned_motion) motion_.key_trigger(); }
    StageMotion motion_; FilterMotion filter_motion_;
    StageSources sources_; PianoRoom room_; SourceMix mix_; PadAmbience ambience_; StageMaster master_; StageOutput output_;
    StageInstrumentConfig config_{}; PreparedSourceMix prepared_{}; bool stopped_{};
    std::array<std::array<double,512>,2> piano_{},reverb_send_{},delay_send_{};
    std::array<std::array<StageLowpass,2>,2> piano_filter_{};
};
} // namespace stave
