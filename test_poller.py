#!/usr/bin/env python3
"""Self-check: RSS/markdown parsing and last-seen dedup."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import poller

CHANGELOG_RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Cursor Changelog</title>
    <item>
      <title>Cloud Agent Canvases</title>
      <link>https://cursor.com/changelog/canvases</link>
      <guid>https://cursor.com/changelog/canvases</guid>
      <pubDate>Fri, 16 Oct 2026 00:00:00 GMT</pubDate>
      <description>Cloud agents can now build interactive canvases.</description>
    </item>
    <item>
      <title>Origin Marketplace</title>
      <link>https://cursor.com/changelog/origin</link>
      <guid>https://cursor.com/changelog/origin</guid>
      <pubDate>Tue, 13 Oct 2026 00:00:00 GMT</pubDate>
      <description>Origin integrates with over 20 apps.</description>
    </item>
  </channel>
</rss>
"""

XAI_MD = """# Release Notes

## October

### Grok Voice 3.0 is available

`grok-voice-3.0` is now available with Speech to Speech.

### safety\\_identifier request field

You can now send `safety_identifier` on Chat Completions.

## September

### Grok 4.7

Grok 4.7 is now available on the xAI API.
"""

STATUS_RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>[Global (api.x.ai)] API outage</title>
      <link>https://status.x.ai/api-global/INC1</link>
      <guid>INC1</guid>
      <description><![CDATA[<h3>Status: INVESTIGATING</h3><p>Severity: outage</p>]]></description>
      <pubDate>Wed, 07 Oct 2026 00:24:33 GMT</pubDate>
      <category>outage</category>
    </item>
    <item>
      <title>[US (us.api.x.ai)] API outage</title>
      <link>https://status.x.ai/api-us/INC1</link>
      <guid>INC1</guid>
      <description><![CDATA[<h3>Status: INVESTIGATING</h3><p>Severity: outage</p>]]></description>
      <pubDate>Wed, 07 Oct 2026 00:24:33 GMT</pubDate>
      <category>outage</category>
    </item>
    <item>
      <title>[grok.com] API outage</title>
      <link>https://status.x.ai/grok-com/INC1</link>
      <guid>INC1</guid>
      <description><![CDATA[<h3>Status: INVESTIGATING</h3><p>Severity: outage</p>]]></description>
      <category>outage</category>
    </item>
    <item>
      <title>[Voice] Planned work</title>
      <link>https://status.x.ai/voice/MAINT1</link>
      <guid>MAINT1</guid>
      <description><![CDATA[<h3>Status: SCHEDULED</h3><p>Scheduled maintenance on Voice.</p>]]></description>
      <category>maintenance</category>
      <category>scheduled</category>
    </item>
    <item>
      <title>[Voice] Old outage</title>
      <link>https://status.x.ai/voice/INC2</link>
      <guid>INC2</guid>
      <description><![CDATA[<h3>Status: RESOLVED</h3><p>Severity: disruption</p>]]></description>
      <pubDate>Tue, 06 Oct 2026 19:03:00 GMT</pubDate>
      <category>disruption</category>
      <category>resolved</category>
    </item>
  </channel>
