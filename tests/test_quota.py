import unittest

from ai_team.quota import parse_antigravity, parse_claude, parse_codex


class QuotaTests(unittest.TestCase):
    def test_codex_uses_account_windows_not_token_counts(self):
        snapshot = parse_codex({"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 123},
            "secondary": {"usedPercent": 17, "windowDurationMins": 10080, "resetsAt": 456}
        }}})
        self.assertEqual(snapshot["windows"], [
            {"label": "5h", "remaining_percent": 88, "resets_at": 123},
            {"label": "week", "remaining_percent": 83, "resets_at": 456},
        ])

    def test_claude_local_usage_result(self):
        snapshot = parse_claude({"is_error": False, "result":
            "Current session: 5% used · resets tonight\n"
            "Current week (all models): 20% used · resets Thursday"})
        self.assertEqual([window["remaining_percent"] for window in snapshot["windows"]], [95, 80])

    def test_antigravity_structured_quota(self):
        snapshot = parse_antigravity({"status": "SUCCESS", "command": {"data": {"groups": [
            {"name": "Gemini Models", "buckets": [
                {"window": "5h", "remaining_fraction": 0.952, "reset_time": "2026-09-27T03:38:55Z"},
                {"window": "weekly", "remaining_fraction": 0.955}]}
        ]}}})
        self.assertEqual([window["remaining_percent"] for window in snapshot["windows"]], [95, 96])
        self.assertTrue(all(window["group"] == "Gemini Models" for window in snapshot["windows"]))

    def test_missing_quota_does_not_become_fake_balance(self):
        for parser, payload in ((parse_codex, {}), (parse_claude, {"result": ""}),
                                (parse_antigravity, {"status": "SUCCESS"})):
            with self.subTest(parser=parser.__name__), self.assertRaises(ValueError):
                parser(payload)


if __name__ == "__main__":
    unittest.main()
