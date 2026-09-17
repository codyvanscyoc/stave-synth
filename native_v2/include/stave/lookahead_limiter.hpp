#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>

namespace stave {
// Reference is for offline compatibility evidence only, not a supported stage
// profile. CeilingSafe fixes the original envelope's release overshoot.
enum class LimiterPolicy { CeilingSafe, Reference };
class LookaheadLimiter final {
public:
    static constexpr unsigned lookahead=72;
    static constexpr double ceiling=.98;
    explicit LookaheadLimiter(unsigned frames=512,LimiterPolicy policy=LimiterPolicy::CeilingSafe)
        :frames_(frames),policy_(policy),release_(std::exp(-1.0/(48000*60.0*.001))) {
        if((frames!=256&&frames!=512)||(policy!=LimiterPolicy::CeilingSafe&&policy!=LimiterPolicy::Reference))
            throw std::invalid_argument("Limiter requires fixed256/512 at48k and a valid policy");
    }
    bool process(const std::array<const double*,2>& input) noexcept {
        for(const auto* p:input) {
            if(!p) return false;
            const auto a=reinterpret_cast<std::uintptr_t>(p);
            for(const auto& c:output_) {
                const auto b=reinterpret_cast<std::uintptr_t>(c.data());
                if((a<b?b-a:a-b)<frames_*sizeof(double)) return false;
            }
        }
        if(stopped_||fault_) { silence(); return false; }
        for(unsigned c=0;c<3;++c) std::copy(tail_[c].begin(),tail_[c].end(),full_[c].begin());
        for(unsigned i=0;i<frames_;++i) {
            for(unsigned c=0;c<2;++c) {
                if(!std::isfinite(input[c][i])) { fault_=true; silence(); return false; }
                full_[c][lookahead+i]=input[c][i];
            }
            full_[2][lookahead+i]=std::max(std::abs(input[0][i]),std::abs(input[1][i]));
        }
        // Monotone index queue. Each input is appended once and removed at
        // most once. Exact maxima over the original inclusive 73-sample
        // windows, without rescanning each overlapping window.
        unsigned head=0,tail=0;
        for(unsigned j=0;j<frames_+lookahead;++j) {
            while(head<tail&&queue_[head]+lookahead<j) ++head;
            while(head<tail&&full_[2][queue_[tail-1]]<=full_[2][j]) --tail;
            queue_[tail++]=j;
            if(j<lookahead) continue;
            const unsigned i=j-lookahead;
            const double target=std::min(ceiling/std::max(full_[2][queue_[head]],1e-9),1.0);
            if(target<gain_) gain_=target;
            else {
                gain_=release_*gain_+(1-release_);
                if(policy_==LimiterPolicy::CeilingSafe) gain_=std::min(target,gain_);
            }
            for(unsigned c=0;c<2;++c) {
                const double value=full_[c][i]*gain_;
                output_[c][i]=policy_==LimiterPolicy::CeilingSafe?std::clamp(value,-ceiling,ceiling):value;
            }
        }
        for(unsigned c=0;c<3;++c) std::copy_n(full_[c].data()+frames_,lookahead,tail_[c].data());
        return true;
    }
    void reset() noexcept { for(auto& c:tail_) c.fill(0); gain_=1; silence(); }
    void stop() noexcept { stopped_=true; silence(); }
    bool healthy() const noexcept { return !stopped_&&!fault_; }
    const double* channel(unsigned c) const noexcept { return c<2?output_[c].data():nullptr; }
    double gain() const noexcept { return gain_; }
private:
    void silence() noexcept { for(auto& c:output_) c.fill(0); }
    unsigned frames_; LimiterPolicy policy_; double release_,gain_{1}; bool fault_{},stopped_{};
    std::array<std::array<double,lookahead>,3> tail_{};
    std::array<std::array<double,512+lookahead>,3> full_{};
    std::array<unsigned,512+lookahead> queue_{};
    std::array<std::array<double,512>,2> output_{};
};
} // namespace stave
