"""Bounded UI-only recovery: never restart audio to repair a listener."""

import time


class UIRecovery:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.failures = 0
        self.attempts = 0
        self.next_attempt = 0.0
        self.blocked = False
        self.last_error = None

    def tick(self, owner, factory, *, audio_healthy):
        """Caller holds its UI lifecycle lock; stop() shares that ownership."""
        if owner._stopping:
            return
        server = owner.ws_server
        if server is not None and server.health_status()["healthy"]:
            self.failures = 0
            return
        self.failures += 1
        if not audio_healthy or self.blocked or self.failures < 2 or self.clock() < self.next_attempt:
            return
        if self.attempts >= 3:
            self.blocked = True
            self.last_error = "UI recovery limit reached; manual attention required"
            return
        self.attempts += 1
        self.next_attempt = self.clock() + min(60.0, 5.0 * 2 ** self.attempts)
        if server is not None and server.stop() is not True:
            self.blocked = True
            self.last_error = "UI worker did not stop; refusing to create duplicate control owners"
            return
        if owner._stopping:
            return
        replacement = factory()
        owner.ws_server = replacement
        try:
            replacement.start()
            self.failures = 0
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            if replacement.stop() is not True:
                self.blocked = True

    def status(self):
        return {"attempts": self.attempts, "blocked": self.blocked, "error": self.last_error}
