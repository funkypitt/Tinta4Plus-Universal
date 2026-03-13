# Tinta4PlusU — Claude Code Context

## What is this project?

Tinta4PlusU (Universal) is a Linux GUI + privileged daemon for controlling the eInk display on the Lenovo ThinkBook Plus Gen 4 IRU. It runs on Ubuntu (GNOME, Cinnamon, XFCE, and KDE tested). The eInk is a **color** display (2560x1600).

This is a fork/universal version. The binary/install names use `tinta4plusu` to coexist with the original `tinta4plus`.

## Architecture

Two-process model communicating via Unix socket (`/tmp/tinta4plusu.sock`):

- **Tinta4Plus.py** — Unprivileged tkinter GUI. Launches the helper via `pkexec`.
- **HelperDaemon.py** — Privileged daemon (needs root for EC port I/O and USB). Runs as a socket server.

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
| `WatchdogTimer.py` | 20s watchdog, triggers daemon shutdown on timeout | Root |
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
  - Bundles: `eink-disable1.jpg`, `eink-disable2.jpg`, `eink-disable3.jpg` as data
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
   - Common: `libusb-1.0-0`, `python3-tk`
   - Script mode adds: `python3`, `python3-usb`
   - GNOME/Cinnamon adds: `gnome-themes-extra`, `policykit-1-gnome` (required for pkexec password dialog)
   - KDE adds: `kscreen`, `plasma-workspace`
   - XFCE adds: `xfce4-settings`
4. Script mode: installs pip packages (`portio`, `pyusb`, `sv-ttk`) via pip3 with `--break-system-packages` fallback
5. Copies onedir bundles (binary mode) or .py files + images (script mode) to `/opt/tinta4plusu/`
6. Creates symlinks (binary) or wrapper scripts (script) in `/usr/local/bin/`
7. Installs `tinta4plusu.desktop` to `/usr/share/applications/`
8. Installs `tinta4plusu-autostart.desktop` to `/etc/xdg/autostart/`
9. Optionally installs PolicyKit policy (`org.tinta4plusu.helper.policy`) for `auth_admin_keep` (user chooses at install time)
10. Verifies dependencies and warns about any missing ones

Error handling: `set -eE` with ERR trap logs the failing line number, step name, and exit code to `/tmp/tinta4plusu-install.log`.

Uninstall: `sudo bash installer.sh --uninstall`

## Key design decisions

### Display switching on Wayland (Mutter)

When enabling the eInk on Wayland, the display is placed at `(0, 0)` — overlapping the OLED — rather than extending to the right. This avoids a visible extended-desktop state (empty wallpaper without dock) during the brief period before the OLED is disabled.

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

### Privacy images

When the eInk is disabled, the app switches to dynamic mode first, then displays a random image from `EINK_DISABLED_IMAGES` fullscreen before powering off the T-CON. This clears any sensitive content from the eInk.

- `eink-disable1.jpg` — "AIME-TOI COMME TU ES!" (teal)
- `eink-disable2.jpg` — "La vie est belle!" (purple)
- `eink-disable3.jpg` — "Vive l'amour." (teal)

All 2560x1600, matching the eInk panel resolution exactly. The original `eink-disable.jpg` (Tux penguin) is no longer referenced by code but still in the repo.

Image resolution in frozen mode uses `sys._MEIPASS` (PyInstaller `_internal/` directory).

### Keyboard shortcuts

- **Help** (Fn+F9): Refresh eInk (clear ghosts)
- **XF86MonBrightnessUp** (Fn+F6): Increase frontlight brightness
- **XF86MonBrightnessDown** (Fn+F5): Decrease frontlight brightness

These work both in the tkinter GUI (`bind_all`) and globally via `GlobalHotkeyListener` (evdev, runs in the helper daemon as root). Only active when eInk is enabled.

### OLED wake sequence

When switching back to OLED, the app forces DPMS on (`xset dpms force on`), unlocks the session via D-Bus (`org.gnome.ScreenSaver` / `org.freedesktop.ScreenSaver`), and logs activity to the GUI.

## File inventory

### Source (tracked in git)
- `Tinta4Plus.py`, `HelperDaemon.py`, `DisplayManager.py`, `ThemeManager.py`, `HelperClient.py`, `ECController.py`, `EInkUSBController.py`, `WatchdogTimer.py`
- `touch_diagnostic.py` (standalone touchscreen mapping diagnostic)
- `eink-disable1.jpg`, `eink-disable2.jpg`, `eink-disable3.jpg` (privacy images)
- `eink-disable.jpg` (original, unused by code)
- `tinta4plusu.spec`, `tinta4plusu-helper.spec`
- `build.sh`, `installer.sh`
- `tinta4plusu.desktop`, `tinta4plusu-autostart.desktop`
- `org.tinta4plusu.helper.policy`

### Generated (in .gitignore)
- `build/` — PyInstaller work directory
- `dist/` — PyInstaller output (the binaries)

## Conventions
- The project does not use a virtualenv — system Python 3.12.3 with system packages
- Dependencies: `python3-tk`, `pyusb`, `portio`, `sv-ttk`, `libusb-1.0-0`, `policykit-1-gnome` (GNOME/Cinnamon)
- GUI uses sv-ttk dark theme
- Logging goes to `/tmp/tinta4plusu.log` (overwrite mode) + console
- Socket path: `/tmp/tinta4plusu.sock`
- Config dir: `~/.config/Tinta4PlusU`
- Commit messages: imperative mood, concise summary line, details in body if needed
