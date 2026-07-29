# Weekly reset alerts + OpenRouter provider — design

Status: approved, pending implementation plan
Branch: `overview-weekly`

## Context

This fork already diverges from upstream in three commits:

1. Overview renders every window (session + weekly) per provider instead of only the first.
2. Codex window classification fixed. The API reports window length in
   `limit_window_seconds`, but `add_window()` only looked for `window_minutes` /
   `limit_window_minutes`, so length detection never fired and a 7-day window kept the
   fallback label "Session (5h)". On plans where the API returns only a weekly window and
   `secondary_window: null`, the card claimed a 5-hour session with a multi-day countdown.
   Tray lookups that hard-required `id == "session"` gained a `primary_window()` fallback so
   correcting the id could not blank the tray.
3. Codex glyph replaced with an inline OpenAI SVG. `⬡` (U+2B21) has no glyph in the WebView2
   font stack and rendered as tofu.

This document covers two additions on top of that: persistent weekly-reset alerts, and
OpenRouter as a third provider.

## Goals

- Alert when the **weekly** window resets for Claude or Codex, covering both the normal
  cadence and off-cadence vendor resets.
- The alert stays on screen until explicitly dismissed. Windows toasts auto-timeout, so the
  app must own a surface that does not.
- Survive restarts, in both directions: a reset that happened while the app was closed still
  raises an alert, and an undismissed alert reappears.
- Add OpenRouter, showing remaining credit balance.
- A setting to disable the alert.

## Non-goals

Named explicitly so they don't get built by accident:

- Alerts for the 5-hour session window.
- Alerts for Claude's `week_opus` / `week_sonnet` / `week_apps` sub-windows. They are not
  present in the accounts this was built against; revisit only if they appear.
- OpenRouter low-balance or spend alerts. OpenRouter does not participate in the alert system
  at all.
- A third tray icon for OpenRouter. The tray stays at two icons; OpenRouter appears in the
  window and tooltip only.
- Any UI that writes a credential to disk.

## Detection — `resetwatch.py` (new module)

Pure logic. No GUI, no network, no global state. `widget.py` is already ~1000 lines and this
logic is subtle enough to deserve isolation and its own tests.

```
detect_resets(prev_providers, next_providers, cfg) -> list[ResetEvent]
```

For each provider in scope, compare the window with `id == "week"` across the two snapshots.
A reset fires when **either** signal trips:

| Signal | Condition | Catches |
|---|---|---|
| Boundary moved | `resets_at` advances by more than `resets_at_advance_sec` (default 3600) | Normal weekly cadence |
| Balance jumped | `remaining_pct` rises by at least `pct_jump_threshold` (default 10) | Off-cadence grant that refills without moving the boundary |

Either alone is insufficient: requiring both would miss the surprise-grant case, which is a
primary requirement.

### Guards

Each of these maps to a real failure mode:

- **Provider health.** Both samples must have `ok == true` with non-`None` `resets_at` and
  `remaining_pct`. An API error or expired token is skipped and must **not** overwrite the
  last-known baseline, otherwise recovery from an error would look like a reset.
- **Clock independence.** Detection compares two `resets_at` *values* against each other and
  never against `now()`. A system clock jump — manual correction, time sync, VM snapshot
  resume — therefore cannot fabricate or suppress an event.
- **Dedupe.** Events are keyed on `(provider, resets_at, to_pct)`. A flapping API that
  oscillates between two readings cannot re-fire the same event.
- **First-run seeding.** With no state file, the first poll seeds the baseline silently. It
  does not alert, which would otherwise fire for every provider on first launch.

## State — `reset-alert-state.json`

Written next to `widget.py`. Must be added to `.gitignore`, which currently ignores only
`config.json`.

```json
{
  "seen":    { "claude:week": { "resets_at": 0, "remaining_pct": 0.0 } },
  "pending": [ { "id": "", "provider": "claude", "from_pct": 0.0, "to_pct": 0.0,
                 "detected_at": 0, "while_away": false } ]
}
```

`seen` is the last known good weekly reading per provider. `pending` is undismissed alerts.
Field values above illustrate types, not defaults. `id` is a stable string derived from the
dedupe key `(provider, resets_at, to_pct)`, so the same underlying reset always produces the
same id across restarts and cannot be queued twice.

