"""Missed schedule notifications, shared by record updates and the scheduler."""
from django.db import transaction
from django.utils import timezone

from vaccination.missed_appointments import missed_reminder_is_due
from vaccination.models import VaccineDose

from .models import Notification


def create_missed_schedule_notification(dose_id, now=None, today=None, expected_date=None):
    now = now or timezone.now()
    with transaction.atomic():
        dose = VaccineDose.objects.select_for_update().select_related(
            "schedule__bite_case__patient__account",
        ).get(pk=dose_id)
        if (
            not missed_reminder_is_due(dose, now=now, today=today)
            or (expected_date is not None and dose.scheduled_date != expected_date)
        ):
            return None
        patient = dose.schedule.bite_case.patient
        if not patient.account_id or not patient.account.is_active:
            return None
        from vaccination.sms import build_missed_reminder_message

        payload = {
            "appointment_id": dose.pk,
            "scheduled_date": dose.scheduled_date.isoformat(),
            "status": "missed",
            "recorded_status": dose.dose_status,
            "dose": dose.number_label,
        }
        notification, _ = Notification.objects.get_or_create(
            recipient_id=patient.account_id, dose=dose,
            scheduled_date=dose.scheduled_date, kind="missed_schedule",
            defaults={"message": build_missed_reminder_message(dose), "payload": payload},
        )
        # A previous overdue alert can later be explicitly marked missed.
        if notification.payload != payload:
            notification.payload = payload
            notification.save(update_fields=["payload"])
        return notification


def dispatch_missed_schedule(dose_id, now=None, expected_date=None, today=None):
    """Persist the in-app notice, then use the existing durable SMS claim."""
    from vaccination.missed_appointments import send_missed_appointment_reminder

    notification = create_missed_schedule_notification(
        dose_id, now=now, today=today, expected_date=expected_date)
    options = {"today": today, "retry_failed": False, "expected_scheduled_date": expected_date}
    if now is not None:
        options["now"] = now
    log, result = send_missed_appointment_reminder(dose_id, **options)
    return notification, log, result


def dispatch_marked_missed_schedules(now=None):
    """Catch explicit missed updates, including bulk updates that skip signals."""
    from zoneinfo import ZoneInfo

    now = now or timezone.now()
    today = timezone.localtime(now, ZoneInfo("Asia/Manila")).date()
    ids = VaccineDose.objects.filter(
        scheduled_date=today, dose_status="missed",
        actual_date__isnull=True, actual_time__isnull=True,
        schedule__bite_case__patient__is_archived=False,
        schedule__bite_case__case_status__in=("new", "under_treatment", "missed"),
    ).values_list("pk", flat=True)
    for dose_id in ids.iterator():
        dispatch_missed_schedule(dose_id, now=now, expected_date=today)
