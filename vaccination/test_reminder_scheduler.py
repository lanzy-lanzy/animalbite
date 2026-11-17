from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from vaccination.reminder_scheduler import run_due_check


@override_settings(SEMAPHORE_ENABLED=True, SEMAPHORE_API_KEY="test-only")
class ReminderSchedulerTests(SimpleTestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name) / "scheduler.state"

    @patch("vaccination.reminder_scheduler.call_command")
    def test_philippine_nine_am_and_once_per_day(self, send):
        self.assertEqual(run_due_check(self.state, datetime(2026, 11, 17, 0, 59, tzinfo=timezone.utc)), "waiting")
        send.assert_not_called()
        due = datetime(2026, 11, 17, 1, tzinfo=timezone.utc)
        self.assertEqual(run_due_check(self.state, due), "checked")
        self.assertEqual(run_due_check(self.state, due), "already_checked")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["type"], "missed")
        for prohibited in ("force", "retry_failed", "approve_missed_backlog", "date"):
            self.assertNotIn(prohibited, send.call_args.kwargs)
        run_due_check(self.state, datetime(2026, 11, 18, 1, tzinfo=timezone.utc))
        self.assertEqual(send.call_count, 2)

    @override_settings(SEMAPHORE_ENABLED=False)
    @patch("vaccination.reminder_scheduler.call_command")
    def test_disabled_gateway_does_not_send_or_consume_day(self, send):
        self.assertEqual(run_due_check(self.state, datetime(2026, 11, 17, 2, tzinfo=timezone.utc)), "gateway_disabled")
        send.assert_not_called()
        self.assertFalse(self.state.exists())

    @override_settings(SEMAPHORE_API_KEY="")
    @patch("vaccination.reminder_scheduler.call_command")
    def test_missing_key_does_not_send(self, send):
        run_due_check(self.state, datetime(2026, 11, 17, 2, tzinfo=timezone.utc))
        send.assert_not_called()

    @patch("vaccination.reminder_scheduler.call_command", side_effect=CommandError("private details"))
    def test_partial_failure_is_not_automatically_retried(self, send):
        due = datetime(2026, 11, 17, 1, tzinfo=timezone.utc)
        with self.assertRaises(CommandError):
            run_due_check(self.state, due)
        self.assertEqual(run_due_check(self.state, due), "already_checked")
        self.assertEqual(send.call_count, 1)

    @patch("vaccination.reminder_scheduler.call_command")
    def test_patient_output_is_captured(self, send):
        def output(*args, **kwargs):
            kwargs["stdout"].write("private patient details")
        send.side_effect = output
        run_due_check(self.state, datetime(2026, 11, 17, 1, tzinfo=timezone.utc))
        self.assertNotIn("private", self.state.read_text())
