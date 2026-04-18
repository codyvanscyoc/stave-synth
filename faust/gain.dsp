declare name "stave_gain";
declare description "FFI spike — stereo gain for null-test vs numpy path";

import("stdfaust.lib");

gain = hslider("gain", 1.0, 0.0, 2.0, 0.001);

process = *(gain), *(gain);
