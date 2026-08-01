# -*- coding: utf-8 -*-
"""
AI Usage Widget — remaining-quota widget for Claude Code, Codex CLI, and OpenRouter.

Reads each CLI's local authentication files and queries its usage endpoints:
  * Claude Code : ~/.claude/.credentials.json  -> api.anthropic.com/api/oauth/usage
  * Codex CLI   : ~/.codex/auth.json           -> chatgpt.com/backend-api/wham/usage
  * OpenRouter  : OPENROUTER_API_KEY            -> openrouter.ai/api/v1/credits

Run:          python widget.py
Dependencies: pip install -r requirements.txt
"""

import base64
import copy
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import resetwatch

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

# The bundle directory and the data directory are DIFFERENT things and must not
# be merged back into one variable.
#
# In a one-file build, PyInstaller extracts ui.html / alert.html / icon into a
# temporary _MEIxxxxx directory and deletes it when the process exits. Resources
# must be read from there (otherwise the app cannot find its own HTML), but
# writing there is pointless: config.json and reset-alert-state.json would
# disappear with the directory on every exit. No error is raised—the write
# succeeds, the file simply vanishes, and the promised persistence across
# restarts silently never works.
#
# Therefore: APP_DIR is only for bundled resources (read-only), while DATA_DIR
# is only for user state (read-write).
if getattr(sys, "frozen", False):
    APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = APP_DIR
HOME = os.path.expanduser("~")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ERROR_LOG_PATH = os.path.join(DATA_DIR, "widget-error.log")

SUPPORTED_LANGUAGES = ("en", "ru")
REFRESH_MIN_SEC = 15
REFRESH_MAX_SEC = 600

DEFAULT_CONFIG = {
    "refresh_interval_sec": 300,
    "display": {
        "daily_markers": True,
    },
    "reset_alert": {
        # Borrowed from resetwatch so the detector's own fallbacks and the
        # config defaults cannot drift apart.
        "enabled": True,
        "pct_jump_threshold": resetwatch.DEFAULT_PCT_JUMP,
        "resets_at_advance_sec": resetwatch.DEFAULT_RESETS_ADVANCE_SEC,
    },
    "window": {"x": None, "y": None, "width": 380, "height": 400, "on_top": True},
    "language": "en",
    "openrouter": {"api_key": ""},
}

CONFIG_HEALTH = {
    "status": "ok",
    "error": None,
    "recovery_required": False,
    "backup_path": None,
}
_CONFIG_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


# Strings drawn by Windows itself: the tray menu and tooltip, the toast, and
# the alert window's title bar. None of these can reach ui.html's L10N table,
# so they need their own. Anything rendered *inside* a WebView belongs in
# L10N, not here -- see the error_info/terr split.
NATIVE_TEXT = {
    "en": {
        "tray_show": "Show",
        "tray_refresh": "Refresh",
        "tray_exit": "Exit",
        "tooltip_resets_in": "resets in",
        "unit_hour": "h",
        "unit_minute": "m",
        "alert_title": "Quota reset",
        "toast_one": "%(provider)s: weekly quota reset",
        "toast_many": "%(count)d weekly quotas reset",
    },
    "ru": {
        "tray_show": "Показать",
        "tray_refresh": "Обновить",
        "tray_exit": "Выход",
        "tooltip_resets_in": "сброс",
        "unit_hour": "ч",
        "unit_minute": "м",
        "alert_title": "Сброс квоты",
        "toast_one": "%(provider)s: недельная квота сброшена",
        "toast_many": "Сброшено недельных квот: %(count)d",
    },
}


def current_language():
    """Return the configured language, or the default if it is unrecognized.

    Every write path runs through normalize_config, so CFG should already hold
    a supported code; the check stays because tests assign CFG entries in place
    and a native surface must never render a raw, unvalidated value.
    """
    language = CFG.get("language", DEFAULT_CONFIG["language"])
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_CONFIG["language"]


def native_text(key, **params):
    """Look up a string for a native Windows surface."""
    text = NATIVE_TEXT[current_language()].get(key) or NATIVE_TEXT["en"][key]
    return text % params if params else text


def error_info(code, params=None, detail=None):
    """Build a stable, localizable error payload for the JavaScript bridge."""
    result = {"code": code}
    if params:
        result["params"] = params
    if detail:
        result["detail"] = str(detail)
    return result


def _log_failure(operation, exc):
    """Append a diagnostic to the widget log without raising."""
    try:
        line = "%s %s failed: %s: %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"),
            operation,
            type(exc).__name__,
            exc,
        )
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _number(value, default, minimum=None, maximum=None, integer=False):
    """Return a finite, optionally clamped number, or the documented default."""
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("not finite")
    except (TypeError, ValueError, OverflowError):
        return default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return int(result) if integer else result


def _bool(value, default):
    """Return the value only when it is a real bool, else the default.

    A hand-edited "yes" or 1 is not a boolean and must not be accepted as one.
    """
    return value if isinstance(value, bool) else default


def _set_health(status, error=None, recovery_required=False, backup_path=None):
    """Replace the config health record; every field is rewritten every time."""
    global CONFIG_HEALTH
    CONFIG_HEALTH = {
        "status": status,
        "error": error,
        "recovery_required": recovery_required,
        "backup_path": backup_path,
    }


