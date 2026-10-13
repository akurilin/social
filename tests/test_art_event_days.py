"""Keep ongoing shows visible on each visit date."""
import datetime as dt
import unittest

from crawler.art_event_days import daily_slots


class ArtEventDaysTests(unittest.TestCase):
    def test_ongoing_show_includes_both_window_edges(self):
        slots = daily_slots(dt.date(2026, 9, 23), dt.date(2026, 9, 28),
                            dt.date(2026, 9, 27), 1, 'America/New_York')
        self.assertEqual([s[0] for s in slots],
                         ['2026-09-27T00:00:00-04:00', '2026-09-28T00:00:00-04:00'])
        self.assertTrue(all(s[1] is None for s in slots))

    def test_weekly_hours_exclude_closed_days_and_follow_dst(self):
        slots = daily_slots(dt.date(2026, 10, 31), dt.date(2026, 11, 2),
                            dt.date(2026, 10, 31), 2, 'America/New_York',
                            weekly={5: [('12:00', '19:00')], 6: [('12:00', '17:00')]})
        self.assertEqual(slots, [('2026-10-31T12:00:00-04:00', '2026-10-31T19:00:00-04:00'),
                                 ('2026-11-01T12:00:00-05:00', '2026-11-01T17:00:00-05:00')])

    def test_date_hours_override_weekly_hours(self):
        slots = daily_slots(dt.date(2026, 9, 23), dt.date(2026, 9, 24),
                            dt.date(2026, 9, 23), 1, 'America/New_York',
                            dated={dt.date(2026, 9, 23): [('17:00', '20:00')],
                                   dt.date(2026, 9, 24): [('12:00', '19:00')]})
        self.assertEqual(slots[0][0], '2026-09-23T17:00:00-04:00')
        self.assertEqual(slots[1][0], '2026-09-24T12:00:00-04:00')

    def test_reversed_range_fails(self):
        with self.assertRaises(ValueError):
            daily_slots(dt.date(2026, 9, 28), dt.date(2026, 9, 23),
                        dt.date(2026, 9, 27), 1, 'America/New_York')
