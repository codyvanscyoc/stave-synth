# Next step: real playing test

**Do not treat a running service or passing code tests as approval for a live
set.** This is the short hands-on sequence for the player and assistant; the
larger qualification gates remain in [ENGINEERING_VALIDATION.md](ENGINEERING_VALIDATION.md).

1. Off-stage, connect the normal Pi4 power supply/hub, Peavey USB interface and
   Yamaha keyboard. Keep PA/monitors muted during hookup and route selection.
   Tell the assistant when connected so USB/ALSA/JACK identities can be checked.
2. On the same trusted private Wi-Fi, open `http://stavepi4.local:8080` in Safari.
   Reload once after a software update. Check that controls connect. The assistant
   will verify both HTTP and WebSocket, not just whether a page is visible.
3. Confirm the exact Peavey stereo output and Yamaha MIDI input. The saved output
   previously named **Yamaha MX Series Analog Stereo**; a newly attached Peavey
   may require an intentional output selection. Device detection does not mean
   the app should silently route to an arbitrary speaker. The connected status
   indicator opens the output list.
4. At conservative listening level, play piano alone: quiet/loud notes, repeated
   notes, sustained chords, pedal release, low and high registers. Verify both
   channels and that STOP releases held sound. Avoid maxing gain to solve an
   unverified route.
5. Bring in OSC1 and OSC2 and the effects used tomorrow. Move filter and faders
   while playing your busiest normal part; hold/release sustain repeatedly.
   Include the actual drone or recorded bed if used. Listen for crackles, delayed
   attacks, unexpected silence and stuck notes while the assistant measures
   render timing, underruns, MIDI counters, temperature and memory.
6. Verify browser reload, Safari sleep/wake and two clients if used. Test one
   usual preset recall and recorder-to-pad only if those are part of the show.
   Confirm the audible result, not merely the button acknowledgement.
7. With outputs muted, separately test keyboard/interface unplug/replug and an
   agreed service restart. Observe automatic recovery; manually reconnecting a
   port does not prove automatic recovery. A cold power boot is a separate gate.
8. Rehearse the actual songs for a full service-length session. Keep a known-good
   fallback available. If a critical fault persists, use the preserved baseline
   or another qualified instrument; do not call the candidate show-ready.

The assistant must record what actually passed and what did not. Do not change
quantum, sample rate, envelope architecture or favorite-patch voicing merely to
make the test numbers look better.
