import logging
import os
import threading
from pathlib import Path

from ..event.device_connected_event import DeviceConnectedEvent
from ..event.host_change_event import HostChangeEvent
from ..registry.logi_device_registry import LogiDeviceRegistry
from ..subscriber.subscriber import Subscriber
from ..topic.topics import Topics

log = logging.getLogger(__name__)

PEER_HOST_ENV = "CLEVERSWITCH_PEER_HOST"
PEER_HOST_FILE = Path.home() / ".config" / "cleverswitch" / "peer_host"
RELAY_DELAY_SECONDS = float(os.environ.get("CLEVERSWITCH_RELAY_DELAY", "1.5"))


def _configured_peer_host() -> int | None:
    value = os.environ.get(PEER_HOST_ENV)
    if value is None and PEER_HOST_FILE.is_file():
        value = PEER_HOST_FILE.read_text().strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        log.warning(f"Invalid peer host {value!r}; disconnect relay disabled")
        return None


class KeyboardDisconnectRelaySubscriber(Subscriber):
    """Local patch: relays Easy-Switch to the remaining devices when the keyboard
    drops without a CHANGE_HOST announcement (firmware that never delivers x1814
    over Bluetooth before the link tears down, e.g. MX Keys for Business 0xB380).

    A disconnect only counts as a switch if the keyboard stays gone for
    RELAY_DELAY_SECONDS; a reconnect within the window cancels the relay, so
    sleep/wake blips and service restarts do not move the mouse. Power-off or
    battery death still trigger one false relay, recovered with the mouse's
    channel button. The 0-based target host comes from $CLEVERSWITCH_PEER_HOST
    or ~/.config/cleverswitch/peer_host; when neither is set the relay is off.
    """

    def __init__(self, device_registry: LogiDeviceRegistry, topics: Topics):
        self._device_registry = device_registry
        self._topics = topics
        self._peer_host = _configured_peer_host()
        self._timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()
        if self._peer_host is not None:
            log.info(
                f"Keyboard disconnect relay armed: peer host {self._peer_host + 1}, "
                f"delay {RELAY_DELAY_SECONDS}s"
            )
        topics.hid_event.subscribe(self)

    def notify(self, event) -> None:
        if self._peer_host is None or not isinstance(event, DeviceConnectedEvent):
            return

        device = self._device_registry.get_by_wpid(event.wpid)
        if device is None or device.role != "keyboard":
            return

        with self._lock:
            timer = self._timers.pop(event.wpid, None)
            if timer is not None:
                timer.cancel()

            if event.link_established:
                return

            if not self._other_connected(event.wpid):
                return

            timer = threading.Timer(RELAY_DELAY_SECONDS, self._relay, args=(event,))
            timer.daemon = True
            self._timers[event.wpid] = timer
            timer.start()

    def _relay(self, event: DeviceConnectedEvent) -> None:
        with self._lock:
            self._timers.pop(event.wpid, None)
            keyboard = self._device_registry.get_by_wpid(event.wpid)
            if keyboard is None or keyboard.connected or not self._other_connected(event.wpid):
                return

        log.info(
            f"'{keyboard.display_name}' left without announcement; "
            f"relaying host change -> host {self._peer_host + 1}"
        )
        self._topics.hid_event.publish(
            HostChangeEvent(slot=event.slot, pid=event.pid, target_host=self._peer_host)
        )

    def _other_connected(self, keyboard_wpid: int) -> bool:
        return any(
            d.connected for d in self._device_registry.all_entries() if d.wpid != keyboard_wpid
        )
