#!/usr/bin/env python

import io
import re
import math
import enum
import struct
import signal
import logging
import asyncio
import warnings
import alsaaudio
import statistics
from typing import Optional
from itertools import islice
from collections import namedtuple
from dataclasses import dataclass

logging.captureWarnings(True)
logging.basicConfig(level=logging.INFO)

@dataclass
class AlsaDeviceConfig:
    pcm_name: str
    format: str
    size: int
    signed: bool
    endian: str
    channels: int
    rate: int
    period_size: int
    period_time: float
    maxamp: int
    reference: int

    is_signed = {'S': True, 'U': False}
    fmt_matcher = re.compile(r'.*(S|U)(\d+)_?(LE|BE)?$').match

    def __init__(self,
                 pcm_name: str = 'default',
                 pcm_data_format: str ='PCM_FORMAT_S16_LE',
                 channels: int = 2,
                 rate: int = 48000,
                 period_frames: int = 1024):
        res = self.fmt_matcher(pcm_data_format)
        
        if res:
            sign, bits, endian = res.groups()
            self.size = int(bits) // 8
            self.signed = self.is_signed[sign]
            self.endian = endian
        else:
            warnings.warn('Invalid format string. Falling back to \'PCM_FORMAT_S16_LE\'')
            pcm_data_format = 'PCM_FORMAT_S16_LE'
            self.size, self.signed, self.endian = 2, True, 'LE'

        self.pcm_name = pcm_name
        self.format = pcm_data_format
        self.channels = channels
        self.rate = rate
        self.frame_size = channels * self.size
        self.period_size = period_frames
        self.period_time = period_frames / rate
        self.maxamp = 1 << int(bits) - 1
        self.reference = 0 if self.signed else self.maxamp


@dataclass
class ProbeConfig:
    idle_interval: float = 1.0
    follow_interval: float = 0.5
    stream_interval: float = 2.0
    hybernate_interval: float = 60
    start_count: int = 1
    stop_count: int = 10


@dataclass
class AlsaDevice:
    name: str
    cfg: AlsaDeviceConfig
    dev_type: int
    dev_mode: int = alsaaudio.PCM_NONBLOCK
    index: Optional[int] = None
    occupied: bool = False
    device: alsaaudio.PCM = None

    def __init__(self, cfg: AlsaDeviceConfig = AlsaDeviceConfig()):
        self.name = cfg.pcm_name
        self.cfg = cfg

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self):
        self.device = alsaaudio.PCM(self.dev_type, self.dev_mode, device=self.cfg.pcm_name)
        self.device.setchannels(self.cfg.channels)
        self.device.setrate(self.cfg.rate)
        self.device.setformat(getattr(alsaaudio, self.cfg.format))
        self.device.setperiodsize(self.cfg.period_size)

    def close(self):
        self.device.close()


class CaptureDevice(AlsaDevice):
    dev_type: int = alsaaudio.PCM_CAPTURE

    def read(self):
        while True:
            length, data = self.device.read()
            if length > 0:
                return data
            elif length == 0:
                warnings.warn(f'Incomplete read {length=}', RuntimeWarning)
            elif length == -32:
                warnings.warn(f'Broken Pipe: {length}', RuntimeWarning)
            else:
                warnings.warn(f'Unknown Error: Code={length}', RuntimeWarning)


class PlaybackDevice(AlsaDevice):
    dev_type: int = alsaaudio.PCM_PLAYBACK

    def write(self, data):
        while True:
            written = self.device.write(data)

            if written == 0:
                warnings.warn(f'Buffer full', RuntimeWarning)
                continue

            return written