def normalize_config(value):
    """Merge and validate supported configuration values."""
    source = value if isinstance(value, dict) else {}
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    language = source.get("language")
    cfg["language"] = (
        language if language in SUPPORTED_LANGUAGES else DEFAULT_CONFIG["language"])
    cfg["refresh_interval_sec"] = _number(
        source.get("refresh_interval_sec"),
        DEFAULT_CONFIG["refresh_interval_sec"],
        REFRESH_MIN_SEC,
        REFRESH_MAX_SEC,
        integer=True,
    )

    source_display = (
        source.get("display") if isinstance(source.get("display"), dict) else {})
    cfg["display"]["daily_markers"] = _bool(
        source_display.get("daily_markers"),
        DEFAULT_CONFIG["display"]["daily_markers"],
    )

    source_window = source.get("window") if isinstance(source.get("window"), dict) else {}
    cfg["window"]["width"] = _number(
        source_window.get("width"),
        DEFAULT_CONFIG["window"]["width"],
        200,
        800,
        integer=True,
    )
    cfg["window"]["height"] = _number(
        source_window.get("height"),
        DEFAULT_CONFIG["window"]["height"],
        300,
        1200,
        integer=True,
    )
    for key in ("x", "y"):
        raw = source_window.get(key)
        cfg["window"][key] = None if raw is None else _number(raw, None, integer=True)
    cfg["window"]["on_top"] = _bool(
        source_window.get("on_top"), DEFAULT_CONFIG["window"]["on_top"])

    source_alert = (
        source.get("reset_alert") if isinstance(source.get("reset_alert"), dict) else {})
    cfg["reset_alert"]["enabled"] = _bool(
        source_alert.get("enabled"), DEFAULT_CONFIG["reset_alert"]["enabled"])
    cfg["reset_alert"]["pct_jump_threshold"] = _number(
        source_alert.get("pct_jump_threshold"),
        DEFAULT_CONFIG["reset_alert"]["pct_jump_threshold"],
        0,
        100,
    )
    cfg["reset_alert"]["resets_at_advance_sec"] = _number(
        source_alert.get("resets_at_advance_sec"),
        DEFAULT_CONFIG["reset_alert"]["resets_at_advance_sec"],
        1,
        604800,
    )

    source_openrouter = (
        source.get("openrouter") if isinstance(source.get("openrouter"), dict) else {})
    api_key = source_openrouter.get("api_key")
    cfg["openrouter"]["api_key"] = api_key if isinstance(api_key, str) else ""
    return cfg


def load_config():
    """Load config safely, retaining a corrupt file for explicit recovery."""
    if not os.path.exists(CONFIG_PATH):
        _set_health("missing")
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("configuration root must be an object")
    except Exception as exc:
        _log_failure("config load", exc)
        _set_health(
            "corrupt",
            error_info("config_corrupt", detail=exc),
            recovery_required=True,
        )
        return copy.deepcopy(DEFAULT_CONFIG)
    _set_health("ok")
    return normalize_config(user)


def _backup_corrupt_config():
    """Copy the recoverable corrupt file before an explicit Settings save."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = "%s.corrupt-%s.bak" % (CONFIG_PATH, stamp)
    try:
        shutil.copy2(CONFIG_PATH, backup_path)
    except Exception as exc:
        _log_failure("config recovery backup", exc)
        return None, exc
    return backup_path, None


def save_config(cfg, allow_recovery=False):
    """Atomically persist config and return True only after replacement succeeds."""
    payload = normalize_config(cfg)
    with _CONFIG_LOCK:
        recovering = CONFIG_HEALTH.get("recovery_required", False)
        if recovering and not allow_recovery:
            exc = RuntimeError("corrupt configuration requires explicit recovery")
            _log_failure("config write blocked", exc)
            return False

        backup_path = None
        if recovering and os.path.exists(CONFIG_PATH):
            backup_path, backup_error = _backup_corrupt_config()
            if backup_error:
                _set_health(
                    "backup_failed",
                    error_info("config_backup_failed", detail=backup_error),
                    recovery_required=True,
                )
                return False

        try:
            resetwatch.atomic_write_json(CONFIG_PATH, payload)
        except Exception as exc:
            _log_failure("config write", exc)
            _set_health(
                "write_failed",
                error_info("config_write_failed", detail=exc),
                recovery_required=recovering,
                backup_path=backup_path,
            )
            return False

        _set_health(
            "recovered" if recovering else "ok", backup_path=backup_path)
        return True


def commit_config(candidate, allow_recovery=False):
    """Persist a candidate config and adopt it as CFG only if the write stuck.

    Every writer -- window geometry, the pin toggle, the settings form -- must
    leave CFG matching what is on disk, so none of them may adopt a candidate
    that save_config() rejected.
    """
    global CFG
    if not save_config(candidate, allow_recovery=allow_recovery):
        return False
    CFG = normalize_config(candidate)
    return True


USER_AGENT = "ai-usage-widget/1.0"


def bearer_headers(token, **extra):
    """Build the request headers every provider fetch shares."""
    headers = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    headers.update(extra)
    return headers


def http_get_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw)


# How every provider presents itself. provider_result(), the failure path in
# refresh_all(), the toast, the alert window, and the tray tooltip all read
# from this, so a rename lands everywhere at once. Fetcher selection is NOT
# driven from here -- see the note in refresh_all().
# "tray" is separate from "name" because the tooltip has 127 characters for
# every provider combined and cannot afford the full titles.
# "has_token" marks the providers backed by an expiring CLI credential, which
# is what drives TOKEN_PROVIDERS and the token badge.
PROVIDER_INFO = {
    "claude": {"name": "Claude Code", "kind": "windows", "tray": "Claude",
               "has_token": True},
    "codex": {"name": "Codex CLI", "kind": "windows", "tray": "Codex",
              "has_token": True},
    "openrouter": {"name": "OpenRouter", "kind": "balance",
                   "tray": "OpenRouter", "has_token": False},
}


def provider_result(provider_id):
    """Build the empty payload every fetcher and every failure path returns."""
    info = PROVIDER_INFO[provider_id]
    return {"id": provider_id, "name": info["name"], "kind": info["kind"],
            "ok": False, "windows": [], "meta": {}, "error": None}


def request_provider_json(url, headers, service, auth_code):
    """GET provider JSON, mapping any failure to a localizable error payload.

    Returns ``(data, None)`` on success or ``(None, error_info)`` on failure.
    A 401/403 gets the provider's own auth code so the UI knows to offer the
    re-login button; everything else falls back to the shared HTTP codes.
    """
    try:
        return http_get_json(url, headers), None
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, error_info(auth_code, params={"status": exc.code})
        return None, error_info(
            "api_http_error", params={"service": service, "status": exc.code})
    except Exception as exc:
        return None, error_info(
            "api_request_failed", params={"service": service}, detail=exc)


def finalize_provider(result, data, service, empty_code):
    """Mark a windowed provider healthy, or explain why it produced nothing.

    The raw key list is what makes an unrecognized API shape diagnosable from
    the card alone, so both windowed fetchers end the same way.
    """
    if result["windows"]:
        result["ok"] = True
    else:
        result["error"] = error_info(empty_code, params={"service": service})
        result["meta"]["raw_keys"] = list(data.keys())[:12]
    return result


def iso_to_epoch(value):
    """Convert an ISO string, Unix number, or None to epoch seconds or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        epoch = float(value)
    elif isinstance(value, str):
        s = value.strip()
        try:
            epoch = float(s)
        except ValueError:
            try:
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                epoch = dt.timestamp()
            except Exception:
                return None
    else:
        return None
    return epoch / 1000.0 if epoch > 4e10 else epoch


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def tooltip_window(p):
    """Choose the tray-tooltip window: weekly, then session, then the first.

    Prefer the weekly window so the tooltip describes the same limit as the
    reset alert. Codex Pro has no session window at all, only a weekly one.
    This used to prefer the session, which put Claude's five-hour limit beside
    Codex's weekly limit."""
    ws = p.get("windows") or []
    return (next((x for x in ws if x["id"] == "week"), None)
            or next((x for x in ws if x["id"] == "session"), None)
            or (ws[0] if ws else None))


