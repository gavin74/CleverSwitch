import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from ..event.device_connected_event import DeviceConnectedEvent
from ..event.host_change_event import HostChangeEvent
from ..event.write_event import WriteEvent
from ..hidpp.constants import (
    CHANGE_HOST_FN_SET,
    FEATURE_CHANGE_HOST,
    HOST_SWITCH_CIDS,
    SW_ID_HOST_CHANGE,
)
from ..hidpp.protocol import build_msg, pack_params
from ..registry.logi_device_registry import LogiDeviceRegistry
from ..subscriber.subscriber import Subscriber
from ..topic.topics import Topics

log = logging.getLogger(__name__)

PEER_HOST_ENV = "CLEVERSWITCH_PEER_HOST"
PEER_HOST_FILE = Path.home() / ".config" / "cleverswitch" / "peer_host"
HOME_NETWORK_FILE = Path.home() / ".config" / "cleverswitch" / "home_network"
BOUNCE_FILE = Path.home() / ".config" / "cleverswitch" / "bounce_followers"
RELAY_DELAY_SECONDS = float(os.environ.get("CLEVERSWITCH_RELAY_DELAY", "1.5"))
RELAY_MAX_IDLE_SECONDS = float(os.environ.get("CLEVERSWITCH_RELAY_MAX_IDLE", "300"))
RELAY_ON_BATTERY = os.environ.get("CLEVERSWITCH_RELAY_ON_BATTERY") == "1"
BOUNCE_GRACE_SECONDS = float(os.environ.get("CLEVERSWITCH_BOUNCE_GRACE", "90"))
VALID_HOSTS = frozenset(HOST_SWITCH_CIDS.values())


def _on_ac_power() -> bool:
    """True on mains power, or wherever the probe fails or does not apply.

    A keyboard link lost while the machine runs on battery means the machine
    was carried away from the desk, not that the user pressed Easy-Switch at
    it. Fails open so a broken probe never disables relaying.
    """
    if RELAY_ON_BATTERY:
        return True
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["/usr/bin/pmset", "-g", "ps"], capture_output=True, text=True, timeout=5
            ).stdout
            return "AC Power" in out
        if sys.platform == "win32":
            import ctypes

            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_uint),
                    ("BatteryFullLifeTime", ctypes.c_uint),
                ]

            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                # 0 = battery, 1 = mains, 255 = unknown (fail open)
                return status.ACLineStatus != 0
    except Exception as e:
        log.debug(f"Power check failed: {e}")
    return True


def _system_idle_seconds() -> float | None:
    """Seconds since the last local keyboard/mouse input, or None if unknown.

    A real Easy-Switch press happens at an active desk; a power-save link drop
    happens long after the user left. Unknown fails open (relay proceeds).
    """
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
            if m:
                return int(m.group(1)) / 1_000_000_000
        elif sys.platform == "win32":
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                # GetTickCount wraps at 2**32 ms (49.7 days); mask the difference.
                ticks = ctypes.windll.kernel32.GetTickCount() - info.dwTime
                return (ticks & 0xFFFFFFFF) / 1000.0
    except Exception as e:
        log.debug(f"Idle check failed: {e}")
    return None


def _gateway_mac() -> str | None:
    """MAC of the default gateway on a physical interface (macOS).

    Reads the routing table rather than `route get default` because a VPN
    moves the primary default onto a utun interface, which has no MAC; the
    LAN default on en* stays in the table and identifies the real router.
    """
    try:
        routes = subprocess.run(
            ["/usr/sbin/netstat", "-rn", "-f", "inet"], capture_output=True, text=True, timeout=5
        ).stdout
        gateway = None
        for line in routes.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "default" and not parts[-1].startswith("utun"):
                gateway = parts[1]
                break
        if gateway is None:
            return None
        arp = subprocess.run(
            ["/usr/sbin/arp", "-n", gateway], capture_output=True, text=True, timeout=5
        ).stdout
        m = re.search(r" at ([0-9a-fA-F:]+) ", arp)
        if m is None:
            return None
        return ":".join(f"{int(part, 16):02x}" for part in m.group(1).split(":"))
    except Exception as e:
        log.debug(f"Gateway lookup failed: {e}")
        return None


