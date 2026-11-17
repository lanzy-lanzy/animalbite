"""
Tests for the nurse-triggered vaccination schedule workflow.

Schedules are deliberately NOT created automatically when a bite case is
created or converted. A nurse (or admin) triggers them explicitly once the
case has been assessed.
"""
import datetime
import json
import re
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Event
from unittest import skipUnless
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from bite_cases.models import AnimalBiteCase
from doctor.models import ClinicalAssessment, Prescription
from inventory.models import MedicineInventory, VaccineInventory
from patients.models import Barangay, Patient, VitalSign
from pre_registrations.models import PreRegistration
from settings_app.models import SystemSetting

from .forms import VaccineDoseForm
from .models import SMSLog, VaccinationSchedule, VaccineDose
from .missed_appointments import (
    missed_dose_count, missed_dose_counts_by_patient, missed_dose_queryset,
    missed_reminder_message, missed_sms_consent_ok,
    missed_reminder_doses, missed_reminder_is_due,
    send_missed_appointment_reminder,
)
from .schedule_utils import ensure_vaccination_schedule, get_dose_days, schedule_base_date

U = get_user_model()


class _MissedAppointmentFixture:
    """Shared records, without TestCase's enclosing transaction for claim tests."""

    def setUp(self):
        self.nurse = U.objects.create_user(username="n.missed", role="nurse")
        self.patient = Patient.objects.create(
            patient_number="PAT-MISS01", first_name="Juan", last_name="Dela Cruz",
            birthdate=datetime.date(1990, 1, 1), contact_number="09173334444",
        )
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-MISS01", patient=self.patient,
            created_by=self.nurse, animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 11, 1, 9, 0),
            case_status="under_treatment",
        )
        self.schedule = VaccinationSchedule.objects.create(
            bite_case=self.case, created_by=self.nurse,
        )
        self.first = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 0",
            scheduled_date=datetime.date(2026, 11, 1),
            actual_date=datetime.date(2026, 11, 1), dose_status="completed",
        )
        self.dose = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 3",
            scheduled_date=datetime.date(2026, 11, 11),
            dose_status="scheduled",
        )
        self.today = datetime.date(2026, 11, 12)
        self.consent = self._pre_registration(consent_given=True)

    @staticmethod
    def _at(day, hour=10, minute=0):
        return datetime.datetime(
            day.year, day.month, day.day, hour, minute,
            tzinfo=ZoneInfo("Asia/Manila"),
        )

    def _pre_registration(self, consent_given, case=None):
        case = case or self.case
        return PreRegistration.objects.create(
            first_name="Juan", last_name="Dela Cruz",
            birthdate=datetime.date(1990, 1, 1), sex="male",
            contact_number="09173334444", address="Purok 1", barangay="San Jose",
            medical_history="None", allergy_info="None", previous_rabies_vax="None",
            bite_datetime=datetime.datetime(2026, 11, 1, 9, 0),
            incident_place="Home", incident_barangay="San Jose",
            animal_type="dog", animal_ownership="owned",
            animal_vaccination_status="unknown", animal_condition="alive",
            exposure_type="bite", body_part_affected="Arm",
            consent_given=consent_given, converted_case=case,
            converted_patient=case.patient,
        )

    @staticmethod
    def _accepted(status="Queued", message_id="missed-1"):
        return {
            "success": True,
            "response": [{"status": status, "message_id": message_id}],
        }

    def _send(self, **kwargs):
        kwargs.setdefault("today", self.today)
        kwargs.setdefault("now", self._at(kwargs["today"]))
        return send_missed_appointment_reminder(self.dose.pk, **kwargs)


@override_settings(SEMAPHORE_ENABLED=False, ABTC_APPOINTMENT_CUTOFF_TIME=None)
class MissedAppointmentSmsTests(_MissedAppointmentFixture, TestCase):

    def test_command_sends_yesterday_dose_to_primary_number_once(self):
        output = StringIO()
        call_command(
            "send_vaccination_reminders", "--type", "missed",
            "--date", self.today.isoformat(), stdout=output,
        )

        log = SMSLog.objects.get(
            dose=self.dose, reminder_type="missed_appointment",
        )
        self.assertEqual(log.status, "queued")
        self.assertEqual(log.recipient, "639173334444")
        self.assertEqual(log.scheduled_date, self.dose.scheduled_date)
        self.assertEqual(log.patient, self.patient)
        self.assertEqual(log.bite_case, self.case)
        self.assertEqual(log.message, missed_reminder_message(self.dose))
        self.assertIn("scheduled 2nd Dose on November 11, 2026", log.message)
        self.assertEqual(log.attempt_count, 0)
        self.assertIn("queued", log.api_response.lower())
        self.assertEqual(
            log.event_key,
            f"missed_appointment:{self.patient.pk}:{self.case.pk}:"
            f"{self.dose.pk}:2026-11-11",
        )

        call_command(
            "send_vaccination_reminders", "--type", "missed",
            "--date", self.today.isoformat(), stdout=StringIO(),
        )
        self.assertEqual(SMSLog.objects.filter(
            dose=self.dose, reminder_type="missed_appointment",
        ).count(), 1)

    def test_daily_command_keeps_advance_day_and_missed_events_separate_without_backfill(self):
        self.today = datetime.date(2026, 11, 13)
        self.dose.scheduled_date = datetime.date(2026, 11, 12)
        self.dose.save(update_fields=['scheduled_date'])
        on_day = VaccineDose.objects.create(
            schedule=self.schedule, dose_label='Day 7',
            scheduled_date=self.today, dose_status='scheduled')
        advance = VaccineDose.objects.create(
            schedule=self.schedule, dose_label='Day 14',
            scheduled_date=self.today + datetime.timedelta(days=3), dose_status='scheduled')
        historical = VaccineDose.objects.create(
            schedule=self.schedule, dose_label='Historical appointment',
            scheduled_date=self.today - datetime.timedelta(days=60), dose_status='missed')
        previous = SMSLog.objects.create(
            dose=self.dose, patient=self.patient, bite_case=self.case,
            reminder_type='on_day', recipient=self.patient.contact_number,
            scheduled_date=self.dose.scheduled_date, status='sent',
            message='Previously accepted appointment-day reminder.', api_response='Existing response')
        saved_log = SMSLog.objects.filter(pk=previous.pk).values().get()
        saved_doses = list(self.schedule.doses.values())
        with patch('django.utils.timezone.now', return_value=self._at(self.today)), patch(
            'vaccination.sms.send_semaphore_sms', return_value=self._accepted(),
        ) as regular_gateway, patch(
            'vaccination.missed_appointments.send_semaphore_sms', return_value=self._accepted(),
        ) as missed_gateway:
            for _ in range(2):
                call_command('send_vaccination_reminders', stdout=StringIO())
            self.assertEqual(regular_gateway.call_count, 2)
            missed_gateway.assert_called_once_with(
                '639173334444',
                'RHU Dumingag ABTC: You missed your scheduled 2nd Dose on November 12, 2026. '
                'Please visit or contact RHU Dumingag as soon as possible to arrange '
                'your next vaccination appointment. Thank you.')
        self.assertEqual(set(SMSLog.objects.exclude(pk=previous.pk).values_list(
            'dose_id', 'reminder_type', 'status')), {
                (on_day.pk, 'on_day', 'sent'),
                (advance.pk, '3days_before', 'sent'),
                (self.dose.pk, 'missed_appointment', 'sent'),
            })
        self.assertFalse(SMSLog.objects.filter(dose=historical).exists())
        self.assertEqual(saved_log, SMSLog.objects.filter(pk=previous.pk).values().get())
        self.assertEqual(saved_doses, list(self.schedule.doses.values()))

    def test_only_recent_unadministered_doses_are_selected(self):
        completed = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Dose 3",
            scheduled_date=self.today - datetime.timedelta(days=1),
            actual_date=self.today, dose_status="completed",
        )
        today_dose = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Dose 4",
            scheduled_date=self.today, dose_status="scheduled",
        )
        future = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Dose 5",
            scheduled_date=self.today + datetime.timedelta(days=2),
            dose_status="scheduled",
        )
        older = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Dose 6",
            scheduled_date=self.today - datetime.timedelta(days=2),
            dose_status="scheduled",
        )
        rescheduled = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Dose 7",
            scheduled_date=self.today + datetime.timedelta(days=3),
            dose_status="rescheduled",
        )
        self.assertEqual(
            [dose.pk for dose in missed_reminder_doses(today=self.today)],
            [self.dose.pk, older.pk],
        )
        self.assertTrue(missed_reminder_is_due(self.dose, today=self.today))
        self.assertTrue(missed_reminder_is_due(older, today=self.today))
        for dose in (completed, today_dose, future, rescheduled):
            self.assertFalse(missed_reminder_is_due(dose, today=self.today))

    def test_scheduled_today_is_not_sent_regardless_of_time(self):
        self.dose.scheduled_date = self.today
        self.dose.save(update_fields=["scheduled_date"])

        for hour in (8, 15, 23):
            self.assertFalse(missed_reminder_doses(
                today=self.today, now=self._at(self.today, hour),
            ))
        with override_settings(ABTC_APPOINTMENT_CUTOFF_TIME="15:00"):
            self.assertFalse(missed_reminder_doses(
                today=self.today, now=self._at(self.today, 18),
            ))
        log, result = send_missed_appointment_reminder(
            self.dose.pk, today=self.today, now=self._at(self.today, 18),
        )
        self.assertIsNone(log)
        self.assertTrue(result["skipped"])

    def test_failed_attempt_is_not_resent_same_day(self):
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value={
                "success": False, "error": "Provider rejected the message",
                "retryable": True, "status_code": 429,
            },
        ) as send:
            log, first_result = send_missed_appointment_reminder(
                self.dose.pk, today=self.today, now=self._at(self.today),
            )
            self.assertEqual(log.status, "failed")
            self.assertFalse(first_result["success"])

            _, same_day_result = send_missed_appointment_reminder(
                self.dose.pk, today=self.today, now=self._at(self.today, 18),
            )
            self.assertTrue(same_day_result["skipped"])

            later = self.today + datetime.timedelta(days=1)
            _, later_result = send_missed_appointment_reminder(
                self.dose.pk, today=later, now=self._at(later),
            )
            self.assertTrue(later_result["skipped"])

        self.assertEqual(send.call_count, 1)
        log.refresh_from_db()
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.attempt_count, 1)

    def test_accepted_reminder_is_never_resent(self):
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value={
                "success": True,
                "response": [{"status": "Sent", "message_id": "m-1"}],
            },
        ) as send:
            log, _ = send_missed_appointment_reminder(
                self.dose.pk, today=self.today, now=self._at(self.today),
            )
            self.assertEqual(log.status, "sent")
            _, again = send_missed_appointment_reminder(
                self.dose.pk, today=self.today, now=self._at(self.today, 18),
            )
            self.assertTrue(again["skipped"])
            call_command(
                "send_vaccination_reminders", "--type", "missed", "--force",
                "--date", self.today.isoformat(), stdout=StringIO(),
            )

        self.assertEqual(send.call_count, 1)
        self.assertEqual(SMSLog.objects.filter(
            dose=self.dose, reminder_type="missed_appointment",
        ).count(), 1)

    def test_missed_reminder_is_in_patient_history_and_report(self):
        send_missed_appointment_reminder(
            self.dose.pk, today=self.today, now=self._at(self.today),
        )
        self.client.force_login(self.nurse)

        detail = self.client.get(reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk],
        ))
        self.assertContains(detail, "Automatic Missed Appointment")
        self.assertContains(detail, "2nd Dose")

        with patch("reports.views.timezone.localdate", return_value=self.today):
            report = self.client.get(reverse("reports:missed_appointments"), {
                "type": "all",
            })
        self.assertContains(report, self.case.case_number)
        self.assertEqual(report.context["total"], 1)

    def test_patient_list_does_not_label_simulated_reminder_as_sent(self):
        log, _ = self._send()
        self.assertEqual(log.status, "queued")
        self.client.force_login(self.nurse)

        with patch("django.utils.timezone.now", return_value=self._at(self.today)):
            listing = self.client.get(reverse("vaccination:sms_log_list"))
        self.assertNotContains(listing, "Missed – SMS Sent")
        self.assertContains(listing, "Queued")
        self.assertContains(listing, self.patient.full_name())
        self.assertContains(listing, reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk],
        ))

    def test_completed_dose_does_not_show_missed_sms_sent(self):
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value=self._accepted(),
        ):
            log, _ = self._send()
        self.assertEqual(log.status, "sent")
        self.dose.actual_date = self.today
        self.dose.dose_status = "completed"
        self.dose.save(update_fields=["actual_date", "dose_status"])
        self.client.force_login(self.nurse)

        listing = self.client.get(reverse("vaccination:sms_log_list"))
        self.assertNotContains(listing, "Missed – SMS Sent")

    def test_message_uses_actual_ordinal_number(self):
        self.assertEqual(
            missed_reminder_message(self.dose),
            "RHU Dumingag ABTC: You missed your scheduled 2nd Dose "
            "on November 11, 2026. Please visit or contact RHU Dumingag "
            "as soon as possible to arrange your next vaccination appointment. Thank you.",
        )
        self.assertIn("1st Dose", missed_reminder_message(self.first))
        third = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 7",
            scheduled_date=datetime.date(2026, 11, 18), dose_status="scheduled",
        )
        self.assertIn("3rd Dose", missed_reminder_message(third))
        for label in ('4th Dose', '5th Dose'):
            dose = VaccineDose.objects.create(
                schedule=self.schedule, dose_label='Protocol follow-up',
                scheduled_date=datetime.date(2026, 11, 25), dose_status='scheduled')
            self.assertIn(label, missed_reminder_message(dose))

    def test_consent_not_given_skips_sms(self):
        self.consent.consent_given = False
        self.consent.save(update_fields=["consent_given"])
        self.assertFalse(missed_sms_consent_ok(self.case))
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
        ) as send:
            log, result = send_missed_appointment_reminder(
                self.dose.pk, today=self.today, now=self._at(self.today),
            )
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.attempt_count, 0)
        self.assertTrue(result["skipped"])
        self.assertIn("consent", log.api_response.lower())
        send.assert_not_called()
        self.assertEqual(SMSLog.objects.filter(dose=self.dose).count(), 1)

    def test_consent_given_allows_sms(self):
        self.assertTrue(missed_sms_consent_ok(self.case))
        log, _ = send_missed_appointment_reminder(
            self.dose.pk, today=self.today, now=self._at(self.today),
        )
        self.assertEqual(log.status, "queued")

    def test_archived_patient_is_not_sent(self):
        Patient.objects.filter(pk=self.patient.pk).update(is_archived=True)
        self.assertFalse(missed_reminder_doses(today=self.today))
        log, result = send_missed_appointment_reminder(
            self.dose.pk, today=self.today, now=self._at(self.today),
        )
        self.assertIsNone(log)
        self.assertTrue(result["skipped"])

    def test_archived_case_is_not_sent(self):
        AnimalBiteCase.objects.filter(pk=self.case.pk).update(case_status="archived")
        self.assertFalse(missed_reminder_doses(today=self.today))
        log, result = send_missed_appointment_reminder(
            self.dose.pk, today=self.today, now=self._at(self.today),
        )
        self.assertIsNone(log)
        self.assertTrue(result["skipped"])

    def test_invalid_primary_contact_is_never_replaced_by_emergency_number(self):
        Patient.objects.filter(pk=self.patient.pk).update(
            contact_number="invalid",
            emergency_contact_number="639177777777",
        )
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
        ) as send:
            log, result = send_missed_appointment_reminder(
                self.dose.pk, today=self.today, now=self._at(self.today),
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["skipped"])
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.attempt_count, 0)
        self.assertEqual(log.recipient, "invalid")
        self.assertIn("contact", log.api_response.lower())
        send.assert_not_called()


@override_settings(SEMAPHORE_ENABLED=True, ABTC_APPOINTMENT_CUTOFF_TIME=None)
class MissedAppointmentContractTests(_MissedAppointmentFixture, TestCase):

    def test_selection_and_sender_agree_on_saved_date_and_status(self):
        scenarios = (
            ("scheduled", -1, None, True),
            ("rescheduled", -1, None, True),
            ("missed", -1, None, True),
            ("scheduled", 0, None, False),
            ("rescheduled", 2, None, False),
            ("missed", 2, None, False),
            ("scheduled", -2, None, True),
            ("scheduled", -7, None, True),
            ("scheduled", -8, None, False),
            ("scheduled", None, None, False),
            ("pending", None, None, False),
            ("pending", -1, None, False),
            ("cancelled", -1, None, False),
            ("completed", -1, None, False),
            ("scheduled", -1, self.today, False),
        )
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value=self._accepted(),
        ) as gateway:
            for index, (status, offset, actual, eligible) in enumerate(scenarios):
                with self.subTest(status=status, offset=offset, actual=actual):
                    dose = VaccineDose.objects.create(
                        schedule=self.schedule, dose_label=f"Eligibility {index}",
                        protocol_day=3,
                        scheduled_date=(self.today + datetime.timedelta(days=offset)
                                        if offset is not None else None),
                        actual_date=actual, dose_status=status,
                    )
                    self.assertEqual(missed_reminder_is_due(dose, today=self.today), eligible)
                    self.assertEqual(
                        missed_reminder_doses(today=self.today).filter(pk=dose.pk).exists(),
                        eligible,
                    )
                    log, result = send_missed_appointment_reminder(
                        dose.pk, today=self.today, now=self._at(self.today),
                    )
                    if eligible:
                        self.assertEqual(log.status, "sent")
                    else:
                        self.assertIsNone(log)
                        self.assertTrue(result["skipped"])
                        self.assertFalse(SMSLog.objects.filter(dose=dose).exists())
            self.assertEqual(gateway.call_count, 5)

    def test_inactive_cases_never_attempt_delivery(self):
        with patch("vaccination.missed_appointments.send_semaphore_sms") as gateway:
            for status in ("completed", "archived", "referred"):
                with self.subTest(status=status):
                    AnimalBiteCase.objects.filter(pk=self.case.pk).update(case_status=status)
                    self.assertFalse(missed_reminder_doses(today=self.today).exists())
                    log, result = self._send(retry_failed=True)
                    self.assertIsNone(log)
                    self.assertTrue(result["skipped"])
            gateway.assert_not_called()

    def test_manila_midnight_is_independent_of_active_utc_timezone(self):
        before = datetime.datetime(2026, 11, 11, 15, 59, tzinfo=datetime.timezone.utc)
        midnight = before + datetime.timedelta(minutes=1)
        with timezone.override("UTC"):
            self.assertFalse(missed_reminder_is_due(self.dose, now=before))
            self.assertTrue(missed_reminder_is_due(self.dose, now=midnight))
            self.assertEqual(list(missed_reminder_doses(now=midnight)), [self.dose])
            # An explicit date is the Manila appointment window, not the UTC date.
            self.assertTrue(missed_reminder_is_due(self.dose, today=self.today, now=before))
            with patch("django.utils.timezone.now", return_value=midnight):
                self.assertEqual(self.dose.appointment_status(), "missed")
                self.assertEqual(self.dose.effective_status, "Missed")
            with patch(
                "vaccination.missed_appointments.send_semaphore_sms",
                return_value=self._accepted(),
            ) as gateway:
                log, result = send_missed_appointment_reminder(self.dose.pk, now=midnight)
            self.assertEqual(log.status, "sent")
            self.assertTrue(result["success"])
            gateway.assert_called_once()

    def test_missing_or_patient_only_consent_is_logged_without_gateway_attempts(self):
        for linked_only_to_patient in (False, True):
            with self.subTest(patient_only=linked_only_to_patient):
                if linked_only_to_patient:
                    self._pre_registration(True)
                PreRegistration.objects.filter(converted_case=self.case).update(converted_case=None)
                if not linked_only_to_patient:
                    PreRegistration.objects.filter(converted_patient=self.patient).delete()
                self.assertFalse(missed_sms_consent_ok(self.case))
                with patch("vaccination.missed_appointments.send_semaphore_sms") as gateway:
                    log, result = self._send()
                    again, _ = self._send(retry_failed=True)
                self.assertEqual(log.status, "skipped")
                self.assertEqual(log.attempt_count, 0)
                self.assertIn("consent", log.api_response.lower())
                self.assertTrue(result["skipped"])
                self.assertEqual(again.pk, log.pk)
                self.assertEqual(SMSLog.objects.filter(dose=self.dose).count(), 1)
                gateway.assert_not_called()

    def test_primary_contact_wins_over_emergency_and_account_mobile(self):
        account = U.objects.create_user("p.missed", role="patient", mobile="09178889999")
        Patient.objects.filter(pk=self.patient.pk).update(
            account=account, contact_number="+63 917 333 4444",
            emergency_contact_number="09177778888",
        )
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value=self._accepted(),
        ) as gateway:
            log, _ = self._send()
        self.assertEqual(log.recipient, "639173334444")
        gateway.assert_called_once_with("639173334444", missed_reminder_message(self.dose))

    def test_blank_primary_contact_does_not_fall_back_to_account_mobile(self):
        account = U.objects.create_user("p.blank", role="patient", mobile="09178889999")
        Patient.objects.filter(pk=self.patient.pk).update(
            account=account, contact_number="", emergency_contact_number="09177778888",
        )
        with patch("vaccination.missed_appointments.send_semaphore_sms") as gateway:
            log, result = self._send(retry_failed=True)
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.attempt_count, 0)
        self.assertTrue(result["skipped"])
        gateway.assert_not_called()

    def test_second_patient_has_an_independent_event_and_recipient(self):
        patient = Patient.objects.create(
            patient_number="PAT-MISS02", first_name="Maria", last_name="Santos",
            birthdate=datetime.date(1992, 2, 2), contact_number="09175556666",
        )
        case = AnimalBiteCase.objects.create(
            case_number="ABC-MISS02", patient=patient, created_by=self.nurse,
            animal_type="cat", exposure_type="bite", case_status="under_treatment",
            date_time_bite=self._at(self.today - datetime.timedelta(days=5)),
        )
        schedule = VaccinationSchedule.objects.create(bite_case=case, created_by=self.nurse)
        dose = VaccineDose.objects.create(
            schedule=schedule, dose_label="Day 0", dose_status="scheduled",
            scheduled_date=self.dose.scheduled_date,
        )
        self._pre_registration(True, case=case)
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value=self._accepted(),
        ) as gateway:
            for _ in range(2):
                call_command("send_vaccination_reminders", "--type", "missed",
                             "--date", self.today.isoformat(), stdout=StringIO())
        self.assertEqual(gateway.call_count, 2)
        logs = list(SMSLog.objects.order_by("dose_id"))
        self.assertEqual(len(logs), 2)
        self.assertEqual({log.recipient for log in logs}, {"639173334444", "639175556666"})
        self.assertEqual({log.patient_id for log in logs}, {self.patient.pk, patient.pk})
        self.assertEqual(len({log.event_key for log in logs}), 2)
        self.assertEqual(SMSLog.objects.get(dose=dose).bite_case_id, case.pk)

    def test_rescheduling_creates_new_event_only_when_new_date_is_yesterday(self):
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value=self._accepted(),
        ) as gateway:
            old, _ = self._send()
            old_message, old_date = old.message, old.scheduled_date
            self.dose.scheduled_date = self.today + datetime.timedelta(days=2)
            self.dose.dose_status = "rescheduled"
            self.dose.save(update_fields=["scheduled_date", "dose_status"])
            no_log, skipped = self._send(retry_failed=True)
            self.assertIsNone(no_log)
            self.assertTrue(skipped["skipped"])
            new, _ = self._send(today=self.today + datetime.timedelta(days=3))
            self.assertNotEqual(new.event_key, old.event_key)
            self.assertEqual(new.scheduled_date, self.dose.scheduled_date)
            self.assertIn("November 14, 2026", new.message)
            self.assertEqual(gateway.call_count, 2)
        old.refresh_from_db()
        self.assertEqual((old.message, old.scheduled_date), (old_message, old_date))
        self.assertEqual(SMSLog.objects.filter(dose=self.dose).count(), 2)

    def test_retry_requires_explicit_opt_in_hour_gap_and_maximum_three_attempts(self):
        failure = {"success": False, "retryable": True, "error": "Message rejected"}
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms", return_value=failure,
        ) as gateway:
            log, _ = self._send()
            self.assertEqual(log.status, "failed")
            for hour, minute, retry, calls in (
                (10, 59, True, 1), (11, 0, False, 1), (11, 0, True, 2),
                (11, 59, True, 2), (12, 0, True, 3), (13, 0, True, 3),
            ):
                with self.subTest(hour=hour, minute=minute, retry=retry):
                    self._send(now=self._at(self.today, hour, minute), retry_failed=retry)
                    self.assertEqual(gateway.call_count, calls)
            log.refresh_from_db()
            self.assertEqual(log.attempt_count, 3)
            history = json.loads(log.api_response)
            self.assertEqual([entry["attempt"] for entry in history], [1, 2, 3])
            self.assertTrue(all(entry["retryable"] for entry in history))

    def test_nonretryable_failure_and_expired_window_cannot_be_retried(self):
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value={"success": False, "retryable": False, "error": "Rejected"},
        ) as gateway:
            log, _ = self._send()
            self.assertEqual(log.status, "failed")
            _, result = self._send(now=self._at(self.today, 12), retry_failed=True)
            self.assertTrue(result["skipped"])
            self._send(today=self.today + datetime.timedelta(days=1), retry_failed=True)
            gateway.assert_called_once()

    def test_retryable_failure_cannot_cross_recovery_window(self):
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value={"success": False, "retryable": True, "error": "Rejected"},
        ) as gateway:
            log, _ = self._send(now=self._at(self.today, 23))
            _, result = self._send(today=self.today + datetime.timedelta(days=7), retry_failed=True)
            self.assertTrue(result["skipped"])
            gateway.assert_called_once()
        log.refresh_from_db()
        self.assertEqual(log.attempt_count, 1)

    def test_force_command_cannot_bypass_duplicates_or_failed_retry_opt_in(self):
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value={"success": False, "retryable": True, "error": "Rejected"},
        ) as gateway:
            self._send()
            with patch("django.utils.timezone.now", return_value=self._at(self.today, 12)):
                for _ in range(2):
                    call_command("send_vaccination_reminders", "--type", "missed", "--force",
                                 "--date", self.today.isoformat(), stdout=StringIO())
            gateway.assert_called_once()

    def test_force_command_cannot_bypass_consent_or_date_window(self):
        self.consent.delete()
        VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Today", scheduled_date=self.today,
            dose_status="scheduled",
        )
        with patch("vaccination.missed_appointments.send_semaphore_sms") as gateway:
            call_command("send_vaccination_reminders", "--type", "missed", "--force",
                         "--date", self.today.isoformat(), stdout=StringIO())
            gateway.assert_not_called()
        log = SMSLog.objects.get()
        self.assertEqual(log.dose_id, self.dose.pk)
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.attempt_count, 0)

    def test_command_reports_failure_to_scheduler(self):
        from django.core.management.base import CommandError
        with patch(
            'vaccination.missed_appointments.send_semaphore_sms',
            return_value={'success': False, 'error': 'Rejected', 'retryable': True},
        ) as gateway:
            with self.assertRaises(CommandError):
                call_command('send_vaccination_reminders', '--type', 'missed',
                             '--date', self.today.isoformat(), stdout=StringIO())
        gateway.assert_called_once()
        self.assertEqual(SMSLog.objects.get(dose=self.dose).status, 'failed')

    def test_dry_run_does_not_claim_or_send(self):
        with patch("vaccination.missed_appointments.send_semaphore_sms") as gateway:
            call_command("send_vaccination_reminders", "--type", "missed", "--dry-run",
                         "--date", self.today.isoformat(), stdout=StringIO())
            gateway.assert_not_called()
        self.assertFalse(SMSLog.objects.exists())


