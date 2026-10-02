# Runtime: uv-managed PROJECT environment (pyproject.toml + uv.lock are the
# single dependency source of truth; this script deliberately carries NO
# PEP 723 block). The LaunchAgent starts it with
#   uv run --project <repo> --frozen --no-sync python feishu_listener.py
# so the daemon can never re-resolve the lock or mutate its own environment;
# install-launchagents.sh performs `uv sync --locked` as the setup phase.
# In-process this host also runs task_orchestrator (the "when" layer).
#
# `from __future__ import annotations` keeps the declared floor honest:
# pyproject.toml says >=3.9, and `str | None` in an evaluated annotation is a
# TypeError before 3.10. The daemons only ever run inside the project venv,
# but a module that cannot even be imported on the declared minimum is a
# claim the repository should not make.

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

lark = None

def _load_sdk():
    global lark
    if lark is None:
        import importlib
        lark = importlib.import_module("lark_oapi")
    return lark
from task_orchestrator import TaskOrchestrator, create_default_orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("feishu_listener")

KEYCHAIN_ACCOUNT = "quota-sentinel"
APP_ID_SERVICE = "quota-sentinel.feishu-app-id"
APP_SECRET_SERVICE = "quota-sentinel.feishu-app-secret"
USER_ID_SERVICE = "quota-sentinel.feishu-user-id"
REPO_DIR = Path(__file__).resolve().parent
LOG_DIR = REPO_DIR / "logs"
# /usage is the Python CLI. This daemon already runs inside the project
# environment, so the project interpreter is the whole invocation.
USAGE_COMMAND: tuple[str, ...] = (sys.executable, "-m", "quota_sentinel", "usage")
AUTHORIZED_USER_ID: str | None = None
TASK_ORCHESTRATOR: TaskOrchestrator | None = None

# Outer bound for one /usage subprocess. Must stay above the scheduler-side
# worst case including child kill grace: lock 20s + Native Codex ~15s +
# CodexBar Codex 2x(20+10)s + Native agy ~21s + CodexBar agy (35+10)s +
# Native opencode ~16s + CodexBar opencode (20+10)s ≈ 207s; Feishu auth 45s
# + send 3x45s + retry delays ≈ 183s. The outer bound includes both
# acquisition and delivery. No cadence changes.
from quota_sentinel.runtime.budgets import listener_usage_budget
USAGE_COMMAND_TIMEOUT_SECONDS = listener_usage_budget(os.environ)

# The listener reads its own credentials before it can serve anything. A wedged
# `security` (an unanswered Keychain prompt, stuck IPC) must not hold daemon
# start-up forever: the read is bounded like every other external process, and
# a timeout is reported exactly like a failed read.
KEYCHAIN_READ_TIMEOUT_SECONDS = 10


def read_keychain(
    service: str,
    security_bin: str = "/usr/bin/security",
    timeout: float = KEYCHAIN_READ_TIMEOUT_SECONDS,
) -> str:
    from quota_sentinel.runtime.keychain import read
    return read(service,security_bin=security_bin,timeout=timeout)


def get_credentials() -> tuple[str, str]:
    app_id = os.environ.get("FEISHU_APP_ID") or read_keychain(APP_ID_SERVICE)
    app_secret = os.environ.get("FEISHU_APP_SECRET") or read_keychain(
        APP_SECRET_SERVICE
    )
    if not app_id or not app_secret:
        logger.critical("Feishu App ID or App Secret is missing!")
        sys.exit(1)
    return app_id, app_secret


def get_authorized_user_id() -> str:
    global AUTHORIZED_USER_ID
    if AUTHORIZED_USER_ID is None:
        AUTHORIZED_USER_ID = os.environ.get("FEISHU_USER_ID") or read_keychain(
            USER_ID_SERVICE
        )
    return AUTHORIZED_USER_ID


class LRUCache:
    def __init__(self, maxsize: int = 1000, ttl_seconds: float = 3600):
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self.cache: OrderedDict[str, float] = OrderedDict()

    def add(self, key: str) -> bool:
        now = time.time()
        self.cleanup(now)
        if key in self.cache:
            return False
        self.cache[key] = now
        if len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)
        return True

    def contains(self, key: str) -> bool:
        now = time.time()
        self.cleanup(now)
        return key in self.cache

    def cleanup(self, now: float) -> None:
        while self.cache and now - next(iter(self.cache.values())) > self.ttl:
            self.cache.popitem(last=False)


dedup_cache = LRUCache()
command_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-usage")
command_slot = threading.BoundedSemaphore(1)
command_environment = None
commands_enabled = True
commands_closing = False
command_lock = threading.RLock()
active_processes = set()
active_futures = set()


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    # Reap descendants even when the leader has already returned.
    if isinstance(process.pid, int) and process.pid > 0:
        from task_orchestrator import SubprocessRunner
        SubprocessRunner._terminate_group(process)


