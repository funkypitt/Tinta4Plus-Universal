"""
Copyright (c) 2025 Jon Cox (joncox123). All rights reserved.

WARNING: This software is provided "AS IS", without any warranty of any kind. It may contain bugs or other defects
that result in data loss, corruption, hardware damage or other issues. Use at your own risk.
It may temporarily or permanently render your hardware inoperable.
It may corrupt or damage the Embedded Controller or eInk T-CON controller in your laptop.
The author is not responsible for any damage, data loss or lost productivity caused by use of this software.
By downloading and using this software you agree to these terms and acknowledge the risks involved.
"""

import subprocess
import os
import time
import json


class DisplayManager:
    """Manage display switching and configuration (no root required).

    Supports X11 (via xrandr) and Wayland (via Mutter D-Bus DisplayConfig).
    Auto-detects session type and desktop environment at startup.
    """

    # ThinkBook Plus Gen 4 IRU hardware specifications
    OLED_RESOLUTION_WH = [2880, 1800]
    EINK_RESOLUTION_WH = [2560, 1600]

    # Connector names as seen by the kernel / display server
    OLED_CONNECTOR = "eDP-1"
    EINK_CONNECTOR = "eDP-2"

    def __init__(self, logger):
        self.logger = logger
        self.session_type = self._detect_session_type()
        self.desktop_env = self._detect_desktop_environment()
        self.logger.info(f"DisplayManager: session={self.session_type}, desktop={self.desktop_env}")

    # ------------------------------------------------------------------
    # Session / DE detection
    # ------------------------------------------------------------------

    def _detect_session_type(self):
        """Detect whether we are running under X11 or Wayland.

        Returns 'x11', 'wayland', or 'unknown'.
        """
        session = os.environ.get('XDG_SESSION_TYPE', '').lower()
        if session in ('x11', 'wayland'):
            return session

        # Fallback: WAYLAND_DISPLAY is set when a Wayland compositor is running
        if os.environ.get('WAYLAND_DISPLAY'):
            return 'wayland'

        # Fallback: DISPLAY is typically set for X11
        if os.environ.get('DISPLAY'):
            return 'x11'

        return 'unknown'

    def _detect_desktop_environment(self):
        """Detect the running desktop environment.

        Returns 'gnome', 'xfce', 'kde', or 'unknown'.
        """
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
        if 'gnome' in desktop or 'ubuntu' in desktop:
            return 'gnome'
        if 'kde' in desktop:
            return 'kde'
        if 'xfce' in desktop:
            return 'xfce'
        return 'unknown'

    # ------------------------------------------------------------------
    # Public API — dispatchers
    # ------------------------------------------------------------------

    def _use_kde_wayland(self):
        """Check if we should use KDE Wayland (kscreen) backend."""
        return self.session_type == 'wayland' and self.desktop_env == 'kde'

    def _use_mutter_wayland(self):
        """Check if we should use Mutter (GNOME) Wayland backend."""
        return self.session_type == 'wayland' and self.desktop_env != 'kde'

    def get_displays(self):
        """Get list of connected displays."""
        if self._use_kde_wayland():
            return self._get_displays_kde()
        if self.session_type == 'wayland':
            return self._get_displays_wayland()
        return self._get_displays_x11()

    def is_display_active(self, display_name):
        """Check if a display is currently active (enabled and has geometry)."""
        if self._use_kde_wayland():
            return self._is_display_active_kde(display_name)
        if self.session_type == 'wayland':
            return self._is_display_active_wayland(display_name)
        return self._is_display_active_x11(display_name)

    def enable_display(self, display_name, scale=None):
        """Enable/turn on a display with optional scaling.

        Args:
            display_name: Name of the display (e.g., 'eDP-1', 'eDP-2')
            scale: Optional scale factor (e.g., 1.60 means UI appears 1.6x larger)
        """
        if self._use_kde_wayland():
            return self._enable_display_kde(display_name, scale)
        if self.session_type == 'wayland':
            return self._enable_display_wayland(display_name, scale)
        return self._enable_display_x11(display_name, scale)

    def disable_display(self, display_name):
        """Disable/turn off a display."""
        if self._use_kde_wayland():
            return self._disable_display_kde(display_name)
        if self.session_type == 'wayland':
            return self._disable_display_wayland(display_name)
        return self._disable_display_x11(display_name)

    def get_display_geometry(self, display_name):
        """Get the geometry (position and size) of a display."""
        if self._use_kde_wayland():
            return self._get_display_geometry_kde(display_name)
        if self.session_type == 'wayland':
            return self._get_display_geometry_wayland(display_name)
        return self._get_display_geometry_x11(display_name)

    def display_fullscreen_image(self, display_name, image_path):
        """Display a fullscreen image on a specific display.

        Args:
            display_name: Name of the display (e.g., 'eDP-2')
            image_path: Path to the image file

        Returns:
            subprocess.Popen object if successful, None otherwise
        """
        if not os.path.exists(image_path):
            self.logger.error(f"Image file not found: {image_path}")
            return None

        # Get display geometry
        geometry = self.get_display_geometry(display_name)
        if not geometry:
            self.logger.error(f"Could not determine geometry for {display_name}")
            return None

        self.logger.info(f"Display {display_name} geometry: {geometry['width']}x{geometry['height']}+{geometry['x']}+{geometry['y']}")

        if self.session_type == 'wayland':
            return self._display_image_wayland(image_path, geometry)
        return self._display_image_x11(image_path, geometry)

    # ------------------------------------------------------------------
    # X11 backend (xrandr)
    # ------------------------------------------------------------------

    def _get_displays_x11(self):
        """Get list of connected displays using xrandr."""
        try:
            result = subprocess.run(
                ['xrandr', '--query'],
                capture_output=True,
                text=True,
                timeout=5
            )

            displays = []
            for line in result.stdout.split('\n'):
                if ' connected' in line:
                    parts = line.split()
                    name = parts[0]
                    primary = 'primary' in line
                    displays.append({'name': name, 'primary': primary})

            return displays

        except Exception as e:
            self.logger.error(f"Failed to get displays: {e}")
            return []

    def _is_display_active_x11(self, display_name):
        """Check if a display is currently active using xrandr."""
        try:
            result = subprocess.run(
                ['xrandr', '--query'],
                capture_output=True,
                text=True,
                timeout=5
            )

            for line in result.stdout.split('\n'):
                if display_name in line and ' connected' in line:
                    parts = line.split()
                    for part in parts:
                        if 'x' in part and '+' in part:
                            return True
                    return False

            return False

        except Exception as e:
            self.logger.error(f"Failed to check display status: {e}")
            return False

    def _enable_display_x11(self, display_name, scale=None):
        """Enable a display using xrandr with optional scaling."""
        try:
            if display_name == "eDP-1":
                native_width, native_height = self.OLED_RESOLUTION_WH
            elif display_name == "eDP-2":
                native_width, native_height = self.EINK_RESOLUTION_WH
            else:
                self.logger.warning(f"Unknown display {display_name}, using auto mode")
                native_width, native_height = None, None

            cmd = ['xrandr', '--output', display_name]

            if native_width and native_height:
                cmd.extend(['--mode', f'{native_width}x{native_height}'])

                if scale is not None and scale != 1.0:
                    scale_inv = 1.0 / scale
                    panning_width = int(native_width * scale_inv)
                    panning_height = int(native_height * scale_inv)
                    cmd.extend(['--panning', f'{panning_width}x{panning_height}'])
                    cmd.extend(['--scale', f'{scale_inv}x{scale_inv}'])
                    self.logger.info(f"Scaling: virtual desktop {panning_width}x{panning_height}, "
                                   f"xrandr scale {scale_inv:.3f}x{scale_inv:.3f} (our scale={scale}), "
                                   f"physical {native_width}x{native_height}")
                else:
                    cmd.extend(['--panning', f'{native_width}x{native_height}'])
                    cmd.extend(['--scale', '1x1'])
            else:
                cmd.append('--auto')

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                self.logger.warning(f"xrandr returned {result.returncode}: {result.stderr.strip()}")

            time.sleep(0.5)

            # Force DPMS on to wake the panel — xrandr may enable the output
            # in the display server while the physical panel stays in standby.
            try:
                subprocess.run(['xset', 'dpms', 'force', 'on'],
                               capture_output=True, timeout=5)
            except Exception as dpms_err:
                self.logger.warning(f"xset dpms force on failed: {dpms_err}")

            time.sleep(0.3)

            if self._is_display_active_x11(display_name):
                scale_info = f" with {scale}x scale" if scale and scale != 1.0 else ""
                self.logger.info(f"Enabled display: {display_name}{scale_info}")
                return True
            else:
                self.logger.error(f"Failed to enable display: {display_name} (display not active after command)")
                return False

        except Exception as e:
            self.logger.error(f"Failed to enable display: {e}")
            return False

    def _disable_display_x11(self, display_name):
        """Disable a display using xrandr."""
        try:
            subprocess.run(
                ['xrandr', '--output', display_name, '--off'],
                capture_output=True,
                timeout=5
            )

            time.sleep(0.2)

            if not self._is_display_active_x11(display_name):
                self.logger.info(f"Disabled display: {display_name}")
                return True
            else:
                self.logger.error(f"Failed to disable display: {display_name} (display still active after command)")
                return False

        except Exception as e:
            self.logger.error(f"Failed to disable display: {e}")
            return False

    def _get_display_geometry_x11(self, display_name):
        """Get display geometry using xrandr."""
        try:
            result = subprocess.run(
                ['xrandr', '--query'],
                capture_output=True,
                text=True,
                timeout=5
            )

            for line in result.stdout.split('\n'):
                if display_name in line and 'connected' in line:
                    parts = line.split()
                    for part in parts:
                        if 'x' in part and '+' in part:
                            geo = part.split('+')
                            size = geo[0].split('x')
                            width = int(size[0])
                            height = int(size[1])
                            x_offset = int(geo[1]) if len(geo) > 1 else 0
                            y_offset = int(geo[2]) if len(geo) > 2 else 0
                            return {
                                'width': width,
                                'height': height,
                                'x': x_offset,
                                'y': y_offset
                            }

            self.logger.warning(f"Could not find geometry for {display_name}")
            return None

        except Exception as e:
            self.logger.error(f"Failed to get display geometry: {e}")
            return None

    def _display_image_x11(self, image_path, geometry):
        """Display fullscreen image using feh (preferred on X11), fallback to imv."""
        if self._command_exists('feh'):
            try:
                cmd = [
                    'feh',
                    '--fullscreen',
                    '--auto-zoom',
                    '--no-menus',
                    '--hide-pointer',
                    image_path
                ]
                self.logger.info("Displaying fullscreen image using feh")
                process = subprocess.Popen(cmd)
                time.sleep(0.5)
                return process
            except Exception as e:
                self.logger.error(f"Failed to display image with feh: {e}")

        if self._command_exists('imv'):
            try:
                cmd = ['imv', '-f', image_path]
                self.logger.info("Displaying image using imv (fallback)")
                self.logger.warning("imv may not position on correct display automatically")
                process = subprocess.Popen(cmd)
                time.sleep(0.5)
                return process
            except Exception as e:
                self.logger.error(f"Failed to display image with imv: {e}")

        self.logger.error("Neither feh nor imv is installed. Please install one:")
        self.logger.error("  For X11: sudo apt install feh")
        self.logger.error("  For Wayland: sudo apt install imv")
        return None

    # ------------------------------------------------------------------
    # Wayland backend (Mutter D-Bus DisplayConfig)
    # ------------------------------------------------------------------

    def _mutter_call(self, method, *args):
        """Call a method on org.gnome.Mutter.DisplayConfig via D-Bus.

        Tries python3-dbus first, falls back to gdbus subprocess.
        Returns the parsed result or None on failure.
        """
        try:
            import dbus
            bus = dbus.SessionBus()
            proxy = bus.get_object('org.gnome.Mutter.DisplayConfig',
                                   '/org/gnome/Mutter/DisplayConfig')
            iface = dbus.Interface(proxy, 'org.gnome.Mutter.DisplayConfig')
            return getattr(iface, method)(*args)
        except ImportError:
            self.logger.debug("python3-dbus not available, using gdbus subprocess")
        except Exception as e:
            self.logger.debug(f"dbus call failed ({method}): {e}, trying gdbus")

        return self._mutter_call_gdbus(method, *args)

    def _mutter_call_gdbus(self, method, *args):
        """Fallback: call Mutter DisplayConfig via gdbus CLI."""
        try:
            cmd = [
                'gdbus', 'call',
                '--session',
                '--dest', 'org.gnome.Mutter.DisplayConfig',
                '--object-path', '/org/gnome/Mutter/DisplayConfig',
                '--method', f'org.gnome.Mutter.DisplayConfig.{method}'
            ]
            for arg in args:
                cmd.append(str(arg))

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                self.logger.error(f"gdbus {method} failed: {result.stderr.strip()}")
                return None
            return result.stdout.strip()
        except Exception as e:
            self.logger.error(f"gdbus call failed ({method}): {e}")
            return None

    def _mutter_get_current_state(self):
        """Get current display state from Mutter.

        Returns a dict with 'serial', 'monitors', and 'logical_monitors',
        or None on failure.

        Each monitor entry: {
            'connector': str,       # e.g. 'eDP-1'
            'vendor': str,
            'product': str,
            'serial': str,
            'modes': [{'id': str, 'width': int, 'height': int,
                       'refresh': float, 'preferred_scale': float,
                       'supported_scales': [float], 'is_current': bool,
                       'is_preferred': bool}],
        }

        Each logical_monitor entry: {
            'x': int, 'y': int, 'scale': float, 'transform': int,
            'primary': bool,
            'monitors': [{'connector': str, 'vendor': str, 'product': str, 'serial': str}],
        }
        """
        try:
            import dbus
            return self._mutter_get_current_state_dbus()
        except ImportError:
            pass
        except Exception:
            pass
        return self._mutter_get_current_state_gdbus()

    def _mutter_get_current_state_dbus(self):
        """Parse GetCurrentState via python3-dbus."""
        import dbus
        bus = dbus.SessionBus()
        proxy = bus.get_object('org.gnome.Mutter.DisplayConfig',
                               '/org/gnome/Mutter/DisplayConfig')
        iface = dbus.Interface(proxy, 'org.gnome.Mutter.DisplayConfig')
        state = iface.GetCurrentState()

        serial = int(state[0])
        raw_monitors = state[1]
        raw_logical = state[2]

        monitors = []
        for mon in raw_monitors:
            # mon = ((connector, vendor, product, serial), [modes], properties)
            spec = mon[0]
            connector = str(spec[0])
            vendor = str(spec[1])
            product = str(spec[2])
            mon_serial = str(spec[3])

            modes = []
            for m in mon[1]:
                # m = (id, width, height, refresh, preferred_scale, supported_scales, properties)
                mode_props = dict(m[6]) if len(m) > 6 else {}
                is_current = bool(mode_props.get('is-current', False))
                is_preferred = bool(mode_props.get('is-preferred', False))
                modes.append({
                    'id': str(m[0]),
                    'width': int(m[1]),
                    'height': int(m[2]),
                    'refresh': float(m[3]),
                    'preferred_scale': float(m[4]),
                    'supported_scales': [float(s) for s in m[5]],
                    'is_current': is_current,
                    'is_preferred': is_preferred,
                })

            monitors.append({
                'connector': connector,
                'vendor': vendor,
                'product': product,
                'serial': mon_serial,
                'modes': modes,
            })

        logical_monitors = []
        for lm in raw_logical:
            # lm = (x, y, scale, transform, primary, [(connector, vendor, product, serial)], properties)
            lm_mons = []
            for ms in lm[5]:
                lm_mons.append({
                    'connector': str(ms[0]),
                    'vendor': str(ms[1]),
                    'product': str(ms[2]),
                    'serial': str(ms[3]),
                })
            logical_monitors.append({
                'x': int(lm[0]),
                'y': int(lm[1]),
                'scale': float(lm[2]),
                'transform': int(lm[3]),
                'primary': bool(lm[4]),
                'monitors': lm_mons,
            })

        return {
            'serial': serial,
            'monitors': monitors,
            'logical_monitors': logical_monitors,
        }

    def _mutter_get_current_state_gdbus(self):
        """Parse GetCurrentState via gdbus subprocess.

        gdbus returns GVariant text format. We parse it with a best-effort approach
        by calling gdbus and then extracting monitor info from the raw text.
        """
        try:
            cmd = [
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Mutter.DisplayConfig',
                '--object-path', '/org/gnome/Mutter/DisplayConfig',
                '--method', 'org.gnome.Mutter.DisplayConfig.GetCurrentState'
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                self.logger.error(f"gdbus GetCurrentState failed: {result.stderr.strip()}")
                return None

            # The output is a complex GVariant. We use a Python helper to parse it
            # by extracting key patterns. This is fragile but works as a fallback.
            return self._parse_gdbus_state(result.stdout)

        except Exception as e:
            self.logger.error(f"Failed to get Mutter state via gdbus: {e}")
            return None

    def _parse_gdbus_state(self, raw_output):
        """Best-effort parse of gdbus GetCurrentState GVariant output.

        Since GVariant text format is complex, we use a simplified approach:
        extract connector names and basic info using subprocess + python3 eval.
        """
        try:
            # Use python3 to evaluate the GVariant-like output as a Python tuple
            # gdbus output closely resembles Python tuple syntax
            # Replace GVariant type annotations that python can't parse
            cleaned = raw_output.strip()
            if cleaned.startswith('(') and cleaned.endswith(')'):
                # Try using python3 subprocess to safely parse
                parse_script = r'''
import sys, ast, json

raw = sys.stdin.read().strip()
# Remove trailing comma before closing paren in top-level tuple
# GVariant uses @type annotations - strip them
import re
# Remove @type annotations like @au, @a(ss), etc.
cleaned = re.sub(r"@[a-z({}\[\])]+\s", "", raw)
# Remove uint32/int32/int64/uint64/double type casts
cleaned = re.sub(r"\b(uint32|int32|int64|uint64|double)\s+", "", cleaned)
# Replace 'true'/'false' with Python booleans
cleaned = cleaned.replace("true", "True").replace("false", "False")
try:
    data = ast.literal_eval(cleaned)
    serial = data[0]
    monitors = []
    for mon in data[1]:
        spec = mon[0]
        modes = []
        for m in mon[1]:
            props = dict(m[6]) if len(m) > 6 else {}
            modes.append({
                "id": str(m[0]),
                "width": int(m[1]),
                "height": int(m[2]),
                "refresh": float(m[3]),
                "preferred_scale": float(m[4]),
                "supported_scales": [float(s) for s in m[5]],
                "is_current": bool(props.get("is-current", False)),
                "is_preferred": bool(props.get("is-preferred", False)),
            })
        monitors.append({
            "connector": str(spec[0]),
            "vendor": str(spec[1]),
            "product": str(spec[2]),
            "serial": str(spec[3]),
            "modes": modes,
        })
    logical = []
    for lm in data[2]:
        lm_mons = [{"connector": str(ms[0]), "vendor": str(ms[1]),
                     "product": str(ms[2]), "serial": str(ms[3])} for ms in lm[5]]
        logical.append({
            "x": int(lm[0]), "y": int(lm[1]),
            "scale": float(lm[2]), "transform": int(lm[3]),
            "primary": bool(lm[4]),
            "monitors": lm_mons,
        })
    print(json.dumps({"serial": serial, "monitors": monitors, "logical_monitors": logical}))
except Exception as e:
    print(json.dumps(None))
'''
                proc = subprocess.run(
                    ['python3', '-c', parse_script],
                    input=cleaned, capture_output=True, text=True, timeout=10
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    parsed = json.loads(proc.stdout.strip())
                    return parsed

            self.logger.warning("Could not parse gdbus GetCurrentState output")
            return None

        except Exception as e:
            self.logger.error(f"Failed to parse gdbus state: {e}")
            return None

    def _find_monitor_in_state(self, state, display_name):
        """Find a monitor entry by connector name in a Mutter state dict."""
        if not state or 'monitors' not in state:
            return None
        for mon in state['monitors']:
            if mon['connector'] == display_name:
                return mon
        return None

    def _find_logical_monitor(self, state, display_name):
        """Find the logical monitor entry that contains a given connector."""
        if not state or 'logical_monitors' not in state:
            return None
        for lm in state['logical_monitors']:
            for ms in lm['monitors']:
                if ms['connector'] == display_name:
                    return lm
        return None

    def _best_scale(self, supported_scales, target_scale):
        """Find the closest supported Mutter scale to the target.

        Mutter only allows discrete scale values (e.g., 1.0, 1.25, 1.5, 1.75, 2.0).
        """
        if not supported_scales:
            return 1.0
        return min(supported_scales, key=lambda s: abs(s - target_scale))

    def _get_displays_wayland(self):
        """Get list of connected displays via Mutter D-Bus."""
        state = self._mutter_get_current_state()
        if not state:
            self.logger.warning("Wayland: could not query Mutter, falling back to X11")
            return self._get_displays_x11()

        displays = []
        for mon in state['monitors']:
            # A monitor is "primary" if it appears in a logical monitor marked primary
            primary = False
            for lm in state.get('logical_monitors', []):
                if lm.get('primary'):
                    for ms in lm['monitors']:
                        if ms['connector'] == mon['connector']:
                            primary = True
            displays.append({'name': mon['connector'], 'primary': primary})

        return displays

    def _is_display_active_wayland(self, display_name):
        """Check if a display is active (has a logical monitor) via Mutter."""
        state = self._mutter_get_current_state()
        if not state:
            self.logger.warning("Wayland: could not query Mutter, falling back to X11")
            return self._is_display_active_x11(display_name)

        return self._find_logical_monitor(state, display_name) is not None

    def _enable_display_wayland(self, display_name, scale=None):
        """Enable a display via Mutter ApplyMonitorsConfig."""
        state = self._mutter_get_current_state()
        if not state:
            self.logger.warning("Wayland: could not query Mutter, falling back to X11")
            return self._enable_display_x11(display_name, scale)

        monitor = self._find_monitor_in_state(state, display_name)
        if not monitor:
            self.logger.error(f"Monitor {display_name} not found in Mutter state")
            return False

        # Find the preferred or current mode
        target_mode = None
        for m in monitor['modes']:
            if m.get('is_preferred'):
                target_mode = m
                break
        if not target_mode and monitor['modes']:
            target_mode = monitor['modes'][0]
        if not target_mode:
            self.logger.error(f"No modes available for {display_name}")
            return False

        # Determine the Mutter scale
        if scale is not None and scale != 1.0:
            mutter_scale = self._best_scale(target_mode.get('supported_scales', [1.0]), scale)
        else:
            mutter_scale = target_mode.get('preferred_scale', 1.0)

        self.logger.info(f"Wayland: enabling {display_name} mode={target_mode['width']}x{target_mode['height']}"
                        f"@{target_mode['refresh']:.1f}Hz scale={mutter_scale}")

        # Build the logical monitors config: keep all existing + add the new one
        logical_configs = []
        next_x = 0

        # Collect existing logical monitors (excluding any that already have this connector)
        for lm in state.get('logical_monitors', []):
            connectors_in_lm = [ms['connector'] for ms in lm['monitors']]
            if display_name not in connectors_in_lm:
                lm_monitors_spec = []
                for ms in lm['monitors']:
                    # Find the current mode for this monitor
                    mon_info = self._find_monitor_in_state(state, ms['connector'])
                    mode_id = ''
                    if mon_info:
                        for mm in mon_info['modes']:
                            if mm.get('is_current'):
                                mode_id = mm['id']
                                break
                        if not mode_id and mon_info['modes']:
                            mode_id = mon_info['modes'][0]['id']
                    lm_monitors_spec.append((ms['connector'], mode_id, {}))

                logical_configs.append({
                    'x': lm['x'], 'y': lm['y'],
                    'scale': lm['scale'],
                    'transform': lm['transform'],
                    'primary': lm['primary'],
                    'monitors': lm_monitors_spec,
                })
                edge = lm['x'] + self._logical_width(lm, state)
                if edge > next_x:
                    next_x = edge

        # Add the new display to the right
        logical_configs.append({
            'x': next_x, 'y': 0,
            'scale': mutter_scale,
            'transform': 0,
            'primary': False,
            'monitors': [(display_name, target_mode['id'], {})],
        })

        return self._mutter_apply_config(state['serial'], logical_configs)

    def _disable_display_wayland(self, display_name):
        """Disable a display via Mutter ApplyMonitorsConfig."""
        state = self._mutter_get_current_state()
        if not state:
            self.logger.warning("Wayland: could not query Mutter, falling back to X11")
            return self._disable_display_x11(display_name)

        if not self._find_logical_monitor(state, display_name):
            self.logger.info(f"Display {display_name} already disabled")
            return True

        # Rebuild logical monitors excluding the target
        logical_configs = []
        for lm in state.get('logical_monitors', []):
            connectors_in_lm = [ms['connector'] for ms in lm['monitors']]
            if display_name not in connectors_in_lm:
                lm_monitors_spec = []
                for ms in lm['monitors']:
                    mon_info = self._find_monitor_in_state(state, ms['connector'])
                    mode_id = ''
                    if mon_info:
                        for mm in mon_info['modes']:
                            if mm.get('is_current'):
                                mode_id = mm['id']
                                break
                        if not mode_id and mon_info['modes']:
                            mode_id = mon_info['modes'][0]['id']
                    lm_monitors_spec.append((ms['connector'], mode_id, {}))

                logical_configs.append({
                    'x': lm['x'], 'y': lm['y'],
                    'scale': lm['scale'],
                    'transform': lm['transform'],
                    'primary': lm['primary'],
                    'monitors': lm_monitors_spec,
                })

        if not logical_configs:
            self.logger.error("Cannot disable all displays")
            return False

        # Ensure at least one is primary
        has_primary = any(lc['primary'] for lc in logical_configs)
        if not has_primary:
            logical_configs[0]['primary'] = True

        success = self._mutter_apply_config(state['serial'], logical_configs)
        if success:
            self.logger.info(f"Disabled display: {display_name}")
        return success

    def _logical_width(self, logical_monitor, state):
        """Compute the logical width of a logical monitor."""
        for ms in logical_monitor['monitors']:
            mon_info = self._find_monitor_in_state(state, ms['connector'])
            if mon_info:
                for mm in mon_info['modes']:
                    if mm.get('is_current'):
                        return int(mm['width'] / logical_monitor['scale'])
                if mon_info['modes']:
                    return int(mon_info['modes'][0]['width'] / logical_monitor['scale'])
        return 0

    def _mutter_apply_config(self, serial, logical_configs):
        """Apply a display configuration via Mutter ApplyMonitorsConfig.

        Args:
            serial: Config serial from GetCurrentState
            logical_configs: List of logical monitor dicts

        Uses python3-dbus if available, otherwise gdbus.
        Method 1 = temporary (reverts after 20s if not confirmed).
        Method 2 = persistent.
        We use method 2 to match xrandr behavior.
        """
        try:
            import dbus
            return self._mutter_apply_config_dbus(serial, logical_configs)
        except ImportError:
            pass
        except Exception as e:
            self.logger.debug(f"dbus ApplyMonitorsConfig failed: {e}, trying gdbus")

        return self._mutter_apply_config_gdbus(serial, logical_configs)

    def _mutter_apply_config_dbus(self, serial, logical_configs):
        """Apply config via python3-dbus."""
        import dbus
        bus = dbus.SessionBus()
        proxy = bus.get_object('org.gnome.Mutter.DisplayConfig',
                               '/org/gnome/Mutter/DisplayConfig')
        iface = dbus.Interface(proxy, 'org.gnome.Mutter.DisplayConfig')

        # Build the D-Bus argument structure
        # ApplyMonitorsConfig(serial, method, logical_monitors, properties)
        # method: 2 = persistent
        dbus_logical = []
        for lc in logical_configs:
            dbus_monitors = []
            for mon_spec in lc['monitors']:
                # (connector, mode_id, properties_dict)
                dbus_monitors.append(dbus.Struct([
                    dbus.String(mon_spec[0]),
                    dbus.String(mon_spec[1]),
                    dbus.Dictionary(mon_spec[2] if len(mon_spec) > 2 else {},
                                    signature='sv'),
                ], signature='ssa{sv}'))

            dbus_logical.append(dbus.Struct([
                dbus.Int32(lc['x']),
                dbus.Int32(lc['y']),
                dbus.Double(lc['scale']),
                dbus.UInt32(lc['transform']),
                dbus.Boolean(lc['primary']),
                dbus.Array(dbus_monitors, signature='(ssa{sv})'),
            ], signature='iidub a(ssa{sv})'))

        try:
            iface.ApplyMonitorsConfig(
                dbus.UInt32(serial),
                dbus.UInt32(2),  # method=2 persistent
                dbus.Array(dbus_logical, signature='(iiduba(ssa{sv}))'),
                dbus.Dictionary({}, signature='sv'),
            )
            time.sleep(0.3)
            return True
        except Exception as e:
            self.logger.error(f"Mutter ApplyMonitorsConfig failed: {e}")
            return False

    def _mutter_apply_config_gdbus(self, serial, logical_configs):
        """Apply config via gdbus subprocess."""
        try:
            # Build GVariant string for the logical monitors array
            lm_parts = []
            for lc in logical_configs:
                mon_parts = []
                for mon_spec in lc['monitors']:
                    props = mon_spec[2] if len(mon_spec) > 2 else {}
                    props_str = '{}'
                    if props:
                        prop_items = ', '.join(f"'{k}': <{v}>" for k, v in props.items())
                        props_str = '{' + prop_items + '}'
                    mon_parts.append(f"('{mon_spec[0]}', '{mon_spec[1]}', @a{{sv}} {props_str})")
                mons_str = ', '.join(mon_parts)
                primary_str = 'true' if lc['primary'] else 'false'
                lm_parts.append(
                    f"({lc['x']}, {lc['y']}, {lc['scale']:.6f}, "
                    f"uint32 {lc['transform']}, {primary_str}, "
                    f"[{mons_str}])"
                )

            logical_str = '[' + ', '.join(lm_parts) + ']'

            cmd = [
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Mutter.DisplayConfig',
                '--object-path', '/org/gnome/Mutter/DisplayConfig',
                '--method', 'org.gnome.Mutter.DisplayConfig.ApplyMonitorsConfig',
                str(serial), 'uint32 2', logical_str, '@a{sv} {}'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                self.logger.error(f"gdbus ApplyMonitorsConfig failed: {result.stderr.strip()}")
                return False

            time.sleep(0.3)
            return True

        except Exception as e:
            self.logger.error(f"Failed to apply Mutter config via gdbus: {e}")
            return False

    def _get_display_geometry_wayland(self, display_name):
        """Get display geometry from Mutter state."""
        state = self._mutter_get_current_state()
        if not state:
            self.logger.warning("Wayland: could not query Mutter, falling back to X11")
            return self._get_display_geometry_x11(display_name)

        lm = self._find_logical_monitor(state, display_name)
        if not lm:
            self.logger.warning(f"Could not find geometry for {display_name}")
            return None

        # Find the current mode to get the pixel dimensions
        monitor = self._find_monitor_in_state(state, display_name)
        width, height = 0, 0
        if monitor:
            for m in monitor['modes']:
                if m.get('is_current'):
                    width = m['width']
                    height = m['height']
                    break
            if not width and monitor['modes']:
                width = monitor['modes'][0]['width']
                height = monitor['modes'][0]['height']

        # Logical size accounts for Mutter scaling
        scale = lm.get('scale', 1.0)
        logical_width = int(width / scale) if scale else width
        logical_height = int(height / scale) if scale else height

        return {
            'width': logical_width,
            'height': logical_height,
            'x': lm['x'],
            'y': lm['y'],
        }

    def _display_image_wayland(self, image_path, geometry):
        """Display fullscreen image using imv (preferred on Wayland), fallback to feh."""
        if self._command_exists('imv'):
            try:
                cmd = ['imv', '-f', image_path]
                self.logger.info("Displaying fullscreen image using imv (Wayland)")
                self.logger.warning("imv may not position on correct display automatically")
                process = subprocess.Popen(cmd)
                time.sleep(0.5)
                return process
            except Exception as e:
                self.logger.error(f"Failed to display image with imv: {e}")

        # feh may work under XWayland
        if self._command_exists('feh'):
            try:
                cmd = [
                    'feh',
                    '--fullscreen',
                    '--auto-zoom',
                    '--no-menus',
                    '--hide-pointer',
                    image_path
                ]
                self.logger.info("Displaying fullscreen image using feh (XWayland fallback)")
                process = subprocess.Popen(cmd)
                time.sleep(0.5)
                return process
            except Exception as e:
                self.logger.error(f"Failed to display image with feh: {e}")

        self.logger.error("Neither imv nor feh is installed. Please install one:")
        self.logger.error("  For Wayland: sudo apt install imv")
        self.logger.error("  For X11: sudo apt install feh")
        return None

    # ------------------------------------------------------------------
    # KDE Wayland backend (kscreen-doctor)
    # ------------------------------------------------------------------

    def _kscreen_get_outputs(self):
        """Parse kscreen-doctor output to get display info.

        Returns a list of dicts: {
            'name': connector name (e.g. 'eDP-1'),
            'enabled': bool,
            'resolution': 'WxH' or None,
            'position': 'X,Y' or None,
            'scale': float,
            'priority': int or None,
        }
        """
        try:
            result = subprocess.run(
                ['kscreen-doctor', '--outputs'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                self.logger.error(f"kscreen-doctor failed: {result.stderr.strip()}")
                return []

            outputs = []
            current = None
            for line in result.stdout.splitlines():
                stripped = line.strip()

                # New output block: "Output: N eDP-1 ..."
                if stripped.startswith('Output:'):
                    if current:
                        outputs.append(current)
                    parts = stripped.split()
                    # parts: ['Output:', '<id>', '<name>', ...]
                    name = parts[2] if len(parts) > 2 else 'unknown'
                    enabled = 'enabled' in stripped.lower()
                    current = {
                        'name': name,
                        'enabled': enabled,
                        'resolution': None,
                        'position': None,
                        'scale': 1.0,
                        'priority': None,
                    }

                if current is None:
                    continue

                if stripped.startswith('Geometry:'):
                    # "Geometry: 0,0 2880x1800"
                    geo_parts = stripped.split()
                    if len(geo_parts) >= 3:
                        current['position'] = geo_parts[1]
                        current['resolution'] = geo_parts[2]

                if stripped.startswith('Scale:'):
                    try:
                        current['scale'] = float(stripped.split()[1])
                    except (IndexError, ValueError):
                        pass

                if stripped.startswith('Priority:'):
                    try:
                        current['priority'] = int(stripped.split()[1])
                    except (IndexError, ValueError):
                        pass

            if current:
                outputs.append(current)

            return outputs

        except FileNotFoundError:
            self.logger.error("kscreen-doctor not found. Install kscreen: sudo apt install kscreen")
            return []
        except Exception as e:
            self.logger.error(f"Failed to query kscreen-doctor: {e}")
            return []

    def _kscreen_find_output(self, display_name):
        """Find a specific output by connector name."""
        for out in self._kscreen_get_outputs():
            if out['name'] == display_name:
                return out
        return None

    def _get_displays_kde(self):
        """Get list of connected displays via kscreen-doctor."""
        outputs = self._kscreen_get_outputs()
        if not outputs:
            self.logger.warning("KDE: kscreen-doctor returned no outputs, falling back to X11")
            return self._get_displays_x11()

        displays = []
        for out in outputs:
            displays.append({
                'name': out['name'],
                'primary': out.get('priority') == 1,
            })
        return displays

    def _is_display_active_kde(self, display_name):
        """Check if a display is active via kscreen-doctor."""
        out = self._kscreen_find_output(display_name)
        if out is None:
            return False
        return out['enabled']

    def _enable_display_kde(self, display_name, scale=None):
        """Enable a display via kscreen-doctor."""
        try:
            # Determine resolution
            if display_name == "eDP-1":
                res = f"{self.OLED_RESOLUTION_WH[0]}x{self.OLED_RESOLUTION_WH[1]}"
            elif display_name == "eDP-2":
                res = f"{self.EINK_RESOLUTION_WH[0]}x{self.EINK_RESOLUTION_WH[1]}"
            else:
                res = None

            # Build kscreen-doctor command
            # Format: output.<name>.enable output.<name>.mode.<WxH> output.<name>.scale.<S>
            parts = [f'output.{display_name}.enable']
            if res:
                parts.append(f'output.{display_name}.mode.{res}')
            if scale is not None and scale != 1.0:
                parts.append(f'output.{display_name}.scale.{scale}')

            cmd = ['kscreen-doctor'] + parts
            self.logger.info(f"KDE: enabling {display_name}: {' '.join(cmd)}")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                self.logger.error(f"kscreen-doctor enable failed: {result.stderr.strip()}")
                return False

            time.sleep(0.5)

            if self._is_display_active_kde(display_name):
                scale_info = f" with {scale}x scale" if scale and scale != 1.0 else ""
                self.logger.info(f"Enabled display: {display_name}{scale_info}")
                return True
            else:
                self.logger.error(f"Failed to enable display: {display_name} (not active after command)")
                return False

        except Exception as e:
            self.logger.error(f"Failed to enable display via kscreen-doctor: {e}")
            return False

    def _disable_display_kde(self, display_name):
        """Disable a display via kscreen-doctor."""
        try:
            cmd = ['kscreen-doctor', f'output.{display_name}.disable']
            self.logger.info(f"KDE: disabling {display_name}")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                self.logger.error(f"kscreen-doctor disable failed: {result.stderr.strip()}")
                return False

            time.sleep(0.5)

            if not self._is_display_active_kde(display_name):
                self.logger.info(f"Disabled display: {display_name}")
                return True
            else:
                self.logger.error(f"Failed to disable display: {display_name} (still active)")
                return False

        except Exception as e:
            self.logger.error(f"Failed to disable display via kscreen-doctor: {e}")
            return False

    def _get_display_geometry_kde(self, display_name):
        """Get display geometry from kscreen-doctor."""
        out = self._kscreen_find_output(display_name)
        if not out or not out['enabled']:
            self.logger.warning(f"Could not find geometry for {display_name}")
            return None

        try:
            # Parse position "X,Y"
            x, y = 0, 0
            if out['position']:
                pos_parts = out['position'].split(',')
                x = int(pos_parts[0])
                y = int(pos_parts[1])

            # Parse resolution "WxH"
            width, height = 0, 0
            if out['resolution']:
                res_parts = out['resolution'].split('x')
                width = int(res_parts[0])
                height = int(res_parts[1])

            # Apply scale to get logical size
            scale = out.get('scale', 1.0)
            logical_width = int(width / scale) if scale else width
            logical_height = int(height / scale) if scale else height

            return {
                'width': logical_width,
                'height': logical_height,
                'x': x,
                'y': y,
            }

        except (ValueError, IndexError) as e:
            self.logger.error(f"Failed to parse geometry for {display_name}: {e}")
            return None

    # ------------------------------------------------------------------
    # Touchscreen input mapping
    # ------------------------------------------------------------------

    def map_touch_to_display(self, display_name):
        """Map all touchscreen input devices to the specified display.

        This fixes misplaced touch coordinates on dual-screen setups
        by remapping the touch digitizer to the active display output.

        Args:
            display_name: Display connector name (e.g., 'eDP-1', 'eDP-2')

        Returns:
            bool: True if at least one device was mapped, False otherwise
        """
        if self.session_type == 'x11':
            return self._map_touch_x11(display_name)
        elif self.session_type == 'wayland':
            if self._use_kde_wayland():
                return self._map_touch_wayland_kde(display_name)
            return self._map_touch_wayland_gnome(display_name)
        else:
            self.logger.warning("Unknown session type, trying X11 touch mapping")
            return self._map_touch_x11(display_name)

    def _get_touchscreen_xinput_ids(self):
        """Find touchscreen device IDs from xinput.

        Returns a list of (device_id, device_name) tuples.
        Uses two strategies:
        1. Name-based: match devices with 'touch' (not 'touchpad') in name
        2. Type-based: check xinput list-props for TouchClass devices

        On the ThinkBook Plus Gen 4, the touchscreen may appear as an
        ELAN device (e.g., 'ELAN0732:00 04F3:2234') without 'touch' in name.
        """
        try:
            result = subprocess.run(
                ['xinput', 'list'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return []

            candidates = []
            for line in result.stdout.splitlines():
                if 'id=' not in line:
                    continue
                try:
                    id_part = line.split('id=')[1].split()[0].strip()
                    dev_id = int(id_part)
                    name = line.split('↳')[-1].split('id=')[0].strip() if '↳' in line else line.split('id=')[0].strip()
                    candidates.append((dev_id, name, line.lower()))
                except (ValueError, IndexError):
                    continue

            devices = []
            seen_ids = set()

            # Pass 1: name-based matching (fast)
            for dev_id, name, lower in candidates:
                is_touch = 'touch' in lower and 'touchpad' not in lower
                # Also match ELAN digitizer devices (common on ThinkBooks)
                is_elan_digitizer = 'elan' in lower and 'touchpad' not in lower and 'fingerprint' not in lower
                if is_touch or is_elan_digitizer:
                    if dev_id not in seen_ids:
                        devices.append((dev_id, name))
                        seen_ids.add(dev_id)

            # Pass 2: type-based — check if remaining pointer devices have touch capability
            if not devices:
                self.logger.info("No touchscreen found by name, probing pointer devices for touch capability...")
                for dev_id, name, lower in candidates:
                    if dev_id in seen_ids:
                        continue
                    if 'slave  pointer' not in lower and 'slave pointer' not in lower:
                        continue
                    if 'touchpad' in lower or 'mouse' in lower or 'trackpoint' in lower:
                        continue
                    # Check if this device has AbsMT axes (touchscreen indicator)
                    try:
                        props = subprocess.run(
                            ['xinput', 'list-props', str(dev_id)],
                            capture_output=True, text=True, timeout=2
                        )
                        if 'Abs MT' in props.stdout or 'Touch' in props.stdout:
                            devices.append((dev_id, name))
                            seen_ids.add(dev_id)
                    except Exception:
                        pass

            return devices

        except Exception as e:
            self.logger.error(f"Failed to list xinput devices: {e}")
            return []

    def _map_touch_x11(self, display_name):
        """Map touchscreen devices to a display using xinput (X11)."""
        devices = self._get_touchscreen_xinput_ids()
        if not devices:
            self.logger.info("No touchscreen devices found via xinput")
            return False

        mapped = False
        for dev_id, dev_name in devices:
            try:
                result = subprocess.run(
                    ['xinput', 'map-to-output', str(dev_id), display_name],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    self.logger.info(f"Mapped touch device '{dev_name}' (id={dev_id}) to {display_name}")
                    mapped = True
                else:
                    self.logger.warning(f"Failed to map touch device '{dev_name}' (id={dev_id}): {result.stderr.strip()}")
            except Exception as e:
                self.logger.error(f"Error mapping touch device {dev_id}: {e}")

        return mapped

    def _get_touchscreen_sysfs(self):
        """Find touchscreen devices from /proc/bus/input/devices.

        Returns a list of dicts with 'name', 'sysfs', 'event_node'.
        Works on both X11 and Wayland.
        """
        devices = []
        try:
            with open('/proc/bus/input/devices', 'r') as f:
                content = f.read()

            blocks = content.split('\n\n')
            for block in blocks:
                lines = block.strip().splitlines()
                if not lines:
                    continue

                name = ''
                handlers = ''
                for line in lines:
                    if line.startswith('N: Name='):
                        name = line.split('=', 1)[1].strip().strip('"')
                    elif line.startswith('H: Handlers='):
                        handlers = line.split('=', 1)[1].strip()

                lower_name = name.lower()
                # Match touchscreen devices, exclude touchpads and fingerprint
                is_touch = 'touch' in lower_name and 'touchpad' not in lower_name
                is_elan_digitizer = 'elan' in lower_name and 'touchpad' not in lower_name and 'fingerprint' not in lower_name
                if is_touch or is_elan_digitizer:
                    # Find event node
                    event_node = None
                    for handler in handlers.split():
                        if handler.startswith('event'):
                            event_node = f'/dev/input/{handler}'
                            break
                    if event_node:
                        devices.append({'name': name, 'event_node': event_node})

        except Exception as e:
            self.logger.error(f"Failed to read /proc/bus/input/devices: {e}")

        return devices

    def _map_touch_wayland_gnome(self, display_name):
        """Map touchscreen to display on GNOME Wayland via gsettings.

        GNOME uses per-device settings under:
        org.gnome.desktop.peripherals.touchscreen:<device-vid-pid>
        with key 'output' = ['connector', 'vendor', 'product', 'serial']
        """
        # On GNOME Wayland, when only one display is active, Mutter
        # should auto-map touch to it. But during transitions or if
        # both are active, we need to explicitly set the mapping.
        state = self._mutter_get_current_state()
        if not state:
            self.logger.warning("Cannot map touch on Wayland: Mutter state unavailable")
            return False

        # Find the monitor spec for the target display
        monitor = self._find_monitor_in_state(state, display_name)
        if not monitor:
            self.logger.warning(f"Monitor {display_name} not found in Mutter state for touch mapping")
            return False

        # Build the output value for gsettings
        output_value = f"['{monitor['connector']}', '{monitor['vendor']}', '{monitor['product']}', '{monitor['serial']}']"

        # Find touchscreen devices and set their output mapping
        devices = self._get_touchscreen_sysfs()
        if not devices:
            self.logger.info("No touchscreen devices found for Wayland touch mapping")
            return False

        mapped = False
        for dev in devices:
            # Derive the gsettings device ID from the device name
            # GNOME uses the format: /org/gnome/desktop/peripherals/touchscreens/<vid>:<pid>/
            # We need to find the vid:pid. Try to get it from sysfs.
            vid_pid = self._get_device_vid_pid(dev['event_node'])
            if not vid_pid:
                self.logger.warning(f"Could not determine VID:PID for {dev['name']}, skipping")
                continue

            schema_id = f"org.gnome.desktop.peripherals.touchscreen:{vid_pid}"
            try:
                result = subprocess.run(
                    ['gsettings', 'set', schema_id, 'output', output_value],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    self.logger.info(f"Mapped touch '{dev['name']}' to {display_name} via gsettings")
                    mapped = True
                else:
                    self.logger.warning(f"gsettings failed for {dev['name']}: {result.stderr.strip()}")
            except Exception as e:
                self.logger.error(f"Error setting gsettings for touch device: {e}")

        if not mapped:
            self.logger.info("Wayland touch mapping: no devices mapped via gsettings (Mutter may auto-map)")

        return mapped

    def _map_touch_wayland_kde(self, display_name):
        """Map touchscreen to display on KDE Wayland.

        KDE Plasma uses libinput device configuration via kscreen or
        the KWin scripting interface. As a fallback, the auto-mapping
        by the compositor should work when only one display is active.
        """
        self.logger.info(f"KDE Wayland: touch should auto-map to active display {display_name}")
        return True

    def _get_device_vid_pid(self, event_node):
        """Get VID:PID for an input device from sysfs.

        Args:
            event_node: e.g., '/dev/input/event5'

        Returns:
            str like '04F3:2234' or None
        """
        try:
            event_name = os.path.basename(event_node)
            # Read the device uevent to get HID_UNIQ or look at the id
            id_path = f'/sys/class/input/{event_name}/device/id'
            if os.path.isdir(id_path):
                with open(os.path.join(id_path, 'vendor'), 'r') as f:
                    vendor = f.read().strip()
                with open(os.path.join(id_path, 'product'), 'r') as f:
                    product = f.read().strip()
                return f"{vendor}:{product}"

            # Try parent device
            device_path = os.path.realpath(f'/sys/class/input/{event_name}/device')
            id_path = os.path.join(device_path, 'id')
            if os.path.isdir(id_path):
                with open(os.path.join(id_path, 'vendor'), 'r') as f:
                    vendor = f.read().strip()
                with open(os.path.join(id_path, 'product'), 'r') as f:
                    product = f.read().strip()
                return f"{vendor}:{product}"

        except Exception as e:
            self.logger.debug(f"Failed to get VID:PID for {event_node}: {e}")

        return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _command_exists(self, command):
        """Check if a command exists in PATH."""
        try:
            subprocess.run(
                ['which', command],
                capture_output=True,
                check=True,
                timeout=2
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
