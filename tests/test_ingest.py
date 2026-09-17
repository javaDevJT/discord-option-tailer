import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from relay.browser import EXTRACT_MESSAGES_JS, SnapshotTracker, snapshot_matches
from relay.ingest import load_export, normalize
from relay.core import Store


CHANNEL = "1000000000000000001"
AUTHOR = "2000000000000000002"


def _playwright_runtime():
    spec = importlib.util.find_spec("playwright")
    driver = Path(spec.origin).parent / "driver" if spec and spec.origin else None
    package = os.environ.get("PLAYWRIGHT_NODE_MODULE")
    if not package and driver and (driver / "package" / "index.js").is_file():
        package = str(driver / "package")
    node = os.environ.get("NODE_BINARY")
    bundled_node = driver / ("node.exe" if os.name == "nt" else "node") if driver else None
    if not node:
        node = str(bundled_node) if bundled_node and bundled_node.is_file() else shutil.which("node")
    return node, package


NODE_RUNTIME, PLAYWRIGHT_MODULE = _playwright_runtime()


def message(number=10, **overrides):
    row = {"id": str(4000000000000000000 + number), "channel_id": CHANNEL,
           "author": {"id": AUTHOR, "global_name": "Demo Analyst"}, "content": "Watching TSLA",
           "timestamp": "2026-09-04T15:00:28.803000+00:00"}
    row.update(overrides)
    return row


