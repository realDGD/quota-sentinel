# /// script
# dependencies = [
#   "lark-oapi>=1.4.0",
# ]
# ///

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

    @patch("feishu_listener.handle_usage_command")
    def test_user_usage_command_triggers_handler(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "user"
        mock_data.event.sender.sender_id.user_id = "test-user-123"
        mock_data.event.message.message_id = "msg-001"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "/usage"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_called_once_with("test-user-123", "msg-001")

    @patch("feishu_listener.handle_usage_command")
    def test_case_insensitive_and_whitespace_usage(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "user"
        mock_data.event.sender.sender_id.user_id = "test-user-123"
        mock_data.event.message.message_id = "msg-002"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "  /USAGE  \n"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_called_once_with("test-user-123", "msg-002")

    @patch("feishu_listener.handle_usage_command")
    def test_bot_app_sender_is_ignored(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "app"
        mock_data.event.sender.sender_id.user_id = "bot-123"
        mock_data.event.message.message_id = "msg-003"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "/usage"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_not_called()

    @patch("feishu_listener.handle_usage_command")
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

    @patch("feishu_listener.handle_usage_command")
    def test_non_usage_text_is_ignored(self, mock_handler):
        mock_data = MagicMock()
        mock_data.event.sender.sender_type = "user"
        mock_data.event.sender.sender_id.user_id = "test-user-123"
        mock_data.event.message.message_id = "msg-other"
        mock_data.event.message.message_type = "text"
        mock_data.event.message.content = json.dumps({"text": "Hello bot"})

        feishu_listener.on_message_receive(mock_data)

        mock_handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
