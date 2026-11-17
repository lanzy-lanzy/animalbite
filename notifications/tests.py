from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from vaccination.tests import _MissedAppointmentFixture
from vaccination.models import SMSLog, VaccineDose
from .models import Notification
from .utils import dispatch_marked_missed_schedules, dispatch_missed_schedule


@override_settings(SEMAPHORE_ENABLED=True, SEMAPHORE_API_KEY="test-only")
class MissedScheduleNotificationTests(_MissedAppointmentFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.account = get_user_model().objects.create_user('notice-patient', role='patient')
        self.patient.account = self.account
        self.patient.save(update_fields=['account'])
        self.now = self._at(self.today, 10)
        self.dose.scheduled_date = self.today
        self.dose.dose_status = 'missed'
        VaccineDose.objects.filter(pk=self.dose.pk).update(scheduled_date=self.today, dose_status='missed')
        gateway_patch = patch('vaccination.missed_appointments.send_semaphore_sms')
        self.gateway = gateway_patch.start()
        self.addCleanup(gateway_patch.stop)
        self.gateway.return_value = {
            'success': True, 'response': [{'message_id': 'test-id', 'status': 'pending'}],
        }

    def test_same_day_marked_missed_sends_both_with_status(self):
        notice, log, result = dispatch_missed_schedule(self.dose.pk, now=self.now)
        self.assertTrue(result['success'])
        self.assertEqual(log.status, 'sent')
        self.assertEqual(notice.recipient, self.account)
        self.assertEqual(notice.payload['status'], 'missed')
        self.assertEqual(notice.payload['recorded_status'], 'missed')
        self.assertEqual(notice.payload['scheduled_date'], self.today.isoformat())
        for message in (notice.message, log.message):
            self.assertIn('missed', message.lower().split())
            self.assertIn('schedule', message.lower().split())
            self.assertIn('Status: Missed', message)

    def test_repeated_signal_and_polling_do_not_duplicate(self):
        dispatch_missed_schedule(self.dose.pk, now=self.now)
        dispatch_marked_missed_schedules(now=self.now)
        dispatch_missed_schedule(self.dose.pk, now=self.now)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(SMSLog.objects.count(), 1)
        self.gateway.assert_called_once()

    def test_status_update_triggers_after_commit(self):
        with patch('notifications.utils.dispatch_missed_schedule') as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                self.dose.save(update_fields=['dose_status'])
                dispatch.assert_not_called()
            dispatch.assert_called_once_with(self.dose.pk)

    def test_future_marked_missed_and_completed_doses_do_not_notify(self):
        for changes in (
            {'scheduled_date': self.today + timedelta(days=1)},
            {'dose_status': 'completed'}, {'actual_date': self.today},
            {'actual_time': self.now.time().replace(tzinfo=None)},
        ):
            with self.subTest(changes=changes):
                VaccineDose.objects.filter(pk=self.dose.pk).update(
                    scheduled_date=self.today, dose_status='missed', actual_date=None, actual_time=None)
                VaccineDose.objects.filter(pk=self.dose.pk).update(**changes)
                dispatch_missed_schedule(self.dose.pk, now=self.now)
        self.assertFalse(Notification.objects.exists())
        self.gateway.assert_not_called()

    def test_unmarked_incomplete_today_waits_until_next_day(self):
        VaccineDose.objects.filter(pk=self.dose.pk).update(dose_status='scheduled')
        dispatch_missed_schedule(self.dose.pk, now=self.now)
        self.gateway.assert_not_called()
        self.assertFalse(Notification.objects.exists())
        dispatch_missed_schedule(self.dose.pk, now=self.now + timedelta(days=1))
        self.assertEqual(Notification.objects.get().payload['recorded_status'], 'scheduled')
        self.gateway.assert_called_once()

    def test_no_consent_blocks_sms_but_keeps_own_in_app_notice(self):
        self.consent.consent_given = False
        self.consent.save(update_fields=['consent_given'])
        notice, log, result = dispatch_missed_schedule(self.dose.pk, now=self.now)
        self.assertIsNotNone(notice)
        self.assertEqual(log.status, 'skipped')
        self.gateway.assert_not_called()

    def test_no_linked_account_still_allows_consented_sms(self):
        self.patient.account = None
        self.patient.save(update_fields=['account'])
        notice, log, result = dispatch_missed_schedule(self.dose.pk, now=self.now)
        self.assertIsNone(notice)
        self.gateway.assert_called_once()

    def test_portal_only_displays_own_notices(self):
        dispatch_missed_schedule(self.dose.pk, now=self.now)
        self.client.force_login(self.account)
        response = self.client.get(reverse('accounts:patient_portal'))
        self.assertContains(response, 'You have missed your schedule')
        self.assertEqual(list(response.context['appointment_notifications'])[0].recipient, self.account)
        other = get_user_model().objects.create_user('unrelated-patient', role='patient')
        self.client.force_login(other)
        response = self.client.get(reverse('accounts:patient_portal'))
        self.assertNotContains(response, 'You have missed your schedule')

    def test_setting_yesterday_date_uses_missed_sms_not_confirmation(self):
        from vaccination.views import _send_schedule_set_reminder

        yesterday = self.today - timedelta(days=1)
        VaccineDose.objects.filter(pk=self.dose.pk).update(
            scheduled_date=yesterday, dose_status='scheduled')
        self.dose.refresh_from_db()
        with patch('vaccination.views.timezone.localdate', return_value=self.today):
            log, reason = _send_schedule_set_reminder(self.dose, self.nurse)
        self.assertIsNone(reason)
        self.assertEqual(log.reminder_type, 'missed_appointment')
        self.assertIn('missed your schedule', log.message)
        self.assertEqual(log.scheduled_date, yesterday)
        self.gateway.assert_called_once()
        with patch('vaccination.views.timezone.localdate', return_value=self.today):
            _send_schedule_set_reminder(self.dose, self.nurse)
        self.gateway.assert_called_once()
