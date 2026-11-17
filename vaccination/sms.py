"""
Semaphore SMS integration for vaccination reminders.
Sender Name: RHUDumingag (configurable via SEMAPHORE_SENDER_NAME)
Docs: https://semaphore.co/docs  POST https://api.semaphore.co/api/v4/messages
Params: apikey, number (comma-separated, 63...), message, sendername
"""
import logging
import re
import json
import requests
from django.conf import settings
from utils.phone import normalize_to_63 as _normalize_to_63

logger = logging.getLogger(__name__)

SEMAPHORE_API_URL = getattr(settings, "SEMAPHORE_API_URL", "https://api.semaphore.co/api/v4/messages")


def normalize_ph_number(raw: str) -> str | None:
    """
    Wrapper for utils.phone.normalize_to_63 for backward compat.
    Semaphore expects 639XXXXXXXXX.
    """
    res = _normalize_to_63(raw)
    if not res:
        logger.warning(f"Invalid PH number after normalize: raw={raw!r}")
    return res


def build_missed_reminder_message(dose) -> str:
    """Name the missed appointment and its saved date, without patient details."""
    return (
        'RHU Dumingag ABTC: You missed your scheduled '
        f'{dose.number_label} on {dose.scheduled_date:%B %d, %Y}. '
        'Please visit or contact RHU Dumingag as soon as possible to arrange '
        'your next vaccination appointment. Thank you.'
    )


def build_reminder_message(patient, case, dose, reminder_type: str, custom_note: str = "") -> str:
    """
    Build Filipino/English friendly SMS for vaccination.
    reminder_type: automatic ('3days_before', 'on_day', 'schedule_set') or
    manual ('manual_2days_before', 'manual_1day_before', 'manual_custom').
    Keep under 160 chars per segment if possible, but Semaphore auto-splits.
    """
    if reminder_type == "missed_appointment" or dose.dose_status == "missed":
        return build_missed_reminder_message(dose)

    patient_name = getattr(patient, "full_name", lambda: str(patient))()
    if callable(patient_name):
        patient_name = patient_name
    else:
        # fallback if full_name is method
        try:
            patient_name = patient.full_name()
        except Exception:
            patient_name = f"{patient.first_name} {patient.last_name}"

    dose_label = dose.dose_label
    display_dose = getattr(dose, "number_label", None) or dose_label
    # Use the dose's actual position in its plan, not the protocol day offset.
    # For example, 'Day 3' is normally Dose 2, not Dose 3.
    ordinal = re.fullmatch(r"(\d+)(?:st|nd|rd|th) Dose", display_dose)
    scheduled_dose = f"Dose {ordinal.group(1)}" if ordinal else display_dose
    sched_date = dose.scheduled_date.strftime("%b %d, %Y (%a)")
    case_no = case.case_number

    # Short, not starting with TEST (Semaphore ignores)
    if reminder_type == "schedule_set":
        return (
            f"ABTC Dumingag: {patient_name} ({case_no}), naitala ang vaccine schedule mo sa "
            f"{sched_date} ({scheduled_dose}). Pumunta sa RHU sa araw na ito. Dalhin ang card. Salamat! -ABTC Dumingag"
        ).strip()
    elif reminder_type == "3days_before":
        return (
            f"ABTC Dumingag: Paalala, {patient_name} ({case_no}) may vaccine schedule sa {sched_date} "
            f"({scheduled_dose}). Mangyaring pumunta sa RHU 3 araw mula ngayon. Dalhin ang card. Salamat! -ABTC Dumingag"
        ).strip()
    elif reminder_type == "on_day":
        return (
            f"ABTC Dumingag: Ngayon na ang vaccine schedule ni {patient_name} ({case_no}) - {scheduled_dose} "
            f"ngayong {sched_date}. Pumunta sa RHU ngayong araw. Dalhin ang vaccination card. -ABTC Dumingag"
        ).strip()
    elif reminder_type == "manual_immediate":
        return (
            f"ABTC Dumingag: Paalala, {patient_name} ({case_no}) - {display_dose} sa {sched_date}. "
            f"Pumunta sa RHU sa nakatakdang petsa. Dalhin ang card. -ABTC Dumingag"
        ).strip()
    elif reminder_type == "manual_2days_before":
        return (
            f"ABTC Dumingag: Paalala, {patient_name} ({case_no}) - {display_dose} sa {sched_date}. "
            f"Pumunta sa RHU sa loob ng 2 araw. Dalhin ang card. -ABTC Dumingag"
        ).strip()
    elif reminder_type == "manual_1day_before":
        return (
            f"ABTC Dumingag: Paalala, {patient_name} ({case_no}) - {display_dose} ay bukas na, {sched_date}. "
            f"Pumunta sa RHU sa nakatakdang oras. Dalhin ang card. -ABTC Dumingag"
        ).strip()
    elif reminder_type == "manual_custom":
        note = (custom_note or "").strip() or "Pumunta sa RHU sa nakatakdang petsa."
        return (
            f"ABTC Dumingag: Paalala para kay {patient_name} ({case_no}) - {display_dose} sa {sched_date}. "
            f"{note} -ABTC Dumingag"
        ).strip()
    else:
        return (
            f"ABTC Dumingag: Paalala para kay {patient_name} ({case_no}) - {dose_label} sa {sched_date}. "
            f"Pumunta sa RHU. -ABTC Dumingag"
        ).strip()


