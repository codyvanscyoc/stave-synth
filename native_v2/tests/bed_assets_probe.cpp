#include "stave/bed_assets.hpp"
#include <iostream>
int main(int argc,char** argv) {
    try {
        if(argc!=2&&argc!=3) return 2;
        auto bank=stave::load_prepared_bed_bank(argv[1]);
        if(argc==2) {
            unsigned mask=0; for(unsigned i=0;i<12;++i) if(bank->loaded(i)) mask|=1u<<i;
            std::cout<<mask<<" "<<bank->bytes()<<"\n"; return 0;
        }
        if(!bank->trigger(unsigned(std::stoi(argv[2])))) return 3;
        for(unsigned block=0;block<8;++block) {
            std::array<double,512> l{},r{};
            if(!bank->process(l.data(),r.data(),512)) return 4;
            std::array<double,1024> interleaved{};
            for(unsigned i=0;i<512;++i) { interleaved[i*2]=l[i]; interleaved[i*2+1]=r[i]; }
            if(std::fwrite(interleaved.data(),sizeof(double),interleaved.size(),stdout)!=interleaved.size()) return 5;
        }
        return 0;
    } catch(const std::exception& error) { std::cerr<<error.what()<<"\n"; return 1; }
}
