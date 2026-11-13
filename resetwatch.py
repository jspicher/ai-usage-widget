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

import resetgrants

WEEK_WINDOW_ID = "week"
DEFAULT_PCT_JUMP = 10.0
DEFAULT_RESETS_ADVANCE_SEC = 3600
GRANT_MAX_AGE_SEC = 120


def grant_key(provider, grant):
    """Delivery identity includes expiry, so an extended grant is a new offer."""
    return "%s|%s|%.6f" % (provider, grant["id"], grant["expires_at"])


def expiry_group_id(provider, coverage, expires_at):
    raw = "%s|%s|%.6f" % (provider, coverage, expires_at)
    return "expiry-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


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


def _reading(resets_at, pct, derived):
    """Return a comparable reading built from raw values, or None."""
    if resets_at is None or pct is None:
        return None
    try:
        return {
            "resets_at": float(resets_at),
            "remaining_pct": float(pct),
            # Mark timestamps derived from the current time; see module docs.
            "resets_at_derived": bool(derived),
        }
    except (TypeError, ValueError):
        return None


def _week_reading(provider):
    """Return a comparable weekly-window reading, or None."""
    if not isinstance(provider, dict) or not provider.get("ok"):
        return None
    for w in provider.get("windows") or []:
        if not isinstance(w, dict) or w.get("id") != WEEK_WINDOW_ID:
            continue
        extra = w.get("extra") or {}
        return _reading(w.get("resets_at"), w.get("remaining_pct"),
                        extra.get("resets_at_derived"))
    return None


