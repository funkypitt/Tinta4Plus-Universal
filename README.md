# Tinta4PlusU (Universal)

Linux GUI for controlling the **color eInk display** on the **Lenovo ThinkBook Plus Gen 4 IRU**.

This is a universal fork of [Tinta4Plus](https://github.com/joncox123/Tinta4Plus) by Jon Cox, with broader desktop environment support and a system installer.

[![Buy Me A Coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&slug=joncox&button_colour=FFDD00&font_colour=000000&font_family=Inter&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/joncox)

<img src="eink-disable.jpg" alt="ThinkBook Plus Gen 4 eInk" width="60%"/>

## Supported configurations

| Desktop | Session | Status |
|---------|---------|--------|
| GNOME | X11 | Tested |
| GNOME | Wayland | Supported (Mutter D-Bus) |
| Cinnamon | X11 | Supported |
| XFCE | X11 | Tested |
| KDE Plasma | X11 | Supported |
| KDE Plasma | Wayland | Supported (kscreen) |

Only **GNOME on X11** and **XFCE on X11** have been properly tested. Other configurations are supported but may have issues.

Base OS: **Ubuntu 24.04 LTS** or later (including Xubuntu, Kubuntu, Linux Mint).

## Hardware

- OLED: 2880x1800 on eDP-1
- eInk: 2560x1600 color on eDP-2
- eInk T-CON controller: USB (VID `048d`, PID `8957`)
- Embedded Controller: I/O ports `0x66`/`0x62` (frontlight, brightness)

## Quick start

### 1. Clone the repository

```bash
git clone https://github.com/Tinta4Plus-Universal/Tinta4Plus-Universal.git
cd Tinta4Plus-Universal
```

### 2. Disable Secure Boot

Frontlight control requires EC access, which needs Secure Boot disabled:

1. Reboot and press **Enter** repeatedly right after power-on to get the boot menu.
2. Press the appropriate F-key to enter BIOS settings.
3. Navigate to **Security** > **Secure Boot** > set to **Disabled**.
4. Save and reboot.

### 3. Install

```bash
sudo bash installer.sh
```

The installer will prompt you to choose between two modes:

#### Python scripts (recommended)

Choose **option 2** (Python scripts) when prompted. This installs the Python source files directly — easier to debug, modify, and update. The installer handles all dependencies automatically.

#### Compiled binaries

Choose **option 1** (compiled binary) when prompted. Requires building first:

```bash
pip install pyinstaller
bash build.sh
sudo bash installer.sh
```

### What the installer does

1. Asks you to choose between compiled binary or Python script mode.
2. Detects your desktop environment (GNOME, Cinnamon, XFCE, KDE) using three fallback methods: environment variables, loginctl session query, and process detection.
3. Installs apt dependencies:
   - **Common**: `libusb-1.0-0`, `python3-tk`, `python3-evdev`
   - **Script mode** adds: `python3`, `python3-usb`, `python3-pil`, `python3-dbus`, `python3-gi`
   - **GNOME/Cinnamon** adds: `gnome-themes-extra`, `policykit-1-gnome` (required for pkexec password dialog)
   - **KDE** adds: `kscreen`, `plasma-workspace`
   - **XFCE** adds: `xfce4-settings`
4. In script mode, installs pip packages: `portio`, `pyusb`, `sv-ttk`.
5. Copies binaries/scripts to `/opt/tinta4plusu/` and creates symlinks in `/usr/local/bin/`.
6. Installs `tinta4plusu.desktop` to `/usr/share/applications/`. The app is not started at login; an autostart entry left by an earlier version is removed.
7. Optionally installs a PolicyKit policy (`org.tinta4plusu.helper.policy`) to cache authentication so you don't re-enter your password every time the helper starts.

Errors during installation are trapped and logged to `/tmp/tinta4plusu-install.log`.

### 4. Launch

After installation, launch from the terminal or application menu:

```bash
tinta4plusu
```

The app does not start at login. Opening it launches the helper daemon, which asks for an administrator password.

Only one copy of the GUI runs at a time: launching it again brings up a small "already running" notice and the launcher icon focuses the existing window.

## Usage

### Switching displays

There are three ways to switch between OLED and eInk:

1. **GUI button** — click **Switch to eInk** / **Switch to OLED**. A countdown (configurable, default 5 s) gives you time to flip the lid; press **Esc** or click the button again to cancel.
2. **Super+P (Fn+F7)** — press the display-switch key. Works system-wide whenever the helper daemon is running. The key is grabbed by the daemon, so the desktop's built-in display projection dialog is suppressed. When the daemon stops, Super+P returns to normal.
3. **CLI toggle** — run `toggle-eink` from a terminal or bind it to any keyboard shortcut in your desktop environment settings.

The switching sequence:

- **To eInk**: enables eDP-2, powers on the T-CON, enables the frontlight, sets dynamic mode, then disables eDP-1. If the eInk output or the T-CON cannot be enabled, the switch is rolled back and the OLED stays on. On Wayland, the eInk is placed at the same position as the OLED (mirror-like) to avoid a visible extended-desktop state during the transition.
- **To OLED**: switches to dynamic mode, shows a privacy image on eInk (to clear sensitive content), powers off the T-CON, re-enables eDP-1, wakes the panel, then disables eDP-2. The OLED is re-enabled even if the T-CON step fails, and an output is never turned off while it is the only active one.

The switch runs in the background; the window stays responsive and shows progress under the button. Closing the window while eInk is active switches back to OLED first.

### Tablet reader mode

For reading long documents (a PDF magazine, a book) with the laptop closed and held like a tablet:

1. Click **📖 Tablet reader mode** (or press **Super+Shift+P**). The app switches to eInk, rotates it to portrait (touch and pen follow), selects *Reading* mode and keeps the machine awake: closing the lid no longer suspends and the screen does not blank.
2. Close the lid — the eInk now faces you — and read. If the desktop tries to re-enable the OLED or undo the rotation on the lid event, the app puts the reader layout back within a few seconds.
3. Open the lid (or click **Leave tablet reader mode** / Super+Shift+P). The eInk goes back to landscape and *Dynamic* mode, the privacy image is shown upright, the T-CON is powered off and the OLED returns. Lid-close behaviour and auto-rotation are restored to what they were.

Settings → *Tablet reader mode* lets you choose the orientation (portrait left / portrait right / landscape) and whether opening the lid leaves reader mode. Reader mode survives a GUI restart (the inhibitors are re-acquired) and a crash (the overridden GNOME lid settings are restored on the next start).

### eInk display modes

- **Reading mode**: optimized for text, slower refresh, less ghosting.
- **Dynamic mode**: faster refresh for scrolling/interaction, more ghosting.

### Refreshing the display (clearing ghosts)

eInk panels accumulate ghosting (afterimages) from partial updates. You can clear it with:

- **Refresh button**: click **⟳ Refresh** in the eInk card (or the floating ⟳ button that appears on the left edge while eInk is active — it can be dragged anywhere, and switched off in Settings).
- **Auto-refresh**: the slider runs a full refresh every 5–60 seconds (off by default).

### Privacy images

When the eInk is switched off it keeps its last frame, so the app first shows a full-screen privacy image. Pick one in **Settings → Privacy image** (with a thumbnail) or leave it on *Random*. Sixteen ship with the app: three text cards and thirteen geometric/abstract designs whose colour themes come from the [Plume keyboard](https://github.com/funkypitt/clavier-plume) presets — Plume light/dark, Catppuccin Mocha, Emerald, Sunflower, Deep Sea light/dark, Snowfall, Steel Grey, Cotton Candy, AMOLED Purple, High-Contrast Yellow and Aurora.

Images are discovered at startup: any `eink-disable<N>.jpg` (2560×1600) next to the app is offered, so drop your own file in `/opt/tinta4plusu/` to add one. The generated set is reproducible with `python3 tools/generate_privacy_images.py`.

### Frontlight

Use the frontlight slider or the − / + buttons (0-8) to control the eInk frontlight. The frontlight turns on automatically when switching to eInk and off when switching back to OLED. Setting brightness to **0 fully disables the frontlight** (rather than setting PWM to 0, which can produce a faint glow on some panels). Moving brightness back to any non-zero value re-enables it.

### Global keyboard shortcuts

These shortcuts work system-wide (via evdev in the helper daemon) whenever the daemon is running. While eInk is off, the brightness keys reach the desktop as usual and control the OLED; every other Super+key shortcut (Super+L, Super+Tab, …) is untouched.

| Shortcut | Action | When |
|----------|--------|------|
| **Super+P** (Fn+F7) | Toggle eInk/OLED | Always (daemon running) |
| **Super+Shift+P** | Tablet reader mode on/off | Always (daemon running) |
| **Help** (Fn+F9) | Refresh eInk (clear ghosts) | eInk enabled |
| **Brightness Up** (Fn+F6) | Increase frontlight brightness | eInk enabled |
| **Brightness Down** (Fn+F5) | Decrease frontlight brightness | eInk enabled |

### Display scaling

The **eInk scale** slider (Settings tab) controls the UI scale on the eInk display (default: 1.0×, applied on the next switch). On X11 this sets the xrandr scale and panning dimensions. On Wayland (Mutter), it uses the closest supported fractional scale.

### Theme auto-switching

When **High-contrast theme while eInk is active** is switched on (off by default), the app switches to a high-contrast theme on eInk and back to Adwaita-dark on OLED. The GUI itself uses a dark theme (sv-ttk).

### Settings persistence

Settings (eInk scale, auto-refresh period, flip countdown, privacy image, theme auto-switch, floating button) are saved to `~/.config/Tinta4PlusU/settings` (JSON) and restored on next launch.

### Touch diagnostic tool

A standalone diagnostic tool (`touch_diagnostic.py`) is included to test touchscreen mapping accuracy on the eInk display. Run it while in eInk mode:

```bash
python3 touch_diagnostic.py
```

It shows targets on screen and reports the offset between expected and actual touch positions.

### CLI toggle tool

The `toggle-eink` command switches between OLED and eInk without the GUI. It connects to the running helper daemon, queries the current state, and performs the full switch sequence (including privacy image, frontlight, touch mapping).

```bash
toggle-eink            # Toggle between eInk and OLED
toggle-eink --status   # Print current display state
```

You can bind `toggle-eink` to any keyboard shortcut in your desktop environment settings for instant switching. The helper daemon must be running (either via the GUI or standalone with `pkexec tinta4plusu-helper`).

## Developing

Run the GUI without touching any hardware to work on the interface:

```bash
python3 Tinta4Plus.py --ui-preview                  # disconnected state
python3 Tinta4Plus.py --ui-preview=connected         # helper connected, OLED
python3 Tinta4Plus.py --ui-preview=eink:settings     # eInk active, Settings tab
```

## Uninstalling

```bash
sudo bash installer.sh --uninstall
```

This removes binaries/scripts from `/opt/tinta4plusu`, symlinks from `/usr/local/bin`, desktop entries, and the PolicyKit policy.

## Architecture

Two-process model communicating via Unix socket (`/tmp/tinta4plusu.sock`):

- **Tinta4Plus.py** — unprivileged tkinter GUI, launched as the user.
- **HelperDaemon.py** — privileged daemon (root via `pkexec`), controls EC and USB hardware.

| Module | Role | Runs as |
|--------|------|---------|
| `Tinta4Plus.py` | Main GUI | User |
| `HelperDaemon.py` | Privileged daemon | Root |
| `DisplayManager.py` | Display switching (xrandr / Mutter D-Bus / kscreen) | User |
| `ThemeManager.py` | GTK/desktop theme switching (GNOME, Cinnamon, XFCE, KDE) | User |
| `HelperClient.py` | Socket IPC client | User |
| `ECController.py` | Embedded Controller I/O | Root |
| `EInkUSBController.py` | USB T-CON controller | Root |
| `WatchdogTimer.py` | Daemon watchdog (60s without a client command) | Root |
| `toggle-eink.py` | CLI display toggle (no GUI needed) | User |
| `touch_diagnostic.py` | Touchscreen mapping diagnostic | User |

### Security model

- Exactly one helper daemon runs at a time (`flock` on `/run/lock/tinta4plusu-helper.lock`); a second copy exits instead of taking over the first one's socket.
- The socket is owned by the user who launched the daemon (mode 0600) and every connection is checked with `SO_PEERCRED`: only root and that user are served. The GUI, in turn, refuses a socket that is not served by root.
- Frames are capped at 64 KiB, idle clients are dropped after 90 s and at most 8 clients are served, so an unprivileged process cannot exhaust the root daemon.
- Only the process that launched the daemon asks it to shut down on exit; other clients (`toggle-eink`, a GUI attached to an existing daemon) just disconnect and the watchdog stops the daemon once nobody is talking to it.
- The daemon never unlocks the session. Super+P works from the lock screen only in the sense that the request is queued; the display switch itself runs in the user's session.

## PolicyKit

During installation you can optionally install a PolicyKit policy (`org.tinta4plusu.helper.policy`) that caches authentication so you don't need to re-enter your password every time the helper starts. The first launch still requires authentication.

On GNOME and Cinnamon, the `policykit-1-gnome` package is required for `pkexec` to show a password dialog. The installer installs this automatically.

## Troubleshooting

### Quick health check

Run these three commands before anything else:

```bash
pkexec whoami          # a password dialog appears, then prints "root"
lsusb | grep 048d      # one line containing 048d:8957 (the eInk T-CON)
mokutil --sb-state     # "SecureBoot disabled" (needed for the frontlight)
```

If the first one shows no dialog, see the pkexec section below. If the second prints nothing, see "eInk commands fail" below. If Secure Boot is enabled, disable it in the BIOS.

When asking for help, include the last lines of the app's Activity tab (there is a **Copy** button) and the output of `tail -30 /var/log/tinta4plusu-helper.log` (helper) and `tail -30 ~/.cache/Tinta4PlusU/gui.log` (GUI).

### eInk commands fail with "E-Ink T-CON not available"

The T-CON USB device (`048d:8957`) was not found. The helper still starts and the frontlight still works. The helper looks for the T-CON again on every eInk command and after resume, so once `lsusb | grep 048d` shows the device, retry. If the device is missing after suspend, close the lid, wait a few seconds and reopen it.

### "Failed to launch helper — password cancelled or pkexec failed"

The polkit authentication agent is not running. On GNOME/Cinnamon, install `policykit-1-gnome`:

```bash
sudo apt install policykit-1-gnome
```

Then log out and back in, or start it manually:

```bash
/usr/lib/policykit-1-gnome/polkit-gnome-authentication-agent-1 &
```

### Black screen after switching back to OLED

The app forces DPMS on and re-activates the session after re-enabling eDP-1, but if the OLED stays black, close and reopen the laptop lid to wake it. Super+P still works to retry the switch.

### Frontlight error on enable

Sometimes the EC register readback differs from the written value. The frontlight is usually enabled despite the error — check visually.

### EC reset procedure

If the laptop becomes unresponsive or the eInk/EC behaves erratically:

1. Power off and disconnect the AC adapter.
2. Press and **hold** the EC reset pinhole (bottom of laptop, near the fan vent) for **60 seconds**.
3. Press and **hold** the power button for **60 seconds**.
4. Press the power button normally to boot (may take up to 60 seconds to show anything on screen).
5. Re-check BIOS to ensure Secure Boot is still disabled.

## Warning and disclaimer

**This software is experimental. No warranty is offered, express or implied.**

This software was independently developed without any input, support, or documentation from eInk or Lenovo. It writes to low-level hardware (Embedded Controller, USB T-CON) and can potentially cause temporary or permanent hardware damage. It has been tested on a limited number of systems.

**Do not modify `ECController.py` or `EInkUSBController.py`** unless you understand the hardware implications.

Use entirely at your own risk. The authors accept no liability for any damage to your hardware or data. See the full [EULA](README_EULA_INSTRUCTIONS_WARNINGS.txt) for details.

## Credits

Original project by [Jon Cox](https://github.com/joncox123) — [Buy him a coffee](https://www.buymeacoffee.com/joncox)
