# Tinta4PlusU (Universal)

Linux GUI for controlling the **color eInk display** on the **Lenovo ThinkBook Plus Gen 4 IRU**.

This is a universal fork of [Tinta4Plus](https://github.com/joncox123/Tinta4Plus) by Jon Cox, with broader desktop environment support and a system installer.

[![Buy Me A Coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&slug=joncox&button_colour=FFDD00&font_colour=000000&font_family=Inter&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/joncox)

<img src="eink-disable.jpg" alt="ThinkBook Plus Gen 4 eInk" width="60%"/>

## Supported configurations

| Desktop | Session | Status |
|---------|---------|--------|
| GNOME | X11 | Tested |
| GNOME | Wayland | Tested (Mutter D-Bus) |
| XFCE | X11 | Tested |
| KDE Plasma | X11 | Supported |
| KDE Plasma | Wayland | Supported (kscreen) |

Base OS: **Ubuntu 24.04 LTS** or later (including Xubuntu, Kubuntu).

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

There are two ways to install: **compiled binaries** (recommended) or **Python scripts** (easier to debug/modify).

#### Option A: Compiled binaries (recommended)

Build first, then install:

```bash
# Install build dependency
pip install pyinstaller

# Build the binaries
bash build.sh

# Install system-wide
sudo bash installer.sh
# Choose option 1 (compiled binary) when prompted
```

#### Option B: Python scripts (development)

```bash
sudo bash installer.sh
# Choose option 2 (Python scripts) when prompted
```

Or run directly without installing:

```bash
# Install dependencies manually
sudo apt install python3-tk python3-usb libusb-1.0-0 feh
pip install portio pyusb sv-ttk

# Run
./Tinta4Plus.py
```

### 4. Launch

After installation, launch from the terminal or application menu:

```bash
tinta4plusu
```

The app also autostarts on login (via `/etc/xdg/autostart/`). In autostart mode, the helper daemon is **not** launched automatically to avoid a password prompt at login — click **Connect to Helper** when you need eInk control.

## Usage

### Switching displays

Click the **eInk Enabled/Disabled** toggle button to switch between OLED and eInk. The switching sequence:

- **To eInk**: enables eDP-2, powers on the T-CON, enables frontlight, sets reading mode, then disables eDP-1.
- **To OLED**: shows a privacy image on eInk (to clear sensitive content), powers off the T-CON, re-enables eDP-1, then disables eDP-2.

### eInk display modes

- **Reading mode**: optimized for text, slower refresh, less ghosting.
- **Dynamic mode**: faster refresh for scrolling/interaction, more ghosting.

### Refreshing the display (clearing ghosts)

eInk panels accumulate ghosting (afterimages) from partial updates. You can clear it with:

- **Refresh button**: click "Refresh eInk (Clear Ghosts)" in the GUI.
- **Keyboard shortcuts**: press **F5** or **Ctrl+R** while the app is focused.
- **Periodic auto-refresh**: adjust the "Refresh period" slider (0 = off, up to 60 seconds). Defaults to off.

### Frontlight

Use the brightness slider (0–8) to control the eInk frontlight. The frontlight turns on automatically when switching to eInk and off when switching back to OLED.

### Display scaling

The "Display Scale" slider controls the UI scale on the eInk display (default: 1.75x). This sets the xrandr scale and panning dimensions so that the desktop fits the eInk panel.

### Theme auto-switching

When "Auto-switch theme" is checked (default), the app switches to a high-contrast theme on eInk and back to Adwaita-dark on OLED.

### Settings persistence

Settings (display scale, refresh period, theme auto-switch) are saved to `~/.config/Tinta4PlusU/settings.json` and restored on next launch.

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
| `ThemeManager.py` | GTK theme switching | User |
| `HelperClient.py` | Socket IPC client | User |
| `ECController.py` | Embedded Controller I/O | Root |
| `EInkUSBController.py` | USB T-CON controller | Root |
| `WatchdogTimer.py` | Daemon watchdog (20s timeout) | Root |

## PolicyKit

During installation you can optionally install a PolicyKit policy (`org.tinta4plusu.helper.policy`) that caches authentication so you don't need to re-enter your password every time the helper starts. The first launch still requires authentication.

## Troubleshooting

### Black screen after switching back to OLED

The app forces DPMS on after re-enabling eDP-1, but if the OLED stays black, close and reopen the laptop lid to wake it.

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

This software was independently developed without any input, support, or documentation from eInk or Lenovo. It writes to low-level hardware (Embedded Controller, USB T-CON) and can potentially cause temporary or permanent hardware damage. It has been tested on a limited number of systems.

**Do not modify `ECController.py` or `EInkUSBController.py`** unless you understand the hardware implications.

Use at your own risk. See the full [EULA](README_EULA_INSTRUCTIONS_WARNINGS.txt) for details.

## Credits

Original project by [Jon Cox](https://github.com/joncox123) — [Buy him a coffee](https://www.buymeacoffee.com/joncox)