class MemScope:
    endian_t = {
        None: '' ,
        'LE': '<',
        'BE': '>',
    }
    struct_t = {
    #   (signed, size)
        (  True,    1): 'b',
        ( False,    1): 'c',
        (  True,    2): 'h',
        ( False,    2): 'H',
        (  True,    3): 'i',
        ( False,    3): 'I',
        (  True,    4): 'i',
        ( False,    4): 'I',
    }
    padding_t = {
    #   (signed, size, endian)
        (  True,    3,   'LE'): lambda x: x + (b'\0' if x[2] < 128 else b'\xff'),
        ( False,    3,   'LE'): lambda x: x + b'\0',
        (  True,    3,   'BE'): lambda x: (b'\0' if x[2] < 128 else b'\xff') + x,
        ( False,    3,   'BE'): lambda x: b'\xff' + x,
    }

    def __init__(self,
                 data: bytearray,
                 dev_cfg: AlsaDeviceConfig):
        self.dev_cfg = dev_cfg
        self.buffer = io.BytesIO(data)
        self.struct = struct.Struct(self._struct_str)

        pad_lookup = (dev_cfg.signed, dev_cfg.size, dev_cfg.endian)
        self.padding = self.padding_t.get(pad_lookup, lambda x: x)

    def __iter__(self):
        self.buffer.seek(0)
        return self

    def __next__(self):
        chunk = b''
        try:
            for _ in range(self.dev_cfg.channels):
                chunk += self.padding(self.buffer.read(self.dev_cfg.size))
            return self.struct.unpack(chunk)
        except (struct.error, IndexError):
            raise StopIteration

    @property
    def _struct_str(self):
        endian = self.endian_t[self.dev_cfg.endian]
        body = self.struct_t[(self.dev_cfg.signed, self.dev_cfg.size)] \
               * self.dev_cfg.channels
        return (f'{endian}{body}')


class PlayerCommand(enum.Enum):
    STOP = enum.auto()
    PLAY = enum.auto()


class PlayerState(enum.Enum):
    IDLE      = enum.auto()
    PLAY      = enum.auto()
    HYBERNATE = enum.auto()
    KILLED    = enum.auto()
    UNKNOWN   = enum.auto()


