"""
Post-resume / post-login display and input validation.

Checks that the display state is sane after suspend, hibernate, or login:
  - Exactly one display is active (no extended-desktop bug)
  - Active display has the correct resolution
  - Display is positioned at (0,0), not offset
  - Keyboard and pointer input devices are present
  - Keyboard layout is restored
  - Touchscreen is mapped to the active display

Can be used as a module (imported by the GUI) or run standalone.
"""

import os
import re
import subprocess
import time


class ResumeCheck:
    """Validate and fix display/input state after resume or login."""

    # Expected hardware resolutions (native, before scaling)
    OLED_RES = (2880, 1800)
    EINK_RES = (2560, 1600)

    def __init__(self, display_mgr, logger):
        self.dm = display_mgr
        self.logger = logger

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, expect_eink=False, saved_oled_scale=None,
            saved_keyboard_layout=None, eink_scale=None, eink_rotation='normal'):
        """Run all post-resume validation checks and apply fixes.

        Args:
            expect_eink: True if the eInk display should be active.
                         When unsure, pass False (default to OLED).
            saved_oled_scale: Scale factor to restore on OLED (e.g. 1.75).
            saved_keyboard_layout: Layout string to restore (from
                                   DisplayManager.get_keyboard_layout()).
            eink_scale: Scale to re-apply when the eInk has to be re-enabled.
            eink_rotation: Rotation the eInk should have ('left'/'right' in
                           reader mode); geometry checks and re-enables use it.

        Returns:
            list[str]: Human-readable log entries for each check/fix.
        """
        results = []
        self._eink_scale = eink_scale
        self._eink_rotation = eink_rotation if eink_rotation in self.dm.ROTATIONS else 'normal'

        # 1. Display state --------------------------------------------------
        oled_on = self.dm.is_display_active(self.dm.OLED_CONNECTOR)
        eink_on = self.dm.is_display_active(self.dm.EINK_CONNECTOR)
        self.logger.info(
            f"ResumeCheck: OLED={'on' if oled_on else 'off'}, "
            f"eInk={'on' if eink_on else 'off'}, "
            f"expect_eink={expect_eink}")

        # --- both active (extended-desktop bug) ---
        if oled_on and eink_on:
            results.extend(self._fix_both_active(expect_eink,
                                                 saved_oled_scale))
        # --- neither active ---
        elif not oled_on and not eink_on:
            results.extend(self._fix_none_active(expect_eink,
                                                 saved_oled_scale))
        # --- wrong display active ---
        elif expect_eink and oled_on and not eink_on:
            # Wanted eInk but only OLED is on — unusual post-resume.
            # Don't force eInk (needs USB T-CON), just warn.
            results.append("Warning: expected eInk but only OLED is active")
        elif not expect_eink and eink_on and not oled_on:
            results.extend(self._switch_eink_to_oled(saved_oled_scale))

        # 2. Geometry validation --------------------------------------------
        results.extend(self._validate_geometry(expect_eink, saved_oled_scale))

        # 3. Panning check (X11 only) --------------------------------------
        results.extend(self._check_panning())

        # 4. Keyboard layout -----------------------------------------------
        if saved_keyboard_layout:
            try:
                self.dm.restore_keyboard_layout(saved_keyboard_layout)
                results.append("Keyboard layout restored")
            except Exception as e:
                results.append(f"Warning: keyboard layout restore failed: {e}")

        # 5. Input devices --------------------------------------------------
        results.extend(self._check_input_devices())

        # 6. Touch mapping --------------------------------------------------
        active_connector = (self.dm.EINK_CONNECTOR if expect_eink
                            else self.dm.OLED_CONNECTOR)
        try:
            self.dm.map_touch_to_display(
                active_connector, rotation=self._eink_rotation if expect_eink else 'normal')
            results.append(f"Touchscreen mapped to {active_connector}")
        except Exception as e:
            results.append(f"Warning: touch mapping failed: {e}")

        for r in results:
            self.logger.info(f"ResumeCheck: {r}")
        return results

    # ------------------------------------------------------------------
    # Display fixes
    # ------------------------------------------------------------------

    def _fix_both_active(self, expect_eink, saved_oled_scale):
        """Both displays are on — disable the wrong one."""
        fixes = []
        if expect_eink:
            self.dm.disable_display(self.dm.OLED_CONNECTOR)
            fixes.append("Fixed: disabled OLED (both were active, eInk expected)")
        else:
            self.dm.disable_display(self.dm.EINK_CONNECTOR)
            time.sleep(0.5)
            scale = saved_oled_scale or 1.0
            self.dm.enable_display(self.dm.OLED_CONNECTOR, scale=scale)
            fixes.append("Fixed: disabled eInk and re-applied OLED "
                         "(both were active)")
        return fixes

    def _fix_none_active(self, expect_eink, saved_oled_scale):
        """No display is active — enable the expected one."""
        fixes = []
        if expect_eink:
            self.dm.enable_display(self.dm.EINK_CONNECTOR, scale=self._eink_scale,
                                   rotation=self._eink_rotation)
            fixes.append("Fixed: enabled eInk (no display was active)")
        else:
            scale = saved_oled_scale or 1.0
            self.dm.enable_display(self.dm.OLED_CONNECTOR, scale=scale)
            fixes.append("Fixed: enabled OLED (no display was active)")
        return fixes

    def _switch_eink_to_oled(self, saved_oled_scale):
        """eInk is active but OLED was expected — switch."""
        self.dm.disable_display(self.dm.EINK_CONNECTOR)
        time.sleep(0.5)
        scale = saved_oled_scale or 1.0
        self.dm.enable_display(self.dm.OLED_CONNECTOR, scale=scale)
        return ["Fixed: switched from eInk to OLED"]

    # ------------------------------------------------------------------
    # Geometry validation
    # ------------------------------------------------------------------

    def _validate_geometry(self, expect_eink, saved_oled_scale):
        """Verify the active display has correct resolution and position."""
        results = []
        connector = (self.dm.EINK_CONNECTOR if expect_eink
                     else self.dm.OLED_CONNECTOR)

        if not self.dm.is_display_active(connector):
            return results

        geom = self.dm.get_display_geometry(connector)
        if not geom:
            results.append(f"Warning: could not read geometry for {connector}")
            return results

        # Position check — should be at origin
        if geom['x'] != 0 or geom['y'] != 0:
            self.logger.warning(
                f"ResumeCheck: {connector} at offset "
                f"({geom['x']},{geom['y']}), repositioning to (0,0)")
            if expect_eink:
                self.dm.enable_display(connector, scale=self._eink_scale, rotation=self._eink_rotation)
            else:
                self.dm.enable_display(connector, scale=saved_oled_scale or 1.0)
            results.append(
                f"Fixed: {connector} was at ({geom['x']},{geom['y']}), "
                f"repositioned to (0,0)")

        # Resolution check — compare against native resolution
        # get_display_geometry returns logical (scaled) size, so we need to
        # account for scaling when comparing
        expected = self.EINK_RES if expect_eink else self.OLED_RES
        if expect_eink and self.dm.is_portrait(self._eink_rotation):
            expected = (expected[1], expected[0])
        scale = self.dm.get_display_scale(connector)
        if scale and scale > 0:
            logical_w = int(expected[0] / scale)
            logical_h = int(expected[1] / scale)
        else:
            logical_w, logical_h = expected

        # Allow small rounding tolerance
        w_ok = abs(geom['width'] - logical_w) <= 2
        h_ok = abs(geom['height'] - logical_h) <= 2
        if not w_ok or not h_ok:
            results.append(
                f"Warning: {connector} resolution {geom['width']}x"
                f"{geom['height']} differs from expected "
                f"{logical_w}x{logical_h} (native {expected[0]}x{expected[1]}"
                f" at scale {scale})")

        return results

    # ------------------------------------------------------------------
    # Panning check (X11)
    # ------------------------------------------------------------------

    def _check_panning(self):
        """Detect xrandr panning (causes the scrolling-desktop bug)."""
        if self.dm.session_type != 'x11':
            return []
        try:
            result = subprocess.run(
                ['xrandr', '--query'], capture_output=True, text=True,
                timeout=5)
            if result.returncode != 0:
                return []

            issues = []
            current_display = None
            for line in result.stdout.splitlines():
                # Match connected display lines
                m = re.match(r'^(\S+)\s+connected', line)
                if m:
                    current_display = m.group(1)
                    continue
                # Look for panning info in mode lines
                if current_display and 'panning' in line.lower():
                    issues.append(
                        f"Warning: {current_display} has panning enabled "
                        f"(may cause scrolling desktop)")
            return issues
        except Exception as e:
            return [f"Warning: panning check failed: {e}"]

    # ------------------------------------------------------------------
    # Input device checks
    # ------------------------------------------------------------------

    def _check_input_devices(self):
        """Verify keyboard and pointer devices are present in the kernel."""
        issues = []
        try:
            with open('/proc/bus/input/devices', 'r') as f:
                content = f.read()
        except Exception as e:
            return [f"Warning: could not read input devices: {e}"]

        has_keyboard = False
        has_pointer = False

        for block in content.split('\n\n'):
            if not block.strip():
                continue
            # Extract handler line
            handlers = ''
            name = ''
            for line in block.splitlines():
                if line.startswith('H:'):
                    handlers = line.lower()
                elif line.startswith('N:'):
                    name = line.lower()

            if not handlers:
                continue

            # Keyboard: look for "kbd" handler with a real keyboard name
            # (skip power buttons, video bus, etc.)
            if 'kbd' in handlers and 'event' in handlers:
                if ('keyboard' in name or 'at translated' in name or
                        'thinkpad' in name):
                    has_keyboard = True

            # Pointer: mouse, touchpad, or pointing stick
            if ('mouse' in handlers or
                    'touchpad' in name or 'trackpoint' in name or
                    'pointing stick' in name):
                has_pointer = True

        if not has_keyboard:
            issues.append("Warning: no keyboard input device detected")
        if not has_pointer:
            issues.append("Warning: no pointer input device detected")

        return issues


# ------------------------------------------------------------------
# Standalone entry point
# ------------------------------------------------------------------

def main():
    """Run resume checks standalone (for manual testing or systemd hook)."""
    import logging

    LOG_FILE = '/tmp/tinta4plusu.log'
    logger = logging.getLogger('tinta4plusu-resume-check')
    logger.setLevel(logging.INFO)

    # Log to file (append) and console
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s'))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    logger.addHandler(fh)
    logger.addHandler(ch)

    # Import DisplayManager from same directory
    import sys
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    from DisplayManager import DisplayManager

    dm = DisplayManager(logger)
    checker = ResumeCheck(dm, logger)

    # Determine expected state: if eDP-2 is the only active display,
    # assume eInk was intended; otherwise default to OLED.
    eink_on = dm.is_display_active(dm.EINK_CONNECTOR)
    oled_on = dm.is_display_active(dm.OLED_CONNECTOR)
    expect_eink = eink_on and not oled_on

    logger.info(f"Standalone resume check: expect_eink={expect_eink}")
    results = checker.run(expect_eink=expect_eink)

    if results:
        for r in results:
            print(r)
    else:
        print("All checks passed, no issues found.")


if __name__ == '__main__':
    main()
