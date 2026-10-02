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
import os
import time
from pathlib import Path
from typing import (
    Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence,
)

from quota_sentinel.app import AppConfig, Application
from quota_sentinel.quota.adapters import PROVIDERS, adapter_for
from quota_sentinel.runtime import keychain, probe_budget, runlog
from quota_sentinel.runtime.cards import (
    format_reset_time, render_progress_test_card, render_task_card,
    render_usage_card,
)
from quota_sentinel.runtime.agy_exec import (
    AGY_PROVIDER, AgyExecConfig, AgyExecRunner,
)
from quota_sentinel.runtime.codex_exec import (
    CODEX_PROVIDER, CodexExecConfig, CodexExecRunner,
)
from quota_sentinel.runtime.direct import (
    DIRECT_PROVIDERS, DIRECT_TIMEOUT_SECONDS, DirectRunner,
)
from quota_sentinel.runtime.dispatch import TransportRouter
from quota_sentinel.runtime.feishu import (
    FeishuClient, FeishuError, FeishuNotifier, KeychainCredentials,
)
from quota_sentinel.runtime.models import PI_PROVIDERS, ModelRunner, ModelRunnerConfig
from quota_sentinel.runtime.quota_probe import QuotaCollector
from quota_sentinel.scheduler import service
from quota_sentinel.state.runlock import SHLOCK_BIN

REPO_DIR = Path(__file__).resolve().parents[2]
OPENCODE_API_KEY_SERVICE = "quota-sentinel.opencode-go-api-key"
CLINEPASS_API_KEY_SERVICE = "quota-sentinel.clinepass-api-key"
DRY_RUN_ENV = "FEISHU_DRY_RUN"
TRANSPORT_ENV = "QUOTA_SENTINEL_TRANSPORT"
DEFAULT_CURL_BIN = Path("/usr/bin/curl")
TRANSPORTS = ("pi", "direct", "codex", "agy")

# Which providers each transport can actually SERVE — the capability the
# runners themselves declare, read here instead of re-typed:
#   * `pi` serves exactly the providers declared by its model runner;
#   * `direct` serves exactly the providers in its own roster;
#   * the codex CLI serves only CODEX_PROVIDER, the agy CLI only AGY_PROVIDER
#     (both refuse anything else in `prepare`/`run`).
# This map exists so an operator override can be refused at the configuration
# entry instead of aborting a run that has already started: the mismatch would
# otherwise surface as a `ValueError` from the runner mid-burst.
TRANSPORT_PROVIDERS: Dict[str, FrozenSet[str]] = {
    "pi": PI_PROVIDERS,
    "direct": frozenset(DIRECT_PROVIDERS),
    "codex": frozenset({CODEX_PROVIDER}),
    "agy": frozenset({AGY_PROVIDER}),
}


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
    """Read an optional budget without allowing an invalid duration.

    The tolerance — unset/empty/non-numeric/non-finite/negative means the
    default, and zero only where `allow_zero` says so — lives in
    ``runtime/probe_budget.py`` now, together with the eight probe budgets that
    also derive the orchestrator's per-phase bound. This wrapper stays because
    the direct runner's timeout (``QUOTA_SENTINEL_DIRECT_TIMEOUT``) is the same
    ``${VAR:-default}`` shape without being part of the probe table.
    """
    return probe_budget.seconds_override(env, name, default, allow_zero=allow_zero)


