"""Composition root: build the production Application, probe and notifier.

This is the ONLY module that knows how the real pieces fit together. The CLI
builds through here so that tests can replace the same boundaries the shell
allowed them to replace (binary paths via environment), and so that nothing
else in the package has to import a concrete external adapter.

Imports stay inside the standard library on purpose: this module is part of
the graph the system interpreter loads (tests/python-entrypoint-regression.py
E11, tests/uv-project-regression.py UV11), so it must never reach for
lark-oapi or any other third-party package.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quota_sentinel.app import AppConfig, Application
from quota_sentinel.quota.adapters import PROVIDERS
from quota_sentinel.runtime import keychain, runlog
from quota_sentinel.runtime.cards import (
    format_reset_time, render_progress_test_card, render_task_card,
    render_usage_card,
)
from quota_sentinel.runtime.feishu import (
    FeishuClient, FeishuError, FeishuNotifier, KeychainCredentials,
)
from quota_sentinel.runtime.models import ModelRunner, ModelRunnerConfig
from quota_sentinel.runtime.quota_probe import QuotaCollector
from quota_sentinel.scheduler import service
from quota_sentinel.state.runlock import SHLOCK_BIN

REPO_DIR = Path(__file__).resolve().parents[2]
OPENCODE_API_KEY_SERVICE = "quota-sentinel.opencode-go-api-key"
DRY_RUN_ENV = "FEISHU_DRY_RUN"
DEFAULT_CURL_BIN = Path("/usr/bin/curl")


class NotReadyError(RuntimeError):
    """This deployment cannot run a task, discovered before spending quota."""


def _env(environment: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def _path_override(
    env: Mapping[str, str], name: str, default: Path
) -> Path:
    """`${VAR:-default}` semantics: an empty value means unset."""
    value = env.get(name, "")
    return Path(value) if value else default


def dry_run(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _env(environment).get(DRY_RUN_ENV) == "1"


def create_notifier(
    *, environment: Optional[Mapping[str, str]] = None,
    dry_run_flag: Optional[bool] = None,
) -> FeishuNotifier:
    flag = dry_run(environment) if dry_run_flag is None else dry_run_flag
    return FeishuNotifier(FeishuClient(dry_run=flag))


def _seconds_override(
    env: Mapping[str, str], name: str, default: float, *, allow_zero: bool = False
) -> float:
    """Read an optional probe budget without allowing an invalid duration."""
    raw = env.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        return default
    return value


def quota_probe_options(environment: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The same overridable boundaries the shell exposed to its suites."""
    env = _env(environment)
    return {
        "codex_bin": _path_override(env, "QUOTA_SENTINEL_CODEX_BIN",
                                    Path("/opt/homebrew/bin/codex")),
        "agy_bin": _path_override(env, "QUOTA_SENTINEL_AGY_BIN",
                                  Path("/opt/homebrew/bin/agy")),
        "uv_bin": _path_override(env, "QUOTA_SENTINEL_UV_BIN",
                                 Path("/opt/homebrew/bin/uv")),
        "codexbar_bin": _path_override(env, "QUOTA_SENTINEL_CODEXBAR_BIN",
                                       Path("/opt/homebrew/bin/codexbar")),
        "opencode_usage_helper": _path_override(
            env, "QUOTA_SENTINEL_OPENCODE_USAGE_HELPER",
            REPO_DIR / "opencode_usage.py",
        ),
        "antigravity_usage_helper": REPO_DIR / "antigravity_usage.py",
        "curl_bin": _path_override(env, "QUOTA_SENTINEL_CURL_BIN", DEFAULT_CURL_BIN),
        "codexbar_timeout": _seconds_override(env, "QUOTA_SENTINEL_CODEXBAR_TIMEOUT", 20),
        "antigravity_codexbar_timeout": _seconds_override(
            env, "QUOTA_SENTINEL_ANTIGRAVITY_CODEXBAR_TIMEOUT", 35
        ),
        "opencode_codexbar_timeout": _seconds_override(
            env, "QUOTA_SENTINEL_OPENCODE_CODEXBAR_TIMEOUT", 20
        ),
        "antigravity_native_timeout": _seconds_override(
            env, "QUOTA_SENTINEL_ANTIGRAVITY_NATIVE_TIMEOUT", 20
        ),
        "opencode_native_timeout": _seconds_override(
            env, "QUOTA_SENTINEL_OPENCODE_NATIVE_TIMEOUT", 15
        ),
        "codexbar_kill_grace": _seconds_override(
            env, "QUOTA_SENTINEL_CODEXBAR_KILL_GRACE", 10, allow_zero=True
        ),
    }


