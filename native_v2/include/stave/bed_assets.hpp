#pragma once
#include "stave/sampled_bed.hpp"
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <sys/stat.h>
#include <unistd.h>

namespace stave {
// Explicit startup/off-audio loader. Private bounded interchange, NOT a WAV
// parser or live replacement. The caller keeps sole ownership until the graph
// seals the completed bank. Refusal never modifies an existing bank/library.
inline std::unique_ptr<SampledBed> load_prepared_bed_bank(const char* path) {
    const int fd=::open(path,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    if(fd<0) throw std::runtime_error("Cannot open prepared pad bank");
    std::FILE* raw=::fdopen(fd,"rb");
    if(!raw) { ::close(fd); throw std::runtime_error("Cannot read prepared bank"); }
    std::unique_ptr<std::FILE,int(*)(std::FILE*)> stream(raw,std::fclose);
    struct stat before{},after{};
    if(::fstat(fd,&before)||!S_ISREG(before.st_mode)||before.st_size<16||
       static_cast<std::uint64_t>(before.st_size)>SampledBed::max_bytes+16+12*16)
        throw std::runtime_error("Invalid prepared bank file/size");
    auto read=[&](unsigned char* p,std::size_t n) {
        if(std::fread(p,1,n,stream.get())!=n) throw std::runtime_error("Truncated prepared bank");
    };
    auto integer=[](const unsigned char* p,unsigned n) {
        std::uint64_t v=0; for(unsigned i=0;i<n;++i) v|=std::uint64_t(p[i])<<(8*i); return v;
    };
    std::array<unsigned char,16> header{}; read(header.data(),16);
    const auto count=integer(header.data()+12,4);
    if(std::memcmp(header.data(),"STVBANK1",8)||integer(header.data()+8,4)!=48000||count>12)
        throw std::runtime_error("Unsupported prepared bank header");
    auto bank=std::make_unique<SampledBed>();
    unsigned seen=0;
    std::uint64_t consumed=16;
    static_assert(sizeof(double)==8&&std::numeric_limits<double>::is_iec559);
    for(unsigned entry=0;entry<count;++entry) {
        read(header.data(),16); consumed+=16;
        const auto slot=integer(header.data(),4),reserved=integer(header.data()+4,4),frames=integer(header.data()+8,8);
        if(slot>=12||reserved||frames<4||frames>PreparedBed::max_bytes/16||
           (seen&(1u<<slot))||frames*16>SampledBed::max_bytes-bank->bytes()||
           consumed+frames*16>static_cast<std::uint64_t>(before.st_size))
            throw std::runtime_error("Invalid prepared slot or memory budget");
        std::vector<double> left(frames),right(frames);
        std::array<unsigned char,8192> bytes{};
        for(std::uint64_t offset=0;offset<frames;) {
            const auto n=std::min<std::uint64_t>(512,frames-offset); read(bytes.data(),n*16);
            for(unsigned i=0;i<n;++i) for(unsigned c=0;c<2;++c) {
                const auto bits=integer(bytes.data()+i*16+c*8,8); double value;
                std::memcpy(&value,&bits,8);
                if(!std::isfinite(value)) throw std::runtime_error("Nonfinite prepared PCM");
                (c?right:left)[offset+i]=value;
            }
            offset+=n;
        }
        std::unique_ptr<const PreparedBed> asset=std::make_unique<PreparedBed>(left.data(),right.data(),frames);
        if(!bank->install(slot,asset)) throw std::runtime_error("Prepared slot install refused");
        seen|=1u<<slot; consumed+=frames*16;
    }
    if(consumed!=static_cast<std::uint64_t>(before.st_size)||::fstat(fd,&after)||
       before.st_size!=after.st_size||before.st_mtime!=after.st_mtime||before.st_ctime!=after.st_ctime)
        throw std::runtime_error("Prepared bank changed or contains trailing data");
#if defined(__APPLE__)
    if(before.st_mtimespec.tv_nsec!=after.st_mtimespec.tv_nsec||before.st_ctimespec.tv_nsec!=after.st_ctimespec.tv_nsec)
#else
    if(before.st_mtim.tv_nsec!=after.st_mtim.tv_nsec||before.st_ctim.tv_nsec!=after.st_ctim.tv_nsec)
#endif
        throw std::runtime_error("Prepared bank changed during read");
    bank->seal(); return bank;
}
} // namespace stave