On startup the first poll is diffed against `seen`. A reset detected on that first
comparison is flagged `while_away: true` and labelled accordingly in the UI, since the app
cannot know when in the gap it happened. Anything already in `pending` is re-shown.

Writes are atomic (temp file plus replace) so a crash mid-write cannot corrupt the file. A
malformed or unreadable file is treated as absent and re-seeded rather than crashing the poll
loop.

## Alert window

A dedicated frameless `on_top` window created with `webview.create_window()` after
`webview.start()`. The main window already uses `frameless=True` and `on_top`, so this is an
established pattern in this codebase rather than a new one.

Behaviour:

- Appears bottom-right, above other windows.
- **Does not take keyboard focus.** The user may be typing in another application; an alert
  that steals focus can swallow a keystroke. This rules out Enter/Esc to dismiss — dismissal
  is a deliberate click.
- A toast fires alongside as best-effort backup, accepting that it will time out.
- Content per row: provider, `from_pct` → `to_pct`, detected time, and the "while you were
  away" tag when set.
- **One window total.** Concurrent resets stack as rows, each with its own Dismiss, plus a
  Dismiss-all control when more than one is pending. Events arriving while the window is open
  append live. The window closes when the last row is dismissed. Nothing auto-clears.

Rendered from a separate `alert.html`, not by overloading `ui.html`.

## Wiring

`refresh_all()` is the single choke point where a new snapshot replaces the old under
`STATE.lock`, so detection hooks in there: capture the previous snapshot, call
`detect_resets`, persist, and raise the window if anything is pending.

`JsApi` gains `get_alerts()`, `dismiss_alert(id)`, and `dismiss_all()`.

## OpenRouter provider

`fetch_openrouter()` alongside the existing fetchers, following their shape.

- Credential: `OPENROUTER_API_KEY` from the environment, falling back to
  `openrouter.api_key` in `config.json` for portability. The application never writes a
  credential to disk.
- `GET /api/v1/credits` → `total_credits`, `total_usage`; balance is the difference.
- `GET /api/v1/key` → `usage_weekly` for spend context, plus `label` for display.
- Unlike the Claude and Codex endpoints, these are documented and supported, so this
  connector should be markedly more stable than the other two.

Provider dicts gain `kind: "windows" | "balance"` so `renderOverview` can branch. OpenRouter
renders as a **plain dollar figure with weekly spend and no progress bar** — credits are
purchased rather than granted, so there is no quota for a bar to be a fraction of, and a bar
against total-ever-purchased would drift toward meaningless.

Settings gains a read-only connector row: connected state, which source the key came from,
the key label, and a masked suffix. No entry field.

## Settings

A single `reset_alert.enabled` toggle covering both providers, **default on**. Thresholds live
in `config.json` only and are not surfaced in the UI.

```json
{ "reset_alert": { "enabled": true,
                   "pct_jump_threshold": 10,
                   "resets_at_advance_sec": 3600 } }
```

When disabled: no alert window is raised and `pending` is cleared, but `seen` continues to be
updated on every poll. Re-enabling therefore starts clean rather than firing a backlog of
resets that occurred while the feature was off.

## Testing

`tests/test_resetwatch.py`, stdlib `unittest`, no network and no GUI. The repository has no
tests today; this module is the one piece subtle enough to justify them.

Cases: boundary-moved fires; balance-jumped fires; ordinary time passing does not; sub-threshold
movement does not; error or `None` samples are skipped and preserve the baseline; a clock jump
does not fabricate an event; first run seeds without alerting; dedupe suppresses a repeat;
`while_away` is set only on the first post-startup comparison.

## Risks

- **Undocumented endpoints.** Claude and Codex usage endpoints are undocumented and may change
  without notice. Detection must degrade to "no event" rather than raising false alerts when a
  response shape changes.
- **Detection is inherently heuristic.** There is no reset event from either vendor; it is
  inferred from two samples. The thresholds are the tuning surface if false positives appear.
- **Public fork.** This repository is public. Account figures, key labels, and plan tiers must
  stay out of committed files. `config.json` and `reset-alert-state.json` are both gitignored.