def make_window(win_id, label, used_pct=None, resets_at=None, extra=None):
    """Build a normalized quota window.

    Percentage windows only. Dollar figures reach the UI through
    ``meta.balance`` instead, because OpenRouter reports a single account
    balance rather than a windowed quota.
    """
    if used_pct is not None:
        used_pct = max(0.0, min(100.0, float(used_pct)))
    return {
        "id": win_id,
        "label": label,
        "used_pct": used_pct,
        "remaining_pct": None if used_pct is None else round(100.0 - used_pct, 2),
        "resets_at": resets_at,          # Epoch seconds or None.
        "extra": extra or {},
    }


# ----------------------------------------------------------------------------
# Claude Code
# ----------------------------------------------------------------------------

CLAUDE_CRED_PATHS = [
    os.path.join(HOME, ".claude", ".credentials.json"),
    os.path.join(HOME, ".config", "claude", ".credentials.json"),
]
CLAUDE_USAGE_URLS = [
    "https://api.anthropic.com/api/oauth/usage",
    "https://claude.ai/api/oauth/usage",
]


def read_claude_credentials(paths=None):
    """Try every Claude credential candidate until one has a usable token."""
    read_errors = []
    found_file = False
    for path in paths or CLAUDE_CRED_PATHS:
        if not os.path.exists(path):
            continue
        found_file = True
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            oauth = data.get("claudeAiOauth") or data.get("oauth") or {}
            token = oauth.get("accessToken") or oauth.get("access_token")
            if not token:
                continue
            return {
                "token": token,
                "subscription": oauth.get("subscriptionType"),
                "expires_at": iso_to_epoch(
                    oauth.get("expiresAt") or oauth.get("expires_at")),
            }, None
        except Exception as exc:
            read_errors.append((path, exc))
    if read_errors:
        path, exc = read_errors[-1]
        return None, error_info(
            "claude_credentials_read_failed",
            params={"path": path},
            detail=exc,
        )
    if found_file:
        return None, error_info("claude_token_missing")
    return None, error_info("claude_credentials_missing")


def fetch_claude():
    result = provider_result("claude")
    credentials, credential_error = read_claude_credentials()
    if not credentials:
        result["error"] = credential_error
        return result
    token = credentials["token"]
    result["meta"]["subscription"] = credentials.get("subscription")
    result["meta"]["token_expires_at"] = credentials.get("expires_at")
    if credentials.get("expires_at") and credentials["expires_at"] < time.time():
        result["meta"]["token_stale"] = True

    headers = bearer_headers(
        token,
        **{"anthropic-beta": "oauth-2025-04-20",
           "Content-Type": "application/json"})
    data, last_err = None, None
    for url in CLAUDE_USAGE_URLS:
        data, last_err = request_provider_json(
            url, headers, "Claude", "claude_auth_expired")
        if data is not None:
            break
    if data is None:
        result["error"] = last_err or error_info(
            "api_no_response", params={"service": "Claude"})
        return result

    label_map = {
        "five_hour": ("session", "session"),
        "seven_day": ("week", "week"),
        "seven_day_sonnet": ("week_sonnet", "weekSonnet"),
        "seven_day_opus": ("week_opus", "weekOpus"),
        "seven_day_oauth_apps": ("week_apps", "weekApps"),
    }
    for key, (wid, label) in label_map.items():
        obj = data.get(key)
        if not isinstance(obj, dict):
            continue
        pct = pick(obj, "utilization", "used_percent", "usage_percent")
        resets = iso_to_epoch(pick(obj, "resets_at", "reset_at", "resetsAt"))
        if pct is not None or resets is not None:
            result["windows"].append(make_window(wid, label, used_pct=pct, resets_at=resets))

    # Extra usage / credits, when returned by the server.
    extra = data.get("extra_usage") or data.get("extraUsage")
    if isinstance(extra, dict):
        result["meta"]["extra_usage"] = extra

    return finalize_provider(result, data, "Claude", "api_format_unrecognized")


# ----------------------------------------------------------------------------
# Codex CLI (ChatGPT)
# ----------------------------------------------------------------------------

def _codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")


def _jwt_claims(jwt):
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return {}


def read_codex_credentials(auth_path=None):
    """Read Codex auth once and return tokens plus secret-free metadata."""
    path = auth_path or os.path.join(_codex_home(), "auth.json")
    if not os.path.exists(path):
        return None, error_info("codex_credentials_missing")
    try:
        with open(path, "r", encoding="utf-8") as f:
            auth = json.load(f)
    except Exception as exc:
        return None, error_info("codex_credentials_read_failed", detail=exc)

    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token") or auth.get("access_token")
    if not access:
        return None, error_info("codex_token_missing")
    account_id = tokens.get("account_id") or auth.get("account_id")
    # Decoded once and reused below: the access token was previously parsed
    # here and again for its expiry, on every poll.
    access_claims = _jwt_claims(access)
    plan = None
    if not account_id:
        for token, claims in ((tokens.get("id_token"), None),
                              (access, access_claims)):
            if not token:
                continue
            if claims is None:
                claims = _jwt_claims(token)
            account = claims.get("https://api.openai.com/auth") or {}
            account_id = (
                account.get("chatgpt_account_id") or account.get("account_id"))
            plan = account.get("chatgpt_plan_type") or plan
            if account_id:
                break
    return {
        "token": access,
        "account_id": account_id,
        "plan": plan,
        "expires_at": iso_to_epoch(access_claims.get("exp")),
    }, None