def require_ready(
    state_dir: Path, *, environment: Optional[Mapping[str, str]] = None,
    providers: Sequence[str] = PROVIDERS,
) -> None:
    """The shell's `validate_run_requirements`, as a refusal.

    Runs BEFORE any model attempt: a missing credential or hook discovered
    after three 300s provider timeouts is quota spent on a run that cannot be
    delivered. Everything the Python port does not actually use (jq, /bin/sleep)
    is deliberately not required here, unlike the shell's list.
    """
    problems = readiness_problems(
        state_dir, environment=environment, providers=providers
    )
    if problems:
        raise NotReadyError("not ready: " + "; ".join(problems))


def create_application(
    state_dir: Path, *, environment: Optional[Mapping[str, str]] = None,
    dry_run_flag: Optional[bool] = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    config: Optional[AppConfig] = None,
) -> Application:
    env = _env(environment)
    state_dir = Path(state_dir)
    # The runner's per-attempt lines carry phase/attempt/result/elapsed and a
    # REDACTED stderr summary; without this seam a failed attempt would leave
    # no trace at all in the operator's run log.
    runner = ModelRunner(
        ModelRunnerConfig.from_env(env),
        logger=logging.getLogger("quota_sentinel.model").info,
    )
    notifier = create_notifier(environment=env, dry_run_flag=dry_run_flag)
    options = quota_probe_options(env)

    def collector_factory(workspace: Path) -> QuotaCollector:
        # The same seam the model runner already has. Without it every tier
        # outcome inside the collector goes to a no-op: a native probe that
        # hangs until its timeout and one that fails in milliseconds look
        # identical in the run log, and the tier that actually served the
        # reading is never written down. The lines carry provider, tier,
        # outcome and seconds only.
        return QuotaCollector(
            state_dir, workspace,
            logger=logging.getLogger("quota_sentinel.quota").info,
            **options,
        )

    return Application(
        state_dir, runner, collector_factory, notifier,
        clock=clock, sleep=sleep,
        config=config if config is not None else AppConfig.from_env(env),
        preflight=lambda providers: require_ready(
            state_dir, environment=env, providers=providers
        ),
    )


# ---------------------------------------------------------------------------
# Readiness and status
# ---------------------------------------------------------------------------

def _provider_hooks(provider: str, config: ModelRunnerConfig) -> List[Path]:
    hooks = [REPO_DIR / ("capture-%s-quota.ts" % provider)]
    if provider == "antigravity":
        # Pi loads this provider extension for antigravity runs; a model
        # attempt without it fails after the timeout, not before.
        hooks.append(config.antigravity_provider_extension)
    return hooks


def _auth_has_provider(path: Path, provider: str) -> bool:
    key = {"codex": "openai-codex", "antigravity": "antigravity",
           "opencode": "opencode-go"}[provider]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(document, dict) and key in document


def opencode_api_key_available(
    *, environment: Optional[Mapping[str, str]] = None
) -> bool:
    env = _env(environment)
    if env.get("OPENCODE_API_KEY"):
        return True
    return keychain.present(OPENCODE_API_KEY_SERVICE, environment=env)


def readiness_problems(
    state_dir: Path, *, environment: Optional[Mapping[str, str]] = None,
    providers: Sequence[str] = PROVIDERS,
) -> List[str]:
    env = _env(environment)
    problems: List[str] = []
    config = ModelRunnerConfig.from_env(env)

    executables = {
        "pi": config.pi_bin,
        "curl": quota_probe_options(env)["curl_bin"],
        "security": Path(keychain.SECURITY_BIN),
        "shlock": Path(SHLOCK_BIN),
    }
    for name, path in executables.items():
        if not os.access(path, os.X_OK):
            problems.append("%s is not executable: %s" % (name, path))

    if not os.access(config.auth_file, os.R_OK):
        problems.append("Pi OAuth credential is not readable: %s" % config.auth_file)
    else:
        for provider in providers:
            if provider not in PROVIDERS:
                problems.append("unknown provider: %s" % provider)
            elif not _auth_has_provider(config.auth_file, provider):
                problems.append("Pi credential for %s is missing" % provider)

    for provider in providers:
        if provider not in PROVIDERS:
            continue
        for hook in _provider_hooks(provider, config):
            if not os.access(hook, os.R_OK):
                problems.append("%s quota hook is not readable: %s" % (provider, hook))

    # An unwritable state directory is a deployment fault the operator should
    # hear about here rather than as a failed lock acquisition mid-run. A
    # MISSING directory is fine: the lock helpers create it.
    state_dir = Path(state_dir)
    if state_dir.exists() and not os.access(state_dir, os.W_OK):
        problems.append("state directory is not writable: %s" % state_dir)

    credentials = KeychainCredentials(env)
    for name, label in (("app_id", "App ID"), ("app_secret", "app secret"),
                        ("user_id", "user id")):
        if not credentials.get(name):
            problems.append("Feishu %s is not configured" % label)
    return problems


