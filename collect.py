#!/usr/bin/env python3
# omarchy:args=[--force] [--api-key-file <path>]
"""Print or write the Ollama Cloud usage record as JSON.

Usage comes from the official API-key endpoint:

    GET https://ollama.com/api/usage
    Authorization: Bearer <personal API key>

It returns account limits (session + weekly usage as 0..1 fractions with
per-model request counts) and 4-week activity. No browser, cookie store, or
keyring is involved; the API key comes from user-owned files and is injected
into the request header by urllib — it is never part of an argv (world-
readably exposed via /proc/<pid>/cmdline) and is never logged or echoed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request

AGENT_ID = "ollama"
AGENT_NAME = "Ollama Cloud"
USAGE_URL = "https://ollama.com/api/usage"

REQUEST_TIMEOUT_SEC = 20
MAX_RESPONSE_BYTES = 262144      # the real response is a few hundred bytes
MAX_MODEL_ROWS = 16              # bound every parsed collection
MAX_MODEL_NAME_CHARS = 64

KEY_FILE_REL = "ollama-cloud-usage/api-key"
ENV_NAME = "OLLAMA_CLOUD_API_KEY"
ENV_KEYFILE = "OLLAMA_USAGE_KEY_FILE"
ENV_HINTS = (
    "No Ollama Cloud API key found. Create a personal key at"
    " https://ollama.com/settings/api, save it to"
    f" ~/.config/{KEY_FILE_REL} with mode 0600, or export"
    " OLLAMA_CLOUD_API_KEY; then refresh."
)


def usage_record_path() -> str:
    root = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(root, "omarchy", "agents", "usage", f"{AGENT_ID}.json")


def empty_result(**overrides) -> dict:
    out: dict = {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "ready": False,
        "hasLocalStats": False,
        "hasPromptStats": True,
        # ollama.com numbers are account-global, so synced machines must not
        # sum them.
        "scope": "account",
        "tierLabel": "",
        "usageStatusText": "",
        "authHelpText": "",
        "limits": [],
        "balance": {},
        "modelRequests": {},
        "modelUsage": {},
    }
    out.update(overrides)
    return out


# ── API key sources (checked in this order) ─────────────────────────────
#   1. --api-key-file <path> (or $OLLAMA_USAGE_KEY_FILE)  explicit override
#   2. $OLLAMA_CLOUD_API_KEY                              environment
#   3. ~/.config/ollama-cloud-usage/api-key               0600, plugin-owned
#   4. ~/.dsh/.credentials.yaml `refs:` entry             DeepSeek Harness
# The file-based sources must be 0600 (group/world-readable files are
# refused with a chmod hint); the key never reaches a command line.

ENV_NAME = "OLLAMA_CLOUD_API_KEY"
ENV_KEYFILE = "OLLAMA_USAGE_KEY_FILE"


def _config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )


def _plugin_key_file() -> str:
    return os.path.join(_config_home(), KEY_FILE_REL)


def resolve_api_key(cli_key_file: str) -> str:
    for candidate in _key_source_paths(cli_key_file):
        key = read_key_file(candidate)
        if key:
            return key
    env_key = os.environ.get(ENV_NAME, "")
    if env_key:
        return env_key.strip()
    dsh_key = key_from_dsh()
    if dsh_key:
        return dsh_key
    raise RuntimeError(ENV_HINTS)


# ── Reset-window anchors ────────────────────────────────────────────────
# /api/usage carries usage fractions but no "resets at" timestamps — those
# only exist on ollama.com/settings ("Resets in 1 hour", "Resets in 6 days").
# The collector therefore extrapolates from a one-off seed taken from that
# page (--seed-resets session=52m --seed-resets weekly=4d6h) and keeps the
# anchors honest afterwards: a usage fraction that DROPS between polls means
# the window rolled, so the anchor is re-pinned at the observed sample.
SESSION_PERIOD_SEC = 5 * 3600
WEEKLY_PERIOD_SEC = 7 * 24 * 3600
RESET_STATE_FILE = "ollama-resets.json"
RESET_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(d|h|m|s)", re.I)
RESET_UNITS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}


def reset_state_path() -> str:
    return os.path.join(os.path.dirname(usage_record_path()), RESET_STATE_FILE)


def load_reset_state() -> dict:
    try:
        with open(reset_state_path(), "r", encoding="utf-8") as handle:
            state = json.load(handle)
            return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_reset_state(state: dict) -> None:
    os.makedirs(os.path.dirname(reset_state_path()), exist_ok=True)
    tmp = f"{reset_state_path()}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, reset_state_path())


def parse_duration(text: str) -> float:
    """'52m', '1h43m', '6d' → seconds (0 for unparseable input)."""
    total = 0.0
    for value, unit in RESET_DURATION_RE.findall(str(text or "")):
        total += float(value) * RESET_UNITS[unit.lower()]
    return total


def seed_resets(specs: list[str]) -> None:
    """--seed-resets session=52m --seed-resets weekly=4d6h taken from the
    settings page at roughly this moment."""
    state = load_reset_state()
    now = time.time()
    for spec in specs or []:
        key, _, value = spec.strip().partition("=")
        key = key.strip().lower()
        seconds = parse_duration(value)
        if key not in ("session", "weekly") or seconds <= 0:
            raise ValueError(
                f"--seed-resets wants session=<duration> or weekly=<duration>"
                f" (d/h/m/s), got {spec!r}"
            )
        period = SESSION_PERIOD_SEC if key == "session" else WEEKLY_PERIOD_SEC
        state[key] = {"anchorEpoch": now + seconds, "periodSec": period}
    save_reset_state(state)
    print(
        "ollama-cloud-usage: seeded reset anchors"
        + (f": {', '.join(s for s in specs if s)}" if specs else ""),
        file=sys.stderr,
    )


def reset_iso(state: dict, key: str, period: float, usage, now: float) -> str:
    """Next reset timestamp for a window, '' when unknown.

    A drop in the usage fraction between the previous sample and now means
    the window rolled since that sample: re-pin the anchor at `now` (error
    is bounded by the poll interval) and let the next period carry on."""
    entry = state.get(key) if isinstance(state.get(key), dict) else {}
    anchor = float(entry.get("anchorEpoch") or 0.0)
    prev = state.get("prev") if isinstance(state.get("prev"), dict) else {}
    prev_usage = prev.get(key)
    dropped = (
        isinstance(usage, (int, float))
        and isinstance(prev_usage, (int, float))
        and usage < prev_usage - 1e-9
    )
    if dropped or (anchor and anchor <= now):
        # Window rolled since the previous sample (or the estimate expired):
        # re-pin at `now`; the error is bounded by the poll interval.
        anchor = now + period
    if not anchor or anchor <= now:
        return ""
    return datetime.fromtimestamp(anchor, tz=timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def apply_reset_anchors(record: dict) -> dict:
    """Attach resetsAt (estimated) to the record's limits; store samples."""
    state = load_reset_state()
    now = time.time()
    limits = record.get("limits") or []
    for entry in limits:
        key = str(entry.get("title") or "").lower()
        if key == "session":
            entry["resetsAt"] = reset_iso(state, "session", SESSION_PERIOD_SEC, entry.get("percent"), now)
        elif key == "weekly":
            entry["resetsAt"] = reset_iso(state, "weekly", WEEKLY_PERIOD_SEC, entry.get("percent"), now)
    state["prev"] = {
        "at": now,
        "session": next((l["percent"] for l in limits if l.get("title") == "Session"), None),
        "weekly": next((l["percent"] for l in limits if l.get("title") == "Weekly"), None),
    }
    save_reset_state(state)
    return record


