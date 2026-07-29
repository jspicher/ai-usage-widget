"""Detect weekly quota resets.

This module contains pure logic with no GUI, network, or global state. It
compares two ``resets_at`` values rather than comparing against the current
time, so a system-clock jump cannot create a false event.

Some APIs return a relative duration instead of an absolute ``resets_at``.
widget.py must then calculate the timestamp as ``now() + secs``. Such a value
depends on the clock, so comparing two consecutive derived timestamps would
again amount to comparing against the clock. Those windows are marked with
``resets_at_derived``; boundary-movement detection is disabled for them, while
the clock-independent remaining-balance jump signal stays active.
"""

import hashlib
import time
import json
import os
import tempfile

WEEK_WINDOW_ID = "week"
DEFAULT_PCT_JUMP = 10.0
DEFAULT_RESETS_ADVANCE_SEC = 3600


def atomic_write_json(path, payload):
    """Write JSON so an interrupted write cannot leave a half-written file.

    The payload goes to a temporary file in the target's own directory (so the
    rename stays on one filesystem), is flushed and fsynced, and only then
    replaces the target. ``os.replace`` is atomic on Windows and POSIX alike.

    Raises on failure. Callers decide how to report it -- config writes drive
    the settings health banner, alert-state writes drive ``last_save_ok``.
    """
    folder = os.path.dirname(os.path.abspath(path)) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _week_reading(provider):
    """Return a comparable weekly-window reading, or None."""
    if not isinstance(provider, dict) or not provider.get("ok"):
        return None
    for w in provider.get("windows") or []:
        if not isinstance(w, dict) or w.get("id") != WEEK_WINDOW_ID:
            continue
        resets_at = w.get("resets_at")
        pct = w.get("remaining_pct")
        if resets_at is None or pct is None:
            return None
        extra = w.get("extra") or {}
        try:
            return {
                "resets_at": float(resets_at),
                "remaining_pct": float(pct),
                # Mark timestamps derived from the current time; see module docs.
                "resets_at_derived": bool(extra.get("resets_at_derived")),
            }
        except (TypeError, ValueError):
            return None
    return None


def readings(providers):
    """Return {provider_id: {...}} for providers with a usable weekly window."""
    out = {}
    for pid, p in (providers or {}).items():
        r = _week_reading(p)
        if r is not None:
            out[pid] = r
    return out


def event_id(provider_id, resets_at, to_pct):
    """Build a stable ID so the same event cannot be queued twice."""
    raw = "%s|%d|%.2f" % (provider_id, int(resets_at or 0), float(to_pct or 0.0))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def detect_resets(prev_readings, next_readings, cfg=None, while_away=False, now=None):
    """Emit an event if the window boundary moves OR the balance jumps."""
    cfg = cfg or {}
    try:
        pct_threshold = float(cfg.get("pct_jump_threshold", DEFAULT_PCT_JUMP))
    except (TypeError, ValueError):
        pct_threshold = DEFAULT_PCT_JUMP
    try:
        advance_threshold = float(
            cfg.get("resets_at_advance_sec", DEFAULT_RESETS_ADVANCE_SEC))
    except (TypeError, ValueError):
        advance_threshold = DEFAULT_RESETS_ADVANCE_SEC

    stamp = time.time() if now is None else now
    events = []
    for pid, new in (next_readings or {}).items():
        old = (prev_readings or {}).get(pid)
        if not old:
            continue  # The first observation only seeds the baseline.
        # If either timestamp was derived from now(), their difference also
        # reflects clock movement. Disable the boundary signal and rely only
        # on a jump in the remaining balance.
        derived = bool(new.get("resets_at_derived") or old.get("resets_at_derived"))
        boundary_moved = (not derived
                          and (new["resets_at"] - old["resets_at"]) > advance_threshold)
        balance_jumped = (new["remaining_pct"] - old["remaining_pct"]) >= pct_threshold
        if not (boundary_moved or balance_jumped):
            continue
        events.append({
            "id": event_id(pid, new["resets_at"], new["remaining_pct"]),
            "provider": pid,
            "from_pct": old["remaining_pct"],
            "to_pct": new["remaining_pct"],
            "resets_at": new["resets_at"],
            "detected_at": stamp,
            "while_away": bool(while_away),
        })
    return events


class AlertStore:
    """Store the reading baseline and undismissed alerts on disk.

    Writes are atomic (temporary file + replace), so a failure during a write
    cannot corrupt the file. A corrupt file is treated as missing.
    """

    def __init__(self, path, log_path=None):
        self.path = path
        # Keep the log beside the state file, in the data directory.
        self.log_path = log_path or os.path.join(
            os.path.dirname(os.path.abspath(path)) or ".", "widget-error.log")
        self.seen = {}
        self.pending = []
        # False after a failed save(). The app runs under pythonw without a
        # console, so this UI-visible flag and the log file are the only ways
        # to learn about a write failure.
        self.last_save_ok = True

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            seen = data.get("seen")
            pending = data.get("pending")
            self.seen = seen if isinstance(seen, dict) else {}
            self.pending = pending if isinstance(pending, list) else []
        except Exception:
            self.seen = {}
            self.pending = []
        return self

    def _log_failure(self, exc):
        """Append to the log beside the state file, ignoring log failures.

        Logging must not break save(), which is called from both the polling
        and GUI threads and must not propagate exceptions.
        """
        try:
            line = "%s save failed: %s: %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), type(exc).__name__, exc)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def save(self):
        """Return True on success and False on failure; never raise.

        A silent failure would otherwise be invisible: the in-memory baseline
        keeps working and the widget looks healthy, but persistence across
        restarts is lost. Therefore the result is returned, logged, and shown
        in settings.
        """
        try:
            atomic_write_json(
                self.path, {"seen": self.seen, "pending": self.pending})
        except Exception as e:
            self._log_failure(e)
            self.last_save_ok = False
            return False
        self.last_save_ok = True
        return True

    def merge_seen(self, new_readings):
        """Update only keys present in ``new_readings``.

        A provider with an error is absent from ``new_readings``, so its
        baseline must be preserved. Otherwise recovery after a failure would
        look like a quota reset.
        """
        for pid, reading in (new_readings or {}).items():
            self.seen[pid] = reading

    def add(self, events):
        known = {e.get("id") for e in self.pending}
        fresh = [e for e in (events or []) if e.get("id") not in known]
        self.pending.extend(fresh)
        return fresh

    def dismiss(self, alert_id):
        before = len(self.pending)
        self.pending = [e for e in self.pending if e.get("id") != alert_id]
        return len(self.pending) != before

    def dismiss_all(self):
        count = len(self.pending)
        self.pending = []
        return count
