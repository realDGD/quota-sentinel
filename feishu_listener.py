# Runtime: uv-managed PROJECT environment (pyproject.toml + uv.lock are the
# single dependency source of truth; this script deliberately carries NO
# PEP 723 block). The LaunchAgent starts it with
#   uv run --project <repo> --frozen --no-sync python feishu_listener.py
# so the daemon can never re-resolve the lock or mutate its own environment;
# install-launchagents.sh performs `uv sync --locked` as the setup phase.
# In-process this host also runs task_orchestrator (the "when" layer).

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

import lark_oapi as lark
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
SCRIPT_PATH = str(Path(__file__).resolve().parent / "quota-sentinel.sh")
LOG_DIR = Path(SCRIPT_PATH).parent / "logs"
AUTHORIZED_USER_ID: str | None = None
TASK_ORCHESTRATOR: TaskOrchestrator | None = None

# Outer bound for one /usage subprocess. Must stay above the shell-side
# worst case including child kill grace: lock 20s + Native Codex ~15s +
# CodexBar Codex 2x(20+10)s + Native agy ~21s + CodexBar agy (35+10)s +
# Native opencode ~16s + CodexBar opencode (20+10)s ≈ 207s; Feishu auth 45s
# + send 3x45s + retry delays ≈ 183s. The outer bound includes both
# acquisition and delivery. No cadence changes.
USAGE_COMMAND_TIMEOUT_SECONDS = 480


def read_keychain(service: str) -> str:
    try:
        res = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                service,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to read keychain for {service}: {e}")
        return ""


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


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


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
        process = subprocess.Popen(
            ["/bin/zsh", SCRIPT_PATH, "usage"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        _stdout, stderr = process.communicate(timeout=USAGE_COMMAND_TIMEOUT_SECONDS)
        if process.returncode != 0:
            raise subprocess.CalledProcessError(
                process.returncode,
                ["/bin/zsh", SCRIPT_PATH, "usage"],
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
            f" {(exc.stderr or '').strip()}"
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


def submit_usage_command(sender_id: str, message_id: str) -> bool:
    # The Feishu SDK invokes handlers on its asyncio receive loop. Running the
    # shell synchronously there would block ACKs and WebSocket heartbeats.
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
    future.add_done_callback(lambda _future: command_slot.release())
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
    logger.info("Starting Feishu WebSocket listener...")
    setup_file_logging()
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


if __name__ == "__main__":
    main()
