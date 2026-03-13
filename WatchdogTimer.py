"""
Copyright (c) 2025 Jon Cox (joncox123). All rights reserved.

WARNING: This software is provided "AS IS", without any warranty of any kind. It may contain bugs or other defects
that result in data loss, corruption, hardware damage or other issues. Use at your own risk.
It may temporarily or permanently render your hardware inoperable.
It may corrupt or damage the Embedded Controller or eInk T-CON controller in your laptop.
The author is not responsible for any damage, data loss or lost productivity caused by use of this software. 
By downloading and using this software you agree to these terms and acknowledge the risks involved.
"""

import threading
import time

class WatchdogTimer:
    """Watchdog timer that triggers shutdown if not reset.

    Suspend-aware: if the system was suspended (timer fires much later
    than expected), the watchdog gives a grace period for the GUI to
    reconnect instead of immediately shutting down.
    """

    SUSPEND_GRACE = 30.0  # extra seconds to wait after detected suspend

    def __init__(self, timeout, callback, logger):
        self.timeout = timeout
        self.callback = callback
        self.logger = logger
        self.timer = None
        self.lock = threading.Lock()
        self._last_reset = time.monotonic()
        self.reset()

    def reset(self):
        """Reset the watchdog timer"""
        with self.lock:
            self._last_reset = time.monotonic()
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.timeout, self._expired)
            self.timer.daemon = True
            self.timer.start()

    def _expired(self):
        """Called when timer expires"""
        elapsed = time.monotonic() - self._last_reset
        # If elapsed time is much larger than the timeout, the system
        # was likely suspended.  Give the GUI a grace period to reconnect.
        if elapsed > self.timeout * 2:
            self.logger.warning(
                f"Watchdog: likely resume from suspend (elapsed {elapsed:.1f}s "
                f"vs timeout {self.timeout}s), granting {self.SUSPEND_GRACE}s grace period"
            )
            with self.lock:
                self.timer = threading.Timer(self.SUSPEND_GRACE, self._grace_expired)
                self.timer.daemon = True
                self.timer.start()
            return
        self.logger.warning(f"Watchdog timer expired ({self.timeout}s), shutting down")
        self.callback()

    def _grace_expired(self):
        """Called when the post-suspend grace period expires without a reset."""
        self.logger.warning(f"Watchdog grace period expired, shutting down")
        self.callback()
    
    def cancel(self):
        """Cancel the watchdog timer"""
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None