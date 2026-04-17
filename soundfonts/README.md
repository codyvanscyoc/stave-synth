# Bundled soundfonts

## TimGM6mb.sf2

Small General MIDI soundfont (~6 MB) by Tim Brechbill. Free to distribute,
widely shipped with MuseScore and as the Debian package `timgm6mb-soundfont`.
Good quality piano and reasonable GM coverage; kept small for quick install.

Used as the default piano source if no larger soundfont is present on the
system. `install.sh` copies this file to
`~/.local/share/stave-synth/soundfonts/` so the app can find it.

To swap in a larger soundfont (e.g. FluidR3_GM, ~150 MB) after install,
drop a `.sf2` file into that directory and restart the synth.
