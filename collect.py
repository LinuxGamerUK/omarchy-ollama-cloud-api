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
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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


def fetch_usage(api_key: str) -> dict:
    """Call /api/usage with the key injected only as an HTTP header."""
    request = Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "omarchy-ollama-cloud-usage/1.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
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
    args = parser.parse_args(argv)

    if args.clear:
        target = usage_record_path()
        if os.path.exists(target):
            os.remove(target)
        return 0

    try:
        record = collect_record(args.api_key_file)
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