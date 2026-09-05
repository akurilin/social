"""Keep simultaneous Mindbody class levels separate."""

import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tools import events_store as store


class MindbodyDedupTests(unittest.TestCase):
    def test_distinct_class_ids_do_not_merge(self):
        base = 'https://cart.mindbodyonline.com/sites/95211/cart/add_booking?'
        first = {'title': 'Midtown Swings - Lindy 100',
                 'start': '2026-09-08T20:00:00-04:00',
                 'venue': 'Danznik NYC', 'host': 'Danznik Studios',
                 'url': base + urlencode({'item[mbo_id]': '34160', 'item[type]': 'Class'})}
        second = {**first, 'title': 'Midtown Swings - Lindy 150',
                  'url': base + urlencode({'item[mbo_id]': '34174', 'item[type]': 'Class'})}
        with tempfile.TemporaryDirectory() as folder:
            con = store.connect(Path(folder) / 'test.db')
            with con:
                one, outcome, _ = store.merge_one(con, first, '2026-09-03')
                self.assertEqual(outcome, 'new')
                two, outcome, _ = store.merge_one(con, second, '2026-09-03')
                self.assertEqual(outcome, 'new')
                self.assertNotEqual(one['id'], two['id'])
                _, outcome, _ = store.merge_one(con, second, '2026-09-03')
                self.assertEqual(outcome, 'unchanged')
            con.close()
