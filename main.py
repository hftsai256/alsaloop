#!/usr/bin/env python3
import signal
import logging
import asyncio
import dbus.service

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from alsaloop import StreamConfig, ProbeConfig, CaptureDevice, LoopStateMachine

class MPRISInterface(dbus.service.Object):
    ''' The base object of an MPRIS player '''

    PATH = "/org/mpris/MediaPlayer2"
    INTROSPECT_INTERFACE = "org.freedesktop.DBus.Introspectable"
    PROP_INTERFACE = dbus.PROPERTIES_IFACE

    def __init__(self, gloop):
        self.name = "org.mpris.MediaPlayer2.usbloop"
        self.bus = dbus.SystemBus(mainloop=gloop)
        self.uname = self.bus.get_unique_name()
        self.dbus_obj = self.bus.get_object("org.freedesktop.DBus",
                                            "/org/freedesktop/DBus")
        self.dbus_obj.connect_to_signal("NameOwnerChanged",
                                        self.name_owner_changed_callback,
                                        arg0=self.name)

        self.acquire_name()
        logging.info("name on DBus aqcuired")

    def name_owner_changed_callback(self, name, old_owner, new_owner):
        if name == self.name and old_owner == self.uname and new_owner != "":
            try:
                pid = self._dbus_obj.GetConnectionUnixProcessID(new_owner)
            except:
                pid = None
            logging.info("Replaced by %s (PID %s)" %
                         (new_owner, pid or "unknown"))
            loop.quit()

    def acquire_name(self):
        self.bus_name = dbus.service.BusName(self.name,
                                             bus=self.bus,
                                             allow_replacement=True,
                                             replace_existing=True)

    def release_name(self):
        if hasattr(self, "_bus_name"):
            del self.bus_name



def main():
    gloop = DBusGMainLoop(set_as_default=True)
    loop = asyncio.get_event_loop()
    mpris = MPRISInterface(gloop)

    shutdown_signals = (signal.SIGTERM, signal.SIGINT)
    stop_signals = (signal.SIGUSR1, )
    restart_signals = (signal.SIGHUP, )

    probe_cfg = ProbeConfig()
    stream_cfg = StreamConfig(pcm_data_format='PCM_FORMAT_S16_LE')

    with CaptureDevice('sysdefault:CARD=system', stream_cfg=stream_cfg) as cdev:
        main_fsm = LoopStateMachine(cdev, probe_cfg=probe_cfg)
        try:
            for s in shutdown_signals:
                loop.add_signal_handler(
                    s, lambda s=s: 
                        asyncio.create_task(main_fsm._shutdown(s)))

            for s in stop_signals:
                loop.add_signal_handler(
                    s, lambda s=s:
                        asyncio.create_task(main_fsm.stop(s)))

            for s in restart_signals:
                loop.add_signal_handler(
                    s, lambda s=s:
                        asyncio.create_task(main_fsm._restart(s)))

            loop.create_task(main_fsm.start())
            loop.run_forever()

        finally:
            loop.close()
            logging.info('Sucessfully shutdown usbloop')


if __name__ == '__main__':
    main()

