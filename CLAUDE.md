# Tinta4PlusU — Claude Code Context

## What is this project?

Tinta4PlusU (Universal) is a Linux GUI + privileged daemon for controlling the eInk display on the Lenovo ThinkBook Plus Gen 4 IRU. It runs on Ubuntu (GNOME, Cinnamon, XFCE, and KDE tested). The eInk is a **color** display (2560x1600).

This is a fork/universal version. The binary/install names use `tinta4plusu` to coexist with the original `tinta4plus`.

## Architecture

Two-process model communicating via Unix socket (`/tmp/tinta4plusu.sock`):

- **Tinta4Plus.py** — Unprivileged tkinter GUI. Launches the helper via `pkexec`. Single-instance (flock on `$XDG_RUNTIME_DIR/tinta4plusu-gui.lock`); display switches run on a worker thread, widgets are only touched on the Tk thread via `_ui()`, and all display mutations share `_display_lock`. `_eink_on` is the thread-safe mirror of the eInk state and is seeded from the daemon's `get-state` on every connect.
- **HelperDaemon.py** — Privileged daemon (needs root for EC port I/O and USB). Runs as a socket server. Single-instance (flock on `/run/lock/tinta4plusu-helper.lock`), checks `SO_PEERCRED` on every connection (root + the launching user only), caps frames at 64 KiB / clients at 8 / idle at 90 s.

### Module map

| File | Role | Runs as |
|------|------|---------|
| `Tinta4Plus.py` | Main GUI, entry point | User |
| `HelperDaemon.py` | Privileged daemon, socket server | Root (via pkexec) |
| `DisplayManager.py` | Display switching via xrandr (X11) / Mutter D-Bus (Wayland) / kscreen (KDE Wayland) | User |
| `ThemeManager.py` | Desktop theme switching (GNOME gsettings / Cinnamon gsettings / XFCE xfconf / KDE) | User |
| `HelperClient.py` | Socket client for GUI→daemon IPC (JSON, length-prefix framing) | User |
| `ECController.py` | Embedded Controller register access via portio (I/O ports 0x66/0x62) | Root |
| `EInkUSBController.py` | USB T-CON controller via pyusb (VID 0x048d, PID 0x8957) | Root |
| `WatchdogTimer.py` | 60s watchdog, triggers daemon shutdown when no client sends commands | Root |
| `toggle-eink.py` | Standalone CLI display toggle (no GUI needed) | User |
| `Indicator.py` | Top-bar indicator (GTK3/AppIndicator), drives the GUI over D-Bus | User |
| `touch_diagnostic.py` | Standalone touchscreen mapping diagnostic tool | User |

### Hardware details
- OLED: 2880x1800 on eDP-1
- eInk: 2560x1600 on eDP-2 (color)
- EC ports: 0x66 (status/cmd), 0x62 (data)
- EC registers: 0x35 (brightness PWM), 0x25 (frontlight power)

## Build & Install System

### PyInstaller (onedir mode, PyInstaller 6.19.0, Python 3.12.3)

Two spec files produce two independent onedir bundles:

- `tinta4plusu.spec` → `dist/tinta4plusu/tinta4plusu` (GUI, console=False)
  - Bundles every `eink-disable<N>.jpg` present as data (globbed, not a fixed list)
  - Hidden imports: `ThemeManager`, `DisplayManager`, `HelperClient`
- `tinta4plusu-helper.spec` → `dist/tinta4plusu-helper/tinta4plusu-helper` (daemon, console=True)
  - Hidden imports: `ECController`, `EInkUSBController`, `WatchdogTimer`

Build: `bash build.sh`

**Known issue:** PyInstaller warns `tkinter installation is broken` on Ubuntu — tkinter can't be fully bundled. The binary works if `python3-tk` is installed at runtime (handled by `installer.sh`).

### installer.sh

Run as root: `sudo bash installer.sh`

What it does:
1. Asks user to choose install mode: compiled binary (option 1) or Python scripts (option 2)
2. Detects desktop environment (3 fallback methods: env vars → loginctl for SUDO_USER session → process detection)
3. Installs apt dependencies:
   - Common: `libusb-1.0-0`, `python3-tk`, `python3-evdev`
   - Script mode adds: `python3`, `python3-usb`, `python3-pil` (thumbnails), `python3-dbus`, `python3-gi` (resume monitor, sleep inhibitor, Mutter)
   - GNOME/Cinnamon adds: `gnome-themes-extra`, `policykit-1-gnome` (required for pkexec password dialog)
   - KDE adds: `kscreen`, `plasma-workspace`
   - XFCE adds: `xfce4-settings`
