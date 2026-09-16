"""Bounded, metadata-only Antigravity /usage query. Execute with uv.

Fail closed on agy < 1.1.11, unknown/disabled quota or any inference usage.
No project cwd, model prompt, TUI scraping, raw errors or shared credentials.
"""

import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time


class QuotaError(ValueError):
    pass


def stop_owned_process(process):
    # start_new_session=True: signal only the group we created, including
    # descendants left after its leader exits. Never attach to another agy.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            continue


def run_bounded(command, cwd, timeout, max_bytes):
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise QuotaError("command_timeout")
                chunk = os.read(process.stdout.fileno(), min(65536, max_bytes + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_bytes:
                    raise QuotaError("output_too_large")
        try:
            rc = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise QuotaError("command_timeout") from None
        if rc != 0:
            raise QuotaError("command_failed")
        return bytes(output)
    finally:
        stop_owned_process(process)
        process.stdout.close()


def version_supported(output):
    match = re.fullmatch(r"(?:agy\s+(?:version\s+)?)?(\d+)\.(\d+)\.(\d+)\s*", output.decode("utf-8"))
    return bool(match and tuple(map(int, match.groups())) >= (1, 1, 11))


def normalize_report(report, captured_at):
    if not isinstance(report, dict) or report.get("status") != "SUCCESS" or report.get("error"):
        raise QuotaError("invalid_usage_report")
    command = report.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        raise QuotaError("not_usage_command")
    turns, usage = report.get("num_turns"), report.get("usage")
    if type(turns) is not int or turns != 0 or not isinstance(usage, dict):
        raise QuotaError("inference_report_rejected")
    if not {"input_tokens", "output_tokens", "total_tokens"}.issubset(usage):
        raise QuotaError("inference_report_rejected")
    for field in ("input_tokens", "output_tokens", "total_tokens", "thinking_tokens", "cache_read_tokens"):
        count = usage.get(field, 0)
        if type(count) not in (int, float) or count != 0:
            raise QuotaError("inference_report_rejected")
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise QuotaError("missing_quota_groups")
    gemini = [g for g in groups if isinstance(g, dict)
              and str(g.get("name", "")).casefold() in ("gemini", "gemini models")]
    if len(gemini) != 1 or not isinstance(gemini[0].get("buckets"), list):
        raise QuotaError("missing_or_ambiguous_gemini_group")
    if gemini[0].get("enabled") is False:
        raise QuotaError("disabled_gemini_group")
    windows = {}
    for bucket in gemini[0]["buckets"]:
        if not isinstance(bucket, dict) or bucket.get("window") not in ("5h", "weekly"):
            continue
        if bucket.get("enabled") is False or bucket.get("usage_known") is False:
            continue
        fraction = bucket.get("remaining_fraction")
        if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise QuotaError("invalid_remaining_fraction")
        reset = bucket.get("reset_time")
        if not isinstance(reset, str):
            raise QuotaError("invalid_reset_time")
        try:
            dt = datetime.datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError
            epoch = int(dt.timestamp())
        except (ValueError, OverflowError, OSError):
            raise QuotaError("invalid_reset_time") from None
        if epoch <= 0 or bucket["window"] in windows:
            raise QuotaError("invalid_or_ambiguous_quota_window")
        windows[bucket["window"]] = {"remainingPercent": math.floor(fraction * 100 + 0.5), "resetAt": epoch}
    if set(windows) != {"5h", "weekly"}:
        raise QuotaError("incomplete_quota_windows")
    return {"source": "Native · agy /usage", "fresh": True, "capturedAt": captured_at,
            "fiveHour": windows["5h"], "weekly": windows["weekly"]}


def binary_identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def fetch_quota(agy, timeout):
    binary = Path(agy).resolve(strict=True)
    identity, deadline = binary_identity(binary), time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="pi-antigravity-usage.") as cwd:
        os.chmod(cwd, 0o700)
        version = run_bounded([str(binary), "--version"], cwd, min(3, timeout), 4096)
        if not version_supported(version):
            raise QuotaError("unsupported_agy_version")
        if binary_identity(binary) != identity:
            raise QuotaError("agy_binary_changed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QuotaError("command_timeout")
        raw = run_bounded([str(binary), "-p", "/usage", "--output-format", "json"], cwd, remaining, 1024 * 1024)
        if binary_identity(binary) != identity:
            raise QuotaError("agy_binary_changed")
        return normalize_report(json.loads(raw), int(time.time()))


def cancelled(_signum, _frame):
    raise QuotaError("command_cancelled")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agy", required=True)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    signal.signal(signal.SIGTERM, cancelled)
    try:
        quota = fetch_quota(args.agy, args.timeout)
    except QuotaError as error:
        print(f"antigravity_usage: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print("antigravity_usage: quota_query_failed", file=sys.stderr)
        return 1
    print(json.dumps(quota, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
