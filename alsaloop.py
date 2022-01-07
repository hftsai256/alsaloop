import os
import io
import re
import json
import struct
import logging
import asyncio
import warnings
import alsaaudio
import statistics
from typing import Optional
from itertools import islice
from collections import namedtuple
from dataclasses import dataclass

from mpris import MPRISConnector
from fileio import *
from config import *

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
                 pcm_name: str = HFBCARD_PCM,
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
class AlsaDevice:
    name: str
    cfg: AlsaDeviceConfig
    dev_type: int
    dev_mode: int = alsaaudio.PCM_NONBLOCK
    index: Optional[int] = None
    occupied: bool = False
    device: alsaaudio.PCM = None

    def __init__(self, cfg: AlsaDeviceConfig = AlsaDeviceConfig()):
        self.name = self._pick(cfg.pcm_name)
        self.cfg = cfg

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self):
        self.device = alsaaudio.PCM(self.dev_type, self.dev_mode, device=self.name)
        self.device.setchannels(self.cfg.channels)
        self.device.setrate(self.cfg.rate)
        self.device.setformat(getattr(alsaaudio, self.cfg.format))
        self.device.setperiodsize(self.cfg.period_size)

    def close(self):
        self.device.close()

    def _pick(self, name):
        pcms = alsaaudio.pcms(self.dev_type)
        if name in pcms:
            return name
        elif f'sysdefault:CARD={name}' in pcms:
            return f'sysdefault:CARD={name}'
        return [n for n in pcms if 'sysdefault:CARD' in n][0]


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
                logging.warning('Write buffer full on playback')
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


class SequenceComp:
    def __init__(self, *reference):
        logging.debug(f'sequencial comparator registered as {reference}')
        self.reference = reference

    def comp(self, val):
        return [val > ref for ref in self.reference]


