#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author: Modul 9 <info@hifiberry.com>
# Based on mpDris2 by
#          Jean-Philippe Braun <eon@patapon.info>,
#          Mantas Mikulėnas <grawity@gmail.com>
# Based on mpDris by:
#          Erik Karlsson <pilo@ayeon.org>
# Some bits taken from quodlibet mpris plugin by:
#           <christoph.reiter@gmx.at>

import logging
import asyncio

from threading import Thread 
from dataclasses import dataclass, asdict

import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from config import *


@dataclass
class DBusRuntimeObject:
    broadcast_name : str
    unique_name: str
    bus_name: dbus.service.BusName
    thread: Thread
    bus: dbus._dbus.SystemBus
    obj: dbus.proxies.ProxyObject


class DBusThread(Thread):
    def __init__(self):
        super().__init__()
        DBusGMainLoop(set_as_default=True)
        self.loop = GLib.MainLoop()

    def run(self):
        self.loop.run()

    def stop(self):
        self.loop.quit()


class MPRISConnector(dbus.service.Object):
    def __init__(self, tx_queue):
        self.txq = tx_queue
        self.player = DBusPlayerProperty()
        self.mpris = DBusMPRISProperty()
        self.dbus = None
        self.aioloop = None

        with open(DBusConfig.introspect_xml, 'r') as fp:
            self.introspect_xml = fp.read()

        self.ifacemap = {
                DBusConfig.player_iface : self.player,
                DBusConfig.mpris_iface  : self.mpris
        }

    def open(self):
        broadcast_name = f'{DBusConfig.mpris_iface}.{PACKAGE_NAME}'

        dbus_thread = DBusThread()
        dbus.service.Object.__init__(self, dbus.SystemBus(), DBusConfig.path)

        bus = dbus.SystemBus()
        dbus_obj = bus.get_object(
                'org.freedesktop.DBus', '/org/freedesktop/DBus')
        dbus_obj.connect_to_signal(
                'NameOwnerChanged', self.change_owner_cb, arg0=broadcast_name)

        self.dbus = DBusRuntimeObject(
                broadcast_name,
                bus.get_unique_name(),
                dbus.service.BusName(
                    broadcast_name, bus=bus, allow_replacement=True, replace_existing=True),
                dbus_thread,
                bus,
                dbus_obj
        )

        dbus_thread.start()
        logging.info('DBus thread started at %s as %s',
                     self.dbus.unique_name, broadcast_name)

    def close(self):
        self.dbus.bus.close()
        self.dbus.thread.stop()

    def change_owner_cb(self, name, old_owner, new_owner):
        if str(name) == self.dbus.broadcast_name \
           and str(old_owner) == self.dbus.unique_name \
           and str(new_owner) != '':
            try:
                pid = self.dbus_obj.GetConnectionUnixProcessID(new_owner)
            except:
                pid = None
            logging.info("Replaced by %s (PID %s)" %
                         (new_owner, pid or "unknown"))
            self.dbus.thread.stop()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    @dbus.service.method(DBusConfig.introspect_iface)
    def Introspect(self):
        return self.introspect_xml

    @dbus.service.signal(DBusConfig.property_iface, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed_properties,
                          invalidated_properties):
        pass

    @dbus.service.method(DBusConfig.property_iface,
                         in_signature="ss", out_signature="v")
    def Get(self, interface, key):
        ret = getattr(self.ifacemap[str(interface)], key)
        logging.debug('Return attribute on %s[%s]: %s', str(interface), key, ret)
        return getattr(self.ifacemap[str(interface)], key)

    @dbus.service.method(DBusConfig.property_iface,
                         in_signature="ssv", out_signature="")
    def Set(self, interface, key, value):
        try:
            setattr(self.ifacemap[str(interface)], key, value)
            logging.debug('Set attribute on %s[%s]: %s', str(interface), key, value)
        except AttributeError:
            pass

    @dbus.service.method(DBusConfig.property_iface,
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        logging.debug('Return all attributes on %s', str(interface))
        return asdict(self.ifacemap[str(interface)])

    # Player methods
    @dbus.service.method(DBusConfig.player_iface, in_signature='', out_signature='')
    def Pause(self):
        logging.debug('Received DBus pause')
        future = asyncio.run_coroutine_threadsafe(self.txq.put(PlayerCommand.STOP), self.aioloop)
        return future.result(0.1)

    @dbus.service.method(DBusConfig.player_iface, in_signature='', out_signature='')
    def PlayPause(self):
        logging.debug('Received DBus play/pause')

        if self.playback_status in [PlayerState.PLAY, PlayerState.IDLE]:
            future = asyncio.run_coroutine_threadsafe(self.txq.put(PlayerCommand.STOP), self.aioloop)
        else:
            future = asyncio.run_coroutine_threadsafe(self.txq.put(PlayerCommand.IDLE), self.aioloop)
        return future.result(0.1)

    @dbus.service.method(DBusConfig.player_iface, in_signature='', out_signature='')
    def Stop(self):
        logging.debug('Received DBus stop')
        future = asyncio.run_coroutine_threadsafe(self.txq.put(PlayerCommand.STOP), self.aioloop)
        return future.result(0.1)

    @dbus.service.method(DBusConfig.player_iface, in_signature='', out_signature='')
    def Play(self):
        future = asyncio.run_coroutine_threadsafe(self.txq.put(PlayerCommand.IDLE), self.aioloop)
        return future.result(0.1)