def _resolve_resets(obj):
    """Return ``(resets_at, derived)`` for one rate-limit object.

    Some responses carry only a relative duration. A timestamp computed from
    now() moves with the system clock, so it is flagged: resetwatch disables
    boundary-movement detection for those and relies on the balance signal
    alone. See resetwatch's module docstring.
    """
    resets = iso_to_epoch(pick(obj, "resets_at", "reset_at", "reset_time"))
    if resets is not None:
        return resets, False
    secs = pick(obj, "resets_in_seconds", "reset_after_seconds")
    if secs is None:
        return None, False
    try:
        return time.time() + float(secs), True
    except (TypeError, ValueError):
        return None, False


def fetch_codex():
    result = provider_result("codex")
    credentials, credential_error = read_codex_credentials()
    if not credentials:
        result["error"] = credential_error
        return result
    access = credentials["token"]
    account_id = credentials.get("account_id")
    if credentials.get("plan"):
        result["meta"]["plan"] = credentials["plan"]
    result["meta"]["token_expires_at"] = credentials.get("expires_at")

    headers = bearer_headers(access, **{"Content-Type": "application/json"})
    if account_id:
        headers["chatgpt-account-id"] = account_id

    data, request_error = request_provider_json(
        "https://chatgpt.com/backend-api/wham/usage", headers,
        "Codex", "codex_auth_expired")
    if not isinstance(data, dict):
        result["error"] = request_error or error_info(
            "api_format_unrecognized", params={"service": "Codex"})
        return result

    if isinstance(data.get("plan_type"), str):
        result["meta"]["plan"] = data["plan_type"]

    rl = data.get("rate_limit") or data.get("rate_limits") or {}

    def add_window(obj, wid, fallback_label):
        if not isinstance(obj, dict):
            return
        pct = pick(obj, "used_percent", "usage_percent", "utilization")
        resets, derived = _resolve_resets(obj)
        # The window duration helps determine the label; it may be in minutes
        # or seconds (limit_window_seconds).
        mins = pick(obj, "window_minutes", "limit_window_minutes")
        if mins is None:
            win_secs = pick(obj, "limit_window_seconds", "window_seconds")
            if win_secs:
                mins = float(win_secs) / 60.0
        label = fallback_label
        if mins:
            mins = float(mins)
            if mins <= 6 * 60:
                label, wid = "session", "session"
            elif mins >= 6.5 * 24 * 60:
                label, wid = "week", "week"
        if pct is not None or resets is not None:
            result["windows"].append(make_window(
                wid, label, used_pct=pct, resets_at=resets,
                extra={"resets_at_derived": True} if derived else None))

    add_window(rl.get("primary_window") or rl.get("primary"), "session", "session")
    add_window(rl.get("secondary_window") or rl.get("secondary"), "week", "week")

    # Additional model-specific limits (for example, Spark).
    for i, item in enumerate(data.get("additional_rate_limits") or []):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("id") or "Extra limit %d" % (i + 1)
        obj = item.get("window") or item.get("rate_limit") or item
        pct = pick(obj, "used_percent", "usage_percent")
        resets, derived = _resolve_resets(obj)
        if pct is not None:
            result["windows"].append(make_window(
                f"extra_{i}", str(title), used_pct=pct, resets_at=resets,
                extra={"resets_at_derived": True} if derived else None))

    credits = data.get("credits")
    if isinstance(credits, dict):
        result["meta"]["credits"] = pick(credits, "balance", "remaining", "amount")

    return finalize_provider(result, data, "Codex", "api_limits_missing")


OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


def _openrouter_key():
    """Read the key from the environment, then config.json; never write it."""
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        return key, "env"
    key = ((CFG.get("openrouter") or {}).get("api_key") or "").strip()
    if key:
        return key, "config.json"
    return None, None


def fetch_openrouter():
    result = provider_result("openrouter")
    key, source = _openrouter_key()
    result["meta"]["key_source"] = source
    if not key:
        result["error"] = error_info("openrouter_key_missing")
        return result

    headers = bearer_headers(key)
    data, request_error = request_provider_json(
        OPENROUTER_CREDITS_URL, headers, "OpenRouter", "openrouter_key_rejected")
    if not isinstance(data, dict):
        result["error"] = request_error or error_info(
            "api_format_unrecognized", params={"service": "OpenRouter"})
        return result

    # A missing field arrives as None, and float(None) raises TypeError, so
    # this one guard covers both absent and nonnumeric values.
    credits = data.get("data") or {}
    try:
        total = float(pick(credits, "total_credits"))
        used = float(pick(credits, "total_usage"))
    except (TypeError, ValueError):
        result["error"] = error_info(
            "api_limits_missing", params={"service": "OpenRouter"})
        return result

    balance = {
        "remaining_usd": round(total - used, 2),
        "total_usd": round(total, 2),
        "used_usd": round(used, 2),
        "week_usd": None,
    }
    # Weekly usage is optional; a failure here must not break the card.
    # Do not collect the key label or mask: the UI does not show them, and
    # key-derived data does not need to travel through snapshots or the WebView.
    try:
        key_data = (http_get_json(OPENROUTER_KEY_URL, headers) or {}).get("data") or {}
        weekly = key_data.get("usage_weekly")
        if weekly is not None:
            balance["week_usd"] = round(float(weekly), 2)
    except Exception:
        pass

    result["meta"]["balance"] = balance
    result["ok"] = True
    return result


# ----------------------------------------------------------------------------
# Data collection + JS API
# ----------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.refresh_wake_event = threading.Event()
        self.snapshot = {"updated_at": None, "providers": {}}
        self.main_window = None


STATE = State()
CFG = load_config()
# Set True only once the startup window.resize() workaround (see main()) confirms
# success. Guards against persisting a chrome-shrunken size on exit if that resize
# ever fails -- see persist_window_geometry().
_INITIAL_SIZE_OK = False

# Use DATA_DIR, not APP_DIR: alert state must survive process exit even in a
# one-file build; see the comment where DATA_DIR is defined.
ALERT_STATE_PATH = os.path.join(DATA_DIR, "reset-alert-state.json")
# Pass the log path explicitly: AlertStore derives its own default beside the
# state file, which lands on the same file only because both sit in DATA_DIR.
# Naming ERROR_LOG_PATH here keeps the two from drifting apart silently.
ALERTS = resetwatch.AlertStore(
    ALERT_STATE_PATH, log_path=ERROR_LOG_PATH).load()
