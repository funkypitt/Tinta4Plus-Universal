"""
Copyright (c) 2025 Jon Cox (joncox123). All rights reserved.

WARNING: This software is provided "AS IS", without any warranty of any kind. It may contain bugs or other defects
that result in data loss, corruption, hardware damage or other issues. Use at your own risk.
It may temporarily or permanently render your hardware inoperable.
It may corrupt or damage the Embedded Controller or eInk T-CON controller in your laptop.
The author is not responsible for any damage, data loss or lost productivity caused by use of this software.
By downloading and using this software you agree to these terms and acknowledge the risks involved.
"""
import socket
import struct
import json
import threading

# Largest frame we are willing to read back from the daemon
MAX_MESSAGE_BYTES = 64 * 1024


class HelperClient:
    """Client for communicating with privileged helper daemon via Unix socket"""

    def __init__(self, logger):
        self.logger = logger
        self.socket = None
        self.connected = False
        self.lock = threading.Lock()

    def connect(self, socket_path, timeout=10.0, quiet=False):
        """Connect to helper daemon socket.

        The socket lives at a well-known path, so before trusting it we
        check (SO_PEERCRED) that whoever listens there really is root —
        a fake daemon planted by another local user is refused.

        quiet=True logs a failed attempt at debug level (used while polling
        for a daemon that is still starting up).
        """
        self.close()
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(socket_path)

            peer_uid = self._peer_uid(sock)
            if peer_uid is not None and peer_uid != 0:
                raise PermissionError(
                    f"socket at {socket_path} is owned by uid {peer_uid}, not root — refusing")

            self.socket = sock
            self.connected = True
            self.logger.info(f"Connected to helper daemon at {socket_path}")
            return True
        except Exception as e:
            (self.logger.debug if quiet else self.logger.error)(f"Failed to connect to helper: {e}")
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.connected = False
            return False

    @staticmethod
    def _peer_uid(sock):
        """Return the uid of the process on the other end, or None if unknown."""
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize('3i'))
            _pid, uid, _gid = struct.unpack('3i', creds)
            return uid
        except (OSError, AttributeError, struct.error):
            return None

    def disconnect(self, shutdown_helper=False):
        """Disconnect from helper daemon.

        Args:
            shutdown_helper: also ask the daemon to exit. Only the process
                that launched the daemon should do this — other clients
                (toggle-eink, a GUI that attached to an existing daemon)
                must not take the daemon away from everyone else. Without
                it, the daemon's watchdog stops it once nobody sends
                keepalives anymore.
        """
        if self.socket:
            if shutdown_helper:
                try:
                    self.send_command('shutdown')
                except Exception:
                    pass
            self.close()
            self.logger.info("Disconnected from helper daemon")

    def close(self):
        """Close the socket without talking to the daemon."""
        sock, self.socket = self.socket, None
        self.connected = False
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def send_command(self, command, **params):
        """
        Send command to helper and receive response
        Returns: response dict (raises on transport errors)
        """
        if not self.connected or not self.socket:
            raise RuntimeError("Not connected to helper daemon")

        with self.lock:
            try:
                # Prepare command
                command_data = {
                    'command': command,
                    'params': params
                }

                # Serialize to JSON
                message = json.dumps(command_data).encode('utf-8')

                # Send with length prefix
                length_prefix = struct.pack('!I', len(message))
                self.socket.sendall(length_prefix + message)

                # Receive response length
                length_data = self._recv_exact(4)
                if not length_data:
                    raise RuntimeError("Connection closed by helper")

                response_length = struct.unpack('!I', length_data)[0]
                if response_length > MAX_MESSAGE_BYTES:
                    raise RuntimeError(f"Oversized response from helper ({response_length} bytes)")

                # Receive response data
                response_data = self._recv_exact(response_length)
                if not response_data:
                    raise RuntimeError("Connection closed by helper")

                # Parse JSON response
                response = json.loads(response_data.decode('utf-8'))

                return response

            except Exception as e:
                self.logger.error(f"Command error: {e}")
                self.connected = False
                raise

    def _recv_exact(self, num_bytes):
        """Receive exactly num_bytes from socket"""
        buf = bytearray(num_bytes)
        view = memoryview(buf)
        got = 0
        while got < num_bytes:
            n = self.socket.recv_into(view[got:], num_bytes - got)
            if not n:
                return None
            got += n
        return bytes(buf)

    def is_connected(self):
        """Check if connected to helper"""
        return self.connected