def safe_gateway_response(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    key = getattr(settings, 'SEMAPHORE_API_KEY', '')
    if key:
        text = text.replace(json.dumps(key)[1:-1], '[hidden]')
    text = re.sub(
        r'(?i)(api[ _-]?key|token|secret|password|bearer)(["\s:=]+)([^"\s,}]+)',
        r'\1\2[hidden]', text,
    )
    return json.loads(text)


def gateway_outcome(result):
    if result.get('simulated'):
        return 'queued'
    if result.get('uncertain'):
        return 'unknown'
    if not result.get('success'):
        return 'failed'
    response = result.get('response')
    if isinstance(response, list) and response and isinstance(response[0], dict):
        item = response[0]
        status = str(item.get('status', '')).casefold()
        if status in ('failed', 'rejected', 'refunded', 'error'):
            return 'failed'
        if item.get('message_id') and str(item['message_id']) != '0' and status in ('queued', 'pending', 'sent', 'delivered'):
            return 'sent'
    return 'unknown'


def send_semaphore_sms(number: str, message: str, sendername: str | None = None) -> dict:
    """
    Low-level Semaphore call.
    Returns dict with keys: success (bool), response (json/list), error (str), raw (requests.Response)
    """
    # Guard: do not start with TEST
    if message.lstrip().upper().startswith("TEST"):
        message = " " + message  # prefix space to avoid silent ignore

    normalized = normalize_ph_number(number)
    if not normalized:
        return {"success": False, "error": f"Invalid PH number: {number}", "response": None}

    sender = sendername or getattr(settings, "SEMAPHORE_SENDER_NAME", "RHUDumingag") or "RHUDumingag"
    api_url = getattr(settings, "SEMAPHORE_API_URL", SEMAPHORE_API_URL)

    # If disabled, simulate success (useful for dev / no credits) — don't require API key
    if not getattr(settings, "SEMAPHORE_ENABLED", False):
        logger.info(f"[SEMAPHORE DISABLED] Would send to {normalized} via {sender}: {message[:80]}...")
        # Simulate queued response like real API
        simulated = [{
            "message_id": 0,
            "recipient": normalized,
            "message": message,
            "sender_name": sender,
            "status": "Queued (simulated - SEMAPHORE_ENABLED=False)",
            "type": "Single",
        }]
        return {"success": True, "response": simulated, "error": None, "simulated": True, "normalized": normalized}

    apikey = getattr(settings, "SEMAPHORE_API_KEY", "")
    if not apikey:
        return {"success": False, "error": "SMS gateway credentials are not configured", "response": None, "retryable": True}

    payload = {
        "apikey": apikey,
        "number": normalized,
        "message": message,
        "sendername": sender,
    }

    try:
        resp = requests.post(api_url, data=payload, timeout=15, allow_redirects=False)
        try:
            data = safe_gateway_response(resp.json())
        except ValueError:
            data = None

        if resp.status_code in (200, 201):
            result = {
                'success': True, 'response': data, 'error': None,
                'status_code': resp.status_code, 'normalized': normalized,
            }
            outcome = gateway_outcome(result)
            result['success'] = outcome == 'sent'
            result['uncertain'] = outcome == 'unknown'
            result['retryable'] = outcome == 'failed'
            if outcome != 'sent':
                result['error'] = (
                    'Gateway rejected the message' if outcome == 'failed'
                    else 'Gateway acceptance could not be confirmed; review before any resend'
                )
            return result
        rejected = 400 <= resp.status_code < 500 and resp.status_code != 408
        logger.warning('SMS gateway returned HTTP %s', resp.status_code)
        return {
            'success': False, 'error': f'SMS gateway HTTP {resp.status_code}',
            'response': data, 'status_code': resp.status_code,
            'uncertain': not rejected, 'retryable': rejected,
        }
    except requests.RequestException:
        logger.warning('SMS gateway request outcome is unconfirmed; automatic retry blocked')
        return {
            'success': False, 'error': 'Gateway request outcome is unknown; review before any resend',
            'response': None, 'uncertain': True, 'retryable': False,
        }


def send_dose_reminder(dose, reminder_type: str = "3days_before", dry_run: bool = False, force: bool = False, custom_note: str = ""):
    """
    High-level helper to send SMS for a specific dose.
    Handles logging via SMSLog and duplicate guard.

    Returns (log_instance or None, result_dict)
    """
    from .models import SMSLog

    schedule = dose.schedule
    case = schedule.bite_case
    patient = case.patient

    # Build candidate numbers: primary contact, then emergency, then account mobile (if patient has account)
    candidates = [
        patient.contact_number,
        getattr(patient, "emergency_contact_number", ""),
        getattr(getattr(patient, "account", None), "mobile", "") if getattr(patient, "account", None) else "",
    ]
    # Pick first valid PH number
    number = None
    for cand in candidates:
        if cand and normalize_ph_number(cand):
            number = cand
            break
    if not number:
        # Fallback to primary even if invalid (will fail gracefully)
        number = patient.contact_number or (candidates[1] if len(candidates) > 1 else "")
    message = build_reminder_message(patient, case, dose, reminder_type, custom_note=custom_note)

    # Duplicate guard: don't re-send same dose + reminder_type + scheduled_date on same day unless force
    from django.utils import timezone
    today = timezone.localdate()
    existing = SMSLog.objects.filter(
        dose=dose,
        reminder_type=reminder_type,
        scheduled_date=dose.scheduled_date,
    ).filter(status__in=["sent", "queued", "pending", "unknown"]).first() if not force and not dry_run else None
    # Alternative: prevent duplicate today
    if existing and not force and not dry_run:
        # Compare the Manila calendar day, not the UTC one
        if existing.sent_at:
            sent_local = (
                timezone.localtime(existing.sent_at)
                if timezone.is_aware(existing.sent_at)
                else existing.sent_at
            )
            if sent_local.date() == today:
                return existing, {"success": False, "error": "Already sent today", "response": None, "skipped": True}

    if dry_run:
        # Create a simulated log without calling API
        log = SMSLog(
            dose=dose,
            patient=patient,
            bite_case=case,
            reminder_type=reminder_type,
            recipient=normalize_ph_number(number) or number,
            message=message,
            scheduled_date=dose.scheduled_date,
            status="pending",
            api_response={"dry_run": True},
        )
        # Don't save yet; caller may save or not
        result = {"success": True, "response": [{"status": "DryRun"}], "dry_run": True}
        return log, result

    result = send_semaphore_sms(number, message)

    # Prepare log data
    normalized = result.get("normalized") or normalize_ph_number(number) or number
    status = gateway_outcome(result)
    api_resp = safe_gateway_response(result.get("response"))
    message_id = None
    if isinstance(api_resp, list) and api_resp and isinstance(api_resp[0], dict):
        message_id = api_resp[0].get("message_id")

    # Save log
    log = SMSLog.objects.create(
        dose=dose,
        patient=patient,
        bite_case=case,
        reminder_type=reminder_type,
        recipient=normalized,
        message=message,
        scheduled_date=dose.scheduled_date,
        status=status,
        semaphore_message_id=str(message_id) if message_id else "",
        api_response=str(api_resp)[:4000] if api_resp is not None else result.get("error", ""),
    )
    return log, result