@override_settings(SEMAPHORE_ENABLED=True, SEMAPHORE_API_KEY="test-private-gateway-key")
class MissedAppointmentGatewayTests(_MissedAppointmentFixture, TestCase):
    api_key = "test-private-gateway-key"

    def _response(self, status=200, data=None):
        response = Mock(status_code=status, text=f"apikey={self.api_key}")
        response.json.return_value = data
        return response

    def _new_dose(self):
        return VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Gateway case", dose_status="scheduled",
            scheduled_date=self.dose.scheduled_date,
        )

    def _assert_unknown(self, post, dose=None):
        dose = dose or self.dose
        log, result = send_missed_appointment_reminder(
            dose.pk, today=self.today, now=self._at(self.today),
        )
        self.assertEqual(log.status, "unknown")
        self.assertEqual(log.attempt_count, 1)
        self.assertFalse(result["success"])
        self.assertFalse(json.loads(log.api_response)[-1]["retryable"])
        self.assertNotIn(self.api_key, log.api_response + str(result))
        again, skipped = send_missed_appointment_reminder(
            dose.pk, today=self.today, now=self._at(self.today, 12), retry_failed=True,
        )
        self.assertEqual(again.pk, log.pk)
        self.assertTrue(skipped["skipped"])
        post.assert_called_once()
        log.refresh_from_db()
        self.assertEqual(log.status, "unknown")
        self.assertEqual(log.attempt_count, 1)
        return log

    def test_real_queued_pending_sent_and_delivered_are_accepted_not_delivery_proof(self):
        for status in ("Queued", "Pending", "Sent", "Delivered"):
            with self.subTest(status=status):
                dose = self._new_dose()
                response = self._response(data=[{"status": status, "message_id": f"id-{status}"}])
                with patch("vaccination.sms.requests.post", return_value=response) as post:
                    log, result = send_missed_appointment_reminder(
                        dose.pk, today=self.today, now=self._at(self.today),
                    )
                    self.assertEqual(log.status, "sent")
                    self.assertEqual(log.semaphore_message_id, f"id-{status}")
                    self.assertTrue(result["success"])
                    _, duplicate = send_missed_appointment_reminder(
                        dose.pk, today=self.today, now=self._at(self.today, 12), retry_failed=True,
                    )
                    self.assertTrue(duplicate["skipped"])
                    post.assert_called_once()
                    self.assertEqual(post.call_args.kwargs["data"]["number"], "639173334444")
                    self.assertEqual(post.call_args.kwargs["data"]["message"], missed_reminder_message(dose))
                    self.assertGreater(post.call_args.kwargs["timeout"], 0)
                    self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_timeout_is_unknown_sanitized_and_never_retried(self):
        with patch(
            "vaccination.sms.requests.post",
            side_effect=requests.Timeout(f"Request body apikey={self.api_key}"),
        ) as post, patch("vaccination.sms.logger") as logger:
            log = self._assert_unknown(post)
        self.assertNotIn(self.api_key, str(logger.mock_calls))
        self.client.force_login(self.nurse)
        for url in (
            reverse("vaccination:sms_log_detail", args=[log.pk]),
            reverse("vaccination:sms_patient_detail", args=[self.patient.pk]),
        ):
            self.assertNotContains(self.client.get(url), self.api_key)

    def test_http_500_is_unknown_even_when_response_claims_queued(self):
        response = self._response(500, [{
            "status": "Queued", "message_id": "possible-send", "apikey": self.api_key,
        }])
        with patch("vaccination.sms.requests.post", return_value=response) as post:
            self._assert_unknown(post)

    def test_http_408_is_ambiguous_not_a_retryable_rejection(self):
        with patch(
            "vaccination.sms.requests.post", return_value=self._response(408, {"error": "timeout"}),
        ) as post:
            self._assert_unknown(post)

    def test_malformed_http_200_is_unknown_without_raw_body_or_secret_leak(self):
        response = self._response()
        response.json.side_effect = ValueError(f"Invalid JSON apikey={self.api_key}")
        with patch("vaccination.sms.requests.post", return_value=response) as post:
            self._assert_unknown(post)

    def test_http_200_requires_recognized_status_and_real_message_id(self):
        for payload in (
            None, [], {}, ["unexpected"], [{"status": "Queued"}],
            [{"status": "Sent", "message_id": 0}], [{"status": "Sent", "message_id": "0"}],
            [{"message_id": "id-1"}], [{"status": "Not Sent", "message_id": "id-1"}],
        ):
            with self.subTest(payload=payload):
                with patch(
                    "vaccination.sms.requests.post", return_value=self._response(data=payload),
                ) as post:
                    self._assert_unknown(post, self._new_dose())

    def test_http_429_is_definite_failure_retried_only_when_explicit(self):
        rejected = self._response(429, {"error": "Insufficient credits", "apikey": self.api_key})
        accepted = self._response(data=[{"status": "Queued", "message_id": "retry-accepted"}])
        with patch("vaccination.sms.requests.post", side_effect=[rejected, accepted]) as post:
            log, result = self._send()
            self.assertEqual(log.status, "failed")
            self.assertFalse(result["success"])
            self.assertTrue(json.loads(log.api_response)[-1]["retryable"])
            self.assertNotIn(self.api_key, log.api_response + str(result))
            _, skipped = self._send(now=self._at(self.today, 12))
            self.assertTrue(skipped["skipped"])
            retried, result = self._send(now=self._at(self.today, 12), retry_failed=True)
            self.assertEqual(retried.pk, log.pk)
            self.assertEqual(retried.status, "sent")
            self.assertTrue(result["success"])
            self.assertEqual(retried.attempt_count, 2)
            self.assertEqual(post.call_count, 2)
            _, duplicate = self._send(now=self._at(self.today, 14), retry_failed=True)
            self.assertTrue(duplicate["skipped"])
            self.assertEqual(post.call_count, 2)

    def test_unexpected_gateway_exception_is_unknown_without_secret_leak(self):
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            side_effect=RuntimeError(f"Unexpected apikey={self.api_key}"),
        ) as gateway:
            self._assert_unknown(gateway)

    @override_settings(SEMAPHORE_ENABLED=False)
    def test_disabled_gateway_queues_simulation_without_success_or_http_request(self):
        with patch("vaccination.sms.requests.post") as post:
            log, result = self._send()
            self.assertEqual(log.status, "queued")
            self.assertEqual(log.attempt_count, 0)
            self.assertFalse(result["success"])
            self.assertTrue(result["simulated"])
            self._send(now=self._at(self.today, 12), retry_failed=True)
            post.assert_not_called()
        self.assertEqual(SMSLog.objects.filter(dose=self.dose).count(), 1)


@override_settings(SEMAPHORE_ENABLED=False, ABTC_APPOINTMENT_CUTOFF_TIME=None)
class MissedAppointmentPresentationTests(_MissedAppointmentFixture, TestCase):

    def _details(self):
        return self.client.get(reverse("vaccination:sms_patient_detail", args=[self.patient.pk]))

    @staticmethod
    def _section(response, heading):
        sections = re.findall(r"<section\b[^>]*>(.*?)</section>", response.content.decode(), re.S)
        return next(section for section in sections if heading in section)

    def _detail_dose(self, response):
        return next(dose for case in response.context["cases"]
                    for dose in case.sms_doses if dose.pk == self.dose.pk)

    def _log(self, status="sent", reminder_type="missed_appointment", **kwargs):
        values = {
            "dose": self.dose, "patient": self.patient, "bite_case": self.case,
            "reminder_type": reminder_type, "recipient": "639173334444",
            "message": f"Saved {reminder_type} message.",
            "scheduled_date": self.dose.scheduled_date, "status": status,
        }
        values.update(kwargs)
        return SMSLog.objects.create(**values)

    def test_model_management_and_sms_details_share_effective_status(self):
        self.client.force_login(self.nurse)
        scenarios = (
            ("scheduled", -1, None, "missed", "Missed"),
            ("rescheduled", -1, None, "missed", "Missed"),
            ("pending", -1, None, "missed", "Missed"),
            ("scheduled", 0, None, "scheduled", "Scheduled"),
            ("missed", 2, None, "scheduled", "Scheduled"),
            ("rescheduled", 2, None, "rescheduled", "Rescheduled"),
            ("missed", None, None, "pending", "Pending Scheduling"),
            ("completed", -1, None, "completed", "Completed"),
            ("scheduled", -1, self.today, "completed", "Completed"),
            ("cancelled", -1, self.today, "completed", "Completed"),
            ("cancelled", -1, None, "cancelled", "Cancelled"),
        )
        with patch("django.utils.timezone.now", return_value=self._at(self.today)):
            for stored, offset, actual, code, label in scenarios:
                with self.subTest(stored=stored, offset=offset, actual=actual):
                    self.dose.dose_status = stored
                    self.dose.scheduled_date = (
                        self.today + datetime.timedelta(days=offset) if offset is not None else None)
                    self.dose.actual_date = actual
                    self.dose.save(update_fields=["dose_status", "scheduled_date", "actual_date"])
                    self.assertEqual(self.dose.appointment_status(today=self.today), code)
                    self.assertEqual(self.dose.effective_status, label)
                    manage = self.client.get(reverse(
                        "vaccination:nurse_schedule_manage", args=[self.schedule.pk]))
                    row = next(row for row in manage.context["dose_rows"] if row["dose"].pk == self.dose.pk)
                    self.assertEqual(row["status_label"], label)
                    self.assertEqual(row["status_class"], code)
                    self.assertEqual(row["is_missed"], code == "missed")
                    detail = self._details()
                    displayed = self._detail_dose(detail)
                    self.assertEqual(displayed.effective_status, label)
                    section = self._section(detail, "B. Vaccination Schedules")
                    dose_row = next(row for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", section, re.S)
                                    if "2nd Dose" in row)
                    badge = f'<span class="status-badge vaccination-status status-{code}">{label}</span>'
                    self.assertIn(badge, dose_row)
                    self.assertContains(manage, badge)
                    self.dose.refresh_from_db()
                    self.assertEqual(self.dose.dose_status, stored)
        self.assertFalse(SMSLog.objects.exists())

    def test_manila_midnight_ui_parity_with_utc_override(self):
        self.client.force_login(self.nurse)
        midnight = datetime.datetime(2026, 11, 11, 16, 0, tzinfo=datetime.timezone.utc)
        with timezone.override("UTC"), patch("django.utils.timezone.now", return_value=midnight):
            manage = self.client.get(reverse(
                "vaccination:nurse_schedule_manage", args=[self.schedule.pk]))
            row = next(row for row in manage.context["dose_rows"] if row["dose"].pk == self.dose.pk)
            self.assertEqual(row["status_label"], "Missed")
            self.assertEqual(self._detail_dose(self._details()).effective_status, "Missed")

    def test_missed_schedule_status_uses_only_current_missed_event(self):
        self.client.force_login(self.nurse)
        self._log(reminder_type="3days_before")
        self._log(reminder_type="on_day")
        self._log(scheduled_date=self.dose.scheduled_date - datetime.timedelta(days=3))
        self._log(dose=self.first, scheduled_date=self.dose.scheduled_date)
        with patch("django.utils.timezone.now", return_value=self._at(self.today)):
            detail = self._details()
            displayed = self._detail_dose(detail)
            self.assertEqual(displayed.sms_reminder_status, "Pending")
            self.assertEqual(displayed.sms_reminder_status_code, "pending")
            self.assertNotIn("Missed Reminder Sent", self._section(detail, "B. Vaccination Schedules"))
            current = self._log()
            # A newer advance message must not override the current missed reminder.
            self._log(status="failed", reminder_type="3days_before")
            for status in ("sent", "queued", "failed", "unknown", "skipped"):
                with self.subTest(status=status):
                    current.status = status
                    current.save(update_fields=["status"])
                    detail = self._details()
                    displayed = self._detail_dose(detail)
                    label = "Missed Reminder Sent" if status == "sent" else dict(SMSLog.STATUS_CHOICES)[status]
                    self.assertEqual(displayed.sms_reminder_status, label)
                    self.assertEqual(displayed.sms_reminder_status_code, status)
                    self.assertIn(label, self._section(detail, "B. Vaccination Schedules"))

    def test_upcoming_reminder_keeps_latest_failure_visible(self):
        self.client.force_login(self.nurse)
        self.dose.scheduled_date = self.today + datetime.timedelta(days=3)
        self.dose.save(update_fields=['scheduled_date'])
        self._log(reminder_type='schedule_set')
        self._log(status='failed', reminder_type='manual_immediate')
        with patch('django.utils.timezone.now', return_value=self._at(self.today)):
            self.assertEqual(self._detail_dose(self._details()).sms_reminder_status, 'Failed')

    def test_history_keeps_one_row_per_event_and_hides_details_without_changing_logs(self):
        self.client.force_login(self.nurse)
        for reminder_type, _ in SMSLog.REMINDER_CHOICES:
            self._log(reminder_type=reminder_type, api_response='Saved gateway response.')
        self._log(scheduled_date=self.dose.scheduled_date - datetime.timedelta(days=3),
                  message="Earlier missed appointment message.")
        saved = list(SMSLog.objects.order_by('pk').values())
        with patch("django.utils.timezone.now", return_value=self._at(self.today)):
            detail = self._details()
        self.assertEqual({log.pk for log in detail.context["logs"]}, {log['id'] for log in saved})
        history = self._section(detail, "C. SMS Reminder History")
        rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', history, re.S)
        self.assertEqual(len(rows), len(saved) + 1)
        for row in rows[1:]:
            self.assertEqual(len(re.findall(r'<td\b', row)), 6)
        for log in saved:
            self.assertNotIn(log['message'], history)
            if log['api_response']:
                self.assertNotIn(log['api_response'], history)
        self.assertNotIn('Message and sending details', history)
        self.assertNotIn('<details', history)
        self.assertNotIn('<summary', history)
        self.assertIn("Advance Reminder", history)
        self.assertIn("Appointment Day Reminder", history)
        self.assertIn("Automatic Missed Appointment Reminder", history)
        self.assertIn("Manual Reminder", history)
        self.assertEqual(saved, list(SMSLog.objects.order_by('pk').values()))

    def test_completed_or_rescheduled_dose_keeps_history_but_not_missed_badge(self):
        self.client.force_login(self.nurse)
        old = self._log(message=missed_reminder_message(self.dose))
        with patch("django.utils.timezone.now", return_value=self._at(self.today)):
            for completed in (True, False):
                with self.subTest(completed=completed):
                    self.dose.actual_date = self.today if completed else None
                    self.dose.dose_status = "completed" if completed else "rescheduled"
                    if not completed:
                        self.dose.scheduled_date = self.today + datetime.timedelta(days=3)
                    self.dose.save(update_fields=["actual_date", "dose_status", "scheduled_date"])
                    detail = self._details()
                    self.assertNotIn("Missed Reminder Sent", self._section(detail, "B. Vaccination Schedules"))
                    self.assertIn(old.pk, [log.pk for log in detail.context['logs']])
                    self.assertNotIn(old.message, self._section(detail, "C. SMS Reminder History"))
                    self.assertNotContains(self.client.get(reverse("vaccination:sms_log_list")), "Missed – SMS Sent")
        old.refresh_from_db()
        self.assertEqual(old.scheduled_date, self.today - datetime.timedelta(days=1))

    def test_page_refreshes_do_not_send_sms(self):
        self.client.force_login(self.nurse)
        with patch("vaccination.missed_appointments.send_semaphore_sms") as gateway:
            for _ in range(2):
                self._details()
                self.client.get(reverse("vaccination:sms_log_list"))
                self.client.get(reverse("vaccination:nurse_schedule_manage", args=[self.schedule.pk]))
            gateway.assert_not_called()
        self.assertFalse(SMSLog.objects.exists())

    def test_manual_workflow_remains_independent_of_missed_consent_and_duplicate_claim(self):
        self.consent.delete()
        self.dose.scheduled_date = self.today + datetime.timedelta(days=2)
        self.dose.save(update_fields=["scheduled_date"])
        self.client.force_login(self.nurse)
        url = reverse("vaccination:sms_manual_send", args=[self.dose.pk])
        payload = {"reminder_type": "manual_immediate"}
        with patch("django.utils.timezone.now", return_value=self._at(self.today)), patch(
            "vaccination.sms.send_semaphore_sms", return_value=self._accepted(),
        ) as gateway:
            first = self.client.post(url, payload)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["status"], "sent")
            duplicate = self.client.post(url, payload)
            self.assertEqual(duplicate.status_code, 409)
            self.assertTrue(duplicate.json()["duplicate"])
            confirmed = self.client.post(url, {**payload, "confirm_duplicate": "1"})
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(gateway.call_count, 2)
        logs = SMSLog.objects.filter(dose=self.dose)
        self.assertEqual(logs.count(), 2)
        self.assertTrue(all(log.reminder_type == "manual_immediate" for log in logs))
        self.assertTrue(all(not log.event_key for log in logs))
        self.assertFalse(SMSLog.objects.filter(reminder_type="missed_appointment").exists())


