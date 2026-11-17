import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from .models import SMSLog, VaccineDose
from .sms import build_missed_reminder_message, gateway_outcome, normalize_ph_number, safe_gateway_response, send_semaphore_sms

MISSED_REMINDER_TYPE = 'missed_appointment'
MISSED_REMINDER_MAX_ATTEMPTS = 3
MISSED_REMINDER_LOOKBACK_DAYS = 7
MISSED_CATCH_UP_REVIEW_LIMIT = 25
ACTIVE_CASE_STATUSES = ('new', 'under_treatment', 'missed')


def _appointment_cutoff():
    value = getattr(settings, 'ABTC_APPOINTMENT_CUTOFF_TIME', None)
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, '%H:%M').time()
        except ValueError:
            return None
    return None


def _local_now(now=None):
    current = now or timezone.now()
    manila = ZoneInfo('Asia/Manila')
    if timezone.is_naive(current):
        current = timezone.make_aware(current, manila)
    return timezone.localtime(current, manila)


def _unadministered_and_active(dose):
    if (
        not dose.scheduled_date
        or dose.actual_date is not None
        or dose.actual_time is not None
        or dose.dose_status not in ('scheduled', 'rescheduled', 'missed')
    ):
        return False

    case = dose.schedule.bite_case
    return (
        not case.patient.is_archived
        and case.case_status in ACTIVE_CASE_STATUSES
    )


def missed_appointment_is_due(dose, today=None, now=None):
    if not _unadministered_and_active(dose):
        return False

    local_now = _local_now(now)
    local_today = today or local_now.date()
    if dose.scheduled_date < local_today:
        return True
    if dose.scheduled_date > local_today:
        return False
    if dose.dose_status == 'missed':
        return True

    cutoff = _appointment_cutoff()
    return cutoff is not None and local_now.time() >= cutoff


def missed_reminder_is_due(dose, today=None, now=None):
    if not _unadministered_and_active(dose):
        return False
    local_today = today or _local_now(now).date()
    return (
        local_today - timedelta(days=MISSED_REMINDER_LOOKBACK_DAYS) <= dose.scheduled_date < local_today
        or (dose.scheduled_date == local_today and dose.dose_status == 'missed')
    )


def missed_reminder_doses(today=None, now=None):
    local_today = today or _local_now(now).date()
    blocked_events = SMSLog.objects.filter(
        dose_id=OuterRef('pk'), scheduled_date=OuterRef('scheduled_date'),
        reminder_type=MISSED_REMINDER_TYPE, status__in=('sent', 'unknown'),
    )
    return VaccineDose.objects.filter(
        scheduled_date__gte=local_today - timedelta(days=MISSED_REMINDER_LOOKBACK_DAYS),
        scheduled_date__lte=local_today,
        actual_date__isnull=True,
        actual_time__isnull=True,
        dose_status__in=('scheduled', 'rescheduled', 'missed'),
        schedule__bite_case__case_status__in=ACTIVE_CASE_STATUSES,
        schedule__bite_case__patient__is_archived=False,
    ).filter(
        Q(scheduled_date__lt=local_today) | Q(dose_status='missed'),
    ).filter(~Exists(blocked_events)).select_related(
        'schedule__bite_case__patient',
        'schedule__bite_case',
    ).order_by('-scheduled_date', 'schedule_id', 'pk')


def missed_sms_consent_ok(case):
    from pre_registrations.models import PreRegistration
    registrations = PreRegistration.objects.filter(converted_case=case)
    return (
        registrations.filter(consent_given=True).exists()
        and not registrations.filter(consent_given=False).exists()
    )


