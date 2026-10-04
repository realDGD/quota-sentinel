"""Bounded, metadata-only OpenCode Go quota query. Execute with python3.

The API key arrives on stdin (never argv, never the environment) and is handed
to curl as an Authorization header through curl's own stdin config, so it can
appear in no process argument list. Fail closed on any unexpected shape: no
response body, key or header value ever reaches stderr, only a fixed reason
code. The query consumes no model tokens or inference turns.
"""

import argparse
import datetime
import json
import math
import os
import selectors
import signal
import subprocess
import sys
from pathlib import Path as _Path
if not __package__:sys.path.insert(0,str(_Path(__file__).resolve().parents[2]))
import tempfile
import time

USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
# API window name -> normalized window name. rolling/weekly are required;
# monthly is the plan's billing-cycle cap and is carried for display only.
REQUIRED_WINDOWS = (("rolling", "fiveHour"), ("weekly", "weekly"))
OPTIONAL_WINDOWS = (("monthly", "monthly"),)
MAX_BODY_BYTES = 64 * 1024


class QuotaError(ValueError):
    pass


def run_bounded(command, cwd, timeout, max_bytes, stdin_text=None):
    from quota_sentinel.platform.process import run_bounded as run_owned
    from quota_sentinel.platform.paths import resolve_launcher
    try:
        prefix=resolve_launcher('curl',explicit=command[0])
        result=run_owned((*prefix,*command[1:]),cwd=cwd,environment=os.environ,
            timeout=timeout,kill_grace=1,max_bytes=max_bytes,input_data=(stdin_text or "").encode())
    except QuotaError:raise
    except (OSError,ValueError):raise QuotaError('command_unavailable') from None
    if result.timed_out or result.returncode==28:raise QuotaError('command_timeout')
    if result.returncode==125:raise QuotaError('output_too_large')
    if result.returncode:raise QuotaError('command_failed_%d'%result.returncode)
    return result.stdout.decode("utf-8","replace")


def http_status(output):
    # curl's --write-out appends "<status>" after the body's final newline.
    body, _, status = output.rpartition("\n")
    return body, status.strip()


def fetch_report(curl, api_key, timeout):
    if not api_key:
        raise QuotaError("auth_missing")
    from quota_sentinel.platform.paths import resolve_launcher
    try:resolve_launcher("curl",explicit=curl)
    except ValueError:raise QuotaError("curl_unavailable") from None
    header = "header = \"Authorization: Bearer %s\"\n" % api_key
    config = header + "header = \"Accept: application/json\"\n"
    with tempfile.TemporaryDirectory(prefix="pi-opencode-usage.") as cwd:
        os.chmod(cwd, 0o700)
        raw = run_bounded(
            [curl, "--config", "-", "--silent", "--show-error",
             "--connect-timeout", str(min(5, timeout)),
             "--max-time", str(timeout),
             "--write-out", "\n%{http_code}",
             USAGE_URL],
            cwd, timeout + 1, MAX_BODY_BYTES, stdin_text=config)
    body, status = http_status(raw)
    if status == "401":
        raise QuotaError("http_401")
    if status == "403":
        raise QuotaError("http_403")
    if status != "200":
        raise QuotaError("http_error")
    try:
        return json.loads(body)
    except ValueError:
        raise QuotaError("invalid_report") from None


def normalize_window(value):
    if not isinstance(value, dict):
        raise QuotaError("invalid_window")
    if value.get("status") not in ("ok", "rate-limited"):
        raise QuotaError("invalid_window_status")
    percent = value.get("percent")
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise QuotaError("invalid_percent")
    reset = value.get("resetsAt")
    if not isinstance(reset, str):
        raise QuotaError("invalid_reset_time")
    try:
        # The endpoint emits ISO-8601 with milliseconds and a trailing Z.
        moment = datetime.datetime.fromisoformat(reset.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError
        epoch = int(moment.timestamp())
    except (ValueError, OverflowError, OSError):
        raise QuotaError("invalid_reset_time") from None
    if epoch <= 0:
        raise QuotaError("invalid_reset_time")
    # The API reports used percent; remaining is its complement on a 0..100 scale.
    remaining = max(0, min(100, int(round(100 - percent))))
    return {"remainingPercent": remaining, "resetAt": epoch}


def normalize_report(report, captured_at):
    usage = report.get("usage") if isinstance(report, dict) else None
    if not isinstance(usage, dict):
        raise QuotaError("invalid_report")
    windows = {}
    for api_name, normalized_name in REQUIRED_WINDOWS:
        if api_name not in usage:
            raise QuotaError("missing_quota_window")
        windows[normalized_name] = normalize_window(usage[api_name])
    result = {"source": "Native · opencode-go /usage", "fresh": True, "capturedAt": captured_at}
    result.update(windows)
    for api_name, normalized_name in OPTIONAL_WINDOWS:
        # A display-only window must never fail the whole probe: an absent or
        # malformed monthly cap simply omits its line from the card.
        try:
            result[normalized_name] = normalize_window(usage[api_name])
        except (QuotaError, KeyError):
            continue
    return result


def cancelled(_signum, _frame):
    raise QuotaError("command_cancelled")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--curl", default="curl")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    signal.signal(signal.SIGTERM, cancelled)
    api_key = sys.stdin.readline().strip()
    try:
        quota = normalize_report(fetch_report(args.curl, api_key, args.timeout), int(time.time()))
    except QuotaError as error:
        print(f"opencode_usage: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print("opencode_usage: quota_query_failed", file=sys.stderr)
        return 1
    print(json.dumps(quota, allow_nan=False, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
