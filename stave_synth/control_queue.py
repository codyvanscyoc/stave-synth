"""One bounded off-MIDI control worker; state/FS work never runs in note dispatch."""

from collections import OrderedDict, deque
import logging
import threading

logger = logging.getLogger(__name__)


class ControlQueue:
    def __init__(self, handler, *, ordered_limit=64):
        self.handler = handler
        self.ordered_limit = ordered_limit
        self._ordered = deque()
        self._latest = OrderedDict()
        self._condition = threading.Condition()
        self._closed = False
        self._thread = None
        self.dropped = 0
        self._generation = 0

    def start(self):
        with self._condition:
            if self._closed:
                raise RuntimeError("stopped control worker cannot restart")
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="stave-controls", daemon=True)
                self._thread.start()

    def submit(self, event, *, key=None):
        with self._condition:
            if self._closed:
                return False
            item = (self._generation, event)
            if key is None:
                if len(self._ordered) >= self.ordered_limit:
                    self.dropped += 1
                    return False
                self._ordered.append(item)
            else:
                if key not in self._latest and len(self._latest) >= 128:
                    self.dropped += 1
                    return False
                self._latest[key] = item
            self._condition.notify()
            return True

    def clear(self):
        with self._condition:
            self._generation += 1
            self._ordered.clear()
            self._latest.clear()

    def is_current(self, generation):
        with self._condition:
            return not self._closed and generation == self._generation

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._ordered or self._latest)
                if self._closed:
                    return
                if self._ordered:
                    generation, event = self._ordered.popleft()
                else:
                    _, (generation, event) = self._latest.popitem(last=False)
            try:
                # The receiver checks generation AGAIN under its control lock;
                # panic can invalidate an item already removed from this queue.
                self.handler(event, generation)
            except Exception:
                logger.exception("Queued control failed")

    def stop(self, timeout=2.0):
        with self._condition:
            self._closed = True
            self._generation += 1
            self._ordered.clear()
            self._latest.clear()
            self._condition.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        return self._thread is None or not self._thread.is_alive()

    def status(self):
        with self._condition:
            return {"pending": len(self._ordered) + len(self._latest), "dropped": self.dropped,
                    "alive": bool(self._thread and self._thread.is_alive()), "closed": self._closed}