def _key_source_paths(cli_key_file: str) -> list[str]:
    out = []
    override = cli_key_file or os.environ.get(ENV_KEYFILE, "")
    if override:
        out.append(os.path.expanduser(override))
    plugin_file = _plugin_key_file()
    if os.path.isfile(plugin_file):
        out.append(plugin_file)
    return out


def key_from_dsh() -> str:
    """Optional convenience: DeepSeek Harness stores provider refs by env name."""
    path = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("OLLAMA_CLOUD_API_KEY:"):
                    value = line.split(":", 1)[1].strip()
                    if value:
                        return value
    except OSError:
        pass
    return ""


def read_key_file(path: str) -> str:
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError(
            f"{path} is readable by group/others; run: chmod 600 {path}"
        )
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect before the follow-up request is built.

    The stock handler copies req.headers (including Authorization) onto the
    follow-up request, so a 301/302/307/308 from ollama.com would forward
    the API key to whatever host the Location header names. Returning None
    makes any redirect surface as an HTTPError instead: the request fails
    loudly and the key never leaves ollama.com.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_usage(api_key: str) -> dict:
    """Call /api/usage with the key injected only as an HTTP header.

    Refuses redirects: the API key must never be forwarded to another host
    on a 301/302/307/308 from ollama.com (open redirect or hijacked host).
    """
    request = Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "omarchy-ollama-cloud-usage/1.0",
            "Accept": "application/json",
        },
    )
    with _NO_REDIRECT_OPENER.open(request, timeout=REQUEST_TIMEOUT_SEC) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("usage response exceeded the 256 KiB cap")
    return json.loads(payload.decode("utf-8"))


