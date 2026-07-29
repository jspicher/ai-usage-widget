# Weekly Reset Alerts + OpenRouter Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Alert persistently when Claude's or Codex's weekly quota resets, and add OpenRouter as a third provider showing remaining credit balance.

**Architecture:** Detection lives in a new pure-logic module (`resetwatch.py`) with no GUI or network, so it can be unit-tested standalone. `widget.py` calls it from `refresh_all()`, the single choke point where a new snapshot replaces the old, and persists baseline plus undismissed alerts to a gitignored state file. Alerts surface in a dedicated frameless always-on-top pywebview window that does not take focus. OpenRouter is a fourth fetcher whose provider dict carries `kind: "balance"` so the UI renders a dollar figure instead of a quota bar.

**Tech Stack:** Python 3.13, pywebview 6.2.1 (WebView2), pystray 0.19.5, Pillow 12.3.0, stdlib `unittest`, `urllib.request`.

## Global Constraints

- Repo: `C:\Users\jeffs\bin\ai-usage-widget-src`, branch `overview-weekly`, remote `origin` = `github.com/jspicher/ai-usage-widget` (fork), `upstream` = `Trafalgardi/ai-usage-widget`.
- This checkout is what the running widget loads. Restart the app to see changes.
- Python is the project venv: `.\.venv\Scripts\python.exe`. Never use global `python`.
- Run the app: `wscript.exe start_widget.vbs`. Stop it: `Get-Process pythonw | Stop-Process -Force`.
- Run tests: `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v` (`-t .` so `import resetwatch` resolves from the repo root).
- **The fork is public.** Never commit account balances, usage totals, plan tiers, key labels, or API keys.
- Commit messages: plain, no attribution or co-author trailers.
- Source comments in this repo are Russian; new user-facing strings need both `ru` and `en` entries in the `L10N` object in `ui.html`.
- Never use em-dashes in code, comments, or commit messages. Use `--`.
- Detection compares two `resets_at` values against each other, never against `now()`. Do not introduce wall-clock comparisons.
- `webview.windows[0]` is assumed to be the main window by existing code (`JsApi.close`, `minimize_to_tray`). The alert window is created later so it is never index 0. Do not reorder window creation.

---

### Task 1: Reset detection logic

**Files:**
- Create: `resetwatch.py`
- Create: `tests/test_resetwatch.py`

**Interfaces:**
- Consumes: nothing (leaf module, stdlib only)
- Produces:
  - `readings(providers: dict) -> dict[str, dict]` mapping provider id to `{"resets_at": float, "remaining_pct": float}`, omitting providers with no usable weekly window
  - `detect_resets(prev_readings: dict, next_readings: dict, cfg: dict | None = None, while_away: bool = False, now: float | None = None) -> list[dict]`
  - `event_id(provider_id: str, resets_at: float, to_pct: float) -> str`
  - Constants `WEEK_WINDOW_ID = "week"`, `DEFAULT_PCT_JUMP = 10.0`, `DEFAULT_RESETS_ADVANCE_SEC = 3600`

- [ ] **Step 1: Create the tests directory and write the failing tests**

Create `tests/test_resetwatch.py`:

```python
import unittest

import resetwatch


def provider(pid="claude", ok=True, resets_at=1000.0, pct=20.0, wid="week"):
    """Build a provider dict shaped like widget.py's fetch_* output."""
    return {
        "id": pid, "name": pid, "ok": ok, "meta": {}, "error": None,
        "windows": [
            {"id": "session", "label": "s", "used_pct": 5.0,
             "remaining_pct": 95.0, "resets_at": 500.0},
            {"id": wid, "label": "w", "used_pct": 100.0 - pct,
             "remaining_pct": pct, "resets_at": resets_at},
        ],
    }


class TestReadings(unittest.TestCase):
    def test_extracts_only_the_week_window(self):
        r = resetwatch.readings({"claude": provider()})
        self.assertEqual(r, {"claude": {"resets_at": 1000.0, "remaining_pct": 20.0}})

    def test_skips_provider_that_is_not_ok(self):
        self.assertEqual(resetwatch.readings({"claude": provider(ok=False)}), {})

    def test_skips_provider_with_no_week_window(self):
        self.assertEqual(resetwatch.readings({"claude": provider(wid="month")}), {})

    def test_skips_none_values(self):
        p = provider()
        p["windows"][1]["remaining_pct"] = None
        self.assertEqual(resetwatch.readings({"claude": p}), {})


class TestDetect(unittest.TestCase):
    def setUp(self):
        self.prev = {"claude": {"resets_at": 1000.0, "remaining_pct": 20.0}}

    def test_boundary_advance_fires(self):
        nxt = {"claude": {"resets_at": 1000.0 + 7200, "remaining_pct": 100.0}}
        events = resetwatch.detect_resets(self.prev, nxt, now=42.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["provider"], "claude")
        self.assertEqual(events[0]["from_pct"], 20.0)
        self.assertEqual(events[0]["to_pct"], 100.0)
        self.assertEqual(events[0]["detected_at"], 42.0)
        self.assertFalse(events[0]["while_away"])

    def test_pct_jump_fires_without_boundary_move(self):
        nxt = {"claude": {"resets_at": 1000.0, "remaining_pct": 80.0}}
        self.assertEqual(len(resetwatch.detect_resets(self.prev, nxt)), 1)

    def test_ordinary_time_passing_does_not_fire(self):
        nxt = {"claude": {"resets_at": 999.0, "remaining_pct": 19.0}}
        self.assertEqual(resetwatch.detect_resets(self.prev, nxt), [])

    def test_sub_threshold_movement_does_not_fire(self):
        nxt = {"claude": {"resets_at": 1000.0 + 60, "remaining_pct": 25.0}}
        self.assertEqual(resetwatch.detect_resets(self.prev, nxt), [])

    def test_boundary_moving_backwards_does_not_fire(self):
        nxt = {"claude": {"resets_at": 1000.0 - 7200, "remaining_pct": 20.0}}
        self.assertEqual(resetwatch.detect_resets(self.prev, nxt), [])

    def test_missing_provider_in_next_is_skipped(self):
        self.assertEqual(resetwatch.detect_resets(self.prev, {}), [])

    def test_first_sighting_seeds_without_alerting(self):
        nxt = {"codex": {"resets_at": 5000.0, "remaining_pct": 90.0}}
        self.assertEqual(resetwatch.detect_resets(self.prev, nxt), [])

    def test_while_away_flag_is_propagated(self):
        nxt = {"claude": {"resets_at": 1000.0 + 7200, "remaining_pct": 100.0}}
        events = resetwatch.detect_resets(self.prev, nxt, while_away=True)
        self.assertTrue(events[0]["while_away"])

    def test_thresholds_are_configurable(self):
        nxt = {"claude": {"resets_at": 1000.0, "remaining_pct": 25.0}}
        cfg = {"pct_jump_threshold": 5}
        self.assertEqual(len(resetwatch.detect_resets(self.prev, nxt, cfg)), 1)

    def test_same_reset_yields_a_stable_id(self):
        nxt = {"claude": {"resets_at": 8000.0, "remaining_pct": 100.0}}
        a = resetwatch.detect_resets(self.prev, nxt, now=1.0)[0]["id"]
        b = resetwatch.detect_resets(self.prev, nxt, now=999.0)[0]["id"]
        self.assertEqual(a, b)

    def test_different_resets_yield_different_ids(self):
        a = resetwatch.event_id("claude", 8000.0, 100.0)
        b = resetwatch.event_id("claude", 9000.0, 100.0)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Expected: `ModuleNotFoundError: No module named 'resetwatch'`

- [ ] **Step 3: Write the implementation**

Create `resetwatch.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add resetwatch.py tests/test_resetwatch.py
git commit -m "Add weekly reset detection module"
```

---

### Task 2: Alert state persistence

**Files:**
- Modify: `resetwatch.py` (append `AlertStore`)
- Modify: `tests/test_resetwatch.py` (append store tests)
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `readings`, `detect_resets` from Task 1
- Produces: `AlertStore(path)` with `.seen: dict`, `.pending: list`, and methods `load() -> AlertStore`, `save() -> None`, `merge_seen(new_readings: dict) -> None`, `add(events: list) -> list` (returns only newly added), `dismiss(event_id: str) -> bool`, `dismiss_all() -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_resetwatch.py`, above the `if __name__` block:

```python
import json
import os
import tempfile


class TestAlertStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "state.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_loads_empty(self):
        s = resetwatch.AlertStore(self.path).load()
        self.assertEqual(s.seen, {})
        self.assertEqual(s.pending, [])

    def test_corrupt_file_loads_empty_instead_of_raising(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        s = resetwatch.AlertStore(self.path).load()
        self.assertEqual(s.seen, {})

    def test_round_trip(self):
        s = resetwatch.AlertStore(self.path).load()
        s.merge_seen({"claude": {"resets_at": 1.0, "remaining_pct": 2.0}})
        s.add([{"id": "abc", "provider": "claude"}])
        s.save()
        again = resetwatch.AlertStore(self.path).load()
        self.assertEqual(again.seen["claude"]["remaining_pct"], 2.0)
        self.assertEqual(len(again.pending), 1)

    def test_save_leaves_no_temp_files(self):
        s = resetwatch.AlertStore(self.path).load()
        s.save()
        self.assertEqual(os.listdir(self.dir.name), ["state.json"])

    def test_merge_seen_preserves_absent_providers(self):
        s = resetwatch.AlertStore(self.path).load()
        s.merge_seen({"claude": {"resets_at": 1.0, "remaining_pct": 2.0},
                      "codex": {"resets_at": 3.0, "remaining_pct": 4.0}})
        s.merge_seen({"claude": {"resets_at": 9.0, "remaining_pct": 9.0}})
        self.assertEqual(s.seen["codex"]["resets_at"], 3.0)
        self.assertEqual(s.seen["claude"]["resets_at"], 9.0)

    def test_add_dedupes_by_id(self):
        s = resetwatch.AlertStore(self.path).load()
        self.assertEqual(len(s.add([{"id": "x"}])), 1)
        self.assertEqual(len(s.add([{"id": "x"}])), 0)
        self.assertEqual(len(s.pending), 1)

    def test_dismiss_removes_one(self):
        s = resetwatch.AlertStore(self.path).load()
        s.add([{"id": "x"}, {"id": "y"}])
        self.assertTrue(s.dismiss("x"))
        self.assertEqual([e["id"] for e in s.pending], ["y"])
        self.assertFalse(s.dismiss("nope"))

    def test_dismiss_all_clears(self):
        s = resetwatch.AlertStore(self.path).load()
        s.add([{"id": "x"}, {"id": "y"}])
        self.assertEqual(s.dismiss_all(), 2)
        self.assertEqual(s.pending, [])
```

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Expected: `AttributeError: module 'resetwatch' has no attribute 'AlertStore'`

- [ ] **Step 3: Write the implementation**

Add these imports at the top of `resetwatch.py` (after `import hashlib`):

```python
import json
import os
import tempfile
```

Append to `resetwatch.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Expected: all tests PASS.

- [ ] **Step 5: Ignore the state file**

Add to `.gitignore` on the line after `config.json`:

```
reset-alert-state.json
```

- [ ] **Step 6: Commit**

```powershell
git add resetwatch.py tests/test_resetwatch.py .gitignore
git commit -m "Add alert state persistence with atomic writes"
```

---

### Task 3: Wire detection into the poll loop

No UI yet. This task ends with the state file being written correctly by the live app.

**Files:**
- Modify: `widget.py` (`DEFAULT_CONFIG` at line 39; imports; after `STATE = State()` / `CFG = load_config()` at line 513; `refresh_all()` at line 693)

**Interfaces:**
- Consumes: `resetwatch.AlertStore`, `resetwatch.readings`, `resetwatch.detect_resets`
- Produces: module globals `ALERT_STATE_PATH: str`, `ALERTS: resetwatch.AlertStore`; function `process_reset_alerts(providers: dict) -> list` returning newly added events

- [ ] **Step 1: Add the import**

In `widget.py`, after the existing stdlib imports and before the `try:` block that imports pystray, add:

```python
import resetwatch
```

- [ ] **Step 2: Add the config defaults**

In `DEFAULT_CONFIG` (line 39), add a `reset_alert` key after `"refresh_interval_sec": 60,`:

```python
    "reset_alert": {
        "enabled": True,
        "pct_jump_threshold": 10,
        "resets_at_advance_sec": 3600,
    },
```

- [ ] **Step 3: Create the store**

Immediately after `CFG = load_config()` (line 514), add:

```python
ALERT_STATE_PATH = os.path.join(APP_DIR, "reset-alert-state.json")
ALERTS = resetwatch.AlertStore(ALERT_STATE_PATH).load()
_FIRST_COMPARE = True
```

- [ ] **Step 4: Add the processing function**

Insert directly above `def refresh_all():` (line 693):

```python
def process_reset_alerts(providers):
    """Сравнивает свежий снимок с базовой линией и копит оповещения."""
    global _FIRST_COMPARE
    cfg = CFG.get("reset_alert") or {}
    new_readings = resetwatch.readings(providers)

    if not cfg.get("enabled", True):
        # Базовая линия обновляется всегда, чтобы повторное включение
        # не выдало пачку старых сбросов.
        ALERTS.merge_seen(new_readings)
        if ALERTS.pending:
            ALERTS.dismiss_all()
        ALERTS.save()
        _FIRST_COMPARE = False
        return []

    events = resetwatch.detect_resets(
        ALERTS.seen, new_readings, cfg, while_away=_FIRST_COMPARE)
    added = ALERTS.add(events)
    ALERTS.merge_seen(new_readings)
    ALERTS.save()
    _FIRST_COMPARE = False
    return added
```

- [ ] **Step 5: Call it from refresh_all**

In `refresh_all()`, replace this block:

```python
        with STATE.lock:
            STATE.snapshot = {"updated_at": time.time(), "providers": providers}
        TRAY.update_tooltip()
```

with:

```python
        with STATE.lock:
            STATE.snapshot = {"updated_at": time.time(), "providers": providers}
        try:
            process_reset_alerts(providers)
        except Exception:
            pass
        TRAY.update_tooltip()
```

- [ ] **Step 6: Verify against the live app**

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
wscript.exe start_widget.vbs
Start-Sleep -Seconds 15
Get-Content reset-alert-state.json -Raw
```

Expected: a JSON file with `seen` containing `claude` and `codex` entries and `pending` empty. First run seeds without alerting.

- [ ] **Step 7: Prove detection fires end to end**

Stop the app, hand-edit `reset-alert-state.json` to simulate a pre-reset baseline, restart, and confirm an event is queued:

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
$s = Get-Content reset-alert-state.json -Raw | ConvertFrom-Json
$s.seen.claude.remaining_pct = 1
$s.seen.claude.resets_at = $s.seen.claude.resets_at - 100000
$s | ConvertTo-Json -Depth 6 | Set-Content reset-alert-state.json -Encoding UTF8
wscript.exe start_widget.vbs
Start-Sleep -Seconds 15
Get-Content reset-alert-state.json -Raw
```

Expected: `pending` now holds one event for `claude` with `while_away: true`.

- [ ] **Step 8: Reset the state and commit**

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item reset-alert-state.json
git add widget.py
git commit -m "Detect weekly quota resets during the poll loop"
```

---

### Task 4: Alert window

**Files:**
- Create: `alert.html`
- Modify: `widget.py` (new `AlertApi` class and `AlertWindowManager`; call from `process_reset_alerts`)

**Interfaces:**
- Consumes: `ALERTS` store from Task 3
- Produces: `ALERT_WINDOW: AlertWindowManager` with `raise_alert() -> None` and `close_if_empty() -> None`; `AlertApi` exposing `get_alerts()`, `dismiss_alert(alert_id)`, `dismiss_all()` to `alert.html`

- [ ] **Step 1: Create alert.html**

```html
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  *{box-sizing:border-box}
  body{margin:0; background:#101012; color:#ececf1; font:12px/1.45 "Segoe UI",system-ui,sans-serif;
       -webkit-user-select:none; user-select:none; overflow:hidden}
  .wrap{padding:12px 14px}
  .head{display:flex; align-items:center; gap:8px; margin-bottom:10px}
  .head .bolt{color:#f5a623; font-size:14px}
  .head h1{font-size:12px; font-weight:600; margin:0; letter-spacing:.02em}
  .head .all{margin-left:auto; font-size:10.5px; opacity:.6; cursor:pointer; text-decoration:underline}
  .row{background:#17171b; border:1px solid #26262c; border-radius:8px;
       padding:10px 12px; margin-bottom:8px}
  .row .who{font-size:12px; font-weight:600; margin-bottom:3px}
  .row .delta{font-size:11px; opacity:.85}
  .row .delta b{color:#3ecf8e; font-weight:600}
  .row .when{font-size:10px; opacity:.5; margin-top:3px}
  .row .away{display:inline-block; margin-top:5px; font-size:9.5px; letter-spacing:.04em;
             text-transform:uppercase; background:#2b2410; color:#f5a623;
             border-radius:4px; padding:2px 6px}
  .row .btn{margin-top:9px; width:100%; padding:6px 0; font-size:11px; cursor:pointer;
            background:#26262c; color:#ececf1; border:1px solid #33333a; border-radius:6px}
  .row .btn:hover{background:#2f2f36}
  .empty{opacity:.5; text-align:center; padding:18px 0}
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <span class="bolt">&#9889;</span>
    <h1 id="title">Quota reset</h1>
    <span class="all" id="all" style="display:none">Dismiss all</span>
  </div>
  <div id="rows"></div>
</div>
<script>
const NAMES = {claude:"Claude Code", codex:"Codex CLI"};

function esc(s){ return String(s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function clock(epoch){
  const d = new Date(epoch * 1000);
  return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
}

async function renderAlerts(){
  if(!window.pywebview || !window.pywebview.api) return;
  const alerts = await window.pywebview.api.get_alerts();
  const rows = document.getElementById("rows");
  document.getElementById("all").style.display = alerts.length > 1 ? "inline" : "none";
  if(!alerts.length){ rows.innerHTML = '<div class="empty">No alerts</div>'; return; }
  rows.innerHTML = alerts.map(a => `
    <div class="row">
      <div class="who">${esc(NAMES[a.provider] || a.provider)}</div>
      <div class="delta">Weekly ${Math.round(a.from_pct)}% &rarr; <b>${Math.round(a.to_pct)}%</b> remaining</div>
      <div class="when">detected ${clock(a.detected_at)}</div>
      ${a.while_away ? '<div class="away">while you were away</div>' : ''}
      <button class="btn" data-id="${esc(a.id)}">Dismiss</button>
    </div>`).join("");
  rows.querySelectorAll(".btn").forEach(b =>
    b.addEventListener("click", async () => {
      await window.pywebview.api.dismiss_alert(b.dataset.id);
      renderAlerts();
    }));
}
window.renderAlerts = renderAlerts;

document.getElementById("all").addEventListener("click", async () => {
  await window.pywebview.api.dismiss_all();
  renderAlerts();
});

window.addEventListener("pywebviewready", renderAlerts);
setTimeout(renderAlerts, 300);
</script>
</body>
</html>
```

- [ ] **Step 2: Add AlertApi and AlertWindowManager to widget.py**

Insert directly above `def process_reset_alerts(providers):`:

```python
class AlertApi:
    """API окна оповещений."""

    def get_alerts(self):
        return copy.deepcopy(ALERTS.pending)

    def dismiss_alert(self, alert_id):
        ALERTS.dismiss(alert_id)
        ALERTS.save()
        ALERT_WINDOW.close_if_empty()
        return True

    def dismiss_all(self):
        ALERTS.dismiss_all()
        ALERTS.save()
        ALERT_WINDOW.close_if_empty()
        return True


class AlertWindowManager:
    """Одно окно на все оповещения. Не забирает фокус."""

    WIDTH = 340
    MARGIN = 16

    def __init__(self):
        self.window = None
        self.lock = threading.Lock()

    def _corner(self, height):
        """Правый нижний угол рабочей области, с запасом под панель задач."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            return sw - self.WIDTH - self.MARGIN, sh - height - 60
        except Exception:
            return None, None

    def raise_alert(self):
        with self.lock:
            if self.window is not None:
                try:
                    self.window.evaluate_js(
                        "window.renderAlerts && window.renderAlerts()")
                    return
                except Exception:
                    self.window = None
            height = min(460, 90 + 130 * max(1, len(ALERTS.pending)))
            x, y = self._corner(height)
            try:
                self.window = webview.create_window(
                    "Quota reset",
                    url=os.path.join(APP_DIR, "alert.html"),
                    js_api=AlertApi(),
                    width=self.WIDTH, height=height, x=x, y=y,
                    frameless=True, easy_drag=True, resizable=False,
                    on_top=True, focus=False,
                    background_color="#101012",
                )
            except Exception:
                self.window = None

    def close_if_empty(self):
        with self.lock:
            if self.window is not None and not ALERTS.pending:
                try:
                    self.window.destroy()
                except Exception:
                    pass
                self.window = None


ALERT_WINDOW = AlertWindowManager()
```

- [ ] **Step 3: Raise the window when anything is pending**

In `process_reset_alerts`, replace the final `return added` with:

```python
    if ALERTS.pending:
        ALERT_WINDOW.raise_alert()
    return added
```

Raising on `pending` rather than on `added` is deliberate: it also restores an undismissed alert after a restart.

- [ ] **Step 4: Fire a best-effort toast alongside the window**

The window is the surface that persists; the toast is redundancy for the moment the reset lands. `pystray` icons expose `notify()`. Add this method to `AlertWindowManager`:

```python
    def toast(self, events):
        """Дополнение к окну, а не замена: системный тост сам исчезнет."""
        if not events or not TRAY_AVAILABLE:
            return
        icon = TRAY.icon_claude or TRAY.icon_codex
        if icon is None:
            return
        names = {"claude": "Claude Code", "codex": "Codex CLI"}
        if len(events) == 1:
            body = "%s: weekly quota reset" % names.get(
                events[0]["provider"], events[0]["provider"])
        else:
            body = "%d weekly quotas reset" % len(events)
        try:
            icon.notify(body, "AI Usage Widget")
        except Exception:
            pass
```

Then in `process_reset_alerts`, change the block added in Step 3 to toast only for genuinely new events:

```python
    if added:
        ALERT_WINDOW.toast(added)
    if ALERTS.pending:
        ALERT_WINDOW.raise_alert()
    return added
```

Toasting on `added` rather than `pending` prevents a toast on every poll while an alert sits undismissed.

- [ ] **Step 5: Verify with a simulated reset**

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
wscript.exe start_widget.vbs
Start-Sleep -Seconds 15
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
$s = Get-Content reset-alert-state.json -Raw | ConvertFrom-Json
$s.seen.claude.remaining_pct = 1
$s.seen.claude.resets_at = $s.seen.claude.resets_at - 100000
$s | ConvertTo-Json -Depth 6 | Set-Content reset-alert-state.json -Encoding UTF8
wscript.exe start_widget.vbs
```

Expected, checked by hand: the alert window appears bottom-right above other windows; clicking into another app and typing shows it never took focus; it stays until Dismiss is clicked; clicking Dismiss closes it; restarting before dismissing brings it back.

- [ ] **Step 6: Reset state and commit**

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item reset-alert-state.json
git add alert.html widget.py
git commit -m "Add persistent dismissible alert window for quota resets"
```

---

### Task 5: OpenRouter provider

**Files:**
- Modify: `widget.py` (new fetcher; `kind` on existing fetchers; `refresh_all`)

**Interfaces:**
- Consumes: `http_get_json`, `pick`, `CFG`
- Produces: `fetch_openrouter() -> dict` with `kind: "balance"` and `meta["balance"] = {"remaining_usd", "total_usd", "used_usd", "week_usd"}`, `meta["key_source"]`, `meta["label"]`, `meta["key_masked"]`; `_openrouter_key() -> tuple[str | None, str | None]`

- [ ] **Step 1: Tag the existing providers with a kind**

In `fetch_claude()` (line 166) change the initial dict to include `"kind": "windows",`. Do the same in `fetch_codex()` (line 260). Both currently start:

```python
    result = {"id": "claude", "name": "Claude Code", "ok": False,
              "windows": [], "meta": {}, "error": None}
```

becomes:

```python
    result = {"id": "claude", "name": "Claude Code", "kind": "windows", "ok": False,
              "windows": [], "meta": {}, "error": None}
```

- [ ] **Step 2: Add the fetcher**

Insert above `class State:` (line 505):

```python
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


def _openrouter_key():
    """Ключ из окружения, затем из config.json. Приложение ключ не пишет."""
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        return key, "env"
    key = ((CFG.get("openrouter") or {}).get("api_key") or "").strip()
    if key:
        return key, "config.json"
    return None, None


def fetch_openrouter():
    result = {"id": "openrouter", "name": "OpenRouter", "kind": "balance",
              "ok": False, "windows": [], "meta": {}, "error": None}
    key, source = _openrouter_key()
    result["meta"]["key_source"] = source
    if not key:
        result["error"] = "Не задан OPENROUTER_API_KEY"
        return result

    headers = {
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "ai-usage-widget/1.0",
    }
    try:
        data = http_get_json(OPENROUTER_CREDITS_URL, headers) or {}
    except urllib.error.HTTPError as e:
        msg = "HTTP %s" % e.code
        if e.code in (401, 403):
            msg += " -- ключ отклонён. Проверь OPENROUTER_API_KEY."
        result["error"] = msg
        return result
    except Exception as e:
        result["error"] = "%s: %s" % (type(e).__name__, e)
        return result

    credits = data.get("data") or {}
    total = pick(credits, "total_credits")
    used = pick(credits, "total_usage")
    if total is None or used is None:
        result["error"] = "API ответил, но лимиты не найдены"
        return result

    balance = {
        "remaining_usd": round(float(total) - float(used), 2),
        "total_usd": round(float(total), 2),
        "used_usd": round(float(used), 2),
        "week_usd": None,
    }
    # Недельный расход и метка ключа не критичны: сбой здесь не роняет карточку.
    try:
        key_data = (http_get_json(OPENROUTER_KEY_URL, headers) or {}).get("data") or {}
        weekly = key_data.get("usage_weekly")
        if weekly is not None:
            balance["week_usd"] = round(float(weekly), 2)
        result["meta"]["label"] = key_data.get("label")
    except Exception:
        pass

    result["meta"]["balance"] = balance
    result["meta"]["key_masked"] = (key[:8] + "\u2026" + key[-4:]) if len(key) > 14 else "\u2026"
    result["ok"] = True
    return result
```

- [ ] **Step 3: Add it to the poll**

In `refresh_all()`, change `max_workers=2` to `max_workers=3` and add the third submission:

```python
            futures = {
                executor.submit(fetch_claude): "claude",
                executor.submit(fetch_codex): "codex",
                executor.submit(fetch_openrouter): "openrouter",
            }
```

- [ ] **Step 4: Verify the provider resolves**

```powershell
.\.venv\Scripts\python.exe -c "import widget, json; r = widget.fetch_openrouter(); print('ok=', r['ok'], 'err=', r['error'], 'source=', r['meta'].get('key_source')); b = r['meta'].get('balance') or {}; print('has_remaining=', b.get('remaining_usd') is not None, 'has_week=', b.get('week_usd') is not None)"
```

Expected: `ok= True`, `err= None`, `source= env`, both `has_*` True. Do not paste the printed values into any committed file.

- [ ] **Step 5: Commit**

```powershell
git add widget.py
git commit -m "Add OpenRouter credits provider"
```

---

### Task 6: Render OpenRouter on the Overview

**Files:**
- Modify: `ui.html` (`GLYPH`/`THEME` near line 422, `renderOverview` near line 480, `renderDetail`, `L10N`, `.strip` CSS)

**Interfaces:**
- Consumes: provider dicts with `kind: "balance"` and `meta.balance` from Task 5
- Produces: no new exports

- [ ] **Step 1: Add the glyph, theme, and strings**

In `ui.html`, extend `GLYPH` and `THEME`:

```javascript
const GLYPH = { claude:"✳", codex:OPENAI_MARK, opencode:"█", openrouter:"◆" };
const THEME = { claude:"th-claude", codex:"th-codex", opencode:"th-opencode", openrouter:"th-openrouter" };
```

Add the theme colour next to the existing `.th-codex` rules in the CSS block:

```css
  .th-openrouter{background:#141419}
  .th-openrouter .glyph{color:#8b7cf6}
  .th-openrouter .bar i{background:#8b7cf6}
```

Add to the `ru` L10N object:

```javascript
    credits: "Кредиты",
    spentWeek: "за неделю",
```

Add to the `en` L10N object:

```javascript
    credits: "Credits",
    spentWeek: "this week",
```

- [ ] **Step 2: Render the balance strip**

In `renderOverview`, change the loop header from:

```javascript
  for(const id of ["claude","codex"]){
```

to:

```javascript
  for(const id of ["claude","codex","openrouter"]){
    if(!DATA.providers[id]) continue;
```

Then, immediately after `const theme = THEME[id];`, insert the balance branch:

```javascript
    if(p.kind === "balance"){
      const b = (p.meta && p.meta.balance) || null;
      html += `<div class="strip ${theme}" data-goto="${id}">
        <div class="top">
          <span class="glyph">${GLYPH[id]}</span>
          <span class="name">${esc(p.name || id)}</span>
          <span class="pct">${b ? "$" + b.remaining_usd.toFixed(2) : "—"}</span>
        </div>`;
      if(b){
        html += `<div class="sub">
            <span>${t("credits")} · ${t("remaining")}</span>
            <span>${b.week_usd != null ? "$" + b.week_usd.toFixed(2) + " " + t("spentWeek") : ""}</span>
          </div>`;
      } else {
        html += `<div class="err">${esc(terr(p.error) || t("noData"))}</div>`;
      }
      html += `</div>`;
      continue;
    }
```

No progress bar is rendered: credits are purchased rather than granted, so there is no quota for a bar to be a fraction of.

- [ ] **Step 3: Guard the detail page**

`renderDetail` iterates `p.windows`, which is empty for OpenRouter. Add a balance branch at the top of `renderDetail`, directly after the `if(!p){...}` guard:

```javascript
  if(p.kind === "balance"){
    const b = (p.meta && p.meta.balance) || null;
    el.innerHTML = `<div class="detail ${THEME[id]}">
      <div class="brand"><span class="glyph">${GLYPH[id]}</span><h1>${esc(p.name)}</h1></div>
      ${b ? `<div class="win">
          <div class="head">
            <span class="label">${t("credits")}</span>
            <span class="bigpct">$${b.remaining_usd.toFixed(2)}</span>
          </div>
          <div class="usd">$${b.used_usd.toFixed(2)}${t("of")}$${b.total_usd.toFixed(2)}</div>
          ${b.week_usd != null ? `<div class="usd">$${b.week_usd.toFixed(2)} ${t("spentWeek")}</div>` : ""}
        </div>` : `<div class="err">${esc(terr(p.error) || t("noData"))}</div>`}
    </div>`;
    return;
  }
```

- [ ] **Step 4: Add the detail page container and nav button**

In the HTML body, after `<div class="page" id="page-codex"></div>`, add:

```html
    <div class="page" id="page-openrouter"></div>
```

In the nav row, after the Codex button, add:

```html
    <button data-page="openrouter">OpenRouter</button>
```

- [ ] **Step 5: Verify**

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
wscript.exe start_widget.vbs
```

Expected, checked by hand: Overview shows three strips; OpenRouter shows a dollar figure and weekly spend with no bar; clicking it opens an OpenRouter detail page; the tray still has exactly two icons.

- [ ] **Step 6: Commit**

```powershell
git add ui.html
git commit -m "Render OpenRouter credit balance on the overview"
```

---

### Task 7: Settings -- alert toggle and connector rows

**Files:**
- Modify: `ui.html` (`renderSettings` near line 616, save handler near line 670, `L10N`)
- Modify: `widget.py` (`JsApi.get_data` to expose connector state)

**Interfaces:**
- Consumes: `CFG["reset_alert"]` from Task 3, `meta.key_source` / `meta.label` / `meta.key_masked` from Task 5
- Produces: no new exports

- [ ] **Step 1: Add the strings**

Add to `ru`:

```javascript
    resetAlert: "Оповещение о сбросе недели",
    connectors: "Коннекторы",
    connConnected: "подключено",
    connMissing: "не настроено",
```

Add to `en`:

```javascript
    resetAlert: "Weekly reset alert",
    connectors: "Connectors",
    connConnected: "connected",
    connMissing: "not configured",
```

- [ ] **Step 2: Add the toggle and connector section**

In `renderSettings`, insert this section immediately before the `<button class="save-btn"` line:

```javascript
      <div class="section">
        <div class="section-title">${t("resetAlert")}</div>
        <div class="toggle-row">
          <label>${t("resetAlert")}</label>
          <label class="switch">
            <input type="checkbox" id="cfg-alert" ${cfg.reset_alert?.enabled !== false ? 'checked' : ''}>
            <span class="slider"></span>
          </label>
        </div>
      </div>
      <div class="section">
        <div class="section-title">${t("connectors")}</div>
        ${["claude","codex","openrouter"].map(id => {
          const p = (DATA.providers || {})[id];
          const on = p && p.ok;
          const src = p && p.meta && p.meta.key_source;
          return `<div class="field" style="align-items:center">
            <label>${esc(p ? p.name : id)}</label>
            <span style="font-size:11px; opacity:.8">
              <span style="color:${on ? '#3ecf8e' : '#d05252'}">&#9679;</span>
              ${on ? t("connConnected") : t("connMissing")}${src ? " &middot; " + esc(src) : ""}
            </span>
          </div>`;
        }).join("")}
      </div>
```

- [ ] **Step 3: Persist the toggle**

In the `#btn-save-settings` click handler, add the key to `newCfg`:

```javascript
      reset_alert: { enabled: $("#cfg-alert").checked },
```

`save_config_api` merges nested dicts key-by-key, so the thresholds already in `config.json` are preserved.

- [ ] **Step 4: Guard the edit-in-progress early return**

`renderSettings` returns early when a `cfg-` field has focus, which would now also skip the connector rows. Change the guard so it only applies to text inputs:

```javascript
  if(existingRefresh && document.activeElement?.id?.startsWith("cfg-")
     && document.activeElement.type !== "checkbox") return;
```

- [ ] **Step 5: Verify**

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
wscript.exe start_widget.vbs
```

Expected, checked by hand: Settings shows the toggle checked by default and three connector rows with green dots, OpenRouter reading `connected · env`. Untick the toggle, Save, restart, and confirm `config.json` holds `"reset_alert": {"enabled": false, ...}` with thresholds intact and no alert window appears even with a simulated reset.

- [ ] **Step 6: Commit**

```powershell
git add ui.html widget.py
git commit -m "Add reset alert toggle and connector status to settings"
```

---

### Task 8: Full verification and push

**Files:** none modified

- [ ] **Step 1: Run the whole test suite**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Expected: all PASS.

- [ ] **Step 2: Confirm no secrets or state are staged**

```powershell
git status --short
git ls-files | Select-String -Pattern "config.json|reset-alert-state.json"
```

Expected: the second command prints nothing.

- [ ] **Step 3: Cold start check**

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item reset-alert-state.json -ErrorAction SilentlyContinue
wscript.exe start_widget.vbs
Start-Sleep -Seconds 20
Get-Content reset-alert-state.json -Raw
```

Expected: seeded baseline, `pending` empty, no alert window on a clean first run.

- [ ] **Step 4: Push**

```powershell
git push origin overview-weekly
```

---

## Notes for the implementer

- `refresh_all()` holds `STATE.refresh_lock` for the whole call, so `process_reset_alerts` cannot run concurrently with itself. `AlertApi` dismissals mutate `ALERTS` from the GUI thread; they only remove entries, and the poll thread only appends, so no additional lock is needed.
- If `webview.create_window` raises because the GUI loop has not started yet, `raise_alert` swallows it and the next poll retries. That is intentional -- the first poll can land before `webview.start()`.
- **`alert.html` ships English-only**, unlike `ui.html` which is fully bilingual. This is a
  deliberate scope cut, not an oversight: the alert has six short strings and the `L10N`
  object lives inside `ui.html`, so sharing it would mean extracting the translation table
  into its own file. Worth doing if the alert copy grows; noted here so it is a decision
  rather than a surprise.
- The Codex classification fix in commit `bf23d59` is an upstream bug affecting any plan whose API returns only a weekly window. Consider a separate PR to `upstream` after this branch settles.
