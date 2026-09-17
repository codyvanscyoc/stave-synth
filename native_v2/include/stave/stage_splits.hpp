#pragma once
#include "stave/stage_notes.hpp"

namespace stave {
struct KeyRange {
    int low{}, high{127}, crossfade{};
    bool valid() const noexcept {
        // Legacy state validates each endpoint independently. Preserve even
        // reversed ranges; do not silently swap a saved patch's endpoints.
        return low>=0&&low<=127&&high>=0&&high<=127&&crossfade>=0&&crossfade<=24;
    }
private:
    friend struct StageSplits;
    double weight(int note) const noexcept {
        if(crossfade<=0) return low<=note&&note<=high?1.:0.;
        if(note<low-crossfade||note>high+crossfade) return 0;
        double t;
        if(note<low) t=double(note-(low-crossfade))/crossfade;
        else if(note>high) t=double((high+crossfade)-note)/crossfade;
        else return 1;
        t=std::clamp(t,0.,1.);
        return t*t*(3.-2.*t);
    }
};
// Raw physical keys, BEFORE transpose/octave. Applied only at note-on; note
// releases retain StageNotes' original destination/pedal ownership. No voices
// are reweighted by editing these ranges while keys are held.
struct StageSplits {
    bool enabled{};
    KeyRange osc1{},osc2{},shimmer{},piano{};
    bool valid() const noexcept { return osc1.valid()&&osc2.valid()&&shimmer.valid()&&piano.valid(); }
    bool weights(int raw,LayerWeights& out) const noexcept {
        if(raw<0||raw>127||!valid()) return false;
        out=enabled?LayerWeights{osc1.weight(raw),osc2.weight(raw),shimmer.weight(raw),piano.weight(raw)}:LayerWeights{};
        return true;
    }
};
} // namespace stave