@override_settings(SEMAPHORE_ENABLED=False)
class MissedAppointmentMigrationTests(_MissedAppointmentFixture, TestCase):
    def test_only_proven_gateway_acceptance_is_relabelled_without_history_loss(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps

        rows = []
        for reference, response in (
            ('real-id', [{'response': [{'message_id': 'real-id', 'status': 'Queued'}]}]),
            ('0', [{'response': [{'message_id': 0, 'status': 'Queued (simulated)'}]}]),
            ('unconfirmed', [{'response': [{'message_id': 'unconfirmed'}]}]),
            ('invalid-history', 'not JSON'),
        ):
            rows.append(SMSLog.objects.create(
                dose=self.dose, patient=self.patient, bite_case=self.case,
                reminder_type='missed_appointment', status='queued',
                scheduled_date=self.dose.scheduled_date, recipient='639173334444',
                message='Saved original reminder', semaphore_message_id=reference,
                api_response=json.dumps(response) if isinstance(response, list) else response,
            ))
        migration = import_module('vaccination.migrations.0010_sms_outcome_statuses')
        migration.correct_accepted_missed_logs(apps, SimpleNamespace(connection=connections['default']))
        self.assertEqual(SMSLog.objects.count(), 4)
        for index, row in enumerate(rows):
            original = row.api_response
            row.refresh_from_db()
            self.assertEqual(row.status, 'sent' if index == 0 else 'queued')
            self.assertEqual(row.api_response, original)
            self.assertEqual(row.message, 'Saved original reminder')


@skipUnless(connections["default"].vendor == "mysql", "Durable claim concurrency requires MySQL")
@override_settings(SEMAPHORE_ENABLED=True, ABTC_APPOINTMENT_CUTOFF_TIME=None)
class MissedAppointmentClaimTests(_MissedAppointmentFixture, TransactionTestCase):
    """Use independent connections: TestCase's outer transaction cannot prove a commit."""

    def _worker_send(self, **kwargs):
        connections.close_all()
        try:
            return self._send(**kwargs)
        finally:
            connections.close_all()

    def test_inflight_claim_is_committed_and_second_caller_cannot_send(self):
        entered, release = Event(), Event()

        def gateway_call(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test gateway was not released")
            return self._accepted()

        with patch(
            "vaccination.missed_appointments.send_semaphore_sms", side_effect=gateway_call,
        ) as gateway, ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self._worker_send)
            try:
                self.assertTrue(entered.wait(10), "First caller did not reach gateway")
                # This connection belongs to the main thread, not either sender.
                claim = SMSLog.objects.get(dose=self.dose)
                self.assertEqual(claim.status, "unknown")
                self.assertEqual(claim.attempt_count, 1)
                self.assertFalse(json.loads(claim.api_response)[-1]["retryable"])
                second = workers.submit(self._worker_send, retry_failed=True)
                duplicate, result = second.result(timeout=10)
                self.assertEqual(duplicate.pk, claim.pk)
                self.assertEqual(duplicate.status, "unknown")
                self.assertTrue(result["skipped"])
                gateway.assert_called_once()
            finally:
                release.set()
            sent, result = first.result(timeout=10)
        self.assertEqual(sent.status, "sent")
        self.assertTrue(result["success"])
        self.assertEqual(SMSLog.objects.filter(dose=self.dose).count(), 1)
        gateway.assert_called_once()

    def test_worker_crash_leaves_durable_unknown_claim_and_blocks_retry(self):
        # BaseException models a terminated worker, not a recoverable provider exception.
        with patch(
            "vaccination.missed_appointments.send_semaphore_sms", side_effect=SystemExit("worker stopped"),
        ) as gateway, ThreadPoolExecutor(max_workers=1) as workers:
            crashed = workers.submit(self._worker_send)
            with self.assertRaises(SystemExit):
                crashed.result(timeout=10)
            claim = SMSLog.objects.get(dose=self.dose)
            self.assertEqual(claim.status, "unknown")
            self.assertEqual(claim.attempt_count, 1)
            duplicate, result = self._send(now=self._at(self.today, 12), retry_failed=True)
            self.assertEqual(duplicate.pk, claim.pk)
            self.assertTrue(result["skipped"])
            gateway.assert_called_once()
        claim.refresh_from_db()
        self.assertEqual(claim.status, "unknown")
        self.assertEqual(SMSLog.objects.filter(dose=self.dose).count(), 1)


@override_settings(SEMAPHORE_ENABLED=True)
class MissedAppointmentRecoveryTests(_MissedAppointmentFixture, TestCase):

    def setUp(self):
        super().setUp()
        missed = patch(
            "vaccination.missed_appointments.send_semaphore_sms",
            return_value=self._accepted(),
        )
        self.addCleanup(missed.stop)
        self.missed_gateway = missed.start()
        regular = patch(
            "vaccination.sms.send_semaphore_sms", return_value=self._accepted(),
        )
        self.addCleanup(regular.stop)
        self.regular_gateway = regular.start()

    def _new_dose(self, age=2, **overrides):
        values = {
            "schedule": self.schedule, "dose_label": "Recovery candidate",
            "scheduled_date": self.today - datetime.timedelta(days=age),
            "dose_status": "scheduled",
        }
        values.update(overrides)
        return VaccineDose.objects.create(**values)

    def _event_key(self, dose):
        return (
            f"missed_appointment:{self.patient.pk}:{self.case.pk}:"
            f"{dose.pk}:{dose.scheduled_date.isoformat()}"
        )

    def _log(self, dose=None, keyed=True, **overrides):
        dose = dose or self.dose
        values = {
            "dose": dose, "patient": self.patient, "bite_case": self.case,
            "reminder_type": "missed_appointment", "recipient": "639173334444",
            "message": missed_reminder_message(dose),
            "scheduled_date": dose.scheduled_date, "status": "pending",
            "event_key": self._event_key(dose) if keyed else None,
            "attempt_count": 0, "semaphore_message_id": "", "api_response": "",
        }
        values.update(overrides)
        return SMSLog.objects.create(**values)

    def _command(self, *args, today=None, now=None, stdout=None):
        today = today or self.today
        output = stdout if stdout is not None else StringIO()
        with patch("django.utils.timezone.now", return_value=now or self._at(today)):
            call_command("send_vaccination_reminders", *args, stdout=output)
        return output.getvalue()

    def _catchups(self, count=26):
        return [
            self._new_dose(age=2 + index % 6, dose_label=f"Catch-up {index + 1}")
            for index in range(count)
        ]

    def _regular_doses(self):
        return self._new_dose(age=0), self._new_dose(age=-3)

    def _assert_november_12_recovered_once(self, processing_day):
        self.dose.scheduled_date = self.today
        self.dose.save(update_fields=["scheduled_date"])
        saved_doses = list(self.schedule.doses.values())
        processed_at = self._at(processing_day, 15, 37)
        self._command("--type", "missed", today=processing_day, now=processed_at)

        log = SMSLog.objects.get(dose=self.dose)
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.sent_at, processed_at)
        self.assertEqual(log.created_at, processed_at)
        self.assertEqual(log.scheduled_date, datetime.date(2026, 11, 12))
        self.assertEqual(log.event_key, self._event_key(self.dose))
        self.assertEqual(log.attempt_count, 1)
        self.assertEqual(log.semaphore_message_id, "missed-1")
        self.assertEqual(log.message, missed_reminder_message(self.dose))
        self.assertIn("2nd Dose on November 12, 2026", log.message)
        attempt = json.loads(log.api_response)[-1]
        self.assertEqual(attempt["at"], processed_at.isoformat())
        self.assertEqual(attempt["status"], "sent")
        saved_log = SMSLog.objects.values().get(pk=log.pk)

        # Keep running through the last eligible day, without changing the event.
        for offset in range(1, (self.today + datetime.timedelta(days=7) - processing_day).days + 1):
            self._command("--type", "missed", today=processing_day + datetime.timedelta(days=offset))
        self.missed_gateway.assert_called_once_with("639173334444", log.message)
        self.regular_gateway.assert_not_called()
        self.assertEqual(SMSLog.objects.count(), 1)
        self.assertEqual(SMSLog.objects.values().get(pk=log.pk), saved_log)
        self.assertEqual(list(self.schedule.doses.values()), saved_doses)

    def test_november_12_recovered_once_on_november_14(self):
        self._assert_november_12_recovered_once(datetime.date(2026, 11, 14))

    def test_november_12_recovered_once_on_november_15(self):
        self._assert_november_12_recovered_once(datetime.date(2026, 11, 15))

    def _assert_pristine_pending_reused(self, keyed):
        original_time = self._at(self.today, 8)
        with patch("django.utils.timezone.now", return_value=original_time):
            pending = self._log(
                keyed=keyed, patient=self.patient if keyed else None,
                bite_case=self.case if keyed else None,
            )
        saved_dose = VaccineDose.objects.values().get(pk=self.dose.pk)
        processing_day = self.today + datetime.timedelta(days=2)
        processed_at = self._at(processing_day, 14, 25)
        self._command("--type", "missed", today=processing_day, now=processed_at)

        pending.refresh_from_db()
        self.assertEqual(list(SMSLog.objects.values_list("pk", flat=True)), [pending.pk])
        self.assertEqual(pending.status, "sent")
        self.assertEqual(pending.event_key, self._event_key(self.dose))
        self.assertEqual(pending.patient_id, self.patient.pk)
        self.assertEqual(pending.bite_case_id, self.case.pk)
        self.assertEqual(pending.attempt_count, 1)
        self.assertEqual(pending.semaphore_message_id, "missed-1")
        self.assertEqual(pending.created_at, original_time)
        self.assertEqual(pending.sent_at, processed_at)
        self.assertEqual(json.loads(pending.api_response)[-1]["at"], processed_at.isoformat())
        self.assertEqual(VaccineDose.objects.values().get(pk=self.dose.pk), saved_dose)
        self.missed_gateway.assert_called_once_with("639173334444", missed_reminder_message(self.dose))

    def test_keyed_pristine_pending_reuses_same_row(self):
        self._assert_pristine_pending_reused(keyed=True)

    def test_legacy_pristine_pending_reuses_same_row(self):
        self._assert_pristine_pending_reused(keyed=False)

    def test_ambiguous_pending_is_never_resent(self):
        for keyed in (True, False):
            for evidence in (
                {"attempt_count": 1},
                {"semaphore_message_id": "possible-submission"},
                {"semaphore_message_id": "0"},
                {"api_response": "[]"},
                {"api_response": "Unconfirmed provider response"},
            ):
                with self.subTest(keyed=keyed, evidence=evidence):
                    dose = self._new_dose()
                    pending = self._log(dose=dose, keyed=keyed, **evidence)
                    saved = SMSLog.objects.values().get(pk=pending.pk)
                    for retry in (False, True):
                        log, result = send_missed_appointment_reminder(
                            dose.pk, today=self.today, now=self._at(self.today, 12),
                            retry_failed=retry, expected_scheduled_date=dose.scheduled_date,
                        )
                        self.assertEqual(log.pk, pending.pk)
                        self.assertTrue(result["skipped"])
                    self.assertEqual(SMSLog.objects.filter(dose=dose).count(), 1)
                    self.assertEqual(SMSLog.objects.values().get(pk=pending.pk), saved)
        self.missed_gateway.assert_not_called()

    def test_sent_and_unknown_events_are_excluded_and_blocked(self):
        for keyed in (True, False):
            for status in ("sent", "unknown"):
                with self.subTest(keyed=keyed, status=status):
                    dose = self._new_dose()
                    previous = self._log(dose=dose, keyed=keyed, status=status)
                    saved = SMSLog.objects.values().get(pk=previous.pk)
                    self.assertFalse(missed_reminder_doses(today=self.today).filter(pk=dose.pk).exists())
                    log, result = send_missed_appointment_reminder(
                        dose.pk, today=self.today, now=self._at(self.today), retry_failed=True,
                    )
                    self.assertEqual(log.pk, previous.pk)
                    self.assertTrue(result["skipped"])
                    self.assertEqual(SMSLog.objects.filter(dose=dose).count(), 1)
                    self.assertEqual(SMSLog.objects.values().get(pk=previous.pk), saved)
        self.missed_gateway.assert_not_called()

    def test_other_dates_doses_and_reminder_types_do_not_block(self):
        self._log(keyed=False, status="sent", scheduled_date=self.today - datetime.timedelta(days=3))
        self._log(keyed=False, status="unknown", reminder_type="on_day")
        self._log(dose=self.first, keyed=False, status="sent", scheduled_date=self.dose.scheduled_date)
        saved = list(SMSLog.objects.order_by("pk").values())
        self.assertTrue(missed_reminder_doses(today=self.today).filter(pk=self.dose.pk).exists())
        log, result = self._send(expected_scheduled_date=self.dose.scheduled_date)
        self.assertTrue(result["success"])
        self.assertEqual(log.status, "sent")
        self.assertEqual(SMSLog.objects.count(), 4)
        self.assertEqual(list(SMSLog.objects.exclude(pk=log.pk).order_by("pk").values()), saved)
        self.missed_gateway.assert_called_once()

    def test_legacy_sent_blocks_pristine_pending(self):
        for keyed in (True, False):
            with self.subTest(keyed=keyed):
                dose = self._new_dose()
                self._log(dose=dose, keyed=False, status="sent")
                self._log(dose=dose, keyed=keyed)
                saved = list(SMSLog.objects.filter(dose=dose).order_by("pk").values())
                self.assertFalse(missed_reminder_doses(today=self.today).filter(pk=dose.pk).exists())
                _, result = send_missed_appointment_reminder(
                    dose.pk, today=self.today, now=self._at(self.today), retry_failed=True,
                )
                self.assertTrue(result["skipped"])
                self.assertEqual(list(SMSLog.objects.filter(dose=dose).order_by("pk").values()), saved)
        self.missed_gateway.assert_not_called()

    def test_actual_time_without_date_blocks_recovery(self):
        VaccineDose.objects.filter(pk=self.dose.pk).update(actual_time=datetime.time(9, 15))
        self.dose.refresh_from_db()
        self.assertIsNone(self.dose.actual_date)
        self.assertFalse(missed_reminder_is_due(self.dose, today=self.today))
        self.assertFalse(missed_reminder_doses(today=self.today).exists())
        log, result = self._send()
        self.assertIsNone(log)
        self.assertTrue(result["skipped"])
        self.assertFalse(SMSLog.objects.exists())
        self.missed_gateway.assert_not_called()

    def test_ineligible_current_doses_are_not_recovered(self):
        for changes in (
            {"dose_status": "completed"},
            {"dose_status": "cancelled"},
            {"dose_status": "rescheduled", "scheduled_date": self.today + datetime.timedelta(days=2)},
            {"actual_date": self.today},
        ):
            with self.subTest(changes=changes):
                dose = self._new_dose(**changes)
                self.assertFalse(missed_reminder_is_due(dose, today=self.today))
                self.assertFalse(missed_reminder_doses(today=self.today).filter(pk=dose.pk).exists())
                saved = VaccineDose.objects.values().get(pk=dose.pk)
                log, result = send_missed_appointment_reminder(
                    dose.pk, today=self.today, now=self._at(self.today), retry_failed=True,
                )
                self.assertIsNone(log)
                self.assertTrue(result["skipped"])
                self.assertEqual(VaccineDose.objects.values().get(pk=dose.pk), saved)
        self.assertFalse(SMSLog.objects.exists())
        self.missed_gateway.assert_not_called()

    def test_superseded_expected_date_is_checked_under_lock(self):
        expected_date = self.dose.scheduled_date
        current_date = self.today - datetime.timedelta(days=2)

        def reschedule_before_claim(dose_id, **kwargs):
            # The command has already selected the old date; both dates are eligible.
            VaccineDose.objects.filter(pk=dose_id).update(
                scheduled_date=current_date, dose_status="rescheduled",
            )
            return send_missed_appointment_reminder(dose_id, **kwargs)

        with patch(
            "vaccination.missed_appointments.send_missed_appointment_reminder",
            side_effect=reschedule_before_claim,
        ) as service:
            self._command("--type", "missed")
        service.assert_called_once_with(
            self.dose.pk, today=self.today, retry_failed=False,
            expected_scheduled_date=expected_date,
        )
        self.dose.refresh_from_db()
        self.assertEqual(self.dose.scheduled_date, current_date)
        self.assertTrue(missed_reminder_is_due(self.dose, today=self.today))
        self.assertFalse(SMSLog.objects.exists())
        self.missed_gateway.assert_not_called()

    def _assert_change_after_claim_blocks_http(self, **changes):
        original_save = SMSLog.save
        claims = []

        def save_then_change(log, *args, **kwargs):
            result = original_save(log, *args, **kwargs)
            if log.dose_id == self.dose.pk and log.status == "unknown":
                claim = SMSLog.objects.get(pk=log.pk)
                self.assertEqual(claim.attempt_count, 1)
                self.assertEqual(claim.status, "unknown")
                claims.append(claim.pk)
                VaccineDose.objects.filter(pk=self.dose.pk).update(**changes)
            return result

        with patch.object(SMSLog, "save", autospec=True, side_effect=save_then_change):
            log, result = self._send(expected_scheduled_date=self.dose.scheduled_date)
        self.assertEqual(claims, [log.pk])
        self.assertTrue(result["skipped"])
        self.assertFalse(result["success"])
        self.assertIn("changed before submission", result["error"])
        log.refresh_from_db()
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.attempt_count, 0)
        self.assertEqual(log.scheduled_date, self.dose.scheduled_date)
        self.assertEqual(log.semaphore_message_id, "")
        self.assertEqual(json.loads(log.api_response)[-1]["status"], "skipped")
        self.assertEqual(SMSLog.objects.count(), 1)
        self.dose.refresh_from_db()
        for field, value in changes.items():
            self.assertEqual(getattr(self.dose, field), value)
        self.missed_gateway.assert_not_called()

    def test_actual_time_recorded_after_claim_prevents_http(self):
        self._assert_change_after_claim_blocks_http(actual_time=datetime.time(9, 45))
        self.assertIsNone(self.dose.actual_date)

    def test_date_changed_after_claim_prevents_http(self):
        self._assert_change_after_claim_blocks_http(
            scheduled_date=self.today - datetime.timedelta(days=2),
        )
        self.assertTrue(missed_reminder_is_due(self.dose, today=self.today))

    def test_day_seven_is_recovered_but_day_eight_is_excluded(self):
        VaccineDose.objects.filter(pk=self.dose.pk).update(
            scheduled_date=self.today - datetime.timedelta(days=7),
        )
        self.dose.refresh_from_db()
        expired = self._new_dose(age=8)
        self.assertTrue(missed_reminder_is_due(self.dose, today=self.today))
        self.assertFalse(missed_reminder_is_due(expired, today=self.today))
        self.assertEqual(list(missed_reminder_doses(today=self.today)), [self.dose])
        log, result = self._send()
        self.assertEqual(log.status, "sent")
        self.assertTrue(result["success"])
        log, result = send_missed_appointment_reminder(
            expired.pk, today=self.today, now=self._at(self.today), retry_failed=True,
        )
        self.assertIsNone(log)
        self.assertTrue(result["skipped"])
        self.assertFalse(SMSLog.objects.filter(dose=expired).exists())
        self.missed_gateway.assert_called_once()

    def test_large_backlog_holds_only_catchups(self):
        from django.core.management.base import CommandError

        catchups = self._catchups()
        on_day, advance = self._regular_doses()
        saved_doses = list(self.schedule.doses.values())
        output = StringIO()
        with self.assertRaisesMessage(CommandError, "backlog review required=True"):
            self._command(stdout=output)
        self.assertIn("catch-up candidates=26", output.getvalue())
        self.assertIn("26 catch-up reminders require administrator review", output.getvalue())
        self.assertEqual(set(SMSLog.objects.values_list("dose_id", "reminder_type", "status")), {
            (self.dose.pk, "missed_appointment", "sent"),
            (on_day.pk, "on_day", "sent"),
            (advance.pk, "3days_before", "sent"),
        })
        self.assertEqual(SMSLog.objects.count(), 3)
        self.assertFalse(SMSLog.objects.filter(dose__in=catchups).exists())
        self.assertEqual(list(self.schedule.doses.values()), saved_doses)
        self.missed_gateway.assert_called_once()
        self.assertEqual(self.regular_gateway.call_count, 2)

    def test_backlog_dry_run_lists_held_batch_without_writes(self):
        catchups = self._catchups()
        self._regular_doses()
        self._log(dose=catchups[0])
        saved_logs = list(SMSLog.objects.order_by("pk").values())
        saved_doses = list(self.schedule.doses.values())
        # A held batch must be reviewable without raising CommandError.
        output = self._command("--dry-run")
        self.assertIn("26 catch-up reminders require administrator review", output)
        self.assertEqual(output.count("[DRY RUN] Would send missed appointment reminder"), 27)
        self.assertIn("[DRY RUN] Would send on_day", output)
        self.assertIn("[DRY RUN] Would send 3days", output)
        for dose in [self.dose, *catchups]:
            self.assertIn(missed_reminder_message(dose), output)
        self.assertEqual(list(SMSLog.objects.order_by("pk").values()), saved_logs)
        self.assertEqual(list(self.schedule.doses.values()), saved_doses)
        self.missed_gateway.assert_not_called()
        self.regular_gateway.assert_not_called()

    def test_approval_sends_recent_backlog_not_historical(self):
        catchups = self._catchups()
        expired = self._new_dose(age=8)
        ancient = self._new_dose(age=60)
        saved_doses = list(self.schedule.doses.values())
        output = self._command("--type", "missed", "--approve-missed-backlog")
        self.assertIn("catch-up candidates=26", output)
        self.assertNotIn("require administrator review", output)
        self.assertEqual(set(SMSLog.objects.values_list("dose_id", flat=True)), {
            dose.pk for dose in [self.dose, *catchups]
        })
        self.assertEqual(SMSLog.objects.filter(status="sent").count(), 27)
        self.assertEqual(SMSLog.objects.count(), 27)
        self.assertFalse(SMSLog.objects.filter(dose__in=[expired, ancient]).exists())
        self.assertEqual(list(self.schedule.doses.values()), saved_doses)
        self.assertEqual(self.missed_gateway.call_count, 27)
        self.regular_gateway.assert_not_called()

    def test_sent_catchups_do_not_trigger_review(self):
        catchups = self._catchups()
        previous = self._log(dose=catchups[0], keyed=False, status="sent", attempt_count=1)
        saved = SMSLog.objects.values().get(pk=previous.pk)
        output = self._command("--type", "missed")
        self.assertIn("catch-up candidates=25", output)
        self.assertNotIn("require administrator review", output)
        self.assertEqual(SMSLog.objects.values().get(pk=previous.pk), saved)
        self.assertEqual(SMSLog.objects.count(), 27)
        self.assertEqual(SMSLog.objects.filter(status="sent").count(), 27)
        self.assertEqual(self.missed_gateway.call_count, 26)

    def test_recovery_retries_failures_only_with_explicit_opt_in(self):
        self.missed_gateway.return_value = {
            "success": False, "retryable": True, "error": "Provider rejected message",
        }
        failed, result = self._send()
        self.assertEqual(failed.status, "failed")
        self.assertFalse(result["success"])
        saved = SMSLog.objects.values().get(pk=failed.pk)
        processing_day = self.today + datetime.timedelta(days=2)
        self.missed_gateway.return_value = self._accepted()
        self._command(
            "--type", "missed", "--force", "--approve-missed-backlog", today=processing_day,
        )
        self.assertEqual(SMSLog.objects.values().get(pk=failed.pk), saved)
        self.missed_gateway.assert_called_once()

        self._command("--type", "missed", "--retry-failed", today=processing_day)
        failed.refresh_from_db()
        self.assertEqual(failed.status, "sent")
        self.assertEqual(failed.attempt_count, 2)
        self.assertEqual(failed.sent_at, self._at(processing_day))
        self.assertEqual(SMSLog.objects.count(), 1)
        self.assertEqual(self.missed_gateway.call_count, 2)


