import unittest

from crawler.robots import RobotsRules


AGENT = "NYCSocialEventCrawler/1.0"


def allowed(lines, url):
    return RobotsRules.parse(lines).can_fetch(AGENT, url)


class RobotsRulesTests(unittest.TestCase):
    def test_longer_allow_overrides_earlier_disallow(self):
        # Bookmanager's file as of 2026-10-23; the first-match parser denied it.
        lines = ["User-agent: *", "Disallow: /", "Allow: /customer", ""]
        self.assertTrue(allowed(lines, "https://api.bookmanager.com/customer/store/getSettings"))
        self.assertTrue(allowed(lines, "https://api.bookmanager.com/customer/event/v2/list"))
        self.assertFalse(allowed(lines, "https://api.bookmanager.com/admin"))
        self.assertFalse(allowed(lines, "https://api.bookmanager.com/"))

    def test_longer_disallow_overrides_shorter_allow(self):
        lines = ["User-agent: *", "Allow: /events", "Disallow: /events/private"]
        self.assertTrue(allowed(lines, "https://example.com/events/public"))
        self.assertFalse(allowed(lines, "https://example.com/events/private/1"))

    def test_allow_wins_equal_length_tie(self):
        lines = ["User-agent: *", "Disallow: /page", "Allow: /page"]
        self.assertTrue(allowed(lines, "https://example.com/page"))

    def test_wildcard_and_end_anchor(self):
        lines = ["User-agent: *", "Disallow: /*.pdf$", "Disallow: /search*q="]
        self.assertFalse(allowed(lines, "https://example.com/files/flyer.pdf"))
        self.assertTrue(allowed(lines, "https://example.com/files/flyer.pdf?download=1"))
        self.assertFalse(allowed(lines, "https://example.com/search/events?q=jazz"))
        self.assertTrue(allowed(lines, "https://example.com/search/events"))

    def test_named_group_replaces_wildcard_group(self):
        lines = ["User-agent: *", "Disallow: /",
                 "", "User-agent: nycsocialeventcrawler", "Disallow: /private"]
        self.assertTrue(allowed(lines, "https://example.com/events"))
        self.assertFalse(allowed(lines, "https://example.com/private"))

    def test_other_named_group_does_not_apply(self):
        lines = ["User-agent: dotbot", "Disallow: /i/"]
        self.assertTrue(allowed(lines, "https://www.bookclubbar.com/i/page"))
        self.assertTrue(allowed(lines, "https://www.bookclubbar.com/events"))

    def test_consecutive_agents_share_rules_and_comments_are_ignored(self):
        lines = ["User-agent: otherbot", "User-agent: *  # everyone else",
                 "Disallow: /tmp  # scratch", "Disallow:"]
        self.assertFalse(allowed(lines, "https://example.com/tmp/x"))
        self.assertTrue(allowed(lines, "https://example.com/events"))

    def test_empty_file_and_full_denial(self):
        self.assertTrue(allowed([], "https://example.com/anything"))
        lines = ["User-agent: *", "Disallow: /"]
        self.assertFalse(allowed(lines, "https://example.com/events"))
        self.assertTrue(allowed(lines, "https://example.com/robots.txt"))


if __name__ == "__main__":
    unittest.main()
