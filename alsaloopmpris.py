#!/usr/bin/env python
import os
import signal
import asyncio
import logging
import argparse

from alsaloop import AlsaDeviceConfig, LoopStateMachine
from config import *


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
            '-c', '--capture', default=None,
            help='''Capturing PCM name to be prioritized.
                    Will fall back to the next available device.
                    default=None''')
    parser.add_argument(
            '-p', '--playback', default='default',
            help=argparse.SUPPRESS) # Hide this option from normal users
    parser.add_argument(
            '-f', '--format', default='PCM_FORMAT_S16_LE',
            help='''PCM data format. Check https://larsimmisch.github.io/pyalsaaudio/libalsaaudio.html for details.
                    default=\'PCM_FORMAT_S16_LE\'''')
    parser.add_argument(
            '-v', '--verbose', action='store_true',
            help='''Increase logging verbosity (DEBUG)''')

    return parser.parse_args()


def logger_config(verbose):
    logging_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=logging_level)
    logging.captureWarnings(True)


def main():
    args = parse_args()
    logger_config(args.verbose)
    logging.info('%s started with PID %d', PACKAGE_NAME, os.getpid())
    loop = asyncio.get_event_loop()

    shutdown_signals = (signal.SIGTERM, signal.SIGINT)
    restart_signals = (signal.SIGHUP, signal.SIGUSR1)

    capture_cfg = AlsaDeviceConfig(args.capture, pcm_data_format=args.format)
    playback_cfg = AlsaDeviceConfig(args.playback, pcm_data_format=args.format)

    with LoopStateMachine(capture_cfg, playback_cfg) as loopsm:

        try:
            for s in shutdown_signals:
                loop.add_signal_handler(
                    s, lambda s=s: 
                        asyncio.create_task(loopsm._shutdown(s)))

            for s in restart_signals:
                loop.add_signal_handler(
                    s, lambda s=s:
                        asyncio.create_task(loopsm._restart(s)))

            loop.create_task(loopsm.run())
            loop.run_forever()

        finally:
            loop.close()
            logging.info('Sucessfully shutdown %s', PACKAGE_NAME)


if __name__ == '__main__':
    main()
