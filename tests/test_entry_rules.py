import unittest

from relay.entry_rules import bounded_entry_candidate, deterministic_entry


def message(content="", *, embed=None, attachments=None, message_id="m-1"):
    return {
        "id": message_id,
        "content": content,
        "source_group": "approved",
        "timestamp": "2026-09-04T14:00:00Z",
        "embeds": [embed] if embed else [],
        "attachments": attachments or [],
    }


class EntryRulesTests(unittest.TestCase):
    def test_structured_entry_is_validated_and_defaults_expiry(self):
        decision = deterministic_entry(message(
            "<@&role>",
            embed={
                "title": "ENTRY",
                "description": "🖼️ Contract: TSLA $352.5P\n💰 Price: .87\n‼️ Comments: none",
                "footer": {"text": "@zendotrades"},
            },
        ))
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "OPEN")
        self.assertEqual(decision["contract"], {
            "symbol": "TSLA",
            "strike": "352.5",
            "option_type": "put",
            "expiry": "nearest",
        })
        self.assertEqual(decision["alert_price"], ".87")
        self.assertEqual(decision["confidence"], 1.0)

    def test_watching_comment_is_not_direct(self):
        msg = message(embed={
            "title": "ENTRY",
            "description": "Contract: QQQ $704P\nPrice: 1.09\nComments: WATCHING THIS TOO",
        })
        self.assertIsNone(deterministic_entry(msg))
        self.assertIsNone(bounded_entry_candidate(msg))

    def test_plain_dollar_prefixed_entry_parses_explicit_date(self):
        decision = deterministic_entry(message(
            "OPEN **$BAC $63 call 9/18 @ $0.96** (swing)",
        ))
        self.assertIsNotNone(decision)
        self.assertEqual(decision["contract"]["expiry"], "2026-09-18")
        self.assertEqual(decision["contract"]["symbol"], "BAC")
        self.assertEqual(decision["alert_price"], "0.96")

    def test_image_can_be_decorative_when_text_has_complete_expiry(self):
        msg = message(
            "OPEN $BAC $63 call 9/18 @ $0.96",
            attachments=[{"filename": "chart.png", "content_type": "image/png"}],
        )
        self.assertIsNotNone(deterministic_entry(msg))

    def test_missing_expiry_with_image_stays_on_codex(self):
        msg = message(
            "ENTRY $BAC $63 call @ $0.96",
            attachments=[{"filename": "card.png", "content_type": "image/png"}],
        )
        self.assertIsNone(deterministic_entry(msg))
        self.assertIsNone(bounded_entry_candidate(msg))

    def test_risk_qualifiers_and_conflicts_are_not_literal_entries(self):
        for content in (
            "OPEN $HNGE $100 call 9/18 @ $2.30 (swing, same size)",
            "OPEN $BAC $63 call 9/18 @ $0.96; roll if needed",
            "OPEN $BAC $63 call 9/18 @ $0.96 and $AAPL $200 put 9/18 @ $1.00",
            "OPEN $BAC $0 call 9/18 @ $0.96",
            "OPEN $BAC $63 call 9/18 @ $0.00",
        ):
            with self.subTest(content=content):
                self.assertIsNone(deterministic_entry(message(content)))

    def test_direct_path_requires_positive_entry_grammar(self):
        for content in (
            "NOT AN ENTRY BAC $63 call 9/18 @ .96",
            "ENTRY CANCELLED BAC $63 call 9/18 @ .96",
            "We missed the ENTRY BAC $63 call 9/18 @ .96",
            "OPEN BAC $63 call 9/18 @ .96 stop .50",
        ):
            with self.subTest(content=content):
                self.assertIsNone(deterministic_entry(message(content)))

    def test_structured_unknown_comment_is_not_direct(self):
        self.assertIsNone(deterministic_entry(message(embed={
            "title": "ENTRY",
            "description": "Contract: BAC $63 call\nPrice: .96\nComments: CANCELLED",
            "footer": {"text": "@zendotrades"},
        })))

    def test_structured_unconsumed_text_and_truncated_fields_are_not_entries(self):
        newline = "\n"
        cases = (
            {
                "title": "ENTRY",
                "description": newline.join((
                    "Contract: BAC $63 call",
                    "Price: .96",
                    "Comments: none",
                )),
                "text": "CANCELLED",
            },
            {
                "title": "ENTRY" + (" " * 300) + "CANCELLED",
                "description": newline.join((
                    "Contract: BAC $63 call",
                    "Price: .96",
                    "Comments: none",
                )),
            },
            {
                "title": "ENTRY",
                "description": newline.join((
                    "Contract: BAC $63 call",
                    "Price: .96",
                    "Comments: none",
                )),
                "footer": {"text": "@zendotrades" + (" " * 300) + "CANCELLED"},
            },
            {
                "title": "ENTRY",
                "fields": [
                    {"name": "Contract", "value": "BAC $63 call"},
                    {"name": "Price", "value": ".96"},
                    {"name": "Comments" + (" " * 300) + "CANCELLED", "value": "none"},
                ],
            },
            {"title": "ENTRY", "fields": {}},
        )
        for embed in cases:
            with self.subTest(embed=embed):
                msg = message(embed=embed)
                self.assertIsNone(deterministic_entry(msg))
                self.assertIsNone(bounded_entry_candidate(msg))
        malformed = message("OPEN $BAC $63 call 9/18 @ $0.96", attachments={"filename": "chart.png"})
        self.assertIsNone(deterministic_entry(malformed))
        self.assertIsNone(bounded_entry_candidate(malformed))

    def test_image_after_ten_attachments_is_seen_without_blocking_dated_plain_entry(self):
        msg = message(
            "OPEN $BAC $63 call 9/18 @ $0.96",
            attachments=[{"filename": "note.txt"}] * 10 + [{"filename": "chart.png", "content_type": "image/png"}],
        )
        self.assertIsNotNone(deterministic_entry(msg))
        self.assertIsNotNone(bounded_entry_candidate(msg))


if __name__ == "__main__":
    unittest.main()