class NurseTriggeredScheduleTests(TestCase):

    def setUp(self):
        self.patient = Patient.objects.create(
            patient_number="PAT-T0001", first_name="Test", last_name="Patient",
            birthdate=datetime.date(1995, 5, 5), contact_number="09170000000",
        )
        self.nurse = U.objects.create_user("n.t", password="pw12345678", role="nurse")
        self.doctor = U.objects.create_user("d.t", password="pw12345678", role="doctor")
        self.admin = U.objects.create_user("a.t", password="pw12345678", role="admin")

    def _case(self, number="ABC-T0001"):
        return AnimalBiteCase.objects.create(
            case_number=number, patient=self.patient, created_by=self.admin,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )

    # -------------------------------------------------- no auto-creation
    def test_creating_a_case_does_not_create_a_schedule(self):
        case = self._case()
        self.assertFalse(VaccinationSchedule.objects.filter(bite_case=case).exists())

    def test_case_create_view_does_not_create_a_schedule(self):
        """The admin case_create view must not materialise a schedule."""
        self.client.force_login(self.admin)
        before = VaccinationSchedule.objects.count()
        response = self.client.get(reverse("bite_cases:case_create"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(VaccinationSchedule.objects.count(), before)

    # -------------------------------------------------- RBAC
    def test_doctor_cannot_set_schedule(self):
        case = self._case()
        self.client.force_login(self.doctor)
        self.client.get(reverse("vaccination:schedule_trigger", args=[case.pk]))
        self.client.get(reverse("vaccination:schedule_create", args=[case.pk]))
        self.assertFalse(VaccinationSchedule.objects.filter(bite_case=case).exists())

    def test_admin_may_set_schedule(self):
        case = self._case()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("vaccination:schedule_trigger", args=[case.pk]))
        self.assertEqual(response.status_code, 200)

    def test_nurse_can_open_trigger_page(self):
        case = self._case()
        self.client.force_login(self.nurse)
        response = self.client.get(reverse("vaccination:schedule_trigger", args=[case.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Doses that will be created")

    # -------------------------------------------------- nurse triggers
    def test_nurse_trigger_creates_schedule_and_doses(self):
        case = self._case()
        self.client.force_login(self.nurse)
        self.client.post(reverse("vaccination:schedule_trigger", args=[case.pk]), follow=True)

        sched = VaccinationSchedule.objects.get(bite_case=case)
        self.assertEqual(sched.created_by, self.nurse)
        doses = list(VaccineDose.objects.filter(schedule=sched).order_by("id"))
        self.assertEqual(len(doses), 5)
        # Only the 1st dose is dated; the rest wait for the nurse to assign them.
        self.assertEqual(doses[0].scheduled_date, timezone.localdate())
        self.assertEqual(doses[0].dose_status, "scheduled")
        for dose in doses[1:]:
            self.assertIsNone(dose.scheduled_date)
            self.assertEqual(dose.dose_status, "pending")
        self.assertTrue(
            AuditLog.objects.filter(
                action="CREATE_SCHEDULE", description__contains=case.case_number
            ).exists()
        )

    def test_triggering_twice_does_not_duplicate(self):
        case = self._case()
        self.client.force_login(self.nurse)
        url = reverse("vaccination:schedule_trigger", args=[case.pk])
        self.client.post(url, follow=True)
        self.client.post(url, follow=True)
        self.assertEqual(VaccinationSchedule.objects.filter(bite_case=case).count(), 1)
        self.assertEqual(VaccineDose.objects.filter(schedule__bite_case=case).count(), 5)

    # -------------------------------------------------- nurse dashboard queue
    def test_nurse_dashboard_lists_awaiting_cases(self):
        case = self._case()
        self.client.force_login(self.nurse)
        response = self.client.get("/nurse/")
        self.assertContains(response, "Cases Awaiting Schedule")
        self.assertContains(response, case.case_number)
        self.assertEqual(response.context["awaiting_schedule_count"], 1)

        self.client.post(reverse("vaccination:schedule_trigger", args=[case.pk]), follow=True)
        response = self.client.get("/nurse/")
        self.assertEqual(response.context["awaiting_schedule_count"], 0)
        self.assertContains(response, "Every case has a schedule")

    # -------------------------------------------------- helpers
    def test_dose_days_defaults_and_setting_override(self):
        SystemSetting.objects.all().delete()
        self.assertEqual(get_dose_days(), [0, 3, 7, 14, 28])

        s = SystemSetting.objects.create(key="vaccination_days", value="0,7,21")
        self.assertEqual(get_dose_days(), [0, 7, 21])

        # a malformed setting falls back rather than exploding
        s.value = "not,numbers"
        s.save()
        self.assertEqual(get_dose_days(), [0, 3, 7, 14, 28])

    def test_base_date_prefers_consultation_then_today(self):
        case = self._case()
        self.assertEqual(schedule_base_date(case), timezone.localdate())

        AnimalBiteCase.objects.filter(pk=case.pk).update(
            date_time_consultation=timezone.make_aware(datetime.datetime(2026, 9, 30, 10, 0))
        )
        case.refresh_from_db()
        self.assertEqual(schedule_base_date(case), datetime.date(2026, 9, 30))

    def test_ensure_schedule_is_idempotent_and_honours_settings(self):
        SystemSetting.objects.all().delete()
        SystemSetting.objects.create(key="vaccination_days", value="0,7,21")
        case = self._case()
        case.date_time_consultation = datetime.datetime(2026, 9, 30, 10, 0)
        case.save()

        sched, created = ensure_vaccination_schedule(case, created_by=self.nurse)
        self.assertTrue(created)
        doses = list(VaccineDose.objects.filter(schedule=sched).order_by("id"))
        self.assertEqual(len(doses), 3)
        # Only the 1st dose carries the base date; later doses stay undated.
        self.assertEqual(
            [d.scheduled_date for d in doses],
            [datetime.date(2026, 9, 30), None, None],
        )
        self.assertEqual([d.dose_status for d in doses], ["scheduled", "pending", "pending"])
        self.assertEqual([d.protocol_day for d in doses], [0, 7, 21])
        self.assertEqual([d.dose_label for d in doses], ["Day 0", "Day 7", "Day 21"])
        self.assertEqual(doses[1].expected_date(), datetime.date(2026, 10, 7))

        sched2, created2 = ensure_vaccination_schedule(case, created_by=self.nurse)
        self.assertFalse(created2)
        self.assertEqual(sched2.pk, sched.pk)
        self.assertEqual(VaccineDose.objects.filter(schedule=sched).count(), 3)


class NurseSchedulingTests(TestCase):
    """The nurse manually dates the doctor-created pending doses."""

    def setUp(self):
        self.patient = Patient.objects.create(
            patient_number="PAT-N0001", first_name="Nurse", last_name="Patient",
            birthdate=datetime.date(1995, 5, 5), contact_number="09170000001",
        )
        self.nurse = U.objects.create_user("n.s", password="pw12345678", role="nurse")
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-N0001", patient=self.patient, created_by=self.nurse,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )
        # date_time_consultation is auto_now_add; pin it so the protocol is fixed.
        AnimalBiteCase.objects.filter(pk=self.case.pk).update(
            date_time_consultation=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0))
        )
        self.case.refresh_from_db()
        self.schedule, _ = ensure_vaccination_schedule(self.case, created_by=self.nurse)
        self.first, self.second, self.third = list(self.schedule.doses.order_by("id"))[:3]
        self.stock = VaccineInventory.objects.create(
            vaccine_name="Rabies Vaccine", brand="Verorab", batch_number="LOT-1",
            expiration_date=datetime.date(2027, 1, 1),
            quantity_received=10, quantity_available=10, quantity_used=0,
        )

    def _payload(self, dose, **overrides):
        data = {
            "dose_label": dose.dose_label,
            "dose_status": "scheduled",
            "scheduled_date": "",
            "actual_date": "",
            "vaccine_brand": "",
            "batch_number": "",
            "expiration_date": "",
            "remarks": "",
        }
        data.update(overrides)
        return data

    def _administer(self, **overrides):
        payload = self._payload(
            self.second,
            dose_status="completed",
            scheduled_date="2026-10-04",
            actual_date="2026-10-04",
            vaccine_brand="Verorab",
            batch_number="LOT-1",
        )
        payload.update(overrides)
        return self.client.post(
            reverse("vaccination:dose_update", args=[self.second.pk]), payload
        )

    # -------------------------------------------------- pending date selection
    def test_pending_dose_form_does_not_prefill_a_calculated_protocol_date(self):
        form = VaccineDoseForm(instance=self.second)
        self.assertIsNone(form.initial["scheduled_date"])
        self.assertEqual(form.initial["dose_status"], "scheduled")
        # The doctor's pending dose itself is untouched until the nurse saves.
        self.second.refresh_from_db()
        self.assertEqual(self.second.dose_status, "pending")
        self.assertIsNone(self.second.scheduled_date)

    # -------------------------------------------------- assign a date
    def test_nurse_assigning_protocol_date_schedules_dose(self):
        self.client.force_login(self.nurse)
        response = self.client.post(
            reverse("vaccination:dose_update", args=[self.second.pk]),
            self._payload(self.second, scheduled_date="2026-10-04"),
        )
        self.assertEqual(response.status_code, 302)
        self.second.refresh_from_db()
        self.assertEqual(self.second.scheduled_date, datetime.date(2026, 10, 4))
        self.assertEqual(self.second.dose_status, "scheduled")
        # Scheduling never touches inventory.
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity_available, 10)

    def test_confirming_a_date_promotes_a_pending_submission(self):
        self.client.force_login(self.nurse)
        self.client.post(
            reverse("vaccination:dose_update", args=[self.second.pk]),
            self._payload(self.second, scheduled_date="2026-10-04", dose_status="pending"),
        )
        self.second.refresh_from_db()
        self.assertEqual(self.second.dose_status, "scheduled")

    def test_later_date_is_allowed(self):
        self.client.force_login(self.nurse)
        self.client.post(
            reverse("vaccination:dose_update", args=[self.second.pk]),
            self._payload(self.second, scheduled_date="2026-10-15"),
        )
        self.second.refresh_from_db()
        self.assertEqual(self.second.scheduled_date, datetime.date(2026, 10, 15))
        self.assertEqual(self.second.dose_status, "scheduled")

    # -------------------------------------------------- protocol conflicts
    def test_fallback_protocol_offset_does_not_block_manual_date(self):
        self.client.force_login(self.nurse)
        response = self.client.post(
            reverse("vaccination:dose_update", args=[self.second.pk]),
            self._payload(self.second, scheduled_date="2026-10-03"),
        )
        self.assertEqual(response.status_code, 302)
        self.second.refresh_from_db()
        self.assertEqual(self.second.scheduled_date, datetime.date(2026, 10, 3))
        self.assertEqual(self.second.dose_status, "scheduled")

    def test_scheduling_without_a_date_is_rejected(self):
        self.client.force_login(self.nurse)
        response = self.client.post(
            reverse("vaccination:dose_update", args=[self.second.pk]),
            self._payload(self.second),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("scheduled_date", response.context["form"].errors)
        self.second.refresh_from_db()
        self.assertEqual(self.second.dose_status, "pending")

    # -------------------------------------------------- completion + stock
    def test_completion_requires_actual_administration_date(self):
        self.client.force_login(self.nurse)
        response = self._administer(actual_date="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("actual_date", response.context["form"].errors)
        self.second.refresh_from_db()
        self.assertNotEqual(self.second.dose_status, "completed")

    def test_stock_is_deducted_once_on_completion(self):
        self.client.force_login(self.nurse)
        self.assertEqual(self._administer().status_code, 302)

        self.second.refresh_from_db()
        self.assertEqual(self.second.dose_status, "completed")
        self.assertEqual(self.second.actual_date, datetime.date(2026, 10, 4))
        self.assertEqual(self.second.administered_by, self.nurse)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity_available, 9)
        self.assertEqual(self.stock.quantity_used, 1)

        # Re-saving the completed dose must not deduct again.
        self._administer()
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity_available, 9)
        self.assertEqual(self.stock.quantity_used, 1)

    # -------------------------------------------------- access + presentation
    def test_patient_cannot_update_a_dose(self):
        user = U.objects.create_user("pat.n", password="pw12345678", role="patient")
        self.patient.account = user
        self.patient.save(update_fields=["account"])
        self.client.force_login(user)
        response = self.client.post(
            reverse("vaccination:dose_update", args=[self.second.pk]),
            self._payload(self.second, scheduled_date="2026-10-04", dose_status="completed"),
            follow=True,
        )
        self.assertNotContains(response, "Dose Day 3 updated")
        self.second.refresh_from_db()
        self.assertEqual(self.second.dose_status, "pending")
        self.assertEqual(response.redirect_chain[-1][0], reverse("accounts:patient_portal"))

    def test_sms_is_blocked_until_a_date_is_assigned(self):
        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse("vaccination:send_sms_for_dose", args=[self.second.pk]), follow=True
        )
        self.assertEqual(response.redirect_chain[-1][0],
                         reverse("vaccination:schedule_detail", args=[self.schedule.pk]))
        self.assertFalse(SMSLog.objects.filter(dose=self.second).exists())

    def test_schedule_detail_marks_pending_doses(self):
        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse("vaccination:schedule_detail", args=[self.schedule.pk])
        )
        self.assertContains(response, "Pending Scheduling")

    def test_nurse_dashboard_counts_pending_doses(self):
        self.client.force_login(self.nurse)
        response = self.client.get("/nurse/")
        self.assertEqual(response.context["pending_scheduling_count"], 4)


class VaccinationRecordViewTests(TestCase):
    """The read-only record sheet shown from Vaccination Records."""

    def setUp(self):
        self.patient = Patient.objects.create(
            patient_number="PAT-R0001", first_name="Record", last_name="Patient",
            birthdate=datetime.date(1990, 4, 4), sex="female", civil_status="married",
            contact_number="09171234567", address="Purok 2, Dumingag",
            parent_guardian="Maria Patient", emergency_contact_name="Jose Patient",
            emergency_contact_number="09179876543",
        )
        self.nurse = U.objects.create_user("n.r", password="pw12345678", role="nurse")
        self.doctor = U.objects.create_user("d.r", password="pw12345678", role="doctor", first_name="Dana")
        self.admin = U.objects.create_user("a.r", password="pw12345678", role="admin")
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-R0001", patient=self.patient, created_by=self.admin,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 30),
            place_of_incident="Purok 2", incident_barangay="Dumingag",
            animal_ownership="owned", animal_vax_status="vaccinated",
            animal_condition="alive", body_part_affected="left forearm",
            number_of_wounds=2, wound_washed_immediately=True,
            first_aid_given="Washed with soap and water",
        )
        AnimalBiteCase.objects.filter(pk=self.case.pk).update(
            date_time_consultation=timezone.make_aware(datetime.datetime(2026, 10, 1, 10, 0))
        )
        self.case.refresh_from_db()
        self.schedule, _ = ensure_vaccination_schedule(self.case, created_by=self.nurse)
        self.url = reverse("vaccination:record_detail", args=[self.schedule.pk])

    def _assessment(self, **overrides):
        data = {
            "patient": self.patient,
            "bite_case": self.case,
            "assessed_by": self.doctor,
            "chief_complaint": "Dog bite on the left forearm",
            "exposure_narrative": "Patient was bitten by the family dog while feeding it.",
            "category_confirmed": "category_iii",
            "dose_protocol": "essen",
            "first_dose_date": datetime.date(2026, 10, 1),
            "vaccine_brand_plan": "Verorab",
            "vaccine_dose_ml": "0.5",
            "vaccine_quantity": 5,
            "treatment_plan": "Complete the Essen regimen and monitor the wound.",
            "dosage_instructions": "1 tablet every 8 hours for 7 days.",
            "follow_up_instructions": "Return on Day 3 and Day 7.",
            "additional_notes": "No known drug allergies.",
            "is_finalized": True,
            "status": "final",
        }
        data.update(overrides)
        return ClinicalAssessment.objects.create(**data)

    def _medicine(self):
        return MedicineInventory.objects.create(
            medicine_name="Amoxicillin", strength="500 mg", dosage_form="capsule",
            unit="capsule", batch_number="MED-1", expiration_date=datetime.date(2027, 1, 1),
            quantity_received=100, quantity_available=100,
        )

    # -------------------------------------------------- access
    def test_staff_roles_can_open_the_record(self):
        for user in (self.nurse, self.doctor, self.admin):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Vaccination Record")

    def test_patient_is_redirected_to_their_portal(self):
        user = U.objects.create_user("pat.r", password="pw12345678", role="patient")
        self.patient.account = user
        self.patient.save(update_fields=["account"])
        self.client.force_login(user)
        response = self.client.get(self.url, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("accounts:patient_portal"))
        self.assertNotContains(response, "record-hero__initials")

    def test_patient_cannot_open_another_patients_record(self):
        other = Patient.objects.create(
            patient_number="PAT-R0002", first_name="Other", last_name="Patient",
            birthdate=datetime.date(1990, 1, 1), contact_number="09170002222", address="Elsewhere",
        )
        user = U.objects.create_user("pat.x", password="pw12345678", role="patient")
        other.account = user
        other.save(update_fields=["account"])
        self.client.force_login(user)
        response = self.client.get(self.url, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("accounts:patient_portal"))
        self.assertNotContains(response, self.patient.patient_number)

    # -------------------------------------------------- structure
    def test_sections_render_in_required_vertical_order(self):
        self._assessment()
        self.client.force_login(self.nurse)
        content = self.client.get(self.url).content.decode()

        markers = [
            "1</span> Patient Information",
            "2</span> Animal Bite Case",
            "3</span> Vital Signs",
            "4</span> Clinical Assessment",
            "Chief Complaint",
            "Exposure Narrative (Verified)",
            "Bite Category",
            "Clinical Assessment and Findings",
            "Vaccination and Treatment Plan",
            "Vaccine brand",
            "Dose Schedule Records",
            "Treatment Prescription and Recommendations",
        ]
        positions = [content.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

    def test_patient_header_uses_initials_and_full_name(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)
        self.assertContains(response, "record-hero__initials")
        self.assertContains(response, ">RP</span>")
        self.assertContains(response, self.patient.full_name())

    def test_print_sheet_button_and_print_styles(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)
        self.assertContains(response, "Print Sheet")
        self.assertContains(response, "window.print()")
        self.assertContains(response, "@page { size: A4 portrait")
        self.assertContains(response, "abtc-print-only")
        self.assertContains(response, "abtc-letterhead")
        self.assertContains(response, "abtc-footer")
        self.assertContains(response, "print_official.css")
        self.assertContains(response, "Animal Bite Treatment Center")
        self.assertContains(response, "System-Generated Document")

    # -------------------------------------------------- empty states
    def test_no_assessment_and_no_vitals_empty_states(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)
        self.assertContains(response, "NO ASSESSMENT YET")
        self.assertContains(response, "No doctor assessment has been recorded for this case.")
        self.assertContains(response, "NO VITALS YET")
        self.assertContains(response, "No vital signs recorded for this patient.")
        self.assertNotContains(response, "Dose Schedule Records")

    # -------------------------------------------------- assessment content
    def test_assessment_details_and_dose_rows(self):
        self._assessment()
        medicine = self._medicine()
        Prescription.objects.create(
            assessment=ClinicalAssessment.objects.get(bite_case=self.case),
            patient=self.patient, bite_case=self.case,
            medicine=medicine, quantity=21,
        )
        first, second = list(self.schedule.doses.order_by("id"))[:2]
        VaccineDose.objects.filter(pk=first.pk).update(
            dose_status="completed", actual_date=datetime.date(2026, 10, 1)
        )
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)

        self.assertContains(response, "Dog bite on the left forearm")
        self.assertContains(response, "family dog while feeding it")
        self.assertContains(response, "Category III")
        self.assertContains(response, "Transdermal bites/scratches")
        self.assertContains(response, "5 Doses")
        self.assertContains(response, "Verorab")
        self.assertContains(response, "0.5")
        self.assertContains(response, "Complete the Essen regimen")
        self.assertContains(response, "Amoxicillin")
        self.assertContains(response, "21 capsules")
        self.assertContains(response, "1 tablet every 8 hours for 7 days.")
        self.assertContains(response, "Return on Day 3 and Day 7.")
        self.assertContains(response, "No known drug allergies.")
        # First dose was administered; the second is still undated.
        self.assertContains(response, "1st Dose")
        self.assertContains(response, "Completed")
        self.assertContains(response, "Not yet set")
        self.assertContains(response, "Pending Scheduling")

    def test_completed_requires_an_actual_administration_date(self):
        self._assessment()
        first = self.schedule.doses.order_by("id").first()
        VaccineDose.objects.filter(pk=first.pk).update(dose_status="completed", actual_date=None)
        self.client.force_login(self.nurse)
        content = self.client.get(self.url).content.decode()
        row = content[content.index("1st Dose"):]
        self.assertNotIn("status-pill--completed", row[:400])

    def test_vitals_render_when_recorded(self):
        VitalSign.objects.create(
            patient=self.patient, taken_at=timezone.now(),
            bp_systolic=120, bp_diastolic=80, temperature=36.7,
            pulse_rate=72, spo2=98, weight=55, height=160, bmi=21.48,
            pain_score=3, taken_by=self.nurse,
        )
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)
        self.assertContains(response, "120/80 mmHg")
        self.assertContains(response, "36.7")
        self.assertContains(response, "72 bpm")
        self.assertContains(response, "98 %")
        self.assertContains(response, "55")
        self.assertContains(response, "160")
        self.assertContains(response, "21.48")
        self.assertContains(response, "3 / 10")
        self.assertContains(response, "n.r")
        self.assertNotContains(response, "NO VITALS YET")

    def test_case_selection_does_not_mix_records(self):
        self._assessment(chief_complaint="Assessment for the first case")
        second_case = AnimalBiteCase.objects.create(
            case_number="ABC-R0002", patient=self.patient, created_by=self.admin,
            animal_type="cat", exposure_type="scratch",
            date_time_bite=datetime.datetime(2026, 10, 5, 9, 0),
        )
        self.client.force_login(self.nurse)
        response = self.client.get(self.url, {"case": second_case.pk})
        self.assertContains(response, second_case.case_number)
        self.assertContains(response, "NO ASSESSMENT YET")
        self.assertNotContains(response, "Assessment for the first case")

    def test_viewing_never_changes_inventory_stock(self):
        stock = VaccineInventory.objects.create(
            vaccine_name="Rabies Vaccine", brand="Verorab", batch_number="LOT-1",
            expiration_date=datetime.date(2027, 1, 1),
            quantity_received=10, quantity_available=10, quantity_used=0,
        )
        self.client.force_login(self.nurse)
        self.client.get(self.url)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity_available, 10)
        self.assertEqual(stock.quantity_used, 0)


