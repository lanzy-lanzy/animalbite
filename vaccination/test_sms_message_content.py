from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings

from vaccination.reminder_scheduler import run_due_check
from vaccination.sms import build_reminder_message
from vaccination.tests import _MissedAppointmentFixture


class SmsMessageContentTests(_MissedAppointmentFixture, TestCase):
    def test_scheduled_templates_use_dose_positions_not_protocol_days(self):
        for reminder in ("schedule_set", "3days_before", "on_day"):
            for dose, label in ((self.first, "Dose 1"), (self.dose, "Dose 2")):
                with self.subTest(reminder=reminder, dose=label):
                    message = build_reminder_message(self.patient, self.case, dose, reminder)
                    self.assertIn(label, message)
                    self.assertNotIn(dose.dose_label, message)
                    self.assertIn(dose.scheduled_date.strftime("%b %d, %Y"), message)
                    self.assertNotIn("missed", message.lower())

    def test_rescheduling_does_not_change_dose_number(self):
        self.dose.scheduled_date = self.today
        self.dose.dose_status = "rescheduled"
        message = build_reminder_message(self.patient, self.case, self.dose, "on_day")
        self.assertIn("Dose 2", message)
        self.assertIn(self.today.strftime("%b %d, %Y"), message)

    def test_explicit_missed_template_and_status_include_missed_and_date(self):
        for reminder, status in (("missed_appointment", "scheduled"), ("on_day", "missed")):
            with self.subTest(reminder=reminder, status=status):
                self.dose.dose_status = status
                message = build_reminder_message(self.patient, self.case, self.dose, reminder)
                self.assertIn("missed", message.lower())
                self.assertIn("2nd Dose", message)
                self.assertIn("November 11, 2026", message)
                self.assertNotIn("Ngayon na", message)

    @override_settings(SEMAPHORE_ENABLED=True, SEMAPHORE_API_KEY="test-only")
    @patch("vaccination.missed_appointments.send_semaphore_sms")
    def test_scheduler_submits_missed_wording_for_the_actual_appointment(self, gateway):
        gateway.return_value = {
            "success": True,
            "response": [{"message_id": "test-only", "status": "pending"}],
        }
        now = datetime(2026, 11, 12, 9, tzinfo=ZoneInfo("Asia/Manila"))
        with TemporaryDirectory() as folder, patch(
            "vaccination.management.commands.send_vaccination_reminders.timezone.localdate",
            return_value=self.today,
        ):
            state = Path(folder) / "scheduler.state"
            self.assertEqual(run_due_check(state, now), "checked")
            self.assertEqual(run_due_check(state, now), "already_checked")
        gateway.assert_called_once()
        message = gateway.call_args.args[1]
        self.assertIn("missed", message.lower())
        self.assertIn("2nd Dose", message)
        self.assertIn("November 11, 2026", message)
        self.assertNotIn("November 12, 2026", message)
