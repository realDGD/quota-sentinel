# Runs in the PROJECT uv environment (needs lark-oapi, provided by
# pyproject.toml): `uv run --frozen --no-sync python
# tests/feishu-listener-regression.py`. No PEP 723 block — the project
# lock is the single dependency source of truth.

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import feishu_listener


class TestFeishuListener(unittest.TestCase):
    def setUp(self):
        feishu_listener.dedup_cache = feishu_listener.LRUCache()
        feishu_listener.AUTHORIZED_USER_ID = "test-user-123"
        feishu_listener.TASK_ORCHESTRATOR = None

    def tearDown(self):
        feishu_listener.AUTHORIZED_USER_ID = None
        feishu_listener.TASK_ORCHESTRATOR = None

    @patch("feishu_listener.submit_usage_command")
    def test_user_usage_command_triggers_handler(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "user"
        mock_data.event.sender.sender_id.user_id = "test-user-123"
        mock_data.event.message.message_id = "msg-001"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "/usage"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_called_once_with("test-user-123", "msg-001")

    @patch("feishu_listener.submit_usage_command")
    def test_case_insensitive_and_whitespace_usage(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "user"
        mock_data.event.sender.sender_id.user_id = "test-user-123"
        mock_data.event.message.message_id = "msg-002"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "  /USAGE  \n"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_called_once_with("test-user-123", "msg-002")

    @patch("feishu_listener.submit_usage_command")
    def test_bot_app_sender_is_ignored(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "app"
        mock_data.event.sender.sender_id.user_id = "bot-123"
        mock_data.event.message.message_id = "msg-003"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "/usage"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_not_called()

    @patch("feishu_listener.submit_usage_command")
    def test_unauthorized_user_is_ignored(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "user"
        mock_data.event.sender.sender_id.user_id = "different-user"
        mock_data.event.message.message_id = "msg-unauthorized"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "/usage"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_not_called()

    @patch("feishu_listener.submit_usage_command")
    def test_deduplication_prevents_duplicate_processing(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "user"
        mock_data.event.sender.sender_id.user_id = "test-user-123"
        mock_data.event.message.message_id = "msg-dup"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "/usage"})

        feishu_listener.on_message_receive(mock_data)
        feishu_listener.on_message_receive(mock_data)

        self.assertEqual(mock_handler.call_count, 1)

    @patch.object(feishu_listener.command_slot, "acquire", return_value=False)
    def test_busy_command_coalesces_with_inflight_query(self, _mock_acquire):
        self.assertTrue(
            feishu_listener.submit_usage_command("test-user-123", "msg-busy")
        )

    @patch("feishu_listener.submit_usage_command")
    def test_non_usage_text_is_ignored(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "user"
        mock_data.event.sender.sender_id.user_id = "test-user-123"
        mock_data.event.message.message_id = "msg-other"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "Hello bot"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_not_called()

    @patch("feishu_listener.subprocess.Popen")
    def test_usage_is_recorded_by_orchestrator_without_changing_command(
        self, mock_popen
    ):
        process = mock_popen.return_value
        process.communicate.return_value = ("", "")
        process.returncode = 0
        process.poll.return_value = 0
        orchestrator = MagicMock()
        orchestrator.run_external_task.side_effect = (
            lambda _task_name, _trigger, action: action()
        )
        feishu_listener.TASK_ORCHESTRATOR = orchestrator

        feishu_listener.handle_usage_command("test-user-123", "msg-history")

        orchestrator.run_external_task.assert_called_once()
        task_name, trigger, _action = orchestrator.run_external_task.call_args.args
        self.assertEqual(task_name, "usage")
        self.assertEqual(trigger, "feishu:msg-history")
        mock_popen.assert_called_once_with(
            ["/bin/zsh", feishu_listener.SCRIPT_PATH, "usage"],
            stdout=feishu_listener.subprocess.PIPE,
            stderr=feishu_listener.subprocess.PIPE,
            text=True,
            start_new_session=True,
        )


if __name__ == "__main__":
    unittest.main()
