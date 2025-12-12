Mode | Description | Relies on LED sliders | Relies on fade slider
--- | --- | --- | ---
Light off | Turns the lamp off | No | No
Light on | Sends the slider-selected color as a static value | Yes | No
Light fade sync | On every beat, pops to the slider color then fades using the decay slider | Yes | Yes
Auto RGB fade (no beat) | Continuously cycles R → G → B with fixed in/out speeds and interval | No | No
Beat RGB step | Each beat steps R → G → B (ignores sliders) and fades out using the decay slider | No | Yes
Slider slow fade | Breathing effect on the current slider color at a fixed slow rate | Yes | No
Beat every 4th | Same as beat fade sync but only fires every 4th beat, using the decay slider | Yes | Yes
Strobe | Rapid flashes of the slider color with 0 ms fade in and ~20 ms fade out | Yes | No
