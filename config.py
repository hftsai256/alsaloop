from typing import Dict
from enum import Enum, auto
from pathlib import Path
from types import SimpleNamespace
from dataclasses import dataclass, field
import dbus

PACKAGE_ROOT = Path(__file__).parent
PACKAGE_NAME = PACKAGE_ROOT.parts[-1]

class PlayerCommand(Enum):
    STOP   = auto()
    IDLE   = auto()
    PLAY   = auto()
    KILL   = auto()

class PlayerState(Enum):
    IDLE      = auto()
    PLAY      = auto()
    HYBERNATE = auto()
    KILLED    = auto()
    UNKNOWN   = auto()


DBusConfig = SimpleNamespace(
    introspect_iface = 'org.freedesktop.DBus.Introspectable',
    property_iface   = dbus.PROPERTIES_IFACE,
    player_iface     = 'org.mpris.MediaPlayer2.Player',
    mpris_iface      = 'org.mpris.MediaPlayer2',
    path             = '/org/mpris/MediaPlayer2',
    introspect_xml   = PACKAGE_ROOT/Path('mpris2_introspection.xml')
)

MPRISStatus = {
    PlayerState.IDLE      : 'stopped',
    PlayerState.PLAY      : 'playing',
    PlayerState.HYBERNATE : 'pause',
    PlayerState.KILLED    : 'stopped',
    PlayerState.UNKNOWN   : 'unknown' }

Env = SimpleNamespace(
    CFGFILE=Path(f'/etc/{PACKAGE_NAME}.json'))


class UpdatableDataclass:
    def update(self, new):
        for key, value in new.items():
            if hasattr(self, key):
                setattr(self, key, value)


@dataclass
class ProbeConfig(UpdatableDataclass):
    sensitivity: float = -60
    idle_interval: float = 0.5
    follow_interval: float = 1.0
    stream_interval: float = 2.0
    hybernate_interval: float = 15
    start_count: int = 1
    stop_count: int = 10
    sample_size: int = 8 


@dataclass
class DBusPlayerProperty:
    PlaybackStatus: str      = ''
    Metadata      : Dict     = field(default_factory=lambda:
                               {'xesam:url': f'{PACKAGE_NAME}://'})
    Rate          : float    = 1.0
    MinimumRate   : float    = 1.0
    MaximumRate   : float    = 1.0
    CanGoNext     : bool     = False 
    CanGoPrevious : bool     = False
    CanPlay       : bool     = True
    CanPause      : bool     = True
    CanSeek       : bool     = False
    CanControl    : bool     = False


@dataclass(frozen=True)
class DBusMPRISProperty:
    Identity            : str  = PACKAGE_NAME
    DesktopEntry        : str  = PACKAGE_NAME
    CanQuit             : bool = False
    CanRaise            : bool = False
    HasTrackList        : bool = False
    SupportedUriSchemes : dbus.Array = field(default_factory=lambda:
                          dbus.Array(signature='s'))
    SupportedMimeTypes  : dbus.Array = field(default_factory=lambda:
                          dbus.Array(signature='s'))



