"""Keep the newsletter placeholder blocked until extraction is implemented."""

import datetime as dt
import tempfile
import unittest
from unittest.mock import Mock

from crawler.adapters.lightning_society import ADAPTER_ID, PARSER_VERSION
from crawler.registry import get_adapter
from crawler.runner import execute_source


class LightningSocietyAdapterTests(unittest.TestCase):
    def test_runner_reports_blocked_without_network_access(self):
        client = Mock()
        source = {"id": "lightning-society", "adapter": ADAPTER_ID}
        self.assertEqual(get_adapter(ADAPTER_ID).id, ADAPTER_ID)
        with tempfile.TemporaryDirectory() as cache:
            result = execute_source(
                source, {}, dt.date(2026, 9, 21),
                client=client, artifact_root=cache,
            )
        self.assertEqual(result["source"]["state"], "blocked")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertIn("official Feedbin API", result["source"]["detail"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["source"]["artifacts"], [])
        self.assertEqual(client.mock_calls, [])
