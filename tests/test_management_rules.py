import unittest

from relay.interpreter import validate_decision
from relay.management_rules import deterministic_management
from relay.signal_text import is_gain_status


def comment(body, *, message_id="m-current", title="Comment", content="<@&1000000000000000009>", **extras):
    return {
        "id": message_id,
        "content": content,
        "source_group": "approved",
        "embeds": [{
            "type": "rich",
            "title": title,
            "description": body,
            "color": 0x123456,
            "content_scan_version": 2,
        }],
        "attachments": [],
        **extras,
    }


def entry_message(message_id="m-entry"):
    return {
        "id": message_id,
        "content": "<@&1000000000000000009>",
        "source_group": "approved",
        "embeds": ["ENTRY\nContract: BAC $63P\nPrice: .80"],
    }


def owned_episode(entry=None, **overrides):
    value = {
        "state": "owned",
        "entry_message": entry or entry_message(),
        "contract": {"symbol": "BAC", "expiry": "2026-10-23", "strike": "63", "option_type": "put"},
        "quantity": 2,
        "average_price": ".80",
        "entry_order_id": "order-1",
        "reason": "trusted episode attribution",
    }
    value.update(overrides)
    return value


class ManagementRulesTests(unittest.TestCase):
    def assert_valid(self, decision, message, context=()):
        self.assertIsNotNone(decision)
        validated = validate_decision(decision, message, list(context))
        self.assertEqual(validated, decision)
        self.assertEqual(set(decision), {
            "action", "origin_message_id", "contract", "quantity", "fraction",
            "alert_price", "stop_price", "profit_only", "confidence", "ambiguous",
            "reason", "evidence", "monitor",
        })

    def test_gain_status_recognizes_actual_plain_comment_examples(self):
        observed_gain_comments = (
            "100% here 🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥",
            "70%",
            "60%",
            "60%",
            "60%",
            "40%",
            "100% ez af",
            "80%",
            "45%",
            "35% slow ass contract",
            "30% But these wicks are INSANE",
            "110%",
            "90% and more too easy",
            "80 fucking %",
            "60%%%%%%%%%%%%%%%%%%%%%%%%%",
            "50%%%%%%%%%%% LFGGGGGGGGGGGGGGG",
            "45%!!!!!!!!!!!!!!!!!!",
            "55% GG WHAT A GOOD ASS FRIDAY",
            "FUUUUUUUUUUUUUUUUUUUUUUUU 40%%%%%%%%%%%%%",
            "50%",
            "35%",
        )
        self.assertEqual(len(observed_gain_comments), 21)
        self.assertTrue(is_gain_status("100%here fire 🔥"))
        for body in observed_gain_comments:
            with self.subTest(body=body):
                self.assertTrue(is_gain_status(body))

    def test_gain_status_leaves_mixed_explicit_trim_eligible(self):
        for body in (
            "up +50%, took 50% here",
            "30% TOOK MOST HERE",
            "LOOCKED IN 50 FUCKING %%%%%%%%%",
            "LOOCKED IN 50%",
            "set stop at 50%",
            "all out +50%",
            "if this falls 50%",
            "50% of contracts",
            "size 50%",
            "up 50%, take half here",
            "the contract is up 50% and the stop is 2.50",
            "-10% on that didnt like that one",
        ):
            with self.subTest(body=body):
                self.assertFalse(is_gain_status(body))

    def test_plain_gain_status_is_contextual_wait_not_sale(self):
        message = comment("100%here fire 🔥🔥")
        decision = deterministic_management(message)
        self.assertEqual(decision["action"], "WAIT")
        self.assertIsNone(decision["contract"])
        self.assertIsNone(decision["fraction"])
        self.assertIsNone(decision["origin_message_id"])
        self.assert_valid(decision, message)

    def test_mixed_status_and_trim_falls_back(self):
        self.assertIsNone(deterministic_management(comment("up 50%, took 50% here")))

    def test_arbitrary_percentage_intent_is_not_swallowed_as_wait(self):
        for body in (
            "set stop at 50%",
            "all out +50%",
            "if this falls 50%",
            "50% of contracts",
            "size 50%",
            "up 50%, take half here",
        ):
            with self.subTest(body=body):
                self.assertIsNone(deterministic_management({"id": "m-intent", "content": body}))

    def test_exact_half_and_breakeven_requires_owned_episode(self):
        message = comment("Took half here — S/L BE")
        entry = entry_message()
        decision = deterministic_management(message, [entry], episode=owned_episode(entry))
        self.assertEqual(decision["action"], "REDUCE")
        self.assertEqual(decision["fraction"], 0.5)
        self.assertEqual(decision["stop_price"], "breakeven")
        self.assertFalse(decision["profit_only"])
        self.assertEqual(decision["contract"]["expiry"], "2026-10-23")
        self.assertEqual([e["message_id"] for e in decision["evidence"]], ["m-entry", "m-current"])
        self.assert_valid(decision, message, [entry])

    def test_exact_half_rejects_unresolved_or_missing_entry_context(self):
        message = comment("Took half here — S/L BE")
        entry = entry_message()
        self.assertIsNone(deterministic_management(message, [entry], episode={"state": "unresolved"}))
        self.assertIsNone(deterministic_management(message, [], episode=owned_episode(entry)))

    def test_half_phrase_with_extra_words_is_not_guessed(self):
        entry = entry_message()
        episode = owned_episode(entry)
        for body in (
            "Took half here — S/L BE and watching",
            "up 35%, Took half here — S/L BE",
            "took 50% here",
            "Took half here S/L BE 2.50",
            "Took half here S/L BE",
        ):
            with self.subTest(body=body):
                self.assertIsNone(deterministic_management(comment(body), [entry], episode=episode))

    def test_exact_close_and_stopout_require_owned_episode(self):
        entry = entry_message()
        episode = owned_episode(entry)
        for body in ("CLOSED", "Stopped out 🛑"):
            with self.subTest(body=body):
                message = comment(body)
                decision = deterministic_management(message, [entry], episode=episode)
                self.assertEqual(decision["action"], "CLOSE")
                self.assertIsNone(decision["fraction"])
                self.assertIsNone(decision["quantity"])
                self.assertFalse(decision["profit_only"])
                self.assert_valid(decision, message, [entry])

    def test_close_phrase_rejects_question_and_extra_price(self):
        entry = entry_message()
        episode = owned_episode(entry)
        for body in ("CLOSED?", "CLOSED 2.50", "\"CLOSED\""):
            with self.subTest(body=body):
                self.assertIsNone(deterministic_management(comment(body), [entry], episode=episode))

    def test_canonical_decimal_strikes_are_formatted_for_decisions(self):
        for source_strike, expected_strike in (("3.8E+2", "380"), ("1E+2", "100")):
            with self.subTest(source_strike=source_strike):
                entry = {
                    "id": "m-entry-round",
                    "content": "OPEN TSLA $" + expected_strike + "P 2026-10-23",
                    "source_group": "approved",
                }
                episode = owned_episode(entry, contract={
                    "symbol": "TSLA",
                    "expiry": "2026-10-23",
                    "strike": source_strike,
                    "option_type": "put",
                })
                message = comment("CLOSED")
                decision = deterministic_management(message, [entry], episode=episode)
                self.assertEqual(decision["contract"]["strike"], expected_strike)
                self.assert_valid(decision, message, [entry])

    def test_close_without_episode_does_not_use_positions(self):
        message = comment("CLOSED")
        arbitrary_positions = [{
            "bot_owned": True,
            "quantity": 4,
            "source_group": "approved",
            "contract": {"symbol": "TSLA", "expiry": "2026-10-23", "strike": "365", "option_type": "put"},
        }]
        self.assertIsNone(deterministic_management(message, positions=arbitrary_positions))

    def test_flat_episode_returns_wait_without_old_contract(self):
        for body in ("CLOSED", "Took half here — S/L BE"):
            with self.subTest(body=body):
                message = comment(body)
                decision = deterministic_management(message, episode={"state": "flat"})
                self.assertEqual(decision["action"], "WAIT")
                self.assertIsNone(decision["contract"])
                self.assertIsNone(decision["origin_message_id"])
                self.assert_valid(decision, message)

    def test_unquantified_took_most_never_uses_gain_as_fraction(self):
        entry = entry_message()
        for body in ("30% TOOK MOST HERE", "TOOK MOST HERE 35%", "TOOK MOST HERE"):
            with self.subTest(body=body):
                self.assertIsNone(deterministic_management(
                    comment(body), [entry], episode=owned_episode(entry),
                ))

    def test_on_watch_contract_and_watching_not_entry_return_wait(self):
        for message in (
            comment("TSLA $365P 👀", title="On watch"),
            comment("WATCHING NOT ENTRY", title="On watch"),
            {"id": "m-watch", "content": "On watch TSLA $365P 👀", "source_group": "approved"},
            {"id": "m-not-entry", "content": "WATCHING NOT ENTRY", "source_group": "approved"},
        ):
            with self.subTest(message=message):
                decision = deterministic_management(message)
                self.assertEqual(decision["action"], "WAIT")
                self.assertIsNone(decision["contract"])
                self.assert_valid(decision, message)

    def test_conflicting_edits_media_and_extra_embeds_defer(self):
        base = comment("CLOSED")
        cases = (
            {**base, "edited_timestamp": "2026-10-23T15:00:00Z"},
            {**base, "edited_at": "2026-10-23T15:00:00Z"},
            {**base, "attachments": [{"filename": "chart.png", "content_type": "image/png"}]},
            {**base, "embeds": base["embeds"] * 2},
            comment("70%", content="<@&1000000000000000009> CLOSED"),
            comment("CLOSED" + (" " * 6001)),
            comment("CLOSED", content="x" * 6001),
            {**base, "embeds": [{**base["embeds"][0], "image": {"url": "https://example.test/chart.png"}}]},
        )
        for message in cases:
            with self.subTest(message=message):
                self.assertIsNone(deterministic_management(message, episode=owned_episode(entry_message())))

    def test_episode_entry_and_current_message_must_share_source_group(self):
        message = comment("CLOSED")
        entry = {**entry_message(), "source_group": "different"}
        self.assertIsNone(deterministic_management(message, [entry], episode=owned_episode(entry)))

    def test_nonliteral_sizing_cancel_and_lockin_comments_defer(self):
        for body in (
            "Sizing light today",
            "Cancel this one",
            "LOOCKED IN 50 FUCKING %%%%%%%%%",
            "LOOCKED IN 50%",
            "Took most here",
        ):
            with self.subTest(body=body):
                self.assertIsNone(deterministic_management(comment(body), episode=owned_episode(entry_message())))


if __name__ == "__main__":
    unittest.main()