class VaccinationRecordsListTests(TestCase):
    """The simplified patient listing at /vaccination/schedules/patients/."""

    def setUp(self):
        self.barangay = Barangay.objects.create(name="Caridad")
        self.other_barangay = Barangay.objects.create(name="San Pablo")
        self.nurse = U.objects.create_user("n.list", password="pw12345678", role="nurse")
        self.admin = U.objects.create_user("a.list", password="pw12345678", role="admin")
        self.patient = Patient.objects.create(
            patient_number="PAT-L0001", first_name="Eragene", middle_name="A",
            last_name="Patihan", birthdate=datetime.date(2005, 5, 5), sex="female",
            contact_number="09392339688", address="Purok 7, Caridad",
            barangay=self.barangay,
        )
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-L0001", patient=self.patient, created_by=self.admin,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )
        self.schedule = VaccinationSchedule.objects.create(
            bite_case=self.case, created_by=self.nurse,
        )
        self.url = reverse("vaccination:patient_schedule_list")

    def _add_patient_with_schedule(self, number, first, last, barangay=None, sex="male"):
        patient = Patient.objects.create(
            patient_number=number, first_name=first, last_name=last,
            birthdate=datetime.date(1998, 3, 3), sex=sex,
            contact_number="09170000001", address="Dumingag",
            barangay=barangay or self.barangay,
        )
        case = AnimalBiteCase.objects.create(
            case_number=f"ABC-{number}", patient=patient, created_by=self.admin,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 2, 9, 0),
        )
        VaccinationSchedule.objects.create(bite_case=case, created_by=self.nurse)
        return patient

    def _approve(self, patient, when):
        return PreRegistration.objects.create(
            first_name=patient.first_name, last_name=patient.last_name,
            birthdate=patient.birthdate, sex=patient.sex,
            contact_number=patient.contact_number, address=patient.address,
            barangay=patient.barangay.name if patient.barangay else "Caridad",
            medical_history="None", allergy_info="None", previous_rabies_vax="None",
            bite_datetime=datetime.datetime(2026, 10, 1, 9, 0),
            incident_place="Purok 7", incident_barangay="Caridad",
            animal_type="dog", animal_ownership="owned",
            animal_vaccination_status="vaccinated", animal_condition="alive",
            exposure_type="bite", body_part_affected="left forearm",
            status=PreRegistration.Status.CONVERTED,
            verified_by=self.admin, verified_at=when, converted_patient=patient,
            converted_case=patient.bite_cases.first(),
        )

    # -------------------------------------------------- structure
    def test_title_and_three_element_filter_row(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)

        self.assertContains(response, "<h1", html=False)
        self.assertContains(response, "Vaccination Records")
        self.assertContains(response, "Search by name, ID, contact, barangay, or address...")
        self.assertContains(response, "All Barangays")
        self.assertContains(response, ">Search</button>")
        self.assertNotContains(response, "Dose Status")
        self.assertNotContains(response, "Due filter")
        self.assertNotContains(response, "From (created)")
        self.assertNotContains(response, "To (created)")
        self.assertNotContains(response, ">Apply</button>")
        self.assertNotContains(response, ">Reset</button>")

    def test_table_has_the_five_required_columns(self):
        self.client.force_login(self.nurse)
        content = self.client.get(self.url).content.decode()

        self.assertEqual(content.count('class="table-header'), 5)
        for heading in ("#", "Patient", "Age/Sex", "Approved Date", "Actions"):
            self.assertIn(heading, content)
        self.assertLess(content.index(">#<"), content.index(">Patient<"))
        self.assertNotIn("Next Due", content)
        self.assertNotIn("Vaccination Status", content)
        self.assertNotIn("Progress", content)
        self.assertNotIn("Case #", content)

    def test_rows_are_numbered_and_continue_across_pages(self):
        for index in range(2, 17):
            self._add_patient_with_schedule(
                f"PAT-L{index:04d}", f"Patient{index}", "List",
            )
        self.client.force_login(self.nurse)

        page_one = self.client.get(
            self.url, {"q": "List", "barangay": self.barangay.pk}
        ).content.decode()
        page_two = self.client.get(
            self.url, {"q": "List", "barangay": self.barangay.pk, "page": 2}
        ).content.decode()

        self.assertIn(">1</td>", page_one)
        self.assertIn(">12</td>", page_one)
        self.assertNotIn(">13</td>", page_one)
        self.assertIn(">13</td>", page_two)

    def test_numbering_restarts_with_a_filtered_list(self):
        self._add_patient_with_schedule(
            "PAT-L0002", "Other", "Person", barangay=self.other_barangay,
        )
        self.client.force_login(self.nurse)
        content = self.client.get(
            self.url, {"barangay": self.other_barangay.pk}
        ).content.decode()
        self.assertIn(">1</td>", content)
        self.assertNotIn(">2</td>", content)

    def test_patient_identity_age_sex_and_barangay(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)

        self.assertContains(response, self.patient.full_name())
        self.assertContains(response, self.patient.patient_number)
        self.assertContains(response, self.patient.contact_display)
        self.assertContains(response, "Caridad")
        self.assertContains(response, f"{self.patient.age} / Female")
        self.assertRegex(response.content.decode(), r">\s*E\s*<")

    def test_nurse_patient_list_links_to_manage_without_other_record_actions(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)

        self.assertContains(
            response,
            reverse("vaccination:nurse_schedule_manage", args=[self.schedule.pk]),
        )
        self.assertNotContains(
            response,
            reverse("vaccination:assessment_detail", args=[self.schedule.pk]),
        )
        self.assertNotContains(response, reverse("patients:patient_detail", args=[self.patient.pk]))
        self.assertNotContains(response, reverse("bite_cases:case_detail", args=[self.case.pk]))
        self.assertNotContains(response, ">Patient</a>")
        self.assertNotContains(response, ">Case →</a>")

    def test_nurse_actions_render_only_administer(self):
        self.client.force_login(self.nurse)
        content = self.client.get(self.url).content.decode()

        self.assertNotIn(reverse("vaccination:assessment_detail", args=[self.schedule.pk]), content)
        self.assertIn(reverse("vaccination:nurse_schedule_manage", args=[self.schedule.pk]), content)
        self.assertRegex(content, r">\s*Administer\s*<")
        self.assertNotIn("vr-act vr-view", content)
        self.assertIn("vr-act vr-add", content)
        self.assertIn("#16856A", content)
        self.assertIn("#106B55", content)
        self.assertIn("#08291F", content)
        self.assertIn("m18 2 4 4", content)
        self.assertNotRegex(content, r">\s*View\s*<")

    def test_admin_actions_show_only_view(self):
        self.client.force_login(self.admin)
        content = self.client.get(self.url).content.decode()

        self.assertIn(reverse("vaccination:schedule_detail", args=[self.schedule.pk]), content)
        self.assertEqual(len(re.findall(r">\s*View\s*<", content)), 2)
        self.assertEqual(content.count('class="vaccination-view-btn'), 2)
        self.assertEqual(content.count('<circle cx="12" cy="12" r="2.75"/>'), 2)
        for style in (
            "background: #157A61;",
            "background: #0C543F;",
            "transform: translateY(-2px);",
            "transition: background 0.2s ease, transform 0.2s ease, box-shadow 0.2s ease;",
        ):
            self.assertIn(style, content)
        self.assertNotRegex(content, r">\s*Manage\s*</a>")
        self.assertNotIn(reverse("vaccination:assessment_detail", args=[self.schedule.pk]), content)
        self.assertNotIn("Administer", content)
        self.assertNotIn("vr-add", content)
        self.assertNotIn("vr-act", content)
        self.assertNotIn("#16856A", content)

    # -------------------------------------------------- data
    def test_patient_appears_once_even_with_multiple_bite_cases(self):
        second_case = AnimalBiteCase.objects.create(
            case_number="ABC-L0002", patient=self.patient, created_by=self.admin,
            animal_type="cat", exposure_type="scratch",
            date_time_bite=datetime.datetime(2026, 10, 9, 9, 0),
        )
        latest_schedule = VaccinationSchedule.objects.create(
            bite_case=second_case, created_by=self.nurse,
        )
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)

        self.assertEqual(response.context["total"], 1)
        self.assertEqual(len(response.context["patients"].object_list), 1)
        row = response.context["patients"].object_list[0]
        self.assertEqual(row.pk, self.patient.pk)
        self.assertEqual(row.schedule_pk, latest_schedule.pk)

    def test_approved_date_uses_the_saved_verification_timestamp(self):
        approved_at = timezone.make_aware(datetime.datetime(2026, 10, 13, 9, 30))
        self._approve(self.patient, approved_at)
        self.client.force_login(self.nurse)

        response = self.client.get(self.url)
        self.assertContains(response, "Oct 13, 2026")

    def test_missing_approved_date_shows_not_recorded(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)
        self.assertContains(response, "Not recorded")

    # -------------------------------------------------- filters
    def test_search_covers_name_id_contact_barangay_and_address(self):
        self._add_patient_with_schedule(
            "PAT-L0002", "Other", "Person", barangay=self.other_barangay,
        )
        self.client.force_login(self.nurse)

        for query in ("Eragene", "PAT-L0001", "0939 233 9688", "Caridad", "Purok 7"):
            with self.subTest(query=query):
                response = self.client.get(self.url, {"q": query})
                self.assertEqual(response.context["total"], 1)
                self.assertEqual(
                    response.context["patients"].object_list[0].pk, self.patient.pk
                )

    def test_barangay_filter_limits_the_listing(self):
        other = self._add_patient_with_schedule(
            "PAT-L0002", "Other", "Person", barangay=self.other_barangay,
        )
        self.client.force_login(self.nurse)

        response = self.client.get(self.url, {"barangay": self.other_barangay.pk})
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["patients"].object_list[0].pk, other.pk)

    def test_pagination_keeps_filters_and_does_not_duplicate_patients(self):
        for index in range(2, 17):
            self._add_patient_with_schedule(
                f"PAT-L{index:04d}", f"Patient{index}", "List",
            )
        self.client.force_login(self.nurse)
        response = self.client.get(
            self.url, {"q": "List", "barangay": self.barangay.pk}
        )

        self.assertEqual(response.context["total"], 15)
        first_page = response.context["patients"].object_list
        self.assertEqual(len(first_page), 12)
        self.assertEqual(len({p.pk for p in first_page}), 12)

        response = self.client.get(
            self.url, {"q": "List", "barangay": self.barangay.pk, "page": 2}
        )
        second_page = response.context["patients"].object_list
        self.assertEqual(len(second_page), 3)
        self.assertFalse({p.pk for p in first_page} & {p.pk for p in second_page})
        self.assertContains(response, "q=List&barangay=" + str(self.barangay.pk) + "&page=")

    # -------------------------------------------------- safety / access
    def test_opening_the_page_does_not_create_schedules_or_touch_stock(self):
        stock = VaccineInventory.objects.create(
            vaccine_name="Rabies Vaccine", brand="Verorab", batch_number="LOT-L1",
            expiration_date=datetime.date(2027, 1, 1),
            quantity_received=10, quantity_available=10, quantity_used=0,
        )
        schedule_count = VaccinationSchedule.objects.count()
        self.client.force_login(self.nurse)
        self.client.get(self.url)

        self.assertEqual(VaccinationSchedule.objects.count(), schedule_count)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity_available, 10)
        self.assertEqual(stock.quantity_used, 0)

    def test_staff_roles_can_open_the_list(self):
        for user in (self.nurse, self.admin):
            self.client.force_login(user)
            with self.subTest(role=user.role):
                self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_patient_role_is_redirected_to_their_portal(self):
        user = U.objects.create_user("pat.list", password="pw12345678", role="patient")
        self.patient.account = user
        self.patient.save(update_fields=["account"])
        self.client.force_login(user)

        response = self.client.get(self.url, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("accounts:patient_portal"))
        self.assertContains(response, "My Dashboard")
        self.assertNotContains(response, "Vaccination Records")


