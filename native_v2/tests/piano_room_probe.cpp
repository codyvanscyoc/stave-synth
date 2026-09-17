#include "stave/piano_room.hpp"
extern "C" {
void* room_create(unsigned frames) noexcept {
    try { return new stave::PianoRoom(frames); } catch (...) { return nullptr; }
}
void room_delete(void* p) { delete static_cast<stave::PianoRoom*>(p); }
int room_configure(void* p, int enabled, double wet, double size, double damp) {
    if (!p || (enabled != 0 && enabled != 1)) return 0;
    return static_cast<stave::PianoRoom*>(p)->configure({enabled != 0, wet, size, damp});
}
int room_process(void* p, const double* l, const double* r, double* dl, double* dr, unsigned n) {
    return p && static_cast<stave::PianoRoom*>(p)->process_block(l, r, dl, dr, n);
}
void room_clear(void* p) { if (p) static_cast<stave::PianoRoom*>(p)->clear(); }
double room_wet(void* p) { return static_cast<stave::PianoRoom*>(p)->current_wet(); }
}
