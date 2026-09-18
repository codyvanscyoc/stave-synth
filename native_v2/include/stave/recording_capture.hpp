#pragma once
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <stdexcept>

namespace stave {
enum class CaptureEnd : unsigned { Idle, Open, Complete, Overflow, InvalidAudio, FrameLimit, WriterError, EngineStopped };
// One take, two owners: audio calls push()/finish(); a disk worker calls pop()
// and may request_stop()/writer_failed(). Construct and destroy OFF audio, with
// both owners quiescent. Never reset/reuse a take or free it in the callback.
// This queue is a recorder transport, not an output/render-ahead audio queue.
// Full/failed recording does NOT stop the instrument. No locks, waits, I/O,
// notifications, allocations or dynamic retirement on either queue path.
template<unsigned Capacity=128>
class RecordingCapture final {
public:
    static_assert(Capacity>0&&Capacity<=512);
    static constexpr std::uint64_t maximum_frames=48000ULL*60*30;
    struct Block { std::array<std::array<float,512>,2> channel{}; };
    explicit RecordingCapture(unsigned frames=512,std::uint64_t limit=maximum_frames,bool armed=true)
        :frames_(frames),limit_(limit),end_(armed?CaptureEnd::Open:CaptureEnd::Idle) {
        if((frames!=256&&frames!=512)||limit<frames||limit>maximum_frames)
            throw std::invalid_argument("Recording requires256/512 frames and bounded take length");
    }
    RecordingCapture(const RecordingCapture&)=delete;
    RecordingCapture& operator=(const RecordingCapture&)=delete;
    // One-shot arming transition. The object may be constructed before JACK
    // activation, then armed by an audio-boundary command. A completed/failed
    // take is never reset or reused while either owner could still reference it.
    bool start() noexcept {
        if(stop_.load(std::memory_order_acquire)||writer_failed_.load(std::memory_order_acquire)) return false;
        auto expected=CaptureEnd::Idle;
        return end_.compare_exchange_strong(expected,CaptureEnd::Open,std::memory_order_release,std::memory_order_relaxed);
    }
    bool active() const noexcept { return end()==CaptureEnd::Open; }
    bool push(const float* left,const float* right,unsigned frames) noexcept {
        if(end()!=CaptureEnd::Open) return false;
        // Worker failures take precedence over a requested clean stop.
        if(writer_failed_.load(std::memory_order_acquire)) return finish(CaptureEnd::WriterError);
        if(stop_.load(std::memory_order_acquire)) return finish(CaptureEnd::Complete);
        if(!left||!right||frames!=frames_) return finish(CaptureEnd::InvalidAudio);
        if(written_.load(std::memory_order_relaxed)>limit_-frames_) return finish(CaptureEnd::FrameLimit);
        const auto head=head_.load(std::memory_order_relaxed),tail=tail_.load(std::memory_order_acquire);
        if(head-tail==Capacity) return finish(CaptureEnd::Overflow);
        // Validate before publication: never export a partial/poisonous block.
        for(unsigned i=0;i<frames_;++i)
            if(!std::isfinite(left[i])||!std::isfinite(right[i])) return finish(CaptureEnd::InvalidAudio);
        auto& block=blocks_[head%Capacity];
        for(unsigned i=0;i<frames_;++i) { block.channel[0][i]=left[i]; block.channel[1][i]=right[i]; }
        written_.fetch_add(frames_,std::memory_order_relaxed);
        head_.store(head+1,std::memory_order_release);
        return true;
    }
    // Audio-owner terminal transition, also usable after that owner is joined.
    // A non-Open failure cannot later become Complete. Already queued blocks
    // remain drainable; finalization must inspect end AND drained(), not empty.
    bool finish(CaptureEnd reason=CaptureEnd::Complete) noexcept {
        if(reason!=CaptureEnd::Idle&&reason!=CaptureEnd::Open&&end()==CaptureEnd::Open)
            end_.store(writer_failed_.load(std::memory_order_acquire)?CaptureEnd::WriterError:reason,std::memory_order_release);
        return false;
    }
    bool pop(Block& destination) noexcept {
        const auto tail=tail_.load(std::memory_order_relaxed),head=head_.load(std::memory_order_acquire);
        if(tail==head) return false;
        const auto& block=blocks_[tail%Capacity];
        for(unsigned c=0;c<2;++c) for(unsigned i=0;i<frames_;++i) destination.channel[c][i]=block.channel[c][i];
        tail_.store(tail+1,std::memory_order_release);
        return true;
    }
    void request_stop() noexcept { stop_.store(true,std::memory_order_release); }
    void writer_failed() noexcept { writer_failed_.store(true,std::memory_order_release); }
    // Writer may fail while draining AFTER the producer ended. Complete means
    // capture ended cleanly, never that a durable WAV was successfully saved.
    bool writer_error() const noexcept { return writer_failed_.load(std::memory_order_acquire); }
    CaptureEnd end() const noexcept { return end_.load(std::memory_order_acquire); }
    std::uint64_t frames_written() const noexcept { return written_.load(std::memory_order_relaxed); }
    // Consumer-only query after observing end: producer can no longer publish.
    bool drained() const noexcept {
        return end()!=CaptureEnd::Open&&tail_.load(std::memory_order_relaxed)==head_.load(std::memory_order_acquire);
    }
    unsigned block_frames() const noexcept { return frames_; }
private:
    static_assert(std::atomic<std::uint64_t>::is_always_lock_free);
    static_assert(std::atomic<CaptureEnd>::is_always_lock_free);
    static_assert(std::atomic<bool>::is_always_lock_free);
    unsigned frames_;
    std::uint64_t limit_;
    std::atomic<std::uint64_t> written_{0}; // one audio writer, telemetry reader
    std::array<Block,Capacity> blocks_{};
    alignas(64) std::atomic<std::uint64_t> head_{0},tail_{0};
    std::atomic<CaptureEnd> end_;
    std::atomic<bool> stop_{false},writer_failed_{false};
};
} // namespace stave
