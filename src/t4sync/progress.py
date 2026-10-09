"""Single-line progress bar for slow, countable work (one request per item).

Drawn on stderr, and only when stderr is a terminal: piped output and CI logs
get nothing, so they stay identical to what they were without it. ASCII only,
because a legacy Windows console may not be able to encode block characters.
"""
import shutil
import sys
import time


class Progress:
    REDRAW_EVERY = 0.1      # seconds; a redraw per item would flicker

    def __init__(self, label, total=None, stream=None, enabled=None):
        self.label = label
        self.total = total
        self.count = 0
        self.stream = stream or sys.stderr
        if enabled is None:
            isatty = getattr(self.stream, "isatty", None)
            enabled = bool(isatty and isatty())
        self.enabled = enabled
        self.started = time.monotonic()
        self._drawn_at = 0.0
        self._width = 0
        self._draw(force=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def update(self, step=1):
        self.count += step
        self._draw(force=self.total is not None and self.count >= self.total)

    def render(self, columns):
        """The line to draw, fitted to `columns`. Separate so it can be tested."""
        elapsed = "%ds" % (time.monotonic() - self.started)
        if not self.total:
            return ("%s ... %s" % (self.label, elapsed))[:columns - 1]
        counter = "%d/%d" % (min(self.count, self.total), self.total)
        tail = "  %s  %s" % (counter, elapsed)
        room = columns - 1 - len(self.label) - len(tail) - 3
        if room < 10:
            return ("%s%s" % (self.label, tail))[:columns - 1]
        filled = int(room * min(self.count, self.total) / self.total)
        return "%s [%s%s]%s" % (self.label, "#" * filled, "-" * (room - filled), tail)

    def _draw(self, force=False):
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._drawn_at < self.REDRAW_EVERY:
            return
        self._drawn_at = now
        line = self.render(shutil.get_terminal_size((80, 24)).columns)
        # Pad over whatever a longer previous line left behind.
        self.stream.write("\r" + line.ljust(self._width))
        self.stream.flush()
        self._width = len(line)

    def close(self):
        """Erase the bar so the next printed line starts clean."""
        if self.enabled and self._width:
            self.stream.write("\r" + " " * self._width + "\r")
            self.stream.flush()
            self._width = 0