# Protect ALERTS (seen/pending) and its save() calls from races between the
# polling thread (process_reset_alerts) and the GUI thread (AlertApi). Without
# this lock, a GUI dismiss() could be overwritten by polling-thread add()/save()
# calls that read the state BEFORE dismissal, causing the alert to reappear
# immediately after the user closed it.
ALERTS_LOCK = threading.Lock()
_FIRST_COMPARE = True


# Save geometry exactly once during the process lifetime. Both exit paths (the
# X button and the tray's Exit command) ultimately return from webview.start()
# to main(). Without this flag, the same exit would write the config twice,
# with the second write reading an already-destroyed window.
_GEOMETRY_LOCK = threading.Lock()
_GEOMETRY_SAVED = False


def persist_window_geometry(window):
    """Persist the main window's position and size; safe to call repeatedly."""
    global _GEOMETRY_SAVED
    with _GEOMETRY_LOCK:
        if _GEOMETRY_SAVED:
            return True
        try:
            candidate = copy.deepcopy(CFG)
            w = candidate["window"]
            w["x"], w["y"] = window.x, window.y
            # Only trust width/height if the startup chrome-shrink workaround
            # confirmed success -- otherwise a failed resize would compound
            # into a smaller window on every future launch.
            if _INITIAL_SIZE_OK:
                w["width"], w["height"] = window.width, window.height
        except Exception as exc:
            _log_failure("window geometry read", exc)
            return False
        if not commit_config(candidate):
            return False
        _GEOMETRY_SAVED = True
        return True


def shutdown_app():
    """Handle both exit paths: the X button and the tray's Exit command.

    The window MUST be destroyed. While it remains alive, webview.start() in
    main() will not return. Stopping the tray first would leave the process
    hanging with a frozen always-on-top widget, no tray icon, and no polling
    loop; it could then be terminated only through Task Manager.
    """
    STATE.shutdown_event.set()
    STATE.refresh_wake_event.set()
    win = STATE.main_window
    try:
        if win is None:
            win = webview.windows[0]
        persist_window_geometry(win)
        win.destroy()
    except Exception:
        # Geometry is either already saved by the call above or unreachable
        # because there is no window, so there is nothing left to persist --
        # drop the tray and hard-exit rather than hang with no way out.
        TRAY.stop()
        os._exit(0)


class TrayManager:
    """Manage one static tray icon instead of one icon per provider.

    Previously, each provider got an icon with percentages drawn over an empty
    square. Multiple icons accumulated in the tray and disappeared whenever a
    provider returned an error. There is now one static app-logo icon that
    lives from startup to exit, with the figures shown in its tooltip.
    """

    # Windows stores the tooltip in szTip: 128 wchar values including the null.
    TOOLTIP_MAX = 127

    def __init__(self):
        self.icon = None
        self.window_ref = None
        self._thread = None

    def _load_icon_image(self):
        path = os.path.join(APP_DIR, "icon", "512.png")
        try:
            with Image.open(path) as src:
                return src.convert("RGBA").resize((64, 64), Image.LANCZOS)
        except Exception:
            # The tray must still start if the icon file is unavailable.
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse((4, 4, 59, 59), outline="#1E90FF", width=6)
            return img

    @staticmethod
    def _tooltip_line(info, provider):
        """Format one tooltip row according to the provider's card kind.

        Routing on "kind" is what keeps the tray in step with PROVIDER_INFO: a
        new provider gets a row without anyone remembering to add one here.
        """
        label = info["tray"]
        healthy = bool(provider and provider.get("ok"))

        # A balance is not a quota window: no percentage and no reset.
        if info["kind"] == "balance":
            meta = (provider.get("meta") or {}) if healthy else {}
            remaining = (meta.get("balance") or {}).get("remaining_usd")
            if remaining is None:
                return "%s: —" % label
            return "%s: $%.2f" % (label, remaining)

        w = tooltip_window(provider) if healthy else None
        if not w or w.get("remaining_pct") is None:
            return "%s: —" % label
        # Use %g rather than %s: round(18.0, 1) prints "18.0", while the
        # window shows "18"; the tooltip should match the card.
        pct = "%g" % round(w["remaining_pct"], 1)
        resets = w.get("resets_at")
        if not resets:
            return "%s: %s%%" % (label, pct)
        secs = max(0, int(resets - time.time()))
        h, rem = divmod(secs, 3600)
        m = rem // 60
        mu = native_text("unit_minute")
        reset_str = ("%d%s %d%s" % (h, native_text("unit_hour"), m, mu)
                     if h > 0 else "%d%s" % (m, mu))
        return "%s: %s%% (%s %s)" % (
            label, pct, native_text("tooltip_resets_in"), reset_str)

    def _build_tooltip(self):
        with STATE.lock:
            snap = copy.deepcopy(STATE.snapshot)
        if not snap.get("updated_at"):
            return "AI Usage Widget"
        lines = ["AI Usage Widget"]
        for pid, info in PROVIDER_INFO.items():
            lines.append(self._tooltip_line(info, snap["providers"].get(pid)))
        return "\n".join(lines)[:self.TOOLTIP_MAX]

    def start(self, window):
        if not TRAY_AVAILABLE:
            return
        self.window_ref = window
        if self.icon is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem(native_text("tray_show"), self._on_show, default=True),
            pystray.MenuItem(native_text("tray_refresh"), self._on_refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(native_text("tray_exit"), self._on_quit),
        )
        self.icon = pystray.Icon(
            "ai-usage", self._load_icon_image(), self._build_tooltip(), menu)
        self._thread = threading.Thread(target=self.icon.run, daemon=True)
        self._thread.start()

    def update_tooltip(self):
        # The tooltip is cosmetic and must not break data polling. The tray
        # previously stayed silent for exactly this reason when _build_tooltip
        # did not exist.
        icon = self.icon
        if icon is None:
            return
        try:
            icon.title = self._build_tooltip()
        except Exception:
            pass

    def stop(self):
        icon = self.icon
        if icon is None:
            return
        self.icon = None
        try:
            icon.stop()
        except Exception:
            pass

    def _on_show(self, icon, item):
        if self.window_ref:
            self.window_ref.show()

    def _on_quit(self, icon, item):
        # Follow the same path as the X button (JsApi.close): save geometry and
        # destroy the window. main() stops the tray icon after webview.start()
        # returns; stopping it here would hide the tray before the window closes.
        shutdown_app()

    def _on_refresh(self, icon, item):
        threading.Thread(target=refresh_all, daemon=True).start()

    def hide_window(self):
        if self.window_ref:
            self.window_ref.hide()


