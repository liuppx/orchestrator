import unittest

from src.clients.feishu_client import (
    message_matches_confirm_keywords,
    normalize_confirm_text,
    parse_feishu_message_text,
    resolve_feishu_runtime_settings,
)


class FeishuSettingsTests(unittest.TestCase):
    def test_resolve_feishu_runtime_settings_merges_multiple_chat_ids(self) -> None:
        config = {
            "feishu_app_id": "app-id",
            "feishu_app_secret": "secret",
            "feishu_handoff_chat_id": "oc_group_b",
            "feishu_handoff_chat_ids": ["oc_group_c", "oc_group_a"],
        }

        settings = resolve_feishu_runtime_settings(config)

        self.assertEqual(settings["chat_id"], "oc_group_b")
        self.assertEqual(settings["chat_ids"], ["oc_group_b", "oc_group_c", "oc_group_a"])

    def test_normalize_confirm_text_strips_leading_feishu_mentions(self) -> None:
        self.assertEqual(normalize_confirm_text("@_user_1 /run"), "/run")
        self.assertEqual(normalize_confirm_text(" @_user_1   @_user_2   /issue agent "), "/issue agent")
        self.assertEqual(normalize_confirm_text("@_user_1/issue chat"), "/issue chat")

    def test_message_matches_confirm_keywords_accepts_inline_slash_command(self) -> None:
        keywords = ["/run", "开始执行", "确认执行", "可以执行"]

        self.assertTrue(message_matches_confirm_keywords("直接执行方案1，/run", keywords))
        self.assertTrue(message_matches_confirm_keywords("@_user_1 直接执行方案1，/run", keywords))
        self.assertFalse(message_matches_confirm_keywords("直接执行方案1，/runner", keywords))

    def test_message_matches_confirm_keywords_accepts_natural_plan_confirmation(self) -> None:
        keywords = ["/run", "开始执行", "确认执行", "可以执行"]

        self.assertTrue(message_matches_confirm_keywords("方案1", keywords))
        self.assertTrue(message_matches_confirm_keywords("执行方案1", keywords))
        self.assertTrue(message_matches_confirm_keywords("采用方案一", keywords))
        self.assertTrue(message_matches_confirm_keywords("@_user_1 按方案1做", keywords))
        self.assertTrue(message_matches_confirm_keywords("方案1可以执行", keywords))
        self.assertFalse(message_matches_confirm_keywords("方案1有哪些风险？", keywords))
        self.assertFalse(message_matches_confirm_keywords("先讨论一下方案1", keywords))

    def test_parse_feishu_post_message_text_flattens_rich_text(self) -> None:
        item = {
            "msg_type": "post",
            "body": {
                "content": (
                    '{"zh_cn":{"title":"","content":['
                    '  [{"tag":"text","text":"第一行"}],'
                    '  [{"tag":"a","text":"#12","href":"https://github.com/example/issues/12"},'
                    '   {"tag":"text","text":" | 第二行"},{"tag":"text","text":"补充"}]'
                    " ]}}"
                )
            },
        }

        self.assertEqual(parse_feishu_message_text(item), "第一行\n#12 | 第二行补充")


if __name__ == "__main__":
    unittest.main()
