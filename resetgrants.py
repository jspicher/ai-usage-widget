"""Sanitized, read-only observations of banked quota resets.

No credentials, redemption identifiers, profile data, or provider prose leave
the collectors. Hashed identities are stable across polls and restarts.
"""

import hashlib
import math
from datetime import datetime, timezone


def epoch(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            stamp = stamp.timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def count(value):
    # Counts are JSON integers, never booleans or inferred from detail length.
    return value if type(value) is int and value >= 0 else None


def unavailable():
    return {"status": "unavailable", "count": None, "grants": [],
            "observed_at": None, "details_complete": False}


def _identity(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _grant(raw, coverage, left, status, now, *, claude=False):
    expires = epoch(raw.get("ends_at" if claude else "expires_at"))
    starts = epoch(raw.get("starts_at" if claude else "granted_at"))
    if expires is not None and expires <= now:
        status = "expired"
    elif left == 0 and status not in ("expired", "unavailable"):
        status = "consumed"
    elif status == "available" and starts is not None and starts > now:
        status = "scheduled"
    return {"id": _identity(raw.get("id")), "coverage": coverage,
            "remaining_count": left, "status": status,
            "starts_at": starts, "expires_at": expires}


def claude(data, now):
    block = data.get("cedar_ember") if isinstance(data, dict) else None
    if not isinstance(block, dict) or block.get("eligible") is not True:
        return unavailable()
    rows = block.get("grants")
    if not isinstance(rows, list):
        return unavailable()
    grants, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict) or count(raw.get("resets_left")) is None:
            return unavailable()
        clears = raw.get("clears")
        scopes = set(s for s in clears if isinstance(s, str)) if isinstance(clears, list) else set()
        weekly = "seven_day" in scopes
        fable = "seven_day_overage_included" in scopes or any("fable" in s.lower() for s in scopes)
        if weekly:
            coverage = "weekly_fable" if fable else "weekly"
        elif fable:
            coverage = "fable"
        elif scopes == {"five_hour"}:
            coverage = "session"
        else:
            coverage = "unknown"
        status = "available"
        if (raw.get("paused") is True or raw.get("usable_now") is False
                or raw.get("unavailable_reason")):
            status = "unavailable"
        grant = _grant(raw, coverage, raw["resets_left"], status, now, claude=True)
        # Deduplicate an API row without making missing identities collide.
        if grant["id"] is not None:
            if grant["id"] in seen:
                continue
            seen.add(grant["id"])
        grants.append(grant)
    return {"status": "ok", "count": sum(g["remaining_count"] for g in grants
            if g["status"] == "available"), "grants": grants,
            "observed_at": now, "details_complete": all(g["id"] for g in grants)}


def codex(data, now, summary=None):
    # A separate usage response can retain the authoritative count when the
    # detail lookup fails. It never supplies grants for expiry reminders.
    source = data if isinstance(data, dict) and count(data.get("available_count")) is not None else summary
    if not isinstance(source, dict) or count(source.get("available_count")) is None:
        return unavailable()
    total = source["available_count"]
    rows = data.get("credits") if isinstance(data, dict) else None
    grants, seen = [], set()
    valid_rows = isinstance(rows, list)
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            valid_rows = False
            continue
        status = raw.get("status")
        status = status if status in ("available", "expired", "redeemed", "consumed") else "unavailable"
        left = 1 if status == "available" else 0
        if raw.get("is_supported_by_plan") is False:
            status = "unavailable"
        coverage = "full" if raw.get("reset_type") == "codex_rate_limits" else "unknown"
        grant = _grant(raw, coverage, left, status, now)
        if grant["id"] is not None:
            if grant["id"] in seen:
                continue
            seen.add(grant["id"])
        else:
            valid_rows = False
        grants.append(grant)
    return {"status": "ok", "count": total, "grants": grants, "observed_at": now,
            # Detail lists may be capped. Absence is evidence only when the
            # complete available inventory was returned.
            "details_complete": valid_rows and sum(g["status"] == "available" for g in grants) == total}