TRAY = TrayManager()


class AlertApi:
    """Expose the alert window API.

    Both dismiss methods call close_if_empty() only AFTER releasing
    ALERTS_LOCK. close_if_empty acquires the window's own lock, and there is no
    need to impose a shared nesting order on every caller when the two locks
    can simply never be held together.
    """

    def get_language(self):
        return current_language()

    def get_alerts(self):
        with ALERTS_LOCK:
            alerts = copy.deepcopy(ALERTS.pending)
        # Resolve display names here rather than in alert.html: PROVIDER_INFO
        # is the only place provider titles live, and a stored event must not
        # freeze a name that a later release renames.
        for alert in alerts:
            info = PROVIDER_INFO.get(alert.get("provider")) or {}
            alert["provider_name"] = info.get("name", alert.get("provider"))
        return alerts

    def dismiss_alert(self, alert_id):
        with ALERTS_LOCK:
            ALERTS.dismiss(alert_id)
            ALERTS.save()
        ALERT_WINDOW.close_if_empty()
        return True

    def dismiss_all(self):
        with ALERTS_LOCK:
            ALERTS.dismiss_all()
            ALERTS.save()
        ALERT_WINDOW.close_if_empty()
        return True


class AlertWindowManager:
    """Manage one non-focus-stealing window for all alerts."""

    WIDTH = 340
    MARGIN = 16

    def __init__(self):
        self.window = None
        self.lock = threading.Lock()

    def _corner(self, height):
        """Return the work area's bottom-right corner, allowing for the taskbar."""
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
        # Snapshot under ALERTS_LOCK and release it BEFORE acquiring self.lock,
        # so the two locks are never nested. If the GUI thread calls
        # dismiss_all() after process_reset_alerts() sees pending alerts but
        # before this call, detect that here and avoid creating an empty alert
        # window with no close button.
        with ALERTS_LOCK:
            pending_count = len(ALERTS.pending)
        if pending_count == 0:
            return
        with self.lock:
            if self.window is not None:
                try:
                    self.window.evaluate_js(
                        "window.renderAlerts && window.renderAlerts()")
                    return
                except Exception:
                    # The failure may be temporary rather than "window is gone."
                    # Because the reference is about to be discarded, destroy
                    # the window here; an unreferenced, frameless, always-on-top
                    # window could not otherwise be closed.
                    self._destroy_locked()
            # 90 = alert.html's .head + .wrap padding, 130 = one .row (padding,
            # three text lines, the optional .away badge, the dismiss button).
            # Re-measure both if those rules in alert.html change.
            height = min(460, 90 + 130 * pending_count)
            x, y = self._corner(height)
            # Record the current count in case create_window registers a window
            # and then fails. Any orphaned window must be destroyed, not merely
            # forgotten.
            marker = len(webview.windows)
            try:
                self.window = webview.create_window(
                    native_text("alert_title"),
                    url=os.path.join(APP_DIR, "alert.html"),
                    js_api=AlertApi(),
                    width=self.WIDTH, height=height, x=x, y=y,
                    frameless=True, easy_drag=True, resizable=False,
                    on_top=True, focus=False,
                    background_color="#101012",
                )
            except Exception:
                self.window = None
                for orphan in list(webview.windows[marker:]):
                    try:
                        orphan.destroy()
                    except Exception:
                        pass

    def _destroy_locked(self):
        """Destroy the window and clear its reference; call only under self.lock."""
        if self.window is None:
            return
        try:
            self.window.destroy()
        except Exception:
            pass
        self.window = None

    def close_if_empty(self):
        with self.lock:
            if not ALERTS.pending:
                self._destroy_locked()

    def toast(self, events):
        """Supplement the window with a system toast, which disappears itself."""
        if not events or not TRAY_AVAILABLE:
            return
        icon = TRAY.icon
        if icon is None:
            return
        if len(events) == 1:
            provider_id = events[0]["provider"]
            info = PROVIDER_INFO.get(provider_id) or {}
            body = native_text(
                "toast_one", provider=info.get("name", provider_id))
        else:
            body = native_text("toast_many", count=len(events))
        try:
            icon.notify(body, "AI Usage Widget")
        except Exception:
            pass


ALERT_WINDOW = AlertWindowManager()


def process_reset_alerts(providers):
    """Compare a fresh snapshot with the baseline and accumulate alerts."""
    global _FIRST_COMPARE
    cfg = CFG.get("reset_alert") or {}
    new_readings = resetwatch.readings(providers)

    if not cfg.get("enabled", True):
        # Always update the baseline so re-enabling alerts does not produce a
        # batch of stale resets.
        with ALERTS_LOCK:
            ALERTS.merge_seen(new_readings)
            if ALERTS.pending:
                ALERTS.dismiss_all()
            ALERTS.save()
        _FIRST_COMPARE = False
        # ALERTS_LOCK is already released. close_if_empty acquires the window's
        # own lock, so these locks are never nested in either order. Disabling
        # alerts must also remove those already on screen; otherwise the window
        # would retain entries that no longer exist.
        ALERT_WINDOW.close_if_empty()
        return []

    # Read, modify, and write ALERTS as one operation under a single lock so
    # the GUI thread (dismiss_alert/dismiss_all) cannot run between
    # detect_resets() and save() and lose an alert dismissal.
    with ALERTS_LOCK:
        events = resetwatch.detect_resets(
            ALERTS.seen, new_readings, cfg, while_away=_FIRST_COMPARE)
        added = ALERTS.add(events)
        ALERTS.merge_seen(new_readings)
        ALERTS.save()
        has_pending = bool(ALERTS.pending)
    _FIRST_COMPARE = False

    # ALERTS_LOCK is already released. toast()/raise_alert() acquire the window
    # through ALERT_WINDOW.lock, so the locks are never nested.
    if added:
        ALERT_WINDOW.toast(added)
    if has_pending:
        ALERT_WINDOW.raise_alert()
    return added


