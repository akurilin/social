import json
import unittest
from pathlib import Path

from crawler.registry import ADAPTERS, get_adapter


ROOT = Path(__file__).resolve().parents[1]


class AdapterRegistryTests(unittest.TestCase):
    def test_each_catalog_adapter_is_registered(self):
        catalog = json.loads((ROOT / "sources.example.json").read_text(encoding="utf-8"))
        configured = {
            source["adapter"]
            for source in catalog["sources"]
            if source.get("enabled", True) and source.get("adapter")
        }
        self.assertEqual(configured - set(ADAPTERS), set())
        for adapter_id in configured:
            self.assertEqual(get_adapter(adapter_id).id, adapter_id)


if __name__ == "__main__":
    unittest.main()
