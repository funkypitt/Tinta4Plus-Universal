#!/usr/bin/env python3
"""
Copyright (c) 2025 Jon Cox (joncox123). All rights reserved.

WARNING: This software is provided "AS IS", without any warranty of any kind. It may contain bugs or other defects
that result in data loss, corruption, hardware damage or other issues. Use at your own risk.
It may temporarily or permanently render your hardware inoperable.
It may corrupt or damage the Embedded Controller or eInk T-CON controller in your laptop.
The author is not responsible for any damage, data loss or lost productivity caused by use of this software.
By downloading and using this software you agree to these terms and acknowledge the risks involved.
"""

"""
ThinkBook Plus Gen 4 IRU E-Ink Control GUI (tkinter version)
Unprivileged GUI that communicates with privileged helper daemon

No root/sudo required for this GUI
Communicates via Unix socket with helper daemon

Threading model
---------------
* The Tk main loop owns every widget. Background threads (display
  switch worker, keepalive, resume monitor, startup check) never touch
  widgets directly; they go through ``_ui()`` which marshals the call
  onto the main loop with ``root.after``.
* Display switching runs on a worker thread so the window stays
  responsive during the 6-10 s sequence. All display mutations (switch,
  startup check, post-resume check) are serialised by ``_display_lock``.
* ``_eink_on`` is a plain bool mirror of the eInk state that any thread
  may read; the Tk variables are only read on the main thread.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from tkinter import font as tkfont
import subprocess
import sys
import os
import fcntl
import time
import threading
import logging
import logging.handlers
import random
import webbrowser
import json
import io
import glob
import queue
import shutil
from datetime import datetime

try:
    import sv_ttk
    HAS_SV_TTK = True
except ImportError:
    HAS_SV_TTK = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from HelperClient import HelperClient
from DisplayManager import DisplayManager
from ThemeManager import ThemeManager
from ResumeCheck import ResumeCheck


# ----------------------------------------------------------------------
# Look & feel
# ----------------------------------------------------------------------

class Palette:
    """Colours for the few places ttk cannot style for us (log, dots, canvas)."""
    BG = '#1c1c1c'
    CARD = '#2b2b2b'
    TEXT = '#e6e6e6'
    MUTED = '#9b9b9b'
    ACCENT = '#60cdff'
    OK = '#6ccb5f'
    WARN = '#ffb454'
    ERR = '#ff6b6b'
    OFF = '#6e6e6e'


def _base_dir():
    """Directory holding our data files (EULA, privacy images)."""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS  # PyInstaller data dir
    return os.path.dirname(os.path.abspath(__file__))


def discover_privacy_images():
    """Privacy images shipped next to the app: eink-disable<N>.jpg, sorted.

    Discovered at runtime so users can add or remove images without touching
    code (the plain eink-disable.jpg is the README illustration, not a
    privacy image).
    """
    names = [os.path.basename(p) for p in glob.glob(os.path.join(_base_dir(), 'eink-disable*.jpg'))]
    numbered = [n for n in names if n[len('eink-disable'):-len('.jpg')].isdigit()]
    return sorted(numbered, key=lambda n: int(n[len('eink-disable'):-len('.jpg')]))


class Tooltip:
    """Minimal hover tooltip for any widget."""

    DELAY_MS = 600

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._tip = None
        self._after = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self._tip, text=self.text, justify='left',
                         bg='#3a3a3a', fg=Palette.TEXT, relief='flat',
                         padx=8, pady=5, wraplength=320)
        label.pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


class StatusChip(ttk.Frame):
    """A coloured dot followed by a short label, e.g. '● Helper connected'."""

    def __init__(self, parent, text='', color=Palette.OFF):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, width=12, height=12, highlightthickness=0,
                                bg=self._bg_of(parent))
        self.dot = self.canvas.create_oval(2, 2, 10, 10, fill=color, outline=color)
        self.canvas.pack(side='left', padx=(0, 6))
        self.label = ttk.Label(self, text=text)
        self.label.pack(side='left')

    @staticmethod
    def _bg_of(widget):
        try:
            return ttk.Style(widget).lookup('TFrame', 'background') or Palette.BG
        except tk.TclError:
            return Palette.BG

    def set(self, text, color):
        self.label.config(text=text)
        self.canvas.itemconfig(self.dot, fill=color, outline=color)


class FloatingRefreshButton:
    """Floating refresh button window that stays on top while eInk is active."""

    SIZE = 64

    def __init__(self, parent, on_refresh_callback, logger):
        self.parent = parent
        self.on_refresh_callback = on_refresh_callback
        self.logger = logger

        # Drag state
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._is_dragging = False

        self.window = tk.Toplevel(parent)
        self.window.title("")
        self.window.overrideredirect(True)          # no decorations
        self.window.attributes('-topmost', True)
        self.window.attributes('-alpha', 0.8)

        # Left edge, vertically centred
        screen_height = self.window.winfo_screenheight()
        y_position = (screen_height // 2) - self.SIZE // 2
        self.window.geometry(f"{self.SIZE}x{self.SIZE}+0+{y_position}")
        self.window.config(bg=Palette.CARD)

        self.button = tk.Button(
            self.window,
            text="⟳",
            font=('TkDefaultFont', 30),
            command=self._on_click,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            bg=Palette.CARD, fg=Palette.ACCENT,
            activebackground='#3a3a3a', activeforeground=Palette.ACCENT,
            cursor='hand2',
        )
        self.button.pack(fill=tk.BOTH, expand=True)

        self.button.bind("<Enter>", lambda e: self._paint('#3a3a3a'))
        self.button.bind("<Leave>", lambda e: self._paint(Palette.CARD))
        self.button.bind("<ButtonPress-1>", self._on_drag_start)
        self.button.bind("<B1-Motion>", self._on_drag_motion)
        self.button.bind("<ButtonRelease-1>", self._on_drag_release)

        self.logger.info("Floating refresh button created")

    def _paint(self, color):
        self.button.config(bg=color)
        self.window.config(bg=color)

    def _on_click(self):
        """Handle button click (only if not dragging)"""
        if not self._is_dragging:
            self.logger.info("Floating refresh button clicked")
            if self.on_refresh_callback:
                self.on_refresh_callback()

    def _on_drag_start(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._is_dragging = False

    def _on_drag_motion(self, event):
        dx = event.x - self._drag_start_x
        dy = event.y - self._drag_start_y
        if abs(dx) > 3 or abs(dy) > 3:
            self._is_dragging = True
            x = self.window.winfo_x() + dx
            y = self.window.winfo_y() + dy
            self.window.geometry(f"+{x}+{y}")

    def _on_drag_release(self, _event):
        # Reset drag flag after a short delay so the click doesn't fire
        self.window.after(100, self._reset_drag_flag)

    def _reset_drag_flag(self):
        self._is_dragging = False

    def destroy(self):
        """Destroy the floating button window"""
        self.logger.info("Destroying floating refresh button")
        if self.window:
            self.window.destroy()
            self.window = None


class SwitchError(Exception):
    """A display switch could not be completed (already rolled back)."""


GUI_BUS_NAME = 'org.tinta4plusu.Gui'
GUI_OBJECT_PATH = '/org/tinta4plusu/Gui'
GUI_IFACE = 'org.tinta4plusu.Gui'
INDICATOR_BUS_NAME = 'org.tinta4plusu.Indicator'


def _make_dbus_service(app, bus):
    """Export the GUI's control interface on the session bus.

    Created on the thread that runs the GLib main loop (the resume monitor),
    which is where dbus-python dispatches incoming calls; handlers hop to the
    Tk thread via app._ui(). Used by the top-bar indicator and scripts:

        gdbus call --session --dest org.tinta4plusu.Gui \
              --object-path /org/tinta4plusu/Gui --method org.tinta4plusu.Gui.Toggle
    """
    import dbus
    import dbus.service

    class GuiService(dbus.service.Object):
        def __init__(self):
            # Keep our own reference: dbus.service.Object stores *its* bus name
            # in self._name, so a BusName kept there would be overwritten and
            # released by the garbage collector.
            self._owned_bus_name = dbus.service.BusName(GUI_BUS_NAME, bus)
            super().__init__(self._owned_bus_name, GUI_OBJECT_PATH)

        @dbus.service.method(GUI_IFACE, out_signature='a{sv}')
        def GetState(self):
            return app.get_state_dict()

        @dbus.service.method(GUI_IFACE)
        def Toggle(self):
            app._ui(app.on_eink_toggled)

        @dbus.service.method(GUI_IFACE)
        def ReaderMode(self):
            app._ui(app.on_reader_toggled)

        @dbus.service.method(GUI_IFACE)
        def Refresh(self):
            app._ui(app.on_refresh_full)

        @dbus.service.method(GUI_IFACE, in_signature='s')
        def SetMode(self, mode):
            app._ui(app.on_set_reading if str(mode) == 'reading' else app.on_set_dynamic)

        @dbus.service.method(GUI_IFACE, in_signature='i')
        def SetBrightness(self, level):
            app._ui(app.set_brightness_from_remote, int(level))

        @dbus.service.method(GUI_IFACE)
        def Connect(self):
            app._ui(app.connect_from_remote)

        @dbus.service.method(GUI_IFACE)
        def Show(self):
            app._ui(app.show_window)

        @dbus.service.method(GUI_IFACE)
        def Hide(self):
            app._ui(app.hide_window)

        @dbus.service.method(GUI_IFACE)
        def Quit(self):
            app._ui(app.on_closing)

    return GuiService()


# ----------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------

class EInkControlGUI:
    """Main GUI application using tkinter"""

    # Version
    VERSION = "0.2.0"

    # Configuration
    SOCKET_PATH = '/tmp/tinta4plusu.sock'
    KEEPALIVE_INTERVAL = 5.0  # seconds (send keepalive every 5s, watchdog is 60s)
    SOCKET_TIMEOUT = 10.0  # seconds
    CONFIG_DIR = os.path.expanduser("~/.config/Tinta4PlusU")
    SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings")
    LOG_FILE = os.path.join(os.path.expanduser("~/.cache/Tinta4PlusU"), "gui.log")

    # Display names (ThinkBook Plus Gen 4 has eDP-1=OLED, eDP-2=E-Ink)
    DISPLAY_OLED = DisplayManager.OLED_CONNECTOR
    DISPLAY_EINK = DisplayManager.EINK_CONNECTOR

    # E-Ink privacy images (one picked at random when disabling E-Ink);
    # discovered from the data directory at startup.
    # NOTE: must install feh (X11) or imv (Wayland) for this to work!
    EINK_DISABLED_IMAGES = discover_privacy_images()
    PRIVACY_RANDOM = "Random"

    # Theme names used by the optional auto-switch
    THEME_HIGH_CONTRAST = "HighContrast"
    THEME_ADWAITA_DARK = "Adwaita-dark"

    BRIGHTNESS_MAX = 8
    MAX_LOG_LINES = 2000
    # Ignore repeated hotkey toggles this long after a switch finished
    TOGGLE_COOLDOWN_S = 3.0

    # Text size presets: label -> factor applied to the theme's base fonts
    TEXT_SIZES = {'Normal': 1.0, 'Large': 1.2, 'Larger': 1.4}
    # sv_ttk's named fonts and their stock pixel sizes (negative = pixels)
    SV_FONT_SIZES = {
        'SunValleyCaptionFont': 12, 'SunValleyBodyFont': 14, 'SunValleyBodyStrongFont': 14,
        'SunValleyBodyLargeFont': 18, 'SunValleySubtitleFont': 20, 'SunValleyTitleFont': 28,
        'SunValleyTitleLargeFont': 40, 'SunValleyDisplayFont': 68,
    }
    BASE_WINDOW = (660, 900)

    # Tablet reader mode: orientation presets (label -> xrandr rotation)
    READER_ORIENTATIONS = {'Portrait (left)': 'left', 'Portrait (right)': 'right', 'Landscape': 'normal'}
    # GNOME settings that would suspend / auto-rotate while reading with the lid closed
    READER_GSETTINGS = [
        ('org.gnome.settings-daemon.plugins.power', 'lid-close-ac-action', "'nothing'"),
        ('org.gnome.settings-daemon.plugins.power', 'lid-close-battery-action', "'nothing'"),
        ('org.gnome.settings-daemon.peripherals.touchscreen', 'orientation-lock', 'true'),
        # gsd locks the screen on lid close when it does not suspend; with the
        # keyboard under the closed lid that lock cannot be dismissed.
        ('org.gnome.desktop.screensaver', 'lock-enabled', 'false'),
        # If something still locks, the on-screen keyboard makes it dismissable.
        ('org.gnome.desktop.a11y.applications', 'screen-keyboard-enabled', 'true'),
    ]

    DEFAULT_SETTINGS = {
        'display_scale': 1.0,
        'refresh_period': 0,
        'autoswitch_theme': False,
        'flip_countdown': 5,
        'privacy_image': 'random',
        'floating_button': True,
        'text_size': 'Large',
        'reader_rotation': 'left',       # eInk rotation in tablet reader mode
        'reader_lid_open_exits': True,   # opening the lid leaves reader mode (-> OLED)
        'reader_active': False,          # persisted so a restart knows we are reading
        'reader_backup': None,           # gsettings values to restore after reader mode
        'indicator': True,               # start the top-bar indicator with the GUI
        'close_to_indicator': True,      # window close hides when the indicator runs
        'reader_open_app': True,         # tablet reader mode launches eink-reader fullscreen
    }
    READER_APP = 'eink-reader'

    def __init__(self, root, HELPER_SCRIPT, logger, autostart=False, ui_preview=False, start_hidden=False):
        self.HELPER_SCRIPT = HELPER_SCRIPT
        self.logger = logger
        self.root = root
        self.ui_preview = ui_preview
        self.start_hidden = start_hidden
        self._window_visible = not start_hidden
        self._indicator_process = None
        self._dbus_service = None
        self._brightness_level = 4
        self._main_thread = threading.current_thread()
        self._ui_queue = queue.Queue()
        self._destroyed = False
        self.root.title("ThinkBook E-Ink Control")

        # Window size follows the text size; capped to the screen so the
        # title bar stays reachable on GNOME (top bar + decorations).
        settings_early = self.load_settings()
        self.text_size = settings_early['text_size'] if settings_early['text_size'] in self.TEXT_SIZES else 'Large'
        factor = self.TEXT_SIZES[self.text_size]
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        win_w = min(int(self.BASE_WINDOW[0] * factor), screen_w - 100)
        win_h = min(int(self.BASE_WINDOW[1] * factor), screen_h - 120)
        self.root.geometry(f"{win_w}x{win_h}+50+50")
        self.root.minsize(560, 620)

        # Helper client
        self.helper = HelperClient(logger)
        self._keepalive_thread = None
        self._keepalive_stop = threading.Event()
        self.helper_process = None
        self._connection_state = 'disconnected'

        # Managers
        self.display_mgr = DisplayManager(logger)
        self.theme_mgr = ThemeManager(logger)
        self.logger.info(f"Session: {self.display_mgr.session_type}, Desktop: {self.display_mgr.desktop_env}")

        # Timers
        self.brightness_timer = None
        self.refresh_timer = None
        self._countdown_after = None
        self._countdown_remaining = 0

        # Switch state
        self._eink_on = False            # thread-safe mirror of the eInk state
        self._switching = False          # a switch worker is running
        self._display_lock = threading.Lock()
        self._last_switch_done = 0.0
        self._eink_mode = None           # 'dynamic' | 'reading' | None
        self._reader_on = False          # tablet reader mode active (thread-safe mirror)
        self._reader_rotation = 'left'
        self._lid_inhibit_fd = None      # logind handle-lid-switch inhibitor
        self._idle_inhibit_cookie = None # org.freedesktop.ScreenSaver inhibitor
        self._lid_closed = None
        self._lid_reassert_timer = None
        self._closing = False
        self._startup_check_done = False
        self._secure_boot_dialog_shown = False
        self._ec_available = False
        self._tcon_available = None      # unknown until the helper tells us

        # Image viewer process for E-Ink privacy screen
        self.eink_image_process = None

        # Floating refresh button
        self.floating_refresh_button = None

        # Saved state to restore when switching back from eInk
        self.saved_oled_scale = None
        self._sleep_inhibit_fd = None
        self.saved_keyboard_layout = None
        self.saved_dpms_timeouts = None

        # Resume monitor thread
        self._resume_monitor_thread = None
        self._resume_monitor_stop = threading.Event()
        self._glib_loop = None

        # Load settings from file (or use defaults)
        settings = self.load_settings()
        self.display_scale = settings['display_scale']
        self.flip_countdown = int(settings['flip_countdown'])
        self._reader_rotation = settings['reader_rotation'] if settings['reader_rotation'] in DisplayManager.ROTATIONS else 'left'
        self._reader_lid_open_exits = bool(settings['reader_lid_open_exits'])
        self._reader_was_active = bool(settings['reader_active'])
        self._reader_backup = settings['reader_backup'] if isinstance(settings['reader_backup'], dict) else None
        self._indicator_enabled = bool(settings['indicator'])
        self._close_to_indicator = bool(settings['close_to_indicator'])
        self._reader_open_app = bool(settings['reader_open_app'])

        # Build UI
        self._thumbnail_cache = {}
        self.build_ui()

        # Apply loaded settings to UI controls after they're created
        self._set_scale_ui(self.display_scale)
        self.refresh_period_var.set(int(settings['refresh_period']))
        self._update_refresh_period_label()
        self.autoswitch_theme_var.set(bool(settings['autoswitch_theme']))
        self.floating_button_var.set(bool(settings['floating_button']))
        self.text_size_var.set(self.text_size)
        self.reader_orientation_var.set(next((k for k, v in self.READER_ORIENTATIONS.items()
                                              if v == self._reader_rotation), 'Portrait (left)'))
        self.reader_lid_var.set(self._reader_lid_open_exits)
        self.indicator_var.set(self._indicator_enabled)
        self.close_to_indicator_var.set(self._close_to_indicator)
        self.reader_app_var.set(self._reader_open_app)
        self.countdown_var.set(self.flip_countdown)
        self._set_privacy_image_selection(settings['privacy_image'])
        self._apply_display_state()
        self._set_connection_state('disconnected')

        # Window close: hide behind the indicator, or quit
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_request)
        self.root.bind('<Control-q>', lambda e: self.on_closing())
        if self.start_hidden:
            self.root.withdraw()

        # Pump for UI calls coming from worker threads
        self._ui_pump_after = self.root.after(self.UI_QUEUE_POLL_MS, self._drain_ui_queue)

        if self.ui_preview:
            # Design/preview mode: no helper, no display or input changes.
            # --ui-preview=connected / --ui-preview=eink fake the respective state.
            self.update_status("UI preview mode — hardware disabled")
            self.log_message("UI preview mode: helper, display checks and resume monitor are off", level='warning')
            state, _, tab = self.ui_preview.partition(':')
            if tab in ('settings', 'activity'):
                self.notebook.select(0 if tab == 'settings' else 1)
            self.ui_preview = state
            if self.ui_preview in ('connected', 'eink'):
                self._ec_available = True
                self.secureboot_chip.set("Secure Boot: off", Palette.OK)
                self._set_connection_state('connected')
                if self.ui_preview == 'eink':
                    self._eink_mode = 'dynamic'
                    self._set_eink_on(True)
                    self.log_message("✓ E-Ink display enabled")
            return

        # Save the initial keyboard layout so we can restore it after resume
        self.saved_keyboard_layout = self.display_mgr.get_keyboard_layout()
        if self.saved_keyboard_layout:
            self.logger.info(f"Saved initial keyboard layout: {self.saved_keyboard_layout}")

        # Start monitoring for system resume (lid open / wake from suspend);
        # the same thread hosts the D-Bus control service.
        self._start_resume_monitor()
        self._start_layout_watchdog()
        if self._indicator_enabled:
            self.root.after(1500, self._ensure_indicator)

        if autostart:
            # Autostart mode: don't launch helper immediately (avoids password prompt at login)
            self.update_status("Click 'Connect' to start the helper")
            self.log_message("Autostart mode — helper not launched automatically")
            self._check_secure_boot_local()
            # No helper to ask, so the startup check assumes OLED.
            self.root.after(3000, self._schedule_startup_check)
        else:
            # Normal launch: connect to helper after short delay. The startup
            # display check runs once we know the helper's eInk state.
            self.root.after(500, self.initialize_helper)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def load_settings(self):
        """Load settings from configuration file"""
        defaults = dict(self.DEFAULT_SETTINGS)

        if not os.path.exists(self.SETTINGS_FILE):
            self.logger.info("Settings file not found, using defaults")
            return defaults

        try:
            with open(self.SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
            self.logger.info(f"Loaded settings from {self.SETTINGS_FILE}")
            # Merge with defaults to handle missing keys
            for key, value in defaults.items():
                settings.setdefault(key, value)
            return settings
        except Exception as e:
            self.logger.error(f"Failed to load settings: {e}")
            return defaults

    def save_settings(self):
        """Save current settings to configuration file"""
        try:
            os.makedirs(self.CONFIG_DIR, exist_ok=True)
            settings = {
                'display_scale': self.display_scale,
                'refresh_period': int(self.refresh_period_var.get()),
                'autoswitch_theme': bool(self.autoswitch_theme_var.get()),
                'flip_countdown': int(self.flip_countdown),
                'privacy_image': self._privacy_image_setting(),
                'floating_button': bool(self.floating_button_var.get()),
                'text_size': self.text_size,
                'reader_rotation': self._reader_rotation,
                'reader_lid_open_exits': bool(self.reader_lid_var.get()),
                'reader_active': bool(self._reader_on),
                'reader_backup': self._reader_backup,
                'indicator': bool(self.indicator_var.get()),
                'close_to_indicator': bool(self.close_to_indicator_var.get()),
                'reader_open_app': bool(self.reader_app_var.get()),
            }
            tmp = self.SETTINGS_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(settings, f, indent=2)
            os.replace(tmp, self.SETTINGS_FILE)
            self.logger.info(f"Saved settings to {self.SETTINGS_FILE}")
        except Exception as e:
            self.logger.error(f"Failed to save settings: {e}")

    # ------------------------------------------------------------------
    # Sleep inhibitor / DPMS
    # ------------------------------------------------------------------

    def _inhibit_sleep(self):
        """Acquire a sleep/suspend inhibitor via systemd-logind D-Bus.

        Prevents the system from suspending while display switching is in
        progress. Released by _uninhibit_sleep().
        """
        if self._sleep_inhibit_fd is not None:
            return  # already held

        try:
            import dbus
            bus = dbus.SystemBus()
            proxy = bus.get_object('org.freedesktop.login1',
                                   '/org/freedesktop/login1')
            mgr = dbus.Interface(proxy, 'org.freedesktop.login1.Manager')
            fd = mgr.Inhibit('sleep', 'Tinta4PlusU', 'Switching displays', 'block')
            self._sleep_inhibit_fd = fd.take()
            self.logger.info("Acquired sleep inhibitor")
        except Exception as e:
            self.logger.warning(f"Could not acquire sleep inhibitor: {e}")

    def _uninhibit_sleep(self):
        """Release the sleep/suspend inhibitor."""
        if self._sleep_inhibit_fd is not None:
            try:
                os.close(self._sleep_inhibit_fd)
                self.logger.info("Released sleep inhibitor")
            except OSError as e:
                self.logger.warning(f"Error releasing sleep inhibitor: {e}")
            self._sleep_inhibit_fd = None

    def _save_dpms_timeouts(self):
        """Save current DPMS state and timeout values from xset q."""
        try:
            result = subprocess.run(['xset', 'q'], capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return
            dpms_enabled = 'DPMS is Enabled' in result.stdout
            # Parse "Standby: NNN    Suspend: NNN    Off: NNN"
            for line in result.stdout.splitlines():
                line = line.strip()
                if 'Standby:' in line and 'Suspend:' in line and 'Off:' in line:
                    parts = line.split()
                    try:
                        standby = int(parts[parts.index('Standby:') + 1])
                        suspend = int(parts[parts.index('Suspend:') + 1])
                        off = int(parts[parts.index('Off:') + 1])
                        self.saved_dpms_timeouts = (standby, suspend, off, dpms_enabled)
                        self.logger.info(f"Saved DPMS: enabled={dpms_enabled} standby={standby} "
                                         f"suspend={suspend} off={off}")
                        return
                    except (ValueError, IndexError):
                        pass
        except Exception as e:
            self.logger.warning(f"Failed to save DPMS timeouts: {e}")

    def _restore_dpms_timeouts(self):
        """Restore previously saved DPMS state and timeout values."""
        if not self.saved_dpms_timeouts:
            return
        standby, suspend, off, dpms_enabled = self.saved_dpms_timeouts
        try:
            subprocess.run(['xset', '+dpms' if dpms_enabled else '-dpms'],
                           capture_output=True, timeout=5)
            subprocess.run(['xset', 'dpms', str(standby), str(suspend), str(off)],
                           capture_output=True, timeout=5)
            self.logger.info(f"Restored DPMS: enabled={dpms_enabled} standby={standby} "
                             f"suspend={suspend} off={off}")
        except Exception as e:
            self.logger.warning(f"Failed to restore DPMS timeouts: {e}")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def build_ui(self):
        """Build the tkinter user interface"""
        style = ttk.Style()
        if HAS_SV_TTK:
            sv_ttk.set_theme("dark")
        else:
            style.theme_use('clam')
        bg = style.lookup('TFrame', 'background') or Palette.BG
        self.root.configure(background=bg)

        # Derive our fonts from the font the theme actually uses for its
        # widgets. sv_ttk defines pixel-sized named fonts (SunValleyBodyFont is
        # 14 px) and ignores TkDefaultFont, which on a HiDPI Tk scaling renders
        # far larger — mixing the two gives wildly inconsistent text. Tk
        # reports point sizes as positive and pixel sizes as negative numbers,
        # so _derived_font scales the magnitude and keeps the sign.
        if HAS_SV_TTK and 'SunValleyBodyFont' in tkfont.names():
            base = tkfont.nametofont('SunValleyBodyFont')
        else:
            base = tkfont.nametofont('TkDefaultFont')
        self._base_font = base
        self._mono_base = tkfont.Font(family=tkfont.nametofont('TkFixedFont').actual('family'),
                                      size=base.cget('size'))
        self.font_title = base.copy()
        self.font_h2 = base.copy()
        self.font_big = base.copy()
        self.font_small = base.copy()
        self.font_mono = self._mono_base.copy()
        self._apply_text_size(self.text_size)

        style.configure('Title.TLabel', font=self.font_title)
        style.configure('H2.TLabel', font=self.font_h2)
        style.configure('Muted.TLabel', foreground=Palette.MUTED)
        style.configure('Small.TLabel', font=self.font_small, foreground=Palette.MUTED)
        style.configure('Warn.TLabel', foreground=Palette.WARN)
        style.configure('Err.TLabel', foreground=Palette.ERR)
        style.configure('Big.Accent.TButton', font=self.font_big, padding=(16, 12))
        style.configure('Big.TButton', font=self.font_big, padding=(16, 12))
        style.configure('Icon.TButton', padding=(6, 2), width=3)

        card_style = 'Card.TFrame' if HAS_SV_TTK else 'TFrame'

        outer = ttk.Frame(self.root, padding=(16, 12, 16, 8))
        outer.grid(row=0, column=0, sticky='nsew')
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        # ---- Header: title + status chips ----------------------------
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        header.columnconfigure(0, weight=1)

        title_box = ttk.Frame(header)
        title_box.grid(row=0, column=0, sticky='w')
        ttk.Label(title_box, text="Tinta4PlusU", style='Title.TLabel').pack(anchor='w')
        ttk.Label(title_box, text="eInk display control · ThinkBook Plus Gen 4",
                  style='Muted.TLabel').pack(anchor='w')

        chips = ttk.Frame(header)
        chips.grid(row=0, column=1, sticky='e')
        self.helper_chip = StatusChip(chips, "Helper: not connected", Palette.OFF)
        self.helper_chip.pack(anchor='e', pady=(2, 3))
        self.secureboot_chip = StatusChip(chips, "Secure Boot: unknown", Palette.OFF)
        self.secureboot_chip.pack(anchor='e')
        Tooltip(self.helper_chip, "The privileged helper daemon talks to the EC and the eInk T-CON.\n"
                                  "It is started with pkexec (admin password).")
        Tooltip(self.secureboot_chip, "Frontlight control needs raw EC port access, which the kernel\n"
                                      "blocks while Secure Boot is enabled.")

        # ---- Card: Display -------------------------------------------
        display_card = ttk.Frame(outer, style=card_style, padding=14)
        display_card.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        display_card.columnconfigure(0, weight=1)

        state_row = ttk.Frame(display_card)
        state_row.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        state_row.columnconfigure(1, weight=1)
        ttk.Label(state_row, text="Active display", style='H2.TLabel').grid(row=0, column=0, sticky='w')
        self.display_chip = StatusChip(state_row, "OLED", Palette.ACCENT)
        self.display_chip.grid(row=0, column=2, sticky='e')

        self.eink_enabled_var = tk.BooleanVar(value=False)
        self.eink_toggle_btn = ttk.Button(display_card, text="Switch to eInk",
                                          style='Big.Accent.TButton',
                                          command=self.on_eink_toggled)
        self.eink_toggle_btn.grid(row=1, column=0, sticky='ew')
        Tooltip(self.eink_toggle_btn, "Also: Super+P (Fn+F7) anywhere, even with this window closed.\n"
                                      "Press Esc to cancel a running countdown.")

        self.switch_hint = ttk.Label(display_card, text="", style='Small.TLabel', anchor='w',
                                     wraplength=580, justify='left')
        self.switch_hint.grid(row=2, column=0, sticky='ew', pady=(8, 0))

        self.reader_btn = ttk.Button(display_card, text="📖  Tablet reader mode", command=self.on_reader_toggled)
        self.reader_btn.grid(row=4, column=0, sticky='ew', pady=(10, 0))
        Tooltip(self.reader_btn, "Switch to eInk in portrait with reading mode, and keep the laptop awake\n"
                                 "with the lid closed so you can read it like a tablet.\n"
                                 "Also: Super+Shift+P. Opening the lid brings the OLED back.")

        self.connect_btn = ttk.Button(display_card, text="Connect to helper",
                                      command=self.initialize_helper)
        # gridded only while disconnected (see _set_connection_state)

        # ---- Card: eInk controls -------------------------------------
        self.eink_card = ttk.Frame(outer, style=card_style, padding=14)
        self.eink_card.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        self.eink_card.columnconfigure(1, weight=1)

        ttk.Label(self.eink_card, text="eInk", style='H2.TLabel').grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))

        # Row: mode + refresh
        ttk.Label(self.eink_card, text="Mode").grid(row=1, column=0, sticky='w', padx=(0, 12))
        mode_row = ttk.Frame(self.eink_card)
        mode_row.grid(row=1, column=1, sticky='w')
        self.mode_var = tk.StringVar(value='')
        toggle_style = 'Toggle.TButton' if HAS_SV_TTK else 'TButton'
        self.btn_set_dynamic = ttk.Checkbutton(mode_row, text="Dynamic", style=toggle_style,
                                               variable=self.mode_var, onvalue='dynamic', offvalue='',
                                               command=lambda: self._on_mode_clicked('dynamic'))
        self.btn_set_dynamic.pack(side='left', padx=(0, 4))
        self.btn_set_reading = ttk.Checkbutton(mode_row, text="Reading", style=toggle_style,
                                               variable=self.mode_var, onvalue='reading', offvalue='',
                                               command=lambda: self._on_mode_clicked('reading'))
        self.btn_set_reading.pack(side='left')
        Tooltip(self.btn_set_dynamic, "Fast refresh with some ghosting — best for scrolling and typing.")
        Tooltip(self.btn_set_reading, "Slower, high-quality refresh — best for reading static pages.")

        self.btn_refresh = ttk.Button(self.eink_card, text="⟳  Refresh", command=self.on_refresh_full)
        self.btn_refresh.grid(row=1, column=2, sticky='e')
        Tooltip(self.btn_refresh, "Full refresh to clear ghosting. Also: Help key (Fn+F9).")

        # Row: frontlight
        ttk.Label(self.eink_card, text="Frontlight").grid(row=2, column=0, sticky='w', padx=(0, 12), pady=(10, 0))
        light_row = ttk.Frame(self.eink_card)
        light_row.grid(row=2, column=1, columnspan=2, sticky='ew', pady=(10, 0))
        light_row.columnconfigure(1, weight=1)
        self.brightness_var = tk.IntVar(value=4)
        self._brightness_programmatic = False
        self.btn_bright_down = ttk.Button(light_row, text="−", style='Icon.TButton',
                                          command=lambda: self._step_brightness(-1))
        self.btn_bright_down.grid(row=0, column=0)
        self.brightness_scale = ttk.Scale(light_row, from_=0, to=self.BRIGHTNESS_MAX, orient='horizontal',
                                          variable=self.brightness_var, command=self.on_brightness_changed)
        self.brightness_scale.grid(row=0, column=1, sticky='ew', padx=8)
        self.btn_bright_up = ttk.Button(light_row, text="+", style='Icon.TButton',
                                        command=lambda: self._step_brightness(+1))
        self.btn_bright_up.grid(row=0, column=2)
        self.brightness_label = ttk.Label(light_row, text="4 / 8", width=6, anchor='e')
        self.brightness_label.grid(row=0, column=3, padx=(8, 0))
        Tooltip(self.brightness_scale, "0 switches the frontlight off. Also: Fn+F5 / Fn+F6 while eInk is active.")

        self.frontlight_note = ttk.Label(self.eink_card, text="", style='Small.TLabel', wraplength=520, justify='left')
        self.frontlight_note.grid(row=3, column=0, columnspan=3, sticky='w', pady=(6, 0))
        self.frontlight_note.grid_remove()   # shown by _set_frontlight_note when needed

        # Row: auto refresh
        ttk.Label(self.eink_card, text="Auto-refresh").grid(row=4, column=0, sticky='w', padx=(0, 12), pady=(10, 0))
        auto_row = ttk.Frame(self.eink_card)
        auto_row.grid(row=4, column=1, columnspan=2, sticky='ew', pady=(10, 0))
        auto_row.columnconfigure(0, weight=1)
        self.refresh_period_var = tk.IntVar(value=0)
        self.refresh_period_slider = ttk.Scale(auto_row, from_=0, to=60, orient='horizontal',
                                               variable=self.refresh_period_var,
                                               command=self.on_refresh_period_changed)
        self.refresh_period_slider.grid(row=0, column=0, sticky='ew')
        self.refresh_period_label = ttk.Label(auto_row, text="off", width=10, anchor='e')
        self.refresh_period_label.grid(row=0, column=1, padx=(8, 0))
        Tooltip(self.refresh_period_slider, "Run a full refresh automatically every N seconds while eInk is active.")

        # ---- Notebook: Settings / Activity ---------------------------
        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=3, column=0, sticky='nsew')
        outer.rowconfigure(3, weight=1)

        self._build_settings_tab()
        self._build_activity_tab()

        # ---- Footer ---------------------------------------------------
        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky='ew', pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Initializing…")
        self.status_label = ttk.Label(footer, textvariable=self.status_var, style='Small.TLabel', anchor='w')
        self.status_label.grid(row=0, column=0, sticky='ew')
        ttk.Label(footer, text=f"v{self.VERSION}", style='Small.TLabel').grid(row=0, column=1, padx=(8, 10))
        coffee = ttk.Label(footer, text="☕ Support the author", style='Small.TLabel', cursor='hand2')
        coffee.grid(row=0, column=2)
        coffee.bind('<Button-1>', lambda e: self.on_buy_coffee())

        # Keyboard shortcuts (window-local)
        self.root.bind_all('<Help>', lambda e: self.on_refresh_full() if self._eink_on else None)
        self.root.bind_all('<XF86MonBrightnessUp>', self._on_brightness_key_up)
        self.root.bind_all('<XF86MonBrightnessDown>', self._on_brightness_key_down)
        self.root.bind('<Escape>', lambda e: self._cancel_countdown(by_user=True))

        self.log_message("Application started")

    @staticmethod
    def _scaled_size(size, factor):
        """Scale a Tk font size, keeping its unit (negative = pixels)."""
        size = size or 10
        new_size = max(6, round(abs(size) * factor))
        return -new_size if size < 0 else new_size

    def _apply_text_size(self, name):
        """Resize every font in the app (theme fonts + ours) live."""
        factor = self.TEXT_SIZES.get(name, 1.2)
        self.text_size = name
        if HAS_SV_TTK:
            # ttk widgets use the theme's named fonts, so scaling those
            # rescales every button, label, tab and entry at once.
            for font_name, px in self.SV_FONT_SIZES.items():
                if font_name in tkfont.names():
                    tkfont.nametofont(font_name).configure(size=-round(px * factor))
            base_size = -round(14 * factor)
        else:
            base_size = self._scaled_size(tkfont.nametofont('TkDefaultFont').cget('size'), factor)
        self._mono_base.configure(size=base_size)
        for font, ratio, weight in ((self.font_title, 2.0, 'bold'), (self.font_h2, 1.3, 'bold'),
                                    (self.font_big, 1.3, 'bold'), (self.font_small, 0.86, 'normal')):
            font.configure(size=self._scaled_size(base_size, ratio), weight=weight)
        self.font_mono.configure(size=self._scaled_size(base_size, 0.93))
        if hasattr(self, 'privacy_preview'):
            self._update_privacy_preview()

    def _build_settings_tab(self):
        holder = ttk.Frame(self.notebook)
        self.notebook.add(holder, text="  Settings  ")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        bg = ttk.Style().lookup('TFrame', 'background') or Palette.BG
        canvas = tk.Canvas(holder, highlightthickness=0, bd=0, bg=bg)
        canvas.grid(row=0, column=0, sticky='nsew')
        vbar = ttk.Scrollbar(holder, orient='vertical', command=canvas.yview)
        vbar.grid(row=0, column=1, sticky='ns')
        canvas.configure(yscrollcommand=vbar.set)
        tab = ttk.Frame(canvas, padding=14)
        window = canvas.create_window((0, 0), window=tab, anchor='nw')

        def _resize(_e=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
            canvas.itemconfigure(window, width=canvas.winfo_width())
        tab.bind('<Configure>', _resize)
        canvas.bind('<Configure>', _resize)

        def _wheel(e):
            if e.num == 4 or e.delta > 0:
                canvas.yview_scroll(-1, 'units')
            elif e.num == 5 or e.delta < 0:
                canvas.yview_scroll(1, 'units')
        for seq in ('<Button-4>', '<Button-5>', '<MouseWheel>'):
            canvas.bind(seq, _wheel)
        self._settings_wheel = (tab, _wheel)

        tab.columnconfigure(1, weight=1)
        row = 0

        # eInk scale
        ttk.Label(tab, text="eInk scale").grid(row=row, column=0, sticky='w', padx=(0, 12))
        scale_row = ttk.Frame(tab)
        scale_row.grid(row=row, column=1, sticky='ew')
        scale_row.columnconfigure(0, weight=1)
        self.scale_var = tk.DoubleVar(value=1.0)
        self.scale_slider = ttk.Scale(scale_row, from_=1.0, to=2.0, orient='horizontal',
                                      variable=self.scale_var, command=self.on_scale_changed)
        self.scale_slider.grid(row=0, column=0, sticky='ew')
        self.scale_label = ttk.Label(scale_row, text="1.00×", width=7, anchor='e')
        self.scale_label.grid(row=0, column=1, padx=(8, 0))
        Tooltip(self.scale_slider, "UI scale on the eInk panel (1.00–2.00). Applied on the next switch to eInk.")
        row += 1
        ttk.Label(tab, text="Applied the next time you switch to eInk.", style='Small.TLabel').grid(
            row=row, column=1, sticky='w', pady=(2, 10))
        row += 1

        # Countdown
        ttk.Label(tab, text="Flip countdown").grid(row=row, column=0, sticky='w', padx=(0, 12))
        cd_row = ttk.Frame(tab)
        cd_row.grid(row=row, column=1, sticky='w')
        self.countdown_var = tk.IntVar(value=5)
        self.countdown_spin = ttk.Spinbox(cd_row, from_=0, to=15, width=4, textvariable=self.countdown_var,
                                          command=self.on_countdown_changed)
        self.countdown_spin.pack(side='left')
        self.countdown_spin.bind('<FocusOut>', lambda e: self.on_countdown_changed())
        self.countdown_spin.bind('<Return>', lambda e: self.on_countdown_changed())
        ttk.Label(cd_row, text="seconds to flip the lid before the switch (0 = immediately)",
                  style='Small.TLabel').pack(side='left', padx=(8, 0))
        row += 1

        # Privacy image
        ttk.Label(tab, text="Privacy image").grid(row=row, column=0, sticky='w', padx=(0, 12), pady=(12, 0))
        img_row = ttk.Frame(tab)
        img_row.grid(row=row, column=1, sticky='ew', pady=(12, 0))
        self.privacy_image_var = tk.StringVar(value=self.PRIVACY_RANDOM)
        self.privacy_image_combo = ttk.Combobox(img_row, textvariable=self.privacy_image_var, width=22,
                                                values=[self.PRIVACY_RANDOM] + list(self.EINK_DISABLED_IMAGES),
                                                state="readonly")
        self.privacy_image_combo.pack(side='left')
        self.privacy_image_combo.bind("<<ComboboxSelected>>", self.on_privacy_image_changed)
        self.privacy_preview = tk.Label(img_row, bg=Palette.CARD, bd=0)
        self.privacy_preview.pack(side='left', padx=(12, 0))
        Tooltip(self.privacy_image_combo, "Shown full-screen on the eInk just before it is powered off,\n"
                                          "so the panel does not keep your desktop as its last frame.")
        row += 1
        ttk.Label(tab, text="Left on the eInk panel when it is switched off.", style='Small.TLabel').grid(
            row=row, column=1, sticky='w', pady=(2, 10))
        row += 1

        # Switches
        switch_style = 'Switch.TCheckbutton' if HAS_SV_TTK else 'TCheckbutton'
        self.autoswitch_theme_var = tk.BooleanVar(value=False)
        self.autoswitch_theme_checkbox = ttk.Checkbutton(
            tab, text="High-contrast theme while eInk is active", style=switch_style,
            variable=self.autoswitch_theme_var, command=self.on_autoswitch_theme_changed)
        self.autoswitch_theme_checkbox.grid(row=row, column=0, columnspan=2, sticky='w', pady=(4, 0))
        Tooltip(self.autoswitch_theme_checkbox,
                f"Switch the desktop theme to {self.THEME_HIGH_CONTRAST} on eInk and back to "
                f"{self.THEME_ADWAITA_DARK} on OLED.")
        row += 1

        self.floating_button_var = tk.BooleanVar(value=True)
        self.floating_button_checkbox = ttk.Checkbutton(
            tab, text="Floating refresh button while eInk is active", style=switch_style,
            variable=self.floating_button_var, command=self.on_floating_button_changed)
        self.floating_button_checkbox.grid(row=row, column=0, columnspan=2, sticky='w', pady=(4, 0))
        Tooltip(self.floating_button_checkbox, "A small always-on-top ⟳ button you can drag anywhere (X11 only).")
        row += 1

        # Tablet reader mode
        ttk.Separator(tab).grid(row=row, column=0, columnspan=2, sticky='ew', pady=12)
        row += 1
        ttk.Label(tab, text="Tablet reader mode", style='H2.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        ttk.Label(tab, text="Orientation").grid(row=row, column=0, sticky='w', padx=(0, 12), pady=(8, 0))
        self.reader_orientation_var = tk.StringVar(value='Portrait (left)')
        self.reader_orientation_combo = ttk.Combobox(tab, textvariable=self.reader_orientation_var, width=16,
                                                     values=list(self.READER_ORIENTATIONS), state='readonly')
        self.reader_orientation_combo.grid(row=row, column=1, sticky='w', pady=(8, 0))
        self.reader_orientation_combo.bind('<<ComboboxSelected>>', self.on_reader_orientation_changed)
        Tooltip(self.reader_orientation_combo, "How the eInk is rotated while reading. Touch and pen follow the rotation.")
        row += 1
        self.reader_lid_var = tk.BooleanVar(value=True)
        self.reader_lid_checkbox = ttk.Checkbutton(
            tab, text="Opening the lid leaves reader mode (back to OLED)", style=switch_style,
            variable=self.reader_lid_var, command=self.save_settings)
        self.reader_lid_checkbox.grid(row=row, column=0, columnspan=2, sticky='w', pady=(6, 0))
        row += 1
        self.reader_app_var = tk.BooleanVar(value=True)
        self.reader_app_checkbox = ttk.Checkbutton(
            tab, text="Open the eInk Reader fullscreen when entering reader mode", style=switch_style,
            variable=self.reader_app_var, command=self.save_settings)
        self.reader_app_checkbox.grid(row=row, column=0, columnspan=2, sticky='w', pady=(6, 0))
        Tooltip(self.reader_app_checkbox, "Launches `eink-reader --fullscreen` (your last book) once the eInk is ready.\n"
                                         "Needs the eInk Reader installed.")
        row += 1
        ttk.Label(tab, text="While reading, closing the lid does not suspend and the screen does not blank; "
                            "both are restored when you leave reader mode.",
                  style='Small.TLabel', wraplength=560, justify='left').grid(row=row, column=0, columnspan=2, sticky='w', pady=(4, 0))
        row += 1
        ttk.Separator(tab).grid(row=row, column=0, columnspan=2, sticky='ew', pady=12)
        row += 1

        # Indicator
        self.indicator_var = tk.BooleanVar(value=True)
        self.indicator_checkbox = ttk.Checkbutton(
            tab, text="Show an indicator in the top bar", style=switch_style,
            variable=self.indicator_var, command=self.on_indicator_changed)
        self.indicator_checkbox.grid(row=row, column=0, columnspan=2, sticky='w', pady=(0, 0))
        Tooltip(self.indicator_checkbox, "Switch display, reader mode, refresh, eInk mode and frontlight from the panel.\n"
                                        "Needs the AppIndicator extension on GNOME (enabled by default on Ubuntu).")
        row += 1
        self.close_to_indicator_var = tk.BooleanVar(value=True)
        self.close_to_indicator_checkbox = ttk.Checkbutton(
            tab, text="Closing the window keeps running in the top bar", style=switch_style,
            variable=self.close_to_indicator_var, command=self.save_settings)
        self.close_to_indicator_checkbox.grid(row=row, column=0, columnspan=2, sticky='w', pady=(4, 0))
        Tooltip(self.close_to_indicator_checkbox, "Ctrl+Q or the indicator's Quit exits for real.")
        row += 1
        ttk.Separator(tab).grid(row=row, column=0, columnspan=2, sticky='ew', pady=12)
        row += 1

        # Text size
        ttk.Label(tab, text="Text size").grid(row=row, column=0, sticky='w', padx=(0, 12), pady=(0, 0))
        size_row = ttk.Frame(tab)
        size_row.grid(row=row, column=1, sticky='w', pady=(12, 0))
        self.text_size_var = tk.StringVar(value='Large')
        self.text_size_combo = ttk.Combobox(size_row, textvariable=self.text_size_var, width=10,
                                            values=list(self.TEXT_SIZES), state='readonly')
        self.text_size_combo.pack(side='left')
        self.text_size_combo.bind('<<ComboboxSelected>>', self.on_text_size_changed)
        ttk.Label(size_row, text="applies immediately", style='Small.TLabel').pack(side='left', padx=(8, 0))
        row += 1

        # Shortcuts reference
        ttk.Separator(tab).grid(row=row, column=0, columnspan=2, sticky='ew', pady=12)
        row += 1
        ttk.Label(tab, text="Keyboard shortcuts", style='H2.TLabel').grid(row=row, column=0, columnspan=2, sticky='w')
        row += 1
        shortcuts = [
            ("Super+P  (Fn+F7)", "Switch between OLED and eInk — works system-wide while the helper runs"),
            ("Super+Shift+P", "Tablet reader mode on / off"),
            ("Help  (Fn+F9)", "Full eInk refresh"),
            ("Fn+F5 / Fn+F6", "Frontlight down / up (eInk only; OLED brightness otherwise)"),
            ("Esc", "Cancel a running countdown (this window)"),
            ("Ctrl+Q", "Quit (closing the window only hides it while the indicator runs)"),
        ]
        for keys, what in shortcuts:
            ttk.Label(tab, text=keys, font=self.font_mono).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=(3, 0))
            ttk.Label(tab, text=what, style='Muted.TLabel', wraplength=400, justify='left').grid(
                row=row, column=1, sticky='w', pady=(3, 0))
            row += 1

        # Mouse wheel anywhere over the tab scrolls it (child widgets would
        # otherwise swallow the event); value widgets keep their own wheel.
        _tab, _wheel = self._settings_wheel
        stack = [_tab]
        while stack:
            w = stack.pop()
            if not isinstance(w, (ttk.Scale, ttk.Spinbox, ttk.Combobox)):
                for seq in ('<Button-4>', '<Button-5>', '<MouseWheel>'):
                    w.bind(seq, _wheel, add='+')
            stack.extend(w.winfo_children())

    def _build_activity_tab(self):
        tab = ttk.Frame(self.notebook, padding=(8, 8, 8, 8))
        self.notebook.add(tab, text="  Activity  ")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        log_kwargs = {'wrap': tk.WORD, 'state': tk.DISABLED, 'font': self.font_mono,
                      'bd': 0, 'highlightthickness': 0, 'padx': 8, 'pady': 6}
        if HAS_SV_TTK:
            log_kwargs.update({'bg': Palette.BG, 'fg': Palette.TEXT, 'insertbackground': Palette.TEXT})
        log_box = ttk.Frame(tab)
        log_box.grid(row=0, column=0, sticky='nsew')
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, **log_kwargs)
        self.log_text.grid(row=0, column=0, sticky='nsew')
        log_bar = ttk.Scrollbar(log_box, orient='vertical', command=self.log_text.yview)
        log_bar.grid(row=0, column=1, sticky='ns')
        self.log_text.configure(yscrollcommand=log_bar.set)
        self.log_text.tag_config('time', foreground=Palette.MUTED)
        self.log_text.tag_config('success', foreground=Palette.OK)
        self.log_text.tag_config('error', foreground=Palette.ERR)
        self.log_text.tag_config('warning', foreground=Palette.WARN)
        self.log_text.tag_config('info', foreground=Palette.TEXT)

        buttons = ttk.Frame(tab)
        buttons.grid(row=1, column=0, sticky='ew', pady=(6, 0))
        ttk.Button(buttons, text="Clear", command=self._clear_log).pack(side='left')
        ttk.Button(buttons, text="Copy", command=self._copy_log).pack(side='left', padx=(6, 0))
        ttk.Button(buttons, text="Open log file", command=self._open_log_file).pack(side='left', padx=(6, 0))
        ttk.Label(buttons, text=self.LOG_FILE, style='Small.TLabel').pack(side='right')

    # ------------------------------------------------------------------
    # Thread-safe UI plumbing
    # ------------------------------------------------------------------

    def _on_main_thread(self):
        return threading.current_thread() is self._main_thread

    UI_QUEUE_POLL_MS = 40

    def _ui(self, fn, *args):
        """Run fn(*args) on the Tk main thread (immediately if already there).

        Worker threads never call into Tk themselves — not even root.after():
        Tkinter marshals such calls to the main thread and blocks the caller
        until the event loop services them, which stalls the worker whenever
        the main loop is busy. Instead the call is queued and the main loop
        drains the queue on a timer.
        """
        if self._on_main_thread():
            fn(*args)
        else:
            self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        """Main thread: run queued cross-thread UI calls."""
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except tk.TclError as e:
                    self.logger.debug(f"UI call after teardown ignored: {e}")
                except Exception as e:
                    self.logger.error(f"UI callback error: {e}")
        except queue.Empty:
            pass
        if not self._destroyed:
            self._ui_pump_after = self.root.after(self.UI_QUEUE_POLL_MS, self._drain_ui_queue)

    def log_message(self, message, level='info'):
        """Add a message to the activity log (any thread) and to the logger."""
        # Classify by content so ✓/✗ lines colour correctly even at level='info'
        lower = message.lower()
        if '✓' in message or 'success' in lower:
            tag, logger_level = 'success', 'info'
        elif '✗' in message or 'error' in lower or 'failed' in lower:
            tag, logger_level = 'error', 'error'
        elif '⚠' in message or level == 'warning':
            tag, logger_level = 'warning', 'warning'
        else:
            tag, logger_level = level, level

        getattr(self.logger, logger_level if logger_level in ('info', 'warning', 'error') else 'info')(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._ui(self._append_log, timestamp, message, tag)

    def _append_log(self, timestamp, message, tag):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{timestamp}  ", 'time')
        self.log_text.insert(tk.END, f"{message}\n", tag)
        # Keep the widget bounded
        lines = int(self.log_text.index('end-1c').split('.')[0])
        if lines > self.MAX_LOG_LINES:
            self.log_text.delete('1.0', f"{lines - self.MAX_LOG_LINES}.0")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete('1.0', tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _copy_log(self):
        text = self.log_text.get('1.0', tk.END)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.update_status("Activity log copied to clipboard")

    def _open_log_file(self):
        try:
            subprocess.Popen(['xdg-open', self.LOG_FILE],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            self.log_message(f"Could not open log file: {e}", level='error')

    def update_status(self, message, error=False):
        """Update the footer status line (any thread)."""
        self._ui(self._set_status, message, error)

    def _set_status(self, message, error):
        self.status_var.set(message)
        self.status_label.configure(style='Err.TLabel' if error else 'Small.TLabel')

    def show_error_dialog(self, message):
        """Log an error (non-blocking; no modal dialogs during switches)."""
        self.log_message(f"ERROR: {message}", level='error')

    def show_info_dialog(self, message):
        self.log_message(message)

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------

    def _set_connection_state(self, state):
        """state: 'connected' | 'connecting' | 'disconnected'"""
        self._connection_state = state
        if state == 'connected':
            self.helper_chip.set("Helper: connected", Palette.OK)
            self.connect_btn.grid_remove()
        elif state == 'connecting':
            self.helper_chip.set("Helper: starting…", Palette.WARN)
            self.connect_btn.grid_remove()
        else:
            self.helper_chip.set("Helper: not connected", Palette.ERR)
            self.connect_btn.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        self._apply_display_state()

    def _set_eink_on(self, on):
        """Record the eInk state (main thread) and refresh the UI."""
        self._eink_on = bool(on)
        self.eink_enabled_var.set(self._eink_on)
        self._apply_display_state()

    def _apply_display_state(self):
        """Refresh every control that depends on eInk/connection/busy state."""
        on = self._eink_on
        connected = self._connection_state == 'connected'
        busy = self._switching or self._countdown_after is not None

        # Active display indicator + title
        if on and self._reader_on:
            self.display_chip.set("eInk · reader", Palette.OK)
            self.root.title("ThinkBook E-Ink Control — reader mode")
        elif on:
            self.display_chip.set("eInk", Palette.OK)
            self.root.title("ThinkBook E-Ink Control — eInk")
        else:
            self.display_chip.set("OLED", Palette.ACCENT)
            self.root.title("ThinkBook E-Ink Control")

        # Big button
        if self._countdown_after is not None:
            self.eink_toggle_btn.config(text=f"Flip the lid now… {self._countdown_remaining}   (Esc to cancel)",
                                        style='Big.TButton', state='normal')
        elif self._switching:
            self.eink_toggle_btn.config(text="Switching…", style='Big.TButton', state='disabled')
        else:
            self.eink_toggle_btn.config(text="Switch to OLED" if on else "Switch to eInk",
                                        style='Big.Accent.TButton',
                                        state='normal' if connected else 'disabled')

        if not connected and not busy:
            self.switch_hint.config(text="Connect to the helper to switch displays.")
        elif busy:
            pass  # hint is driven by the countdown / switch sequence
        elif on and self._reader_on:
            self.switch_hint.config(text="Reader mode: close the lid and read. Open it (or press the button) to come back.")
        elif on:
            self.switch_hint.config(text="Flip the lid back to the OLED side before switching.")
        else:
            self.switch_hint.config(text="You will have a few seconds to flip the lid to the eInk side.")

        self.reader_btn.config(
            text="📖  Leave tablet reader mode" if self._reader_on else "📖  Tablet reader mode",
            state='normal' if (connected and not busy) else 'disabled')

        # eInk card
        eink_ctl = 'normal' if (on and connected and not busy) else 'disabled'
        for w in (self.btn_set_dynamic, self.btn_set_reading, self.btn_refresh):
            w.config(state=eink_ctl)
        light_ctl = 'normal' if (on and connected and not busy and self._ec_available) else 'disabled'
        for w in (self.brightness_scale, self.btn_bright_down, self.btn_bright_up):
            w.config(state=light_ctl)
        self.refresh_period_slider.config(state='normal' if not busy else 'disabled')
        self.mode_var.set(self._eink_mode or '')

    def _set_busy_hint(self, text):
        self._ui(self.switch_hint.config, {'text': text})

    # ------------------------------------------------------------------
    # Helper connection
    # ------------------------------------------------------------------

    def initialize_helper(self):
        """Connect to a running helper daemon, or launch one."""
        self._connect_or_launch(first_time=True)

    def attempt_helper_restart(self):
        """Reconnect after the keepalive noticed the helper went away."""
        self.stop_keepalive()
        self.log_message("Attempting to reconnect to helper daemon...")
        self._connect_or_launch(first_time=False)

    def _connect_or_launch(self, first_time):
        self._set_connection_state('connecting')

        # First try to connect to an existing helper
        if os.path.exists(self.SOCKET_PATH):
            if self.helper.connect(self.SOCKET_PATH, timeout=self.SOCKET_TIMEOUT):
                self.log_message("✓ Connected to existing helper daemon")
                self._on_helper_ready()
                return
            # connect() logs why; a stale socket is cleaned up by the daemon itself

        # No usable helper, launch it
        self.log_message("Helper daemon not running, launching (admin password may be required)...")
        self.update_status("Launching helper daemon (password required)...")
        threading.Thread(target=self._launch_helper_thread, daemon=True, name='helper-launch').start()

    def _launch_helper_thread(self):
        """Launch helper daemon in background thread"""
        try:
            helper_path = self.HELPER_SCRIPT

            if not os.path.exists(helper_path):
                self._ui(self._helper_launch_failed, "Helper not found: " + helper_path)
                return

            # If helper is a compiled binary, run it directly via pkexec
            # If it's a .py script, invoke via python3
            if helper_path.endswith('.py'):
                cmd = ['pkexec', 'python3', helper_path]
            else:
                cmd = ['pkexec', helper_path]

            # Reap a previous launch so it does not linger as a zombie
            if self.helper_process is not None and self.helper_process.poll() is not None:
                self.helper_process = None

            self.helper_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Wait for helper to start (longer at login when polkit agent may not be ready)
            time.sleep(2.0)

            # Try to connect — up to 20 attempts (≈12s total after initial wait)
            for _attempt in range(20):
                # Check if pkexec process died (user cancelled password, etc.)
                if self.helper_process.poll() is not None:
                    rc = self.helper_process.returncode
                    self._ui(self._helper_launch_failed,
                             f"Helper process exited (code {rc}) — password cancelled or pkexec failed")
                    return
                if os.path.exists(self.SOCKET_PATH):
                    # Quiet: the path may still be the previous daemon's stale socket
                    if self.helper.connect(self.SOCKET_PATH, timeout=self.SOCKET_TIMEOUT, quiet=True):
                        self._ui(self._helper_launch_success)
                        return
                time.sleep(0.5)

            self._ui(self._helper_launch_failed, "Helper started but socket not available after 12s")

        except Exception as e:
            self._ui(self._helper_launch_failed, str(e))

    def _helper_launch_success(self):
        """Called on the main thread when the helper was launched by us."""
        self.log_message("✓ Helper daemon launched")
        self._on_helper_ready()

    def _helper_launch_failed(self, error):
        """Called on the main thread when helper launch failed"""
        self.update_status(f"Helper not available — {error}", error=True)
        self.log_message(f"ERROR: Failed to launch helper - {error}", level='error')
        self._set_connection_state('disconnected')
        # Update Secure Boot indicator locally (since we can't ask the helper)
        self._check_secure_boot_local()
        # No helper to ask about the eInk, so the startup check assumes OLED
        self._schedule_startup_check()

    def _on_helper_ready(self):
        """Common path after connecting to a helper (new or existing)."""
        self.update_status("Connected to helper daemon")
        self._set_connection_state('connected')
        self.start_keepalive()
        self.root.after(300, self._after_connect)

    def _after_connect(self):
        self.check_ec_status()
        self._sync_state_from_helper()
        self._schedule_startup_check()

    def _sync_state_from_helper(self):
        """Seed our eInk/brightness state from the daemon.

        The daemon outlives GUI restarts, so it — not a fresh BooleanVar —
        knows whether the eInk is currently powered.
        """
        try:
            response = self.helper.send_command('get-state')
        except Exception as e:
            self.log_message(f"Could not read helper state: {e}", level='error')
            return
        if not response or not response.get('success'):
            return

        self._tcon_available = response.get('tcon_available')
        eink_on = bool(response.get('eink_enabled'))
        if eink_on != self._eink_on:
            self.log_message(f"Helper reports eInk {'enabled' if eink_on else 'disabled'} — syncing")
        self._set_eink_on(eink_on)
        if eink_on:
            self._eink_mode = None  # unknown; the user can re-apply
            if self.floating_button_var.get():
                self._ensure_floating_button(True)
            self._start_refresh_timer()
            if self._reader_was_active:
                # We were reading when the GUI last ran: pick the mode back up
                # (inhibitors are per-process and must be re-acquired).
                self.log_message("Resuming tablet reader mode from the previous session")
                self._reader_on = True
                self._acquire_reader_inhibitors()
                self._apply_display_state()
        elif self._reader_was_active or self._reader_backup:
            # Reader mode ended without us (crash / power loss): undo its side effects
            self._reader_on = False
            self._restore_reader_system_prefs()
            self.save_settings()
        self._reader_was_active = False

        level = response.get('brightness_level')
        if level is not None:
            self._set_brightness_ui(int(level))

        if self._tcon_available is False:
            self.log_message("⚠ eInk T-CON (USB) not detected by the helper — eInk switching will fail "
                             "until it is", level='warning')

    def _check_secure_boot_local(self):
        """Check Secure Boot status locally via mokutil (no helper needed)"""
        try:
            result = subprocess.run(['mokutil', '--sb-state'],
                                    capture_output=True, text=True, timeout=2)
            if 'SecureBoot enabled' in result.stdout:
                self.secureboot_chip.set("Secure Boot: on", Palette.ERR)
            else:
                self.secureboot_chip.set("Secure Boot: off", Palette.OK)
        except Exception as e:
            self.logger.warning(f"Could not check Secure Boot locally: {e}")

    def start_keepalive(self):
        """Start periodic keepalive messages in a background thread.

        A dedicated thread (not root.after) keeps the daemon's watchdog fed
        even while the main loop is busy, and across suspend/resume.
        """
        self.stop_keepalive()
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True, name='keepalive')
        self._keepalive_thread.start()
        self.logger.info(f"Started keepalive thread ({self.KEEPALIVE_INTERVAL}s interval)")

    def stop_keepalive(self):
        """Stop the keepalive thread."""
        self._keepalive_stop.set()
        thread = self._keepalive_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._keepalive_thread = None

    def _keepalive_loop(self):
        """Background thread that sends keepalives at regular intervals."""
        while not self._keepalive_stop.wait(self.KEEPALIVE_INTERVAL):
            if not self.helper.is_connected():
                self._ui(self._on_keepalive_lost, "Helper disconnected")
                return
            try:
                response = self.helper.send_command('keepalive')
                if not response or not response.get('success'):
                    self._ui(self._on_keepalive_lost, "Keepalive failed")
                    return
                notifs = response.get('notifications', [])
                if notifs:
                    self._ui(self._process_notifications, notifs)
            except Exception as e:
                self.logger.error(f"Keepalive error: {e}")
                self._ui(self._on_keepalive_lost, f"Lost connection to helper - {e}")
                return

    def _on_keepalive_lost(self, reason):
        """Called on the main thread when keepalive detects a disconnect."""
        if self._closing:
            return
        self.update_status("Helper disconnected — reconnecting…", error=True)
        self.log_message(f"ERROR: {reason}", level='error')
        self._set_connection_state('disconnected')
        self.attempt_helper_restart()

    def _process_notifications(self, notifs):
        """Process hotkey notifications on the main thread (toggles coalesced)."""
        toggle_requested = False
        reader_requested = False
        for notif in notifs:
            ntype = notif.get('type')
            if ntype == 'reader':
                reader_requested = True
                continue
            if ntype == 'brightness':
                level = notif.get('level')
                if level is not None:
                    self._set_brightness_ui(int(level))
                    self.log_message(f"Hotkey: brightness set to {level}")
            elif ntype == 'refresh':
                self.log_message("Hotkey: eInk refresh performed")
            elif ntype == 'toggle':
                toggle_requested = True

        if toggle_requested:
            self.log_message("Hotkey: Super+P toggle requested")
            since_last = time.monotonic() - self._last_switch_done
            if self._switching or self._countdown_after is not None:
                self.log_message("Ignoring toggle: a switch is already in progress", level='warning')
            elif since_last < self.TOGGLE_COOLDOWN_S:
                self.log_message("Ignoring toggle: a switch just finished", level='warning')
            else:
                self.on_eink_toggled(skip_countdown=True)
        elif reader_requested:
            self.log_message("Hotkey: Super+Shift+P reader mode requested")
            if self._switching or self._countdown_after is not None:
                self.log_message("Ignoring: a switch is already in progress", level='warning')
            elif time.monotonic() - self._last_switch_done < self.TOGGLE_COOLDOWN_S:
                self.log_message("Ignoring: a switch just finished", level='warning')
            else:
                self.on_reader_toggled()

    def check_ec_status(self):
        """Check EC access status and gate the frontlight controls accordingly."""
        try:
            response = self.helper.send_command('get-ec-status')
        except Exception as e:
            self.logger.error(f"Failed to check EC status: {e}")
            self.log_message(f"Warning: Could not verify EC status: {e}", level='error')
            return
        if not response or not response.get('success'):
            return

        ec_status = response.get('ec_status', {})
        secure_boot = bool(ec_status.get('secure_boot_enabled'))
        self.secureboot_chip.set("Secure Boot: on" if secure_boot else "Secure Boot: off",
                                 Palette.ERR if secure_boot else Palette.OK)

        if secure_boot or not ec_status.get('available'):
            error_msg = ec_status.get('error_message', 'EC access not available')
            self._ec_available = False
            self.log_message(f"⚠ {error_msg}", level='warning')
            if secure_boot:
                self._set_frontlight_note(
                    "Frontlight controls are unavailable while Secure Boot is enabled "
                    "(disable it in the BIOS: Security → Secure Boot).")
                if not self._secure_boot_dialog_shown:
                    self._secure_boot_dialog_shown = True
                    messagebox.showwarning(
                        "Secure Boot Enabled",
                        "Secure Boot is currently enabled in your BIOS.\n\n"
                        "Frontlight controls require direct hardware access which is blocked by Secure Boot.\n\n"
                        "To enable frontlight controls:\n"
                        "1. Reboot your computer\n"
                        "2. Press ENTER (or F2) during boot to enter BIOS\n"
                        "3. Navigate to Security → Secure Boot\n"
                        "4. Set Secure Boot to 'Disabled'\n"
                        "5. Save and exit (F10)\n\n"
                        "Note: E-Ink display controls will continue to work normally.")
            else:
                self._set_frontlight_note(f"Frontlight unavailable: {error_msg}")
        else:
            self._ec_available = True
            self._set_frontlight_note("")
            self.log_message("EC access verified — frontlight controls enabled")
            self.sync_frontlight_state()
        self._apply_display_state()

    def _set_frontlight_note(self, text):
        if text:
            self.frontlight_note.config(text=text, style='Warn.TLabel')
            self.frontlight_note.grid()
        else:
            self.frontlight_note.config(text="")
            self.frontlight_note.grid_remove()

    def sync_frontlight_state(self):
        """Query EC and update GUI to match actual frontlight brightness"""
        try:
            response = self.helper.send_command('get-frontlight-state')
            if response and response.get('success'):
                brightness = response.get('brightness_level')
                if brightness is not None:
                    self._set_brightness_ui(int(brightness))
                    self.log_message(f"Synced brightness level: {brightness}")
        except Exception as e:
            self.logger.warning(f"Failed to sync frontlight state: {e}")
            self.log_message("Warning: Could not sync frontlight state from EC", level='error')

    def execute_helper_command(self, command, **params):
        """Execute a command via helper; returns the response dict or None (any thread)."""
        if not self.helper.is_connected():
            self.log_message(f"✗ Cannot run '{command}': not connected to helper", level='error')
            return None
        try:
            response = self.helper.send_command(command, **params)
        except Exception as e:
            self.log_message(f"✗ Command error ({command}): {e}", level='error')
            return None

        if response and response.get('success'):
            self.log_message(f"✓ {response.get('message', 'Command completed')}")
            if 'readback' in response:
                self.logger.info(f"  Readback value: {response['readback']}")
            return response

        error = response.get('error', 'Unknown error') if response else 'No response'
        self.log_message(f"✗ {command} failed: {error}", level='error')
        return None

    # ------------------------------------------------------------------
    # Toggle: countdown + worker thread
    # ------------------------------------------------------------------

    def on_eink_toggled(self, skip_countdown=False):
        """Handle the big button / hotkey: start a countdown or the switch."""
        if self._switching:
            self.log_message("Switch already in progress...", level='warning')
            return
        if self._countdown_after is not None:
            # Button pressed again during the countdown: cancel it
            self._cancel_countdown(by_user=True)
            return
        if self._connection_state != 'connected':
            self.log_message("Cannot switch: helper not connected", level='warning')
            return

        countdown = 0 if skip_countdown else self.flip_countdown
        if countdown > 0:
            direction = "OLED" if self._eink_on else "eInk"
            self.log_message(f"Flip your screen to {direction} now!")
            self._run_countdown(countdown)
        else:
            self._start_switch()

    def _run_countdown(self, remaining):
        """Tick the flip countdown, then perform the switch."""
        if remaining > 0:
            self._countdown_remaining = remaining
            self._countdown_after = self.root.after(1000, self._run_countdown, remaining - 1)
            self._apply_display_state()
            self.switch_hint.config(text="Flip the lid now — switching when the countdown ends.")
            self.update_status(f"Switching in {remaining}…")
        else:
            self._countdown_after = None
            self._start_switch()

    def _cancel_countdown(self, by_user=False):
        if self._countdown_after is None:
            return False
        self.root.after_cancel(self._countdown_after)
        self._countdown_after = None
        if by_user:
            self.log_message("Switch cancelled")
            self.update_status("Switch cancelled")
        self._apply_display_state()
        return True

    def _snapshot_params(self):
        """UI values the worker needs (read on the main thread only)."""
        return {
            'scale': self.display_scale,
            'brightness': int(self.brightness_var.get()),
            'autoswitch_theme': bool(self.autoswitch_theme_var.get()),
            'privacy_image': self._pick_privacy_image(),
            'floating_button': bool(self.floating_button_var.get()),
            'reader_rotation': self._reader_rotation,
            'open_reader_app': bool(self.reader_app_var.get()),
        }

    SWITCH_LABELS = {'eink': 'eInk', 'oled': 'OLED', 'reader_on': 'reader mode', 'reader_off': 'OLED (leaving reader mode)'}

    def _start_switch(self, kind=None):
        """Snapshot UI state and run a switch sequence on a worker thread.

        kind: 'eink' | 'oled' | 'reader_on' | 'reader_off' (default: toggle eInk/OLED)
        """
        if self._switching:
            return
        if kind is None:
            kind = 'oled' if self._eink_on else 'eink'
        self._switching = True
        self._apply_display_state()
        params = self._snapshot_params()
        self.update_status(f"Switching to {self.SWITCH_LABELS[kind]}…")
        threading.Thread(target=self._switch_worker, args=(kind, params),
                         daemon=True, name='display-switch').start()

    def _switch_worker(self, kind, params):
        error = None
        if not self._display_lock.acquire(timeout=60):
            error = "another display operation is still running"
        else:
            try:
                if kind == 'eink':
                    self._enable_eink_sequence(params)
                elif kind == 'oled':
                    self._disable_eink_sequence(params)
                elif kind == 'reader_on':
                    self._enter_reader_sequence(params)
                elif kind == 'reader_off':
                    self._leave_reader_sequence(params)
            except SwitchError as e:
                error = str(e)
            except Exception as e:
                self.logger.exception("Display switch crashed")
                error = f"unexpected error: {e}"
            finally:
                self._uninhibit_sleep()
                if error and kind == 'reader_on' and not self._reader_on:
                    self._release_reader_inhibitors()
                self._display_lock.release()
        if not error and kind == 'reader_on' and self._lid_closed:
            # The lid was closed during the switch: make sure the compositor's
            # reaction to it did not undo the layout we just applied.
            threading.Timer(2.0, self._reassert_reader_layout).start()
        self._ui(self._switch_finished, kind, error)

    def _switch_finished(self, kind, error):
        """Main thread: close out a switch."""
        self._switching = False
        self._last_switch_done = time.monotonic()
        if error:
            self.log_message(f"✗ Switch to {self.SWITCH_LABELS[kind]} failed: {error}", level='error')
            self.update_status(f"Switch failed — {error}", error=True)
            # Our idea of the state may be stale; ask the daemon
            if self.helper.is_connected():
                self._sync_state_from_helper()
        elif self._reader_on:
            self.update_status("Reader mode — close the lid and read")
        else:
            self.update_status("eInk display active" if self._eink_on else "OLED display active")
        self.save_settings()  # persists reader_active
        self._apply_display_state()
        if self._closing:
            self._finish_close()

    # --- enable ----------------------------------------------------------

    def _enable_eink_sequence(self, p, rotation='normal'):
        """Worker thread: OLED → eInk, with rollback on failure."""
        self.log_message("Enabling E-Ink display...")
        self._inhibit_sleep()

        # Save state we restore on the way back
        try:
            oled_scale = self.display_mgr.get_display_scale(self.DISPLAY_OLED)
            if oled_scale is not None:
                self.saved_oled_scale = oled_scale
                self.log_message(f"Saved OLED scale: {oled_scale}")
            else:
                self.log_message("Could not read OLED scale, will use default on restore", level='warning')
        except Exception as e:
            self.logger.warning(f"Failed to save OLED scale: {e}")
        try:
            kb = self.display_mgr.get_keyboard_layout()
            if kb:
                self.saved_keyboard_layout = kb
                self.log_message(f"Saved keyboard layout: {kb}")
        except Exception as e:
            self.logger.warning(f"Failed to save keyboard layout: {e}")
        self._save_dpms_timeouts()

        theme_switched = False
        if p['autoswitch_theme']:
            theme_switched = self._set_theme(self.THEME_HIGH_CONTRAST)

        def rollback(reason):
            self.log_message(f"⚠ {reason} — rolling back", level='warning')
            try:
                if self.display_mgr.is_display_active(self.DISPLAY_EINK):
                    self.display_mgr.disable_display(self.DISPLAY_EINK)
            except Exception as e:
                self.logger.warning(f"Rollback: could not disable eInk output: {e}")
            if theme_switched:
                self._set_theme(self.THEME_ADWAITA_DARK)
            raise SwitchError(reason)

        atomic = self.display_mgr.supports_atomic_switch()
        if atomic:
            # GNOME: one persistent configuration change, OLED-only → eInk-only,
            # applied *through* the compositor so it keeps (and re-applies) our
            # layout itself. The OLED goes dark now; the eInk shows the desktop
            # as soon as the T-CON is powered in the next step.
            self._set_busy_hint("Switching the desktop to the eInk output…")
            self.log_message(f"Switching desktop to {self.DISPLAY_EINK} only (scale {p['scale']}"
                             + (f", {rotation}" if rotation != 'normal' else "") + ")...")
            if not self.display_mgr.set_sole_output(self.DISPLAY_EINK, scale=p['scale'], rotation=rotation):
                if theme_switched:
                    self._set_theme(self.THEME_ADWAITA_DARK)
                raise SwitchError(f"the desktop refused to switch to {self.DISPLAY_EINK}")
            time.sleep(0.8)
        else:
            # Step 1: Enable E-Ink output first (overlapping the OLED)
            self._set_busy_hint("Enabling the eInk output…")
            self.log_message(f"Enabling E-Ink display on {self.DISPLAY_EINK} with {p['scale']}x scale"
                             + (f", {rotation}" if rotation != 'normal' else "") + "...")
            if not self.display_mgr.enable_display(self.DISPLAY_EINK, scale=p['scale'], rotation=rotation):
                rollback(f"could not enable {self.DISPLAY_EINK}")
            self.log_message(f"✓ E-Ink display ({self.DISPLAY_EINK}) enabled with {p['scale']}x scale")
            time.sleep(1.0)  # let the compositor settle

        # Step 2: Power the T-CON via the helper
        self._set_busy_hint("Powering the eInk panel…")
        if not self.execute_helper_command('enable-eink'):
            if atomic:
                self.log_message("⚠ T-CON failed — switching the desktop back to the OLED", level='warning')
                self.display_mgr.set_sole_output(self.DISPLAY_OLED, scale=self.saved_oled_scale or 1.0)
                if theme_switched:
                    self._set_theme(self.THEME_ADWAITA_DARK)
                raise SwitchError("the helper could not enable the eInk T-CON")
            rollback("the helper could not enable the eInk T-CON")
        self._ui(self._set_eink_on, True)

        # Step 3: Frontlight at the current brightness
        self.log_message("Enabling frontlight for E-Ink display...")
        if self.execute_helper_command('enable-frontlight', brightness_level=p['brightness']):
            self.log_message(f"✓ Frontlight enabled with brightness {p['brightness']}")
        else:
            self.log_message("⚠ Failed to enable frontlight (may not be available)", level='warning')

        # Step 4: Dynamic mode as the default
        if self.execute_helper_command('set-dynamic'):
            self._eink_mode = 'dynamic'
        else:
            self.log_message("⚠ Failed to set dynamic mode", level='warning')
        time.sleep(0.5)

        if not atomic:
            # Step 5: Turn the OLED off — the eInk is up, so this is safe now
            self._set_busy_hint("Turning the OLED off…")
            if self.display_mgr.disable_display(self.DISPLAY_OLED):
                self.log_message(f"✓ OLED display ({self.DISPLAY_OLED}) disabled")
            else:
                self.log_message(f"⚠ Failed to disable OLED display on {self.DISPLAY_OLED}", level='warning')

            # Step 5b: Re-apply the eInk config now that it is the sole output.
            # On X11, overlapping two outputs at (0,0) with --panning can leave
            # the panning viewport broken once the other output goes away.
            time.sleep(0.3)
            self.display_mgr.enable_display(self.DISPLAY_EINK, scale=p['scale'], rotation=rotation)

        # Step 6: Touch input follows the eInk
        if self.display_mgr.map_touch_to_display(self.DISPLAY_EINK, rotation=rotation):
            self.log_message(f"✓ Touchscreen mapped to {self.DISPLAY_EINK}")
        else:
            self.log_message("Could not map touchscreen (may auto-map)")
        self._schedule_touch_remap(self.DISPLAY_EINK, rotation)

        self._ui(self._start_refresh_timer)
        self._ui(self._ensure_floating_button, p['floating_button'])
        self._verify_final_outputs(active=self.DISPLAY_EINK, inactive=self.DISPLAY_OLED, scale=p['scale'],
                                   rotation=rotation)
        self.log_message("✓ E-Ink display enabled")

    # --- disable ---------------------------------------------------------

    def _disable_eink_sequence(self, p):
        """Worker thread: eInk → OLED. The OLED is always brought back."""
        self.log_message("Preparing to disable E-Ink display...")
        self._inhibit_sleep()

        if self._reader_on:
            # Landscape first so the privacy image is shown upright, and
            # release the lid/idle inhibitors.
            self._apply_reader_layout(p, on=False)

        self._ui(self._stop_refresh_timer)
        self._ui(self._ensure_floating_button, False)

        # Step 1: Dynamic mode renders the colour privacy image properly
        self._set_busy_hint("Showing the privacy image…")
        self.execute_helper_command('set-dynamic')
        time.sleep(0.5)

        # Step 2: Privacy image on the eInk (stays as the panel's last frame)
        self.eink_image_process = None
        image_path = os.path.join(_base_dir(), p['privacy_image'])
        if os.path.exists(image_path):
            self.log_message(f"Displaying privacy image ({p['privacy_image']}) on {self.DISPLAY_EINK}...")
            self.eink_image_process = self.display_mgr.display_fullscreen_image(self.DISPLAY_EINK, image_path)
            if self.eink_image_process:
                # eInk panels need 2-3 s for a full refresh in dynamic mode.
                # The viewer must stay up through the T-CON power-off so the
                # panel retains the image rather than the desktop.
                time.sleep(3.0)
            else:
                self.log_message("Warning: Could not display privacy image", level='warning')
        else:
            self.log_message(f"Warning: Privacy image not found: {image_path}", level='warning')

        # Step 3: Frontlight off, then T-CON off
        self._set_busy_hint("Powering the eInk panel off…")
        if not self.execute_helper_command('disable-frontlight'):
            self.log_message("⚠ Failed to disable frontlight", level='warning')
        tcon_off = bool(self.execute_helper_command('disable-eink'))
        if tcon_off:
            self._ui(self._set_eink_on, False)
            self._eink_mode = None
        else:
            self.log_message("✗ Helper could not power the eInk off — restoring the OLED anyway", level='error')

        # Step 4: Viewer can go now (T-CON is off, panel keeps the image)
        self._kill_image_viewer()

        # Step 5: OLED back on, at its previous scale
        self._set_busy_hint("Turning the OLED on…")
        restore_scale = self.saved_oled_scale if self.saved_oled_scale else 1.0
        if self.display_mgr.supports_atomic_switch():
            oled_ok = self.display_mgr.set_sole_output(self.DISPLAY_OLED, scale=restore_scale)
            if oled_ok:
                self.log_message(f"✓ Desktop switched back to {self.DISPLAY_OLED} only")
            else:
                self.log_message(f"✗ The desktop refused to switch back to {self.DISPLAY_OLED}", level='error')
            time.sleep(0.8)
        else:
            oled_ok = self.display_mgr.enable_display(self.DISPLAY_OLED, scale=restore_scale)
            if oled_ok:
                self.log_message(f"✓ OLED display ({self.DISPLAY_OLED}) enabled with scale {restore_scale}")
            else:
                self.log_message(f"✗ Failed to enable OLED display on {self.DISPLAY_OLED}", level='error')
            time.sleep(1.0)

            if oled_ok:
                # Step 6: eInk output off (refused automatically if it is the last one)
                if self.display_mgr.disable_display(self.DISPLAY_EINK):
                    self.log_message(f"✓ E-Ink display ({self.DISPLAY_EINK}) disabled")
                else:
                    self.log_message(f"⚠ Failed to disable E-Ink display on {self.DISPLAY_EINK}", level='warning')
                # Re-apply OLED config as sole output (same panning fix as the eInk path)
                time.sleep(0.3)
                self.display_mgr.enable_display(self.DISPLAY_OLED, scale=restore_scale)

        # Step 7: Wake the OLED panel (the display change can blank it);
        # retry once since the blanking can arrive asynchronously.
        self.display_mgr.wake_display()
        time.sleep(1.0)
        self.display_mgr.wake_display()

        # Step 8: Touch, theme, keyboard layout, DPMS back to normal
        if self.display_mgr.map_touch_to_display(self.DISPLAY_OLED):
            self.log_message(f"✓ Touchscreen mapped to {self.DISPLAY_OLED}")
        else:
            self.log_message("Could not map touchscreen (may auto-map)")
        if p['autoswitch_theme']:
            self._set_theme(self.THEME_ADWAITA_DARK)
        if self.saved_keyboard_layout:
            self.display_mgr.restore_keyboard_layout(self.saved_keyboard_layout)
            self.log_message("✓ Keyboard layout restored")
        self._restore_dpms_timeouts()

        if oled_ok:
            self._verify_final_outputs(active=self.DISPLAY_OLED, inactive=self.DISPLAY_EINK, scale=restore_scale)

        if not oled_ok:
            raise SwitchError(f"could not re-enable {self.DISPLAY_OLED}")
        if not tcon_off:
            raise SwitchError("the eInk T-CON could not be powered off (OLED restored)")
        self.log_message("✓ OLED display restored")

    def _verify_final_outputs(self, active, inactive, scale, settle=1.5, rotation='normal'):
        """Worker thread: confirm the end state and correct it once if needed.

        The compositor can quietly re-enable an output we just turned off
        (seen on GNOME/X11: eDP-2 came back after a switch to OLED, and the
        next startup check found both outputs active). Wait for things to
        settle, then re-apply the intended configuration if it drifted.
        """
        time.sleep(settle)
        try:
            active_on = self.display_mgr.is_display_active(active)
            inactive_on = self.display_mgr.is_display_active(inactive)
        except Exception as e:
            self.logger.warning(f"Final output check skipped: {e}")
            return
        if active_on and not inactive_on:
            self.logger.info(f"Final output check: {active} on, {inactive} off — OK")
            return

        self.log_message(f"⚠ Output state drifted after the switch ({active}={'on' if active_on else 'off'}, "
                         f"{inactive}={'on' if inactive_on else 'off'}) — correcting", level='warning')
        self.display_mgr.set_sole_output(active, scale=scale, rotation=rotation)
        try:
            active_on = self.display_mgr.is_display_active(active)
            inactive_on = self.display_mgr.is_display_active(inactive)
        except Exception:
            return
        if active_on and not inactive_on:
            self.log_message(f"✓ Output state corrected: {active} on, {inactive} off")
        else:
            self.log_message(f"✗ Output state still wrong after correction ({active}={'on' if active_on else 'off'}, "
                             f"{inactive}={'on' if inactive_on else 'off'})", level='error')

    # --- tablet reader mode -----------------------------------------------

    def _enter_reader_sequence(self, p):
        """Worker thread: (OLED →) eInk, rotated, reading mode, lid-safe."""
        # Arm the lid/idle protections before anything else, so closing the
        # lid while the displays are still switching cannot suspend or lock.
        self._set_busy_hint("Preparing reader mode — keep the lid open for a moment…")
        self._notify("Reader mode: the OLED is about to turn off",
                     "When this screen goes dark, close the lid. The eInk needs about 15 s to be ready.",
                     timeout_ms=20000, urgent=True)
        self._acquire_reader_inhibitors()
        # Give the notification a quiet moment on screen before anything moves
        time.sleep(2.0)
        if not self._eink_on:
            # Enable the eInk *flat* first: a rotated eInk next to the OLED would
            # grow the X screen (1600x2560 beside 2880x1800), and GNOME answers a
            # screen-size change by cloning both panels — the retry storm then
            # blinks the OLED and the switch rolls back. Flat at (0,0) with
            # panning fits inside the existing screen and GNOME stays quiet;
            # the rotation is applied once the eInk is the sole output.
            self._enable_eink_sequence(p)
        self._apply_reader_layout(p, on=True)
        if p.get('open_reader_app'):
            self._ui(self._launch_reader_app)
        self.log_message("✓ Tablet reader mode on — you can close the lid now")
        self._notify("Reader mode ready", "Reading mode, portrait. Open the lid to return to the OLED.",
                     timeout_ms=8000)

    def _notify(self, title, body, timeout_ms=6000, urgent=False):
        """Desktop notification (shows on whichever display is active, i.e. the
        eInk during reader mode). Replaces the previous one. Any thread."""
        try:
            import dbus
            bus = dbus.SessionBus()
            notifier = dbus.Interface(bus.get_object('org.freedesktop.Notifications', '/org/freedesktop/Notifications'),
                                      'org.freedesktop.Notifications')
            hints = {'urgency': dbus.Byte(2 if urgent else 1)}
            self._notify_id = int(notifier.Notify('Tinta4PlusU', getattr(self, '_notify_id', 0),
                                                  'preferences-desktop-display', title, body, [], hints, timeout_ms))
        except Exception as e:
            self.logger.debug(f"notification failed: {e}")

    def _launch_reader_app(self):
        """Open the eInk Reader fullscreen — once; Lector has no single-instance guard."""
        exe = shutil.which(self.READER_APP)
        if not exe:
            self.log_message("eInk Reader not installed — not launching it", level='warning')
            return
        try:
            running = subprocess.run(['pgrep', '-f', r'python3 -m lector'], capture_output=True, text=True)
            if running.returncode == 0 and running.stdout.strip():
                self.log_message("eInk Reader already running — not starting another copy")
                return
        except Exception:
            pass
        try:
            subprocess.Popen([exe, '--fullscreen'], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.log_message("✓ eInk Reader launched fullscreen")
        except Exception as e:
            self.log_message(f"Could not launch the eInk Reader: {e}", level='warning')

    def _leave_reader_sequence(self, p):
        """Worker thread: back to landscape/dynamic, then to the OLED."""
        self._notify("Leaving reader mode", "Switching back to the OLED…", timeout_ms=8000)
        self._disable_eink_sequence(p)   # undoes the reader layout first
        self.log_message("✓ Tablet reader mode off")

    def _apply_reader_layout(self, p, on, already_rotated=False):
        """Worker thread: rotate the eInk, map touch, set the eInk mode, inhibitors."""
        rotation = p['reader_rotation'] if on else 'normal'
        if not already_rotated:
            self._set_busy_hint("Rotating the eInk…" if on else "Back to landscape…")
            if not self.display_mgr.set_sole_output(self.DISPLAY_EINK, scale=p['scale'], rotation=rotation):
                if on:
                    raise SwitchError(f"could not rotate {self.DISPLAY_EINK} to {rotation}")
                self.log_message(f"⚠ Could not restore landscape on {self.DISPLAY_EINK}", level='warning')
            time.sleep(0.5)
            if self.display_mgr.map_touch_to_display(self.DISPLAY_EINK, rotation=rotation):
                self.log_message(f"✓ Touch and pen mapped to {self.DISPLAY_EINK} ({rotation})")
            self._schedule_touch_remap(self.DISPLAY_EINK, rotation)

        if on:
            if self.execute_helper_command('set-reading'):
                self._eink_mode = 'reading'
            self._reader_on = True
            self._acquire_reader_inhibitors()  # no-op if already armed
            self._ui(self._apply_display_state)
        else:
            self._release_reader_inhibitors()
            self._reader_on = False
            if self.execute_helper_command('set-dynamic'):
                self._eink_mode = 'dynamic'
            self._ui(self._apply_display_state)

    def _schedule_touch_remap(self, display, rotation, delays=(4.0, 10.0)):
        """Map digitizers again later: the eInk T-CON's input interfaces (and
        GNOME's own input mapper) can show up after the first mapping."""
        def remap(delay):
            time.sleep(delay)
            if self._closing or self._switching:
                return
            expected_active, _, expected_rotation = self._expected_layout()
            if expected_active != display or expected_rotation != rotation:
                return  # state moved on
            try:
                if self.display_mgr.map_touch_to_display(display, rotation=rotation):
                    self.logger.info(f"Touch re-mapped to {display} ({rotation}) after {delay:.0f}s")
            except Exception as e:
                self.logger.warning(f"Touch re-map failed: {e}")
        for d in delays:
            threading.Thread(target=remap, args=(d,), daemon=True, name='touch-remap').start()

    def _reassert_reader_layout(self):
        """Worker thread: after a lid event the compositor may have re-enabled
        the OLED or dropped the rotation; put the reader layout back."""
        if not self._display_lock.acquire(timeout=60):
            return
        try:
            if not self._reader_on:
                return
            rotation = self._reader_rotation
            eink_on = self.display_mgr.is_display_active(self.DISPLAY_EINK)
            oled_on = self.display_mgr.is_display_active(self.DISPLAY_OLED)
            rot_now = self.display_mgr.get_display_rotation(self.DISPLAY_EINK) if eink_on else None
            if eink_on and not oled_on and rot_now == rotation:
                self.logger.info("Lid event: reader layout intact")
                return
            self.log_message(f"Lid event: outputs changed (eInk={'on' if eink_on else 'off'} {rot_now or ''}, "
                             f"OLED={'on' if oled_on else 'off'}) — restoring reader layout", level='warning')
            self.display_mgr.set_sole_output(self.DISPLAY_EINK, scale=self.display_scale, rotation=rotation)
            self.display_mgr.map_touch_to_display(self.DISPLAY_EINK, rotation=rotation)
            self.log_message("✓ Reader layout restored")
        except Exception as e:
            self.logger.error(f"Reader layout re-assert failed: {e}")
        finally:
            self._display_lock.release()

    # Inhibitors and desktop preferences for reading with the lid closed

    def _acquire_reader_inhibitors(self):
        self._apply_reader_system_prefs()
        if self._lid_inhibit_fd is None:
            try:
                import dbus
                bus = dbus.SystemBus()
                mgr = dbus.Interface(bus.get_object('org.freedesktop.login1', '/org/freedesktop/login1'),
                                     'org.freedesktop.login1.Manager')
                fd = mgr.Inhibit('handle-lid-switch', 'Tinta4PlusU',
                                 'Reading on the eInk with the lid closed', 'block')
                self._lid_inhibit_fd = fd.take()
                self.logger.info("Acquired lid-switch inhibitor")
            except Exception as e:
                self.log_message(f"⚠ Could not inhibit lid-switch handling: {e}", level='warning')
        if self._idle_inhibit_cookie is None:
            try:
                import dbus
                bus = dbus.SessionBus()
                ss = dbus.Interface(bus.get_object('org.freedesktop.ScreenSaver', '/org/freedesktop/ScreenSaver'),
                                    'org.freedesktop.ScreenSaver')
                self._idle_inhibit_cookie = int(ss.Inhibit('Tinta4PlusU', 'Reading on the eInk'))
                self.logger.info("Acquired idle/screensaver inhibitor")
            except Exception as e:
                self.logger.warning(f"Could not inhibit idle blanking: {e}")

    def _release_reader_inhibitors(self):
        if self._lid_inhibit_fd is not None:
            try:
                os.close(self._lid_inhibit_fd)
            except OSError:
                pass
            self._lid_inhibit_fd = None
            self.logger.info("Released lid-switch inhibitor")
        if self._idle_inhibit_cookie is not None:
            try:
                import dbus
                bus = dbus.SessionBus()
                ss = dbus.Interface(bus.get_object('org.freedesktop.ScreenSaver', '/org/freedesktop/ScreenSaver'),
                                    'org.freedesktop.ScreenSaver')
                ss.UnInhibit(self._idle_inhibit_cookie)
            except Exception as e:
                self.logger.warning(f"Could not release idle inhibitor: {e}")
            self._idle_inhibit_cookie = None
        self._restore_reader_system_prefs()

    def _apply_reader_system_prefs(self):
        """GNOME: don't suspend on lid close, don't auto-rotate. Originals are
        kept in the settings file so they survive a crash."""
        if self.display_mgr.desktop_env not in ('gnome', 'cinnamon') or self._reader_backup:
            return
        backup = {}
        for schema, key, value in self.READER_GSETTINGS:
            try:
                cur = subprocess.run(['gsettings', 'get', schema, key], capture_output=True, text=True, timeout=5)
                if cur.returncode != 0:
                    continue
                backup[f"{schema} {key}"] = cur.stdout.strip()
                subprocess.run(['gsettings', 'set', schema, key, value], capture_output=True, timeout=5)
            except Exception as e:
                self.logger.warning(f"gsettings {schema} {key}: {e}")
        if backup:
            self._reader_backup = backup
            self.save_settings()
            self.logger.info(f"Reader mode: desktop prefs overridden ({len(backup)} keys)")

    def _restore_reader_system_prefs(self):
        if not self._reader_backup:
            return
        for schema_key, value in self._reader_backup.items():
            schema, _, key = schema_key.partition(' ')
            try:
                subprocess.run(['gsettings', 'set', schema, key, value], capture_output=True, timeout=5)
            except Exception as e:
                self.logger.warning(f"gsettings restore {schema_key}: {e}")
        self.logger.info("Reader mode: desktop prefs restored")
        self._reader_backup = None
        self.save_settings()

    # Lid events (from the resume-monitor thread)

    def _on_lid_changed(self, closed):
        if closed == self._lid_closed:
            return
        self._lid_closed = closed
        if not self._reader_on:
            return
        self.logger.info(f"Lid {'closed' if closed else 'opened'} in reader mode")
        if closed:
            # Give the compositor a moment to do whatever it does on lid close, then undo it
            threading.Timer(2.5, self._reassert_reader_layout).start()
        elif self._reader_lid_open_exits:
            self._ui(self._lid_opened_leave_reader)

    def _lid_opened_leave_reader(self):
        if not self._reader_on or self._switching or self._closing:
            return
        self._cancel_countdown()
        self.log_message("Lid opened — leaving tablet reader mode")
        self._start_switch('reader_off')

    def on_reader_toggled(self):
        """Reader button / Super+Shift+P."""
        if self._switching:
            self.log_message("Switch already in progress...", level='warning')
            return
        self._cancel_countdown()
        if self._connection_state != 'connected':
            self.log_message("Cannot start reader mode: helper not connected", level='warning')
            return
        self._start_switch('reader_off' if self._reader_on else 'reader_on')

    def on_reader_orientation_changed(self, _event=None):
        rotation = self.READER_ORIENTATIONS.get(self.reader_orientation_var.get(), 'left')
        if rotation == self._reader_rotation:
            return
        self._reader_rotation = rotation
        self.log_message(f"Reader orientation: {self.reader_orientation_var.get()}")
        self.save_settings()
        if self._reader_on and not self._switching:
            threading.Thread(target=self._reassert_reader_layout, daemon=True, name='reader-rotate').start()

    def _set_theme(self, theme):
        """Apply a desktop theme; never let a theme failure abort a switch."""
        try:
            self.theme_mgr.set_theme(theme)
            return True
        except Exception as e:
            self.log_message(f"⚠ Could not switch theme to {theme}: {e}", level='warning')
            return False

    def _kill_image_viewer(self):
        proc, self.eink_image_process = self.eink_image_process, None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
            self.log_message("Closed image viewer (image persisted on E-Ink)")
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _ensure_floating_button(self, wanted):
        """Main thread: create or destroy the floating refresh button."""
        if wanted and self.floating_refresh_button is None:
            try:
                self.floating_refresh_button = FloatingRefreshButton(self.root, self.on_refresh_full, self.logger)
            except Exception as e:
                self.logger.warning(f"Floating refresh button not available: {e}")
                self.log_message("Floating refresh button unavailable (Wayland limitation)", level='warning')
        elif not wanted and self.floating_refresh_button is not None:
            self.floating_refresh_button.destroy()
            self.floating_refresh_button = None

    # ------------------------------------------------------------------
    # eInk controls
    # ------------------------------------------------------------------

    def on_refresh_full(self):
        """Perform full E-Ink refresh"""
        if not self._eink_on:
            return
        self.update_status("Refreshing E-Ink display...")
        if self.execute_helper_command('refresh-eink'):
            self.update_status("E-Ink refresh complete")

    def _on_mode_clicked(self, mode):
        if self.mode_var.get() == '':
            # Clicking the already-active mode deselects the toggle; keep it on
            self.mode_var.set(mode)
            return
        if mode == 'dynamic':
            self.on_set_dynamic()
        else:
            self.on_set_reading()

    def on_set_dynamic(self):
        """Set E-Ink to Dynamic Mode (fast refresh)"""
        self.update_status("Setting Dynamic Mode...")
        if self.execute_helper_command('set-dynamic'):
            self._eink_mode = 'dynamic'
            self.update_status("E-Ink in Dynamic mode")
        self._apply_display_state()

    def on_set_reading(self):
        """Set E-Ink to Reading Mode (high-quality refresh)"""
        self.update_status("Setting Reading Mode...")
        if self.execute_helper_command('set-reading'):
            self._eink_mode = 'reading'
            self.update_status("E-Ink in Reading mode")
        self._apply_display_state()

    def _set_brightness_ui(self, level):
        """Reflect a brightness level in the slider/label without sending it."""
        level = max(0, min(self.BRIGHTNESS_MAX, int(level)))
        self._brightness_level = level
        self._brightness_programmatic = True
        try:
            self.brightness_var.set(level)
        finally:
            self._brightness_programmatic = False
        self.brightness_label.config(text=f"{level} / {self.BRIGHTNESS_MAX}" if level else "off")

    def on_brightness_changed(self, value):
        """Slider moved (also fires on programmatic set, which we ignore)."""
        if self._brightness_programmatic:
            return
        level = int(round(float(value)))
        self._brightness_level = level
        self._brightness_programmatic = True
        try:
            self.brightness_var.set(level)   # snap the knob to whole steps
        finally:
            self._brightness_programmatic = False
        self.brightness_label.config(text=f"{level} / {self.BRIGHTNESS_MAX}" if level else "off")

        # Debounce: avoid a burst of EC writes while dragging
        if self.brightness_timer:
            self.root.after_cancel(self.brightness_timer)
        self.brightness_timer = self.root.after(300, self._set_brightness, level)

    def _step_brightness(self, delta):
        level = max(0, min(self.BRIGHTNESS_MAX, int(self.brightness_var.get()) + delta))
        if level == int(self.brightness_var.get()):
            return
        self._set_brightness_ui(level)
        self._set_brightness(level)

    def _set_brightness(self, level):
        """Actually set the brightness after debounce"""
        self.brightness_timer = None
        if not self._eink_on:
            return  # the slider is only live in eInk mode
        if self.execute_helper_command('set-brightness', level=level):
            self.update_status(f"Frontlight brightness {level}" if level else "Frontlight off")

    def _on_brightness_key_up(self, _event):
        """XF86MonBrightnessUp — step frontlight up when in eInk mode"""
        if not self._eink_on:
            return None
        self._step_brightness(+1)
        return "break"

    def _on_brightness_key_down(self, _event):
        """XF86MonBrightnessDown — step frontlight down when in eInk mode"""
        if not self._eink_on:
            return None
        self._step_brightness(-1)
        return "break"

    def _update_refresh_period_label(self):
        period = int(self.refresh_period_var.get())
        self.refresh_period_label.config(text="off" if period == 0 else f"every {period} s")

    def on_refresh_period_changed(self, value):
        """Handle refresh period slider change (snaps to 5 s steps)"""
        period = int(round(float(value) / 5.0) * 5)
        if int(self.refresh_period_var.get()) != period:
            self.refresh_period_var.set(period)
        self._update_refresh_period_label()
        if self._eink_on:
            self._start_refresh_timer()
        self.save_settings()

    def _start_refresh_timer(self):
        """Start or restart the periodic refresh timer (main thread)"""
        self._stop_refresh_timer()
        period = int(self.refresh_period_var.get())
        if period > 0 and self._eink_on:
            self.refresh_timer = self.root.after(period * 1000, self._periodic_refresh)
            self.logger.info(f"Started periodic refresh timer ({period}s)")

    def _stop_refresh_timer(self):
        """Stop the periodic refresh timer (main thread)"""
        if self.refresh_timer:
            self.root.after_cancel(self.refresh_timer)
            self.refresh_timer = None
            self.logger.info("Stopped periodic refresh timer")

    def _periodic_refresh(self):
        """Execute a periodic refresh and reschedule"""
        self.refresh_timer = None
        if not self._eink_on:
            return
        if not self._switching:
            self.log_message("Performing periodic refresh...")
            self.execute_helper_command('refresh-eink')
        self._start_refresh_timer()

    # ------------------------------------------------------------------
    # Settings handlers
    # ------------------------------------------------------------------

    def _set_scale_ui(self, scale):
        self.scale_var.set(scale)
        self.scale_label.config(text=f"{scale:.2f}×")

    def on_scale_changed(self, value):
        """Handle display scale slider change (snaps to 0.05)"""
        scale = round(round(float(value) / 0.05) * 0.05, 2)
        if abs(scale - self.display_scale) < 1e-9:
            self.scale_label.config(text=f"{scale:.2f}×")
            return
        self.display_scale = scale
        self._set_scale_ui(scale)
        self.log_message(f"Display scale set to {scale:.2f}× (applies on next switch to eInk)")
        self.save_settings()

    def on_countdown_changed(self):
        try:
            value = int(self.countdown_var.get())
        except (tk.TclError, ValueError):
            return
        value = max(0, min(15, value))
        if value != self.flip_countdown:
            self.flip_countdown = value
            self.countdown_var.set(value)
            self.log_message(f"Flip countdown set to {value} s")
            self.save_settings()

    def on_autoswitch_theme_changed(self):
        enabled = self.autoswitch_theme_var.get()
        self.log_message("Theme auto-switching enabled" if enabled else "Theme auto-switching disabled")
        self.save_settings()

    def on_text_size_changed(self, _event=None):
        name = self.text_size_var.get()
        if name not in self.TEXT_SIZES or name == self.text_size:
            return
        self._apply_text_size(name)
        self.log_message(f"Text size: {name}")
        self.save_settings()

    def on_floating_button_changed(self):
        wanted = self.floating_button_var.get()
        if self._eink_on:
            self._ensure_floating_button(wanted)
        self.save_settings()

    def _privacy_image_setting(self):
        choice = self.privacy_image_var.get()
        return choice if choice in self.EINK_DISABLED_IMAGES else 'random'

    def _set_privacy_image_selection(self, value):
        """Apply a stored privacy_image setting ('random' or a filename) to the combobox."""
        if value in self.EINK_DISABLED_IMAGES:
            self.privacy_image_var.set(value)
        else:
            self.privacy_image_var.set(self.PRIVACY_RANDOM)
        self._update_privacy_preview()

    def _pick_privacy_image(self):
        """Return the privacy image file name: dropdown override if set, else random."""
        choice = self.privacy_image_var.get()
        if choice in self.EINK_DISABLED_IMAGES:
            return choice
        if not self.EINK_DISABLED_IMAGES:
            return 'eink-disable1.jpg'  # reported as missing by the disable sequence
        return random.choice(self.EINK_DISABLED_IMAGES)

    def on_privacy_image_changed(self, _event=None):
        choice = self.privacy_image_var.get()
        self.log_message("Privacy image: random selection" if choice == self.PRIVACY_RANDOM
                         else f"Privacy image: {choice}")
        self._update_privacy_preview()
        self.save_settings()

    def _update_privacy_preview(self):
        """Show a small thumbnail of the selected privacy image (needs Pillow)."""
        choice = self.privacy_image_var.get()
        if not HAS_PIL or choice not in self.EINK_DISABLED_IMAGES:
            self.privacy_preview.config(image='', text='')
            self.privacy_preview.image = None
            return
        photo = self._thumbnail_cache.get(choice)
        if photo is None:
            try:
                # Go through PPM so only Pillow's core is needed (no ImageTk)
                img = Image.open(os.path.join(_base_dir(), choice)).convert('RGB')
                img.thumbnail((96, 60))
                buf = io.BytesIO()
                img.save(buf, format='PPM')
                photo = tk.PhotoImage(data=buf.getvalue())
                self._thumbnail_cache[choice] = photo
            except Exception as e:
                self.logger.warning(f"Could not load preview for {choice}: {e}")
                return
        self.privacy_preview.config(image=photo)
        self.privacy_preview.image = photo

    # ------------------------------------------------------------------
    # Indicator / D-Bus control
    # ------------------------------------------------------------------

    def get_state_dict(self):
        """State for the indicator (any thread; plain mirrors only)."""
        return {
            'connected': self._connection_state == 'connected',
            'eink_on': bool(self._eink_on),
            'reader_on': bool(self._reader_on),
            'switching': bool(self._switching),
            'countdown': int(self._countdown_remaining if self._countdown_after is not None else 0),
            'mode': self._eink_mode or '',
            'brightness': int(self._brightness_level),
            'frontlight_available': bool(self._ec_available),
            'window_visible': bool(self._window_visible),
        }

    def set_brightness_from_remote(self, level):
        level = max(0, min(self.BRIGHTNESS_MAX, int(level)))
        self._set_brightness_ui(level)
        self._set_brightness(level)

    def connect_from_remote(self):
        if self._connection_state == 'disconnected':
            self.show_window()
            self.initialize_helper()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self._window_visible = True

    def hide_window(self):
        self.root.withdraw()
        self._window_visible = False

    def on_close_request(self):
        """Window close button: hide behind the indicator when it is running."""
        if self._close_to_indicator and not self._closing and self._indicator_running():
            self.hide_window()
            self.log_message("Window hidden — still running in the top bar (Quit from the indicator or Ctrl+Q)")
            return
        self.on_closing()

    def _indicator_running(self):
        proc = self._indicator_process
        if proc is not None and proc.poll() is None:
            return True
        try:
            import dbus
            return bool(dbus.SessionBus().name_has_owner(INDICATOR_BUS_NAME))
        except Exception:
            return False

    def _indicator_command(self):
        for candidate in (shutil.which('tinta4plusu-indicator'), os.path.join(_base_dir(), 'Indicator.py')):
            if candidate and os.path.exists(candidate):
                return [candidate] if not candidate.endswith('.py') else [sys.executable, candidate]
        return None

    def _ensure_indicator(self):
        """Start the top-bar indicator unless one is already running."""
        if self.ui_preview or not self.indicator_var.get() or self._indicator_running():
            return
        cmd = self._indicator_command()
        if not cmd:
            self.logger.info("Indicator not installed")
            return
        try:
            self._indicator_process = subprocess.Popen(
                cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.logger.info("Started top-bar indicator")
        except Exception as e:
            self.log_message(f"Could not start the indicator: {e}", level='warning')

    def on_indicator_changed(self):
        self.save_settings()
        if self.indicator_var.get():
            self._ensure_indicator()
        elif self._indicator_process is not None and self._indicator_process.poll() is None:
            self._indicator_process.terminate()
            self._indicator_process = None

    def _start_dbus_service(self, bus_module):
        """Export org.tinta4plusu.Gui (called on the GLib loop thread)."""
        try:
            session = bus_module.SessionBus()
            if session.name_has_owner(GUI_BUS_NAME):
                self.logger.warning("D-Bus name already owned — control service not started")
                return
            self._dbus_service = _make_dbus_service(self, session)
            self.logger.info(f"D-Bus control service at {GUI_BUS_NAME}")
        except Exception as e:
            self.logger.warning(f"D-Bus control service unavailable: {e}")

    def on_buy_coffee(self):
        """Open the upstream author's Buy Me A Coffee page"""
        try:
            webbrowser.open('https://buymeacoffee.com/joncox')
            self.log_message("Opening Buy Me A Coffee page...")
        except Exception as e:
            self.log_message(f"Failed to open browser: {e}", level='error')

    # ------------------------------------------------------------------
    # Layout watchdog
    # ------------------------------------------------------------------

    LAYOUT_WATCHDOG_S = 4.0        # poll interval (one `xrandr --query`, a few ms)
    LAYOUT_WATCHDOG_CONFIRM = 2    # consecutive mismatches before correcting

    def _start_layout_watchdog(self):
        """GNOME has no stored monitor layout for this machine, so whenever it
        reconfigures on its own (lid, hotplug, DPMS wake, settings) it falls
        back to an *extended* desktop across both panels — long after our
        switch finished. Watch the outputs while idle and put them back."""
        self._layout_watchdog_stop = threading.Event()
        self._layout_mismatches = 0
        threading.Thread(target=self._layout_watchdog_loop, daemon=True, name='layout-watchdog').start()

    def _expected_layout(self):
        """(active, inactive, rotation) for the current state."""
        if self._eink_on:
            return (self.DISPLAY_EINK, self.DISPLAY_OLED,
                    self._reader_rotation if self._reader_on else 'normal')
        return (self.DISPLAY_OLED, self.DISPLAY_EINK, 'normal')

    def _layout_watchdog_loop(self):
        while not self._layout_watchdog_stop.wait(self.LAYOUT_WATCHDOG_S):
            if (self._switching or self._closing or self._countdown_after is not None
                    or self._connection_state != 'connected'
                    or time.monotonic() - self._last_switch_done < 2 * self.LAYOUT_WATCHDOG_S):
                self._layout_mismatches = 0
                continue
            try:
                active, inactive, rotation = self._expected_layout()
                ok = (self.display_mgr.is_display_active(active)
                      and not self.display_mgr.is_display_active(inactive))
            except Exception as e:
                self.logger.debug(f"layout watchdog: {e}")
                continue
            if ok:
                self._layout_mismatches = 0
                continue
            self._layout_mismatches += 1
            if self._layout_mismatches < self.LAYOUT_WATCHDOG_CONFIRM:
                continue
            self._layout_mismatches = 0
            self._correct_layout(active, inactive, rotation)

    def _correct_layout(self, active, inactive, rotation):
        """Worker: re-apply the expected output layout (under the display lock)."""
        if not self._display_lock.acquire(timeout=10):
            return
        try:
            if self._switching:
                return
            self.log_message(f"⚠ Desktop changed the display layout behind our back "
                             f"({inactive} came on) — restoring {active}", level='warning')
            scale = self.display_scale if active == self.DISPLAY_EINK else (self.saved_oled_scale or 1.0)
            self.display_mgr.set_sole_output(active, scale=scale, rotation=rotation)
            self.display_mgr.map_touch_to_display(active, rotation=rotation)
            if (self.display_mgr.is_display_active(active)
                    and not self.display_mgr.is_display_active(inactive)):
                self.log_message(f"✓ Display layout restored ({active} only)")
            else:
                self.log_message("✗ Could not restore the display layout", level='error')
        except Exception as e:
            self.logger.error(f"layout correction failed: {e}")
        finally:
            self._display_lock.release()

    # ------------------------------------------------------------------
    # Startup / resume checks
    # ------------------------------------------------------------------

    def _schedule_startup_check(self):
        """Run the post-login display check once, after we know the eInk state."""
        if self._startup_check_done or self.ui_preview:
            return
        self._startup_check_done = True
        threading.Thread(target=self._run_display_check, args=("Startup check",),
                         daemon=True, name='startup-check').start()

    def _run_display_check(self, label):
        """Background: validate display/input state against what we expect.

        ``expect_eink`` comes from the daemon-synced mirror, so a GUI that
        starts while the eInk is powered no longer forces the OLED back on.
        """
        if not self._display_lock.acquire(timeout=60):
            self.logger.warning(f"{label}: skipped, a display operation is still running")
            return
        try:
            expect_eink = self._eink_on
            self.logger.info(f"{label}: running (expect_eink={expect_eink})")
            checker = ResumeCheck(self.display_mgr, self.logger)
            results = checker.run(
                expect_eink=expect_eink,
                saved_oled_scale=self.saved_oled_scale,
                saved_keyboard_layout=self.saved_keyboard_layout,
                eink_scale=self.display_scale,
                eink_rotation=self._reader_rotation if self._reader_on else 'normal')
            summary = "; ".join(r for r in results
                                if r.startswith("Fixed:") or r.startswith("Warning:"))
            if summary:
                self.log_message(f"{label}: {summary}", level='warning')
            else:
                self.logger.info(f"{label}: all OK")
        except Exception as e:
            self.logger.error(f"{label} failed: {e}")
        finally:
            self._display_lock.release()

    def _start_resume_monitor(self):
        """Background thread that listens for system resume events (logind PrepareForSleep)."""
        self._resume_monitor_stop.clear()
        self._resume_monitor_thread = threading.Thread(
            target=self._resume_monitor_loop, daemon=True, name='resume-monitor')
        self._resume_monitor_thread.start()

    def _resume_monitor_loop(self):
        """Background loop: listen for PrepareForSleep(false) from logind."""
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib
        except ImportError:
            self.logger.warning("Resume monitor: dbus/GLib not available, falling back to poll")
            self._resume_monitor_poll()
            return

        try:
            DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
            bus.add_signal_receiver(
                self._on_prepare_for_sleep,
                signal_name='PrepareForSleep',
                dbus_interface='org.freedesktop.login1.Manager',
                bus_name='org.freedesktop.login1')
            # Lid open/close (UPower) drives tablet reader mode
            bus.add_signal_receiver(
                self._on_upower_properties_changed,
                signal_name='PropertiesChanged',
                dbus_interface='org.freedesktop.DBus.Properties',
                bus_name='org.freedesktop.UPower',
                path='/org/freedesktop/UPower')
            try:
                up = bus.get_object('org.freedesktop.UPower', '/org/freedesktop/UPower')
                self._lid_closed = bool(dbus.Interface(up, 'org.freedesktop.DBus.Properties').Get(
                    'org.freedesktop.UPower', 'LidIsClosed'))
            except Exception:
                pass

            self._start_dbus_service(dbus)

            self._glib_loop = GLib.MainLoop()
            self.logger.info("Resume monitor started (D-Bus PrepareForSleep)")
            context = self._glib_loop.get_context()
            while not self._resume_monitor_stop.is_set():
                context.iteration(True)
        except Exception as e:
            self.logger.warning(f"Resume monitor D-Bus setup failed: {e}, falling back to poll")
            self._resume_monitor_poll()

    def _resume_monitor_poll(self):
        """Fallback resume monitor using /sys/power/wakeup_count polling."""
        self.logger.info("Resume monitor started (poll fallback)")
        try:
            with open('/sys/power/wakeup_count', 'r') as f:
                last_count = f.read().strip()
        except Exception:
            self.logger.warning("Resume monitor: cannot read wakeup_count, monitor disabled")
            return

        while not self._resume_monitor_stop.wait(3.0):
            try:
                with open('/sys/power/wakeup_count', 'r') as f:
                    count = f.read().strip()
                if count != last_count:
                    last_count = count
                    self.logger.info("Resume detected (wakeup_count changed)")
                    self._on_system_resume()
            except Exception:
                pass
            closed = self._read_lid_state_acpi()
            if closed is not None:
                self._on_lid_changed(closed)

    @staticmethod
    def _read_lid_state_acpi():
        try:
            for name in os.listdir('/proc/acpi/button/lid'):
                with open(f'/proc/acpi/button/lid/{name}/state') as f:
                    return 'closed' in f.read()
        except Exception:
            return None
        return None

    def _on_upower_properties_changed(self, _iface, changed, _invalidated):
        if 'LidIsClosed' in changed:
            self._on_lid_changed(bool(changed['LidIsClosed']))

    def _on_prepare_for_sleep(self, going_to_sleep):
        """D-Bus signal handler for PrepareForSleep (runs on the monitor thread)."""
        if not going_to_sleep:
            self.logger.info("System resumed from suspend (PrepareForSleep=false)")
            time.sleep(2.0)  # let the display subsystem stabilise
            self._on_system_resume()

    def _on_system_resume(self):
        """After wake from suspend / lid open: fix displays, then refresh the T-CON handle."""
        # The T-CON can re-enumerate on resume, leaving the helper's USB
        # handle stale; reconnect it *before* the check may rely on it.
        try:
            if self.helper and self.helper.is_connected():
                response = self.helper.send_command('reconnect-usb')
                if response and response.get('reconnected'):
                    self.log_message("↻ Post-resume: eInk USB reconnected")
                elif response and not response.get('success'):
                    self.logger.warning(f"Resume: eInk USB reconnect failed: {response.get('error')}")
        except Exception as e:
            self.logger.error(f"Resume: eInk USB reconnect error: {e}")

        self._run_display_check("↻ Post-resume")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def on_closing(self):
        """Window close: get the user back to the OLED first, then exit."""
        if self._closing:
            return
        self._closing = True
        self.logger.info("Application closing")
        self._cancel_countdown()

        if self._switching:
            # Let the running switch finish; _switch_finished() calls _finish_close()
            self.update_status("Waiting for the current switch to finish before exiting…")
            return

        if self._eink_on and self._connection_state == 'connected':
            # Switch back to OLED so the privacy image covers the eInk and the
            # OLED is on for whoever opens the lid next.
            self.log_message("eInk active at exit — switching back to OLED first")
            self.update_status("Switching back to OLED before exiting…")
            self._start_switch()
            return

        self._finish_close()

    def _finish_close(self):
        # Stop monitors
        if hasattr(self, '_layout_watchdog_stop'):
            self._layout_watchdog_stop.set()
        self._resume_monitor_stop.set()
        if self._glib_loop:
            try:
                self._glib_loop.quit()
            except Exception:
                pass

        self._uninhibit_sleep()
        if self._indicator_process is not None and self._indicator_process.poll() is None:
            self._indicator_process.terminate()
        if self._reader_on:
            # Leaving with the eInk still rotated (helper gone): at least undo the system prefs
            self._release_reader_inhibitors()
        self._stop_refresh_timer()
        self._ensure_floating_button(False)
        self._kill_image_viewer()
        self.stop_keepalive()

        # Only the process that launched the helper may shut it down; a GUI
        # that attached to an existing daemon leaves it for the watchdog.
        launched_here = self.helper_process is not None
        if self.helper.is_connected():
            self.helper.disconnect(shutdown_helper=launched_here)
        if launched_here:
            try:
                self.helper_process.wait(timeout=3)
            except Exception:
                try:
                    self.helper_process.terminate()
                    self.helper_process.wait(timeout=2)
                except Exception:
                    pass

        self._destroyed = True
        if self._ui_pump_after:
            self.root.after_cancel(self._ui_pump_after)
        self.root.destroy()


# ----------------------------------------------------------------------
# Startup helpers
# ----------------------------------------------------------------------

def show_disclaimer_dialog(parent):
    """Show disclaimer dialog on first launch. Returns True if user agrees, False otherwise."""
    eula_file = os.path.join(_base_dir(), "README_EULA_INSTRUCTIONS_WARNINGS.txt")

    try:
        with open(eula_file, 'r') as f:
            DISCLAIMER_TEXT = f.read()
    except FileNotFoundError:
        messagebox.showerror("EULA Not Found",
                             "EULA file not found:\n" + eula_file +
                             "\n\nThe application will now exit.", parent=parent)
        sys.exit(1)
    except Exception as e:
        messagebox.showerror("EULA Not Found",
                             f"Failed to read EULA file:\n{eula_file}\n\nError: {str(e)}\n\n"
                             "The application will now exit.", parent=parent)
        sys.exit(1)

    # Check if agreement file exists
    config_dir = EInkControlGUI.CONFIG_DIR
    agree_file = os.path.join(config_dir, "agree")
    if os.path.exists(agree_file):
        return True  # User has already agreed

    # Create a custom EULA dialog
    dialog = tk.Toplevel(parent)
    dialog.title("End User License Agreement")
    dialog.geometry("900x750")
    dialog.resizable(False, False)

    # Disable the close button (X) - user must click Agree or Disagree
    dialog.protocol("WM_DELETE_WINDOW", lambda: None)

    # Make dialog modal
    dialog.transient(parent)
    dialog.grab_set()

    # Center the dialog on screen
    dialog.update_idletasks()
    x = (dialog.winfo_screenwidth() // 2) - (900 // 2)
    y = (dialog.winfo_screenheight() // 2) - (750 // 2)
    dialog.geometry(f"900x750+{x}+{y}")

    main_frame = ttk.Frame(dialog, padding="20")
    main_frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(main_frame, text="End User License Agreement",
              font=('TkDefaultFont', 14, 'bold')).pack(pady=(0, 10))
    ttk.Label(main_frame,
              text="Please read the following agreement carefully before using this software.",
              font=('TkDefaultFont', 10)).pack(pady=(0, 10))

    text_frame = ttk.Frame(main_frame)
    text_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 15))

    text_widget = scrolledtext.ScrolledText(text_frame, wrap=tk.WORD, font=('TkDefaultFont', 10),
                                            padx=10, pady=10)
    text_widget.pack(fill=tk.BOTH, expand=True)
    text_widget.insert('1.0', DISCLAIMER_TEXT)
    text_widget.config(state=tk.DISABLED)

    result = {'agreed': False}

    def on_agree():
        result['agreed'] = True
        dialog.destroy()

    def on_disagree():
        result['agreed'] = False
        dialog.destroy()

    button_frame = ttk.Frame(main_frame)
    button_frame.pack(fill=tk.X)
    ttk.Button(button_frame, text="I Disagree", command=on_disagree, width=15).pack(side=tk.LEFT, padx=(0, 10))
    ttk.Button(button_frame, text="I Agree", command=on_agree, width=15,
               style='Accent.TButton' if HAS_SV_TTK else 'TButton').pack(side=tk.RIGHT)

    dialog.focus_set()
    dialog.wait_window()

    if result['agreed']:
        try:
            os.makedirs(config_dir, exist_ok=True)
            with open(agree_file, 'w') as f:
                f.write('')
            return True
        except Exception as e:
            print(f"Error creating agreement file: {e}")
            return False
    return False


def _resolve_helper_path(logger):
    """Resolve the helper daemon path, checking binary then script locations."""
    # When running as PyInstaller binary, use the bundle dir for relative paths
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    candidates = [
        '/usr/local/bin/tinta4plusu-helper',                          # Installed binary (symlink)
        os.path.join(base_dir, 'tinta4plusu-helper'),                 # Portable binary (same dir)
        '/usr/local/bin/HelperDaemon.py',                            # Legacy installed script
        os.path.join(base_dir, 'HelperDaemon.py'),                   # Dev script (same dir)
    ]

    for path in candidates:
        if os.path.exists(path):
            logger.info(f"Using helper at: {path}")
            return path

    # Return first candidate as default even if not found yet (will be checked at launch)
    logger.warning("No helper found at any known location")
    return candidates[0]


_instance_lock_fd = None


def acquire_single_instance_lock(logger):
    """Return True if this is the only running GUI instance.

    Holds an exclusive flock() on a per-user lock file for the lifetime of the
    process. The kernel drops the lock when the process dies (even on a crash),
    so there is no stale-lock handling to do. Without this, every click on the
    launcher starts another full copy of the GUI, and the copies then fight over
    the displays (each runs its own startup/resume check).
    """
    global _instance_lock_fd
    lock_dir = os.environ.get('XDG_RUNTIME_DIR')
    if not lock_dir or not os.path.isdir(lock_dir):
        lock_dir = EInkControlGUI.CONFIG_DIR
        os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, 'tinta4plusu-gui.lock')

    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        logger.warning(f"Could not open instance lock {lock_path}: {e} — continuing without it")
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        logger.warning(f"Another instance is already running (lock held on {lock_path})")
        return False
    # Record our PID for humans debugging; the flock is what actually matters.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _instance_lock_fd = fd
    return True


