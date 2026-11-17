from settings_app.models import SystemSetting

def settings_context(request):
    settings_dict = {}
    try:
        for s in SystemSetting.objects.all():
            settings_dict[s.key] = s.value
    except Exception:
        pass
    return {"system_settings": settings_dict}


def admin_sidebar_notifications(request):
    """Admin sidebar badges: pending online registrations and New Case bite cases."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated and user.role == 'admin'):
        return {}
    from bite_cases.models import AnimalBiteCase
    from pre_registrations.models import PreRegistration
    from pre_registrations.services import PENDING_STATUSES

    return {
        'pending_online_registration_count': PreRegistration.objects.filter(
            status__in=PENDING_STATUSES, is_archived=False,
            converted_patient__isnull=True, converted_case__isnull=True,
        ).count(),
        'new_bite_case_count': AnimalBiteCase.objects.filter(case_status='new').count(),
    }


def doctor_notifications(request):
    """Real counts behind the Doctor Portal bell: unclaimed cases and my drafts."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated and getattr(user, 'role', '') == 'doctor'):
        return {}
    from bite_cases.models import AnimalBiteCase
    from doctor.models import ClinicalAssessment
    from doctor.queue import pending_assessment_count

    queue_count = AnimalBiteCase.objects.filter(clinical_assessment__isnull=True).count()
    draft_count = ClinicalAssessment.objects.filter(assessed_by=user, status='draft').count()
    return {
        'bell_queue': queue_count,
        'bell_drafts': draft_count,
        'bell_total': queue_count + draft_count,
        'pending_clinical_count': pending_assessment_count(user),
        'doctor_new_case_count': AnimalBiteCase.objects.filter(case_status='new').count(),
    }


def inventory_notifications(request):
    """Inventory alerts behind the Nurse Portal bell (vaccines + medicines)."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated and getattr(user, 'role', '') == 'nurse'):
        return {}
    from inventory.notifications import get_inventory_alerts

    alerts = get_inventory_alerts()
    return {
        'inventory_alerts': alerts,
        'inventory_alert_total': len(alerts),
    }


def missed_vaccination_notifications(request):
    """Red Administer Vaccine badge: overdue unadministered dose count (Nurse)."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated and getattr(user, 'role', '') == 'nurse'):
        return {}
    from vaccination.missed_appointments import missed_dose_count

    return {'missed_dose_total': missed_dose_count()}