def refresh_all():
    if not STATE.refresh_lock.acquire(blocking=False):
        return
    try:
        providers = {}
        # Listed rather than read off PROVIDER_INFO: holding the function
        # objects in that table would freeze a second binding beside each
        # module-level name, so reassigning a fetcher would stop taking effect.
        with ThreadPoolExecutor(max_workers=len(PROVIDER_INFO)) as executor:
            futures = {
                executor.submit(fetch_claude): "claude",
                executor.submit(fetch_codex): "codex",
                executor.submit(fetch_openrouter): "openrouter",
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    providers[name] = future.result()
                except Exception as exc:
                    _log_failure("%s refresh" % name, exc)
                    failed = provider_result(name)
                    failed["error"] = error_info("internal_error")
                    providers[name] = failed
        with STATE.lock:
            STATE.snapshot = {"updated_at": time.time(), "providers": providers}
        try:
            process_reset_alerts(providers)
        except Exception as exc:
            # The widget runs under pythonw with no console, so an unlogged
            # failure here is indistinguishable from "no resets happened".
            _log_failure("reset alerts", exc)
        TRAY.update_tooltip()
    finally:
        STATE.refresh_lock.release()


def poll_delay(cfg=None):
    """Return a guarded polling delay for loaded or hand-edited config."""
    source = CFG if cfg is None else cfg
    default = DEFAULT_CONFIG["refresh_interval_sec"]
    return _number(
        source.get("refresh_interval_sec", default),
        default,
        REFRESH_MIN_SEC,
        REFRESH_MAX_SEC,
        integer=True,
    )


def refresh_loop():
    while not STATE.shutdown_event.is_set():
        # Consume the wake that started this iteration before doing work. Any
        # interval change that happens during refresh_all() then remains set
        # and skips the old delay instead of being cleared accidentally.
        STATE.refresh_wake_event.clear()
        try:
            refresh_all()
        except Exception as exc:
            _log_failure("refresh loop", exc)
        # poll_delay() clamps and falls back on its own, so it cannot raise.
        delay = poll_delay()
        if STATE.shutdown_event.is_set():
            break
        STATE.refresh_wake_event.wait(timeout=delay)


# The only paths the settings form may write. config_for_ui() filters what
# leaves for the WebView; this is the symmetric filter on the way back in, so
# a hand-edited or replayed payload cannot reach a key the form never offered
# -- the OpenRouter secret above all, where writing "***" over a real key
# would be unrecoverable.
UI_WRITABLE_FIELDS = (
    ("language",),
    ("refresh_interval_sec",),
    ("window", "width"),
    ("window", "height"),
    ("window", "on_top"),
    ("display", "daily_markers"),
    ("reset_alert", "enabled"),
)


def apply_ui_config(candidate, update):
    """Copy only the allowlisted settings fields from a WebView payload."""
    for path in UI_WRITABLE_FIELDS:
        source = update
        for key in path[:-1]:
            source = source.get(key) if isinstance(source, dict) else None
        if not isinstance(source, dict) or path[-1] not in source:
            continue
        target = candidate
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = source[path[-1]]
    return candidate


def config_for_ui():
    """Return the secret-free config subset sent to the WebView.

    A user may put the OpenRouter key in config.json, and this payload crosses
    into the JavaScript context on every poll. Nothing in ui.html reads the
    openrouter section -- the settings form does not include it, and the
    connector row reads meta.key_source off the provider snapshot instead -- so
    the whole section is dropped rather than masked. Masking would still tell
    the page whether a key exists.
    """
    safe = copy.deepcopy(CFG)
    safe.pop("openrouter", None)
    return safe


def config_health_for_ui():
    """Return config persistence health without exposing local file contents."""
    health = copy.deepcopy(CONFIG_HEALTH)
    if health.get("backup_path"):
        health["backup_path"] = os.path.basename(health["backup_path"])
    return health


TOKEN_PROVIDERS = tuple(
    pid for pid, info in PROVIDER_INFO.items() if info["has_token"])


def token_status_from_snapshot(providers, current_time=None):
    """Derive display status from cached, secret-free expiry metadata.

    ``meta.token_expires_at`` is written by the credential readers, which
    already return epoch seconds or None, so no conversion happens here.
    """
    now_value = time.time() if current_time is None else current_time
    result = dict.fromkeys(TOKEN_PROVIDERS)
    for provider_id in TOKEN_PROVIDERS:
        provider = (providers or {}).get(provider_id) or {}
        expiry = (provider.get("meta") or {}).get("token_expires_at")
        if expiry is None:
            continue
        remaining = expiry - now_value
        if remaining <= 0:
            result[provider_id] = {"status": "expired", "remaining": 0}
        elif remaining < 3600:
            result[provider_id] = {
                "status": "expiring", "remaining": remaining}
        else:
            result[provider_id] = {"status": "valid", "remaining": remaining}
    return result


def resolve_cli_command(name, arguments):
    """Resolve a CLI and route Windows command wrappers through COMSPEC."""
    executable = shutil.which(name)
    if not executable:
        return None, error_info("cli_not_found", params={"cli": name})
    suffix = os.path.splitext(executable)[1].lower()
    if suffix in (".cmd", ".bat"):
        command_shell = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not command_shell:
            return None, error_info("cli_shell_missing")
        return [
            command_shell, "/d", "/s", "/c", executable, *arguments
        ], None
    return [executable, *arguments], None


def launch_cli_login(name, arguments):
    """Launch a CLI login and detect commands that fail immediately."""
    command, resolve_error = resolve_cli_command(name, arguments)
    if not command:
        return {"success": False, "error": resolve_error}
    try:
        proc = subprocess.Popen(
            command,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        try:
            exit_code = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return {"success": True, "value": "login_started"}
        if exit_code != 0:
            return {
                "success": False,
                "error": error_info(
                    "cli_early_exit",
                    params={"cli": name, "exit_code": exit_code},
                ),
                "exit_code": exit_code,
            }
        return {
            "success": True,
            "value": "login_completed",
            "exit_code": exit_code,
        }
    except FileNotFoundError:
        return {
            "success": False,
            "error": error_info("cli_not_found", params={"cli": name}),
        }
    except Exception as exc:
        return {
            "success": False,
            "error": error_info(
                "cli_launch_failed", params={"cli": name}, detail=exc),
        }


class JsApi:
    def get_data(self):
        with STATE.lock:
            snap = copy.deepcopy(STATE.snapshot)
        snap["now"] = time.time()
        snap["refresh_interval_sec"] = CFG["refresh_interval_sec"]
        snap["token_status"] = token_status_from_snapshot(
            snap.get("providers"), snap["now"])
        snap["on_top"] = CFG["window"]["on_top"]
        snap["_config"] = config_for_ui()
        snap["config_health"] = config_health_for_ui()
        with ALERTS_LOCK:
            snap["state_write_failed"] = not ALERTS.last_save_ok
        return snap

    def refresh_now(self):
        if STATE.refresh_lock.locked():
            return False
        threading.Thread(target=refresh_all, daemon=True).start()
        return True

    def login_claude(self):
        return launch_cli_login("claude", ["auth", "login"])

    def login_codex(self):
        return launch_cli_login("codex", ["login"])

    def toggle_on_top(self):
        new_val = not CFG["window"]["on_top"]
        candidate = copy.deepcopy(CFG)
        candidate["window"]["on_top"] = new_val
        if not commit_config(candidate):
            return {
                "ok": False,
                "value": CFG["window"]["on_top"],
                "error": CONFIG_HEALTH.get("error") or error_info(
                    "config_write_failed"),
            }
        try:
            win = STATE.main_window or webview.windows[0]
            win.on_top = new_val
        except Exception as exc:
            return {
                "ok": False,
                "value": new_val,
                "error": error_info("window_update_failed", detail=exc),
            }
        return {"ok": True, "value": new_val}

    def save_config_api(self, cfg):
        if not isinstance(cfg, dict):
            return {"ok": False, "error": error_info("config_invalid")}
        try:
            old_lang = CFG["language"]
            candidate = apply_ui_config(copy.deepcopy(CFG), cfg)
            if not commit_config(candidate, allow_recovery=True):
                return {
                    "ok": False,
                    "error": CONFIG_HEALTH.get("error") or error_info(
                        "config_write_failed"),
                }
            STATE.refresh_wake_event.set()
            try:
                win = STATE.main_window or webview.windows[0]
                w = CFG["window"]
                win.on_top = w["on_top"]
                win.resize(w["width"], w["height"])
            except Exception as exc:
                _log_failure("window settings apply", exc)
            # The tray menu and tooltip are built once per language.
            if CFG["language"] != old_lang:
                TRAY.update_tooltip()
            return {
                "ok": True,
                "config": config_for_ui(),
                "config_health": config_health_for_ui(),
            }
        except Exception as exc:
            _log_failure("config API save", exc)
            return {
                "ok": False,
                "error": error_info("config_write_failed", detail=exc),
            }

    def close(self):
        shutdown_app()

    def minimize_to_tray(self):
        # window_ref is only ever set by TrayManager.start(), which returns
        # early when the tray is unavailable -- so it implies TRAY_AVAILABLE.
        if TRAY.window_ref:
            TRAY.hide_window()
            return True
        return False

    def update_tray_icon(self):
        # The icon itself is static; only its tooltip needs updating. Keep this
        # method name because ui.html calls it on every poll.
        if TRAY.window_ref:
            TRAY.update_tooltip()
            return True
        return False


def main():
    global webview
    try:
        import webview  # pywebview
    except ImportError:
        print("pywebview is not installed. Run install.bat first.")
        sys.exit(1)

    # normalize_config guarantees every key here, so index directly rather than
    # restating defaults that would silently diverge from DEFAULT_CONFIG.
    w = CFG["window"]
    window = webview.create_window(
        "AI Usage",
        url=os.path.join(APP_DIR, "ui.html"),
        js_api=JsApi(),
        width=w["width"],
        height=w["height"],
        x=w["x"],
        y=w["y"],
        frameless=True,
        easy_drag=False,
        on_top=w["on_top"],
        resizable=True,
        background_color="#101012",
    )
    STATE.main_window = window
    # pywebview 6.2.1 winforms backend bug: create_window() sets the Form's
    # outer Size *before* switching FormBorderStyle to None for frameless
    # windows. .NET preserves ClientSize across that border-style change, so
    # the outer size silently shrinks by the (soon-removed) caption/border
    # chrome (~16px width, ~39px height at 96 DPI) -- the window launches
    # smaller than requested. window.resize() runs after the frameless style
    # is already applied and is unaffected, so re-asserting the size corrects
    # it -- but resize() is @_shown_call-decorated and blocks on the window's
    # "shown" event (only set once webview.start()'s message loop actually
    # shows the window), so it must run off the main thread, started before
    # webview.start() is called, not inline here.
    def _fix_initial_size():
        global _INITIAL_SIZE_OK
        try:
            window.resize(w["width"], w["height"])
            _INITIAL_SIZE_OK = True
        except Exception:
            pass

    threading.Thread(target=_fix_initial_size, daemon=True).start()
    # Set the window icon through ctypes.
    icon_path = os.path.join(APP_DIR, "icon", "app.ico")
    if os.path.exists(icon_path):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ai.usage.widget")
            hwnd = window.native
            if hwnd:
                icon = ctypes.windll.user32.LoadImageW(None, icon_path, 1, 0, 0, 0x00000010 | 0x00000020)
                if icon:
                    ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, icon)  # WM_SETICON
        except Exception:
            pass
    if TRAY_AVAILABLE:
        TRAY.start(window)
    # Start the polling thread only NOW, after the main window exists. Otherwise
    # the first process_reset_alerts() could call raise_alert() before the main
    # window is created, making webview.windows[0] the alert window. JsApi.close,
    # minimize_to_tray, and toggle_on_top all assume windows[0] is the main window.
    threading.Thread(target=refresh_loop, daemon=True).start()
    webview.start(debug=False)
    STATE.shutdown_event.set()
    STATE.refresh_wake_event.set()
    TRAY.stop()
    # If this was not an exit through X or the tray (for example, an external
    # close), geometry has not yet been saved, so save it here. Otherwise no-op.
    persist_window_geometry(window)


if __name__ == "__main__":
    main()
