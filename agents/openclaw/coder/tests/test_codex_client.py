import unittest

from src.clients.codex_client import (
    parse_codex_jsonl_error_messages,
    parse_codex_jsonl_events,
    parse_codex_jsonl_usage,
)


class CodexClientTests(unittest.TestCase):
    def test_parse_codex_jsonl_events_extracts_thread_and_last_message(self) -> None:
        thread_id, text = parse_codex_jsonl_events(
            "\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread-123"}',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"second"}}',
                ]
            )
        )

        self.assertEqual(thread_id, "thread-123")
        self.assertEqual(text, "second")

    def test_parse_codex_jsonl_usage_prefers_last_token_usage(self) -> None:
        usage = parse_codex_jsonl_usage(
            "\n".join(
                [
                    '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":100,"output_tokens":10,"total_tokens":110},"last_token_usage":{"input_tokens":40,"cached_input_tokens":5,"output_tokens":7,"reasoning_output_tokens":3,"total_tokens":55}}}}',
                ]
            )
        )

        self.assertEqual(
            usage,
            {
                "input_tokens": 40,
                "cached_input_tokens": 5,
                "output_tokens": 7,
                "reasoning_output_tokens": 3,
                "total_tokens": 55,
            },
        )

    def test_parse_codex_jsonl_usage_falls_back_to_total_token_usage(self) -> None:
        usage = parse_codex_jsonl_usage(
            "\n".join(
                [
                    '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":12,"output_tokens":8,"total_tokens":20}}}}',
                ]
            )
        )

        self.assertEqual(
            usage,
            {
                "input_tokens": 12,
                "cached_input_tokens": 0,
                "output_tokens": 8,
                "reasoning_output_tokens": 0,
                "total_tokens": 20,
            },
        )

    def test_parse_codex_jsonl_error_messages_collects_runtime_errors(self) -> None:
        messages = parse_codex_jsonl_error_messages(
            "\n".join(
                [
                    '{"type":"error","message":"Reconnecting... 1/5"}',
                    '{"type":"turn.failed","error":{"message":"unexpected status 503 Service Unavailable"}}',
                ]
            )
        )

        self.assertEqual(
            messages,
            [
                "Reconnecting... 1/5",
                "unexpected status 503 Service Unavailable",
            ],
        )


if __name__ == "__main__":
    unittest.main()
