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
"""

import os
import sys
import json
import socket
import struct
import signal
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

from WatchdogTimer import WatchdogTimer
from ECController import ECController
from EInkUSBController import EInkUSBController
from GlobalHotkeyListener import GlobalHotkeyListener

# Configuration
SOCKET_PATH = '/tmp/tinta4plusu.sock'
PID_FILE = '/tmp/tinta4plusu.pid'
WATCHDOG_TIMEOUT = 60.0  # seconds
HTTP_PORT = 19849  # localhost HTTP API for browser extensions (e.g. PageTurn)
LOG_LEVEL = logging.DEBUG  # Changed to DEBUG for detailed EC port access logging


def _make_http_handler(daemon):
    """Create an HTTP request handler bound to the given daemon instance."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == '/refresh-eink':
                try:
                    if daemon.eink and daemon.eink_enabled:
                        daemon.eink.refresh_full()
                        daemon.logger.info("HTTP API: eInk refresh")
                        self._respond(200, {'success': True, 'message': 'E-Ink refreshed'})
                    else:
                        self._respond(503, {'success': False, 'error': 'eInk not enabled'})
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
        
        # Hardware controllers
        self.eink = None
        self.ec = None

        # eInk state tracking (for global hotkeys)
        self.eink_enabled = False
        self.brightness_level = 4  # default
        self._pending_notifications = []
        self._notify_lock = threading.Lock()

        # HTTP API server (for browser extensions like PageTurn)
        self.http_server = None

        # Global hotkey listener
        self.hotkey_listener = GlobalHotkeyListener(
            self.logger,
            on_brightness_up=self._hotkey_brightness_up,
            on_brightness_down=self._hotkey_brightness_down,
            on_refresh=self._hotkey_refresh,
        )

        # Watchdog
        self.watchdog = WatchdogTimer(WATCHDOG_TIMEOUT, self.shutdown, self.logger)
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        self.logger.info(f"Received signal {signum}, shutting down")
        self.shutdown()
    
    def _create_pid_file(self):
        """Create PID file"""
        try:
            with open(self.pid_file, 'w') as f:
                f.write(str(os.getpid()))
            self.logger.info(f"Created PID file: {self.pid_file}")
        except Exception as e:
            self.logger.error(f"Failed to create PID file: {e}")
    
    def _remove_pid_file(self):
        """Remove PID file"""
        try:
            if os.path.exists(self.pid_file):
                os.remove(self.pid_file)
                self.logger.info("Removed PID file")
        except Exception as e:
            self.logger.warning(f"Failed to remove PID file: {e}")
    
    def _create_socket(self):
        """Create Unix domain socket"""
        # Remove old socket if exists
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(self.socket_path)
        self.server_socket.listen(1)
        
        # Set permissions (readable/writable by all for simplicity)
        os.chmod(self.socket_path, 0o666)
        
        self.logger.info(f"Listening on socket: {self.socket_path}")
    
    def _remove_socket(self):
        """Remove socket file"""
        try:
            if self.server_socket:
                self.server_socket.close()
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
            self.logger.info("Removed socket")
        except Exception as e:
            self.logger.warning(f"Failed to remove socket: {e}")
    
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

            # Initialize E-Ink USB controller
            self.logger.info("Initializing E-Ink USB controller")
            self.eink = EInkUSBController(self.logger)
            self.eink.connect()

            self.logger.info("Hardware initialization complete")
            return True

        except Exception as e:
            self.logger.error(f"Hardware initialization failed: {e}")
            return False
    
    def cleanup_hardware(self):
        """Cleanup hardware connections"""
        if self.eink:
            self.eink.disconnect()
        self.logger.info("Hardware cleanup complete")
    
    # ------------------------------------------------------------------
    # Global hotkey callbacks (called from evdev listener thread)
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

    def _hotkey_brightness_up(self):
        """Handle global brightness-up key."""
        if not self.eink_enabled:
            return
        if self.brightness_level < 8:
            new_level = self.brightness_level + 1
            try:
                if self.ec and self.ec.access_available:
                    self.ec.set_brightness(new_level)
                    self.brightness_level = new_level
                    self.logger.info(f"Hotkey: brightness up → {new_level}")
                    self._queue_notification({'type': 'brightness', 'level': new_level})
            except Exception as e:
                self.logger.error(f"Hotkey brightness up error: {e}")

    def _hotkey_brightness_down(self):
        """Handle global brightness-down key."""
        if not self.eink_enabled:
            return
        if self.brightness_level > 0:
            new_level = self.brightness_level - 1
            try:
                if self.ec and self.ec.access_available:
                    self.ec.set_brightness(new_level)
                    self.brightness_level = new_level
                    self.logger.info(f"Hotkey: brightness down → {new_level}")
                    self._queue_notification({'type': 'brightness', 'level': new_level})
            except Exception as e:
                self.logger.error(f"Hotkey brightness down error: {e}")

    def _hotkey_refresh(self):
        """Handle global refresh key (F5/F9)."""
        if not self.eink_enabled:
            return
        try:
            if self.eink:
                self.eink.refresh_full()
                self.logger.info("Hotkey: eInk refresh")
                self._queue_notification({'type': 'refresh'})
        except Exception as e:
            self.logger.error(f"Hotkey refresh error: {e}")

    def handle_command(self, command_data):
        """Process a command and return response"""
        try:
            cmd = command_data.get('command')
            params = command_data.get('params', {})
            
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
                self.eink.enable_eink()
                self.eink_enabled = True
                response['success'] = True
                response['message'] = 'E-Ink display enabled'

            elif cmd == 'disable-eink':
                self.eink.disable_eink()
                self.eink_enabled = False
                response['success'] = True
                response['message'] = 'E-Ink display disabled'
            
            elif cmd == 'refresh-eink':
                self.eink.refresh_full()
                response['success'] = True
                response['message'] = 'E-Ink full refresh completed'

            elif cmd == 'set-dynamic':
                self.eink.set_dynamic_mode()
                response['success'] = True
                response['message'] = 'E-Ink set to Dynamic Mode (fast refresh)'

            elif cmd == 'set-reading':
                self.eink.set_reading_mode()
                response['success'] = True
                response['message'] = 'E-Ink set to Reading Mode (high-quality refresh)'

            elif cmd == 'get-ec-status':
                # Return EC access status
                status = self.ec.get_access_status()
                response['success'] = True
                response['ec_status'] = status
                response['message'] = 'EC status retrieved'

            elif cmd == 'get-frontlight-state':
                # Read current frontlight state from EC
                if not self.ec.access_available:
                    raise RuntimeError(self.ec.error_message or "EC access not available")

                enabled = self.ec.get_frontlight_state()
                brightness = self.ec.read_brightness()

                response['success'] = True
                response['frontlight_enabled'] = enabled
                response['brightness_level'] = brightness
                response['message'] = 'Frontlight state retrieved'

            elif cmd == 'enable-frontlight':
                # Check EC access first
                if not self.ec.access_available:
                    raise RuntimeError(self.ec.error_message or "EC access not available")

                # Get optional brightness level parameter
                brightness_level = params.get('brightness_level')
                success, readback = self.ec.enable_frontlight(brightness_level=brightness_level)
                response['success'] = success
                response['readback'] = f"0x{readback:02x}"
                response['message'] = 'Frontlight enabled' if success else 'Frontlight enable failed (readback mismatch)'

            elif cmd == 'disable-frontlight':
                # Check EC access first
                if not self.ec.access_available:
                    raise RuntimeError(self.ec.error_message or "EC access not available")

                success, readback = self.ec.disable_frontlight()
                response['success'] = success
                response['readback'] = f"0x{readback:02x}"
                response['message'] = 'Frontlight disabled' if success else 'Frontlight disable failed (readback mismatch)'

            elif cmd == 'set-brightness':
                # Check EC access first
                if not self.ec.access_available:
                    raise RuntimeError(self.ec.error_message or "EC access not available")

                level = params.get('level')
                if level is None:
                    raise ValueError("Missing 'level' parameter")

                success, readback = self.ec.set_brightness(int(level))
                if success:
                    self.brightness_level = int(level)
                response['success'] = success
                response['readback'] = f"0x{readback:02x}"
                response['level'] = level
                response['message'] = f'Brightness set to {level}' if success else f'Brightness set failed (readback mismatch)'
            
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
    
    def handle_client(self, client_socket):
        """Handle a client connection"""
        try:
            while self.running:
                # Receive data (with 4-byte length prefix)
                length_data = client_socket.recv(4)
                if not length_data:
                    break
                
                msg_length = struct.unpack('!I', length_data)[0]
                
                # Receive the message
                data = b''
                while len(data) < msg_length:
                    chunk = client_socket.recv(msg_length - len(data))
                    if not chunk:
                        break
                    data += chunk
                
                if len(data) != msg_length:
                    break
                
                # Parse JSON command
                command_data = json.loads(data.decode('utf-8'))
                
                # Process command
                response = self.handle_command(command_data)
                
                # Send response
                response_json = json.dumps(response).encode('utf-8')
                response_length = struct.pack('!I', len(response_json))
                client_socket.sendall(response_length + response_json)
                
        except Exception as e:
            self.logger.error(f"Client handler error: {e}")
        finally:
            client_socket.close()
    
    def run(self):
        """Main server loop"""
        try:
            # Check if already running
            if os.path.exists(self.pid_file):
                self.logger.warning(f"PID file exists: {self.pid_file}")
                try:
                    with open(self.pid_file, 'r') as f:
                        old_pid = int(f.read().strip())
                    # Check if process is still running
                    os.kill(old_pid, 0)
                    self.logger.error(f"Helper already running (PID {old_pid})")
                    return 1
                except (OSError, ValueError):
                    self.logger.info("Stale PID file, removing")
                    os.remove(self.pid_file)
            
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
                self.http_server = HTTPServer(('127.0.0.1', HTTP_PORT), handler)
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
            while self.running:
                try:
                    # Set timeout so we can check self.running periodically
                    self.server_socket.settimeout(1.0)
                    try:
                        client_socket, _ = self.server_socket.accept()
                        self.logger.info("Client connected")
                        
                        # Handle in a thread (though we expect only one client)
                        client_thread = threading.Thread(
                            target=self.handle_client,
                            args=(client_socket,)
                        )
                        client_thread.daemon = True
                        client_thread.start()
                        
                    except socket.timeout:
                        continue
                        
                except Exception as e:
                    if self.running:
                        self.logger.error(f"Accept error: {e}")
                        break
            
            return 0
            
        except Exception as e:
            self.logger.error(f"Fatal error: {e}")
            return 1
        
        finally:
            self.shutdown()
    
    def shutdown(self):
        """Shutdown the daemon"""
        if not self.running:
            return
        
        self.logger.info("Shutting down...")
        self.running = False
        
        # Cancel watchdog
        self.watchdog.cancel()

        # Stop hotkey listener
        self.hotkey_listener.stop()

        # Stop HTTP API server
        if self.http_server:
            self.http_server.shutdown()

        # Cleanup
        self.cleanup_hardware()
        self._remove_socket()
        self._remove_pid_file()
        
        self.logger.info("Shutdown complete")


def main():
    """Entry point"""
    # Check if running as root
    if os.geteuid() != 0:
        print("ERROR: This helper must be run as root (use pkexec or sudo)", file=sys.stderr)
        return 1

    # Setup logging
    log_handlers = [
        logging.StreamHandler(sys.stderr),  # Console output
        logging.FileHandler('/tmp/TintaHelper.log', mode='w')  # File output (overwrite mode)
    ]

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