def _on_home_network() -> bool:
    """True when the LAN router matches a recorded home router, when no home
    network is recorded, or when the router cannot be determined (fail open)."""
    if sys.platform != "darwin" or not HOME_NETWORK_FILE.is_file():
        return True
    try:
        recorded = {
            line.strip().lower()
            for line in HOME_NETWORK_FILE.read_text().splitlines()
            if line.strip()
        }
    except OSError:
        return True
    if not recorded:
        return True
    mac = _gateway_mac()
    return mac is None or mac in recorded


def _configured_peer_host() -> int | None:
    try:
        value = os.environ.get(PEER_HOST_ENV)
        if value is None and PEER_HOST_FILE.is_file():
            value = PEER_HOST_FILE.read_text().strip()
    except OSError as e:
        log.warning(f"Cannot read {PEER_HOST_FILE}: {e}; disconnect relay disabled")
        return None
    if not value:
        return None
    try:
        host = int(value)
    except ValueError:
        log.warning(f"Invalid peer host {value!r}; disconnect relay disabled")
        return None
    if host not in VALID_HOSTS:
        log.warning(
            f"Peer host {host} not in {sorted(VALID_HOSTS)}; disconnect relay disabled"
        )
        return None
    return host


def _bounce_wpids() -> set[int]:
    """Follower wpids that may be sent home when no keyboard is present.

    One wpid per line (hex like 0xB035 or decimal). Listing devices explicitly
    keeps a host's own dedicated mouse out of the bounce logic."""
    try:
        if not BOUNCE_FILE.is_file():
            return set()
        wpids = set()
        for line in BOUNCE_FILE.read_text().splitlines():
            line = line.strip()
            if line:
                wpids.add(int(line, 0))
        return wpids
    except (OSError, ValueError) as e:
        log.warning(f"Ignoring bounce config {BOUNCE_FILE}: {e}")
        return set()