4. Script mode: installs pip packages (`portio`, `pyusb`, `sv-ttk`) via pip3 with `--break-system-packages` fallback
5. Copies onedir bundles (binary mode) or .py files + images (script mode) to `/opt/tinta4plusu/`
6. Creates symlinks (binary) or wrapper scripts (script) in `/usr/local/bin/`
7. Installs `tinta4plusu.desktop` to `/usr/share/applications/`
8. Removes any `/etc/xdg/autostart/tinta4plusu-autostart.desktop` left by earlier versions — the app is deliberately not autostarted, so opening it always goes with the helper's admin password prompt
9. Optionally installs PolicyKit policy (`org.tinta4plusu.helper.policy`) for `auth_admin_keep` (user chooses at install time)
10. Verifies dependencies and warns about any missing ones

Error handling: `set -eE` with ERR trap logs the failing line number, step name, and exit code to `/tmp/tinta4plusu-install.log`.

Uninstall: `sudo bash installer.sh --uninstall`

## Key design decisions

### Display switching on GNOME (Mutter: Wayland only — X11 stays on xrandr)

**Do not route X11 through Mutter.** It was tried (5 Oct 2026): the API works on X11, but Mutter treats both eDP outputs as laptop panels. With the lid closed it refuses to activate the eInk (`Refusing to activate a closed laptop panel`) and re-applies any *stored* layout (`~/.config/monitors.xml`) — the eInk went blank right after closing the lid. Raw xrandr cannot be vetoed, and with **no** monitors.xml Mutter leaves the layout alone on lid close (first hardware test: "Lid event: reader layout intact"). So: X11 = xrandr, never apply persistently, never create monitors.xml; the layout watchdog covers GNOME's occasional extended-desktop fallback. On Wayland layouts are applied through `org.gnome.Mutter.DisplayConfig` (`DisplayManager._use_mutter_apply()`), where the same lid limitation will apply. Mutter rejects overlapping and non-adjacent two-monitor layouts, so the switch is one *atomic* `ApplyMonitorsConfig` (method 1 = **temporary**: persistent (2) makes gnome-shell show "Keep these display settings?" and *revert after 20 s* without an answer — fatal with the lid closed; `~/.config/monitors.xml` holding OLED-only was written once and is GNOME's own fallback) with the target as the sole monitor (`set_sole_output()`); the GUI uses it whenever `supports_atomic_switch()` is true (OLED-only → eInk-only, then T-CON on; on the way back privacy image, T-CON off, then eInk-only → OLED-only). `enable_display()` on Mutter places the new monitor adjacent (x = existing width in the layout mode's unit: physical px on this X11 session, `layout-mode` 2). `global-scale-required` means one scale for all monitors: the eInk gets the OLED's logical scale (2 here) × our display-scale setting, snapped to Mutter's supported values. Queries on X11 stay on xrandr. Raw xrandr changes made GNOME "forget" the layout and fall back to its default extended desktop (both panels, 5440 px wide) on the next lid/hotplug/DPMS event — that was the recurring "two screens on one" bug.

Other desktops keep the stepwise xrandr/kscreen path (enable target, disable other, re-apply target).

### Touch devices

`_get_touchscreen_xinput_ids()` returns **every slave pointer with absolute axes** (not touchpads): the OLED's Wacom finger/pen/eraser and the eInk's ITE T-CON interfaces. The eInk finger digitizer is the device the kernel names only "ITE Tech. Inc. ITE T-CON" / "… UNKNOWN" (`XITouchClass`, `Abs MT Position`) — a name filter on "touch" missed it, which left eInk touches on the landscape mapping in portrait reader mode (taps landed point-reflected). `_schedule_touch_remap()` maps again 4 s and 10 s after a switch because T-CON interfaces can enumerate late and GNOME's input mapper may overwrite the matrix. Mutter transform 1 == xrandr `left`, 3 == `right` (verified live), and the rotation matrices in `TOUCH_ROTATION_MATRIX` match.

### Layout watchdog

`_layout_watchdog_loop` (GUI, every 4 s while idle and connected) checks that exactly the expected output is active (eInk, rotated in reader mode, or OLED) and, after two consecutive mismatches, re-applies it with `set_sole_output()` and logs "Desktop changed the display layout behind our back". This catches reconfigurations GNOME performs long after a switch finished.

### Helper path resolution (`_resolve_helper_path()`)

Priority order:
1. `/usr/local/bin/tinta4plusu-helper` — installed binary (symlink to /opt)
2. `./tinta4plusu-helper` — portable binary (same dir as GUI)
3. `/usr/local/bin/HelperDaemon.py` — legacy installed script
4. `./HelperDaemon.py` — dev script (same dir)

Uses `sys.frozen` to detect PyInstaller mode and resolve `base_dir` accordingly (`sys.executable` dir for frozen, `__file__` dir for script).

### pkexec auto-detection (`_launch_helper_thread()`)

- If helper path ends in `.py` → `pkexec python3 <path>`
- If helper is a binary → `pkexec <path>` (no python3 prefix)
- Requires `policykit-1-gnome` on GNOME/Cinnamon for the password dialog agent

### T-CON availability and USB access (`HelperDaemon._eink_op()`)

- The daemon starts even if the T-CON USB device is missing (EC-only mode: frontlight works, eInk commands return "E-Ink T-CON not available").
- Every T-CON operation goes through `_eink_op()`, which holds `_usb_lock` (socket clients, the hotkey thread and the HTTP API all reach the device) and calls `EInkUSBController.ensure_connected()` first.
- `ensure_connected()` reconnects only when there is no handle or the device re-enumerated (bus/address changed, typically after suspend). It never issues a USB reset: a reset makes the T-CON show its boot splash.
- The GUI sends `reconnect-usb` after resume; any later eInk command would also trigger the reconnect.

### Privacy images

When the eInk is disabled, the app switches to dynamic mode first, then displays a privacy image fullscreen before powering off the T-CON. This clears any sensitive content from the eInk.

Images are discovered at startup (`discover_privacy_images()`: every `eink-disable<N>.jpg` next to the code, sorted numerically), so adding or removing a file is enough — no list to edit. Shipped in git:

- `eink-disable1.jpg` — "AIME-TOI COMME TU ES!" (teal)
- `eink-disable2.jpg` — "La vie est belle!" (purple)
- `eink-disable3.jpg` — "Vive l'amour." (teal)
- `eink-disable5.jpg` … `eink-disable17.jpg` — geometric/abstract designs generated by `tools/generate_privacy_images.py` (Pillow only, fixed seeds, 2× supersampled). Colour themes are the Plume keyboard presets: plume-light, plume-dark, catppuccin-mocha, emerald, sunflower, deep-sea-light, deep-sea-dark, snowfall, steel-grey, cotton-candy, amoled-purple, high-contrast-yellow, gradient-aurora. Re-run the script after editing a palette or pattern; add new designs to its `IMAGES` list (next free index).

`eink-disable4.jpg` is a personal image, git-ignored, and stays on this machine only. All 2560x1600, matching the eInk panel resolution exactly. The original `eink-disable.jpg` (Tux penguin) is the README illustration, not a privacy image.

The GUI exposes a "Privacy image" dropdown (under Display Control) that lets the user pin a specific filename instead of the default random pick. Stored in settings as `privacy_image` (`'random'` or one of the filenames); `toggle-eink.py` honors the same setting.

Image resolution in frozen mode uses `sys._MEIPASS` (PyInstaller `_internal/` directory).

### Keyboard shortcuts

- **Super+P** (Fn+F7): Toggle eInk/OLED — always active when daemon is running. The listener grabs the keyboard device so the DE's display projection dialog is suppressed. 2-second debounce prevents rapid fire.
- **Super+Shift+P**: Tablet reader mode on/off (listener tracks Shift state; Shift itself is forwarded)
- **Help** (Fn+F9): Refresh eInk (clear ghosts) — only when eInk enabled
- **XF86MonBrightnessUp** (Fn+F6): Increase frontlight brightness — only when eInk enabled
- **XF86MonBrightnessDown** (Fn+F5): Decrease frontlight brightness — only when eInk enabled

These work both in the tkinter GUI (`bind_all`) and globally via `GlobalHotkeyListener` (evdev, runs in the helper daemon as root). The listener holds Super back until it knows whether P follows; any other key (or a Super auto-repeat) first injects the pending Super-down so Super+L / Super+Tab / Super+drag reach the desktop intact. Brightness/Help keys are only swallowed when the daemon callback returns True (eInk on); otherwise they pass through to the desktop. Callbacks' hardware work runs on a worker thread (`run_async`) so key forwarding never stalls. Its own `tinta4plusu-fwd-*` UInput mirrors are skipped when scanning devices.

### OLED wake sequence

When switching back to OLED, the app forces DPMS on (`xset dpms force on`; on Wayland deactivates `org.gnome.ScreenSaver` and `loginctl activate`s its own session). It deliberately never calls `loginctl unlock-session`: Super+P is handled by a root evdev listener and therefore fires from the lock screen, so unlocking here would bypass the lock screen.

### Tablet reader mode

`_enter_reader_sequence` = (enable eInk if needed) + `_apply_reader_layout(on=True)`: `enable_display(eDP-2, scale, rotation)`, `map_touch_to_display(eDP-2, rotation)` (X11 sets the *Coordinate Transformation Matrix* = output area × rotation; pen/stylus/eraser devices are mapped too), helper `set-reading`, then inhibitors: logind `handle-lid-switch` (block), `org.freedesktop.ScreenSaver.Inhibit` (idle), and on GNOME the gsettings `lid-close-{ac,battery}-action=nothing` + `orientation-lock=true` with the originals saved in the settings file (`reader_backup`) and restored on exit or on the next start after a crash. `reader_active` is persisted so a restarted GUI re-acquires the inhibitors.

Leaving (`_leave_reader_sequence`, also what Super+P / the window close do while reading) = `_disable_eink_sequence`, which first calls `_apply_reader_layout(on=False)` so the eInk is landscape **before** the privacy image is drawn.

`READER_GSETTINGS` also sets `org.gnome.desktop.screensaver lock-enabled=false` (gsd locks on lid close when it does not suspend — observed on the first hardware test, undismissable with the keyboard under the lid) and `a11y.applications screen-keyboard-enabled=true` as a safety net. X11 rotation: `RRSetPanning` is refused on a rotated CRTC, so `_apply_xrandr_enable` passes `--panning` only for `normal`; `_fit_framebuffer_x11()` shrinks the screen once the rotated output is alone.

Lid events come from UPower `PropertiesChanged(LidIsClosed)` on the resume-monitor D-Bus thread (`/proc/acpi/button/lid` in the poll fallback): closed → `_reassert_reader_layout()` 2.5 s later (Mutter treats both eDP outputs as laptop panels and may reconfigure on lid close); opened → `reader_off` if `reader_lid_open_exits`. `ResumeCheck.run(eink_rotation=...)` keeps the rotation across suspend. Hotkey: Super+Shift+P → daemon notification `{'type': 'reader'}`.

### eInk Reader (git submodule `reader/`)

The Lector fork lives in its own repository (https://github.com/funkypitt/eink-reader) and is vendored here as a submodule so one clone/installer gives the whole setup. `installer.sh` offers to run `reader/installer.sh`. Reader mode launches `eink-reader --fullscreen` (`_launch_reader_app`, setting `reader_open_app`) after the layout is applied; the reader has Kindle-style touch navigation (edge taps / swipes, centre double-tap = fullscreen). Bump the submodule pointer after pushing reader changes: `git submodule update --remote reader && git commit -am "Update reader"`.

### Top-bar indicator and D-Bus control

`Indicator.py` (GTK3 + AyatanaAppIndicator3, separate process, `tinta4plusu-indicator`) owns the session name `org.tinta4plusu.Indicator` and polls the GUI every 2 s. The GUI exports `org.tinta4plusu.Gui` at `/org/tinta4plusu/Gui` (`_make_dbus_service`, created on the resume-monitor thread that runs the GLib loop; handlers hop to Tk via `_ui()`): `GetState`, `Toggle`, `ReaderMode`, `Refresh`, `SetMode(s)`, `SetBrightness(i)`, `Connect`, `Show`, `Hide`, `Quit`. Gotcha: `dbus.service.Object.__init__` overwrites `self._name`, so the `BusName` must be passed to it / kept under another attribute or the name is released by GC. The GUI starts the indicator (`_ensure_indicator`, setting `indicator`), hides instead of quitting on window close while an indicator runs (`close_to_indicator`; Ctrl+Q quits), and a second `tinta4plusu` launch asks the running one to `Show()`. `--hidden` starts withdrawn (used by the indicator's login autostart `tinta4plusu-indicator.desktop`, which the installer offers to put in `/etc/xdg/autostart`). Icons: `icons/tinta4plusu-{off,oled,eink,reader}-symbolic.svg` (recoloured by the panel because of the `-symbolic` suffix).

### Black-screen guards

`DisplayManager.disable_display()` refuses to turn off the only active output. The enable sequence rolls back (eInk output off, theme restored) if eDP-2 or the T-CON cannot be enabled; the disable sequence always re-enables the OLED even when the T-CON step fails. The sleep inhibitor is released in a `finally`. Closing the GUI while eInk is active switches back to OLED first.

### GUI design notes

- sv_ttk dark theme. Text size (Settings → Normal/Large/Larger, default Large = 1.2×) is applied by rescaling sv_ttk's named fonts (`SunValley*Font`, pixel sizes) in `_apply_text_size()`; our own fonts derive from `SunValleyBodyFont` — never from `TkDefaultFont`, which Tk scales independently and renders much larger. Use `Card.TFrame`, `Accent.TButton`, `Toggle.TButton`, `Switch.TCheckbutton`.
- Layout: header with status chips → "Active display" card (one big accent button, countdown shown in the button, Esc cancels) → "eInk" card (mode toggles, refresh, frontlight −/slider/+, auto-refresh) → Notebook (Settings | Activity) → footer status line.
- `python3 Tinta4Plus.py --ui-preview[=connected|eink][:settings|activity]` renders the UI with hardware, helper and monitors disabled.

## File inventory

### Source (tracked in git)
- `Tinta4Plus.py`, `HelperDaemon.py`, `DisplayManager.py`, `ThemeManager.py`, `HelperClient.py`, `ECController.py`, `EInkUSBController.py`, `WatchdogTimer.py`
- `toggle-eink.py` (standalone CLI display toggle)
- `Indicator.py`, `icons/tinta4plusu-*-symbolic.svg`, `tinta4plusu-indicator.desktop` (top-bar indicator + login autostart)
- `touch_diagnostic.py` (standalone touchscreen mapping diagnostic)
- `eink-disable1.jpg`–`eink-disable3.jpg` (text privacy images), `eink-disable5.jpg`–`eink-disable17.jpg` (generated designs), `tools/generate_privacy_images.py` (their generator); `eink-disable4.jpg` is local-only (.gitignore)
- `eink-disable.jpg` (README illustration, unused by code)
- `tinta4plusu.spec`, `tinta4plusu-helper.spec`
- `build.sh`, `installer.sh`
- `tinta4plusu.desktop`
- `tinta4plusu-autostart.desktop` (no longer installed; kept for manual use with `--autostart`)
- `tcon-protocol.md` (T-CON USB protocol notes from Windows captures)
- `org.tinta4plusu.helper.policy`

### Generated (in .gitignore)
- `build/` — PyInstaller work directory
- `dist/` — PyInstaller output (the binaries)

## Conventions
- The project does not use a virtualenv — system Python 3.12.3 with system packages. Wrappers, installer checks, pip installs and the pkexec launch all name `/usr/bin/python3` explicitly: a pyenv/conda/uv `python3` first in PATH would otherwise run the app on an interpreter without `python3-tk`/`dbus`/`gi` (tester report, Oct 2026).
- Dependencies: `python3-tk`, `pyusb`, `portio`, `sv-ttk`, `libusb-1.0-0`, `policykit-1-gnome` (GNOME/Cinnamon)
- GUI uses sv-ttk dark theme
- GUI log: `~/.cache/Tinta4PlusU/gui.log` (rotating, 1 MB × 3) + console. Helper log: `/var/log/tinta4plusu-helper.log` (0644). Nothing is written to predictable `/tmp` paths except the socket.
- Socket path: `/tmp/tinta4plusu.sock` (0600, owned by the launching user; peer-cred checked). Daemon lock/pid: `/run/lock/tinta4plusu-helper.lock`, `/run/tinta4plusu-helper.pid`.
- Config dir: `~/.config/Tinta4PlusU` (`settings` JSON keys: display_scale, refresh_period, autoswitch_theme, flip_countdown, privacy_image, floating_button, text_size, reader_rotation, reader_lid_open_exits, reader_active, reader_backup, indicator, close_to_indicator, reader_open_app)
- `HelperClient.disconnect(shutdown_helper=...)`: only the process that launched the daemon passes True.
- `ECController` serialises every EC transaction with an RLock; `EInkUSBController` access is serialised by the daemon's `_usb_lock`.
- Commit messages: imperative mood, concise summary line, details in body if needed
