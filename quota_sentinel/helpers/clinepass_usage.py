"""Bounded, metadata-only ClinePass quota query. Execute with python3.

Tier ① for ClinePass, mirroring ``opencode_usage.py``: the API key arrives on
stdin (never argv, never the environment) and is handed to curl through curl's
own stdin config, so it can appear in no process argument list. Fail closed on
any unexpected shape: no response body, key or header value ever reaches
stderr, only a fixed reason code. The query consumes no model tokens or
inference turns.

Endpoint note: ``/api/v1/users/me/plan/usage-limits`` is not part of Cline's
public API reference (which documents chat completions, models and errors).
It is the endpoint the Cline CLI/extension, CodexBar and the community usage
tools all read, and it is the only source that reports the subscription's
three windows with their resets, so it is what tier ① speaks. The documented
``X-Title`` header is sent because Cline's own docs say it labels the caller in
their usage logs.
"""

import argparse
import calendar
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
from pathlib import Path as _Path
if not __package__:sys.path.insert(0,str(_Path(__file__).resolve().parents[2]))
import tempfile
import time

USAGE_URL = "https://api.cline.bot/api/v1/users/me/plan/usage-limits"
USER_AGENT = "quota-sentinel/1.0"
TITLE = "quota-sentinel"
# The gateway's `type` -> the normalized window name. five_hour and weekly are
# required; monthly is the plan's billing-cycle cap, carried for display only.
REQUIRED_WINDOWS = (("five_hour", "fiveHour"), ("weekly", "weekly"))
OPTIONAL_WINDOWS = (("monthly", "monthly"),)
MAX_BODY_BYTES = 64 * 1024

# The gateway answers with nanosecond precision ("...T09:09:42.819795817Z"),
# which `datetime.fromisoformat` refuses on the 3.9 interpreter the deployment
# targets, so the whole seconds are parsed and the fraction is dropped.
_ISO_Z = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z$"
)


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
    config = (
        'header = "Authorization: Bearer %s"\n' % api_key
        + 'header = "Accept: application/json"\n'
        + 'header = "X-Title: %s"\n' % TITLE
    )
    with tempfile.TemporaryDirectory(prefix="pi-clinepass-usage.") as cwd:
        os.chmod(cwd, 0o700)
        raw = run_bounded(
            [curl, "--config", "-", "--silent", "--show-error",
             "--user-agent", USER_AGENT,
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


def reset_epoch(value):
    if not isinstance(value, str):
        raise QuotaError("invalid_reset_time")
    match = _ISO_Z.match(value)
    if match is None:
        raise QuotaError("invalid_reset_time")
    try:
        parts = tuple(int(part) for part in match.groups()[:6])
        epoch = calendar.timegm(parts + (0, 0, 0))
    except (ValueError, OverflowError, OSError):
        raise QuotaError("invalid_reset_time") from None
    if epoch <= 0:
        raise QuotaError("invalid_reset_time")
    return epoch


def normalize_window(limit):
    if not isinstance(limit, dict):
        raise QuotaError("invalid_window")
    percent = limit.get("percentUsed")
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise QuotaError("invalid_percent")
    # An account with no open window reports percentUsed 0 and omits resetsAt
    # entirely. Without a reset there is no boundary to schedule from, so the
    # window is rejected rather than given a fabricated one.
    if limit.get("resetsAt") is None:
        raise QuotaError("missing_reset_time")
    # The API reports used percent; remaining is its complement on a 0..100 scale.
    remaining = max(0, min(100, int(round(100 - percent))))
    return {"remainingPercent": remaining, "resetAt": reset_epoch(limit["resetsAt"])}


def normalize_report(report, captured_at):
    if not isinstance(report, dict):
        raise QuotaError("invalid_report")
    if report.get("success") is not True:
        raise QuotaError("response_not_success")
    data = report.get("data")
    if not isinstance(data, dict):
        raise QuotaError("invalid_report")
    limits = data.get("limits")
    if not isinstance(limits, list):
        raise QuotaError("invalid_report")
    by_type = {}
    for limit in limits:
        if not isinstance(limit, dict):
            raise QuotaError("invalid_window")
        if limit.get("type") in dict(REQUIRED_WINDOWS + OPTIONAL_WINDOWS):
            by_type[limit["type"]] = limit
    result = {"source": "Native · clinepass /plan/usage-limits",
              "fresh": True, "capturedAt": captured_at}
    for api_name, normalized_name in REQUIRED_WINDOWS:
        if api_name not in by_type:
            raise QuotaError("missing_quota_window")
        result[normalized_name] = normalize_window(by_type[api_name])
    for api_name, normalized_name in OPTIONAL_WINDOWS:
        # A display-only window must never fail the whole probe: an absent or
        # malformed monthly cap simply omits its line from the card.
        try:
            result[normalized_name] = normalize_window(by_type[api_name])
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
        print(f"clinepass_usage: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print("clinepass_usage: quota_query_failed", file=sys.stderr)
        return 1
    print(json.dumps(quota, allow_nan=False, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
