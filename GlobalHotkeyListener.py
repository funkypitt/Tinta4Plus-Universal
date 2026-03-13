"""
Global hotkey listener using Linux evdev.

Runs in a background thread, reading keyboard events from /dev/input
without requiring window focus.  Only active while the eInk display is
enabled — brightness and refresh keys are ignored in OLED mode.

Must run as root (same process as the helper daemon).
"""

import threading
import logging

try:
    import evdev
    from evdev import ecodes
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False


# Keys we care about
HOTKEYS = {
    'KEY_BRIGHTNESSUP',
    'KEY_BRIGHTNESSDOWN',
    'KEY_HELP',
}


class GlobalHotkeyListener:
    """Listen for global hotkeys via evdev and fire callbacks."""

    def __init__(self, logger, on_brightness_up=None, on_brightness_down=None,
                 on_refresh=None):
        self.logger = logger
        self.on_brightness_up = on_brightness_up
        self.on_brightness_down = on_brightness_down
        self.on_refresh = on_refresh
        self._running = False
        self._threads = []
        self._devices = []

    def start(self):
        """Start listening on all keyboard devices."""
        if not HAS_EVDEV:
            self.logger.warning("GlobalHotkeyListener: evdev not installed, global hotkeys disabled")
            return

        self._running = True
        devices = self._find_keyboard_devices()
        if not devices:
            self.logger.warning("GlobalHotkeyListener: no keyboard input devices found")
            return

        for dev in devices:
            self._devices.append(dev)
            t = threading.Thread(target=self._read_loop, args=(dev,), daemon=True)
            t.start()
            self._threads.append(t)
            self.logger.info(f"GlobalHotkeyListener: listening on {dev.path} ({dev.name})")

    def stop(self):
        """Stop all listener threads."""
        self._running = False
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass
        self._devices.clear()
        self._threads.clear()
        self.logger.info("GlobalHotkeyListener: stopped")

    def _find_keyboard_devices(self):
        """Find input devices that have our hotkeys."""
        keyboards = []
        try:
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                    caps = dev.capabilities(verbose=False)
                    # EV_KEY = 1
                    ev_key_caps = caps.get(ecodes.EV_KEY, [])
                    # Check if this device has any of our hotkeys
                    needed = {getattr(ecodes, k, None) for k in HOTKEYS} - {None}
                    if needed & set(ev_key_caps):
                        keyboards.append(dev)
                    else:
                        dev.close()
                except Exception:
                    pass
        except Exception as e:
            self.logger.error(f"GlobalHotkeyListener: error scanning devices: {e}")
        return keyboards

    def _read_loop(self, dev):
        """Read events from a single device."""
        try:
            for event in dev.read_loop():
                if not self._running:
                    break
                # Only key-down events (value == 1)
                if event.type != ecodes.EV_KEY or event.value != 1:
                    continue

                code = event.code
                if code == ecodes.KEY_BRIGHTNESSUP:
                    if self.on_brightness_up:
                        self._safe_call(self.on_brightness_up)
                elif code == ecodes.KEY_BRIGHTNESSDOWN:
                    if self.on_brightness_down:
                        self._safe_call(self.on_brightness_down)
                elif code == getattr(ecodes, 'KEY_HELP', None):
                    if self.on_refresh:
                        self._safe_call(self.on_refresh)

        except OSError:
            # Device closed or disconnected
            if self._running:
                self.logger.warning(f"GlobalHotkeyListener: device {dev.path} disconnected")
        except Exception as e:
            if self._running:
                self.logger.error(f"GlobalHotkeyListener: read error on {dev.path}: {e}")

    def _safe_call(self, callback):
        """Call a callback, catching exceptions."""
        try:
            callback()
        except Exception as e:
            self.logger.error(f"GlobalHotkeyListener: callback error: {e}")