class ScheduleOwnershipEnforcementTests(TestCase):
    """The vaccination schedule detail page must be scoped to the owning patient:
    a patient can open only their own schedule, never another patient's."""

    def _make_patient_with_login(self, number, first, user_name):
        user = U.objects.create_user(user_name, password="pw12345678", role="patient")
        patient = Patient.objects.create(
            patient_number=number, first_name=first, last_name="Doe",
            birthdate=datetime.date(1990, 1, 1), contact_number="09170001111",
            account=user,
        )
        case = AnimalBiteCase.objects.create(
            case_number=f"ABC-{number}", patient=patient, created_by=user,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )
        sched = VaccinationSchedule.objects.create(bite_case=case)
        VaccineDose.objects.create(
            schedule=sched, dose_label="Day 0",
            scheduled_date=datetime.date(2026, 10, 1),
        )
        return user, patient, sched

    def setUp(self):
        self.alice, self.alice_patient, self.alice_sched = self._make_patient_with_login(
            "PAT-A0001", "Alice", "alice"
        )
        self.bob, self.bob_patient, self.bob_sched = self._make_patient_with_login(
            "PAT-B0002", "Bob", "bob"
        )
        self.admin = U.objects.create_user("adm", password="pw12345678", role="admin")

    # ---- patient portal read-only view (the page patients actually reach) ----
    def test_patient_can_view_own_schedule(self):
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse("vaccination:my_schedule_detail", args=[self.alice_sched.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.alice_sched.bite_case.case_number)

    def test_patient_cannot_view_another_patient_schedule(self):
        """Bob must get a 404 (not a soft render) when requesting Alice's schedule."""
        self.client.force_login(self.bob)
        response = self.client.get(
            reverse("vaccination:my_schedule_detail", args=[self.alice_sched.pk])
        )
        self.assertEqual(response.status_code, 404)
        # Alice's case identifier must never appear in the response body.
        self.assertNotIn(self.alice_sched.bite_case.case_number.encode(), response.content)

    # ---- staff schedule detail ----
    def test_staff_can_view_any_schedule(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("vaccination:schedule_detail", args=[self.alice_sched.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_patient_redirected_away_from_staff_schedule_detail(self):
        """A patient hitting the staff schedule_detail URL must not see the data.
        Either the middleware or the view guard bounces them to their portal."""
        self.client.force_login(self.bob)
        response = self.client.get(
            reverse("vaccination:schedule_detail", args=[self.alice_sched.pk]),
            follow=True,
        )
        self.assertNotContains(response, self.alice_sched.bite_case.case_number)
        # Ultimately lands on the patient portal, not the schedule page.
        self.assertEqual(response.redirect_chain[-1][0], reverse("accounts:patient_portal"))


class DoctorAssessmentOnlyViewTests(TestCase):
    """View on Vaccination Records shows the doctor assessment and nothing else."""

    def setUp(self):
        self.nurse = U.objects.create_user("n.assess", password="pw12345678", role="nurse")
        self.admin = U.objects.create_user("a.assess", password="pw12345678", role="admin")
        self.patient_user = U.objects.create_user("p.assess", password="pw12345678", role="patient")
        self.patient = Patient.objects.create(
            patient_number="PAT-A0001", first_name="Maria", last_name="Santos",
            birthdate=datetime.date(1999, 6, 6), sex="female",
            contact_number="09171112222", address="Purok 3",
            account=self.patient_user,
        )
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-A0001", patient=self.patient, created_by=self.admin,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )
        self.schedule = VaccinationSchedule.objects.create(bite_case=self.case, created_by=self.nurse)
        VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 0",
            scheduled_date=datetime.date(2026, 10, 1), dose_status="completed",
            actual_date=datetime.date(2026, 10, 1), protocol_day=0,
        )
        VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 3", protocol_day=3, dose_status="pending",
        )
        self.medicine = MedicineInventory.objects.create(
            medicine_name="Amoxicillin", dosage_form="capsule", unit="capsule",
            batch_number="MED-A1", expiration_date=datetime.date(2027, 5, 1),
            quantity_received=30, quantity_available=30,
        )
        self.assessment = ClinicalAssessment.objects.create(
            patient=self.patient, bite_case=self.case,
            assessed_by=U.objects.create_user(
                username="doctor.assess", role="doctor"
            ),
            chief_complaint="Dog bite on the left leg.",
            exposure_narrative="Verified: stray dog bit the patient while walking.",
            category_confirmed="category_iii", pep_indicated=True,
            vaccine_brand_plan="Verorab", vaccine_dose_ml="0.5", vaccine_quantity=5,
            dose_schedule_plan="Days 0, 3, 7, 14, 28", dose_protocol="essen",
            first_dose_date=datetime.date(2026, 10, 1),
            treatment_plan="Wound care and complete PEP series.",
            dosage_instructions="Take one capsule every 8 hours for 7 days.",
            follow_up_instructions="Return on Day 3 and Day 7.",
            additional_notes="No known drug allergies.",
            status="final", is_finalized=True, finalized_at=timezone.now(),
        )
        Prescription.objects.create(
            assessment=self.assessment, patient=self.patient, bite_case=self.case,
            medicine=self.medicine, quantity=10,
        )
        self.url = reverse("vaccination:assessment_detail", args=[self.schedule.pk])

    def test_nurse_sees_only_the_doctor_assessment(self):
        self.client.force_login(self.nurse)
        content = self.client.get(self.url).content.decode()

        self.assertIn("Doctor's Assessment", content)
        self.assertIn("MS", content)
        self.assertIn(self.patient.full_name(), content)
        for text in (
            "Chief Complaint", "Dog bite on the left leg.",
            "Exposure Narrative", "Verified: stray dog bit",
            "WHO bite category", "Category III",
            "Dose Schedule", "Vaccine brand", "Verorab",
            "Vaccination and Treatment Plan",
            "Treatment Prescription and Recommendations",
            "Wound care and complete PEP series.",
            "Amoxicillin", "10 capsules",
            "Take one capsule every 8 hours",
            "Return on Day 3 and Day 7.",
            "No known drug allergies.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, content)

    def test_patient_case_and_vitals_sections_are_not_rendered(self):
        self.client.force_login(self.nurse)
        content = self.client.get(self.url).content.decode()

        self.assertNotIn("Patient Information", content)
        self.assertNotIn("Animal Bite Case", content)
        self.assertNotIn("Vital Signs", content)
        self.assertNotIn("NO VITALS YET", content)
        self.assertNotIn("Date of incident", content)
        self.assertNotIn("Registered date", content)

    def test_assessment_page_has_no_editable_controls(self):
        self.client.force_login(self.nurse)
        content = self.client.get(self.url).content.decode()
        # Scope to the record sheet: the global app shell (QR scanner) has its
        # own lookup form, which is not part of the assessment.
        sheet = content.split('mx-auto record-sheet"', 1)[1].split("</main>", 1)[0]
        self.assertNotIn("<form", sheet)
        self.assertNotIn("<input", sheet)
        self.assertNotIn("<select", sheet)
        self.assertNotIn("<textarea", sheet)

    def test_draft_is_labelled_as_an_unapproved_draft(self):
        ClinicalAssessment.objects.filter(pk=self.assessment.pk).update(
            status="draft", is_finalized=False,
        )
        self.client.force_login(self.nurse)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_no_assessment_shows_no_assessment_yet(self):
        self.assessment.delete()
        self.client.force_login(self.nurse)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_patient_cannot_open_the_assessment_view(self):
        self.client.force_login(self.patient_user)
        response = self.client.get(self.url, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("accounts:patient_portal"))

    def test_viewing_never_touches_stock_or_records(self):
        stock = VaccineInventory.objects.create(
            vaccine_name="Rabies Vaccine", brand="Verorab", batch_number="LOT-V1",
            expiration_date=datetime.date(2027, 1, 1),
            quantity_received=10, quantity_available=10, quantity_used=0,
        )
        audit_before = AuditLog.objects.count()
        self.client.force_login(self.nurse)
        self.client.get(self.url)

        stock.refresh_from_db()
        self.assertEqual((stock.quantity_available, stock.quantity_used), (10, 0))
        self.assertEqual(AuditLog.objects.count(), audit_before)


@override_settings(SEMAPHORE_ENABLED=False)
class NurseVaccinationManageTests(TestCase):
    """Manage page: confirmed administration/dispensing deduct stock exactly once."""

    def setUp(self):
        self.nurse = U.objects.create_user("n.manage", password="pw12345678", role="nurse")
        self.admin = U.objects.create_user("a.manage", password="pw12345678", role="admin")
        self.doctor = U.objects.create_user("d.manage", password="pw12345678", role="doctor")
        self.patient_user = U.objects.create_user("p.manage", password="pw12345678", role="patient")
        self.patient = Patient.objects.create(
            patient_number="PAT-M0001", first_name="Jose", last_name="Cruz",
            birthdate=datetime.date(1988, 8, 8), sex="male",
            contact_number="09173334444", address="Purok 4",
            account=self.patient_user,
        )
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-M0001", patient=self.patient, created_by=self.admin,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )
        self.assessment = ClinicalAssessment.objects.create(
            patient=self.patient, bite_case=self.case, assessed_by=self.admin,
            chief_complaint="Bite wound",
            category_confirmed="category_iii", pep_indicated=True,
            vaccine_brand_plan="Verorab", vaccine_lot_number="LOT-V1",
            vaccine_dose_ml="0.5", vaccine_quantity=5,
            dose_protocol="essen", dose_schedule_plan="Days 0, 3, 7, 14, 28",
            first_dose_date=datetime.date(2026, 10, 1),
            treatment_plan="Complete PEP and antibiotics.",
            dosage_instructions="One capsule every 8 hours for 7 days.",
            follow_up_instructions="Return for the next dose.",
            status="final", is_finalized=True,
        )
        self.schedule = self.case.vaccination_schedule
        self.first_dose = self.schedule.doses.order_by("id").first()
        self.medicine = MedicineInventory.objects.create(
            medicine_name="Amoxicillin", dosage_form="capsule", unit="capsule",
            batch_number="MED-M1", expiration_date=datetime.date(2027, 5, 1),
            quantity_received=30, quantity_available=30,
        )
        self.rx = Prescription.objects.create(
            assessment=self.assessment, patient=self.patient, bite_case=self.case,
            medicine=self.medicine, quantity=10,
        )
        self.vaccine = VaccineInventory.objects.create(
            vaccine_name="Rabies Vaccine", brand="Verorab", batch_number="LOT-V1",
            expiration_date=datetime.date(2027, 1, 1),
            quantity_received=10, quantity_available=10, quantity_used=0,
        )
        self.url = reverse("vaccination:nurse_schedule_manage", args=[self.schedule.pk])

    def test_manage_page_shows_both_sections_and_prescriptions(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Protocol date:")
        self.assertNotContains(response, "scheduleProtocolHint")
        self.assertContains(response, "Scheduled Date")
        for text in ("Administer Vaccine", "Confirm Prescribed Medicines",
                     "Verorab", "Amoxicillin", "10 capsules", "Pending Dispensing"):
            with self.subTest(text=text):
                self.assertIn(text, content)
        self.assertContains(response, reverse("vaccination:dose_administer", args=[self.first_dose.pk]))
        self.assertNotContains(response, reverse("vaccination:dose_schedule", args=[self.first_dose.pk]))
        self.assertContains(response, reverse("inventory:prescription_dispense", args=[self.rx.pk]))

    def test_other_roles_cannot_open_management(self):
        for user in (self.doctor, self.patient_user):
            self.client.force_login(user)
            with self.subTest(role=user.role):
                response = self.client.get(self.url, follow=True)
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                self.assertNotIn("Confirm Prescribed Medicines", content)
                self.assertNotIn(reverse("vaccination:dose_administer", args=[self.first_dose.pk]), content)

    def test_confirming_administration_completes_dose_and_deducts_once(self):
        actual_date = timezone.localdate()
        payload = {
            "actual_date": actual_date.isoformat(),
            "actual_time": "10:30",
            "vaccine_brand": "Verorab",
            "batch_number": "LOT-V1",
        }
        self.client.force_login(self.nurse)
        self.client.post(reverse("vaccination:dose_administer", args=[self.first_dose.pk]), payload)

        self.first_dose.refresh_from_db()
        self.vaccine.refresh_from_db()
        self.assertEqual(self.first_dose.dose_status, "completed")
        self.assertEqual(self.first_dose.actual_date, actual_date)
        self.assertEqual(self.first_dose.actual_time, datetime.time(10, 30))
        self.assertEqual(self.first_dose.administered_by, self.nurse)
        self.assertEqual(self.vaccine.quantity_available, 9)
        self.assertEqual(self.vaccine.quantity_used, 1)
        self.assertTrue(AuditLog.objects.filter(action="ADMINISTER_VACCINE").exists())

        self.client.post(reverse("vaccination:dose_administer", args=[self.first_dose.pk]), payload)
        self.vaccine.refresh_from_db()
        self.assertEqual((self.vaccine.quantity_available, self.vaccine.quantity_used), (9, 1))

    def test_future_dose_shows_warning_and_records_pending_request_without_administering(self):
        today = timezone.localdate()
        scheduled_date = today + datetime.timedelta(days=2)
        self.first_dose.scheduled_date = scheduled_date
        self.first_dose.save(update_fields=["scheduled_date"])
        self.client.force_login(self.nurse)

        page = self.client.get(self.url)
        self.assertContains(page, "Early Administration Warning")
        self.assertContains(page, "This vaccine dose is not yet scheduled for administration.")
        self.assertContains(page, "Why do you need to administer this vaccine dose before its scheduled date?")
        self.assertContains(page, "Reason for Early Administration")
        self.assertContains(page, "Submit Reason")

        with patch("vaccination.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("vaccination:dose_early_administration_request", args=[self.first_dose.pk]),
                {"reason": "Urgent clinical travel requirement"},
                REMOTE_ADDR="192.0.2.10",
                follow=True,
            )

        self.first_dose.refresh_from_db()
        self.vaccine.refresh_from_db()
        request_log = AuditLog.objects.get(action="EARLY_ADMIN_REQUEST")
        self.assertContains(
            response,
            "Your reason has been recorded. This vaccine dose cannot be administered before its scheduled date without the required authorization.",
        )
        self.assertEqual(self.first_dose.dose_status, "scheduled")
        self.assertEqual(self.first_dose.scheduled_date, scheduled_date)
        self.assertIsNone(self.first_dose.actual_date)
        self.assertIsNone(self.first_dose.administered_by)
        self.assertEqual((self.vaccine.quantity_available, self.vaccine.quantity_used), (10, 0))
        self.assertEqual(request_log.user, self.nurse)
        self.assertEqual(request_log.target_type, "bite_case")
        self.assertEqual(request_log.target_id, self.case.pk)
        self.assertEqual(request_log.ip_address, "192.0.2.10")
        self.assertIn("Jose Cruz", request_log.description)
        self.assertIn(f"dose ID {self.first_dose.pk}", request_log.description)
        self.assertIn(str(scheduled_date), request_log.description)
        self.assertIn("Urgent clinical travel requirement", request_log.description)
        self.assertFalse(AuditLog.objects.filter(action="ADMINISTER_VACCINE").exists())

    def test_future_dose_cannot_be_administered_by_direct_post(self):
        today = timezone.localdate()
        self.first_dose.scheduled_date = today + datetime.timedelta(days=2)
        self.first_dose.save(update_fields=["scheduled_date"])
        self.client.force_login(self.nurse)

        with patch("vaccination.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("vaccination:dose_administer", args=[self.first_dose.pk]),
                {
                    "actual_date": today.isoformat(),
                    "vaccine_brand": "Verorab",
                    "batch_number": "LOT-V1",
                },
                follow=True,
            )

        self.first_dose.refresh_from_db()
        self.vaccine.refresh_from_db()
        self.assertContains(response, "scheduled for a future date and cannot be administered yet")
        self.assertEqual(self.first_dose.dose_status, "scheduled")
        self.assertIsNone(self.first_dose.actual_date)
        self.assertEqual((self.vaccine.quantity_available, self.vaccine.quantity_used), (10, 0))
        self.assertFalse(AuditLog.objects.filter(action="ADMINISTER_VACCINE").exists())

    def test_administration_date_cannot_precede_the_scheduled_date(self):
        today = timezone.localdate()
        self.first_dose.scheduled_date = today - datetime.timedelta(days=1)
        self.first_dose.save(update_fields=["scheduled_date"])
        self.client.force_login(self.nurse)

        with patch("vaccination.views.timezone.localdate", return_value=today):
            response = self.client.post(
                reverse("vaccination:dose_administer", args=[self.first_dose.pk]),
                {
                    "actual_date": (today - datetime.timedelta(days=2)).isoformat(),
                    "vaccine_brand": "Verorab",
                    "batch_number": "LOT-V1",
                },
                follow=True,
            )

        self.first_dose.refresh_from_db()
        self.vaccine.refresh_from_db()
        self.assertContains(response, "Administration date cannot be earlier than the dose scheduled date")
        self.assertEqual(self.first_dose.dose_status, "scheduled")
        self.assertIsNone(self.first_dose.actual_date)
        self.assertEqual((self.vaccine.quantity_available, self.vaccine.quantity_used), (10, 0))

    def test_display_brand_with_the_lot_batch_still_deducts_stock(self):
        ClinicalAssessment.objects.filter(pk=self.assessment.pk).update(
            vaccine_brand_plan="Rabies Vaccine (Verorab)")
        self.client.force_login(self.nurse)
        self.client.post(
            reverse("vaccination:dose_administer", args=[self.first_dose.pk]),
            {
                "actual_date": timezone.localdate().isoformat(),
                "vaccine_brand": "Rabies Vaccine (Verorab)",
                "batch_number": "LOT-V1",
            },
        )
        self.vaccine.refresh_from_db()
        self.assertEqual((self.vaccine.quantity_available, self.vaccine.quantity_used), (9, 1))

    def test_administration_is_blocked_until_the_assessment_is_final(self):
        ClinicalAssessment.objects.filter(pk=self.assessment.pk).update(
            status="draft", is_finalized=False,
        )
        self.client.force_login(self.nurse)
        response = self.client.post(
            reverse("vaccination:dose_administer", args=[self.first_dose.pk]),
            {"actual_date": timezone.localdate().isoformat(), "vaccine_brand": "Verorab"},
            follow=True,
        )
        self.first_dose.refresh_from_db()
        self.vaccine.refresh_from_db()
        self.assertEqual(self.first_dose.dose_status, "scheduled")
        self.assertEqual(self.vaccine.quantity_available, 10)
        self.assertContains(response, "must be finalized")

    def test_scheduling_a_pending_dose_does_not_touch_stock(self):
        pending = self.schedule.doses.exclude(pk=self.first_dose.pk).order_by("id").first()
        earliest = self.first_dose.scheduled_date + datetime.timedelta(days=pending.protocol_day)
        self.client.force_login(self.nurse)
        self.client.post(
            reverse("vaccination:dose_schedule", args=[pending.pk]),
            {"scheduled_date": earliest.isoformat()},
        )

        pending.refresh_from_db()
        self.vaccine.refresh_from_db()
        self.assertEqual(pending.scheduled_date, earliest)
        self.assertEqual(pending.dose_status, "scheduled")
        self.assertEqual(self.vaccine.quantity_available, 10)
        self.assertEqual(self.vaccine.quantity_used, 0)

    def test_nurse_selected_schedule_date_is_saved_and_displayed_without_stock_change(self):
        pending = self.schedule.doses.exclude(pk=self.first_dose.pk).order_by("id").first()
        selected_date = timezone.localdate() + datetime.timedelta(days=30)
        self.client.force_login(self.nurse)

        self.client.post(
            reverse("vaccination:dose_schedule", args=[pending.pk]),
            {"scheduled_date": selected_date.isoformat()},
        )

        pending.refresh_from_db()
        self.vaccine.refresh_from_db()
        response = self.client.get(self.url)
        self.assertEqual(pending.scheduled_date, selected_date)
        self.assertEqual(pending.dose_status, "scheduled")
        self.assertContains(response, selected_date.strftime("%b %d, %Y"))
        self.assertIn(f"'{selected_date.isoformat()}'", response.content.decode())
        self.assertEqual((self.vaccine.quantity_available, self.vaccine.quantity_used), (10, 0))

    def test_schedule_date_before_explicit_doctor_regimen_is_rejected_with_specific_reason(self):
        from .schedule_utils import prescribed_minimum_date

        pending = self.schedule.doses.exclude(pk=self.first_dose.pk).order_by("id").first()
        too_early = self.first_dose.scheduled_date + datetime.timedelta(days=1)
        self.assertEqual(
            prescribed_minimum_date(pending)['date'],
            self.first_dose.scheduled_date + datetime.timedelta(days=3),
        )
        self.client.force_login(self.nurse)
        response = self.client.post(
            reverse("vaccination:dose_schedule", args=[pending.pk]),
            {"scheduled_date": too_early.isoformat()},
            follow=True,
        )
        pending.refresh_from_db()
        self.assertIsNone(pending.scheduled_date)
        messages = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("finalized" in message and "Essen" in message for message in messages), messages)
        self.assertTrue(any("earliest date is Oct 04, 2026" in message for message in messages), messages)
        self.assertNotContains(response, "This protocol requires")

    def test_fallback_protocol_offset_does_not_block_nurse_selected_date(self):
        ClinicalAssessment.objects.filter(pk=self.assessment.pk).update(
            dose_protocol="", dose_schedule_plan="Clinician-directed dates",
        )
        pending = self.schedule.doses.exclude(pk=self.first_dose.pk).order_by("id").first()
        selected_date = self.first_dose.scheduled_date + datetime.timedelta(days=1)
        self.client.force_login(self.nurse)

        response = self.client.post(
            reverse("vaccination:dose_schedule", args=[pending.pk]),
            {"scheduled_date": selected_date.isoformat()},
            follow=True,
        )

        pending.refresh_from_db()
        self.assertEqual(pending.scheduled_date, selected_date)
        self.assertEqual(pending.dose_status, "scheduled")
        self.assertContains(response, "Vaccination schedule updated successfully.")
        self.assertNotContains(response, "This protocol requires")

    def test_nurse_cannot_change_first_dose_date(self):
        first_date = self.first_dose.scheduled_date
        self.client.force_login(self.nurse)
        self.client.post(
            reverse("vaccination:dose_schedule", args=[self.first_dose.pk]),
            {"scheduled_date": (first_date + datetime.timedelta(days=5)).isoformat()},
            follow=True,
        )

        self.first_dose.refresh_from_db()
        self.assertEqual(self.first_dose.scheduled_date, first_date)
        self.assertFalse(
            AuditLog.objects.filter(
                action="SCHEDULE_DOSE",
                description__contains=self.first_dose.dose_label,
            ).exists()
        )

    def test_confirming_dispensing_deducts_medicine_once(self):
        url = reverse("inventory:prescription_dispense", args=[self.rx.pk])
        self.client.force_login(self.nurse)
        self.client.post(url, {"next": self.url})

        self.rx.refresh_from_db()
        self.medicine.refresh_from_db()
        self.assertEqual(self.rx.status, "dispensed")
        self.assertEqual(self.rx.dispensed_by, self.nurse)
        self.assertIsNotNone(self.rx.dispensed_at)
        self.assertEqual(self.medicine.quantity_available, 20)
        self.assertEqual(self.medicine.quantity_dispensed, 10)
        self.assertTrue(AuditLog.objects.filter(action="DISPENSE_MEDICINE").exists())

        self.client.post(url, {"next": self.url})
        self.medicine.refresh_from_db()
        self.assertEqual((self.medicine.quantity_available, self.medicine.quantity_dispensed), (20, 10))

    def test_draft_assessment_cannot_dispense_medicines(self):
        ClinicalAssessment.objects.filter(pk=self.assessment.pk).update(
            status="draft", is_finalized=False,
        )
        self.client.force_login(self.nurse)
        self.client.post(
            reverse("inventory:prescription_dispense", args=[self.rx.pk]),
            {"next": self.url},
        )
        self.rx.refresh_from_db()
        self.medicine.refresh_from_db()
        self.assertEqual(self.rx.status, "pending")
        self.assertEqual(self.medicine.quantity_available, 30)
        self.assertFalse(AuditLog.objects.filter(action="DISPENSE_MEDICINE").exists())

    def test_opening_management_never_changes_stock(self):
        audit_before = AuditLog.objects.count()
        self.client.force_login(self.nurse)
        self.client.get(self.url)

        self.vaccine.refresh_from_db()
        self.medicine.refresh_from_db()
        self.assertEqual((self.vaccine.quantity_available, self.vaccine.quantity_used), (10, 0))
        self.assertEqual(self.medicine.quantity_available, 30)
        self.assertEqual(AuditLog.objects.count(), audit_before)

    def test_print_schedule_button_sits_below_section_b(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)
        content = response.content.decode()
        print_url = reverse("vaccination:nurse_schedule_print", args=[self.schedule.pk])

        self.assertIn(print_url, content)
        self.assertIn("Print Schedule", content)
        self.assertIn("btn-print-schedule", content)
        self.assertGreater(
            content.index("Print Schedule"),
            content.index("Confirm Prescribed Medicines"),
        )

    def test_print_slip_shows_patient_vaccine_and_saved_schedule(self):
        first = self.first_dose
        first.dose_status = "completed"
        first.actual_date = datetime.date(2026, 10, 1)
        first.scheduled_date = datetime.date(2026, 10, 1)
        first.save(update_fields=["dose_status", "actual_date", "scheduled_date"])

        second = self.schedule.doses.order_by("id")[1]
        second.dose_status = "scheduled"
        second.scheduled_date = datetime.date(2026, 10, 4)
        second.save(update_fields=["dose_status", "scheduled_date"])

        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse("vaccination:nurse_schedule_print", args=[self.schedule.pk]))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("/static/images/lgu_dumingag_logo.png", content)
        self.assertIn("size: A4 portrait", content)
        self.assertIn("abtc-logo", content)
        for marker in (
            "print_official.css", "abtc-letterhead", "abtc-footer",
            "Animal Bite Treatment Center", "System-Generated Document",
        ):
            self.assertIn(marker, content)
        for text in (
            "Animal Bite Treatment Center", "Municipal Health Office",
            "Patient Vaccination Schedule", "Patient Information",
            "Vaccination Information", "Vaccination Schedule",
            "Appointment Reminder",
            self.patient.full_name(), self.patient.patient_number,
            self.case.case_number, "Verorab", "5 Doses", "Oct 01, 2026",
            "1st Dose", "2nd Dose", "3rd Dose", "4th Dose", "5th Dose",
            "Completed", "Scheduled", "Pending Scheduling", "Not yet set",
        ):
            with self.subTest(text=text):
                self.assertIn(text, content)
        self.assertIn(timezone.localdate().strftime("%B %d, %Y"), content)
        self.assertIn(
            "Please follow your confirmed vaccination appointments. "
            "For doses marked Pending Scheduling, contact the Animal Bite "
            "Treatment Center to confirm your next appointment. Bring this "
            "schedule slip during your clinic visits.",
            content,
        )
        for hidden in (
            "Confirm Prescribed Medicines", "Administer Vaccine",
            "Confirm Dispensed", "Set date", "Nurse Portal",
        ):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, content)

    def test_print_slip_uses_the_doctor_selected_dose_count(self):
        ClinicalAssessment.objects.filter(pk=self.assessment.pk).update(
            dose_protocol="zagreb", dose_schedule_plan="Days 0, 7, 21",
        )
        self.client.force_login(self.nurse)
        content = self.client.get(
            reverse("vaccination:nurse_schedule_print", args=[self.schedule.pk])
        ).content.decode()

        self.assertIn("3 Doses", content)
        self.assertIn("3rd Dose", content)
        self.assertNotIn("4th Dose", content)
        self.assertNotIn("5th Dose", content)

    def test_print_slip_identifies_rig_by_its_inventory_name(self):
        rig = VaccineInventory.objects.create(
            vaccine_name="RIG (Rabies Immunoglobulin)", brand="Imogam",
            batch_number="RIG-P1", expiration_date=datetime.date(2027, 6, 1),
            quantity_received=5, quantity_available=5,
        )
        ClinicalAssessment.objects.filter(pk=self.assessment.pk).update(
            vaccine_inventory=rig)
        self.client.force_login(self.nurse)
        content = self.client.get(
            reverse("vaccination:nurse_schedule_print", args=[self.schedule.pk])
        ).content.decode()

        self.assertIn("RIG (Rabies Immunoglobulin) (Imogam)", content)

    def test_manage_page_flags_rig_with_multi_dose_schedule_for_clinical_review(self):
        rig = VaccineInventory.objects.create(
            vaccine_name="RIG (Rabies Immunoglobulin)", brand="Imogam",
            batch_number="RIG-REVIEW", expiration_date=datetime.date(2027, 6, 1),
            quantity_received=5, quantity_available=5,
        )
        ClinicalAssessment.objects.filter(pk=self.assessment.pk).update(
            vaccine_inventory=rig,
        )
        self.client.force_login(self.nurse)

        response = self.client.get(self.url)

        self.assertContains(response, "Clinical review required")
        self.assertContains(response, "rabies immunoglobulin (RIG)")
        self.assertContains(response, "prescription has not been changed")

    def test_print_slip_reflects_updated_dates_without_writing_records(self):
        pending = self.schedule.doses.exclude(pk=self.first_dose.pk).order_by("id").first()
        pending.dose_status = "scheduled"
        pending.scheduled_date = datetime.date(2026, 10, 20)
        pending.save(update_fields=["dose_status", "scheduled_date"])
        stock_before = (self.vaccine.quantity_available, self.vaccine.quantity_used)
        audit_before = AuditLog.objects.count()

        self.client.force_login(self.nurse)
        content = self.client.get(
            reverse("vaccination:nurse_schedule_print", args=[self.schedule.pk])
        ).content.decode()

        self.assertIn("Oct 20, 2026", content)
        pending.refresh_from_db()
        self.assertEqual(pending.scheduled_date, datetime.date(2026, 10, 20))
        self.vaccine.refresh_from_db()
        self.assertEqual(
            (self.vaccine.quantity_available, self.vaccine.quantity_used), stock_before)
        self.assertEqual(AuditLog.objects.count(), audit_before)
        self.assertNotIn("Oct 04, 2026", content)
        self.assertNotIn("Oct 08, 2026", content)

    def test_only_vaccination_managers_can_open_the_print_slip(self):
        url = reverse("vaccination:nurse_schedule_print", args=[self.schedule.pk])
        for user in (self.nurse, self.admin):
            self.client.force_login(user)
            with self.subTest(role=user.role):
                self.assertEqual(self.client.get(url).status_code, 200)

        for user, destination in (
            (self.doctor, reverse("dashboard:index")),
            (self.patient_user, reverse("accounts:patient_portal")),
        ):
            self.client.force_login(user)
            with self.subTest(role=user.role):
                response = self.client.get(url)
                self.assertRedirects(
                    response, destination, fetch_redirect_response=False)

    # -------------------------------------------------- SMS reminders
    def _next_dose_and_protocol_date(self):
        pending = (
            self.schedule.doses.exclude(pk=self.first_dose.pk).order_by("id").first()
        )
        target = self.first_dose.scheduled_date + datetime.timedelta(
            days=pending.protocol_day)
        return pending, target

    def _schedule(self, dose, target):
        return self.client.post(
            reverse("vaccination:dose_schedule", args=[dose.pk]),
            {"scheduled_date": target.isoformat()},
            follow=True,
        )

    def test_setting_a_dose_date_texts_the_patient(self):
        pending, target = self._next_dose_and_protocol_date()
        self.client.force_login(self.nurse)

        response = self._schedule(pending, target)

        pending.refresh_from_db()
        log = SMSLog.objects.get(dose=pending)
        self.assertEqual(pending.scheduled_date, target)
        self.assertEqual(log.reminder_type, "schedule_set")
        self.assertEqual(log.scheduled_date, target)
        self.assertTrue(log.recipient.endswith("9173334444"))
        self.assertIn(log.status, ("queued", "sent"))
        self.assertIn("SMS reminder sent", response.content.decode())
        self.assertTrue(AuditLog.objects.filter(action="SEND_SMS").exists())

    def test_resaving_the_same_date_does_not_resend(self):
        pending, target = self._next_dose_and_protocol_date()
        self.client.force_login(self.nurse)

        self._schedule(pending, target)
        self._schedule(pending, target)

        self.assertEqual(
            SMSLog.objects.filter(
                dose=pending, reminder_type="schedule_set").count(),
            1,
        )

    def test_changing_the_date_sends_an_updated_confirmation(self):
        pending, target = self._next_dose_and_protocol_date()
        later = target + datetime.timedelta(days=1)
        self.client.force_login(self.nurse)

        self._schedule(pending, target)
        self._schedule(pending, later)

        logs = SMSLog.objects.filter(
            dose=pending, reminder_type="schedule_set").order_by("sent_at")
        self.assertEqual(logs.count(), 2)
        self.assertEqual(logs.last().scheduled_date, later)

    def test_missing_patient_number_still_saves_the_schedule(self):
        pending, target = self._next_dose_and_protocol_date()
        Patient.objects.filter(pk=self.patient.pk).update(
            contact_number="not-a-number",
            emergency_contact_number="",
            account=None,
        )
        self.client.force_login(self.nurse)

        response = self._schedule(pending, target)

        pending.refresh_from_db()
        self.assertEqual(pending.scheduled_date, target)
        self.assertFalse(SMSLog.objects.filter(dose=pending).exists())
        self.assertIn("no valid mobile number", response.content.decode())

    def test_manual_reminder_for_a_scheduled_dose(self):
        self.client.force_login(self.nurse)

        response = self.client.post(
            reverse("vaccination:send_sms_for_dose", args=[self.first_dose.pk]),
            {"reminder_type": "on_day"},
            follow=True,
        )

        log = SMSLog.objects.get(dose=self.first_dose)
        self.assertEqual(log.reminder_type, "on_day")
        self.assertIn("SMS reminder sent", response.content.decode())

    def test_send_due_reminders_is_one_click_and_never_duplicates(self):
        today = timezone.localdate()
        self.first_dose.scheduled_date = today
        self.first_dose.dose_status = "scheduled"
        self.first_dose.save(update_fields=["scheduled_date", "dose_status"])
        self.client.force_login(self.nurse)
        url = reverse("vaccination:trigger_sms_reminders")

        first = self.client.post(url, {"rtype": "all"}, follow=True)
        self.assertContains(first, "Reminders sent: 1")
        self.assertEqual(
            SMSLog.objects.filter(
                dose=self.first_dose, reminder_type="on_day").count(),
            1,
        )

        second = self.client.post(url, {"rtype": "all"}, follow=True)
        self.assertContains(second, "Already sent today: 1")
        self.assertEqual(
            SMSLog.objects.filter(
                dose=self.first_dose, reminder_type="on_day").count(),
            1,
        )

    def test_sms_pages_do_not_name_the_provider(self):
        self.client.force_login(self.nurse)
        for url in (
            reverse("vaccination:sms_log_list"),
            reverse("vaccination:schedule_detail", args=[self.schedule.pk]),
        ):
            content = self.client.get(url).content.decode()
            with self.subTest(url=url):
                for term in ("Semaphore", "SEMAPHORE", "RHUDumingag",
                             "api.semaphore"):
                    self.assertNotIn(term, content)

        content = self.client.get(
            reverse("vaccination:sms_log_list")).content.decode()
        self.assertIn("Send Due Reminders", content)
        self.assertIn("Patient List", content)

    # --- Missed / overdue display on the Manage dose table ---

    def _dose_at(self, index):
        return self.schedule.doses.order_by("id")[index]

    def _set_dose(self, index, scheduled_date, status="scheduled",
                  actual_date=None, actual_time=None):
        dose = self._dose_at(index)
        dose.scheduled_date = scheduled_date
        dose.dose_status = status
        dose.actual_date = actual_date
        dose.actual_time = actual_time
        dose.save(update_fields=[
            "scheduled_date", "dose_status", "actual_date", "actual_time"])
        return dose

    def _manage(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response

    def test_past_unadministered_dose_shows_missed_with_alert_and_actions(self):
        today = timezone.localdate()
        self._set_dose(0, today - datetime.timedelta(days=12), status="completed",
                       actual_date=today - datetime.timedelta(days=12),
                       actual_time=datetime.time(9, 0))
        second = self._set_dose(1, today - datetime.timedelta(days=1))
        self._set_dose(2, today)
        self._set_dose(3, today + datetime.timedelta(days=4))
        self._set_dose(4, today + datetime.timedelta(days=18))

        response = self._manage()
        content = response.content.decode()
        rows = response.context["dose_rows"]

        self.assertEqual(response.context["missed_count"], 1)
        self.assertTrue(rows[1]["is_missed"])
        self.assertEqual(rows[1]["status_label"], "Missed")
        self.assertEqual(rows[1]["status_class"], "missed")
        self.assertIsNone(rows[1]["dose"].actual_date)
        self.assertEqual(rows[0]["status_label"], "Completed")
        self.assertFalse(rows[0]["is_missed"])
        for index in (2, 3, 4):
            self.assertFalse(rows[index]["is_missed"])
            self.assertEqual(rows[index]["status_label"], "Scheduled")
        self.assertIn("status-missed", content)
        self.assertIn("Missed Vaccination Appointment", content)
        yesterday = today - datetime.timedelta(days=1)
        self.assertIn("missed the scheduled 2nd Dose on", content)
        self.assertIn(
            f"{yesterday.strftime('%B')} {yesterday.day}, {yesterday.year}", content)
        self.assertIn("This dose has not yet been administered.", content)
        self.assertIn(
            "Please review the patient's vaccination record and coordinate "
            "the next appropriate action.", content)
        self.assertContains(response, reverse("vaccination:dose_schedule", args=[second.pk]))
        self.assertContains(response, reverse("vaccination:dose_administer", args=[second.pk]))

    def test_dose_scheduled_today_is_scheduled_not_missed(self):
        today = timezone.localdate()
        self._set_dose(0, today - datetime.timedelta(days=3), status="completed",
                       actual_date=today - datetime.timedelta(days=3))
        self._set_dose(1, today)
        self._set_dose(2, today + datetime.timedelta(days=4))
        self._set_dose(3, today + datetime.timedelta(days=11))
        self._set_dose(4, today + datetime.timedelta(days=25))

        response = self._manage()
        rows = response.context["dose_rows"]
        self.assertFalse(rows[1]["is_missed"])
        self.assertEqual(rows[1]["status_label"], "Scheduled")
        self.assertEqual(rows[1]["status_class"], "scheduled")
        self.assertEqual(response.context["missed_count"], 0)
        self.assertNotIn("Missed Vaccination Appointment", response.content.decode())

    def test_future_doses_are_scheduled_not_missed(self):
        today = timezone.localdate()
        self._set_dose(0, today, status="completed", actual_date=today)
        for index, offset in ((1, 3), (2, 7), (3, 14), (4, 28)):
            self._set_dose(index, today + datetime.timedelta(days=offset))

        response = self._manage()
        rows = response.context["dose_rows"]
        for index in (1, 2, 3, 4):
            self.assertFalse(rows[index]["is_missed"])
            self.assertEqual(rows[index]["status_label"], "Scheduled")
        self.assertEqual(response.context["missed_count"], 0)
        self.assertNotIn("Missed Vaccination Appointment", response.content.decode())

    def test_completed_dose_with_past_date_is_never_missed(self):
        today = timezone.localdate()
        past = today - datetime.timedelta(days=5)
        self._set_dose(0, past, status="completed", actual_date=past,
                       actual_time=datetime.time(10, 30))
        self._set_dose(1, today + datetime.timedelta(days=2))
        self._set_dose(2, today + datetime.timedelta(days=6))
        self._set_dose(3, today + datetime.timedelta(days=13))
        self._set_dose(4, today + datetime.timedelta(days=27))

        response = self._manage()
        content = response.content.decode()
        rows = response.context["dose_rows"]
        self.assertFalse(rows[0]["is_missed"])
        self.assertEqual(rows[0]["status_label"], "Completed")
        self.assertEqual(rows[0]["status_class"], "completed")
        self.assertIn("status-completed", content)
        self.assertIn(past.strftime("%b %d, %Y"), content)
        self.assertIn("10:30 AM", content)
        self.assertEqual(response.context["missed_count"], 0)
        self.assertNotIn("Missed Vaccination Appointment", content)

    def test_missed_status_clears_once_the_dose_is_administered(self):
        today = timezone.localdate()
        self._set_dose(0, today - datetime.timedelta(days=12), status="completed",
                       actual_date=today - datetime.timedelta(days=12))
        second = self._set_dose(1, today - datetime.timedelta(days=1))
        self._set_dose(2, today + datetime.timedelta(days=4))
        self._set_dose(3, today + datetime.timedelta(days=11))
        self._set_dose(4, today + datetime.timedelta(days=25))

        before = self._manage()
        self.assertTrue(before.context["dose_rows"][1]["is_missed"])
        self.assertIn("Missed Vaccination Appointment", before.content.decode())

        self.client.post(
            reverse("vaccination:dose_administer", args=[second.pk]),
            {"actual_date": today.isoformat(), "actual_time": "10:30",
             "vaccine_brand": "Verorab", "batch_number": "LOT-V1"},
        )
        second.refresh_from_db()
        self.assertEqual(second.dose_status, "completed")

        after = self._manage()
        self.assertFalse(after.context["dose_rows"][1]["is_missed"])
        self.assertEqual(after.context["dose_rows"][1]["status_label"], "Completed")
        self.assertEqual(after.context["missed_count"], 0)
        self.assertNotIn("Missed Vaccination Appointment", after.content.decode())

    def test_missed_status_clears_when_the_dose_is_rescheduled_to_a_future_date(self):
        today = timezone.localdate()
        self._set_dose(0, today - datetime.timedelta(days=12), status="completed",
                       actual_date=today - datetime.timedelta(days=12))
        second = self._set_dose(1, today - datetime.timedelta(days=1))
        self._set_dose(2, today + datetime.timedelta(days=4))
        self._set_dose(3, today + datetime.timedelta(days=11))
        self._set_dose(4, today + datetime.timedelta(days=25))

        before = self._manage()
        self.assertTrue(before.context["dose_rows"][1]["is_missed"])
        self.assertIn("Missed Vaccination Appointment", before.content.decode())

        self.client.post(
            reverse("vaccination:dose_schedule", args=[second.pk]),
            {"scheduled_date": (today + datetime.timedelta(days=2)).isoformat()},
        )
        second.refresh_from_db()
        self.assertEqual(second.scheduled_date, today + datetime.timedelta(days=2))

        after = self._manage()
        self.assertFalse(after.context["dose_rows"][1]["is_missed"])
        self.assertEqual(after.context["missed_count"], 0)
        self.assertNotIn("Missed Vaccination Appointment", after.content.decode())


@override_settings(SEMAPHORE_ENABLED=False, ABTC_APPOINTMENT_CUTOFF_TIME=None)
class NurseMissedDoseBadgeTests(TestCase):
    """Sidebar badge + Vaccination Records highlights for overdue unadministered doses."""

    def setUp(self):
        self.nurse = U.objects.create_user("n.badge", password="pw12345678", role="nurse")
        self.today = timezone.localdate()
        self.patient_a = self._patient("PAT-BADGE-A", "John Paul", "Borling", "09171110001", "R")
        self.patient_b = self._patient("PAT-BADGE-B", "Lai", "Sil", "09171110002", "D")
        self.patient_c = self._patient("PAT-BADGE-C", "Clean", "Patient", "09171110003")
        self.case_a = self._case("ABC-BADGE-A", self.patient_a)
        self.case_b = self._case("ABC-BADGE-B", self.patient_b)
        self.case_c = self._case("ABC-BADGE-C", self.patient_c)
        for case in (self.case_a, self.case_b, self.case_c):
            PreRegistration.objects.create(
                first_name=case.patient.first_name, last_name=case.patient.last_name,
                birthdate=case.patient.birthdate, sex="male",
                contact_number=case.patient.contact_number,
                address="Purok 1", barangay="San Jose",
                medical_history="None", allergy_info="None", previous_rabies_vax="None",
                bite_datetime=datetime.datetime(2026, 10, 1, 9, 0),
                incident_place="Home", incident_barangay="San Jose",
                animal_type="dog", animal_ownership="owned",
                animal_vaccination_status="unknown", animal_condition="alive",
                exposure_type="bite", body_part_affected="Arm",
                consent_given=True, converted_case=case, converted_patient=case.patient,
            )
        self.schedule_a = self._schedule(self.case_a)
        self.schedule_b = self._schedule(self.case_b)
        self.schedule_c = self._schedule(self.case_c)
        day = datetime.timedelta(days=1)
        # Patient A: one missed dose (2nd, yesterday).
        self._dose(self.schedule_a, "Day 0", self.today - 12 * day, "completed", self.today - 12 * day)
        self.a_missed = self._dose(self.schedule_a, "Day 3", self.today - day, "scheduled")
        self._dose(self.schedule_a, "Day 7", self.today, "scheduled")
        self._dose(self.schedule_a, "Day 14", self.today + 4 * day, "scheduled")
        # Patient B: two missed doses (2nd three days ago, 3rd yesterday).
        self._dose(self.schedule_b, "Day 0", self.today - 12 * day, "completed", self.today - 12 * day)
        self.b_missed1 = self._dose(self.schedule_b, "Day 3", self.today - 3 * day, "scheduled")
        self.b_missed2 = self._dose(self.schedule_b, "Day 7", self.today - day, "scheduled")
        self._dose(self.schedule_b, "Day 14", self.today + 4 * day, "scheduled")
        # Patient C: no missed dose (completed, today, future).
        self._dose(self.schedule_c, "Day 0", self.today - 12 * day, "completed", self.today - 12 * day)
        self._dose(self.schedule_c, "Day 3", self.today, "scheduled")
        self._dose(self.schedule_c, "Day 7", self.today + 4 * day, "scheduled")
        self.list_url = reverse("vaccination:patient_schedule_list")
        self.dashboard_url = reverse("nurse:dashboard")

    def _patient(self, number, first, last, contact, middle=""):
        return Patient.objects.create(
            patient_number=number, first_name=first, middle_name=middle, last_name=last,
            birthdate=datetime.date(1990, 1, 1), sex="male",
            contact_number=contact, address="Purok 1",
        )

    def _case(self, number, patient):
        return AnimalBiteCase.objects.create(
            case_number=number, patient=patient, created_by=self.nurse,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
            case_status="under_treatment",
        )

    def _schedule(self, case):
        return VaccinationSchedule.objects.create(bite_case=case, created_by=self.nurse)

    def _dose(self, schedule, label, scheduled_date, status, actual_date=None):
        return VaccineDose.objects.create(
            schedule=schedule, dose_label=label, scheduled_date=scheduled_date,
            dose_status=status, actual_date=actual_date,
        )

    def _complete(self, dose):
        dose.dose_status = "completed"
        dose.actual_date = self.today
        dose.save(update_fields=["dose_status", "actual_date"])

    def test_detection_counts_only_overdue_unadministered_active_doses(self):
        self.assertEqual(missed_dose_count(today=self.today), 3)
        counts = missed_dose_counts_by_patient(today=self.today)
        self.assertEqual(counts.get(self.patient_a.pk), 1)
        self.assertEqual(counts.get(self.patient_b.pk), 2)
        self.assertNotIn(self.patient_c.pk, counts)
        pks = set(missed_dose_queryset(today=self.today).values_list("pk", flat=True))
        self.assertEqual(pks, {self.a_missed.pk, self.b_missed1.pk, self.b_missed2.pk})

    def test_detection_excludes_cancelled_archived_and_inactive(self):
        self.assertEqual(missed_dose_count(today=self.today), 3)
        self.a_missed.dose_status = "cancelled"
        self.a_missed.save(update_fields=["dose_status"])
        self.assertEqual(missed_dose_count(today=self.today), 2)
        self.a_missed.dose_status = "scheduled"
        self.a_missed.save(update_fields=["dose_status"])
        self.patient_b.is_archived = True
        self.patient_b.save(update_fields=["is_archived"])
        self.assertEqual(missed_dose_count(today=self.today), 1)
        self.patient_b.is_archived = False
        self.patient_b.save(update_fields=["is_archived"])
        self.case_a.case_status = "completed"
        self.case_a.save(update_fields=["case_status"])
        self.assertEqual(missed_dose_count(today=self.today), 2)

    def test_sidebar_badge_shows_total_missed_count(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["missed_dose_total"], 3)
        content = response.content.decode()
        self.assertIn("background:#DC3545", content)
        self.assertIn('aria-label="3 missed vaccination doses"', content)

    def test_single_missed_dose_shows_badge_1(self):
        self._complete(self.b_missed1)
        self._complete(self.b_missed2)
        self.client.force_login(self.nurse)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.context["missed_dose_total"], 1)
        self.assertIn('aria-label="1 missed vaccination dose"', response.content.decode())

    def test_two_missed_doses_show_badge_2(self):
        self._complete(self.a_missed)
        self.client.force_login(self.nurse)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.context["missed_dose_total"], 2)
        self.assertIn('aria-label="2 missed vaccination doses"', response.content.decode())

    def test_sidebar_badge_hidden_when_no_missed_doses(self):
        for dose in (self.a_missed, self.b_missed1, self.b_missed2):
            self._complete(dose)
        self.client.force_login(self.nurse)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.context["missed_dose_total"], 0)
        content = response.content.decode()
        self.assertNotIn("background:#DC3545", content)
        self.assertNotIn("missed vaccination dose", content)

    def test_badge_consistent_across_nurse_pages(self):
        self.client.force_login(self.nurse)
        urls = (self.dashboard_url, self.list_url, reverse("vaccination:sms_log_list"))
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["missed_dose_total"], 3)
                self.assertIn('aria-label="3 missed vaccination doses"', response.content.decode())

    def test_vaccination_records_highlight_missed_patients(self):
        self.client.force_login(self.nurse)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Missed Dose (1)", content)
        self.assertIn("Missed Dose (2)", content)
        self.assertIn("No Missed Dose", content)
        # Each patient renders once in the desktop table and once in the mobile cards.
        self.assertEqual(content.count("John Paul R Borling"), 2)
        manage_a = reverse("vaccination:nurse_schedule_manage", args=[self.schedule_a.pk])
        self.assertEqual(content.count(manage_a), 2)
        self.assertIn("Administer", content)
        self.assertIn('name="q"', content)
        self.assertIn('name="barangay"', content)

    def test_manage_page_shows_missed_status_and_alert(self):
        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse("vaccination:nurse_schedule_manage", args=[self.schedule_a.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["missed_count"], 1)
        content = response.content.decode()
        self.assertIn("status-missed", content)
        self.assertIn("Missed Vaccination Appointment", content)
        self.assertIn("John Paul R Borling missed the scheduled 2nd Dose on", content)

    def test_badge_clears_when_missed_dose_completed(self):
        self.client.force_login(self.nurse)
        before = self.client.get(self.dashboard_url)
        self.assertEqual(before.context["missed_dose_total"], 3)
        self._complete(self.a_missed)
        after = self.client.get(self.dashboard_url)
        self.assertEqual(after.context["missed_dose_total"], 2)
        self.assertIn('aria-label="2 missed vaccination doses"', after.content.decode())

    def test_badge_clears_when_missed_dose_rescheduled_to_future(self):
        self.client.force_login(self.nurse)
        before = self.client.get(self.dashboard_url)
        self.assertEqual(before.context["missed_dose_total"], 3)
        self.a_missed.scheduled_date = self.today + datetime.timedelta(days=2)
        self.a_missed.dose_status = "scheduled"
        self.a_missed.save(update_fields=["scheduled_date", "dose_status"])
        after = self.client.get(self.dashboard_url)
        self.assertEqual(after.context["missed_dose_total"], 2)
        self.assertNotIn(self.a_missed.pk, set(
            missed_dose_queryset(today=self.today).values_list("pk", flat=True)))

    def test_refreshing_pages_does_not_send_or_duplicate_sms(self):
        self.client.force_login(self.nurse)
        before = SMSLog.objects.count()
        manage_a = reverse("vaccination:nurse_schedule_manage", args=[self.schedule_a.pk])
        for _ in range(3):
            self.client.get(self.dashboard_url)
            self.client.get(self.list_url)
            self.client.get(manage_a)
        self.assertEqual(SMSLog.objects.count(), before)

    def test_missed_dose_feeds_automatic_sms_reminder(self):
        self.assertIn(self.a_missed, missed_dose_queryset(today=self.today))
        call_command(
            "send_vaccination_reminders", "--type", "missed",
            "--date", self.today.isoformat(), stdout=StringIO(),
        )
        log = SMSLog.objects.get(dose=self.a_missed, reminder_type="missed_appointment")
        self.assertEqual(log.status, "queued")
        self.assertEqual(log.message, missed_reminder_message(self.a_missed))
        # Sending SMS never changes the missed-dose count.
        self.assertEqual(missed_dose_count(today=self.today), 3)

    def test_missed_dose_counted_once_even_with_multiple_sms(self):
        self.client.force_login(self.nurse)
        for _ in range(2):
            call_command(
                "send_vaccination_reminders", "--type", "missed",
                "--date", self.today.isoformat(), stdout=StringIO(),
            )
        self.assertEqual(SMSLog.objects.filter(
            dose=self.a_missed, reminder_type="missed_appointment").count(), 1)
        self.assertEqual(missed_dose_count(today=self.today), 3)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.context["missed_dose_total"], 3)