class LoopStateMachine:
    def __init__(self,
                 capture_cfg: AlsaDeviceConfig,
                 playback_cfg: AlsaDeviceConfig):
        self.task_queue = asyncio.Queue()
        self.capture_cfg = capture_cfg
        self.playback_cfg = playback_cfg
        self.probe_cfg = self.__load_config()
        self.capture = None
        self.dbus = None
        self.active = self.probe_cfg.sensitivity

        self._local_state = PlayerState.UNKNOWN
        self._buffer = b''

    def __reverse_db(self, db):
        return self.capture_cfg.maxamp * 10**(db/20)

    @property
    def playback_free(self):
        """Check if playback device is busy"""
        return cat(f'/proc/asound/{HFBCARD_NAME}/pcm0p/sub0/status').strip() == 'closed'

    @property
    def active(self):
        """Calculated median from data across all channels.
           40 frames takes around 1 ms at 40 frames with setero data on pi3, which is slow enough
           to generate noticable jitter when playing high sampled stream. Therefore it is necessary
           to lower the sample size and/or decrease process priority to mitigate this effect.
        """
        samples = islice(MemScope(self._buffer, self.capture_cfg), self.probe_cfg.sample_size)
        data = [abs(val - self.capture_cfg.reference) for packet in samples for val in packet]
        med = statistics.median(data)
        return self._threscomp.comp(med)

    @active.setter
    def active(self, val):
        if val == 0:
            self._threscomp = SequenceComp(0, 0)
        else:
            val = -abs(val)
            self._threscomp = SequenceComp(
                self.__reverse_db(val),
                self.__reverse_db(val - 3))

    def __load_config(self):
        default = ProbeConfig()
        try:
            with open(Env.CFGFILE, 'r') as fp:
                json_db = json.load(fp)
                default.update(json_db)
                logging.info('Load config from %s', Env.CFGFILE)
        except (FileNotFoundError, json.decoder.JSONDecodeError):
            logging.info('Cannot read %s. Using default configuration', Env.CFGFILE)
        return default

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self):
        self.capture = CaptureDevice(self.capture_cfg)
        self.dbus = MPRISConnector(self.task_queue)
        self.capture.open()
        self.dbus.open()

    def close(self):
        self.capture.close()
        self.dbus.close()

    @property
    def state(self):
        """Wrapping over self.state property so that it can be synchronized with
           PlaybackStatus from MPRIS connector"""
        return self._local_state

    @state.setter
    def state(self, val):
        self._local_state = val
        try:
            self.dbus.player.PlaybackStatus = MPRISStatus[val]
        except AttributeError:
            logging.warning('Cannot connect to DBus.')

    async def run(self):
        """Main command dispatcher a.k.a. main coroutine.
           Responsible to take external (MPRIS/DBus) or internal command
           to switch between operation states"""
        FutureTask = namedtuple('TaskInfo', ['state', 'delay', 'coro'])
        cmdtasks = {
            PlayerCommand.STOP: FutureTask(PlayerState.HYBERNATE,
                                           self.probe_cfg.hybernate_interval,
                                           [self._wake]),
            PlayerCommand.IDLE: FutureTask(PlayerState.IDLE,
                                           self.probe_cfg.idle_interval,
                                           [self._idle]),
            PlayerCommand.PLAY: FutureTask(PlayerState.PLAY,
                                           0,
                                           [self._monitor, self._stream]),
            PlayerCommand.KILL: FutureTask(PlayerState.KILLED,
                                           0,
                                           [self._shutdown])
        }

        self.loop = asyncio.get_running_loop()
        self.dbus.aioloop = self.loop
        await self.task_queue.put(PlayerCommand.IDLE)

        while self.state is not PlayerState.KILLED:
            cmd = await self.task_queue.get()
            await self._gather()

            todo = cmdtasks[cmd]
            logging.info('Dispatch received command %s in %.1f seconds', cmd, todo.delay)
            self.state = todo.state
            for coroutine in todo.coro:
                self.loop.call_later(todo.delay, asyncio.create_task, coroutine())
            self.task_queue.task_done()

    async def _wake(self):
        await self.task_queue.put(PlayerCommand.IDLE)

    async def _idle(self):
        """Idle state, sampling signal from the capturing device periodically.
           If playback device is busy (another player is running) then switch to
           hybernate state. Also, setting to the lowest priority to avoid interfering
           with other task that may create noticable jitter."""
        os.nice(19)
        counter = 0
        while self.state == PlayerState.IDLE:
            if not self.playback_free:
                await asyncio.sleep(self.probe_cfg.follow_interval)
                continue

            self._buffer = self.capture.read()
            if all(self.active):
                counter += 1
            else:
                counter = 0
            logging.debug(f'{self.active!r}, {counter=}')

            if counter >= self.probe_cfg.start_count:
                await self.task_queue.put(PlayerCommand.PLAY)
                return
            elif counter > 0:
                await asyncio.sleep(self.probe_cfg.follow_interval)
            else:
                await asyncio.sleep(self.probe_cfg.idle_interval)

    async def _monitor(self):
        """Monitoring streaming signal intensity periodically. Shut off playback device
           when the capturing signal is below certain threshold over a period of time"""
        counter = 0
        while self.state == PlayerState.PLAY:
            if not any(self.active):
                counter += 1
            else:
                counter = 0
            logging.debug(f'{self.active!r}, {counter=}')

            if counter >= self.probe_cfg.stop_count:
                await self.task_queue.put(PlayerCommand.IDLE)
                return
            elif counter > 0:
                await asyncio.sleep(self.probe_cfg.follow_interval)
            else:
                await asyncio.sleep(self.probe_cfg.stream_interval)

    async def _stream(self):
        """Redirect whatever is captured from the designated interface. Bring the process
           priority to normal."""
        os.nice(0)
        logging.info('start redirecting [%s] => [%s]', self.capture.name, self.playback_cfg.pcm_name)
        try:
            with PlaybackDevice(self.playback_cfg) as playback:
                playback.write(self._buffer)
                while self.state == PlayerState.PLAY:
                    self._buffer = self.capture.read()
                    playback.write(self._buffer)
                    await asyncio.sleep(0.001)

        except alsaaudio.ALSAAudioError as e:
            logging.info('Error opening playback device: %s', e)
            await self.task_queue.put(PlayerCommand.STOP)

    async def _gather(self):
        """Cancel all other active tasks to close ALSA devices gracefully"""
        tasks = [t for t in asyncio.all_tasks() if t is not
                 asyncio.current_task()]

        [task.cancel() for task in tasks]

        logging.info('Cancelling %d outstanding tasks', len(tasks))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _restart(self, sig):
        """Restart handler"""
        self.state = PlayerState.UNKNOWN
        logging.info('Received restart signal %s.', sig.name)
        await self._gather()
        self.probe_cfg = self.__load_config()
        self.loop.create_task(self.run())

    async def _shutdown(self, sig):
        """Kill handler"""
        self.state = PlayerState.KILLED
        logging.info('Received exit signal %s.', sig.name)
        await self._gather()
        self.close()
        self.loop.stop()
