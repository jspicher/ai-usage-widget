"""Обнаружение сброса недельной квоты.

Чистая логика: без GUI, без сети, без глобального состояния.
Сравнение идёт между двумя значениями resets_at, а не с текущим временем,
поэтому скачок системных часов не может создать ложное событие.
"""

import hashlib
import time

WEEK_WINDOW_ID = "week"
DEFAULT_PCT_JUMP = 10.0
DEFAULT_RESETS_ADVANCE_SEC = 3600


def _week_reading(provider):
    """Сопоставимое показание недельного окна, либо None."""
    if not isinstance(provider, dict) or not provider.get("ok"):
        return None
    for w in provider.get("windows") or []:
        if not isinstance(w, dict) or w.get("id") != WEEK_WINDOW_ID:
            continue
        resets_at = w.get("resets_at")
        pct = w.get("remaining_pct")
        if resets_at is None or pct is None:
            return None
        try:
            return {"resets_at": float(resets_at), "remaining_pct": float(pct)}
        except (TypeError, ValueError):
            return None
    return None


def readings(providers):
    """{provider_id: {...}} только для провайдеров с пригодным недельным окном."""
    out = {}
    for pid, p in (providers or {}).items():
        r = _week_reading(p)
        if r is not None:
            out[pid] = r
    return out


def event_id(provider_id, resets_at, to_pct):
    """Стабильный id: одно и то же событие не попадёт в очередь дважды."""
    raw = "%s|%d|%.2f" % (provider_id, int(resets_at or 0), float(to_pct or 0.0))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def detect_resets(prev_readings, next_readings, cfg=None, while_away=False, now=None):
    """Событие, если сдвинулась граница окна ЛИБО подскочил остаток."""
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
            continue  # первое наблюдение -- только засев базовой линии
        boundary_moved = (new["resets_at"] - old["resets_at"]) > advance_threshold
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