</rss>
"""

STATUS_RESOLVED_RSS = STATUS_RSS.replace("Status: INVESTIGATING", "Status: RESOLVED").replace(
    "<category>outage</category>",
    "<category>outage</category><category>resolved</category>",
    1,
)


class ParseAndDedupTest(unittest.TestCase):
    def test_changelog_keys_are_guids(self) -> None:
        items = poller.parse_changelog(CHANGELOG_RSS)
        self.assertEqual(
            [item.key for item in items],
            [
                "https://cursor.com/changelog/canvases",
                "https://cursor.com/changelog/origin",
            ],
        )
        self.assertEqual(items[0].title, "Cloud Agent Canvases")

    def test_xai_notes_split_on_h3(self) -> None:
        items = poller.parse_xai_notes(XAI_MD)
        self.assertEqual(
            [item.key for item in items],
            [
                "grok voice 3.0 is available",
                "safety_identifier request field",
                "grok 4.7",
            ],
        )
        self.assertTrue(items[0].link.endswith("#grok-voice-3-0-is-available"))
        self.assertIn("Speech to Speech", items[0].summary)

    def test_status_dedup_by_guid_and_filters(self) -> None:
        items = poller.parse_status(STATUS_RSS)
        keys = [item.key for item in items]
        self.assertEqual(keys, ["api outage:open", "old outage:resolved"])
        self.assertFalse(any("maint" in item.key for item in items))
        self.assertTrue(items[0].title.startswith("Incident:"))
        self.assertTrue(items[1].title.startswith("Resolved:"))

    def test_first_run_sends_nothing_then_only_new_keys(self) -> None:
        state = poller.empty_state()
        changelog = poller.parse_changelog(CHANGELOG_RSS)
        to_send, keys, mode = poller.select_updates(
            state, poller.SOURCE_CHANGELOG, changelog
        )
        self.assertEqual(mode, "bootstrap")
        self.assertEqual(to_send, [])
        self.assertEqual(len(keys), 2)
        poller.mark_seen(state, poller.SOURCE_CHANGELOG, keys)
        state["initialized"].append(poller.SOURCE_CHANGELOG)

        to_send, keys, mode = poller.select_updates(
            state, poller.SOURCE_CHANGELOG, changelog
        )
        self.assertIsNone(mode)
        self.assertEqual(to_send, [])
        self.assertEqual(keys, [])

        extra_rss = CHANGELOG_RSS.replace(
            "</channel>",
            """<item>
      <title>New Thing</title>
      <link>https://cursor.com/changelog/new</link>
      <guid>https://cursor.com/changelog/new</guid>
      <pubDate>Fri, 23 Oct 2026 00:00:00 GMT</pubDate>
      <description>Brand new.</description>
    </item></channel>""",
        )
        updated = poller.parse_changelog(extra_rss)
        to_send, _, mode = poller.select_updates(
            state, poller.SOURCE_CHANGELOG, updated
        )
        self.assertIsNone(mode)
        self.assertEqual([item.key for item in to_send], ["https://cursor.com/changelog/new"])

    def test_status_open_then_resolved_without_component_spam(self) -> None:
        state = poller.empty_state()
        first = poller.parse_status(STATUS_RSS)
        to_send, keys, mode = poller.select_updates(state, poller.SOURCE_STATUS, first)
        self.assertEqual(mode, "bootstrap")
        self.assertEqual(to_send, [])
        poller.mark_seen(state, poller.SOURCE_STATUS, keys)
        state["initialized"].append(poller.SOURCE_STATUS)

        # Same investigating snapshot: nothing new, including duplicate component rows.
        to_send, _, mode = poller.select_updates(state, poller.SOURCE_STATUS, first)
        self.assertIsNone(mode)
        self.assertEqual(to_send, [])

        resolved = poller.parse_status(STATUS_RESOLVED_RSS)
        to_send, _, mode = poller.select_updates(state, poller.SOURCE_STATUS, resolved)
        self.assertIsNone(mode)
        self.assertEqual([item.key for item in to_send], ["api outage:resolved"])
        self.assertTrue(to_send[0].title.startswith("Resolved:"))

    def test_status_collapses_same_title_across_guids(self) -> None:
        rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>[Automations] Investigating service degradation</title>
    <guid>g1</guid>
    <description>Status: INVESTIGATING</description>
    <category>disruption</category>
  </item>
  <item>
    <title>[Cloud Agents] Investigating service degradation</title>
    <guid>g2</guid>
    <description>Status: INVESTIGATING</description>
    <category>disruption</category>
  </item>
  <item>
    <title>[Grok Bot] Investigating service degradation — Grok Bot</title>
    <guid>g3</guid>
    <description>Status: RESOLVED</description>
    <category>disruption</category>
    <category>resolved</category>
  </item>
</channel></rss>
"""
        items = poller.parse_status(rss)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].key, "investigating service degradation:resolved")
        self.assertTrue(items[0].title.startswith("Resolved:"))

    def _many_status_items(self, n: int, *, resolved: bool = True) -> list[poller.Item]:
        chunks = []
        status = "RESOLVED" if resolved else "INVESTIGATING"
        extra_cat = "<category>resolved</category>" if resolved else ""
        for i in range(n):
            chunks.append(
                f"""<item>
      <title>[Automations] Unique outage {i}</title>
      <guid>G{i}</guid>
      <description>Status: {status}</description>
      <category>disruption</category>
      {extra_cat}
    </item>"""
            )
        rss = '<?xml version="1.0"?><rss version="2.0"><channel>' + "".join(chunks) + "</channel></rss>"
        return poller.parse_status(rss)

    def test_status_absorbs_backfill_instead_of_sending(self) -> None:
        state = poller.empty_state()
        seed = self._many_status_items(1)
        to_send, keys, mode = poller.select_updates(state, poller.SOURCE_STATUS, seed)
        self.assertEqual(mode, "bootstrap")
        poller.mark_seen(state, poller.SOURCE_STATUS, keys)
        state["initialized"].append(poller.SOURCE_STATUS)

        flood = self._many_status_items(poller.STATUS_BACKFILL_THRESHOLD + 2)
        self.assertGreater(len(flood), poller.STATUS_BACKFILL_THRESHOLD)
        to_send, keys, mode = poller.select_updates(state, poller.SOURCE_STATUS, flood)
        self.assertEqual(mode, "backfill")
        self.assertEqual(to_send, [])
        self.assertEqual(len(keys), len(flood))
        poller.mark_seen(state, poller.SOURCE_STATUS, keys)

        to_send, _, mode = poller.select_updates(state, poller.SOURCE_STATUS, flood)
        self.assertIsNone(mode)
        self.assertEqual(to_send, [])

    def test_status_still_sends_small_batch(self) -> None:
        state = poller.empty_state()
        seed = self._many_status_items(1)
        _, keys, _ = poller.select_updates(state, poller.SOURCE_STATUS, seed)
        poller.mark_seen(state, poller.SOURCE_STATUS, keys)
        state["initialized"].append(poller.SOURCE_STATUS)

        two = self._many_status_items(2)
        to_send, keys, mode = poller.select_updates(state, poller.SOURCE_STATUS, two)
        self.assertIsNone(mode)
        self.assertEqual(len(to_send), 1)
        self.assertEqual(keys, [])
        self.assertEqual(to_send[0].key, "unique outage 1:resolved")

    def test_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-seen.json"
            state = poller.empty_state()
            poller.mark_seen(state, poller.SOURCE_CHANGELOG, ["g1"])
            state["initialized"].append(poller.SOURCE_CHANGELOG)
            poller.save_state(path, state)
            loaded = poller.load_state(path)
            self.assertEqual(loaded["seen"][poller.SOURCE_CHANGELOG], ["g1"])
            self.assertIn(poller.SOURCE_CHANGELOG, loaded["initialized"])


if __name__ == "__main__":
    unittest.main()