class SmsReminderPagesTests(TestCase):
    """The Admin and Nurse portals share one redesigned SMS reminders surface."""

    def setUp(self):
        self.admin = U.objects.create_user("a.sms", password="pw12345678", role="admin")
        self.nurse = U.objects.create_user("n.sms", password="pw12345678", role="nurse")
        self.doctor = U.objects.create_user("d.sms", password="pw12345678", role="doctor")
        self.patient_user = U.objects.create_user("p.sms", password="pw12345678", role="patient")
        self.patient = Patient.objects.create(
            patient_number="PAT-S0001", first_name="Maria", last_name="Santos",
            birthdate=datetime.date(1990, 1, 1), contact_number="09173334444",
            account=self.patient_user,
        )
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-S0001", patient=self.patient, created_by=self.admin,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )
        self.schedule = VaccinationSchedule.objects.create(
            bite_case=self.case, created_by=self.admin)
        self.first_dose = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 0",
            scheduled_date=datetime.date(2026, 11, 10), dose_status="scheduled")
        self.second_dose = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 3", protocol_day=3,
            scheduled_date=datetime.date(2026, 11, 13), dose_status="scheduled")
        self.confirmation = SMSLog.objects.create(
            dose=self.first_dose, patient=self.patient, bite_case=self.case,
            reminder_type="schedule_set", recipient="639173334444",
            message="ABTC Dumingag: your appointment is confirmed.",
            scheduled_date=datetime.date(2026, 11, 10), status="queued",
        )
        # No patient FK on this row: the display must fall back to the case.
        self.advance = SMSLog.objects.create(
            dose=self.second_dose, bite_case=self.case,
            reminder_type="3days_before", recipient="639173334444",
            message="ABTC Dumingag: your appointment is in 3 days.",
            scheduled_date=datetime.date(2026, 11, 13), status="sent",
        )
        self.failed = SMSLog.objects.create(
            patient=self.patient, reminder_type="on_day",
            recipient="639173334444",
            message="ABTC Dumingag: your appointment is today.",
            scheduled_date=datetime.date(2026, 11, 10), status="failed",
            api_response="Semaphore error: SEMAPHORE_API_KEY not configured",
        )

    def _list(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse("vaccination:sms_log_list"), params)

    def test_both_managers_see_patient_list_with_one_row_per_patient(self):
        for user in (self.admin, self.nurse):
            with self.subTest(role=user.role):
                response = self._list(user)
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                for text in (
                    "SMS Reminders", "Patient List", "Patient Name",
                    "Contact Number", "Next Dose", "Next Scheduled Date",
                    "Reminder Status", "Action", "View", "Search...",
                ):
                    self.assertIn(text, content)
                self.assertEqual(content.count('id="sms-search"'), 1)
                self.assertEqual(len(response.context["patients"]), 1)
                self.assertEqual(response.context["page_obj"].paginator.count, 1)
                self.assertEqual(content.count("Maria Santos"), 1)
                self.assertEqual(response.context["total"], 3)
                self.assertEqual(response.context["sent"], 1)
                self.assertEqual(response.context["queued"], 1)
                self.assertEqual(response.context["failed"], 1)
                self.assertContains(response, reverse(
                    "vaccination:sms_patient_detail", args=[self.patient.pk]))
                self.assertNotIn(self.confirmation.message, content)
                self.assertNotIn("Upcoming Vaccination Reminders", content)
                self.assertNotIn("Reminder History", content)
                self.assertNotIn("Send SMS", content)
                self.assertEqual(content.count('class="sms-view-button"'), 1)

    def test_global_search_matches_patient_case_contact_dose_date_and_status(self):
        searches = ("Maria", "ABC-S0001", "0917", "2nd Dose", "Nov 13, 2026",
                    "Sent", "Queued", "Failed")
        for keyword in searches:
            with self.subTest(keyword=keyword):
                response = self._list(self.nurse, q=keyword)
                self.assertEqual(response.context["page_obj"].paginator.count, 1)
                self.assertContains(response, "Maria Santos")

        empty = self._list(self.nurse, q="no-such-patient")
        self.assertEqual(empty.context["page_obj"].paginator.count, 0)
        self.assertContains(empty, "No patients match your search.")

    def test_patient_sms_details_show_only_patient_schedules_and_history(self):
        other_patient = Patient.objects.create(
            patient_number="PAT-S0002", first_name="Other", last_name="Person",
            birthdate=datetime.date(1990, 1, 1), contact_number="09175556666",
        )
        other_case = AnimalBiteCase.objects.create(
            case_number="ABC-S0002", patient=other_patient, created_by=self.admin,
            animal_type="cat", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 2, 9, 0),
        )
        other_schedule = VaccinationSchedule.objects.create(
            bite_case=other_case, created_by=self.admin)
        VaccineDose.objects.create(
            schedule=other_schedule, dose_label="Day 3",
            scheduled_date=datetime.date(2026, 11, 14), dose_status="scheduled")
        SMSLog.objects.create(
            patient=other_patient, bite_case=other_case,
            reminder_type="on_day", recipient="639175556666",
            message="Other patient's reminder", status="sent",
        )
        self.client.force_login(self.nurse)
        url = reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk])
        with patch('django.utils.timezone.now', return_value=datetime.datetime(
            2026, 11, 12, 9, tzinfo=ZoneInfo('Asia/Manila'),
        )):
            response = self.client.get(url)
        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        for text in (
            "Patient SMS Details", "PAT-S0001", "ABC-S0001",
            "Current Treatment Status", "Vaccination Schedules",
            "1st Dose", "2nd Dose", "SMS Reminder History",
            "Schedule Confirmation", "Advance Reminder",
            "Appointment Day Reminder", "Queued", "Sent", "Failed",
            "Send SMS", "Send Vaccination Reminder",
            "Confirm Send SMS", "Select Scheduled Dose",
        ):
            self.assertIn(text, content)
        self.assertNotIn("PAT-S0002", content)
        self.assertNotIn("ABC-S0002", content)
        self.assertNotIn("Other patient's reminder", content)
        self.assertEqual(
            len(re.findall(r'<button\b[^>]*id="open-sms-modal"[^>]*>', content)),
            1)
        self.assertEqual(
            response.context["eligible_doses"][0]["dose"], self.second_dose)

    def test_patient_details_do_not_show_send_button_to_admin(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, ">Send SMS</button>", html=False)

    def test_detail_page_shows_the_complete_saved_record(self):
        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse("vaccination:sms_log_detail", args=[self.confirmation.pk]))
        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        for text in (
            "SMS Reminder Details",
            "Maria Santos",
            "ABC-S0001",
            "1st Dose",
            "Confirmation",
            "639173334444",
            "Nov 10, 2026",
            "Complete SMS Message",
            self.confirmation.message,
        ):
            with self.subTest(text=text):
                self.assertIn(text, content)
        self.assertNotIn("Semaphore", content)
        self.assertNotIn("RHUDumingag", content)

    def test_detail_page_shows_a_sanitized_failure_reason(self):
        self.client.force_login(self.nurse)
        content = self.client.get(
            reverse("vaccination:sms_log_detail", args=[self.failed.pk])).content.decode()
        self.assertIn("Failure Reason", content)
        self.assertIn("SMS provider", content)
        self.assertIn("not configured", content)
        self.assertNotIn("Semaphore", content)

    def test_non_staff_roles_cannot_reach_sms_pages(self):
        detail_url = reverse("vaccination:sms_log_detail", args=[self.confirmation.pk])
        patient_detail_url = reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk])
        for user, destination in (
            (self.doctor, reverse("dashboard:index")),
            (self.patient_user, reverse("accounts:patient_portal")),
        ):
            self.client.force_login(user)
            for url in (
                    reverse("vaccination:sms_log_list"), detail_url,
                    patient_detail_url):
                with self.subTest(role=user.role, url=url):
                    self.assertRedirects(
                        self.client.get(url), destination,
                        fetch_redirect_response=False)

    def test_pagination_keeps_active_filters(self):
        for index in range(26):
            patient = Patient.objects.create(
                patient_number=f"PAT-P{index:04d}", first_name="Page",
                last_name=f"Patient {index:02d}",
                birthdate=datetime.date(1990, 1, 1),
                contact_number=f"0917333{index:04d}",
            )
            case = AnimalBiteCase.objects.create(
                case_number=f"ABC-P{index:04d}", patient=patient,
                created_by=self.admin, animal_type="dog", exposure_type="bite",
                date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
            )
            SMSLog.objects.create(
                bite_case=case, patient=patient,
                reminder_type="schedule_set", recipient="639173334444",
                message="ABTC Dumingag: extra reminder %s." % index,
                scheduled_date=datetime.date(2026, 11, 10), status="sent",
            )
        response = self._list(self.nurse, q="Patient")
        self.assertEqual(response.context["page_obj"].paginator.count, 26)
        self.assertIn("?q=Patient&amp;page=2", response.content.decode())


