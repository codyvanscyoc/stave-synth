#pragma once
#include "stave/recording_capture.hpp"
#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>

namespace stave {
enum class WriterState : unsigned { Idle, Writing, Complete, Failed };

// One-take off-audio worker. The audio owner only publishes fixed blocks into
// RecordingCapture. This worker exclusively creates a private partial WAV,
// drains the queue, fsyncs a finalized header/payload, then publishes with a
// no-replace hard link. Existing takes/symlinks are never overwritten.
class RecordingWriter final {
public:
    RecordingWriter(RecordingCapture<>& capture,const char* directory,const char* filename,unsigned frames)
        :capture_(capture),frames_(frames),name_(filename?filename:"") {
        if(!directory||!valid_name(name_)||(frames!=256&&frames!=512))
            throw std::invalid_argument("Invalid recording writer destination/cadence");
        partial_=name_+".partial";
        directory_=::open(directory,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if(directory_<0) throw std::runtime_error("Cannot open recording directory");
    }
    ~RecordingWriter() { shutdown(); if(directory_>=0) ::close(directory_); }
    RecordingWriter(const RecordingWriter&)=delete;
    RecordingWriter& operator=(const RecordingWriter&)=delete;
    void start() {
        if(worker_.joinable()) throw std::logic_error("Recording writer already started");
        worker_=std::thread([this]{ run(); });
    }
    void shutdown() noexcept {
        stop_.store(true,std::memory_order_release);
        if(worker_.joinable()) worker_.join();
    }
    WriterState state() const noexcept { return state_.load(std::memory_order_acquire); }
    std::uint64_t frames_written() const noexcept { return durable_frames_.load(std::memory_order_relaxed); }
private:
    static bool valid_name(const std::string& name) noexcept {
        if(name.size()<5||name.size()>96||name.substr(name.size()-4)!=".wav") return false;
        for(char c:name) if(!((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='-'||c=='_'||c=='.')) return false;
        return name!=".wav"&&name.find("..") == std::string::npos;
    }
    static void u16(std::FILE* out,std::uint16_t value) noexcept {
        const unsigned char b[]{static_cast<unsigned char>(value),static_cast<unsigned char>(value>>8)};
        std::fwrite(b,1,2,out);
    }
    static void u32(std::FILE* out,std::uint32_t value) noexcept {
        const unsigned char b[]{static_cast<unsigned char>(value),static_cast<unsigned char>(value>>8),
            static_cast<unsigned char>(value>>16),static_cast<unsigned char>(value>>24)};
        std::fwrite(b,1,4,out);
    }
    bool header(std::FILE* out,std::uint64_t frames) noexcept {
        if(frames>0xffffffffu/8u||std::fseek(out,0,SEEK_SET)) return false;
        const auto bytes=static_cast<std::uint32_t>(frames*8);
        std::fwrite("RIFF",1,4,out);u32(out,50u+bytes);std::fwrite("WAVE",1,4,out);
        std::fwrite("fmt ",1,4,out);u32(out,18);u16(out,3);u16(out,2);u32(out,48000);
        u32(out,48000*8);u16(out,8);u16(out,32);u16(out,0);
        std::fwrite("fact",1,4,out);u32(out,4);u32(out,static_cast<std::uint32_t>(frames));
        std::fwrite("data",1,4,out);u32(out,bytes);
        return std::ferror(out)==0;
    }
    bool block(std::FILE* out,const RecordingCapture<>::Block& source) noexcept {
        std::array<float,1024> interleaved{};
        for(unsigned i=0;i<frames_;++i) { interleaved[2*i]=source.channel[0][i]; interleaved[2*i+1]=source.channel[1][i]; }
        return std::fwrite(interleaved.data(),sizeof(float)*2,frames_,out)==frames_;
    }
    void fail(std::FILE* out=nullptr) noexcept {
        capture_.writer_failed();
        if(out) std::fclose(out);
        if(directory_>=0&&!partial_.empty()) ::unlinkat(directory_,partial_.c_str(),0);
        state_.store(WriterState::Failed,std::memory_order_release);
    }
    void run() noexcept {
        while(!stop_.load(std::memory_order_acquire)&&capture_.end()==CaptureEnd::Idle)
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        if(stop_.load(std::memory_order_acquire)) return;
        const int fd=::openat(directory_,partial_.c_str(),O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
        if(fd<0) { fail(); return; }
        std::FILE* out=::fdopen(fd,"w+b");
        if(!out) { ::close(fd); fail(); return; }
        if(!header(out,0)) { fail(out); return; }
        state_.store(WriterState::Writing,std::memory_order_release);
        RecordingCapture<>::Block item;
        std::uint64_t frames=0;
        for(;;) {
            bool drained_any=false;
            while(capture_.pop(item)) {
                drained_any=true;
                if(!block(out,item)) { fail(out); return; }
                frames+=frames_; durable_frames_.store(frames,std::memory_order_relaxed);
            }
            const auto end=capture_.end();
            if(end!=CaptureEnd::Idle&&end!=CaptureEnd::Open&&capture_.drained()) {
                if(end!=CaptureEnd::Complete||!frames||capture_.writer_error()) { fail(out); return; }
                if(!header(out,frames)||std::fflush(out)||::fsync(fd)) { fail(out); return; }
                const int close_result=std::fclose(out);
                out=nullptr;
                if(close_result) { fail(); return; }
                if(::linkat(directory_,partial_.c_str(),directory_,name_.c_str(),0)||
                   ::unlinkat(directory_,partial_.c_str(),0)||::fsync(directory_)) { fail(); return; }
                state_.store(WriterState::Complete,std::memory_order_release); return;
            }
            if(stop_.load(std::memory_order_acquire)) { fail(out); return; }
            if(!drained_any) std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
    }
    RecordingCapture<>& capture_;
    unsigned frames_;
    int directory_{-1};
    std::string name_,partial_;
    std::thread worker_;
    std::atomic<bool> stop_{false};
    std::atomic<WriterState> state_{WriterState::Idle};
    std::atomic<std::uint64_t> durable_frames_{0};
};
} // namespace stave
