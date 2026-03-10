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


class ThemeManager:
    """Manage GTK theme switching across desktop environments.

    Supports GNOME (gsettings) and XFCE (xfconf-query).
    Auto-detects the desktop environment at startup.
    """

    # Mapping for GNOME color-scheme preference
    _GNOME_COLOR_SCHEME = {
        'HighContrast': 'default',
        'Adwaita-dark': 'prefer-dark',
    }

    def __init__(self, logger):
        self.logger = logger
        self.desktop_env = self._detect_desktop_environment()
        self.logger.info(f"ThemeManager: desktop={self.desktop_env}")

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
    # Public API
    # ------------------------------------------------------------------

    def set_theme(self, theme_name):
        """Set the GTK theme.

        Args:
            theme_name: Theme name (e.g., 'HighContrast', 'Adwaita-dark')
                        For KDE, mapped to Plasma color schemes.

        Returns:
            bool: True if successful, False otherwise
        """
        if self.desktop_env == 'gnome':
            return self._set_gnome_theme(theme_name)
        elif self.desktop_env == 'kde':
            return self._set_kde_theme(theme_name)
        elif self.desktop_env == 'xfce':
            return self._set_xfce_theme(theme_name)
        else:
            self.logger.warning(f"Unknown desktop environment, trying gsettings then xfconf-query")
            if self._set_gnome_theme(theme_name):
                return True
            return self._set_xfce_theme(theme_name)

    def get_current_theme(self):
        """Get the current GTK theme name.

        Returns:
            str: Current theme name, or None if detection failed
        """
        if self.desktop_env == 'gnome':
            return self._get_gnome_theme()
        elif self.desktop_env == 'kde':
            return self._get_kde_theme()
        elif self.desktop_env == 'xfce':
            return self._get_xfce_theme()
        else:
            theme = self._get_gnome_theme()
            if theme:
                return theme
            return self._get_xfce_theme()

    # ------------------------------------------------------------------
    # GNOME backend (gsettings)
    # ------------------------------------------------------------------

    def _set_gnome_theme(self, theme_name):
        """Set theme via gsettings (GNOME / GTK)."""
        try:
            subprocess.run([
                'gsettings', 'set',
                'org.gnome.desktop.interface', 'gtk-theme',
                theme_name
            ], check=True, capture_output=True, timeout=5)

            # Also set color-scheme if we know the mapping
            color_scheme = self._GNOME_COLOR_SCHEME.get(theme_name)
            if color_scheme:
                subprocess.run([
                    'gsettings', 'set',
                    'org.gnome.desktop.interface', 'color-scheme',
                    color_scheme
                ], check=True, capture_output=True, timeout=5)

            self.logger.info(f"GNOME: switched to {theme_name} theme")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to set GNOME theme: {e}")
            return False
        except FileNotFoundError:
            self.logger.error("gsettings not found (not running GNOME?)")
            return False

    def _get_gnome_theme(self):
        """Get current theme via gsettings."""
        try:
            result = subprocess.run([
                'gsettings', 'get',
                'org.gnome.desktop.interface', 'gtk-theme'
            ], capture_output=True, text=True, check=True, timeout=5)
            # gsettings returns value with quotes, e.g. "'Adwaita-dark'"
            return result.stdout.strip().strip("'")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # KDE backend (plasma-apply-colorscheme)
    # ------------------------------------------------------------------

    # Map GTK theme names used by the app to Plasma color schemes
    _KDE_THEME_MAP = {
        'HighContrast': 'BreezeHighContrast',
        'Adwaita-dark': 'BreezeDark',
        'Adwaita': 'BreezeLight',
    }

    _KDE_THEME_REVERSE = {v: k for k, v in _KDE_THEME_MAP.items()}

    def _set_kde_theme(self, theme_name):
        """Set color scheme via plasma-apply-colorscheme (KDE Plasma)."""
        plasma_scheme = self._KDE_THEME_MAP.get(theme_name, theme_name)
        try:
            subprocess.run([
                'plasma-apply-colorscheme', plasma_scheme
            ], check=True, capture_output=True, timeout=5)
            self.logger.info(f"KDE: switched to {plasma_scheme} color scheme")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to set KDE color scheme: {e}")
            return False
        except FileNotFoundError:
            self.logger.error("plasma-apply-colorscheme not found (not running KDE Plasma?)")
            return False

    def _get_kde_theme(self):
        """Get current color scheme via plasma-apply-colorscheme."""
        try:
            result = subprocess.run([
                'plasma-apply-colorscheme', '--list-schemes'
            ], capture_output=True, text=True, timeout=5)
            # Lines with " (current)" suffix indicate the active scheme
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.endswith('(current color scheme)') or line.endswith('(current)'):
                    scheme = line.rsplit('(', 1)[0].strip().lstrip('* ')
                    # Map back to GTK name if possible
                    return self._KDE_THEME_REVERSE.get(scheme, scheme)
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # XFCE backend (xfconf-query)
    # ------------------------------------------------------------------

    def _set_xfce_theme(self, theme_name):
        """Set theme via xfconf-query (XFCE)."""
        try:
            subprocess.run([
                'xfconf-query',
                '-c', 'xsettings',
                '-p', '/Net/ThemeName',
                '-s', theme_name
            ], check=True, capture_output=True, timeout=5)
            self.logger.info(f"XFCE: switched to {theme_name} theme")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to set XFCE theme: {e}")
            return False
        except FileNotFoundError:
            self.logger.error("xfconf-query not found (not running XFCE?)")
            return False

    def _get_xfce_theme(self):
        """Get current theme via xfconf-query."""
        try:
            result = subprocess.run([
                'xfconf-query',
                '-c', 'xsettings',
                '-p', '/Net/ThemeName'
            ], capture_output=True, text=True, check=True, timeout=5)
            return result.stdout.strip()
        except Exception:
            return None