@override_settings(SEMAPHORE_ENABLED=False)
class SmsUpcomingAndManualSendTests(TestCase):
    """Upcoming 2nd+ doses and the additional manual reminder action."""

    def setUp(self):
        self.admin = U.objects.create_user("a.up", password="pw12345678", role="admin")
        self.nurse = U.objects.create_user("n.up", password="pw12345678", role="nurse")
        self.doctor = U.objects.create_user("d.up", password="pw12345678", role="doctor")
        self.patient_user = U.objects.create_user("p.up", password="pw12345678", role="patient")
        self.patient = Patient.objects.create(
            patient_number="PAT-U0001", first_name="Juan", last_name="Dela Cruz",
            birthdate=datetime.date(1988, 5, 5), contact_number="09173334444",
            account=self.patient_user,
        )
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-U0001", patient=self.patient, created_by=self.admin,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )
        self.schedule = VaccinationSchedule.objects.create(
            bite_case=self.case, created_by=self.admin)
        self.today = timezone.localdate()
        day = datetime.timedelta(days=1)
        self.first = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 0",
            scheduled_date=self.today + 2 * day, dose_status="scheduled")
        self.second = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 3", protocol_day=3,
            scheduled_date=self.today + 2 * day, dose_status="scheduled")
        self.third = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 7", protocol_day=7,
            scheduled_date=self.today + day, dose_status="scheduled")
        self.fourth = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 14", protocol_day=14,
            scheduled_date=self.today + 5 * day, dose_status="rescheduled")
        self.due = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 28", protocol_day=28,
            scheduled_date=self.today, dose_status="scheduled")
        self.completed = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 35", protocol_day=35,
            scheduled_date=self.today + 2 * day, dose_status="completed")
        self.cancelled = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 42", protocol_day=42,
            scheduled_date=self.today + 2 * day, dose_status="cancelled")
        self.unscheduled = VaccineDose.objects.create(
            schedule=self.schedule, dose_label="Day 49", protocol_day=49,
            dose_status="pending")
        self.list_url = reverse("vaccination:sms_log_list")
        self.send_url = reverse("vaccination:sms_manual_send", args=[self.second.pk])

    def _list(self, user=None, **params):
        self.client.force_login(user or self.nurse)
        return self.client.get(self.list_url, params)

    def _send(self, dose=None, kind="manual_2days_before", note="",
              confirm=False, user=None, return_to_patient=False):
        self.client.force_login(user or self.nurse)
        payload = {"reminder_type": kind, "custom_message": note}
        if confirm:
            payload["confirm_duplicate"] = "1"
        if return_to_patient:
            payload["return_to_patient"] = str(self.patient.pk)
        dose = dose or self.second
        return self.client.post(
            reverse("vaccination:sms_manual_send", args=[dose.pk]), payload)

    def test_upcoming_lists_only_eligible_second_dose_onwards(self):
        response = self._list()
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertIn("Juan Dela Cruz", content)
        self.assertIn("5th Dose", content)
        self.assertIn("View", content)
        self.assertNotIn("Send SMS", content)
        self.assertNotIn("Upcoming Vaccination Reminders", content)
        self.assertEqual(SMSLog.objects.count(), 0)

    def test_patient_details_provide_one_send_button_and_confirmation_modal(self):
        second_case = AnimalBiteCase.objects.create(
            case_number="ABC-U0002", patient=self.patient,
            created_by=self.admin, animal_type="cat", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 2, 9, 0),
        )
        second_schedule = VaccinationSchedule.objects.create(
            bite_case=second_case, created_by=self.admin)
        VaccineDose.objects.create(
            schedule=second_schedule, dose_label="Day 0",
            scheduled_date=self.today + datetime.timedelta(days=2),
            dose_status="scheduled")
        VaccineDose.objects.create(
            schedule=second_schedule, dose_label="Day 3",
            scheduled_date=self.today + datetime.timedelta(days=4),
            dose_status="scheduled")
        self.client.force_login(self.nurse)
        response = self.client.get(reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk]))
        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Select Animal Bite Case", content)
        self.assertIn("Select Scheduled Dose", content)
        self.assertIn("SMS Message Preview", content)
        self.assertIn("manual_2days_before", content)
        self.assertEqual(
            len(re.findall(r'<button\b[^>]*id="open-sms-modal"[^>]*>', content)),
            1)
        self.assertEqual(len(response.context["eligible_doses"]), 5)
        self.assertEqual(len(response.context["eligible_cases"]), 2)
        self.assertEqual(SMSLog.objects.count(), 0)

    def test_upcoming_details_modal_summarises_the_latest_sms_record(self):
        log = SMSLog.objects.create(
            dose=self.second, patient=self.patient, bite_case=self.case,
            reminder_type="manual_2days_before", recipient="639173334444",
            message="ABTC Dumingag: earlier manual reminder.",
            scheduled_date=self.second.scheduled_date, status="sent",
            sent_at=timezone.now(),
        )
        self.client.force_login(self.nurse)
        response = self.client.get(reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk]))
        content = response.content.decode()
        self.assertContains(response, "Manual Reminder")
        self.assertContains(response, "Sent")
        self.assertContains(response, timezone.localtime(log.sent_at).strftime('%b %d, %Y'))
        self.assertEqual(
            self.client.get(reverse(
                "vaccination:sms_patient_detail", args=[self.patient.pk])).status_code,
            200)
        self.assertEqual(SMSLog.objects.count(), 1)

    def test_upcoming_shows_no_registered_number_and_disables_send(self):
        self.patient.contact_number = "not-a-number"
        self.patient.emergency_contact_number = ""
        self.patient.save(update_fields=["contact_number", "emergency_contact_number"])
        self.patient_user.mobile = ""
        self.patient_user.save(update_fields=["mobile"])

        self.client.force_login(self.nurse)
        response = self.client.get(reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk]))
        content = response.content.decode()
        self.assertEqual(response.context["eligible_doses"], [])
        self.assertIn('disabled title="No eligible scheduled doses or registered mobile number"', content)
        self.assertEqual(SMSLog.objects.count(), 0)

    def test_upcoming_filters_use_saved_appointment_dates(self):
        response = self._list()
        row = response.context["patients"][0]
        self.assertEqual(row["next_dose"], self.due)

    def test_manual_send_records_history_and_details(self):
        response = self._send(self.second, "manual_2days_before")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "queued")

        log = SMSLog.objects.get(
            dose=self.second, reminder_type="manual_2days_before")
        self.assertEqual(log.status, "queued")
        self.assertEqual(log.recipient, "639173334444")
        self.assertEqual(log.patient_id, self.patient.pk)
        self.assertEqual(log.bite_case_id, self.case.pk)
        self.assertEqual(log.scheduled_date, self.second.scheduled_date)
        self.assertIn("2nd Dose", log.message)
        self.assertIn(self.second.scheduled_date.strftime("%b %d, %Y"), log.message)
        self.assertTrue(
            AuditLog.objects.filter(
                action="SEND_SMS", description__contains="Manual SMS").exists())

        listing = self._list().content.decode()
        self.assertIn("Queued", listing)

        detail = self.client.get(reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk])).content.decode()
        self.assertIn("Manual Reminder", detail)
        log_detail = self.client.get(
            reverse("vaccination:sms_log_detail", args=[log.pk])).content.decode()
        self.assertIn("Manual – 2 Days Before", log_detail)
        self.assertIn("Manual", detail)
        self.assertIn(log.message, detail)

    def test_confirmed_manual_send_returns_to_selected_patient_details(self):
        response = self._send(
            self.second, "manual_2days_before", return_to_patient=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["redirect"],
            reverse("vaccination:sms_patient_detail", args=[self.patient.pk]))

    def test_manual_send_now_works_for_any_appointment_date(self):
        two_days = self._send(self.second, "manual_immediate")
        self.assertEqual(two_days.status_code, 200)
        tomorrow = self._send(self.third, "manual_immediate")
        self.assertEqual(tomorrow.status_code, 200)

        log = SMSLog.objects.get(dose=self.second, reminder_type="manual_immediate")
        self.assertEqual(log.get_reminder_type_display(), "Manual – Send Now")
        self.assertIn("2nd Dose", log.message)
        self.assertIn("Pumunta sa RHU sa nakatakdang petsa.", log.message)
        self.assertNotIn("2 araw", log.message)
        self.assertIn(
            "3rd Dose",
            SMSLog.objects.get(dose=self.third, reminder_type="manual_immediate").message)
        self.assertIn("Manual Reminder", self.client.get(reverse(
            "vaccination:sms_patient_detail", args=[self.patient.pk])).content.decode())

        duplicate = self._send(self.second, "manual_immediate")
        self.assertEqual(duplicate.status_code, 409)
        self.assertTrue(duplicate.json()["duplicate"])

    def test_manual_send_rejects_mismatched_day_and_inactive_doses(self):
        mismatched = self._send(self.second, "manual_1day_before")
        self.assertEqual(mismatched.status_code, 400)
        self.assertIn("2 day(s) away", mismatched.json()["error"])
        for dose in (self.completed, self.cancelled, self.unscheduled, self.first):
            with self.subTest(dose=dose.pk):
                response = self._send(dose, "manual_2days_before")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(SMSLog.objects.count(), 0)

    def test_manual_send_requires_a_valid_registered_number(self):
        self.patient.contact_number = "not-a-number"
        self.patient.save(update_fields=["contact_number"])
        response = self._send(self.second)
        self.assertEqual(response.status_code, 400)
        self.assertIn("missing or invalid", response.json()["error"])
        self.assertEqual(SMSLog.objects.count(), 0)

    def test_manual_send_warns_on_duplicate_and_requires_confirmation(self):
        self.assertEqual(self._send(self.second).status_code, 200)
        duplicate = self._send(self.second)
        self.assertEqual(duplicate.status_code, 409)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertIn("already", duplicate.json()["warning"])
        self.assertEqual(SMSLog.objects.filter(dose=self.second).count(), 1)

        again = self._send(self.second)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(SMSLog.objects.filter(dose=self.second).count(), 1)

        confirmed = self._send(self.second, confirm=True)
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(SMSLog.objects.filter(dose=self.second).count(), 2)

    def test_manual_custom_requires_and_uses_a_note(self):
        missing = self._send(self.fourth, "manual_custom")
        self.assertEqual(missing.status_code, 400)
        sent = self._send(
            self.fourth, "manual_custom", note="Palihog tawag sa RHU kung dili makaadto.")
        self.assertEqual(sent.status_code, 200)
        log = SMSLog.objects.get(dose=self.fourth, reminder_type="manual_custom")
        self.assertIn("4th Dose", log.message)
        self.assertIn("Palihog tawag sa RHU kung dili makaadto.", log.message)

    def test_manual_send_is_limited_to_staff_and_post(self):
        self.client.force_login(self.nurse)
        self.assertEqual(self.client.get(self.send_url).status_code, 405)

        admin_response = self._send(self.second, user=self.admin)
        self.assertEqual(admin_response.status_code, 403)
        doctor_response = self._send(self.second, user=self.doctor)
        self.assertEqual(doctor_response.status_code, 403)
        # Patients are kept inside patient-facing routes by the portal middleware.
        self.client.force_login(self.patient_user)
        patient_response = self.client.post(
            self.send_url, {"reminder_type": "manual_2days_before"})
        self.assertRedirects(
            patient_response, reverse("accounts:patient_portal"),
            fetch_redirect_response=False)
        self.assertEqual(SMSLog.objects.count(), 0)

    def test_manual_send_does_not_create_a_second_automatic_reminder(self):
        automatic = SMSLog.objects.create(
            dose=self.second, patient=self.patient, bite_case=self.case,
            reminder_type="3days_before", recipient="639173334444",
            message="ABTC Dumingag: automatic three-day reminder.",
            scheduled_date=self.second.scheduled_date, status="sent",
        )
        response = self._send(self.second)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SMSLog.objects.filter(pk=automatic.pk).exists())
        self.assertEqual(SMSLog.objects.filter(dose=self.second).count(), 2)


@override_settings(SEMAPHORE_ENABLED=False)
class AdminScheduleDetailVisibilityTests(TestCase):
    """Admin schedule detail hides Progress, SMS Reminders and Patient & Case.

    The nurse and doctor clinical views keep every section. This is a
    visibility-only change: no schedule, dose, or SMS record is touched.
    """

    def setUp(self):
        self.admin = U.objects.create_user("adm.sdv", password="pw12345678", role="admin")
        self.nurse = U.objects.create_user("nur.sdv", password="pw12345678", role="nurse")
        self.doctor = U.objects.create_user("doc.sdv", password="pw12345678", role="doctor")
        self.patient = Patient.objects.create(
            patient_number="PAT-SDV01", first_name="Amara", last_name="Santos",
            birthdate=datetime.date(1990, 3, 3), contact_number="09170000099",
        )
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-SDV01", patient=self.patient, created_by=self.nurse,
            animal_type="dog", exposure_type="bite",
            date_time_bite=datetime.datetime(2026, 10, 1, 9, 0),
        )
        AnimalBiteCase.objects.filter(pk=self.case.pk).update(
            date_time_consultation=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0))
        )
        self.case.refresh_from_db()
        self.schedule, _ = ensure_vaccination_schedule(self.case, created_by=self.nurse)
        self.url = reverse("vaccination:schedule_detail", args=[self.schedule.pk])

    def _get(self, user):
        self.client.force_login(user)
        return self.client.get(self.url).content.decode()

    def test_admin_keeps_header_and_timeline_only(self):
        html = self._get(self.admin)
        self.assertIn("Vaccination Schedule", html)
        self.assertIn(self.case.case_number, html)
        self.assertIn(self.patient.full_name(), html)
        self.assertIn("View Case", html)
        self.assertIn("Vaccination Doses", html)
        self.assertIn("view only", html)
        self.assertNotIn("tap to update", html)
        self.assertNotIn("Workflow:", html)
        self.assertNotIn(reverse("vaccination:dose_update", args=[self.schedule.doses.first().pk]), html)

    def test_back_button_returns_to_role_specific_records_list(self):
        pattern = r'<a href="([^"]+)" class="w-9 h-9 rounded-full[^"]*">'
        admin_html = self._get(self.admin)
        match = re.search(pattern, admin_html)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), reverse("vaccination:patient_schedule_list"))
        for user in (self.nurse, self.doctor):
            with self.subTest(role=user.role):
                match = re.search(pattern, self._get(user))
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), reverse("vaccination:schedule_list"))

    def test_admin_hides_the_three_sections(self):
        html = self._get(self.admin)
        for marker in ("Progress", "stock auto-deducts.", "The patient is texted",
                       "Send Due Reminders", "Patient & Case", "Antibiotics"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, html)
        self.assertNotIn(self.patient.contact_display, html)

    def test_admin_hides_workflow_footer_and_follow_ups(self):
        html = self._get(self.admin)
        self.assertNotIn("Workflow:", html)
        self.assertNotIn("Doctor sets the dose count", html)
        self.assertNotIn("Follow-ups →", html)
        self.assertNotIn(reverse("vaccination:follow_up_list"), html)
        self.assertIn("Vaccination Doses", html)

    def test_nurse_and_doctor_keep_every_section(self):
        for user in (self.nurse, self.doctor):
            with self.subTest(role=user.role):
                html = self._get(user)
                for marker in ("Progress", "The patient is texted", "Send Due Reminders",
                               "Patient & Case", "Antibiotics", "Vaccination Doses",
                               "Workflow:", "Follow-ups →"):
                    self.assertIn(marker, html)
                self.assertIn(reverse("vaccination:follow_up_list"), html)

    def test_viewing_does_not_change_records(self):
        before = (self.schedule.doses.count(),
                  SMSLog.objects.filter(dose__schedule=self.schedule).count())
        self._get(self.admin)
        self._get(self.nurse)
        after = (self.schedule.doses.count(),
                 SMSLog.objects.filter(dose__schedule=self.schedule).count())
        self.assertEqual(before, after)

    def test_admin_timeline_uses_ordinal_dose_labels(self):
        html = self._get(self.admin)
        for label in ("1st Dose", "2nd Dose", "3rd Dose", "4th Dose", "5th Dose"):
            with self.subTest(label=label):
                self.assertIn(label, html)
        for day in ("Day 0", "Day 3", "Day 7", "Day 14", "Day 28"):
            with self.subTest(day=day):
                self.assertNotIn(day, html)

    def test_other_roles_keep_day_based_labels(self):
        for user in (self.nurse, self.doctor):
            with self.subTest(role=user.role):
                html = self._get(user)
                for day in ("Day 0", "Day 3", "Day 7"):
                    self.assertIn(day, html)

    def test_ordinal_labels_follow_dose_sequence(self):
        labels = [d.number_label for d in self.schedule.doses.order_by("id")]
        self.assertEqual(
            labels, ["1st Dose", "2nd Dose", "3rd Dose", "4th Dose", "5th Dose"])

    def test_label_change_is_presentation_only(self):
        def snapshot():
            return [(d.pk, d.dose_label, d.scheduled_date, d.actual_date, d.dose_status)
                    for d in self.schedule.doses.order_by("id")]
        before = snapshot()
        self._get(self.admin)
        self.assertEqual(snapshot(), before)
        self.assertEqual(
            [d.dose_label for d in self.schedule.doses.order_by("id")],
            ["Day 0", "Day 3", "Day 7", "Day 14", "Day 28"])


@override_settings(SEMAPHORE_ENABLED=False)
class AdminDoseReadOnlyTests(TestCase):
    """Admin vaccination timeline is view-only; dose-update endpoints deny admin.

    The Nurse workflow keeps full administration rights. A denied admin never
    changes a dose, stock quantity, or audit trail.
    """

    def setUp(self):
        self.admin = U.objects.create_user("adm.ro", password="pw12345678", role="admin")
        self.nurse = U.objects.create_user("nur.ro", password="pw12345678", role="nurse")
        self.patient = Patient.objects.create(
            patient_number="PAT-RO01", first_name="Read", last_name="Only",
            birthdate=datetime.date(1992, 2, 2), contact_number="09170000077",
        )
        self.case = AnimalBiteCase.objects.create(
            case_number="ABC-RO01", patient=self.patient, created_by=self.nurse,
            animal_type="dog", exposure_type="bite",
            date_time_bite=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0)),
        )
        AnimalBiteCase.objects.filter(pk=self.case.pk).update(
            date_time_consultation=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0))
        )
        self.case.refresh_from_db()
        self.schedule, _ = ensure_vaccination_schedule(self.case, created_by=self.nurse)
        self.first = self.schedule.doses.order_by("id").first()
        self.second = self.schedule.doses.order_by("id")[1]
        self.stock = VaccineInventory.objects.create(
            vaccine_name="Rabies Vaccine", brand="Verorab", batch_number="LOT-RO",
            expiration_date=datetime.date(2027, 1, 1),
            quantity_received=10, quantity_available=10, quantity_used=0,
        )
        self.detail_url = reverse("vaccination:schedule_detail", args=[self.schedule.pk])

    def _detail(self, user):
        self.client.force_login(user)
        return self.client.get(self.detail_url).content.decode()

    def test_admin_timeline_is_view_only(self):
        html = self._detail(self.admin)
        self.assertIn("view only", html)
        self.assertIn("1st Dose", html)
        for kept in ("Scheduled", "Actual", "Vaccine · Batch", "Remarks"):
            with self.subTest(kept=kept):
                self.assertIn(kept, html)
        for hidden in ("No SMS yet for this dose", "3-day reminder", "Today reminder",
                       "tap to update",
                       "text-[11px] font-medium text-muted text-center hidden sm:block",
                       reverse("vaccination:dose_update", args=[self.first.pk]),
                       reverse("vaccination:send_sms_for_dose", args=[self.first.pk])):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, html)

    def test_nurse_timeline_keeps_controls(self):
        html = self._detail(self.nurse)
        self.assertIn("tap to update", html)
        self.assertIn("3-day reminder", html)
        self.assertIn(reverse("vaccination:dose_update", args=[self.first.pk]), html)
        self.assertIn(reverse("vaccination:send_sms_for_dose", args=[self.first.pk]), html)

    def test_admin_cannot_open_dose_update(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("vaccination:dose_update", args=[self.first.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_admin_cannot_post_dose_update(self):
        self.client.force_login(self.admin)
        before = self.second.dose_status
        resp = self.client.post(
            reverse("vaccination:dose_update", args=[self.second.pk]),
            {"dose_label": self.second.dose_label, "dose_status": "completed",
             "scheduled_date": "2026-10-04", "actual_date": "2026-10-04",
             "vaccine_brand": "Verorab", "batch_number": "LOT-RO", "remarks": ""},
        )
        self.assertEqual(resp.status_code, 403)
        self.second.refresh_from_db()
        self.assertEqual(self.second.dose_status, before)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity_available, 10)
        self.assertFalse(AuditLog.objects.filter(action="UPDATE_DOSE").exists())

    def test_admin_cannot_post_dose_schedule(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("vaccination:dose_schedule", args=[self.second.pk]),
            {"scheduled_date": "2026-10-04"},
        )
        self.assertEqual(resp.status_code, 403)
        self.second.refresh_from_db()
        self.assertIsNone(self.second.scheduled_date)

    def test_admin_cannot_post_dose_administer(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("vaccination:dose_administer", args=[self.first.pk]),
            {"vaccine_brand": "Verorab", "batch_number": "LOT-RO"},
        )
        self.assertEqual(resp.status_code, 403)
        self.first.refresh_from_db()
        self.assertNotEqual(self.first.dose_status, "completed")
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity_available, 10)

    def test_nurse_can_still_open_dose_update(self):
        self.client.force_login(self.nurse)
        resp = self.client.get(reverse("vaccination:dose_update", args=[self.first.pk]))
        self.assertEqual(resp.status_code, 200)
