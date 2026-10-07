#!/usr/bin/env python3
"""
Tinta4PlusU top-bar indicator.

A small GTK/AppIndicator process that shows the current display state in
the panel and offers the everyday commands (switch display, tablet reader
mode, refresh, eInk mode, frontlight) without having to find the control
window. It owns no display logic: every action is a D-Bus call to the
running GUI (org.tinta4plusu.Gui on the session bus), which keeps all
switching code in one place. If the GUI is not running the menu offers to
start it.

Works wherever StatusNotifier/AppIndicator icons are shown: KDE, XFCE,
Cinnamon natively, GNOME via the AppIndicator extension (enabled by
default on Ubuntu). Needs gir1.2-ayatanaappindicator3-0.1 (or the older
gir1.2-appindicator3-0.1).

    tinta4plusu-indicator              # start the indicator
    tinta4plusu-indicator --autostart  # at login: also start the GUI hidden
"""

import os
import sys
import subprocess
import logging

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib  # noqa: E402

try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
except (ValueError, ImportError):
    try:
        gi.require_version('AppIndicator3', '0.1')
        from gi.repository import AppIndicator3  # noqa: E402
    except (ValueError, ImportError):
        AppIndicator3 = None

import dbus  # noqa: E402
import dbus.service  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402

GUI_BUS_NAME = 'org.tinta4plusu.Gui'
GUI_PATH = '/org/tinta4plusu/Gui'
GUI_IFACE = 'org.tinta4plusu.Gui'
INDICATOR_BUS_NAME = 'org.tinta4plusu.Indicator'
POLL_MS = 2000

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(HERE, 'icons')
ICONS = {
    'off': 'tinta4plusu-off-symbolic',
    'oled': 'tinta4plusu-oled-symbolic',
    'eink': 'tinta4plusu-eink-symbolic',
    'reader': 'tinta4plusu-reader-symbolic',
}
GUI_COMMANDS = ['/usr/local/bin/tinta4plusu', os.path.join(HERE, 'Tinta4Plus.py')]

log = logging.getLogger('tinta4plusu-indicator')


