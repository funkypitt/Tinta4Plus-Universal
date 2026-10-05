#!/usr/bin/env python3
"""
Standalone eInk/OLED toggle for ThinkBook Plus Gen 4 IRU.

Connects to the running tinta4plusu helper daemon and performs a full
display switch without the GUI.  Bind this to any keyboard shortcut
in your desktop environment for instant display toggling.

Usage:
    python3 toggle-eink.py          # Toggle between eInk and OLED
    python3 toggle-eink.py --status # Print current state and exit

Exit codes:
    0  Success
    1  Error (daemon not running, switch failed, etc.)
"""

import sys
import os
import time
import random
import glob
import subprocess
import logging

# Allow imports from the same directory as this script
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

from HelperClient import HelperClient
from DisplayManager import DisplayManager

# Configuration — must match Tinta4Plus.py / HelperDaemon.py
SOCKET_PATH = '/tmp/tinta4plusu.sock'
DISPLAY_OLED = 'eDP-1'
DISPLAY_EINK = 'eDP-2'
# Privacy images shipped next to the scripts: every eink-disable<N>.jpg
# (same discovery rule as Tinta4Plus.py, so both tools see the same set)
EINK_DISABLED_IMAGES = sorted(
    (os.path.basename(p) for p in glob.glob(os.path.join(script_dir, 'eink-disable*.jpg'))
     if os.path.basename(p)[len('eink-disable'):-len('.jpg')].isdigit()),
    key=lambda n: int(n[len('eink-disable'):-len('.jpg')]))
DEFAULT_SCALE = 1.0
DEFAULT_BRIGHTNESS = 4


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
    )
    return logging.getLogger('toggle-eink')


def resolve_image_path(image_name):
    """Find a privacy image relative to the script or installed location."""
    candidates = [
        os.path.join(script_dir, image_name),
        os.path.join('/opt/tinta4plusu', image_name),
    ]
    # PyInstaller bundle
    if getattr(sys, 'frozen', False):
        candidates.insert(0, os.path.join(sys._MEIPASS, image_name))

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def load_settings(logger):
    """Load user settings (scale, brightness, privacy image) from the GUI config file."""
    settings_file = os.path.expanduser('~/.config/Tinta4PlusU/settings')
    defaults = {
        'display_scale': DEFAULT_SCALE,
        'brightness': DEFAULT_BRIGHTNESS,
        'privacy_image': 'random',
    }
    try:
        import json
        with open(settings_file, 'r') as f:
            data = json.load(f)
        defaults['display_scale'] = data.get('display_scale', DEFAULT_SCALE)
        defaults['privacy_image'] = data.get('privacy_image', 'random')
        # Brightness isn't persisted in settings — use default
    except Exception:
        pass
    return defaults


def switch_to_eink(helper, display_mgr, logger, scale, brightness):
    """Perform the full OLED → eInk switch sequence."""
    # Save OLED scale for later restore
    saved_oled_scale = display_mgr.get_display_scale(DISPLAY_OLED)

    # 1. Enable eInk display output
    logger.info(f"Enabling eInk display on {DISPLAY_EINK} with {scale}x scale")
    if not display_mgr.enable_display(DISPLAY_EINK, scale=scale):
        logger.error("Failed to enable eInk display output")
        return False

    time.sleep(1.0)

    # 2. Enable eInk via USB T-CON controller
    resp = helper.send_command('enable-eink')
    if not resp or not resp.get('success'):
        logger.error(f"enable-eink failed: {resp}")
        return False

    # 3. Enable frontlight
    helper.send_command('enable-frontlight', brightness_level=brightness)

    # 4. Set dynamic mode
    helper.send_command('set-dynamic')

    time.sleep(0.5)

    # 5. Disable OLED
    logger.info(f"Disabling OLED on {DISPLAY_OLED}")
    display_mgr.disable_display(DISPLAY_OLED)

    time.sleep(0.3)

    # 6. Re-apply eInk as sole output (fixes panning on X11)
    display_mgr.enable_display(DISPLAY_EINK, scale=scale)

    # 7. Map touch
    display_mgr.map_touch_to_display(DISPLAY_EINK)

    # 8. Disable DPMS for eInk
    try:
        subprocess.run(['xset', '-dpms'], capture_output=True, timeout=5)
    except Exception:
        pass

    logger.info("Switched to eInk")
    return True