def _setup_logging():
    """Console + private per-user log file (never a predictable /tmp path)."""
    handlers = [logging.StreamHandler()]
    try:
        os.makedirs(os.path.dirname(EInkControlGUI.LOG_FILE), mode=0o700, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            EInkControlGUI.LOG_FILE, maxBytes=1_000_000, backupCount=3))
    except OSError as e:
        print(f"WARNING: cannot open log file {EInkControlGUI.LOG_FILE}: {e}", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers)
    return logging.getLogger('tinta4plusu-gui')


def main():
    """Entry point"""
    autostart = '--autostart' in sys.argv
    start_hidden = '--hidden' in sys.argv      # used by the indicator's login autostart
    # Developer mode: no helper, no display changes. Optional =connected / =eink
    ui_preview = False
    for arg in sys.argv[1:]:
        if arg == '--ui-preview':
            ui_preview = 'disconnected'
        elif arg.startswith('--ui-preview='):
            ui_preview = arg.split('=', 1)[1] or 'disconnected'

    logger = _setup_logging()
    logger.info("===== Tinta4PlusU GUI starting =====")

    # Log uncaught exceptions instead of losing them
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_exception

    HELPER_SCRIPT = _resolve_helper_path(logger)

    if not ui_preview and not acquire_single_instance_lock(logger):
        # Another copy is running: ask it to show its window (it may be hidden
        # behind the indicator); fall back to a notice if it cannot be reached.
        try:
            import dbus
            dbus.Interface(dbus.SessionBus().get_object(GUI_BUS_NAME, GUI_OBJECT_PATH), GUI_IFACE).Show()
            sys.exit(0)
        except Exception:
            pass
        root = tk.Tk(className='tinta4plusu')
        root.withdraw()
        messagebox.showinfo("Tinta4PlusU already running",
                            "Tinta4PlusU is already running.\n\n"
                            "Look for the existing \"ThinkBook E-Ink Control\" window.")
        root.destroy()
        sys.exit(0)

    # className sets WM_CLASS, which GNOME matches against StartupWMClass in
    # the .desktop file so the launcher focuses this window instead of
    # showing a generic "tk" app.
    root = tk.Tk(className='tinta4plusu')
    root.withdraw()  # Hide the main window initially

    # Check disclaimer agreement BEFORE showing the main window
    if not show_disclaimer_dialog(root):
        print("User declined disclaimer. Exiting.")
        root.destroy()
        sys.exit(0)

    # User agreed, show the main window
    root.deiconify()
    app = EInkControlGUI(root, HELPER_SCRIPT, logger, autostart=autostart, ui_preview=ui_preview,
                         start_hidden=start_hidden)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        app.on_closing()


if __name__ == '__main__':
    main()