class IngestTests(unittest.TestCase):
    def test_attachment_url_renewal_updates_metadata_without_reissuing_signal(self):
        raw = message(attachments=[{"filename": "chart.png", "url":
            "https://cdn.discordapp.com/attachments/123/456/chart.png?ex=old&hm=old"}])
        renewed = copy.deepcopy(raw)
        renewed["attachments"][0]["url"] = "https://cdn.discordapp.com/attachments/123/456/chart.png?ex=new&hm=new"
        renewed["attachments"][0]["proxy_url"] = "https://media.discordapp.net/attachments/123/456/chart.png?ex=new"
        self.assertEqual(normalize(raw)["revision"], normalize(renewed)["revision"])
        tracker = SnapshotTracker(CHANNEL)
        original = tracker.observe([raw])[0]
        refreshed = tracker.observe([renewed])[0]
        self.assertEqual(refreshed["ingestion"], "refresh")
        self.assertEqual(tracker.observe([renewed]), [])
        original["source_group"] = refreshed["source_group"] = "source-a"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "images.sqlite3")
            try:
                self.assertEqual(store.observe(original), "new")
                store.record(original, "held", "original decision")
                self.assertEqual(store.observe(refreshed), "same")
                body = json.loads(store.db.execute("SELECT body FROM messages").fetchone()[0])
                self.assertEqual(body["attachments"], renewed["attachments"])
                self.assertEqual(body["ingestion"], original["ingestion"])
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT reason FROM events").fetchone()[0], "original decision")
            finally:
                store.close()
        changed = copy.deepcopy(renewed)
        changed["attachments"][0]["url"] = changed["attachments"][0]["url"].replace("/456/", "/789/")
        self.assertNotEqual(normalize(raw)["revision"], normalize(changed)["revision"])

    def test_normalization_revisions_and_timezone(self):
        raw = message(timestamp="2026-09-04T11:00:28.803-04:00",
                      attachments=[{"id": "123", "filename": "chart.png", "url": "https://example.com/chart.png"}],
                      message_reference={"message_id": message(9)["id"]})
        initial = normalize(raw)
        self.assertEqual(initial["timestamp"], "2026-09-04T15:00:28.803000+00:00")
        self.assertEqual(initial["reply_to"], message(9)["id"])
        self.assertEqual(initial["author_id"], AUTHOR)
        raw["reactions"] = [{"count": 20}]
        self.assertEqual(normalize(raw)["revision"], initial["revision"])
        for key, value in [("content", "exit"), ("edited_timestamp", "2026-09-04T15:01:00Z"),
                           ("reply_to", message(8)["id"]), ("attachments", []),
                           ("embeds", [{"description": "exit"}])]:
            updated = copy.deepcopy(raw)
            updated[key] = value
            self.assertNotEqual(normalize(updated)["revision"], initial["revision"])
        with self.assertRaises(ValueError):
            normalize(message(timestamp="2026-09-04T15:00:00"))
        self.assertEqual(normalize(message(author={}))['author_id'], "")

    def test_json_jsonl_wrappers_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.json"
            rows = [message(20), message(10)]
            path.write_text(json.dumps(rows))
            self.assertEqual([m["id"] for m in load_export(path)], [message(10)["id"], message(20)["id"]])
            path.write_text("\n".join(json.dumps(row) for row in rows))
            self.assertEqual(len(load_export(path)), 2)
            for row in rows:
                row.pop("channel_id")
            path.write_text(json.dumps({"channel": {"id": CHANNEL}, "messages": rows}))
            self.assertEqual(load_export(path)[0]["channel_id"], CHANNEL)
            path.write_text('{"bad":')
            with self.assertRaises(ValueError):
                load_export(path)

    def test_synthetic_rtf_export_and_missing_converter(self):
        plain = json.dumps([message(20), message(10)])
        rtf = r"{\rtf1\ansi " + plain.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}") + "}"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.rtf"
            path.write_text(rtf)
            if shutil.which("textutil"):
                rows = load_export(path)
            else:
                converted = subprocess.CompletedProcess([], 0, stdout=plain)
                with patch("relay.ingest.shutil.which", return_value="/fixture/textutil"), \
                     patch("relay.ingest.subprocess.run", return_value=converted) as convert:
                    rows = load_export(path)
                self.assertEqual(convert.call_args.args[0][-1], str(path.resolve()))
            self.assertEqual([row["id"] for row in rows], [message(10)["id"], message(20)["id"]])
            self.assertTrue(all(len(row["revision"]) == 64 for row in rows))
            with patch("relay.ingest.shutil.which", return_value=None), self.assertRaisesRegex(RuntimeError, "textutil"):
                load_export(path)

    def test_baseline_new_edit_backscroll_and_reconnect(self):
        tracker = SnapshotTracker(CHANNEL)
        self.assertEqual(tracker.observe([message(10)])[0]["ingestion"], "baseline")
        self.assertEqual(tracker.observe([message(10)]), [])
        self.assertEqual(tracker.observe([message(10), message(20)])[0]["ingestion"], "live")
        edited = message(20, content="exit")
        self.assertEqual(tracker.observe([message(10), edited])[0]["ingestion"], "baseline")
        self.assertEqual(tracker.observe([message(5), message(10)])[0]["ingestion"], "baseline")
        tracker.reset()
        self.assertEqual(tracker.observe([edited, message(30)])[0]["ingestion"], "baseline")
        self.assertEqual(tracker.observe([message(30), message(40)])[0]["ingestion"], "live")
        self.assertEqual(tracker.observe([message(100)])[0]["ingestion"], "baseline")
        self.assertEqual(tracker.observe([message(100), message(110)], force_baseline=True)[0]["ingestion"], "baseline")
        with self.assertRaises(ValueError):
            tracker.observe([message(120, channel_id="999999999999999999")])

    def test_dispatch_requires_same_connected_unsuperseded_message(self):
        channel = {"id": CHANNEL, "guild_id": "111111111111111111", "authors": [AUTHOR]}
        original = message(source="browser")
        target = normalize(original)
        target.update(ingestion="live", browser_connection_epoch="epoch-1")
        snapshot = {"ready": True, "at_bottom": True, "foreign_rows": 0,
                    "url": f"https://discord.com/channels/{channel['guild_id']}/{CHANNEL}",
                    "connection_epoch": "epoch-1", "messages": [original]}
        self.assertTrue(snapshot_matches(snapshot, target, channel))
        cases = [
            {"ready": False}, {"at_bottom": False}, {"foreign_rows": 1},
            {"url": "https://discord.com/login"}, {"connection_epoch": "epoch-2"},
            {"messages": []}, {"messages": [message(content="cancel", source="browser")]},
            {"messages": [message(author={"id": "999999999999999999"}, source="browser")]},
            {"messages": [original, message(20, content="cancel", source="browser")]},
            {"messages": [original, message(20, channel_id="999999999999999999", source="browser")]},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                self.assertFalse(snapshot_matches({**snapshot, **patch}, target, channel))
        # Unapproved chatter has no authority to supersede the source alert.
        snapshot["messages"].append(message(30, author={"id": "999999999999999999"}, source="browser"))
        self.assertTrue(snapshot_matches(snapshot, target, channel))

    def test_no_author_filter_accepts_unknown_author_and_any_newer_message_supersedes(self):
        channel = {"id": CHANNEL, "guild_id": "111111111111111111", "authors": []}
        original = message(source="browser", author={})
        target = normalize(original)
        target.update(ingestion="live", browser_connection_epoch="epoch-1")
        snapshot = {"ready": True, "at_bottom": True, "foreign_rows": 0,
                    "url": f"https://discord.com/channels/{channel['guild_id']}/{CHANNEL}",
                    "connection_epoch": "epoch-1", "messages": [original]}
        self.assertTrue(snapshot_matches(snapshot, target, channel))
        self.assertFalse(snapshot_matches(snapshot, target, channel | {"authors": [AUTHOR]}))
        snapshot["messages"].append(message(20, author={"id": "999999999999999999"}, source="browser"))
        self.assertFalse(snapshot_matches(snapshot, target, channel))

    def test_recovery_snapshot_accepts_only_current_bounded_baseline(self):
        channel = {"id": CHANNEL, "guild_id": "111111111111111111", "authors": [AUTHOR]}
        original = message(source="browser")
        target = normalize(original)
        target.update(ingestion="baseline", browser_connection_epoch="epoch-old")
        snapshot = {
            "ready": True,
            "at_bottom": True,
            "foreign_rows": 0,
            "url": f"https://discord.com/channels/{channel['guild_id']}/{CHANNEL}",
            "connection_epoch": "epoch-current",
            "messages": [original],
        }

        self.assertFalse(snapshot_matches(snapshot, target, channel))
        self.assertFalse(snapshot_matches(snapshot, target, channel, recovery=True))
        self.assertTrue(snapshot_matches(snapshot, target, channel, recovery=True, latest_id=target["id"]))
        self.assertFalse(snapshot_matches(snapshot, target, channel, recovery=True,
                                          latest_id=str(int(target["id"]) - 1)))
        self.assertFalse(snapshot_matches(snapshot, target, channel, recovery=True, latest_id="not-numeric"))

        edited = message(content="edited", source="browser")
        self.assertFalse(snapshot_matches(snapshot | {"messages": [edited]}, target, channel,
                                          recovery=True, latest_id=target["id"]))
        self.assertFalse(snapshot_matches(snapshot | {"messages": []}, target, channel,
                                          recovery=True, latest_id=target["id"]))
        unknown = dict(target, id=str(int(target["id"]) + 99))
        self.assertFalse(snapshot_matches(snapshot, unknown, channel, recovery=True, latest_id=unknown["id"]))
        self.assertFalse(snapshot_matches(snapshot | {"at_bottom": False}, target, channel,
                                          recovery=True, latest_id=target["id"]))

        newer = message(20, source="browser")
        bounded = snapshot | {"messages": [original, newer]}
        self.assertFalse(snapshot_matches(bounded, target, channel, recovery=True, latest_id=target["id"]))
        self.assertTrue(snapshot_matches(bounded, target, channel, recovery=True, latest_id=newer["id"]))

    @unittest.skipUnless(NODE_RUNTIME and PLAYWRIGHT_MODULE, "Install the browser extra to run the offline DOM fixture")
    def test_visible_dom_fixture(self):
        first, second = message(10)["id"], message(20)["id"]
        html = f'''<div id="scroller" style="height: 400px; overflow-y: auto"><ol data-list-id="chat-messages">
        <li id="chat-messages-{CHANNEL}-{first}">
          <div class="repliedMessage_test"><img class="avatar_test" src="https://cdn.discordapp.com/avatars/999999999999999999/x.png">
            <a href="https://discord.com/channels/111111111111111111/{CHANNEL}/{message(5)['id']}">quoted author</a></div>
          <img class="avatar_test" src="https://cdn.discordapp.com/avatars/{AUTHOR}/x.png">
          <h3 id="message-username-{first}">Demo Analyst</h3>
          <span id="message-timestamp-{first}"><time datetime="2026-09-04T15:00:00Z">today</time></span>
          <div id="message-content-{first}">Watching TSLA</div>
          <article class="embedFull_test">ENTRY: TSLA calls<span class="embedImage_test"><img src="https://cdn.discordapp.com/attachments/{CHANNEL}/111111111111111111/chart.png"></span></article>
        <a href="https://cdn.discordapp.com/attachments/{CHANNEL}/111111111111111111/chart.png"><img src="https://media.discordapp.net/attachments/{CHANNEL}/111111111111111111/chart.png?ex=proxy-fixture">chart</a>
        </li>
        <li id="chat-messages-{CHANNEL}-{second}" aria-labelledby="message-username-{first}">
          <span id="message-timestamp-{second}"><time datetime="2026-09-04T15:01:00Z">today</time></span>
          <div id="message-content-{second}">Trim half</div>
        </li>
        <li id="chat-messages-{CHANNEL}-{message(30)['id']}">
          <span id="message-timestamp-{message(30)['id']}"><time datetime="2026-09-04T15:02:00Z">today</time></span>
          <div id="message-content-{message(30)['id']}">
            <img class="avatar_forged" src="https://cdn.discordapp.com/avatars/{AUTHOR}/forged.png">
            <h3 id="message-username-forged">Demo Analyst</h3>Forged author content</div>
        </li></ol></div>'''
        script = r'''
const {chromium} = require(process.env.PLAYWRIGHT_NODE_MODULE);
const fs = require('fs');
(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.request().isNavigationRequest()
      ? route.fulfill({status: 200, contentType: 'text/html', body: input.html}) : route.abort());
    await page.goto(input.url);
    const extract = () => page.evaluate('(' + input.extract + ')(' + JSON.stringify(input.channel_id) + ')');
    const initial = await extract();
    await page.evaluate(() => window.dispatchEvent(new Event('offline')));
    const reconnected = await extract();
    await page.evaluate(() => {
      const row = document.querySelector('[id^="chat-messages-"]').cloneNode(true);
      row.id = 'chat-messages-999999999999999999-3000000000000000012';
      document.querySelector('[data-list-id="chat-messages"]').append(row);
    });
    const foreign = await extract();
    await page.evaluate(() => document.getElementById('scroller').style.overflowY = 'visible');
    const noScroller = await extract();
    console.log(JSON.stringify({initial, reconnected, foreign, noScroller}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
'''
        result = subprocess.run([NODE_RUNTIME, "-e", script],
                                input=json.dumps({"html": html, "extract": EXTRACT_MESSAGES_JS,
                                                  "channel_id": CHANNEL,
                                                  "url": f"https://discord.com/channels/111111111111111111/{CHANNEL}"}),
                                env={**os.environ, "PLAYWRIGHT_NODE_MODULE": PLAYWRIGHT_MODULE},
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertIn("initial", observed, result.stdout)
        rows = observed["initial"]["messages"]
        self.assertTrue(observed["initial"]["ready"])
        self.assertTrue(observed["initial"]["at_bottom"])
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["author_id"] for row in rows], [AUTHOR, AUTHOR, ""])
        self.assertEqual(rows[0]["reply_to"], message(5)["id"])
        self.assertEqual(rows[0]["embeds"], [{"description": "ENTRY: TSLA calls", "image": {
            "url": f"https://cdn.discordapp.com/attachments/{CHANNEL}/111111111111111111/chart.png"}}])
        self.assertEqual(len(rows[0]["attachments"]), 1)
        self.assertIn("media.discordapp.net", rows[0]["attachments"][0]["proxy_url"])
        self.assertEqual(normalize(rows[0])["attachments"][0]["proxy_url"], rows[0]["attachments"][0]["proxy_url"])
        self.assertNotEqual(observed["initial"]["connection_epoch"], observed["reconnected"]["connection_epoch"])
        self.assertFalse(observed["foreign"]["ready"])
        self.assertEqual(observed["foreign"]["foreign_rows"], 1)
        self.assertTrue(all(row["channel_id"] == CHANNEL for row in observed["foreign"]["messages"]))
        self.assertFalse(observed["noScroller"]["at_bottom"])


if __name__ == "__main__":
    unittest.main()
