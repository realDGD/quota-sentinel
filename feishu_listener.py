# /// script
# dependencies = [
#   "lark-oapi>=1.4.0",
# ]
# ///

import json
import logging
import os
import subprocess
import sys
import time
from collections import OrderedDict

import lark_oapi as lark

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("feishu_listener")

KEYCHAIN_ACCOUNT = "quota-sentinel"
APP_ID_SERVICE = "com.example.quota-sentinel.feishu-app-id"
APP_SECRET_SERVICE = "com.example.quota-sentinel.feishu-app-secret"
SCRIPT_PATH = "/Users/__USER__/code/quota-sentinel/quota-sentinel.sh"


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

    def cleanup(self, now: float) -> None:
        while self.cache and now - next(iter(self.cache.values())) > self.ttl:
            self.cache.popitem(last=False)


dedup_cache = LRUCache()


def handle_usage_command(sender_id: str, message_id: str) -> None:
    logger.info(
        f"Triggering usage query for sender {sender_id} (message_id={message_id})"
    )
    try:
        result = subprocess.run(
            ["/bin/zsh", SCRIPT_PATH, "usage"],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode == 0:
            logger.info(
                f"Successfully sent /usage notification for message_id={message_id}"
            )
        else:
            logger.error(
                f"Usage command returned {result.returncode}: {result.stderr.strip()}"
            )
    except Exception as e:
        logger.error(f"Error executing usage command: {e}")


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

        message_id = getattr(message, "message_id", "")
        if message_id and not dedup_cache.add(message_id):
            logger.debug(f"Ignoring duplicate message_id: {message_id}")
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
            sender_id_obj = getattr(sender, "sender_id", None)
            sender_id = getattr(sender_id_obj, "user_id", "") or "unknown"
            logger.info(f"Received /usage command from user {sender_id}")
            handle_usage_command(sender_id, message_id)
        else:
            logger.debug(f"Ignored non-usage text command: {text}")

    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)


def main() -> None:
    logger.info("Starting Feishu WebSocket listener...")
    app_id, app_secret = get_credentials()

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )

    client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )

    try:
        client.start()
    except KeyboardInterrupt:
        logger.info("Feishu WebSocket listener stopped by user.")
    except Exception as e:
        logger.critical(f"Feishu WebSocket client failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
