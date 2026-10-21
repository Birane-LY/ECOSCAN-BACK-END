import argparse
from datetime import datetime, time

from django.test import SimpleTestCase
from django.utils import timezone

from analysis.management.commands.executer_taches_energetiques_planifiees import (
    _parse_heure,
    prochaine_execution,
)


class ScheduledAnalysisTests(SimpleTestCase):
    def test_next_run_is_today_when_daily_time_is_still_ahead(self):
        with timezone.override("UTC"):
            now = timezone.make_aware(datetime(2026, 4, 12, 1, 30))

            result = prochaine_execution(now, time(2, 15))

        self.assertEqual(result, timezone.make_aware(datetime(2026, 4, 12, 2, 15)))

    def test_next_run_is_tomorrow_after_daily_time_has_passed(self):
        with timezone.override("UTC"):
            now = timezone.make_aware(datetime(2026, 4, 12, 2, 16))

            result = prochaine_execution(now, time(2, 15))

        self.assertEqual(result, timezone.make_aware(datetime(2026, 4, 13, 2, 15)))

    def test_daily_time_parser_rejects_invalid_values(self):
        self.assertEqual(_parse_heure("02:15"), time(2, 15))
        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_heure("25:00")