def setup_file_logging() -> None:
    """Mirror listener logs into the project logs/ directory next to the
    shell run logs. Best-effort: stdout/stderr logging keeps working."""
    try:
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = LOG_DIR / "listener.log"
        handler = logging.FileHandler(log_path)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
        os.chmod(log_path, 0o600)  # FileHandler follows umask; match shell logs
    except OSError as exc:
        logger.warning(f"File logging disabled: {exc}")


def handle_usage_command(sender_id: str, message_id: str) -> None:
    logger.info(
        f"Triggering usage query for sender {sender_id} (message_id={message_id})"
    )
    process: subprocess.Popen[str] | None = None
    started = time.monotonic()
    elapsed = lambda: f"{time.monotonic() - started:.1f}s"

    def execute_usage() -> None:
        nonlocal process
        with command_lock:
            if commands_closing:
                return
            options = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, start_new_session=True)
            if command_environment is not None:
                options['env'] = dict(command_environment, QUOTA_SENTINEL_REPLY_USER=sender_id)
            process = subprocess.Popen(list(USAGE_COMMAND), **options)
            active_processes.add(process)
        _stdout, stderr = process.communicate(timeout=USAGE_COMMAND_TIMEOUT_SECONDS)
        if process.returncode != 0:
            raise subprocess.CalledProcessError(
                process.returncode,
                list(USAGE_COMMAND),
                stderr=stderr,
            )

    try:
        if TASK_ORCHESTRATOR is not None:
            TASK_ORCHESTRATOR.run_external_task(
                "usage", f"feishu:{message_id or 'unknown'}", execute_usage
            )
        else:
            execute_usage()
        logger.info(
            f"Successfully sent /usage notification for message_id={message_id}"
            f" (elapsed={elapsed()})"
        )
    except subprocess.CalledProcessError as exc:
        logger.error(
            f"Usage command returned {exc.returncode} after {elapsed()}:"
            " (details omitted to keep credentials private)"
        )
    except subprocess.TimeoutExpired:
        logger.error(
            f"Usage command timed out for message_id={message_id} after"
            f" {USAGE_COMMAND_TIMEOUT_SECONDS}s (elapsed={elapsed()})"
        )
    except Exception as e:
        logger.error(f"Error executing usage command: {e}")
    finally:
        if process is not None:
            terminate_process_group(process)
            with command_lock:
                active_processes.discard(process)


def submit_usage_command(sender_id: str, message_id: str) -> bool:
    # The Feishu SDK invokes handlers on its asyncio receive loop. Running the
    # scheduler synchronously there would block ACKs and WebSocket heartbeats.
    if commands_closing or not commands_enabled:
        return False
    if not command_slot.acquire(blocking=False):
        # Only the configured recipient can reach this path, and all results go
        # to that same private chat. Let the in-flight result satisfy repeated
        # commands instead of queueing duplicate quota probes.
        logger.info("Coalescing /usage with the query already in progress")
        return True
    try:
        future: Future[None] = command_executor.submit(
            handle_usage_command, sender_id, message_id
        )
    except Exception:
        command_slot.release()
        raise
    with command_lock:
        active_futures.add(future)
    def finished(done):
        with command_lock:
            active_futures.discard(done)
        command_slot.release()
    future.add_done_callback(finished)
    return True


def on_message_receive(data: lark.api.im.v1.P2ImMessageReceiveV1) -> None:
    try:
        event = data.event
        if not event:
            return

        message = event.message
        sender = event.sender
        if not message or not sender:
            return

        # 1. Filter out bot/app messages to prevent infinite reply loop
        sender_type = getattr(sender, "sender_type", None)
        if sender_type != "user":
            logger.debug(f"Ignoring message from non-user sender_type: {sender_type}")
            return

        sender_id_obj = getattr(sender, "sender_id", None)
        sender_id = getattr(sender_id_obj, "user_id", "")
        if not sender_id or sender_id != get_authorized_user_id():
            logger.warning(
                f"Ignoring command from unauthorized user {sender_id or 'unknown'}"
            )
            return

        # 2. Check message type and content
        msg_type = getattr(message, "message_type", "")
        if msg_type != "text":
            return

        content_raw = getattr(message, "content", "")
        if not content_raw:
            return

        try:
            content_json = json.loads(content_raw)
            text = content_json.get("text", "").strip()
        except Exception:
            text = content_raw.strip()

        # Check for /usage command (case-insensitive)
        if text.lower() == "/usage":
            message_id = getattr(message, "message_id", "")
            if message_id and dedup_cache.contains(message_id):
                logger.debug(f"Ignoring duplicate message_id: {message_id}")
                return
            logger.info(f"Received /usage command from user {sender_id}")
            if submit_usage_command(sender_id, message_id) and message_id:
                dedup_cache.add(message_id)
        else:
            logger.debug(f"Ignored non-usage text command: {text}")

    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)


