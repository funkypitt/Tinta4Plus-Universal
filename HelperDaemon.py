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
ThinkBook Plus Gen 4 IRU E-Ink Control Helper
Privileged daemon for hardware control via Unix socket

Requires: sudo/pkexec to run
Dependencies: pyusb, portio (or python-periphery)

Security model
--------------
* Exactly one daemon runs at a time (flock on LOCK_FILE). A second copy
  exits instead of stealing the first one's socket.
* The socket is only usable by root and by the user who launched the
  daemon (pkexec/sudo tell us who that is). Every accepted connection is
  checked with SO_PEERCRED; the file mode is a second line of defence.
* Frames are capped at MAX_MESSAGE_BYTES, idle clients are dropped, and
  the number of concurrent clients is bounded, so an unprivileged local
  process cannot exhaust the memory or file descriptors of a root process.
"""

import os
import sys
import json
import time
import fcntl
import socket
import struct
import signal
import threading
import logging
import logging.handlers
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from WatchdogTimer import WatchdogTimer
from ECController import ECController
from EInkUSBController import EInkUSBController
from GlobalHotkeyListener import GlobalHotkeyListener

# Configuration
SOCKET_PATH = '/tmp/tinta4plusu.sock'          # must match HelperClient users (GUI, toggle-eink)
LOCK_FILE = '/run/lock/tinta4plusu-helper.lock'
PID_FILE = '/run/tinta4plusu-helper.pid'
LOG_FILE = '/var/log/tinta4plusu-helper.log'
WATCHDOG_TIMEOUT = 60.0  # seconds without any client command before the daemon exits
HTTP_PORT = 19849  # localhost HTTP API for browser extensions (e.g. PageTurn)
HTTP_MIN_REFRESH_INTERVAL = 1.5  # seconds; protects the panel from refresh loops
LOG_LEVEL = logging.INFO

MAX_MESSAGE_BYTES = 64 * 1024   # a command is a few hundred bytes of JSON
CLIENT_IDLE_TIMEOUT = 90.0      # seconds; the GUI sends a keepalive every 5 s
MAX_CLIENTS = 8                 # GUI + toggle-eink + a few spares

BRIGHTNESS_MIN, BRIGHTNESS_MAX = 0, 8


def _make_http_handler(daemon):
    """Create an HTTP request handler bound to the given daemon instance."""

    class _Handler(BaseHTTPRequestHandler):
        timeout = 10  # socket timeout per connection; one slow client can't block the API

        def do_POST(self):
            if self.path == '/refresh-eink':
                try:
                    if not (daemon.eink and daemon.eink_enabled):
                        self._respond(503, {'success': False, 'error': 'eInk not enabled'})
                        return
                    if not daemon._http_refresh_allowed():
                        self._respond(429, {'success': False, 'error': 'refresh rate limited'})
                        return
                    daemon._eink_op(daemon.eink.refresh_full)
                    daemon.logger.info("HTTP API: eInk refresh")
                    self._respond(200, {'success': True, 'message': 'E-Ink refreshed'})
                except Exception as e:
                    daemon.logger.error(f"HTTP API refresh error: {e}")
                    self._respond(500, {'success': False, 'error': str(e)})
            else:
                self._respond(404, {'error': 'not found'})

        def do_OPTIONS(self):
            # CORS preflight
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def _respond(self, code, body):
            self.send_response(code)
            self._cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def _cors_headers(self):
            # The API only exposes a rate-limited, side-effect-light refresh,
            # which is why a wildcard origin is tolerable here.
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')

        def log_message(self, fmt, *args):
            # Silence default stderr logging; we use our own logger
            pass

    return _Handler


class HelperDaemon:
    """Main helper daemon with socket server and hardware controllers"""

    def __init__(self, logger):
        self.logger = logger
        self.running = False
        self.socket_path = SOCKET_PATH
        self.pid_file = PID_FILE
        self.server_socket = None
        self._socket_ino = None      # inode of the socket *we* bound
        self._lock_fd = None

        # Hardware controllers
        self.eink = None
        self.ec = None

        # eInk state tracking (for global hotkeys)
        self.eink_enabled = False
        self.brightness_level = 4  # default
        self._pending_notifications = []
        self._notify_lock = threading.Lock()
        # Serializes T-CON USB transfers across socket, hotkey and HTTP threads
        self._usb_lock = threading.Lock()
        self._http_last_refresh = 0.0
        self._http_lock = threading.Lock()

        # Client bookkeeping
        self._clients_lock = threading.Lock()
        self._client_count = 0
        self.allowed_uids = self._allowed_uids()

        # HTTP API server (for browser extensions like PageTurn)
        self.http_server = None

        # Global hotkey listener
        self.hotkey_listener = GlobalHotkeyListener(
            self.logger,
            on_brightness_up=self._hotkey_brightness_up,
            on_brightness_down=self._hotkey_brightness_down,
            on_refresh=self._hotkey_refresh,
            on_toggle=self._hotkey_toggle,
            on_reader=self._hotkey_reader,
        )

        # Watchdog — never fires while a command is being handled
        self._busy_lock = threading.Lock()
        self._busy_commands = 0
        self.watchdog = WatchdogTimer(WATCHDOG_TIMEOUT, self.shutdown, self.logger,
                                      is_busy=lambda: self._busy_commands > 0)

        # Shutdown may be requested by the watchdog, a client, a signal and
        # run()'s finally block at the same time; only the first one acts.
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        self.logger.info(f"Received signal {signum}, shutting down")
        self.shutdown()

    # ------------------------------------------------------------------
    # Single instance, PID file, socket
    # ------------------------------------------------------------------

    @staticmethod
    def _allowed_uids():
        """Return the set of uids allowed to talk to us, or None for 'anyone'.

        pkexec exports PKEXEC_UID and sudo exports SUDO_UID for the invoking
        user. Root is always allowed.
        """
        uids = {0}
        for var in ('PKEXEC_UID', 'SUDO_UID'):
            value = os.environ.get(var)
            if value and value.isdigit():
                uids.add(int(value))
        if uids == {0}:
            return None
        return uids

    def _acquire_instance_lock(self):
        """Hold an exclusive flock for the daemon's lifetime; False if another daemon holds it."""
        try:
            os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
            fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            self.logger.warning(f"Cannot open lock file {LOCK_FILE}: {e} — continuing without it")
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd
        return True

    def _release_instance_lock(self):
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _create_pid_file(self):
        """Create PID file (informational; the flock is what enforces single instance)"""
        try:
            with open(self.pid_file, 'w') as f:
                f.write(f"{os.getpid()}\n")
            self.logger.info(f"Created PID file: {self.pid_file}")
        except Exception as e:
            self.logger.error(f"Failed to create PID file: {e}")

    def _remove_pid_file(self):
        """Remove PID file if it is ours"""
        try:
            with open(self.pid_file) as f:
                if f.read().strip() != str(os.getpid()):
                    return
            os.remove(self.pid_file)
            self.logger.info("Removed PID file")
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.warning(f"Failed to remove PID file: {e}")

    def _socket_in_use(self):
        """True if something is still accepting connections at socket_path."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(self.socket_path)
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _create_socket(self):
        """Create Unix domain socket"""
        if os.path.lexists(self.socket_path):
            if self._socket_in_use():
                raise RuntimeError(
                    f"{self.socket_path} is served by another process — not taking it over")
            self.logger.info("Removing stale socket file")
            os.remove(self.socket_path)

        old_umask = os.umask(0o077)   # create the socket with no access at all ...
        try:
            self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_socket.bind(self.socket_path)
        finally:
            os.umask(old_umask)
        self.server_socket.listen(4)
        self._socket_ino = os.stat(self.socket_path).st_ino

        # ... then open it to exactly the launching user (root always can).
        if self.allowed_uids:
            user_uids = sorted(self.allowed_uids - {0})
            if user_uids:
                os.chown(self.socket_path, user_uids[0], -1)
            os.chmod(self.socket_path, 0o600)
            self.logger.info(f"Listening on socket: {self.socket_path} (uids {sorted(self.allowed_uids)})")
        else:
            os.chmod(self.socket_path, 0o666)
            self.logger.warning(f"Listening on socket: {self.socket_path} — launching user unknown "
                                "(no PKEXEC_UID/SUDO_UID), any local user may connect")

    def _remove_socket(self):
        """Close the listening socket and unlink the path — only if it is still ours."""
        try:
            if self.server_socket:
                self.server_socket.close()
                self.server_socket = None
            try:
                st = os.stat(self.socket_path)
            except FileNotFoundError:
                return
            if self._socket_ino is not None and st.st_ino == self._socket_ino:
                os.remove(self.socket_path)
                self.logger.info("Removed socket")
            else:
                self.logger.info("Socket path now belongs to another daemon, leaving it")
        except Exception as e:
            self.logger.warning(f"Failed to remove socket: {e}")

    # ------------------------------------------------------------------
    # Hardware
    # ------------------------------------------------------------------

    def initialize_hardware(self):
        """Initialize hardware controllers"""
        try:
            # Initialize EC controller
            self.logger.info("Initializing EC controller")
            self.ec = ECController(self.logger)

            # Check if EC access is available
            ec_status = self.ec.get_access_status()
            if not ec_status['available']:
                self.logger.warning(f"EC access not available: {ec_status['error_message']}")
                # Continue anyway - E-Ink will still work

            # Initialize E-Ink USB controller. A missing T-CON is not fatal:
            # the EC (frontlight) still works, and _eink_op() retries the
            # connection on the next eInk command.
            self.logger.info("Initializing E-Ink USB controller")
            self.eink = EInkUSBController(self.logger)
            try:
                self.eink.connect()
            except Exception as e:
                self.logger.warning(f"E-Ink T-CON not available, running in EC-only mode: {e}")

            self.logger.info("Hardware initialization complete")
            return True

        except Exception as e:
            self.logger.error(f"Hardware initialization failed: {e}")
            return False

    def cleanup_hardware(self):
        """Cleanup hardware connections.

        If we are going away while the eInk is in use (GUI crashed, watchdog
        fired), switch the frontlight off so it does not drain the battery
        unattended. The T-CON is left as it is: powering it off here would
        blank the display the user may still be looking at, with no GUI
        left to bring the OLED back.
        """
        if self.eink_enabled and self.ec and self.ec.access_available:
            try:
                self.ec.disable_frontlight()
                self.logger.info("Frontlight switched off at shutdown")
            except Exception as e:
                self.logger.warning(f"Could not switch frontlight off at shutdown: {e}")
        if self.eink:
            with self._usb_lock:
                self.eink.disconnect()
        self.logger.info("Hardware cleanup complete")

    def _ensure_eink(self):
        """(Re)connect to the T-CON if needed. Caller must hold _usb_lock.

        Returns True if a new connection was made.
        """
        try:
            return self.eink.ensure_connected()
        except Exception as e:
            raise RuntimeError(f"E-Ink T-CON not available: {e}")

    def _eink_op(self, op):
        """Run a T-CON operation with exclusive access to the USB device"""
        with self._usb_lock:
            self._ensure_eink()
            return op()

    def _tcon_available(self):
        return bool(self.eink is not None and getattr(self.eink, 'dev', None) is not None)

    def _http_refresh_allowed(self):
        with self._http_lock:
            now = time.monotonic()
            if now - self._http_last_refresh < HTTP_MIN_REFRESH_INTERVAL:
                return False
            self._http_last_refresh = now
            return True

    # ------------------------------------------------------------------
    # Global hotkey callbacks (called from evdev listener thread)
    #
    # Each callback answers *synchronously* whether it owns the key right
    # now (so the listener knows whether to swallow it) and hands the slow
    # hardware work to the listener's worker thread.
    # ------------------------------------------------------------------

    def _queue_notification(self, notif):
        """Queue a notification for the GUI to pick up on next keepalive."""
        with self._notify_lock:
            self._pending_notifications.append(notif)

    def _drain_notifications(self):
        """Return and clear all pending notifications."""
        with self._notify_lock:
            notifs = self._pending_notifications[:]
            self._pending_notifications.clear()
        return notifs

    def _frontlight_hotkeys_active(self):
        return self.eink_enabled and self.ec is not None and self.ec.access_available

    def _hotkey_brightness_up(self):
        """Handle global brightness-up key. Returns True if consumed."""
        if not self._frontlight_hotkeys_active():
            return False
        self.hotkey_listener.run_async(self._do_brightness_step, +1)
        return True

    def _hotkey_brightness_down(self):
        """Handle global brightness-down key. Returns True if consumed."""
        if not self._frontlight_hotkeys_active():
            return False
        self.hotkey_listener.run_async(self._do_brightness_step, -1)
        return True

    def _do_brightness_step(self, delta):
        new_level = min(BRIGHTNESS_MAX, max(BRIGHTNESS_MIN, self.brightness_level + delta))
        if new_level == self.brightness_level:
            return
        try:
            if new_level == 0:
                # Fully disable frontlight at brightness 0 (PWM 0 can still glow)
                self.ec.disable_frontlight()
            else:
                if self.brightness_level == 0:
                    self.ec.enable_frontlight()
                self.ec.set_brightness(new_level)
            self.brightness_level = new_level
            self.logger.info(f"Hotkey: brightness → {new_level}")
            self._queue_notification({'type': 'brightness', 'level': new_level})
        except Exception as e:
            self.logger.error(f"Hotkey brightness error: {e}")

    def _hotkey_refresh(self):
        """Handle global refresh key (Help / Fn+F9). Returns True if consumed."""
        if not (self.eink_enabled and self.eink):
            return False
        self.hotkey_listener.run_async(self._do_refresh)
        return True

    def _do_refresh(self):
        try:
            self._eink_op(self.eink.refresh_full)
            self.logger.info("Hotkey: eInk refresh")
            self._queue_notification({'type': 'refresh'})
        except Exception as e:
            self.logger.error(f"Hotkey refresh error: {e}")

    def _hotkey_toggle(self):
        """Handle Super+P display toggle hotkey.

        Queues a 'toggle' notification that the GUI picks up on the next
        keepalive. The daemon itself cannot switch displays (that needs the
        user's session), so without a GUI the key does nothing.
        """
        self.logger.info("Hotkey: Super+P toggle requested")
        self._queue_notification({'type': 'toggle'})
        return True

    def _hotkey_reader(self):
        """Handle Super+Shift+P: queue a tablet-reader-mode toggle for the GUI."""
        self.logger.info("Hotkey: Super+Shift+P reader mode requested")
        self._queue_notification({'type': 'reader'})
        return True

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _require_ec(self):
        if not self.ec or not self.ec.access_available:
            raise RuntimeError((self.ec and self.ec.error_message) or "EC access not available")

    @staticmethod
    def _parse_level(value):
        if value is None:
            raise ValueError("Missing brightness level")
        if isinstance(value, bool):
            raise ValueError("Brightness level must be an integer")
        level = int(value)
        if not BRIGHTNESS_MIN <= level <= BRIGHTNESS_MAX:
            raise ValueError(f"Brightness level must be {BRIGHTNESS_MIN}-{BRIGHTNESS_MAX}")
        return level

    def handle_command(self, command_data):
        """Process a command and return response (watchdog held off meanwhile)"""
        with self._busy_lock:
            self._busy_commands += 1
        try:
            return self._handle_command(command_data)
        finally:
            with self._busy_lock:
                self._busy_commands -= 1
            self.watchdog.reset()  # the end of a long command counts as life too

    def _handle_command(self, command_data):
        try:
            if not isinstance(command_data, dict):
                raise ValueError("Command must be a JSON object")
            cmd = command_data.get('command')
            params = command_data.get('params') or {}
            if not isinstance(params, dict):
                raise ValueError("'params' must be a JSON object")

            self.logger.debug(f"Handling command: {cmd}")

            # Reset watchdog on any command
            self.watchdog.reset()

            response = {'success': False, 'error': None}

            if cmd == 'keepalive':
                # Simple keepalive/ping command
                response['success'] = True
                response['message'] = 'pong'
                # Attach any pending hotkey notifications
                notifs = self._drain_notifications()
                if notifs:
                    response['notifications'] = notifs

            elif cmd == 'enable-eink':
                self._eink_op(self.eink.enable_eink)
                self.eink_enabled = True
                response['success'] = True
                response['message'] = 'E-Ink display enabled'

            elif cmd == 'disable-eink':
                self._eink_op(self.eink.disable_eink)
                self.eink_enabled = False
                response['success'] = True
                response['message'] = 'E-Ink display disabled'

            elif cmd == 'refresh-eink':
                self._eink_op(self.eink.refresh_full)
                response['success'] = True
                response['message'] = 'E-Ink full refresh completed'

            elif cmd == 'set-dynamic':
                self._eink_op(self.eink.set_dynamic_mode)
                response['success'] = True
                response['message'] = 'E-Ink set to Dynamic Mode (fast refresh)'

            elif cmd == 'set-reading':
                self._eink_op(self.eink.set_reading_mode)
                response['success'] = True
                response['message'] = 'E-Ink set to Reading Mode (high-quality refresh)'

            elif cmd == 'reconnect-usb':
                # Sent by the GUI after resume: the T-CON may have re-enumerated
                with self._usb_lock:
                    reconnected = self._ensure_eink()
                response['success'] = True
                response['reconnected'] = reconnected
                response['message'] = ('E-Ink USB reconnected' if reconnected
                                       else 'E-Ink USB connection still valid')

            elif cmd == 'get-ec-status':
                # Return EC access status
                status = self.ec.get_access_status()
                response['success'] = True
                response['ec_status'] = status
                response['message'] = 'EC status retrieved'

            elif cmd == 'get-frontlight-state':
                # Read current frontlight state from EC
                self._require_ec()

                enabled = self.ec.get_frontlight_state()
                brightness = self.ec.read_brightness()

                response['success'] = True
                response['frontlight_enabled'] = enabled
                response['brightness_level'] = brightness
                response['message'] = 'Frontlight state retrieved'

            elif cmd == 'enable-frontlight':
                self._require_ec()

                # Optional brightness level parameter, validated before any EC write
                brightness_level = params.get('brightness_level')
                if brightness_level is not None:
                    brightness_level = self._parse_level(brightness_level)
                success, readback = self.ec.enable_frontlight(brightness_level=brightness_level)
                if success and brightness_level is not None:
                    self.brightness_level = brightness_level
                response['success'] = success
                response['readback'] = f"0x{readback:02x}"
                response['message'] = 'Frontlight enabled' if success else 'Frontlight enable failed (readback mismatch)'

            elif cmd == 'disable-frontlight':
                self._require_ec()

                success, readback = self.ec.disable_frontlight()
                response['success'] = success
                response['readback'] = f"0x{readback:02x}"
                response['message'] = 'Frontlight disabled' if success else 'Frontlight disable failed (readback mismatch)'

            elif cmd == 'set-brightness':
                self._require_ec()

                # Validate first: an out-of-range level must not switch the frontlight on
                level = self._parse_level(params.get('level'))
                if level == 0:
                    # Brightness 0 = fully disable frontlight (PWM 0 can still glow)
                    success, readback = self.ec.disable_frontlight()
                    if success:
                        self.brightness_level = 0
                    response['success'] = success
                    response['readback'] = f"0x{readback:02x}"
                    response['level'] = 0
                    response['message'] = 'Frontlight disabled (brightness 0)' if success else 'Frontlight disable failed'
                else:
                    # Non-zero: ensure frontlight is on, then set brightness
                    self.ec.enable_frontlight()
                    success, readback = self.ec.set_brightness(level)
                    if success:
                        self.brightness_level = level
                    response['success'] = success
                    response['readback'] = f"0x{readback:02x}"
                    response['level'] = level
                    response['message'] = f'Brightness set to {level}' if success else f'Brightness set failed (readback mismatch)'

            elif cmd == 'get-state':
                response['success'] = True
                response['eink_enabled'] = self.eink_enabled
                response['brightness_level'] = self.brightness_level
                response['tcon_available'] = self._tcon_available()
                response['ec_available'] = bool(self.ec and self.ec.access_available)
                response['message'] = 'State retrieved'

            elif cmd == 'shutdown':
                response['success'] = True
                response['message'] = 'Shutting down'
                # Shutdown after sending response
                threading.Timer(0.1, self.shutdown).start()

            else:
                raise ValueError(f"Unknown command: {cmd}")

            return response

        except Exception as e:
            self.logger.error(f"Command error: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    # ------------------------------------------------------------------
    # Socket server
    # ------------------------------------------------------------------

    @staticmethod
    def _recv_exact(sock, n):
        """Read exactly n bytes, or return None on EOF."""
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = sock.recv_into(view[got:], n - got)
            if not k:
                return None
            got += k
        return bytes(buf)

    @staticmethod
    def _peer_credentials(sock):
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        pid, uid, gid = struct.unpack('3i', creds)
        return pid, uid, gid

    def _accept_client(self, client_socket):
        """Admission control: peer credentials and client cap. Returns True to serve."""
        try:
            pid, uid, _gid = self._peer_credentials(client_socket)
        except OSError as e:
            self.logger.warning(f"Rejecting client: cannot read peer credentials ({e})")
            return False
        if self.allowed_uids is not None and uid not in self.allowed_uids:
            self.logger.warning(f"Rejecting client pid {pid}: uid {uid} is not allowed")
            return False
        with self._clients_lock:
            if self._client_count >= MAX_CLIENTS:
                self.logger.warning(f"Rejecting client pid {pid}: too many clients ({MAX_CLIENTS})")
                return False
            self._client_count += 1
        self.logger.info(f"Client connected (pid {pid}, uid {uid})")
        return True

    def handle_client(self, client_socket):
        """Handle a client connection"""
        client_socket.settimeout(CLIENT_IDLE_TIMEOUT)
        try:
            while self.running:
                # Receive data (with 4-byte length prefix)
                length_data = self._recv_exact(client_socket, 4)
                if not length_data:
                    break

                msg_length = struct.unpack('!I', length_data)[0]
                if msg_length == 0 or msg_length > MAX_MESSAGE_BYTES:
                    self.logger.warning(f"Dropping client: bad frame length {msg_length}")
                    break

                data = self._recv_exact(client_socket, msg_length)
                if data is None:
                    break

                # Parse JSON command
                try:
                    command_data = json.loads(data.decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    response = {'success': False, 'error': f'Malformed command: {e}'}
                else:
                    response = self.handle_command(command_data)

                # Send response
                response_json = json.dumps(response).encode('utf-8')
                response_length = struct.pack('!I', len(response_json))
                client_socket.sendall(response_length + response_json)

        except socket.timeout:
            self.logger.info(f"Client idle for {CLIENT_IDLE_TIMEOUT:.0f}s, disconnecting")
        except Exception as e:
            self.logger.error(f"Client handler error: {e}")
        finally:
            with self._clients_lock:
                self._client_count -= 1
            try:
                client_socket.close()
            except OSError:
                pass
            self.logger.info("Client disconnected")

    def run(self):
        """Main server loop"""
        try:
            if not self._acquire_instance_lock():
                self.logger.error("Another helper daemon is already running — exiting")
                return 1

            # Create PID file
            self._create_pid_file()

            # Initialize hardware
            if not self.initialize_hardware():
                self.logger.error("Failed to initialize hardware")
                return 1

            # Start global hotkey listener
            self.hotkey_listener.start()

            # Start HTTP API server for browser extensions
            try:
                handler = _make_http_handler(self)
                self.http_server = ThreadingHTTPServer(('127.0.0.1', HTTP_PORT), handler)
                self.http_server.daemon_threads = True
                http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
                http_thread.start()
                self.logger.info(f"HTTP API listening on http://127.0.0.1:{HTTP_PORT}")
            except Exception as e:
                self.logger.warning(f"Failed to start HTTP API server: {e}")

            # Create socket
            self._create_socket()

            self.running = True
            self.logger.info("Helper daemon started, waiting for connections")

            # Accept connections
            self.server_socket.settimeout(1.0)  # so we can check self.running periodically
            while self.running:
                try:
                    client_socket, _ = self.server_socket.accept()
                except socket.timeout:
                    continue
                except OSError as e:
                    if not self.running:
                        break
                    # EMFILE/ENFILE etc.: transient, keep serving instead of
                    # letting any local process shut the daemon down by
                    # exhausting file descriptors.
                    self.logger.error(f"Accept error: {e}")
                    time.sleep(0.5)
                    continue

                if not self._accept_client(client_socket):
                    try:
                        client_socket.close()
                    except OSError:
                        pass
                    continue

                client_thread = threading.Thread(
                    target=self.handle_client,
                    args=(client_socket,),
                    daemon=True,
                )
                client_thread.start()

            return 0

        except Exception as e:
            self.logger.error(f"Fatal error: {e}")
            return 1

        finally:
            self.shutdown()

    def shutdown(self):
        """Shutdown the daemon (idempotent, thread-safe)"""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        self.logger.info("Shutting down...")
        self.running = False

        # Cancel watchdog
        self.watchdog.cancel()

        # Stop hotkey listener
        self.hotkey_listener.stop()

        # Stop HTTP API server
        if self.http_server:
            try:
                self.http_server.shutdown()
                self.http_server.server_close()
            except Exception as e:
                self.logger.warning(f"HTTP server shutdown error: {e}")

        # Cleanup
        self.cleanup_hardware()
        self._remove_socket()
        self._remove_pid_file()
        self._release_instance_lock()

        self.logger.info("Shutdown complete")


def main():
    """Entry point"""
    # Check if running as root
    if os.geteuid() != 0:
        print("ERROR: This helper must be run as root (use pkexec or sudo)", file=sys.stderr)
        return 1

    # Files we create (log, pid, lock) are root-private
    os.umask(0o077)

    # Setup logging
    log_handlers = [logging.StreamHandler(sys.stderr)]
    try:
        log_handlers.append(logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3))
        # The log holds no secrets (hotkey names, EC register values) and is
        # the first thing asked for in bug reports, so let users read it.
        os.chmod(LOG_FILE, 0o644)
    except OSError as e:
        print(f"WARNING: cannot open {LOG_FILE}: {e}", file=sys.stderr)

    logging.basicConfig(
        level=LOG_LEVEL,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=log_handlers
    )
    logger = logging.getLogger('tinta4plusu-helper')

    # Setup exception hook to log uncaught exceptions
    def handle_exception(exc_type, exc_value, exc_traceback):
        """Log uncaught exceptions"""
        if issubclass(exc_type, KeyboardInterrupt):
            # Allow keyboard interrupt to exit normally
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_exception

    logger.info("ThinkBook E-Ink Helper starting")
    logger.info(f"Watchdog timeout: {WATCHDOG_TIMEOUT}s")

    daemon = HelperDaemon(logger)
    return daemon.run()


if __name__ == '__main__':
    sys.exit(main())