def clamp_fraction(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return min(max(number, 0.0), 1.0)


def build_record(api_payload) -> dict:
    limits: list = []
    model_requests: dict = {}
    if isinstance(api_payload, dict):
        raw_limits = api_payload.get("limits") or {}
        if isinstance(raw_limits, dict):
            for key, label, title in (
                ("session", "Session (5-hour)", "Session"),
                ("weekly", "Weekly", "Weekly"),
            ):
                window = raw_limits.get(key)
                if isinstance(window, dict):
                    limits.append(
                        {
                            "label": label,
                            # The panel expects percent as a 0-1 fraction.
                            "percent": clamp_fraction(window.get("usage")),
                            "resetsAt": "",
                            "title": title,
                        }
                    )
        if isinstance(api_payload.get("activity"), dict):
            for entry in api_payload["activity"].get("models") or []:
                if not isinstance(entry, dict):
                    break
                if len(model_requests) >= MAX_MODEL_ROWS:
                    break
                name = str(entry.get("name") or "")[:MAX_MODEL_NAME_CHARS]
                if name:
                    model_requests[name] = int(entry.get("request_count") or 0)
    return empty_result(
        ready=True,
        tierLabel="",
        limits=limits,
        balance={},
        modelRequests=model_requests,
    )


def collect_record(cli_key_file: str) -> dict:
    api_key = resolve_api_key(cli_key_file)
    record = build_record(fetch_usage(api_key))
    if not record["limits"]:
        raise RuntimeError("usage API parsed but no limits found")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print or write the Ollama Cloud usage record as JSON"
    )
    # --force / --limits-only exist so every collector accepts the same invocation.
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limits-only", action="store_true")
    parser.add_argument(
        "--write", action="store_true",
        help="write ~/.local/state/omarchy/agents/usage/ollama.json",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="remove ollama.json so the agents panel drops the Ollama Cloud provider",
    )
    parser.add_argument(
        "--api-key-file", default="",
        help="File holding the personal Ollama Cloud API key (0600); "
        "otherwise $OLLAMA_CLOUD_API_KEY, ~/.config/ollama-cloud-usage/api-key, "
        "or ~/.dsh/.credentials.yaml",
    )
    parser.add_argument(
        "--seed-resets", action="append", default=[], metavar="WINDOW=DURATION",
        help="one-off anchor seed read from ollama.com/settings, e.g. "
        "--seed-resets session=52m --seed-resets weekly=4d6h; the collector "
        "extrapolates the next resets afterwards and re-pins them whenever "
        "an observed usage drop proves the window rolled",
    )
    args = parser.parse_args(argv)

    if args.clear:
        target = usage_record_path()
        if os.path.exists(target):
            os.remove(target)
        return 0

    if args.seed_resets:
        seed_resets(args.seed_resets)

    try:
        record = collect_record(args.api_key_file)
        if record.get("ready"):
            record = apply_reset_anchors(record)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ollama-cloud-usage: {exc}", file=sys.stderr)
        if os.path.exists(usage_record_path()):
            # Keep the last good record; a transient failure (network, API
            # drift) should not blank the panel chip.
            return 0
        record = empty_result(
            usageStatusText="Ollama Cloud usage unavailable",
            authHelpText=ENV_HINTS,
        )

    if args.write:
        target = usage_record_path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        print(f"ollama-cloud-usage: wrote {target}")
    else:
        print(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())