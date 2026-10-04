"""
Global hotkey listener using Linux evdev.

Runs in background threads, reading keyboard events from /dev/input
without requiring window focus.

The Super+P combo (Fn+F7 on ThinkBook) is always active and triggers
the display toggle callback.  The brightness and Help keys are only
consumed when the daemon callback reports that it handled them (i.e.
while the eInk display is enabled); otherwise they pass through so the
desktop's own OLED brightness handling keeps working.

The listener grabs each keyboard device exclusively and re-injects every
event it does not consume through a UInput virtual device, so all other
keys reach the desktop normally.

Design notes
------------
* Super is held back for as long as we do not know whether P will
  follow.  As soon as any *other* key arrives (or Super starts
  auto-repeating, i.e. it is being held for a Super+mouse gesture), the
  pending Super-down is injected first so that Super+L, Super+Tab,
  Super+drag etc. all reach the desktop intact.
* Callbacks run on a single worker thread, never on the read loop, so a
  slow callback (USB refresh, EC round-trips) cannot stall keystroke
  forwarding and freeze typing.
* Our own UInput mirrors are skipped when scanning for devices, so a
  second daemon can never grab the first one's forwarder.

Must run as root (same process as the helper daemon).
"""

import queue
import threading
import time

try:
    import evdev
    from evdev import ecodes, UInput
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False


# Keys we care about (single-key hotkeys)
HOTKEYS = {
    'KEY_BRIGHTNESSUP',
    'KEY_BRIGHTNESSDOWN',
    'KEY_HELP',
}

# Keys needed for Super+P combo
COMBO_KEYS = {
    'KEY_LEFTMETA',
    'KEY_P',
}

# Name prefix of the UInput mirrors this module creates
FORWARDER_PREFIX = 'tinta4plusu-fwd-'

# Minimum time between two Super+P toggles
TOGGLE_DEBOUNCE_S = 2.0

KEY_DOWN, KEY_UP, KEY_REPEAT = 1, 0, 2