def missed_vaccination_doses(today=None, now=None):
    candidates = VaccineDose.objects.filter(
        scheduled_date__isnull=False,
        actual_date__isnull=True,
        dose_status__in=('scheduled', 'rescheduled', 'missed'),
        schedule__bite_case__case_status__in=ACTIVE_CASE_STATUSES,
        schedule__bite_case__patient__is_archived=False,
    ).select_related(
        'schedule__bite_case__patient',
        'schedule__bite_case',
    ).order_by('schedule_id', 'pk')
    return [
        dose for dose in candidates
        if missed_appointment_is_due(dose, today=today, now=now)
    ]


def missed_dose_queryset(today=None, now=None):
    """Overdue unadministered doses on active cases; one row per dose, no SMS joins."""
    local_today = today or _local_now(now).date()
    return VaccineDose.objects.filter(
        scheduled_date__isnull=False,
        scheduled_date__lt=local_today,
        actual_date__isnull=True,
        dose_status__in=('scheduled', 'rescheduled', 'missed'),
        schedule__bite_case__case_status__in=ACTIVE_CASE_STATUSES,
        schedule__bite_case__patient__is_archived=False,
    )


def missed_dose_count(today=None, now=None):
    """Total individual missed doses behind the Nurse sidebar badge."""
    return missed_dose_queryset(today=today, now=now).count()


def missed_dose_counts_by_patient(today=None, now=None):
    """Per-patient missed-dose counts for the Vaccination Records list."""
    rows = (
        missed_dose_queryset(today=today, now=now)
        .values('schedule__bite_case__patient_id')
        .annotate(total=Count('pk'))
    )
    return {row['schedule__bite_case__patient_id']: row['total'] for row in rows}


def missed_reminder_message(dose):
    return build_missed_reminder_message(dose)


def _unsubmitted_pending(log):
    return (
        log.status == 'pending' and log.attempt_count == 0
        and not log.semaphore_message_id and not log.api_response.strip()
    )