class LoopStateMachine:
    def __init__(self,
                 queue: asyncio.Queue,
                 capture_cfg: AlsaDeviceConfig,
                 playback_cfg: AlsaDeviceConfig,
                 probe_cfg: ProbeConfig = ProbeConfig(),
                 threshold_db: float = -60):
        self.queue = queue
        self.state = PlayerState.UNKNOWN
        self.counter = 0
        self.is_streaming = False
        self.capture_cfg = capture_cfg
        self.playback_cfg = playback_cfg
        self.capture = CaptureDevice(self.capture_cfg)
        self.probe_cfg = probe_cfg
        self.buffer = b''

        Thresholds = namedtuple('Thresholds', ['start', 'stop'])
        self.thresholds = Thresholds(
                self.__reverse_db(threshold_db),
                self.__reverse_db(threshold_db - 3)
        )

    def __reverse_db(self, db):
        return self.capture_cfg.maxamp * 10**(-abs(db)/20)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self):
        self.capture = CaptureDevice(self.capture_cfg)
        self.capture.open()

    def close(self):
        self.capture.close()

    async def run(self):
        TaskInfo = namedtuple('TaskInfo', ['state', 'delay', 'coro'])
        manifests = {
            PlayerCommand.STOP: TaskInfo(PlayerState.HYBERNATE,
                                         self.probe_cfg.hybernate_interval,
                                         self._wakeup),
            PlayerCommand.PLAY: TaskInfo(PlayerState.IDLE, 0, self._idle)
        }

        self.loop = asyncio.get_running_loop()
        await self.queue.put(PlayerCommand.PLAY)

        while self.state is not PlayerState.KILLED:
            cmd = await self.queue.get()
            await self._gather()

            todo = manifests[cmd]
            self.state = todo.state
            self.loop.call_later(todo.delay, asyncio.create_task, todo.coro())

    async def _wakeup(self):
        self.state = PlayerState.IDLE
        self.loop.create_task(self._idle())

    async def _idle(self):
        while self.state == PlayerState.IDLE:
            self.buffer = self.capture.read()
            med = self._smp_median(self.buffer)
            if med > self.thresholds.start:
                self.counter += 1
                logging.debug(f'probe_idle: {med:.0f} > {self.thresholds.start:.0f}, {self.counter=}')
            else:
                self.counter = 0
                logging.debug(f'probe_idle: {med:.0f} < {self.thresholds.start:.0f}, {self.counter=}')

            if self.counter >= self.probe_cfg.start_count:
                self.counter = 0
                self.state = PlayerState.PLAY
                self.loop.create_task(self._stream())
                await asyncio.sleep(self.probe_cfg.stream_interval)
                self.loop.create_task(self._monitor())
            elif self.counter > 0:
                await asyncio.sleep(self.probe_cfg.follow_interval)
            else:
                await asyncio.sleep(self.probe_cfg.idle_interval)

    async def _monitor(self):
        while self.state == PlayerState.PLAY:
            med = self._smp_median(self.buffer)
            if med < self.thresholds.stop:
                self.counter += 1
                logging.debug(f'probe_stream: {med:.0f} < {self.thresholds.stop:.0f}, {self.counter=}')
            else:
                self.counter = 0
                logging.debug(f'probe_stream: {med:.0f} > {self.thresholds.stop:.0f}, {self.counter=}')

            if self.counter >= self.probe_cfg.stop_count:
                self.state = PlayerState.IDLE
                self.counter = 0
                await asyncio.sleep(self.probe_cfg.idle_interval)
                self.loop.create_task(self._idle())
            elif self.counter > 0:
                await asyncio.sleep(self.probe_cfg.follow_interval)
            else:
                await asyncio.sleep(self.probe_cfg.stream_interval)

    async def _stream(self):
        logging.info(f'start redirecting [{self.capture.name}] => [{self.playback_pcm_name}]')

        with PlaybackDevice(self.playback_cfg) as playback:
            playback.write(self.buffer)

            while self.state == PlayerState.PLAY:
                self.buffer = self.capture.read()
                playback.write(self.buffer)
                await asyncio.sleep(0.001)

        logging.info('close playback')

    async def _gather(self):
        tasks = [t for t in asyncio.all_tasks() if t is not
                 asyncio.current_task()]

        [task.cancel() for task in tasks]

        logging.info(f"Cancelling {len(tasks)} outstanding tasks")
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _restart(self, sig):
        self.state = PlayerState.UNKNOWN
        logging.info(f'Received restart signal {sig.name}.')
        await self._gather()
        self.loop.create_task(self._idle())

    async def _shutdown(self, sig):
        self.state = PlayerState.KILLED
        logging.info(f'Received exit signal {sig.name}...')
        await self._gather()
        self.loop.stop()

    def _smp_median(self, buffer, n_sample=5):
        sumsq = []
        samples = islice(MemScope(buffer, self.capture_cfg), n_sample)
        data = [abs(val - self.capture_cfg.reference) for packet in samples for val in packet]

        return statistics.median(data)


def main():
    loop = asyncio.get_event_loop()
    queue = asyncio.Queue()

    shutdown_signals = (signal.SIGTERM, signal.SIGINT)
    stop_signals = (signal.SIGUSR1, )
    restart_signals = (signal.SIGHUP, )

    capture_cfg = AlsaDeviceConfig('sysdefault:CARD=system', pcm_data_format='PCM_FORMAT_S16_LE')
    playback_cfg = AlsaDeviceConfig('default', pcm_data_format='PCM_FORMAT_S16_LE')

    with LoopStateMachine(queue, capture_cfg, playback_cfg) as redirector:

        try:
            for s in shutdown_signals:
                loop.add_signal_handler(
                    s, lambda s=s: 
                        asyncio.create_task(redirector._shutdown(s)))

            for s in stop_signals:
                loop.add_signal_handler(
                    s, lambda s=s:
                        asyncio.create_task(redirector._shutdown(s)))

            for s in restart_signals:
                loop.add_signal_handler(
                    s, lambda s=s:
                        asyncio.create_task(redirector._restart(s)))

            loop.create_task(redirector.run())
            loop.run_forever()

        finally:
            loop.close()
            logging.info('Sucessfully shutdown usbloop')


if __name__ == '__main__':
    main()

