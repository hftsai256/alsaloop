# Alsaloop
This package is responsible for detecting audio input in the HifiBerryOS system.

## Requirements
The `pyalsaaudio` package is used to read from and write to audio devices. This package does not work on Windows.

## Overview
The original intention to re-write this package is to add support on external audio capturing devices, esp. over USB.
However the original program was a big-loop structure as most simple microcontroller examples: Shared resources are
dumped in global namespace, main program and DBus/MPRIS are executed under different processes and put internal
communication on `stdout`, etc.

The new archetecture is built around a state machine `LoopStateMachine` with a command dispatcher `run()`. There are
4 possible states plus an unknown state:

    class PlayerState(Enum):
        IDLE      : No audio detected.
        PLAY      : Audio detected. Open up the playback device and redirect captured audio.
        HYBERNATE : Sleep n seconds. Can only be invoked by `pause` command over MPRIS interface.
        KILLED    : Receives SIGKILL, SIGTERM or other killing signals.
        UNKNOWN

## Special Remarks
* USB capturing device can be tricky when read under non blocking mode. Emperically my device requires waiting
10 ms after a failed read. The actual time may vary across different interfaces.
* DBus library itself is quite simple, but `python-dbus` package is over-complicated and only supports
event loop from `GLib`. We will have to pass in asyncio event loop reference in order to exchange the
information (issuing command from `DBus/MPRIS` connector to the state machine). I have no idea what was the
original retionale for this decision, but moving over to [`dbussy` package](https://github.com/ldo/dbussy)
and utilize the native `asyncio` interface in the future is strongly encouraged. It is just a bad idea to
introduce an entire GUI library just to bring in "event loop" concept.
* Another process in Hifiberry OS that may run under the similar, but way much more simplified structure, is
[`genclocks.py`](https://github.com/hifiberry/hifiberry-dsp/blob/master/hifiberrydsp/genclocks.py) from
[`hifiberry-dsp`](https://github.com/hifiberry/hifiberry-dsp). It may be better to have another abstraction
layer on top of `LoopStateMachine` and to be built as a package.
* Need structured unittests and git hooks.