def send_missed_appointment_reminder(dose_id, today=None, now=None, retry_failed=False,
                                     expected_scheduled_date=None):
    local_now = _local_now(now)
    local_today = today or local_now.date()
    # Commit the claim before HTTP: a worker crash must never erase a possible send.
    with transaction.atomic(durable=True):
        dose = VaccineDose.objects.select_for_update().select_related(
            'schedule__bite_case__patient',
        ).get(pk=dose_id)
        if (
            not missed_reminder_is_due(dose, today=local_today, now=local_now)
            or (expected_scheduled_date is not None and dose.scheduled_date != expected_scheduled_date)
        ):
            return None, {'success': False, 'skipped': True, 'error': 'Appointment changed or is outside the recent missed-reminder window'}

        patient = dose.schedule.bite_case.patient
        case = dose.schedule.bite_case
        event_key = (
            f'{MISSED_REMINDER_TYPE}:{patient.pk}:{case.pk}:'
            f'{dose.pk}:{dose.scheduled_date.isoformat()}'
        )
        legacy_logs = list(SMSLog.objects.select_for_update().filter(
            dose=dose, reminder_type=MISSED_REMINDER_TYPE,
            scheduled_date=dose.scheduled_date, event_key__isnull=True,
            status__in=('sent', 'queued', 'pending', 'unknown'),
        ))
        if legacy_logs:
            legacy = legacy_logs[0]
            if (
                len(legacy_logs) != 1 or not _unsubmitted_pending(legacy)
                or legacy.patient_id not in (None, patient.pk)
                or legacy.bite_case_id not in (None, case.pk)
                or SMSLog.objects.filter(event_key=event_key).exists()
            ):
                return legacy, {'success': False, 'skipped': True, 'error': 'Existing event requires no resend or operator review'}
            log, created = legacy, False
            log.event_key = event_key
            log.patient, log.bite_case = patient, case
            log.save(update_fields=['event_key', 'patient', 'bite_case', 'updated_at'])
        else:
            log, created = SMSLog.objects.get_or_create(
                event_key=event_key,
                defaults={
                    'dose': dose, 'patient': patient, 'bite_case': case,
                    'reminder_type': MISSED_REMINDER_TYPE,
                    'recipient': patient.contact_number or '',
                    'message': missed_reminder_message(dose),
                    'scheduled_date': dose.scheduled_date, 'status': 'queued',
                },
            )
        try:
            history = json.loads(log.api_response) if log.api_response else []
        except (TypeError, ValueError):
            history = []
        if not isinstance(history, list):
            history = []
        if not created:
            can_submit = _unsubmitted_pending(log) or log.status == 'skipped' or (
                log.status == 'queued' and log.attempt_count == 0
                and log.semaphore_message_id in ('', '0')
                and getattr(settings, 'SEMAPHORE_ENABLED', False)
            )
            if log.status == 'failed' and retry_failed:
                can_submit = (
                    bool(history) and isinstance(history[-1], dict)
                    and history[-1].get('retryable') is True
                    and local_now - log.sent_at >= timedelta(hours=1)
                    and log.attempt_count < MISSED_REMINDER_MAX_ATTEMPTS
                )
            if not can_submit:
                return log, {'success': False, 'skipped': True, 'error': 'Reminder already processed; retry blocked'}

        recipient = normalize_ph_number(patient.contact_number)
        reason = ''
        if not missed_sms_consent_ok(case):
            reason = 'SMS consent not recorded or not given'
        elif not recipient:
            reason = 'The patient primary contact number is invalid'
        log.recipient = recipient or (patient.contact_number or '')
        log.message = missed_reminder_message(dose)
        log.sent_at = local_now
        if reason:
            if log.status == 'skipped' and history and history[-1].get('error') == reason:
                return log, {'success': False, 'skipped': True, 'error': reason}
            log.status = 'skipped'
            log.api_response = json.dumps(history + [{
                'at': local_now.isoformat(), 'status': 'skipped', 'error': reason,
            }])
            log.save()
            return log, {'success': False, 'skipped': True, 'error': reason}

        log.status = 'unknown'
        log.attempt_count += 1
        attempt = {
            'attempt': log.attempt_count, 'at': local_now.isoformat(),
            'status': 'unknown', 'retryable': False,
            'error': 'Submission outcome not yet confirmed; do not retry',
        }
        history.append(attempt)
        log.api_response = json.dumps(history)
        log.save()

    current = VaccineDose.objects.select_related('schedule__bite_case__patient').get(pk=dose_id)
    if (
        current.scheduled_date != log.scheduled_date
        or not missed_reminder_is_due(current, today=local_today, now=local_now)
        or not missed_sms_consent_ok(current.schedule.bite_case)
        or normalize_ph_number(current.schedule.bite_case.patient.contact_number) != recipient
    ):
        result = {'success': False, 'skipped': True, 'error': 'Appointment or consent/contact changed before submission'}
        log.status = 'skipped'
        log.attempt_count -= 1
    else:
        try:
            result = safe_gateway_response(send_semaphore_sms(recipient, log.message))
        except Exception:
            result = {
                'success': False, 'uncertain': True,
                'error': 'Gateway outcome is unknown; automatic retry blocked',
            }
        log.status = gateway_outcome(result)
        if result.get('simulated'):
            log.attempt_count -= 1
        result['success'] = log.status == 'sent'
        if log.status == 'unknown':
            result['error'] = 'Gateway outcome is unknown; automatic retry blocked'

    response = result.get('response')
    if isinstance(response, list) and response and isinstance(response[0], dict):
        log.semaphore_message_id = str(response[0].get('message_id') or '')
    attempt.update({
        'attempt': log.attempt_count, 'status': log.status,
        'response': response, 'error': result.get('error'),
        'retryable': log.status == 'failed' and result.get('retryable') is True,
    })
    log.api_response = json.dumps(history, ensure_ascii=False, default=str)
    log.save(update_fields=(
        'status', 'attempt_count', 'semaphore_message_id', 'api_response', 'updated_at',
    ))
    return log, result