def stored_readings(seen):
    """Return the persisted baseline entries that still have a usable shape.

    A state file written by an older or forked build -- or hand-edited -- can
    hold an entry missing a key. detect_resets indexes those keys directly, so
    keeping such an entry would raise on every poll from then on and silently
    kill weekly reset alerts for good. Dropping it costs one reseed instead.
    """
    out = {}
    for pid, r in (seen or {}).items():
        if not isinstance(r, dict):
            continue
        reading = _reading(r.get("resets_at"), r.get("remaining_pct"),
                           r.get("resets_at_derived"))
        if reading is not None:
            out[pid] = reading
    return out


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
            "kind": "weekly_reset",
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
        self.delivered = {}
        # False after a failed save(). The app runs under pythonw without a
        # console, so this UI-visible flag and the log file are the only ways
        # to learn about a write failure.
        self.last_save_ok = True
        # Serialized form of the last successful write. save() runs on every
        # poll whether or not anything moved, and each call is an fsync -- a
        # real disk barrier. On an idle machine the payload is byte-identical
        # every time, so compare before spending one.
        self._written = None

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            seen = data.get("seen")
            pending = data.get("pending")
            if not isinstance(pending, list):
                pending = []
            # Drop entries the rest of the module could not use; see
            # stored_readings.
            self.seen = stored_readings(seen if isinstance(seen, dict) else {})
            self.pending = []
            for entry in pending:
                if not isinstance(entry, dict):
                    continue
                row = dict(entry, kind=entry.get("kind", "weekly_reset"))
                if row["kind"] == "reset_expiry":
                    if (not isinstance(row.get("id"), str)
                            or not isinstance(row.get("provider"), str)
                            or resetgrants.epoch(row.get("expires_at")) is None
                            or row.get("threshold_hours") not in (10, 1)
                            or not isinstance(row.get("grants"), list)):
                        continue
                    row["grants"] = [m for m in row["grants"] if isinstance(m, dict)
                                     and isinstance(m.get("key"), str)
                                     and resetgrants.count(m.get("remaining_count")) is not None]
                    if not row["grants"]:
                        continue
                self.pending.append(row)
            delivered = data.get("delivered")
            self.delivered = {key: [h for h in hours if type(h) is int and h in (10, 1)]
                              for key, hours in (delivered.items() if isinstance(delivered, dict) else [])
                              if isinstance(key, str) and isinstance(hours, list)}
        except Exception:
            self.seen = {}
            self.pending = []
            self.delivered = {}
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
        payload = {"seen": self.seen, "pending": self.pending,
                   "delivered": self.delivered}
        # sort_keys so an unchanged payload always compares equal.
        fingerprint = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        # Skip the write only if the file that write produced is still there.
        # Something outside the process (a cleanup, a sync tool, antivirus) can
        # remove it, and without this check an idle machine would never notice.
        if (self.last_save_ok and fingerprint == self._written
                and os.path.exists(self.path)):
            return True
        try:
            atomic_write_json(self.path, payload)
        except Exception as e:
            self._log_failure(e)
            self.last_save_ok = False
            self._written = None
            return False
        self.last_save_ok = True
        self._written = fingerprint
        return True

    def merge_seen(self, new_readings):
        """Update only keys present in ``new_readings``.

        A provider with an error is absent from ``new_readings``, so its
        baseline must be preserved. Otherwise recovery after a failure would
        look like a quota reset.
        """
        for pid, reading in (new_readings or {}).items():
            self.seen[pid] = reading

    def process_expiry(self, providers, now=None):
        """Update persistent rows and deliver each grant threshold once.

        The caller holds the app's alert lock across this method and save().
        Only fresh explicit grant observations can deliver; a capped list's
        missing rows never imply consumption. Expiry itself needs no network.
        """
        stamp = time.time() if now is None else now
        fresh, groups = {}, {}
        for pid, provider in (providers or {}).items():
            block = (provider.get("meta") or {}).get("banked_resets") or {}
            observed = resetgrants.epoch(block.get("observed_at"))
            if (block.get("status") != "ok" or observed is None
                    or not -5 <= stamp - observed <= GRANT_MAX_AGE_SEC):
                continue
            by_key = {}
            fresh[pid] = (block, by_key)
            for grant in block.get("grants") or []:
                expires = resetgrants.epoch(grant.get("expires_at"))
                if not grant.get("id") or expires is None:
                    continue
                key = grant_key(pid, grant)
                by_key[key] = grant
                if (block.get("count", 0) == 0 or grant.get("status") != "available"
                        or not grant.get("remaining_count") or expires <= stamp):
                    continue
                starts = resetgrants.epoch(grant.get("starts_at"))
                if starts is not None and starts > stamp:
                    continue
                gid = expiry_group_id(pid, grant["coverage"], expires)
                groups.setdefault(gid, []).append((key, grant, pid))

        # Refresh already-visible reminders, including those loaded at startup.
        for row in self.pending:
            if row.get("kind") != "reset_expiry":
                continue
            expired = (resetgrants.epoch(row.get("expires_at")) or float("inf")) <= stamp
            reading = fresh.get(row.get("provider"))
            for member in row.get("grants") or []:
                if expired:
                    member.update(status="expired", remaining_count=0)
                elif reading:
                    block, by_key = reading
                    current = by_key.get(member["key"])
                    if block.get("count") == 0:
                        member.update(status="unavailable", remaining_count=0)
                    elif current is not None:
                        member.update(status=current["status"],
                                      remaining_count=current["remaining_count"])
                    elif block.get("details_complete"):
                        member.update(status="unavailable", remaining_count=0)
                    else:
                        member["status"] = "unverified"
            self._update_expiry_row(row, expired)

        notices = []
        pending = {row.get("id"): row for row in self.pending}
        for gid, members in groups.items():
            grant = members[0][1]
            remaining = grant["expires_at"] - stamp
            threshold = 1 if remaining <= 3600 else 10 if remaining <= 36000 else None
            if threshold is None:
                continue
            due = [key for key, _, _ in members if threshold not in self.delivered.get(key, [])]
            if not due:
                continue
            for key in due:
                hours = self.delivered.setdefault(key, [])
                # Late startup skips the ten-hour stage permanently, including
                # if the clock later moves backwards.
                for hour in ((10, 1) if threshold == 1 else (10,)):
                    if hour not in hours:
                        hours.append(hour)
            row = pending.get(gid)
            if row is None:
                row = {"id": gid, "kind": "reset_expiry", "provider": members[0][2],
                       "coverage": grant["coverage"], "expires_at": grant["expires_at"],
                       "detected_at": stamp, "grants": []}
                self.pending.append(row)
                pending[gid] = row
            row.update(threshold_hours=threshold, updated_at=stamp)
            known = {m["key"]: m for m in row["grants"]}
            for key, current, _ in members:
                known[key] = {"key": key, "remaining_count": current["remaining_count"],
                              "status": "available"}
            row["grants"] = list(known.values())
            self._update_expiry_row(row)
            notices.append(dict(row))
        return notices

    @staticmethod
    def _update_expiry_row(row, expired=False):
        members = row.get("grants") or []
        row["remaining_count"] = sum(m.get("remaining_count", 0) for m in members
                                     if m.get("status") == "available")
        row["status"] = ("expired" if expired else "available" if row["remaining_count"]
                         else "unverified" if any(m.get("status") == "unverified" for m in members)
                         else "unavailable")

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
