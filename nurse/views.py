from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from functools import wraps


def is_nurse_user(user):
    """RBAC: only users with role == 'nurse' may use the Nurse Portal."""
    return user.is_authenticated and getattr(user, 'role', '') == 'nurse'


def nurse_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.urls import reverse
            from urllib.parse import quote
            login_url = reverse('accounts:login')
            return redirect(f"{login_url}?next={quote(request.get_full_path())}")
        if getattr(request.user, 'role', '') != 'nurse':
            messages.error(request, 'Not authorized — Nurse Portal is restricted to Nurse role.')
            return redirect('dashboard:index')
        return view_func(request, *args, **kwargs)
    return _wrapped


@login_required
@nurse_required
def dashboard(request):
    """Nurse Portal hub — the 8 nurse use cases in one simple screen."""
    from vaccination.models import SMSLog, VaccineDose
    from inventory.models import VaccineInventory
    from bite_cases.models import AnimalBiteCase

    today = date.today()

    due_today = VaccineDose.objects.filter(
        dose_status='scheduled', scheduled_date=today
    ).select_related('schedule__bite_case__patient').order_by('schedule__bite_case__case_number')[:10]
    due_today_count = VaccineDose.objects.filter(
        dose_status='scheduled', scheduled_date=today
    ).count()
    missed_count = VaccineDose.objects.filter(dose_status='missed').count()
    pending_scheduling_count = VaccineDose.objects.filter(dose_status='pending').count()
    upcoming_count = VaccineDose.objects.filter(
        dose_status='scheduled', scheduled_date__gt=today
    ).count()
    sms_pending = SMSLog.objects.filter(status__in=['pending', 'queued']).count()
    sms_failed = SMSLog.objects.filter(status='failed').count()
    low_stock = VaccineInventory.objects.filter(quantity_available__lte=5).count()

    # Cases with no vaccination schedule yet — the nurse sets these.
    awaiting_schedule = (
        AnimalBiteCase.objects
        .filter(vaccination_schedule__isnull=True)
        .select_related('patient')
        .order_by('-created_at')[:10]
    )
    awaiting_schedule_count = AnimalBiteCase.objects.filter(
        vaccination_schedule__isnull=True
    ).count()

    # Chart 1: Dose status breakdown
    administered_count = VaccineDose.objects.filter(dose_status='completed').count()
    dose_status_chart = {
        'labels': ['Administered', 'Due Today', 'Upcoming', 'Pending Scheduling', 'Missed'],
        'counts': [
            administered_count,
            due_today_count,
            upcoming_count,
            pending_scheduling_count,
            missed_count,
        ],
    }

    # Chart 2: Doses administered over the last 7 days
    last_7_days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    admin_by_day = {
        row['actual_date']: row['total']
        for row in (
            VaccineDose.objects
            .filter(dose_status='completed', actual_date__range=(last_7_days[0], today))
            .values('actual_date')
            .annotate(total=Count('id'))
        )
    }
    daily_administered_chart = {
        'labels': [d.strftime('%b %d') for d in last_7_days],
        'counts': [admin_by_day.get(d, 0) for d in last_7_days],
    }

    context = {
        'due_today': due_today,
        'due_today_count': due_today_count,
        'missed_count': missed_count,
        'pending_scheduling_count': pending_scheduling_count,
        'upcoming_count': upcoming_count,
        'administered_count': administered_count,
        'sms_pending': sms_pending,
        'sms_failed': sms_failed,
        'low_stock': low_stock,
        'today': today,
        'awaiting_schedule': awaiting_schedule,
        'awaiting_schedule_count': awaiting_schedule_count,
        'dose_status_chart': dose_status_chart,
        'daily_administered_chart': daily_administered_chart,
    }
    return render(request, 'nurse/dashboard.html', context)


def _finalized_doctor_assessments():
    """Assessments the Nurse may review, excluding drafts and incomplete data."""
    from doctor.models import ClinicalAssessment

    return (
        ClinicalAssessment.objects.filter(
            bite_case__isnull=False,
            assessed_by__role='doctor',
            is_finalized=True,
            status='final',
            finalized_at__isnull=False,
        )
        .exclude(chief_complaint='')
        .exclude(exposure_narrative='')
        .exclude(category_confirmed='')
        .exclude(treatment_plan='')
        .exclude(bite_case__case_status='archived')
        .select_related(
            'patient__barangay', 'bite_case', 'assessed_by',
        )
        .order_by('-finalized_at', '-pk')
    )


@login_required
@nurse_required
@require_GET
def doctor_finding_list(request):
    from django.db.models import Q

    assessments = _finalized_doctor_assessments()
    q = request.GET.get('q', '').strip()
    if q:
        assessments = assessments.filter(
            Q(patient__first_name__icontains=q)
            | Q(patient__last_name__icontains=q)
            | Q(patient__patient_number__icontains=q)
            | Q(bite_case__case_number__icontains=q)
            | Q(category_confirmed__icontains=q)
        )
    page_obj = Paginator(assessments, 20).get_page(request.GET.get('page'))
    return render(request, 'nurse/doctor_findings.html', {
        'assessments': page_obj,
        'page_obj': page_obj,
        'q': q,
    })


@login_required
@nurse_required
@require_GET
def doctor_finding_detail(request, case_pk):
    """Nurse Portal: one finalized Doctor assessment, strictly read-only.

    Uses the same Assessment Details design as Doctor Portal's My Assessments.
    """
    from django.urls import reverse
    from doctor.models import ClinicalAssessment

    assessment = get_object_or_404(
        _finalized_doctor_assessments().select_related(
            'patient__barangay', 'patient__created_by', 'bite_case',
            'assessed_by', 'vaccine_inventory',
        ),
        bite_case_id=case_pk,
    )
    bite_case = assessment.bite_case
    if bite_case.is_archived:
        messages.error(
            request,
            'This bite case has been archived and is available to the System Administrator only.',
        )
        return redirect('nurse:doctor_finding_list')

    patient = assessment.patient
    schedule = getattr(bite_case, 'vaccination_schedule', None)
    doses = list(schedule.doses.all()) if schedule else []

    first_dose_date = doses[0].scheduled_date if doses else None
    if first_dose_date is None and doses:
        first_dose_date = doses[0].actual_date
    if first_dose_date is None:
        first_dose_date = assessment.first_dose_date

    dose_count = len(doses)
    if not dose_count and assessment.dose_protocol:
        dose_count = len(ClinicalAssessment.protocol_days(assessment.dose_protocol))
    if dose_count:
        dose_schedule_label = f'{dose_count} Dose{"" if dose_count == 1 else "s"}'
    elif assessment.dose_schedule_plan:
        dose_schedule_label = assessment.dose_schedule_plan
    else:
        dose_schedule_label = ''

    return render(request, 'doctor/assessment_detail.html', {
        'assessment': assessment,
        'patient': patient,
        'bite_case': bite_case,
        'latest_vitals': patient.get_latest_vitals(),
        'schedule': schedule,
        'doses': doses,
        'first_dose_date': first_dose_date,
        'dose_schedule_label': dose_schedule_label,
        'prescriptions': assessment.prescriptions.prefetch_related('medicine').order_by('id'),
        'nurse_view': True,
        'portal_label': 'Nurse Portal',
        'back_url': reverse('nurse:doctor_finding_list'),
        'back_label': 'Back to Doctor Findings',
    })
