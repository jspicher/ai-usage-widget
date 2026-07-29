"""Обнаружение сброса недельной квоты.

Чистая логика: без GUI, без сети, без глобального состояния.
Сравнение идёт между двумя значениями resets_at, а не с текущим временем,
поэтому скачок системных часов не может создать ложное событие.

Оговорка: часть API отдаёт не абсолютный resets_at, а "через сколько
секунд", и тогда widget.py вынужден вычислить момент как now() + secs.
Такое значение само зависит от часов, и сравнение двух подряд идущих
вычисленных отметок снова стало бы сравнением с часами. Такие окна
помечены флагом resets_at_derived, и для них сигнал сдвига границы
отключён -- остаётся только скачок остатка, на часы не завязанный.
"""

import hashlib
import time
import json
import os
import tempfile

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
        extra = w.get("extra") or {}
        try:
            return {
                "resets_at": float(resets_at),
                "remaining_pct": float(pct),
                # Отметка "вычислено от текущего времени" -- см. докстринг модуля.
                "resets_at_derived": bool(extra.get("resets_at_derived")),
            }
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
        # Если хоть одна из двух отметок вычислена от now(), их разность
        # отражает в том числе сдвиг часов -- сигнал границы отключаем и
        # полагаемся только на скачок остатка.
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
    """Базовая линия показаний плюс неотклонённые оповещения, на диске.

    Запись атомарная (временный файл + replace): падение в момент записи
    не может испортить файл. Битый файл трактуется как отсутствующий.
    """

    def __init__(self, path):
        self.path = path
        self.seen = {}
        self.pending = []

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

    def save(self):
        payload = {"seen": self.seen, "pending": self.pending}
        folder = os.path.dirname(os.path.abspath(self.path)) or "."
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".reset-alert-",
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            tmp = None
        except Exception:
            pass
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def merge_seen(self, new_readings):
        """Обновляет только присутствующие ключи.

        Провайдер с ошибкой отсутствует в new_readings, и его базовая линия
        должна сохраниться -- иначе восстановление после сбоя выглядело бы
        как сброс квоты.
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