def main() -> None:
    global TASK_ORCHESTRATOR
    if os.environ.get('QUOTA_SENTINEL_CONFIG'):
        from quota_sentinel.__main__ import main as cli_main
        raise SystemExit(cli_main(['serve']))
    logger.info("Starting Feishu WebSocket listener...")
    setup_file_logging()
    _load_sdk()
    app_id, app_secret = get_credentials()
    if not get_authorized_user_id():
        logger.critical("Authorized Feishu user ID is missing!")
        sys.exit(1)

    orchestrator_enabled = os.environ.get("QUOTA_SENTINEL_ORCHESTRATOR_ENABLED", "1") != "0"
    if orchestrator_enabled:
        TASK_ORCHESTRATOR = create_default_orchestrator(task_logger=logger)
        TASK_ORCHESTRATOR.start()
        logger.info("Local task orchestrator started")
    else:
        logger.warning("Local task orchestrator disabled by environment")

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )

    client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=event_handler,
        # The SDK INFO message prints its WebSocket URL, including ephemeral
        # access_key/ticket query parameters. Keep SDK output at ERROR while
        # retaining our own command lifecycle logs above.
        log_level=lark.LogLevel.ERROR,
        auto_reconnect=True,
    )

    def stop_for_signal(signum: int, _frame: object) -> None:
        logger.info(f"Listener received signal {signum}; stopping orchestrator")
        if TASK_ORCHESTRATOR is not None:
            TASK_ORCHESTRATOR.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_for_signal)

    try:
        client.start()
    except KeyboardInterrupt:
        logger.info("Feishu WebSocket listener stopped by user.")
    except Exception as e:
        logger.critical(f"Feishu WebSocket client failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if TASK_ORCHESTRATOR is not None:
            TASK_ORCHESTRATOR.stop()


class FeishuListener:
    """Listener only: the host supplies and owns its scheduler reference."""
    def __init__(self, config, state_dir, config_path, scheduler):
        self.config = config
        self.state_dir, self.config_path = Path(state_dir), Path(config_path)
        self.scheduler = scheduler
        self.client = None
        self._sdk_tasks = set()

    def run(self):
        global TASK_ORCHESTRATOR, USAGE_COMMAND, USAGE_COMMAND_TIMEOUT_SECONDS
        global command_environment, commands_enabled, commands_closing, command_executor
        global AUTHORIZED_USER_ID
        import asyncio
        sdk = _load_sdk()
        from quota_sentinel.runtime.feishu import SelectedCredentials
        credentials=SelectedCredentials(self.config.credentials,timeout=self.config.budgets['credentials']['timeout'])
        app_id, app_secret = credentials.get('app_id'), credentials.get('app_secret')
        if not app_id or not app_secret:raise RuntimeError('Feishu App ID or App Secret is missing')
        AUTHORIZED_USER_ID=credentials.get('user_id')
        if not AUTHORIZED_USER_ID:
            raise RuntimeError('Authorized Feishu user ID is missing')
        TASK_ORCHESTRATOR = self.scheduler
        commands_enabled = self.config.features.quota_queries
        commands_closing = False
        command_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='feishu-usage')
        command_environment = dict(os.environ, QUOTA_SENTINEL_CONFIG=str(self.config_path))
        USAGE_COMMAND = (sys.executable, '-m', 'quota_sentinel', '--state-dir',
                         str(self.state_dir), '--config', str(self.config_path), 'usage')
        USAGE_COMMAND_TIMEOUT_SECONDS = listener_usage_budget(command_environment, config=self.config)
        handler = sdk.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(on_message_receive).build()
        self.client = sdk.ws.Client(app_id=app_id, app_secret=app_secret,
            event_handler=handler, log_level=sdk.LogLevel.ERROR, auto_reconnect=True)
        from lark_oapi.ws.client import loop
        self._before_tasks = asyncio.all_tasks(loop)
        try:
            self.client.start()
        finally:
            self._sdk_tasks = asyncio.all_tasks(loop) - self._before_tasks

    def stop(self):
        global commands_closing, TASK_ORCHESTRATOR
        with command_lock:
            commands_closing = True
            processes = tuple(active_processes)
        for process in processes:
            terminate_process_group(process)
        command_executor.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + 6
        while True:
            with command_lock:
                futures = tuple(active_futures)
            if not futures:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('listener command worker did not stop')
            time.sleep(0.05)
        if self.client is not None:
            import asyncio
            from lark_oapi.ws.client import loop
            for task in self._sdk_tasks:
                task.cancel()
            async def disconnect():
                await asyncio.wait_for(self.client._disconnect(), timeout=2)
                if self._sdk_tasks:
                    await asyncio.wait(self._sdk_tasks, timeout=2)
            try:
                loop.run_until_complete(disconnect())
            except (asyncio.TimeoutError, OSError):
                logger.error('WebSocket cleanup exceeded its deadline')
        TASK_ORCHESTRATOR = None


if __name__ == "__main__":
    main()
