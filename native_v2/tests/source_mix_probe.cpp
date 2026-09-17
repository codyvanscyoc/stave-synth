#include "stave/source_mix.hpp"
#include <cstdio>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <string>
namespace { void check(bool pass) { if(!pass) std::abort(); } }
int main() {
    std::unique_ptr<stave::SourceMix> mix;
    std::string operation;
    std::cout << std::setprecision(17);
    while(std::cin >> operation) {
        if(operation=="reset") {
            unsigned frames; std::cin >> frames; mix=std::make_unique<stave::SourceMix>(frames);
        } else if(operation=="config") {
            stave::SourceMixConfig config; int hard,shimmer,high;
            std::cin >> config.fader1 >> config.fader2 >> config.pan1 >> config.pan2 >> hard >> shimmer >> high >> config.shimmer_mix;
            check(mix && (hard==0||hard==1) && (shimmer==0||shimmer==1) && (high==0||high==1));
            config.hard_pan=hard; config.shimmer=shimmer; config.shimmer_high=high;
            check(mix->configure(config));
        } else if(operation=="advance") {
            check(bool(mix)); const auto prepared=mix->advance(); const auto state=mix->state();
            std::cout << state[0] << ' ' << state[1] << ' ' << prepared.amplitude1 << ' ' << prepared.amplitude2
                      << ' ' << prepared.pan1 << ' ' << prepared.pan2 << ' ' << prepared.render_osc1 << ' ' << prepared.render_osc2
                      << ' ' << prepared.render_shimmer << ' ' << prepared.skip_voices << ' ' << prepared.haas_active
                      << ' ' << prepared.shimmer_high << '\n';
        } else if(operation=="guards") {
            check(bool(mix)); const auto before=mix->state();
            for(double bad : {-1.,1.01,std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()}) {
                stave::SourceMixConfig c; c.fader1=bad; check(!mix->configure(c)); c={};
                c.fader2=bad; check(!mix->configure(c)); c={}; c.shimmer_mix=bad; check(!mix->configure(c));
            }
            for(double bad : {-1.01,1.01,std::numeric_limits<double>::quiet_NaN()}) {
                stave::SourceMixConfig c; c.pan1=bad; check(!mix->configure(c)); c={}; c.pan2=bad; check(!mix->configure(c));
            }
            check(mix->state()==before);
            for(unsigned bad : {0u,1u,255u,513u}) {
                bool rejected=false; try { stave::SourceMix invalid(bad); } catch(...) { rejected=true; }
                check(rejected);
            }
        } else { std::fputs("Unknown fixture command\n",stderr); return 1; }
        if(!std::cin) return 2;
    }
}