def status_lines(
    state_dir: Path, *, environment: Optional[Mapping[str, str]] = None,
    now: Optional[int] = None,
) -> List[str]:
    """The shell's `status` report, in the same order and shape."""
    env = _env(environment)
    options = quota_probe_options(env)
    lines = [
        "ready",
        "channel: feishu enterprise app",
    ]
    if os.access(options["codex_bin"], os.X_OK) or os.access(options["codexbar_bin"], os.X_OK):
        lines.append(
            "quota primary: Native Direct (Codex app-server / Antigravity agy "
            "/ OpenCode Go usage API) · CodexBar fallback"
        )
    else:
        lines.append("quota primary: unavailable; last Pi snapshots may be used")
    if opencode_api_key_available(environment=env):
        lines.append("opencode api key: configured")
    else:
        lines.append(
            "opencode api key: missing (%s); Native tier disabled"
            % OPENCODE_API_KEY_SERVICE
        )

    states = service.load_roster(Path(state_dir), PROVIDERS)
    for provider in PROVIDERS:
        next_due = states[provider].next_due_at
        if next_due is None:
            lines.append("next %s run: due now" % provider)
        else:
            lines.append("next %s run: %s" % (provider, format_reset_time(next_due)))
    return lines


# ---------------------------------------------------------------------------
# Card preview and test delivery
# ---------------------------------------------------------------------------

def preview_readings(now: int) -> Dict[str, Dict[str, Any]]:
    """The shell's `setup_mock_preview_quota`, as display-only documents."""
    def window(percent: int, reset_at: int) -> Dict[str, int]:
        return {"remainingPercent": percent, "resetAt": reset_at}

    return {
        "codex": {
            "source": "Native · codex app-server", "fresh": True,
            "capturedAt": now,
            "fiveHour": window(100, now + 17880),
            "weekly": window(84, now + 595800),
        },
        "antigravity": {
            "source": "Native · agy local service", "fresh": True,
            "capturedAt": now,
            "fiveHour": window(77, now + 17700),
            "weekly": window(86, now + 369660),
        },
        "opencode": {
            "source": "Native · opencode-go /usage", "fresh": True,
            "capturedAt": now,
            "fiveHour": window(88, now + 16980),
            "weekly": window(95, now + 317340),
            "monthly": window(98, now + 2574000),
        },
    }


def preview_payload(
    mode: str, *, user_id: str, request_uuid: str, now: int,
) -> Dict[str, Any]:
    """One preview/test payload for every mode the shell accepted."""
    readings = preview_readings(now)
    results = {provider: "发送成功" for provider in PROVIDERS}
    if mode == "usage":
        return render_usage_card(PROVIDERS, readings, user_id, request_uuid, now=now)
    if mode == "progress":
        return render_progress_test_card(user_id, request_uuid)
    if mode in ("single", "codex"):
        members = ("codex",)
    elif mode == "antigravity":
        members = ("antigravity",)
    elif mode == "opencode":
        members = ("opencode",)
    elif mode == "both":
        # "both" keeps its historical meaning: the original pair.
        members = ("codex", "antigravity")
    else:
        members = tuple(PROVIDERS)
    return render_task_card(members, results, readings, user_id, request_uuid, now=now)


def send_payload(payload: Mapping[str, Any], *, environment=None,
                 dry_run_flag: Optional[bool] = None) -> Optional[dict]:
    return FeishuClient(
        dry_run=(dry_run(environment) if dry_run_flag is None else dry_run_flag),
        environment=environment,
    ).send(payload)


def notifier_user_id(*, environment: Optional[Mapping[str, str]] = None) -> str:
    """The configured recipient, for the test-card path."""
    value = KeychainCredentials(_env(environment)).get("user_id")
    if not value:
        raise FeishuError("missing Feishu user id")
    return value


def run_log_dir() -> Path:
    return runlog.default_log_dir()


__all__ = [
    "REPO_DIR", "create_application", "create_notifier", "dry_run",
    "opencode_api_key_available", "preview_payload", "preview_readings",
    "quota_probe_options", "readiness_problems", "require_ready", "run_log_dir",
    "send_payload", "status_lines",
]