class KeyboardDisconnectRelaySubscriber(Subscriber):
    """Relays Easy-Switch to the remaining devices when the keyboard drops
    without a CHANGE_HOST announcement (firmware that never delivers x1814
    over Bluetooth before the link tears down, e.g. MX Keys for Business 0xB380).

    A disconnect only counts as a switch if the keyboard stays gone for
    RELAY_DELAY_SECONDS (a reconnect or a real x1814 announcement within the
    window cancels the relay), there was local input within
    RELAY_MAX_IDLE_SECONDS, and the machine is on AC power (set
    CLEVERSWITCH_RELAY_ON_BATTERY=1 to relay on battery too). When
    ~/.config/cleverswitch/home_network records router MACs (one per line),
    the relay additionally stays mute on other networks. This keeps
    sleep/wake blips, service restarts, the keyboard's idle power-save drop
    and carrying a laptop out of range from moving the mouse. Powering the
    keyboard off mid-use still triggers one false relay, recovered with the
    mouse's channel button. The 0-based target host comes from
    $CLEVERSWITCH_PEER_HOST or ~/.config/cleverswitch/peer_host; when neither
    is set the relay is off.

    Bounce: a follower listed in ~/.config/cleverswitch/bounce_followers that
    is connected while no keyboard is, for BOUNCE_GRACE_SECONDS, is sent to
    the peer host by itself. This self-heals the strand where a relay (or a
    failed keyboard attach on the other machine) parks the shared mouse on a
    host the keyboard never reached. Only listed wpids are ever bounced.
    """

    def __init__(self, device_registry: LogiDeviceRegistry, topics: Topics):
        self._device_registry = device_registry
        self._topics = topics
        self._peer_host = _configured_peer_host()
        self._bounce_wpids = _bounce_wpids()
        self._timers: dict[int, threading.Timer] = {}
        self._bounce_timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()
        if self._peer_host is None:
            return
        bounce_note = (
            f", bouncing {sorted(hex(w) for w in self._bounce_wpids)} after "
            f"{BOUNCE_GRACE_SECONDS:.0f}s without a keyboard"
            if self._bounce_wpids
            else ""
        )
        log.info(
            f"Keyboard disconnect relay armed: peer host {self._peer_host + 1}, "
            f"delay {RELAY_DELAY_SECONDS}s{bounce_note}"
        )
        topics.hid_event.subscribe(self)

    def notify(self, event) -> None:
        if isinstance(event, HostChangeEvent):
            self._cancel_for_announcement(event)
            return

        if not isinstance(event, DeviceConnectedEvent):
            return

        device = self._device_registry.get_by_wpid(event.wpid)
        if device is None:
            return

        if device.role != "keyboard":
            self._track_follower(event, device)
            return

        with self._lock:
            timer = self._timers.pop(event.wpid, None)
            if timer is not None:
                timer.cancel()

            if event.link_established:
                self._cancel_bounces_locked()
                return

            if not self._followers_connected(event.wpid):
                return

            timer = threading.Timer(RELAY_DELAY_SECONDS, self._relay, args=(event,))
            timer.daemon = True
            self._timers[event.wpid] = timer
            timer.start()

    def _cancel_for_announcement(self, event: HostChangeEvent) -> None:
        """A real x1814 announcement already handled this switch; drop any
        pending relay for the announcing device so it cannot fire twice."""
        for device in self._device_registry.all_entries():
            if device.pid == event.pid and device.slot == event.slot:
                with self._lock:
                    timer = self._timers.pop(device.wpid, None)
                    if timer is not None:
                        timer.cancel()
                return

    def _track_follower(self, event: DeviceConnectedEvent, device) -> None:
        if event.wpid not in self._bounce_wpids:
            return
        with self._lock:
            timer = self._bounce_timers.pop(event.wpid, None)
            if timer is not None:
                timer.cancel()
            if not event.link_established or self._keyboard_connected():
                return
            timer = threading.Timer(BOUNCE_GRACE_SECONDS, self._bounce, args=(event.wpid,))
            timer.daemon = True
            self._bounce_timers[event.wpid] = timer
            timer.start()
        log.info(
            f"'{device.display_name}' connected with no keyboard present; sending it to "
            f"host {self._peer_host + 1} in {BOUNCE_GRACE_SECONDS:.0f}s unless one arrives"
        )

    def _bounce(self, wpid: int) -> None:
        with self._lock:
            self._bounce_timers.pop(wpid, None)
            device = self._device_registry.get_by_wpid(wpid)
            if device is None or not device.connected or self._keyboard_connected():
                return
        change_host_idx = device.available_features.get(FEATURE_CHANGE_HOST)
        if change_host_idx is None:
            return
        log.info(
            f"'{device.display_name}' still has no keyboard with it; "
            f"sending it alone to host {self._peer_host + 1}"
        )
        request_id = (change_host_idx << 8) | (CHANGE_HOST_FN_SET & 0xF0) | SW_ID_HOST_CHANGE
        msg = build_msg(device.slot, request_id, pack_params((self._peer_host,)))
        self._topics.write.publish(WriteEvent(slot=device.slot, pid=device.pid, hid_message=msg))

    def _relay(self, event: DeviceConnectedEvent) -> None:
        with self._lock:
            self._timers.pop(event.wpid, None)
            keyboard = self._device_registry.get_by_wpid(event.wpid)
            if keyboard is None or keyboard.connected or not self._followers_connected(event.wpid):
                return

        idle = _system_idle_seconds()
        if idle is not None and idle > RELAY_MAX_IDLE_SECONDS:
            log.info(
                f"'{keyboard.display_name}' disconnected after {idle:.0f}s without local input; "
                f"idle drop, not relaying"
            )
            return

        if not _on_ac_power():
            log.info(
                f"'{keyboard.display_name}' disconnected while on battery; "
                f"machine likely undocked, not relaying"
            )
            return

        if not _on_home_network():
            log.info(
                f"'{keyboard.display_name}' disconnected away from the home network; "
                f"not relaying"
            )
            return

        # The probes above take time; a reconnect during them must win.
        keyboard = self._device_registry.get_by_wpid(event.wpid)
        if keyboard is None or keyboard.connected:
            return

        log.info(
            f"'{keyboard.display_name}' left without announcement; "
            f"relaying host change -> host {self._peer_host + 1}"
        )
        self._topics.hid_event.publish(
            HostChangeEvent(slot=event.slot, pid=event.pid, target_host=self._peer_host)
        )

    def _followers_connected(self, keyboard_wpid: int) -> bool:
        return any(
            d.connected
            for d in self._device_registry.all_entries()
            if d.wpid != keyboard_wpid and d.role != "keyboard"
        )

    def _keyboard_connected(self) -> bool:
        return any(
            d.connected for d in self._device_registry.all_entries() if d.role == "keyboard"
        )

    def _cancel_bounces_locked(self) -> None:
        for timer in self._bounce_timers.values():
            timer.cancel()
        self._bounce_timers.clear()