def quota_probe_options(environment: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The same overridable boundaries the shell exposed to its suites.

    The eight budget options are NOT typed here any more: they come from
    ``runtime/probe_budget.py``, the module that also derives the outer
    ``check`` bound from them. When both halves owned a copy, an operator could
    raise a probe timeout that the derived bound did not know about — and the
    watchdog would kill a probe still inside its own rules.
    """
    env = _env(environment)
    options: Dict[str, Any] = {
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
        "clinepass_usage_helper": _path_override(
            env, "QUOTA_SENTINEL_CLINEPASS_USAGE_HELPER",
            REPO_DIR / "clinepass_usage.py",
        ),
        "curl_bin": _path_override(env, "QUOTA_SENTINEL_CURL_BIN", DEFAULT_CURL_BIN),
    }
    options.update(probe_budget.quota_probe_timeouts(env))
    return options


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


def transport_for(provider: str) -> str:
    """How one provider's task is delivered: "pi", "direct", "codex" or "agy"."""
    return adapter_for(provider).transport


def supported_transports(provider: str) -> List[str]:
    """The transports that can actually deliver `provider`, sorted.

    Read from ``TRANSPORT_PROVIDERS``, so this answer and the refusal below can
    never disagree with what the runners themselves accept.
    """
    return sorted(
        name for name in TRANSPORTS if provider in TRANSPORT_PROVIDERS[name]
    )


def provider_transports(environment: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The declared transports, with an operator override for A/B runs.

    ``QUOTA_SENTINEL_TRANSPORT="opencode=pi"`` (comma-separated) moves one
    provider back onto the Pi agent without editing code, so a transport change
    can be compared against the path it replaced and reverted by unsetting one
    variable.

    TOLERANCE, and its limit. A name that is simply unknown — a provider that
    is not in the roster, a transport that does not exist — is ignored rather
    than fatal: this is a diagnostic seam, not a configuration file, and a
    half-typed line must not take a scheduler tick down. A pair whose two names
    are both real but that the chosen transport cannot serve is a different
    thing entirely: ``opencode=agy`` would be accepted here and then abort the
    whole run from ``AgyExecRunner.prepare`` once the operator had already
    committed to it, so it is refused NOW, at the configuration entry, before
    any state, lock, credential or model work. The refusal names the provider,
    the rejected transport and the transports that would work instead.
    """
    env = _env(environment)
    mapping = {provider: transport_for(provider) for provider in PROVIDERS}
    for item in env.get(TRANSPORT_ENV, "").split(","):
        provider, separator, transport = item.partition("=")
        provider, transport = provider.strip(), transport.strip()
        if not separator or provider not in mapping or transport not in TRANSPORTS:
            # Unknown provider or unknown transport: documented as ignored.
            continue
        if provider not in TRANSPORT_PROVIDERS[transport]:
            raise ValueError(
                "provider %r does not run on the %r transport, which serves "
                "only: %s; %s supports: %s"
                % (provider, transport,
                   ", ".join(sorted(TRANSPORT_PROVIDERS[transport])) or "nothing",
                   provider, ", ".join(supported_transports(provider)) or "nothing")
            )
        mapping[provider] = transport
    return mapping


def create_application(
    state_dir: Path, *, environment: Optional[Mapping[str, str]] = None,
    dry_run_flag: Optional[bool] = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    config: Optional[AppConfig] = None,
    software_config=None, runtime_plan=None,
) -> Application:
    env = _env(environment)
    state_dir = Path(state_dir)
    if runtime_plan is not None:
        from .selected_factory import create_selected_application
        return create_selected_application(state_dir, software_config, runtime_plan, environment=env, clock=clock, sleep=sleep, dry_run_flag=dry_run_flag)
    # FIRST, before any config object, runner, workspace or state work: an
    # override that names a transport which cannot serve the provider is a
    # configuration error, and it must be reported as one (the CLI turns the
    # ValueError into `quota_sentinel: invalid argument: ...`, exit 3) instead
    # of aborting a run after the operator committed to it.
    transports = provider_transports(env)
    options = quota_probe_options(env)
    logger = logging.getLogger("quota_sentinel.model").info
    # Two transports, one surface. The Pi runner's per-attempt lines carry
    # phase/attempt/result/elapsed and a REDACTED stderr summary; without this
    # seam a failed attempt would leave no trace at all in the operator's run
    # log. The direct runner speaks the same lines, so one log covers both.
    #
    # Pi and Codex are each other's fallback, but the chain is always ONE hop
    # deep. Two terminal instances exist for exactly that reason: a runner can
    # hand an attempt to its counterpart, and a counterpart that has just taken
    # an attempt can never hand it back.
    codex_config = CodexExecConfig.from_env(env, state_dir=state_dir)
    agy_config = AgyExecConfig.from_env(env, state_dir=state_dir)
    pi_terminal = ModelRunner(ModelRunnerConfig.from_env(env), logger=logger)
    codex_terminal = CodexExecRunner(
        codex_config, logger=logging.getLogger("quota_sentinel.codex").info
    )
    agy_terminal = AgyExecRunner(
        agy_config, logger=logging.getLogger("quota_sentinel.agy").info
    )
    # The shipped priority: Pi first (54 tokens per ignition), the official CLI
    # as the transport that takes over when Pi cannot deliver (~1.7k).
    pi_runner = ModelRunner(
        ModelRunnerConfig.from_env(env), logger=logger,
        fallback_for={"codex": codex_terminal},
    )
    direct_runner = DirectRunner(
        curl_bin=options["curl_bin"],
        timeout=_seconds_override(
            env, "QUOTA_SENTINEL_DIRECT_TIMEOUT", DIRECT_TIMEOUT_SECONDS
        ),
        logger=logging.getLogger("quota_sentinel.direct").info,
        environment=env,
    )
    # Selecting the codex transport inverts the priority for A/B runs, and
    # keeps its own fallback for a FUNCTIONAL failure: when the minimal profile
    # stops applying (a renamed feature flag, a server-side model change) the
    # attempt is delivered by Pi instead of quietly costing five times as much.
    # A COST regression is a different thing and is NOT re-delivered: a turn
    # that completed and merely reports more tokens than the minimal profile
    # allows is ACCEPTED as the delivery it already is — `CodexExecRunner`
    # logs it as `cost-regression` and returns success, because Pi has nothing
    # left to deliver and re-running the work would spend the quota twice.
    codex_runner = CodexExecRunner(
        codex_config, logger=logging.getLogger("quota_sentinel.codex").info,
        fallback=pi_terminal,
    )
    # Antigravity's shipped priority runs the other way: the official CLI with
    # its minimal agent is the primary path (~564 input tokens per ignition
    # against the stock agent's ~22,311), and Pi takes the attempt over when
    # that profile stops applying — a renamed frontmatter key or a dropped
    # --agent would otherwise cost 40x without failing.
    agy_runner = AgyExecRunner(
        agy_config, logger=logging.getLogger("quota_sentinel.agy").info,
        fallback=pi_terminal,
    )
    runner = TransportRouter(
        {"pi": pi_runner, "direct": direct_runner, "codex": codex_runner,
         "agy": agy_runner},
        transports,
    )
    notifier = create_notifier(environment=env, dry_run_flag=dry_run_flag)

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

def _provider_hooks(provider: str, config: ModelRunnerConfig, transport: str) -> List[Path]:
    if transport != "pi":
        # A direct provider never starts Pi, so neither the capture extension
        # nor Pi's provider extension is part of its delivery path; requiring
        # a file this deployment does not use would refuse runs that work.
        return []
    hooks = [] if provider == "clinepass" else [REPO_DIR / ("capture-%s-quota.ts" % provider)]
    if provider == "antigravity":
        # Pi loads this provider extension for antigravity runs; a model
        # attempt without it fails after the timeout, not before.
        hooks.append(config.antigravity_provider_extension)
    return hooks


def _auth_has_provider(path: Path, provider: str) -> bool:
    key = {"codex": "openai-codex", "antigravity": "antigravity",
           "opencode": "opencode-go", "clinepass": "clinepass"}[provider]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(document, dict) and key in document


def direct_api_key_available(
    provider: str, *, environment: Optional[Mapping[str, str]] = None
) -> bool:
    """Whether a direct provider's key is reachable, env var or Keychain.

    A missing key is reported by `status` and fails the provider's own attempt
    in milliseconds; it deliberately does not refuse the whole roster, which is
    how the port treated the OpenCode key before the direct transport existed.
    """
    env = _env(environment)
    spec = DIRECT_PROVIDERS[provider]
    if spec.env_key and env.get(spec.env_key):
        return True
    return keychain.present(spec.key_service, environment=env)


def opencode_api_key_available(
    *, environment: Optional[Mapping[str, str]] = None
) -> bool:
    return direct_api_key_available("opencode", environment=environment)


def clinepass_api_key_available(
    *, environment: Optional[Mapping[str, str]] = None
) -> bool:
    return direct_api_key_available("clinepass", environment=environment)


def readiness_problems(
    state_dir: Path, *, environment: Optional[Mapping[str, str]] = None,
    providers: Sequence[str] = PROVIDERS,
) -> List[str]:
    env = _env(environment)
    problems: List[str] = []
    config = ModelRunnerConfig.from_env(env)
    transports = provider_transports(env)

    # Only a transport that is actually reachable may demand its binaries and
    # credentials; a direct provider whose key is missing fails in
    # milliseconds inside its own attempt instead, without spending a single
    # token, so it is reported by `status` rather than refusing the whole
    # roster. The codex transport is the exception that proves the rule: it
    # falls back to Pi, so Pi's prerequisites are checked for it too — a
    # fallback that cannot run is not a fallback, it is a second failure
    # discovered late.
    pi_providers = [
        provider for provider in providers
        if provider in PROVIDERS and transports.get(provider) == "pi"
    ]
    # The codex transport runs OpenAI's own CLI, so it needs that binary and
    # the CLI's own credential — not Pi's. Checking here is what keeps a
    # missing credential a startup problem instead of a failed attempt that
    # already cost the fallback transport a model turn.
    codex_providers = [
        provider for provider in providers
        if provider in PROVIDERS and transports.get(provider) == "codex"
    ]
    # The agy transport runs Google's own CLI and resolves its own sign-in from
    # the machine's keyring, so it demands no credential path of ours — only
    # the binary, checked below.
    agy_providers = [
        provider for provider in providers
        if provider in PROVIDERS and transports.get(provider) == "agy"
    ]
    codex_config = CodexExecConfig.from_env(env, state_dir=state_dir)
    agy_config = AgyExecConfig.from_env(env, state_dir=state_dir)
    # Either direction of the priority chain needs BOTH clients: Pi and the
    # official CLI are each other's fallback for the codex provider, and agy's
    # fallback is Pi for the antigravity one — a fallback that cannot run is not
    # a fallback, it is a second failure discovered late.
    pi_needed = sorted(set(pi_providers) | set(codex_providers) | set(agy_providers))
    codex_needed = sorted(set(codex_providers) | {p for p in pi_providers if p == "codex"})
    pi_providers = pi_needed

    executables = {
        "curl": quota_probe_options(env)["curl_bin"],
        "security": Path(keychain.SECURITY_BIN),
        "shlock": Path(SHLOCK_BIN),
    }
    if pi_needed:
        executables["pi"] = config.pi_bin
    if codex_needed:
        executables["codex"] = codex_config.codex_bin
    if agy_providers:
        executables["agy"] = agy_config.agy_bin
    for name, path in executables.items():
        if not os.access(path, os.X_OK):
            problems.append("%s is not executable: %s" % (name, path))

    for provider in pi_providers:
        if provider == "clinepass":
            from .pi_plugins import plugin_entry, plugin_guidance
            entry = plugin_entry(provider, config)
            if entry is None or not entry.is_file():
                problems.append("missing ClinePass Pi plugin: " + "; ".join(plugin_guidance(provider)))
    for provider in providers:
        if provider not in PROVIDERS:
            problems.append("unknown provider: %s" % provider)

    if pi_providers:
        if not os.access(config.auth_file, os.R_OK):
            problems.append("Pi OAuth credential is not readable: %s" % config.auth_file)
        else:
            for provider in pi_providers:
                if not _auth_has_provider(config.auth_file, provider):
                    problems.append("Pi credential for %s is missing" % provider)

    if codex_needed:
        codex_auth = Path(codex_config.codex_home) / "auth.json"
        if not os.access(codex_auth, os.R_OK):
            problems.append("Codex CLI credential is not readable: %s" % codex_auth)

    for provider in providers:
        if provider not in PROVIDERS:
            continue
        for hook in _provider_hooks(provider, config, transports.get(provider, "pi")):
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
    transports = provider_transports(env)
    if opencode_api_key_available(environment=env):
        lines.append("opencode api key: configured")
    else:
        lines.append(
            "opencode api key: missing (%s); Native tier disabled"
            % OPENCODE_API_KEY_SERVICE
        )
    if clinepass_api_key_available(environment=env):
        lines.append("clinepass api key: configured")
    else:
        lines.append(
            "clinepass api key: missing (%s); CodexBar tier only"
            % CLINEPASS_API_KEY_SERVICE
        )
    for provider in PROVIDERS:
        lines.append("transport %s: %s" % (provider, transports.get(provider, "pi")))

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
        "clinepass": {
            "source": "CodexBar · api", "fresh": True,
            "capturedAt": now,
            "fiveHour": window(91, now + 17280),
            "weekly": window(97, now + 590400),
            "monthly": window(99, now + 2566800),
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
    elif mode == "clinepass":
        members = ("clinepass",)
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
    "REPO_DIR", "create_application", "create_notifier", "direct_api_key_available",
    "dry_run", "clinepass_api_key_available", "opencode_api_key_available",
    "preview_payload", "preview_readings", "provider_transports",
    "quota_probe_options", "readiness_problems", "require_ready", "run_log_dir",
    "send_payload", "status_lines", "transport_for",
]