class Indicator:
    def __init__(self, autostart=False):
        self.autostart = autostart
        self.bus = dbus.SessionBus()
        # Owning a name lets the GUI know an indicator is around (close-to-tray)
        self._bus_name = dbus.service.BusName(INDICATOR_BUS_NAME, self.bus)
        self._updating = False
        self._last_state = None
        self._gui_present = None

        self.ind = AppIndicator3.Indicator.new(
            'tinta4plusu', ICONS['off'], AppIndicator3.IndicatorCategory.HARDWARE)
        self.ind.set_icon_theme_path(ICON_DIR)
        self.ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.ind.set_title('Tinta4PlusU')

        self._build_menu()
        self.refresh()
        GLib.timeout_add(POLL_MS, self.refresh)

        if autostart:
            # Give the session a moment, then make sure a (hidden) GUI exists
            GLib.timeout_add_seconds(4, self._ensure_gui, True)

    # ------------------------------------------------------------------
    # GUI proxy
    # ------------------------------------------------------------------

    def _gui(self):
        try:
            obj = self.bus.get_object(GUI_BUS_NAME, GUI_PATH)
            return dbus.Interface(obj, GUI_IFACE)
        except dbus.DBusException:
            return None

    def _call(self, method, *args):
        gui = self._gui()
        if gui is None:
            self._ensure_gui(False)
            return
        try:
            getattr(gui, method)(*args, timeout=5)
        except dbus.DBusException as e:
            log.warning(f"{method} failed: {e}")
        GLib.timeout_add(300, lambda: (self.refresh(), False)[1])  # one-shot: refresh() returns True

    def _ensure_gui(self, hidden):
        """Start the control window if it is not running."""
        if self.bus.name_has_owner(GUI_BUS_NAME):
            return False
        for cmd in GUI_COMMANDS:
            if os.path.exists(cmd):
                args = [cmd] if not cmd.endswith('.py') else [sys.executable, cmd]
                if hidden:
                    args += ['--autostart', '--hidden']
                log.info(f"Starting GUI: {' '.join(args)}")
                subprocess.Popen(args, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return False
        log.error("Tinta4PlusU GUI not found")
        return False

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _item(self, label, callback=None, sensitive=True):
        item = Gtk.MenuItem(label=label)
        if callback:
            item.connect('activate', lambda *_: callback())
        item.set_sensitive(sensitive)
        return item

    def _build_menu(self):
        m = Gtk.Menu()
        self.it_state = self._item("Tinta4PlusU", sensitive=False)
        m.append(self.it_state)
        m.append(Gtk.SeparatorMenuItem())

        self.it_switch = self._item("Switch to eInk", lambda: self._call('Toggle'))
        m.append(self.it_switch)
        self.it_reader = Gtk.CheckMenuItem(label="Tablet reader mode")
        self.it_reader.connect('toggled', self._on_reader_toggled)
        m.append(self.it_reader)
        self.it_connect = self._item("Connect to helper (password)…", lambda: self._call('Connect'))
        m.append(self.it_connect)
        self.it_start = self._item("Start Tinta4PlusU…", lambda: self._ensure_gui(False))
        m.append(self.it_start)
        m.append(Gtk.SeparatorMenuItem())

        self.it_refresh = self._item("Refresh eInk now", lambda: self._call('Refresh'))
        m.append(self.it_refresh)

        self.it_mode = Gtk.MenuItem(label="eInk mode")
        mode_menu = Gtk.Menu()
        self.rb_dynamic = Gtk.RadioMenuItem(label="Dynamic (fast)")
        self.rb_reading = Gtk.RadioMenuItem(label="Reading (high quality)", group=self.rb_dynamic)
        self.rb_dynamic.connect('toggled', lambda w: self._on_mode(w, 'dynamic'))
        self.rb_reading.connect('toggled', lambda w: self._on_mode(w, 'reading'))
        mode_menu.append(self.rb_dynamic)
        mode_menu.append(self.rb_reading)
        self.it_mode.set_submenu(mode_menu)
        m.append(self.it_mode)

        self.it_light = Gtk.MenuItem(label="Frontlight")
        light_menu = Gtk.Menu()
        self.rb_light = []
        group = None
        for level in range(0, 9):
            label = "Off" if level == 0 else f"{level} / 8"
            rb = Gtk.RadioMenuItem(label=label, group=group)
            group = group or rb
            rb.connect('toggled', lambda w, lvl=level: self._on_light(w, lvl))
            light_menu.append(rb)
            self.rb_light.append(rb)
        self.it_light.set_submenu(light_menu)
        m.append(self.it_light)
        m.append(Gtk.SeparatorMenuItem())

        self.it_show = self._item("Open control window", lambda: self._call('Show'))
        m.append(self.it_show)
        m.append(self._item("Quit Tinta4PlusU", self._quit_all))
        m.show_all()
        self.ind.set_menu(m)

    # --- callbacks (ignored while we are syncing the menu to the state) ---

    def _on_reader_toggled(self, _w):
        if not self._updating:
            self._call('ReaderMode')

    def _on_mode(self, w, mode):
        if not self._updating and w.get_active():
            self._call('SetMode', mode)

    def _on_light(self, w, level):
        if not self._updating and w.get_active():
            self._call('SetBrightness', level)

    def _quit_all(self):
        gui = self._gui()
        if gui is not None:
            try:
                gui.Quit(timeout=5)
            except dbus.DBusException:
                pass
        Gtk.main_quit()

    # ------------------------------------------------------------------
    # State sync
    # ------------------------------------------------------------------

    def refresh(self):
        state = None
        gui = self._gui()
        if gui is not None:
            try:
                state = {str(k): v for k, v in gui.GetState(timeout=2).items()}
            except dbus.DBusException:
                state = None
        self._apply_state(state)
        return True  # keep polling

    def _apply_state(self, s):
        self._updating = True
        try:
            present = s is not None
            self.it_start.set_visible(not present)
            for w in (self.it_switch, self.it_reader, self.it_refresh, self.it_mode, self.it_light, self.it_show):
                w.set_visible(present)
            if not present:
                self.it_state.set_label("Tinta4PlusU — control window not running")
                self.it_connect.set_visible(False)
                self.ind.set_icon_full(ICONS['off'], 'Tinta4PlusU: not running')
                return

            connected = bool(s.get('connected'))
            eink = bool(s.get('eink_on'))
            reader = bool(s.get('reader_on'))
            busy = bool(s.get('switching')) or int(s.get('countdown', 0)) > 0
            self.it_connect.set_visible(not connected)

            if busy:
                text = f"Switching… {int(s.get('countdown', 0)) or ''}".rstrip()
            elif not connected:
                text = "Helper not connected"
            elif reader:
                text = "eInk · tablet reader mode"
            elif eink:
                text = f"eInk · {str(s.get('mode') or 'unknown')} mode"
            else:
                text = "OLED display"
            self.it_state.set_label(f"Tinta4PlusU — {text}")

            key = 'off' if not connected else 'reader' if reader else 'eink' if eink else 'oled'
            self.ind.set_icon_full(ICONS[key], f'Tinta4PlusU: {text}')

            self.it_switch.set_label("Switch to OLED" if eink else "Switch to eInk")
            self.it_switch.set_sensitive(connected and not busy)
            self.it_reader.set_active(reader)
            self.it_reader.set_label("Leave tablet reader mode" if reader else "Tablet reader mode")
            self.it_reader.set_sensitive(connected and not busy)
            for w in (self.it_refresh, self.it_mode):
                w.set_sensitive(connected and eink and not busy)
            self.it_light.set_sensitive(connected and eink and not busy and bool(s.get('frontlight_available', True)))

            mode = str(s.get('mode') or '')
            if mode == 'reading':
                self.rb_reading.set_active(True)
            elif mode == 'dynamic':
                self.rb_dynamic.set_active(True)
            level = int(s.get('brightness', 0))
            if 0 <= level <= 8:
                self.rb_light[level].set_active(True)
        finally:
            self._updating = False


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    if AppIndicator3 is None:
        print("ERROR: AppIndicator binding missing. Install gir1.2-ayatanaappindicator3-0.1", file=sys.stderr)
        return 1
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    if bus.name_has_owner(INDICATOR_BUS_NAME):
        log.info("Indicator already running")
        return 0
    Indicator(autostart='--autostart' in sys.argv)
    Gtk.main()
    return 0


if __name__ == '__main__':
    sys.exit(main())