def switch_to_oled(helper, display_mgr, logger, privacy_image='random'):
    """Perform the full eInk → OLED switch sequence."""
    # Reader mode may have left the eInk in portrait: go back to landscape
    # first so the privacy image is shown upright.
    try:
        if display_mgr.get_display_rotation(DISPLAY_EINK) != 'normal':
            logger.info("eInk is rotated (reader mode) — restoring landscape first")
            display_mgr.enable_display(DISPLAY_EINK, rotation='normal')
            display_mgr.map_touch_to_display(DISPLAY_EINK)
            time.sleep(0.5)
    except Exception as e:
        logger.warning(f"Could not reset eInk rotation: {e}")
    # 1. Switch to dynamic mode for color privacy image
    helper.send_command('set-dynamic')
    time.sleep(0.5)

    # 2. Display privacy image (dropdown override from GUI settings, else random)
    if privacy_image != 'random' and privacy_image in EINK_DISABLED_IMAGES:
        chosen = privacy_image
    else:
        chosen = random.choice(EINK_DISABLED_IMAGES)
    image_path = resolve_image_path(chosen)
    image_process = None
    if image_path:
        logger.info(f"Displaying privacy image: {chosen}")
        image_process = display_mgr.display_fullscreen_image(DISPLAY_EINK, image_path)
        if image_process:
            time.sleep(3.0)

    # 3. Disable frontlight
    helper.send_command('disable-frontlight')

    # 4. Disable eInk T-CON (while image viewer still running)
    resp = helper.send_command('disable-eink')
    if not resp or not resp.get('success'):
        logger.error(f"disable-eink failed: {resp}")
        if image_process:
            image_process.terminate()
        return False

    # 5. Kill image viewer
    if image_process:
        try:
            image_process.terminate()
            image_process.wait(timeout=2)
        except Exception:
            try:
                image_process.kill()
            except Exception:
                pass

    # 6. Enable OLED
    logger.info(f"Enabling OLED on {DISPLAY_OLED}")
    display_mgr.enable_display(DISPLAY_OLED)

    time.sleep(1.0)

    # 7. Disable eInk display output
    display_mgr.disable_display(DISPLAY_EINK)

    time.sleep(0.3)

    # 8. Re-apply OLED as sole output
    display_mgr.enable_display(DISPLAY_OLED)

    # 9. Wake and unlock
    display_mgr.wake_display()
    time.sleep(1.0)
    display_mgr.wake_display()

    # 10. Map touch back to OLED
    display_mgr.map_touch_to_display(DISPLAY_OLED)

    # 11. Re-enable DPMS
    try:
        subprocess.run(['xset', '+dpms'], capture_output=True, timeout=5)
    except Exception:
        pass

    logger.info("Switched to OLED")
    return True


def main():
    logger = setup_logging()

    status_only = '--status' in sys.argv

    # Connect to daemon
    if not os.path.exists(SOCKET_PATH):
        logger.error("Helper daemon not running (socket not found)")
        print("Error: tinta4plusu helper daemon is not running.", file=sys.stderr)
        print("Start the GUI first, or run: pkexec tinta4plusu-helper", file=sys.stderr)
        return 1

    helper = HelperClient(logger)
    if not helper.connect(SOCKET_PATH, timeout=5.0):
        logger.error("Could not connect to helper daemon")
        return 1

    # Query current state
    try:
        state = helper.send_command('get-state')
    except Exception as e:
        logger.error(f"Failed to query state: {e}")
        return 1

    if not state or not state.get('success'):
        logger.error(f"get-state failed: {state}")
        return 1

    eink_active = state.get('eink_enabled', False)

    if status_only:
        display = "eInk" if eink_active else "OLED"
        brightness = state.get('brightness_level', '?')
        print(f"Display: {display}")
        print(f"Brightness: {brightness}")
        return 0

    # Load user settings
    settings = load_settings(logger)
    scale = settings['display_scale']
    brightness = settings.get('brightness', DEFAULT_BRIGHTNESS)

    display_mgr = DisplayManager(logger)

    if eink_active:
        logger.info("Currently on eInk — switching to OLED")
        ok = switch_to_oled(helper, display_mgr, logger, settings.get('privacy_image', 'random'))
    else:
        logger.info("Currently on OLED — switching to eInk")
        ok = switch_to_eink(helper, display_mgr, logger, scale, brightness)

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
