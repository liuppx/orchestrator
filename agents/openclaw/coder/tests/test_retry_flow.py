import tempfile
import unittest
from pathlib import Path

import src.db as db_module
from src.scheduler import session_allows_feishu_followup


class RetryFlowTests(unittest.TestCase):
    def test_failed_session_allows_followup(self) -> None:
        self.assertTrue(session_allows_feishu_followup("failed"))
        self.assertTrue(session_allows_feishu_followup("bound"))
        self.assertTrue(session_allows_feishu_followup("waiting_confirm"))
        self.assertFalse(session_allows_feishu_followup("queued"))
        self.assertFalse(session_allows_feishu_followup("running"))
        self.assertFalse(session_allows_feishu_followup("done"))

    def test_confirmed_binding_still_scanned_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"db_path": str(Path(tmpdir) / "issue_bot.db")}
            db_module.init_db(config)
            db_module.upsert_feishu_binding(
                config,
                chat_id="oc_test",
                thread_id="omt_test",
                repo_full_name="yeying-community/deployer",
                issue_number=95,
                session_key="gh-yeying-community-deployer-issue-95",
                binding_state="confirmed",
                note="retry ready",
                root_message_id="om_root",
                prompt_message_id="om_prompt",
                last_seen_message_id="om_last",
                last_seen_message_time="1",
                confirm_message_id="om_confirm",
                confirm_message_time="1",
                created_at="2026-05-09T00:00:00Z",
                updated_at="2026-05-09T00:00:00Z",
            )

            rows = db_module.fetch_waiting_feishu_bindings(config)

            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0]["binding_state"]), "confirmed")


if __name__ == "__main__":
    unittest.main()
