from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIB_ROOT = PLUGIN_ROOT / "lib"
sys.path.insert(0, str(LIB_ROOT))

from tkn_codex_context.chat_logs import (  # noqa: E402
    normalize_path_text,
    path_is_within,
    read_session,
)


class ChatLogCompatibilityTests(unittest.TestCase):
    def test_wsl_mount_path_matches_windows_path(self) -> None:
        self.assertEqual(
            normalize_path_text("/mnt/c/path/to/example"),
            normalize_path_text(r"C:\path\to\example"),
        )
        self.assertTrue(
            path_is_within(
                "/mnt/d/path/to/example/subfolder",
                r"D:\path\to\example",
            )
        )

    def test_reads_legacy_top_level_session_and_message_records(self) -> None:
        session = read_session(FIXTURES / "chat-legacy-session.jsonl")

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual("legacy-thread-0001", session.id)
        self.assertEqual("2025-08-24T10:45:58.916Z", session.timestamp)
        self.assertEqual(
            "https://example.invalid/example/project.git",
            session.repository_url,
        )
        self.assertEqual(
            ["Rebuild this project context."],
            [message.text for message in session.user_messages],
        )
        self.assertEqual(
            ["The project context was reviewed."],
            [message.text for message in session.assistant_messages],
        )


if __name__ == "__main__":
    unittest.main()
