"""Offline tests: fake feeds in, check what PokeRadar would alert on."""
import json, os, tempfile, time, unittest
from unittest import mock

import radar

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
# One feed of each kind is enough for the tests
CFG_SMALL = {**CFG, "google_news": {"en": ["pokemon cards"], "ja": []},
             "bing_news": [], "reddit": [], "rss": []}


def entry(title, source, href, minutes_ago=10, link=None):
    return {
        "title": f"{title} - {source}",
        "link": link or f"https://news.google.com/x/{abs(hash(title + source))}",
        "source": {"title": source, "href": href},
        "summary": "",
        "published_parsed": time.gmtime(time.time() - minutes_ago * 60),
    }


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.json")
        self.sent = []
        self.p_notify = mock.patch.object(radar, "notify", side_effect=lambda *a, **k: self.sent.append(a))
        self.p_notify.start()

    def tearDown(self):
        self.p_notify.stop()

    def run_with(self, entries):
        with mock.patch.object(radar, "fetch_feed", return_value=entries):
            return radar.run_once(CFG_SMALL, self.state)

    def test_first_run_seeds_without_story_alerts(self):
        self.run_with([entry("Thief steals $70K of Pokémon cards", "CBC", "https://www.cbc.ca")])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("live", self.sent[0][0])
        self.assertTrue(os.path.exists(self.state))

    def test_upstream_theft_story_alerts_as_early(self):
        self.run_with([])  # seed
        self.sent.clear()
        alerts = self.run_with([entry(
            "Thief hid in flea market booth, walked out with $70,000 in Pokémon cards",
            "CBC", "https://www.cbc.ca")])
        self.assertEqual(len(alerts), 1)
        title, body, link, prio, tags = self.sent[0]
        self.assertIn("EARLY", body)
        self.assertGreaterEqual(alerts[0][2], 7)

    def test_off_topic_is_ignored(self):
        self.run_with([])
        alerts = self.run_with([entry("How to beat Giovanni in Pokemon Go (October 2026)",
                                      "Dexerto", "https://www.dexerto.com")])
        self.assertEqual(alerts, [])

    def test_aggregator_story_flagged_as_covered(self):
        self.run_with([])
        self.sent.clear()
        self.run_with([entry("Crypto hacker convicted of $53M theft after buying rare Pokemon cards",
                             "Dexerto", "https://www.dexerto.com")])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Already on Dexerto", self.sent[0][1])

    def test_same_story_alerts_once_then_spreading_once(self):
        self.run_with([])
        self.sent.clear()
        t = "Winnipeg flea market thief steals $70K Pokémon cards hockey bag"
        self.run_with([entry(t, "CBC", "https://www.cbc.ca")])
        self.run_with([entry("Winnipeg flea market thief steals Pokémon cards in hockey bag",
                             "CTV News", "https://www.ctvnews.ca")])
        self.run_with([entry("Pokémon cards thief Winnipeg flea market hockey bag $70K",
                             "Global News", "https://globalnews.ca")])
        self.run_with([entry("Winnipeg flea market thief hockey bag Pokémon slabs",
                             "Winnipeg Free Press", "https://www.winnipegfreepress.com")])
        kinds = [s[0][:9] for s in self.sent]
        self.assertEqual(len(self.sent), 2, self.sent)
        self.assertTrue(self.sent[1][0].startswith("Spreading"))

    def test_stale_story_skipped(self):
        self.run_with([])
        alerts = self.run_with([entry("Thief steals $70K of Pokémon cards", "CBC",
                                      "https://www.cbc.ca", minutes_ago=60 * 20)])
        self.assertEqual(alerts, [])

    def test_japanese_story_scores(self):
        self.run_with([])
        alerts = self.run_with([entry("ポケカ転売で男を逮捕 大量の未開封BOX", "NHK",
                                      "https://www3.nhk.or.jp")])
        self.assertEqual(len(alerts), 1)

    def test_non_pokemon_card_ignored(self):
        self.run_with([])
        alerts = self.run_with([entry("Kobe Bryant card sells for $1.11 million after record price jump",
                                      "Yahoo Sports", "https://sports.yahoo.com")])
        self.assertEqual(alerts, [])

    def test_viral_find_story_alerts(self):
        self.run_with([])
        alerts = self.run_with([entry("Grandma finds 1st Edition Charizard in attic, sells for $420,000",
                                      "WKYC", "https://www.wkyc.com")])
        self.assertEqual(len(alerts), 1)
        self.assertGreaterEqual(alerts[0][2], 9)

    def test_boring_deal_post_ignored(self):
        self.run_with([])
        alerts = self.run_with([entry("Best Pokémon TCG deals: 30th Celebration ETB on sale at Amazon",
                                      "Mashable", "https://mashable.com")])
        self.assertEqual(alerts, [])

    def test_money_parsing(self):
        f = radar.biggest_amount
        self.assertEqual(f("$70,000 in cards"), 70000)
        self.assertEqual(f("a $5.3 million card"), 5.3e6)
        self.assertEqual(f("$70k gone"), 70000)
        self.assertEqual(f("worth 8.4 million dollars"), 8.4e6)
        self.assertEqual(f("no money here, 30th celebration"), 0)
        self.assertEqual(f("scalpers flipped $41,800 m worth"), 41800)  # was read as $41.8B
        self.assertEqual(f("$1.4mil"), 1.4e6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
