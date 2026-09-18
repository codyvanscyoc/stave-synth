#include "stave/recording_writer.hpp"
#include <array>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

namespace {
void check(bool ok,int line) { if(!ok) { std::fprintf(stderr,"Writer guard failed at %d\n",line); std::abort(); } }
#define require(x) check(bool(x),__LINE__)
}
int main(int argc,char** argv) {
    require(argc==2);
    const std::filesystem::path root=argv[1];
    std::array<float,512> left{},right{};
    for(unsigned frames:{256u,512u}) {
        const auto name=std::string("take-")+std::to_string(frames)+".wav";
        stave::RecordingCapture<> capture(frames,frames*4,false);
        stave::RecordingWriter writer(capture,root.c_str(),name.c_str(),frames);writer.start();
        require(capture.start());
        for(unsigned block=0;block<3;++block) {
            for(unsigned i=0;i<frames;++i) { left[i]=float(block+i)/2048;right[i]=-left[i]; }
            require(capture.push(left.data(),right.data(),frames));
        }
        capture.request_stop();require(!capture.push(left.data(),right.data(),frames));
        for(unsigned n=0;n<1000&&writer.state()!=stave::WriterState::Complete;++n)
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        require(writer.state()==stave::WriterState::Complete&&writer.frames_written()==frames*3);
        writer.shutdown();
        const auto path=root/name;require(std::filesystem::file_size(path)==58+frames*3*8);
        std::ifstream input(path,std::ios::binary);std::array<char,58> header{};input.read(header.data(),header.size());
        require(std::string(header.data(),4)=="RIFF"&&std::string(header.data()+8,8)=="WAVEfmt "&&std::string(header.data()+50,4)=="data");
        // Existing destination must never be replaced.
        stave::RecordingCapture<> second(frames,frames*2,false);
        stave::RecordingWriter refused(second,root.c_str(),name.c_str(),frames);refused.start();require(second.start());
        require(second.push(left.data(),right.data(),frames));second.request_stop();require(!second.push(left.data(),right.data(),frames));
        for(unsigned n=0;n<1000&&refused.state()!=stave::WriterState::Failed;++n)
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        require(refused.state()==stave::WriterState::Failed);refused.shutdown();
        require(std::filesystem::file_size(path)==58+frames*3*8);
    }
    std::puts("PASS: off-audio writer durable float WAV, exact frames, no-replace publication and failure isolation");
}
