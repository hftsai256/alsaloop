# Alsaloop+ (Enhanced to support USB interface)
This package is responsible for detecting audio input in the HifiBerryOS system.

## Requirements
Raspberry pi 4 is strongly recommended when streaming high bitrate audio stream. The USB I/O latency on Raspberry
pi 3 is slow enough to generate noticable jitter in the background. Skipping polling from the capturing device
when the playback device is occupied may mitigate this artifact, but I don't have proper instruments to measure
the jitter quantitatively.

From a pure hardware perspective, I would consider Rock 3A. However its support to buildroot still has a long
way to go.

## Overview
The original intention to re-write this package is to add support on external audio capturing devices, esp.
over USB. However the original program was a big-loop structure with a bunch of smells: Shared resources are
dumped in global namespace, main program and DBus/MPRIS are executed under different processes and put internal
communication on `stdout`, etc.

The new archetecture is built around a state machine `LoopStateMachine` with a command dispatcher `run()`.
There are 4 possible states plus an unknown state:

    class PlayerState(Enum):
        IDLE      : No audio detected.
        PLAY      : Audio detected. Open up the playback device and redirect captured audio.
        HYBERNATE : Sleep n seconds. Entered when paused.
        KILLED    : Receives SIGKILL or SIGTERM.
        UNKNOWN

This package is built in mind to be the drop-in replacement to the official `alsaloop` package, installed under
`/opt/alsaloop`, therefore there are some anti-pattern designs left behind and I prefer not to tidy them up.

Also we need to create the file `/custom/hifiberry/analoginput.feature` to pretend we have an input audio interface,
so that Beocreate UI can load `alsaloop` (or Analog Input) package. This is included in `hackfeature.sh` script,
and will prompt you to run `/opt/hifiberry/bin/reconfigure-players` manually to apply this hack. If you
are experiencing UI loading error, try to remove the `analoginput.feature` file, reconfigure, and then add it
back in.

## Special Remarks
* USB capturing device can be tricky when read under non blocking mode. Emperically my device requires waiting
10 ms after a failed read. The actual time may vary across different interfaces.

* DBus library itself is quite simple, but `python-dbus` package is over-complicated and only supports
event loop from `GLib`. We will have to pass in asyncio event loop reference in order to exchange the
information (issuing command from `DBus/MPRIS` connector to the state machine). I have no idea what was the
original retionale for this decision, but moving over to [`dbussy` package](https://github.com/ldo/dbussy)
and utilize the native `asyncio` interface in the future is strongly encouraged. It is a bad idea to
introduce an entire GUI library just to bring in the "event loop" concept.

* Another process in Hifiberry OS that can run under the similar, but way much more simplified structure, is
[`genclocks.py`](https://github.com/hftsai256/hifiberry-dsp/blob/master/hifiberrydsp/genclocks.py) from
[`hifiberry-dsp`](https://github.com/hifiberry/hifiberry-dsp). It may be better to have another abstraction
layer on top of `LoopStateMachine` and to be built as a package.

* Need structured unittests and git hooks.
