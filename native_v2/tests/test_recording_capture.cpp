#include "stave/recording_capture.hpp"
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <memory>
#include <new>
#include <thread>

namespace {
thread_local bool tracking=false;
thread_local unsigned allocations=0;
void check(bool ok,int line) { if(!ok) { std::fprintf(stderr,"Capture guard failed at %d\n",line); std::abort(); } }
#define require(x) check(bool(x),__LINE__)
void* allocate(std::size_t n) { if(tracking) ++allocations; if(auto* p=std::malloc(n?n:1)) return p; throw std::bad_alloc(); }
}
void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p,std::size_t) noexcept { std::free(p); }
void operator delete[](void* p,std::size_t) noexcept { std::free(p); }

int main() {
    using E=stave::CaptureEnd;
    for(unsigned frames:{256u,512u}) {
        using Q=stave::RecordingCapture<2>;
        std::array<float,512> left{},right{};
        Q::Block block;
        for(unsigned i=0;i<frames;++i) { left[i]=float(i)/512; right[i]=-left[i]; }
        Q overflow(frames);
        tracking=true;
        require(overflow.push(left.data(),right.data(),frames));
        require(overflow.push(right.data(),left.data(),frames));
        require(!overflow.push(left.data(),right.data(),frames)&&overflow.end()==E::Overflow);
        require(!overflow.drained()); overflow.finish(); require(overflow.end()==E::Overflow);
        require(overflow.pop(block));
        for(unsigned i=0;i<frames;++i) require(block.channel[0][i]==left[i]&&block.channel[1][i]==right[i]);
        require(overflow.pop(block));
        for(unsigned i=0;i<frames;++i) require(block.channel[0][i]==right[i]&&block.channel[1][i]==left[i]);
        require(overflow.drained()&&!overflow.pop(block));
        Q stopped(frames); stopped.request_stop();
        require(!stopped.push(left.data(),right.data(),frames)&&stopped.end()==E::Complete&&stopped.drained());
        stopped.writer_failed(); require(stopped.writer_error()); // late disk failure remains visible
        Q failed(frames); failed.writer_failed(); failed.request_stop();
        require(!failed.push(left.data(),right.data(),frames)&&failed.end()==E::WriterError);
        Q limit(frames,frames*2);
        for(unsigned i=0;i<2;++i) { require(limit.push(left.data(),right.data(),frames)); require(limit.pop(block)); }
        require(!limit.push(left.data(),right.data(),frames)&&limit.end()==E::FrameLimit&&limit.drained());
        for(unsigned bad=0;bad<4;++bad) {
            Q invalid(frames);
            auto l=left; if(bad==0) l[frames-1]=std::numeric_limits<float>::quiet_NaN();
            if(bad==1) l[0]=std::numeric_limits<float>::infinity();
            require(!invalid.push(bad==2?nullptr:l.data(),right.data(),bad==3?frames/2:frames));
            require(invalid.end()==E::InvalidAudio&&invalid.drained()&&!invalid.pop(block));
        }
        tracking=false; require(allocations==0);
        // Concurrent wrap/order/payload test. External test handshake bounds
        // occupancy without making the production producer wait or retry.
        auto concurrent=std::make_unique<Q>(frames);
        std::atomic<unsigned> consumed{0};
        constexpr unsigned count=4000;
        std::thread consumer([&] {
            Q::Block b;
            tracking=true;
            for(unsigned n=0;n<count;++n) {
                while(!concurrent->pop(b)) std::this_thread::yield();
                for(unsigned i=0;i<frames;++i) require(b.channel[0][i]==float(n)&&b.channel[1][i]==-float(n));
                consumed.store(n+1,std::memory_order_release);
            }
            tracking=false; require(allocations==0);
        });
        tracking=true;
        for(unsigned n=0;n<count;++n) {
            while(consumed.load(std::memory_order_acquire)!=n) std::this_thread::yield();
            left.fill(float(n)); right.fill(-float(n));
            require(concurrent->push(left.data(),right.data(),frames));
        }
        concurrent->finish(); tracking=false; consumer.join();
        require(concurrent->end()==E::Complete&&concurrent->drained()&&allocations==0);
    }
    bool refused=false;
    try { stave::RecordingCapture<> invalid(128); } catch(const std::invalid_argument&) { refused=true; }
    require(refused);
    std::puts("PASS: recorder SPSC exact payload/order,8000 concurrent blocks, bounded overflow/limit, invalid PCM, stop/error/drain, zero scoped new/new[]; no disk/live qualification");
}
