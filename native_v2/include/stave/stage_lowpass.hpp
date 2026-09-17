#pragma once
#include <algorithm>
#include <cmath>

namespace stave {
// Original double DF-II lowpass; coefficient edits retain the delay state.
struct StageLowpass {
    double b0{},b1{},b2{},a1{},a2{},s1{},s2{};
    StageLowpass() noexcept { tune(8000,.707); }
    void tune(double hz,double q) noexcept {
        const double w=2*3.14159265358979323846*std::clamp(hz,20.,21600.)/48000;
        const double c=std::cos(w),a=std::sin(w)/(2*std::clamp(q,.1,10.)),d=1+a;
        b0=((1-c)/2)/d; b1=(1-c)/d; b2=b0; a1=(-2*c)/d; a2=(1-a)/d;
    }
    double tick(double x) noexcept {
        const double y=b0*x+s1; s1=b1*x-a1*y+s2; s2=b2*x-a2*y; return y;
    }
};
} // namespace stave