class GlobalHotkeyListener:
    """Listen for global hotkeys via evdev and fire callbacks.

    Callbacks may return a truthy value to signal that they handled the
    key, in which case the key event is consumed; a falsy return (or
    None) lets the key pass through to the desktop.  ``on_toggle`` is
    always consumed.
    """

    def __init__(self, logger, on_brightness_up=None, on_brightness_down=None,
                 on_refresh=None, on_toggle=None):
        self.logger = logger
        self.on_brightness_up = on_brightness_up
        self.on_brightness_down = on_brightness_down
        self.on_refresh = on_refresh
        self.on_toggle = on_toggle
        self._running = False
        self._threads = []
        self._devices = []
        self._uinputs = []
        # Super state machine (shared across devices; a laptop has one Super)
        self._super_held = False          # physical key is down
        self._super_pending = False       # down event not yet forwarded
        self._super_forwarded = False     # down event was forwarded to the DE
        self._super_used_in_combo = False  # Super+P fired while held
        # Keys whose key-down we swallowed: swallow their repeat/up too
        self._swallowed = set()
        # Debounce toggle to prevent rapid fire
        self._last_toggle_time = 0.0
        # Callback worker
        self._work = queue.Queue()
        self._worker = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start listening on all keyboard devices."""
        if not HAS_EVDEV:
            self.logger.warning("GlobalHotkeyListener: evdev not installed, global hotkeys disabled")
            return

        self._running = True
        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name='hotkey-worker')
        self._worker.start()

        devices = self._find_keyboard_devices()
        if not devices:
            self.logger.warning("GlobalHotkeyListener: no keyboard input devices found")
            return

        for dev in devices:
            # Create a UInput mirror that will re-inject forwarded events
            try:
                ui = UInput.from_device(dev, name=f'{FORWARDER_PREFIX}{dev.name}')
            except Exception as e:
                self.logger.warning(f"GlobalHotkeyListener: could not create UInput for {dev.path}: {e}")
                dev.close()
                continue

            # Grab the physical device so the DE doesn't see raw events
            try:
                dev.grab()
            except Exception as e:
                self.logger.warning(f"GlobalHotkeyListener: could not grab {dev.path}: {e}")
                ui.close()
                dev.close()
                continue

            self._devices.append(dev)
            self._uinputs.append(ui)
            t = threading.Thread(target=self._read_loop, args=(dev, ui), daemon=True,
                                 name=f'hotkey-{dev.path.rsplit("/", 1)[-1]}')
            t.start()
            self._threads.append(t)
            self.logger.info(f"GlobalHotkeyListener: grabbed+forwarding {dev.path} ({dev.name})")

    def stop(self):
        """Stop all listener threads and release devices."""
        self._running = False
        self._work.put(None)
        for dev in self._devices:
            try:
                dev.ungrab()
            except Exception:
                pass
            try:
                dev.close()
            except Exception:
                pass
        for ui in self._uinputs:
            try:
                ui.close()
            except Exception:
                pass
        self._devices.clear()
        self._uinputs.clear()
        self._threads.clear()
        self.logger.info("GlobalHotkeyListener: stopped")

    def _find_keyboard_devices(self):
        """Find input devices that have our hotkeys or combo keys."""
        keyboards = []
        try:
            all_needed = {getattr(ecodes, k, None) for k in HOTKEYS | COMBO_KEYS} - {None}
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                    # Never grab our own (or a previous daemon's) forwarder
                    if dev.name.startswith(FORWARDER_PREFIX):
                        dev.close()
                        continue
                    caps = dev.capabilities(verbose=False)
                    ev_key_caps = caps.get(ecodes.EV_KEY, [])
                    if all_needed & set(ev_key_caps):
                        keyboards.append(dev)
                    else:
                        dev.close()
                except Exception:
                    pass
        except Exception as e:
            self.logger.error(f"GlobalHotkeyListener: error scanning devices: {e}")
        return keyboards

    # ------------------------------------------------------------------
    # Event processing
    # ------------------------------------------------------------------

    def _read_loop(self, dev, ui):
        """Read events from a single device, consuming hotkeys and forwarding the rest."""
        try:
            for event in dev.read_loop():
                if not self._running:
                    break

                # Forward all non-key events (SYN, MSC, etc.) unconditionally
                if event.type != ecodes.EV_KEY:
                    ui.write_event(event)
                    continue

                if not self._handle_key(event, ui):
                    ui.write_event(event)
                    ui.syn()

        except OSError:
            if self._running:
                self.logger.warning(f"GlobalHotkeyListener: device {dev.path} disconnected")
        except Exception as e:
            if self._running:
                self.logger.error(f"GlobalHotkeyListener: read error on {dev.path}: {e}")

    def _handle_key(self, event, ui):
        """Decide what to do with one EV_KEY event.

        Returns True when the event was consumed (must not be forwarded).
        """
        code, value = event.code, event.value

        # --- Super (left meta) ------------------------------------------
        if code == ecodes.KEY_LEFTMETA:
            if value == KEY_DOWN:
                self._super_held = True
                self._super_pending = True
                self._super_forwarded = False
                self._super_used_in_combo = False
                return True  # hold back until we know whether P follows
            if value == KEY_REPEAT:
                # Super is being held on its own (Super+drag, Super+scroll):
                # the DE needs to see it now.
                self._flush_pending_super(ui)
                return not self._super_forwarded
            # KEY_UP
            self._super_held = False
            if self._super_pending and not self._super_used_in_combo:
                # Plain Super tap: forward down+up so the DE opens Activities
                self._flush_pending_super(ui)
            self._super_pending = False
            forwarded = self._super_forwarded
            self._super_forwarded = False
            return not forwarded  # forward the up only if the DE saw the down

        # --- Super+P ----------------------------------------------------
        if code == ecodes.KEY_P and self._super_held:
            if value == KEY_DOWN:
                self._super_used_in_combo = True
                self._super_pending = False
                now = time.monotonic()
                if now - self._last_toggle_time > TOGGLE_DEBOUNCE_S:
                    self._last_toggle_time = now
                    if self.on_toggle:
                        self.logger.info("GlobalHotkeyListener: Super+P toggle detected")
                        self._dispatch(self.on_toggle)
            return True  # swallow down, repeat and up of P while Super is held

        # Any other key while Super is pending: it is a Super+X shortcut,
        # so let the DE see Super first.
        if self._super_pending:
            self._flush_pending_super(ui)

        # --- Single-key hotkeys ----------------------------------------
        if code in self._swallowed:
            if value == KEY_UP:
                self._swallowed.discard(code)
            return True

        if value != KEY_DOWN:
            return False

        callback = None
        if code == ecodes.KEY_BRIGHTNESSUP:
            callback = self.on_brightness_up
        elif code == ecodes.KEY_BRIGHTNESSDOWN:
            callback = self.on_brightness_down
        elif code == getattr(ecodes, 'KEY_HELP', None):
            callback = self.on_refresh

        if callback is None:
            return False

        # The callback decides synchronously whether it owns the key right
        # now (eInk on?) but does the slow hardware work on the worker.
        handled = self._safe_call(callback)
        if handled:
            self._swallowed.add(code)
            return True
        return False

    def _flush_pending_super(self, ui):
        """Inject the held-back Super-down so the DE sees it."""
        if self._super_pending:
            self._super_pending = False
            self._super_forwarded = True
            ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTMETA, KEY_DOWN)
            ui.syn()

    # ------------------------------------------------------------------
    # Callback execution
    # ------------------------------------------------------------------

    def run_async(self, func, *args):
        """Run func(*args) on the hotkey worker thread.

        Also used by the daemon's hotkey callbacks for their slow hardware
        work, so the evdev read loop is never blocked.
        """
        self._work.put((func, args))

    def _dispatch(self, callback):
        """Run a callback on the worker thread (never on the read loop)."""
        self.run_async(callback)

    def _worker_loop(self):
        while True:
            item = self._work.get()
            if item is None:
                return
            func, args = item
            try:
                func(*args)
            except Exception as e:
                self.logger.error(f"GlobalHotkeyListener: worker error: {e}")

    def _safe_call(self, callback):
        """Call a callback, catching exceptions. Returns its result."""
        try:
            return callback()
        except Exception as e:
            self.logger.error(f"GlobalHotkeyListener: callback error: {e}")
            return False
