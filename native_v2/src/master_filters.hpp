#pragma once
#include <algorithm>
#include <cmath>

namespace stave::detail {
constexpr double master_pi=3.14159265358979323846;
inline double master_compression_target(double over,double knee,double ratio) noexcept {
    const double slope=1-1/std::max(1.,ratio);
    if(over>knee*.5) return over*slope;
    if(over>=-knee*.5&&knee>0) {
        const double x=(over+knee*.5)/knee;
        return x*x*knee*.5*slope;
    }
    // The original's exact-threshold, zero-knee case divides 0/0.
    // Use the continuous hard-knee limit, zero gain reduction.
    return 0;
}
// Original transposed-direct-form-II kernel and coefficient conventions.
struct MasterBiquad {
    double b0{1},b1{},b2{},a1{},a2{},s1{},s2{};
    double tick(double x) noexcept {
        const double y=b0*x+s1;
        s1=b1*x-a1*y+s2; s2=b2*x-a2*y; return y;
    }
    void highpass(double hz,double q=.707) noexcept {
        const double w=2*master_pi*std::clamp(hz,20.,21600.)/48000;
        const double c=std::cos(w),a=std::sin(w)/(2*std::clamp(q,.1,10.)),a0=1+a;
        b0=((1+c)/2)/a0; b1=(-(1+c))/a0; b2=b0; a1=(-2*c)/a0; a2=(1-a)/a0;
    }
    void peak(double hz,double gain,double q) noexcept {
        const double A=std::pow(10.,gain/40),w=2*master_pi*std::clamp(hz,20.,21600.)/48000;
        const double c=std::cos(w),a=std::sin(w)/(2*std::clamp(q,.1,20.)),a0=1+a/A;
        b0=(1+a*A)/a0; b1=(-2*c)/a0; b2=(1-a*A)/a0; a1=(-2*c)/a0; a2=(1-a/A)/a0;
    }
    void shelf(double gain) noexcept {
        const double A=std::pow(10.,gain/40),w=2*master_pi*250/48000;
        const double c=std::cos(w),a=std::sin(w)/(2*.707),v=2*std::sqrt(A)*a;
        const double a0=(A+1)+(A-1)*c+v;
        b0=(A*((A+1)-(A-1)*c+v))/a0;
        b1=(2*A*((A-1)-(A+1)*c))/a0;
        b2=(A*((A+1)-(A-1)*c-v))/a0;
        a1=(-2*((A-1)+(A+1)*c))/a0; a2=((A+1)+(A-1)*c-v)/a0;
    }
};
struct MasterOnePole {
    double a{},state{};
    void highpass(double hz) noexcept { const double w=2*master_pi*std::clamp(hz,20.,21600.)/48000; a=1/(1+w); }
    double tick(double x) noexcept { const double y=a*x+state; state=-a*x+a*y; return y; }
};
} // namespace stave::detail
