from functools import wraps
from datetime import date, datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import Lower, Trim
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from patients.models import Patient
import csv
import io
from bite_cases.models import AnimalBiteCase
from vaccination.models import VaccineDose
from vaccination.missed_appointments import missed_vaccination_doses
from inventory.models import MedicineInventory, VaccineInventory
from inventory.notifications import get_expiry_warning_days

from .pdf import build_official_pdf
from .services import (
    barangay_distribution, completed_schedule_case_ids,
    effective_status, grouped_medicine_products, grouped_totals,
    grouped_vaccine_products, inventory_summary, patient_treatment_summary,
    stock_alert_sections,
)

REPORTS_ROLES = {'admin', 'nurse', 'health_worker', 'encoder'}
RECORD_REPORT_PERIODS = (
    ('daily', 'Today'),
    ('weekly', 'This Week'),
    ('monthly', 'This Month'),
    ('all', 'All Time'),
)
SEARCH_FIELDS = {
    'all': 'All Fields',
    'patient_name': 'Patient Name',
    'patient_id': 'Patient ID',
    'sex': 'Sex',
    'case_number': 'Case Number',
    'barangay': 'Barangay',
    'animal': 'Animal',
    'treatment_status': 'Treatment Status',
}


def reports_access(view_func):
    """Reports are for authorized ABTC staff, never patients or doctors."""
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if getattr(request.user, 'role', '') not in REPORTS_ROLES:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped


# ---------- helpers ----------
SIMPLIFIED_PERIODS = ('daily', 'weekly', 'monthly', 'last7', 'last30', 'annual')


def _parse_dates(request, simplified=False):
    """Return start_date, end_date, report_type, preset label for filtering."""
    report_type = request.GET.get('type', 'custom')
    if simplified and report_type not in SIMPLIFIED_PERIODS:
        # Hidden filters (custom dates, stale values) never apply here.
        report_type = 'all'
    start_str = request.GET.get('start_date', '')
    end_str = request.GET.get('end_date', '')
    today = timezone.localdate()
    start = end = None
    label = ''
    if report_type == 'daily':
        start = end = today
        label = f"Daily — {today.strftime('%b %d, %Y')}"
    elif report_type == 'weekly':
        start = today - timedelta(days=today.weekday())
        end = today
        label = f"Weekly — {start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}"
    elif report_type == 'monthly':
        start = today.replace(day=1)
        end = today
        label = f"Monthly — {today.strftime('%B %Y')}"
    elif report_type == 'annual':
        start = today.replace(month=1, day=1)
        end = today
        label = f"Annual — {today.year}"
    elif report_type == 'last7':
        start = today - timedelta(days=6)
        end = today
        label = f"Last 7 days — {start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}"
    elif report_type == 'last30':
        start = today - timedelta(days=29)
        end = today
        label = f"Last 30 days — {start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}"
    elif report_type == 'all':
        label = "All time"
    else:  # custom
        report_type = 'custom'
        try:
            if start_str and end_str:
                start = datetime.strptime(start_str, '%Y-%m-%d').date()
                end = datetime.strptime(end_str, '%Y-%m-%d').date()
                label = f"Custom — {start.strftime('%b %d, %Y')} to {end.strftime('%b %d, %Y')}"
            elif start_str:
                start = datetime.strptime(start_str, '%Y-%m-%d').date()
                end = today
                label = f"From {start.strftime('%b %d, %Y')} to {today.strftime('%b %d, %Y')}"
            else:
                report_type = 'all'
                label = "All time"
        except ValueError:
            report_type = 'all'
            label = "All time"
    return start, end, report_type, label

def _filter_cases(qs, request, start, end, extra_filters=True):
    q = request.GET.get('q', '').strip()
    search_field = _search_field(request)
    if extra_filters:
        barangay = request.GET.get('barangay', '')
        animal = request.GET.get('animal', '')
        status = request.GET.get('status', '')
        exposure = request.GET.get('exposure', '')
    else:
        # Simplified Reports Center pages only honor the period and search.
        barangay = animal = status = exposure = ''
    # Case reports are period reports on the recorded bite incident date.
    # Use aware day bounds so the filter does not depend on DB timezone tables.
    tz = timezone.get_current_timezone()
    if start:
        qs = qs.filter(date_time_bite__gte=timezone.make_aware(
            datetime.combine(start, datetime.min.time()), tz))
    if end:
        qs = qs.filter(date_time_bite__lte=timezone.make_aware(
            datetime.combine(end, datetime.max.time()), tz))
    if barangay:
        qs = qs.filter(incident_barangay__icontains=barangay)
    if animal:
        qs = qs.filter(animal_type=animal)
    if status:
        qs = qs.filter(case_status=status)
    if exposure:
        qs = qs.filter(exposure_type=exposure)
    if q:
        qs = qs.filter(pk__in=_case_search_queryset(
            q, start, end, search_field).values('pk'))
    return qs


def _date_term(value):
    for date_format in ('%Y-%m-%d', '%m/%d/%Y', '%m-%d-%Y', '%b %d, %Y'):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def _period_case_filter(queryset, start, end):
    tz = timezone.get_current_timezone()
    if start:
        queryset = queryset.filter(date_time_bite__gte=timezone.make_aware(
            datetime.combine(start, datetime.min.time()), tz))
    if end:
        queryset = queryset.filter(date_time_bite__lte=timezone.make_aware(
            datetime.combine(end, datetime.max.time()), tz))
    return queryset


def _choice_value(choices, value):
    normalized = ' '.join(value.casefold().replace('_', ' ').split())
    return next((
        stored for stored, label in choices
        if normalized in {
            ' '.join(stored.casefold().replace('_', ' ').split()),
            ' '.join(label.casefold().replace('_', ' ').split()),
        }
    ), None)


def _search_field(request):
    field = request.GET.get('search_field', 'all').strip()
    return field if field in SEARCH_FIELDS else 'all'


def _name_query(query, prefix='patient__'):
    name_q = Q()
    for token in query.split():
        name_q &= (
            Q(**{f'{prefix}first_name__icontains': token})
            | Q(**{f'{prefix}middle_name__icontains': token})
            | Q(**{f'{prefix}last_name__icontains': token})
            | Q(**{f'{prefix}suffix__icontains': token})
        )
    return name_q


def _case_barangay_query(query):
    return (
        Q(_incident_barangay__iexact=query)
        | Q(_patient_barangay__iexact=query)
    )


def _effective_status_cases(cases, status):
    completed_ids = completed_schedule_case_ids()
    if status == 'completed':
        return cases.filter(
            Q(case_status='completed') | Q(pk__in=completed_ids)
        ).exclude(case_status='archived')
    if status == 'under_treatment':
        return cases.filter(case_status='under_treatment').exclude(
            pk__in=completed_ids)
    return cases.filter(case_status=status)


def _dose_search_queryset(query, start=None, end=None):
    if not query or _choice_value(Patient.SEX_CHOICES, query):
        return VaccineDose.objects.none()

    dose_q = (
        Q(dose_label__icontains=query)
        | Q(vaccine_brand__icontains=query)
        | Q(batch_number__icontains=query)
        | Q(remarks__icontains=query)
        | Q(schedule__other_medicine__icontains=query)
        | Q(schedule__doctor_notes__icontains=query)
        | Q(schedule__bite_case__case_number__icontains=query)
        | Q(schedule__bite_case__patient__patient_number__icontains=query)
    )
    name_q = Q()
    for token in query.split():
        name_q &= (
            Q(schedule__bite_case__patient__first_name__icontains=token)
            | Q(schedule__bite_case__patient__middle_name__icontains=token)
            | Q(schedule__bite_case__patient__last_name__icontains=token)
            | Q(schedule__bite_case__patient__suffix__icontains=token)
        )
    dose_q |= name_q
    status = _choice_value(VaccineDose.DOSE_STATUS, query)
    if status:
        dose_q |= Q(dose_status=status)
    case_status = _choice_value(AnimalBiteCase.CASE_STATUS, query)
    if case_status:
        dose_q |= Q(schedule__bite_case__case_status=case_status)
    date_value = _date_term(query)
    if date_value:
        dose_q |= (
            Q(scheduled_date=date_value)
            | Q(actual_date=date_value)
        )

    doses = VaccineDose.objects.filter(dose_q)
    event_q = Q()
    for field in ('scheduled_date', 'actual_date'):
        field_q = Q()
        if start:
            field_q &= Q(**{f'{field}__gte': start})
        if end:
            field_q &= Q(**{f'{field}__lte': end})
        if start or end:
            event_q |= field_q
    if start or end:
        doses = doses.filter(event_q)
    return doses.distinct()


def _case_search_queryset(query, start=None, end=None, search_field='all'):
    """Search related patient and case fields with exact category handling."""
    query = query.strip()
    if not query:
        return AnimalBiteCase.objects.none()

    cases = AnimalBiteCase.objects.all().annotate(
        _incident_barangay=Trim('incident_barangay'),
        _patient_barangay=Trim('patient__barangay__name'))
    normalized_query = ' '.join(query.split())

    field_filters = {
        'patient_name': _name_query(normalized_query),
        'patient_id': Q(patient__patient_number__icontains=query),
        'sex': Q(patient__sex__iexact=normalized_query),
        'case_number': Q(case_number__icontains=query),
        'animal': (
            Q(animal_type__icontains=query)
            | Q(animal_other__icontains=query)
        ),
    }

    if search_field == 'barangay':
        exact_barangay = _period_case_filter(
            cases.filter(_case_barangay_query(normalized_query)), start, end)
        if exact_barangay.exists():
            return exact_barangay.distinct()
        return _period_case_filter(
            cases.filter(
                Q(_incident_barangay__icontains=query)
                | Q(_patient_barangay__icontains=query)),
            start, end).distinct()

    if search_field in field_filters:
        return _period_case_filter(
            cases.filter(field_filters[search_field]), start, end).distinct()

    if search_field == 'treatment_status':
        status = _choice_value(AnimalBiteCase.CASE_STATUS, normalized_query)
        if status:
            return _period_case_filter(
                _effective_status_cases(cases, status), start, end).distinct()
        return cases.none()

    sex = _choice_value(Patient.SEX_CHOICES, normalized_query)
    if sex:
        return _period_case_filter(
            cases.filter(patient__sex=sex), start, end).distinct()

    # A complete stored barangay name is a categorical match, not a substring
    # against unrelated address or incident text.
    exact_barangay = cases.filter(_case_barangay_query(normalized_query))
    exact_barangay = _period_case_filter(exact_barangay, start, end)
    if exact_barangay.exists():
        return exact_barangay.distinct()

    animal_type = _choice_value(AnimalBiteCase.ANIMAL_TYPES, normalized_query)
    if animal_type:
        return _period_case_filter(
            cases.filter(animal_type=animal_type), start, end).distinct()

    case_status = _choice_value(
        AnimalBiteCase.CASE_STATUS, normalized_query)
    if case_status:
        return _period_case_filter(
            _effective_status_cases(cases, case_status), start, end).distinct()

    search_q = (
        Q(case_number__icontains=query)
        | Q(patient__patient_number__icontains=query)
        | Q(patient__address__icontains=query)
        | Q(patient__contact_number__icontains=query)
        | Q(_patient_barangay__icontains=query)
        | Q(_incident_barangay__icontains=query)
        | Q(place_of_incident__icontains=query)
        | Q(animal_other__icontains=query)
        | Q(wound_description__icontains=query)
        | Q(first_aid_given__icontains=query)
        | Q(initial_consultation_notes__icontains=query)
        | _name_query(normalized_query)
    )

    birthdate = _date_term(query)
    if birthdate:
        search_q |= Q(patient__birthdate=birthdate)
        search_q |= Q(date_time_bite__date=birthdate)

    exposure_type = _choice_value(AnimalBiteCase.EXPOSURE_TYPE, normalized_query)
    animal_condition = _choice_value(
        AnimalBiteCase.ANIMAL_CONDITION, normalized_query)
    animal_vax_status = _choice_value(
        AnimalBiteCase.VAX_STATUS, normalized_query)
    if exposure_type:
        search_q |= Q(exposure_type=exposure_type)
    if animal_condition:
        search_q |= Q(animal_condition=animal_condition)
    if animal_vax_status:
        search_q |= Q(animal_vax_status=animal_vax_status)
    if case_status:
        search_q |= Q(case_status=case_status)
    category_term = ' '.join(normalized_query.casefold().replace('_', ' ').split())
    for prefix in ('who bite category ', 'who category '):
        if category_term.startswith(prefix):
            category_term = 'category ' + category_term[len(prefix):]
            break
    category = {
        'category i': 'category_i',
        'category ii': 'category_ii',
        'category iii': 'category_iii',
    }.get(category_term)
    if category:
        search_q |= Q(exposure__category=category)

    dose_ids = _dose_search_queryset(query, start, end).values(
        'schedule__bite_case_id')

    case_matches = _period_case_filter(cases.filter(search_q), start, end)
    return (case_matches | cases.filter(pk__in=dose_ids)).distinct()


def _inventory_search_query(query, today):
    from django.db.models import F

    search_q = (
        Q(vaccine_name__icontains=query)
        | Q(brand__icontains=query)
        | Q(batch_number__icontains=query)
        | Q(source_supplier__icontains=query)
    )
    medicine_q = (
        Q(medicine_name__icontains=query)
        | Q(generic_name__icontains=query)
        | Q(strength__icontains=query)
        | Q(batch_number__icontains=query)
        | Q(source_supplier__icontains=query)
    )
    status = ' '.join(query.casefold().split())
    if status == 'expired':
        search_q |= Q(expiration_date__lt=today)
        medicine_q |= Q(expiration_date__lt=today)
    elif status == 'out of stock':
        search_q |= Q(expiration_date__gte=today, quantity_available__lte=0)
        medicine_q |= Q(expiration_date__gte=today, quantity_available__lte=0)
    elif status == 'low stock':
        search_q |= Q(
            expiration_date__gte=today, quantity_available__gt=0,
            quantity_available__lte=F('reorder_level'))
        medicine_q |= Q(
            expiration_date__gte=today, quantity_available__gt=0,
            quantity_available__lte=F('reorder_level'))
    elif status == 'available':
        search_q |= Q(
            expiration_date__gte=today,
            quantity_available__gt=F('reorder_level'))
        medicine_q |= Q(
            expiration_date__gte=today,
            quantity_available__gt=F('reorder_level'))
    expiry_date = _date_term(query)
    if expiry_date:
        search_q |= Q(expiration_date=expiry_date)
        medicine_q |= Q(expiration_date=expiry_date)
    return search_q, medicine_q


def _animal_label(bite_case):
    label = bite_case.get_animal_type_display()
    if bite_case.animal_type == 'other' and bite_case.animal_other:
        return f'{label} ({bite_case.animal_other})'
    return label


SEARCH_RESULT_HEADERS = [
    'Patient', 'Patient ID', 'Sex', 'Case Number', 'Barangay', 'Animal',
    'Treatment Status',
]


def _search_result_barangay(bite_case, query='', search_field='all'):
    patient_barangay = (
        bite_case.patient.barangay.name if bite_case.patient.barangay else '')
    if (
        query and patient_barangay
        and search_field in ('all', 'barangay')
        and query.casefold() in patient_barangay.casefold()
    ):
        return _barangay_label(patient_barangay)
    return _barangay_label(
        bite_case.incident_barangay or patient_barangay)


def _search_result_row(
        bite_case, completed_ids, query='', search_field='all'):
    return [
        bite_case.patient.full_name(),
        bite_case.patient.patient_number,
        bite_case.patient.get_sex_display(),
        bite_case.case_number,
        _search_result_barangay(bite_case, query, search_field),
        _animal_label(bite_case),
        effective_status(bite_case, completed_ids),
    ]


def _barangay_label(value):
    return (value or '').strip() or 'Unspecified'


def _csv_response(filename, headers, rows):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    writer = csv.writer(resp)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return resp


def _case_filter_summary(request, label, extra_filters=True):
    lines = [("Period", label)]
    keys = (('q', 'Search'),)
    search_field = _search_field(request)
    if search_field != 'all' and request.GET.get('q', '').strip():
        lines.append(('Search field', SEARCH_FIELDS[search_field]))
    if extra_filters:
        keys = (
            ('barangay', 'Barangay'),
            ('animal', 'Animal'),
            ('status', 'Status'),
            ('exposure', 'Exposure'),
        ) + keys
    for key, title in keys:
        value = request.GET.get(key, '').strip()
        if value:
            lines.append((title, value))
    if len(lines) == 1:
        lines.append(("Filters", "None"))
    return lines


def _export_period(start, end):
    return (
        start.isoformat() if start else 'all',
        end.isoformat() if end else 'all',
    )


def _pdf_response(pdf_bytes, filename, inline=False):
    resp = HttpResponse(pdf_bytes, content_type='application/pdf')
    disp = 'inline' if inline else 'attachment'
    resp['Content-Disposition'] = f'{disp}; filename="{filename}"'
    # allow iframe preview on same origin (fixes 127.0.0.1 refused to connect)
    resp['X-Frame-Options'] = 'SAMEORIGIN'
    # CSP frame-ancestors if present
    return resp

def _build_pdf(title, subtitle, headers, rows, summary_lines=None, landscape=False):
    """Build every report PDF with the shared official A4 portrait template.

    `landscape` is accepted for backwards compatibility and intentionally ignored:
    all ABTC documents print portrait.
    """
    return build_official_pdf(title, subtitle, headers, rows, summary_lines)


def _render_report_print(
        request, title, meta, headers, rows, summary_lines=None,
        summary_at_bottom=False):
    """Render the official HTML print document for a report (browser Print)."""
    return render(request, 'reports/print_official.html', {
        'document_title': title,
        'document_meta': meta,
        'summary_lines': summary_lines or [],
        'headers': headers,
        'rows': rows,
        'printed_at': timezone.localtime(),
        'summary_at_bottom': summary_at_bottom,
    })


def _record_report_period(request, queryset, date_field):
    """Apply a report period to a timestamp field and return its display label."""
    start, end, report_type, label = _parse_dates(request, simplified=True)
    tz = timezone.get_current_timezone()
    if start:
        queryset = queryset.filter(**{
            f'{date_field}__gte': timezone.make_aware(
                datetime.combine(start, datetime.min.time()), tz),
        })
    if end:
        queryset = queryset.filter(**{
            f'{date_field}__lte': timezone.make_aware(
                datetime.combine(end, datetime.max.time()), tz),
        })
    return queryset, report_type, label


def _patient_records_query(request):
    patients, report_type, label = _record_report_period(
        request, Patient.objects.select_related('barangay'),
        'created_at')
    query = request.GET.get('q', '').strip()
    if query:
        normalized_query = ' '.join(query.split())
        sex = _choice_value(Patient.SEX_CHOICES, normalized_query)
        if sex:
            patients = patients.filter(sex=sex)
        else:
            patients = patients.filter(
                Q(patient_number__icontains=query)
                | Q(contact_number__icontains=query)
                | Q(address__icontains=query)
                | Q(barangay__name__icontains=query)
                | _name_query(normalized_query, prefix='')
            )
    return patients.distinct().order_by('last_name', 'first_name', 'pk'), report_type, label


def _bite_case_records_query(request):
    cases, report_type, label = _record_report_period(
        request, AnimalBiteCase.objects.select_related(
            'patient', 'patient__barangay'),
        'date_time_bite')
    query = request.GET.get('q', '').strip()
    if query:
        normalized_query = ' '.join(query.split())
        animal = _choice_value(AnimalBiteCase.ANIMAL_TYPES, normalized_query)
        exposure = _choice_value(AnimalBiteCase.EXPOSURE_TYPE, normalized_query)
        status = _choice_value(AnimalBiteCase.CASE_STATUS, normalized_query)
        query_filter = (
            Q(case_number__icontains=query)
            | Q(incident_barangay__icontains=query)
            | Q(patient__barangay__name__icontains=query)
            | Q(animal_other__icontains=query)
            | _name_query(normalized_query)
        )
        if animal:
            query_filter |= Q(animal_type=animal)
        else:
            query_filter |= Q(animal_type__icontains=query)
        if exposure:
            query_filter |= Q(exposure_type=exposure)
        else:
            query_filter |= Q(exposure_type__icontains=query)
        if status:
            query_filter |= Q(case_status=status)
        else:
            query_filter |= Q(case_status__icontains=query)
        cases = cases.filter(query_filter)
    return cases.order_by('-date_time_bite', '-pk'), report_type, label


@reports_access
def patients_report(request):
    patients, report_type, label = _patient_records_query(request)
    total = patients.count()
    export = request.GET.get('export')
    if export in ('print', 'pdf'):
        generated_at = timezone.localtime()
        rows = [
            [
                index, patient.patient_number, patient.full_name(),
                patient.age, patient.get_sex_display(),
                patient.barangay.name if patient.barangay else '',
                patient.contact_display,
                timezone.localtime(patient.created_at).strftime('%b %d, %Y'),
            ]
            for index, patient in enumerate(patients, start=1)
        ]
        headers = [
            'No.', 'Patient ID', 'Patient Name', 'Age', 'Sex',
            'Barangay', 'Contact Number', 'Registration Date',
        ]
        summary_lines = [('Total Patients', str(total))]
        if export == 'print':
            return _render_report_print(
                request, 'PATIENTS REPORT',
                f'{label} · Generated {generated_at:%B %d, %Y}',
                headers, rows, summary_lines, summary_at_bottom=True)
        pdf = _build_pdf(
            'PATIENTS REPORT', label, headers, rows, summary_lines)
        return _pdf_response(
            pdf, f"ABTC_Patients_{date.today().isoformat()}.pdf",
            inline=request.GET.get('preview') == '1')

    page_obj = Paginator(patients, 25).get_page(request.GET.get('page'))
    return render(request, 'reports/patients_report.html', {
        'page_obj': page_obj,
        'total': total,
        'q': request.GET.get('q', '').strip(),
        'report_type': report_type,
        'label': label,
        'period_options': RECORD_REPORT_PERIODS,
    })


@reports_access
def animal_bite_cases_report(request):
    cases, report_type, label = _bite_case_records_query(request)
    total = cases.count()
    export = request.GET.get('export')
    if export in ('print', 'pdf'):
        generated_at = timezone.localtime()
        rows = [
            [
                index, case.case_number, case.patient.full_name(),
                timezone.localtime(case.date_time_bite).strftime('%b %d, %Y'),
                _animal_label(case), case.get_exposure_type_display(),
                case.incident_barangay, case.get_case_status_display(),
            ]
            for index, case in enumerate(cases, start=1)
        ]
        headers = [
            'No.', 'Case Number', 'Patient Name', 'Date of Incident',
            'Animal Type', 'Exposure Type', 'Barangay', 'Case Status',
        ]
        summary_lines = [('Total Animal Bite Cases', str(total))]
        if export == 'print':
            return _render_report_print(
                request, 'ANIMAL BITE CASES REPORT',
                f'{label} · Generated {generated_at:%B %d, %Y}',
                headers, rows, summary_lines, summary_at_bottom=True)
        pdf = _build_pdf(
            'ANIMAL BITE CASES REPORT', label, headers, rows, summary_lines)
        return _pdf_response(
            pdf, f"ABTC_Animal_Bite_Cases_{date.today().isoformat()}.pdf",
            inline=request.GET.get('preview') == '1')

    page_obj = Paginator(cases, 25).get_page(request.GET.get('page'))
    for case in page_obj:
        case.report_animal = _animal_label(case)
    return render(request, 'reports/animal_bite_cases_report.html', {
        'page_obj': page_obj,
        'total': total,
        'q': request.GET.get('q', '').strip(),
        'report_type': report_type,
        'label': label,
        'period_options': RECORD_REPORT_PERIODS,
    })


@reports_access
def reports_index(request):
    start, end, rtype, label = _parse_dates(request)
    q = request.GET.get('q', '').strip()
    cases = _case_search_queryset(
        q, start, end, 'all') if q else _period_case_filter(
        AnimalBiteCase.objects.all(), start, end)
    completed_ids = completed_schedule_case_ids()
    summary = patient_treatment_summary(cases, completed_ids)
    inventory = inventory_summary()
    patient_page = case_page = dose_page = vaccine_page = medicine_page = None
    result_count = 0
    if q:
        normalized_query = ' '.join(q.split())
        sex = _choice_value(Patient.SEX_CHOICES, normalized_query)
        if sex:
            patients = Patient.objects.filter(sex=sex)
        else:
            patients = Patient.objects.filter(
                Q(patient_number__icontains=q)
                | Q(address__icontains=q)
                | Q(contact_number__icontains=q)
                | Q(barangay__name__icontains=q)
                | _name_query(normalized_query, prefix='')
            )
        patients = patients.distinct().order_by(
            'last_name', 'first_name', 'pk')

        doses = _dose_search_queryset(q, start, end).select_related(
            'schedule__bite_case__patient')
        today = timezone.localdate()
        vaccine_search, medicine_search = _inventory_search_query(q, today)
        vaccines = VaccineInventory.objects.filter(
            vaccine_search).order_by('vaccine_name', 'brand', 'batch_number')
        medicines = MedicineInventory.objects.filter(
            medicine_search).order_by(
                'medicine_name', 'expiration_date', 'batch_number')

        patient_page = Paginator(patients, 20).get_page(
            request.GET.get('patients_page'))
        case_page = Paginator(
            cases.select_related('patient', 'patient__barangay').order_by(
                '-date_time_bite', '-pk'), 20).get_page(
                    request.GET.get('cases_page'))
        dose_page = Paginator(
            doses.order_by('schedule__bite_case__case_number', 'id'), 20
        ).get_page(request.GET.get('vaccinations_page'))
        vaccine_page = Paginator(vaccines, 20).get_page(
            request.GET.get('vaccines_page'))
        medicine_page = Paginator(medicines, 20).get_page(
            request.GET.get('medicines_page'))

        for case in case_page:
            case.report_status = effective_status(case, completed_ids)
            case.report_animal = _animal_label(case)
            case.report_barangay = _search_result_barangay(case, q, 'all')
        for vaccine in vaccine_page:
            if vaccine.expiration_date < today:
                vaccine.report_stock_status = 'Expired'
            elif vaccine.quantity_available <= 0:
                vaccine.report_stock_status = 'Out of Stock'
            elif vaccine.quantity_available <= vaccine.reorder_level:
                vaccine.report_stock_status = 'Low Stock'
            else:
                vaccine.report_stock_status = 'Available'
        for medicine in medicine_page:
            medicine.report_stock_status = medicine.stock_status()

        result_count = sum((
            patients.count(), cases.count(), doses.count(),
            vaccines.count(), medicines.count(),
        ))
    context = {
        'summary': summary,
        'inventory': inventory,
        'report_type': rtype, 'label': label,
        'q': q,
        'today': timezone.localdate(),
        'search_submitted': bool(q),
        'patient_page': patient_page,
        'case_page': case_page,
        'dose_page': dose_page,
        'vaccine_page': vaccine_page,
        'medicine_page': medicine_page,
        'result_count': result_count,
    }
    return render(request, 'reports/index.html', context)

@reports_access
def cases_report(request):
    start, end, report_type, label = _parse_dates(request, simplified=True)
    title = label if label != "All time" else "All Cases"
    qs = _filter_cases(
        AnimalBiteCase.objects.select_related('patient'), request, start, end,
        extra_filters=False)

    # summary
    total = qs.count()
    by_status = qs.values('case_status').annotate(c=Count('id'))
    by_animal = qs.values('animal_type').annotate(c=Count('id'))
    by_barangay = qs.values('incident_barangay').annotate(c=Count('id')).order_by('-c')[:5]

    export = request.GET.get('export', '')
    preview = request.GET.get('preview', '')

    # CSV
    if export == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="cases_report_{date.today().isoformat()}.csv"'
        w = csv.writer(resp)
        if request.GET.get('q', '').strip():
            completed_ids = completed_schedule_case_ids()
            w.writerow(SEARCH_RESULT_HEADERS)
            for case in qs.select_related('patient').order_by(
                    '-date_time_bite', '-pk')[:5000]:
                w.writerow(_search_result_row(
                    case, completed_ids, request.GET.get('q', '').strip(),
                    _search_field(request)))
        else:
            w.writerow(['Case #', 'Patient', 'Date of Bite', 'Consultation', 'Animal', 'Exposure', 'Barangay', 'Status'])
            for c in qs.order_by('-created_at')[:5000]:
                w.writerow([c.case_number, c.patient.full_name(), c.date_time_bite.date() if c.date_time_bite else '', c.created_at.date(), c.get_animal_type_display(), c.get_exposure_type_display(), c.incident_barangay, c.get_case_status_display()])
        return resp

    # PDF (preview inline or download) and official browser print document
    if export in ('pdf', 'print'):
        rows = []
        if request.GET.get('q', '').strip():
            headers = SEARCH_RESULT_HEADERS
            completed_ids = completed_schedule_case_ids()
            for case in qs.select_related('patient').order_by(
                    '-date_time_bite', '-pk')[:2000]:
                rows.append(_search_result_row(
                    case, completed_ids, request.GET.get('q', '').strip(),
                    _search_field(request)))
        else:
            headers = ["Case #", "Patient", "Bite Date", "Animal", "Exposure", "Barangay", "Status"]
            for c in qs.select_related('patient').order_by('-created_at')[:2000]:
                rows.append([c.case_number, c.patient.full_name(), c.date_time_bite.strftime('%Y-%m-%d') if c.date_time_bite else '', c.get_animal_type_display(), c.get_exposure_type_display(), c.incident_barangay, c.get_case_status_display()])
        subtitle = label + (f" · {qs.count()} record(s)" if qs.count() else "")
        summary = [("Total cases", str(total))]
        summary += _case_filter_summary(request, label, extra_filters=False)
        if export == 'print':
            return _render_report_print(request, title, subtitle, headers, rows, summary)
        pdf = _build_pdf(title, subtitle, headers, rows, summary_lines=summary)
        inline = preview == '1' or request.GET.get('inline') == '1'
        filename = f"ABTC_Cases_{start.isoformat() if start else 'all'}_{end.isoformat() if end else 'all'}.pdf"
        return _pdf_response(pdf, filename, inline=inline)

    # HTML preview (paginated)
    cases = qs.order_by('-created_at')
    # simple pagination via slice? Use Django paginator
    from django.core.paginator import Paginator
    paginator = Paginator(cases, 20)
    page = request.GET.get('page')
    page_obj = paginator.get_page(page)

    context = {
        'cases': page_obj, 'page_obj': page_obj,
        'title': title, 'label': label,
        'total': total, 'by_status': by_status, 'by_animal': by_animal, 'by_barangay': by_barangay,
        'report_type': report_type,
        'q': request.GET.get('q',''),
        'search_field': _search_field(request),
    }
    return render(request, 'reports/cases_report.html', context)

@reports_access
def vaccine_report(request):
    start, end, report_type, label = _parse_dates(request)
    items = VaccineInventory.objects.all().order_by(
        'vaccine_name', 'brand', 'batch_number')
    if start and end:
        items = items.filter(date_received__gte=start, date_received__lte=end)
    elif start:
        items = items.filter(date_received__gte=start)
    elif end:
        items = items.filter(date_received__lte=end)

    q = request.GET.get('q', '').strip()
    if q:
        search_q = (
            Q(vaccine_name__icontains=q)
            | Q(brand__icontains=q)
            | Q(batch_number__icontains=q)
        )
        if q.isdecimal():
            quantity = int(q)
            search_q |= (
                Q(quantity_received=quantity)
                | Q(quantity_used=quantity)
                | Q(quantity_available=quantity)
            )
        for date_format in (
            '%Y-%m-%d', '%m/%d/%Y', '%m-%d-%Y',
            '%b %d, %Y', '%B %d, %Y',
        ):
            try:
                expiry_date = datetime.strptime(q, date_format).date()
            except ValueError:
                continue
            search_q |= Q(expiration_date=expiry_date)
            break
        items = items.filter(search_q)

    total_received = items.aggregate(total=Sum('quantity_received'))['total'] or 0
    total_used = items.aggregate(total=Sum('quantity_used'))['total'] or 0
    total_available = items.aggregate(total=Sum('quantity_available'))['total'] or 0

    export = request.GET.get('export','')
    preview = request.GET.get('preview','')

    if export == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="vaccine_report_{date.today().isoformat()}.csv"'
        w = csv.writer(resp)
        w.writerow(['Vaccine', 'Brand', 'Batch', 'Received', 'Used', 'Available', 'Expiry', 'Date Received'])
        for i in items:
            w.writerow([i.vaccine_name, i.brand, i.batch_number, i.quantity_received, i.quantity_used, i.quantity_available, i.expiration_date, i.date_received])
        return resp

    if export in ('pdf', 'print'):
        headers = ["Vaccine", "Brand", "Batch", "Received", "Used", "Available", "Expiry"]
        rows = [[i.vaccine_name, i.brand, i.batch_number, str(i.quantity_received), str(i.quantity_used), str(i.quantity_available), i.expiration_date.strftime('%Y-%m-%d') if i.expiration_date else ''] for i in items[:1500]]
        subtitle = label + f" · {items.count()} lot(s) · Received {total_received} · Used {total_used} · Available {total_available}"
        summary = [("Total lots", str(items.count())), ("Date range", label), ("Total received", str(total_received)), ("Total used", str(total_used)), ("Available", str(total_available))]
        if export == 'print':
            return _render_report_print(
                request, "Vaccine Inventory Report", subtitle, headers, rows, summary)
        pdf = _build_pdf("Vaccine Inventory Report", subtitle, headers, rows, summary_lines=summary)
        inline = preview=='1' or request.GET.get('inline')=='1'
        filename = f"ABTC_Vaccine_{start.isoformat() if start else 'all'}_{end.isoformat() if end else 'all'}.pdf"
        return _pdf_response(pdf, filename, inline=inline)

    from django.core.paginator import Paginator
    paginator = Paginator(items, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'reports/vaccine_report.html', {
        'items': page_obj, 'page_obj': page_obj,
        'total_received': total_received, 'total_used': total_used, 'total_available': total_available,
        'label': label, 'report_type': report_type,
        'start_date': start.strftime('%Y-%m-%d') if start else '',
        'end_date': end.strftime('%Y-%m-%d') if end else '',
        'q': q, 'has_filters': bool(start or end or q),
        'period_type': request.GET.get('type', report_type),
    })

@reports_access
def missed_appointments_report(request):
    return _vaccination_followup_report(request, 'missed')


@reports_access
def completed_vaccinations_report(request):
    return _vaccination_followup_report(request, 'completed')


@reports_access
def under_treatment_report(request):
    return _vaccination_followup_report(request, 'treatment')


def _ordinal_label(position):
    suffix = 'th'
    if position % 100 not in (11, 12, 13):
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(position % 10, 'th')
    return f'{position}{suffix} Dose'


def _search_report_values(query, values):
    if not query:
        return True
    keyword = ' '.join(query.casefold().split())
    return any(keyword in ' '.join(str(value or '').casefold().split())
               for value in values)


def _date_search_values(value):
    if not value:
        return []
    return [
        value.isoformat(),
        value.strftime('%b %d, %Y'),
        value.strftime('%B %d, %Y'),
        value.strftime('%m/%d/%Y'),
    ]


def _missed_row_matches(query, cells, scheduled_date):
    if not query:
        return True

    keyword = ' '.join(query.casefold().split())
    numeric_keyword = None
    try:
        numeric_keyword = int(keyword)
    except ValueError:
        pass

    if numeric_keyword is not None and numeric_keyword in (cells[0], cells[7]):
        return True

    date_keyword = None
    for date_format in (
            '%Y-%m-%d', '%m/%d/%Y', '%m-%d-%Y', '%b %d, %Y',
            '%B %d, %Y'):
        try:
            date_keyword = datetime.strptime(query.strip(), date_format).date()
            break
        except ValueError:
            continue
    if date_keyword is not None and scheduled_date == date_keyword:
        return True

    text_cells = (cells[1], cells[2], cells[3], cells[4], cells[6])
    if _search_report_values(query, text_cells):
        return True

    return (
        date_keyword is None
        and not query.strip().isdigit()
        and _search_report_values(
        query, _date_search_values(scheduled_date))
    )


def _parse_report_search_date(query):
    for date_format in (
            '%Y-%m-%d', '%m/%d/%Y', '%m-%d-%Y', '%b %d, %Y',
            '%B %d, %Y'):
        try:
            return datetime.strptime(query.strip(), date_format).date()
        except ValueError:
            continue
    return None


def _completed_row_matches(query, cells, scheduled_date, actual_date):
    if not query:
        return True

    keyword = ' '.join(query.casefold().split())
    try:
        row_number = int(keyword)
    except ValueError:
        row_number = None
    if row_number is not None:
        return row_number == cells[0]

    date_keyword = _parse_report_search_date(query)
    if date_keyword is not None:
        return date_keyword in (scheduled_date, actual_date)

    text_cells = (*cells[1:5], *cells[7:9])
    if _search_report_values(query, text_cells):
        return True

    return _search_report_values(
        query,
        _date_search_values(scheduled_date)
        + _date_search_values(actual_date),
    )


def _dose_position_map(doses):
    positions = {}
    counts = {}
    for dose in doses:
        counts[dose.schedule_id] = counts.get(dose.schedule_id, 0) + 1
        positions[dose.pk] = _ordinal_label(counts[dose.schedule_id])
    return positions


def _vaccination_followup_report(request, kind):
    start, end, report_type, label = _parse_dates(request, simplified=True)
    query = request.GET.get('q', '').strip()
    today = timezone.localdate()
    headers_by_kind = {
        'missed': [
            'No.', 'Patient Name', 'Case Number', 'Contact Number',
            'Dose Number', 'Scheduled Date', 'Administration Status',
            'Days Overdue',
        ],
        'completed': [
            'No.', 'Patient Name', 'Case Number', 'Vaccine Name / Brand',
            'Dose Number', 'Scheduled Date', 'Actual Administration Date',
            'Administered By', 'Status',
        ],
        'treatment': [
            'No.', 'Patient Name', 'Case Number', 'Animal Type',
            'Total Required Doses', 'Completed Doses', 'Remaining Doses',
            'Next Scheduled Dose', 'Next Schedule Date', 'Treatment Status',
        ],
    }
    titles = {
        'missed': 'Missed Appointments',
        'completed': 'Completed Vaccinations',
        'treatment': 'Under Treatment',
    }
    descriptions = {
        'missed': 'Patients with overdue scheduled vaccine doses that were not administered.',
        'completed': 'Patients with successfully administered and recorded vaccine doses.',
        'treatment': 'Patients with active vaccination treatment plans and remaining scheduled doses.',
    }
    badge_classes = {
        'missed': 'bg-red-100 text-red-800',
        'completed': 'bg-emerald-100 text-emerald-800',
        'treatment': 'bg-amber-100 text-amber-800',
    }
    status_indexes = {'missed': 6, 'completed': 8, 'treatment': 9}
    rows = []

    if kind in ('missed', 'completed'):
        date_field = 'scheduled_date' if kind == 'missed' else 'actual_date'
        if kind == 'missed':
            doses = missed_vaccination_doses(today=today)
            if start:
                doses = [dose for dose in doses if dose.scheduled_date >= start]
            if end:
                doses = [dose for dose in doses if dose.scheduled_date <= end]
        else:
            doses = VaccineDose.objects.select_related(
                'schedule__bite_case__patient', 'administered_by'
            ).prefetch_related('schedule__doses')
            if start:
                doses = doses.filter(**{f'{date_field}__gte': start})
            if end:
                doses = doses.filter(**{f'{date_field}__lte': end})
            doses = doses.filter(
                dose_status='completed', actual_date__isnull=False,
            ).order_by('schedule_id', 'pk')

        dose_list = list(doses) if kind == 'missed' else list(doses.distinct())
        scheduled_doses = []
        seen_schedules = set()
        for dose in dose_list:
            if dose.schedule_id not in seen_schedules:
                scheduled_doses.extend(dose.schedule.doses.all())
                seen_schedules.add(dose.schedule_id)
        positions = _dose_position_map(scheduled_doses)
        for dose in dose_list:
            case = dose.schedule.bite_case
            patient = case.patient
            dose_number = positions[dose.pk]
            scheduled = dose.scheduled_date
            if kind == 'missed':
                status = 'Missed Appointment'
                overdue = max((today - scheduled).days, 0)
                cells = [
                    0, patient.full_name(), case.case_number,
                    patient.contact_display, dose_number,
                    scheduled.strftime('%b %d, %Y'), status, overdue,
                ]
                search_values = cells + _date_search_values(scheduled)
            else:
                status = 'Completed'
                course_doses = [
                    item for item in dose.schedule.doses.all()
                    if item.dose_status != 'cancelled'
                ]
                course_completed = bool(course_doses) and all(
                    item.dose_status == 'completed'
                    and item.actual_date is not None
                    for item in course_doses
                )
                vaccine = dose.vaccine_brand or '—'
                administered_by = (
                    dose.administered_by.get_full_name()
                    or dose.administered_by.username
                    if dose.administered_by else '—'
                )
                cells = [
                    0, patient.full_name(), case.case_number, vaccine,
                    dose_number,
                    scheduled.strftime('%b %d, %Y') if scheduled else '—',
                    dose.actual_date.strftime('%b %d, %Y'),
                    administered_by,
                    'Treatment Course Completed' if course_completed else status,
                ]
                search_values = cells + _date_search_values(scheduled)
                search_values += _date_search_values(dose.actual_date)
            if kind == 'missed':
                rows.append({
                    'cells': cells,
                    'status': status,
                    'search_values': search_values,
                    'scheduled_date': scheduled,
                })
            else:
                rows.append({
                    'cells': cells,
                    'status': status,
                    'search_values': search_values,
                    'scheduled_date': scheduled,
                    'actual_date': dose.actual_date,
                })
    else:
        cases = AnimalBiteCase.objects.filter(
            case_status__in=('new', 'under_treatment'),
            vaccination_schedule__isnull=False,
        ).select_related(
            'patient', 'vaccination_schedule',
        ).prefetch_related(
            'vaccination_schedule__doses',
        ).order_by('date_time_bite', 'case_number', 'pk')
        cases = _period_case_filter(cases, start, end)
        for case in cases:
            all_doses = list(case.vaccination_schedule.doses.all())
            required_doses = [
                dose for dose in all_doses
                if dose.dose_status != 'cancelled'
            ]
            completed_doses = [
                dose for dose in required_doses
                if dose.dose_status == 'completed'
                and dose.actual_date is not None
            ]
            remaining = len(required_doses) - len(completed_doses)
            if not required_doses or remaining <= 0:
                continue
            positions = _dose_position_map(all_doses)
            next_dose = min(
                (
                    dose for dose in required_doses
                    if dose not in completed_doses and dose.scheduled_date
                ),
                key=lambda dose: dose.scheduled_date,
                default=None,
            )
            next_label = (
                positions[next_dose.pk] if next_dose else '—'
            )
            next_date = next_dose.scheduled_date if next_dose else None
            status = 'Under Treatment'
            cells = [
                0, case.patient.full_name(), case.case_number,
                case.get_animal_type_display(), len(required_doses),
                len(completed_doses), remaining, next_label,
                next_date.strftime('%b %d, %Y') if next_date else '—',
                status,
            ]
            search_values = cells + _date_search_values(next_date)
            search_values += _date_search_values(
                timezone.localtime(case.date_time_bite).date()
                if case.date_time_bite else None)
            if _search_report_values(query, search_values):
                rows.append({
                    'cells': cells,
                    'status': status,
                    'search_values': search_values,
                })

    if kind == 'missed':
        matching_rows = []
        for index, row in enumerate(rows, start=1):
            row['cells'][0] = index
            if _missed_row_matches(
                    query, row['cells'], row['scheduled_date']):
                matching_rows.append(row)
        rows = matching_rows
    elif kind == 'completed':
        matching_rows = []
        for index, row in enumerate(rows, start=1):
            row['cells'][0] = index
            if _completed_row_matches(
                    query, row['cells'], row['scheduled_date'],
                    row['actual_date']):
                matching_rows.append(row)
        rows = matching_rows
    else:
        for index, row in enumerate(rows, start=1):
            row['cells'][0] = index

    count = len(rows)
    export = request.GET.get('export', '')
    headers = headers_by_kind[kind]
    title = titles[kind]
    if export in ('print', 'pdf', 'csv'):
        export_rows = [row['cells'] for row in rows]
        summary = [
            ('Reporting Period', label),
            ('Total Records', str(count)),
        ]
        if query:
            summary.append(('Search', query))
        if export == 'csv':
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = (
                f'attachment; filename="{kind}_vaccination_report_'
                f'{today.isoformat()}.csv"')
            writer = csv.writer(response)
            writer.writerow(headers)
            writer.writerows(export_rows)
            return response
        if export == 'print':
            return _render_report_print(
                request, title, label, headers, export_rows, summary)
        pdf = _build_pdf(title, label, headers, export_rows,
                         summary_lines=summary)
        return _pdf_response(
            pdf, f'ABTC_{kind.title()}_{today.isoformat()}.pdf',
            inline=request.GET.get('preview') == '1')

    from django.core.paginator import Paginator
    paginator = Paginator(rows, 25)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'reports/vaccination_followup_report.html', {
        'headers': headers,
        'rows': page_obj,
        'page_obj': page_obj,
        'total': count,
        'title': title,
        'description': descriptions[kind],
        'label': label,
        'report_type': report_type,
        'period_options': RECORD_REPORT_PERIODS,
        'q': query,
        'kind': kind,
        'badge_class': badge_classes[kind],
        'status_index': status_indexes[kind],
    })


# ---------- patient and treatment ----------

PATIENT_HEADERS = [
    'Patient Name', 'Patient ID', 'Case Number', 'Barangay', 'Animal Type',
    'Date of Bite Incident', 'Treatment Status',
]


def _patient_row(bite_case, completed_ids):
    return [
        bite_case.patient.full_name(),
        bite_case.patient.patient_number,
        bite_case.case_number,
        _barangay_label(bite_case.incident_barangay),
        _animal_label(bite_case),
        bite_case.date_time_bite.strftime('%Y-%m-%d') if bite_case.date_time_bite else '',
        effective_status(bite_case, completed_ids),
    ]


@reports_access
def patient_treatment_report(request):
    start, end, report_type, label = _parse_dates(request, simplified=True)
    qs = _filter_cases(
        AnimalBiteCase.objects.select_related('patient'), request, start, end,
        extra_filters=False)
    completed_ids = completed_schedule_case_ids()
    summary = patient_treatment_summary(qs, completed_ids)
    cases = qs.order_by('-date_time_bite', '-id')
    export = request.GET.get('export', '')
    preview = request.GET.get('preview', '')
    subtitle = f"{label} · {summary['total_cases']} case(s) · {summary['total_patients']} patient(s)"

    if export == 'csv':
        if request.GET.get('q', '').strip():
            headers = SEARCH_RESULT_HEADERS
            rows = [
                _search_result_row(
                    c, completed_ids, request.GET.get('q', '').strip(),
                    _search_field(request))
                for c in cases[:5000]]
        else:
            headers = PATIENT_HEADERS
            rows = [_patient_row(c, completed_ids) for c in cases[:5000]]
        return _csv_response(
            f"ABTC_Patient_Treatment_{date.today().isoformat()}.csv",
            headers, rows)

    if export in ('pdf', 'print'):
        if request.GET.get('q', '').strip():
            headers = SEARCH_RESULT_HEADERS
            rows = [
                _search_result_row(
                    c, completed_ids, request.GET.get('q', '').strip(),
                    _search_field(request))
                for c in cases[:2000]]
        else:
            headers = PATIENT_HEADERS
            rows = [_patient_row(c, completed_ids) for c in cases[:2000]]
        summary_lines = _case_filter_summary(
            request, label, extra_filters=False) + [
            ('Patients', str(summary['total_patients'])),
            ('Bite cases', str(summary['total_cases'])),
            ('Completed', str(summary['completed'])),
            ('Under treatment', str(summary['under_treatment'])),
        ]
        if export == 'print':
            return _render_report_print(
                request, "Patient and Treatment Report", subtitle,
                headers, rows, summary_lines)
        pdf = _build_pdf(
            "Patient and Treatment Report", subtitle, headers, rows,
            summary_lines=summary_lines)
        s, e = _export_period(start, end)
        inline = preview == '1' or request.GET.get('inline') == '1'
        return _pdf_response(
            pdf, f"ABTC_Patient_Treatment_{s}_{e}.pdf", inline=inline)

    paginator = Paginator(cases, 20)
    page_obj = paginator.get_page(request.GET.get('page'))
    for case in page_obj:
        case.report_status = effective_status(case, completed_ids)
        case.report_barangay = _barangay_label(case.incident_barangay)
        case.report_animal = _animal_label(case)
    return render(request, 'reports/patient_treatment.html', {
        'page_obj': page_obj,
        'summary': summary,
        'label': label,
        'report_type': report_type,
        'q': request.GET.get('q', ''),
        'search_field': _search_field(request),
        'pdf_title': 'Patient and Treatment Report',
    })


# ---------- inventory snapshots ----------

def _search_products(products, q, fields):
    if not q:
        return products
    needle = q.casefold()
    return [
        product for product in products
        if any(
            needle in str(product.get(field) or '').casefold()
            or any(needle in str(getattr(batch, field, '') or '').casefold()
                   for batch in product.get('batches', ()))
            for field in fields
        )
    ]


def _inventory_context(request, label, products, totals, snapshot_date):
    return {
        'products': products,
        'totals': totals,
        'snapshot_date': snapshot_date,
        'label': label,
        'q': request.GET.get('q', ''),
        'has_filters': bool(request.GET.get('q', '').strip()),
    }


@reports_access
def vaccine_inventory_report(request):
    q = request.GET.get('q', '').strip()
    products = _search_products(
        grouped_vaccine_products(), q, (
            'name', 'brand', 'batch_number', 'source_supplier',
            'expiration_date', 'stock_status'))
    totals = grouped_totals(products)
    today = timezone.localdate()
    label = f"Current inventory snapshot · {today:%b %d, %Y}"
    export = request.GET.get('export', '')
    preview = request.GET.get('preview', '')
    headers = [
        'Vaccine Name', 'Brand', 'Batches', 'Total Received', 'Total Used',
        'Total Available', 'Nearest Expiration', 'Stock Status',
    ]

    def row(product):
        return [
            product['name'], product['brand'], product['batch_count'],
            product['total_received'], product['total_used'],
            product['total_available'],
            product['nearest_expiration'].strftime('%Y-%m-%d')
            if product['nearest_expiration'] else '',
            product['stock_status'],
        ]

    if export == 'csv':
        return _csv_response(
            f"ABTC_Vaccine_Inventory_{today.isoformat()}.csv",
            headers, [row(product) for product in products])

    if export in ('pdf', 'print'):
        summary_lines = [
            ('Snapshot', f"Current inventory as of {today:%b %d, %Y}"),
            ('Vaccine products', str(totals['products'])),
            ('Batches', str(totals['batches'])),
            ('Total received', str(totals['received'])),
            ('Total used', str(totals['used'])),
            ('Available (non-expired)', str(totals['available'])),
        ]
        rows = [row(product) for product in products]
        if export == 'print':
            return _render_report_print(
                request, "Vaccine Inventory Report", label, headers, rows,
                summary_lines)
        pdf = _build_pdf(
            "Vaccine Inventory Report", label, headers, rows,
            summary_lines=summary_lines)
        inline = preview == '1' or request.GET.get('inline') == '1'
        return _pdf_response(
            pdf, f"ABTC_Vaccine_Inventory_{today.isoformat()}.pdf",
            inline=inline)

    context = _inventory_context(request, label, products, totals, today)
    context['pdf_title'] = 'Vaccine Inventory Report'
    return render(request, 'reports/vaccine_inventory.html', context)


@reports_access
def medicine_inventory_report(request):
    q = request.GET.get('q', '').strip()
    products = _search_products(
        grouped_medicine_products(), q,
        ('name', 'generic_name', 'strength', 'dosage_form',
         'batch_number', 'source_supplier', 'expiration_date',
         'stock_status'))
    totals = grouped_totals(products)
    today = timezone.localdate()
    label = f"Current inventory snapshot · {today:%b %d, %Y}"
    export = request.GET.get('export', '')
    preview = request.GET.get('preview', '')
    headers = [
        'Medicine Name', 'Generic / Brand', 'Dosage Form', 'Unit', 'Batches',
        'Total Received', 'Total Dispensed', 'Total Available',
        'Nearest Expiration', 'Stock Status',
    ]

    def row(product):
        return [
            product['display_name'], product['generic_name'] or '—',
            product['dosage_form'], product['unit'], product['batch_count'],
            product['total_received'], product['total_dispensed'],
            product['total_available'],
            product['nearest_expiration'].strftime('%Y-%m-%d')
            if product['nearest_expiration'] else '',
            product['stock_status'],
        ]

    if export == 'csv':
        return _csv_response(
            f"ABTC_Medicine_Inventory_{today.isoformat()}.csv",
            headers, [row(product) for product in products])

    if export in ('pdf', 'print'):
        summary_lines = [
            ('Snapshot', f"Current inventory as of {today:%b %d, %Y}"),
            ('Medicine products', str(totals['products'])),
            ('Batches', str(totals['batches'])),
            ('Total received', str(totals['received'])),
            ('Total dispensed', str(totals['dispensed'])),
            ('Available (non-expired)', str(totals['available'])),
        ]
        rows = [row(product) for product in products]
        if export == 'print':
            return _render_report_print(
                request, "Medicine Inventory Report", label, headers, rows,
                summary_lines)
        pdf = _build_pdf(
            "Medicine Inventory Report", label, headers, rows,
            summary_lines=summary_lines)
        inline = preview == '1' or request.GET.get('inline') == '1'
        return _pdf_response(
            pdf, f"ABTC_Medicine_Inventory_{today.isoformat()}.pdf",
            inline=inline)

    context = _inventory_context(request, label, products, totals, today)
    context['pdf_title'] = 'Medicine Inventory Report'
    return render(request, 'reports/medicine_inventory.html', context)


@reports_access
def stock_alerts_report(request):
    sections = stock_alert_sections()
    total = sum(
        len(section['rows'])
        for section in sections if section['key'] != 'pulled_out'
    )
    pulled_out_total = next(
        (len(section['rows']) for section in sections
         if section['key'] == 'pulled_out'),
        0,
    )
    today = timezone.localdate()
    label = f"Current inventory snapshot · {today:%b %d, %Y}"
    export = request.GET.get('export', '')
    preview = request.GET.get('preview', '')
    headers = [
        'Medicine / Vaccine', 'Form', 'Unit', 'Batches', 'Available',
        'Expiry',
    ]
    rows = [
        [
            f"{row['module_label']}: {row['product']} · {section['title']}",
            row['form'], row['unit'],
            (
                (
                    f"{row['batch_summary']}; "
                    f"Pull-out date: {row['pull_out_date']:%b %d, %Y}; "
                    f"Reason: {row['reason']}; "
                    f"Responsible: {row['responsible']}; "
                    f"Transaction Reference: {row['reference']}"
                )
                if section['key'] == 'pulled_out'
                else row.get('batch_summary') or row['batch']
            ),
            f"{row['available']} {row['unit']}".strip(),
            (
                row.get('expiry_summary') or
                (row['expiration'].strftime('%Y-%m-%d')
                 if row.get('expiration') else '')
                if section['key'] != 'pulled_out'
                else row['expiration'].strftime('%Y-%m-%d')
                if row.get('expiration') else ''
            ),
        ]
        for section in sections for row in section['rows']
    ]

    if export == 'csv':
        return _csv_response(
            f"ABTC_Stock_Alerts_{today.isoformat()}.csv", headers, rows)

    if export in ('pdf', 'print'):
        summary_lines = [('Snapshot', f"Current inventory as of {today:%b %d, %Y}")]
        summary_lines += [
            (section['title'], str(len(section['rows']))) for section in sections
        ]
        summary_lines.insert(
            0, ('Active alerts', str(total)))
        if export == 'print':
            return _render_report_print(
                request, "Stock Alerts Report", label, headers, rows,
                summary_lines)
        pdf = _build_pdf(
            "Stock Alerts Report", label, headers, rows,
            summary_lines=summary_lines)
        inline = preview == '1' or request.GET.get('inline') == '1'
        return _pdf_response(
            pdf, f"ABTC_Stock_Alerts_{today.isoformat()}.pdf", inline=inline)

    return render(request, 'reports/stock_alerts.html', {
        'sections': sections,
        'total': total,
        'pulled_out_total': pulled_out_total,
        'snapshot_date': today,
        'label': label,
        'pdf_title': 'Stock Alerts Report',
    })


# ---------- animal bite statistics ----------

def _distribution_context(request, distribution, label, report_type):
    return {
        'rows': distribution['rows'],
        'total': distribution['total'],
        'top': distribution['top'],
        'label': label,
        'report_type': report_type,
        'q': request.GET.get('q', ''),
    }


def _barangay_case_groups(cases):
    groups = {}
    for case in cases.select_related('patient').order_by(
            'date_time_bite', 'case_number', 'pk'):
        name = (case.incident_barangay or '').strip() or 'Unspecified'
        key = name.casefold()
        group = groups.setdefault(key, {'name': name, 'cases': []})
        group['cases'].append(case)
    return sorted(
        (
            {'name': group['name'], 'cases': group['cases'],
             'total': len(group['cases'])}
            for group in groups.values()
        ),
        key=lambda group: group['name'].casefold(),
    )


def _animal_type_case_groups(cases):
    groups = {}
    for case in cases.select_related('patient').order_by(
            'date_time_bite', 'case_number', 'pk'):
        key = case.animal_type
        group = groups.setdefault(key, {
            'name': case.get_animal_type_display(), 'cases': [],
        })
        group['cases'].append(case)
    return sorted(
        (
            {'key': key, 'name': group['name'], 'cases': group['cases'],
             'total': len(group['cases'])}
            for key, group in groups.items()
        ),
        key=lambda group: group['name'].casefold(),
    )


def _animal_report_cases(request, start, end):
    cases = _filter_cases(
        AnimalBiteCase.objects.all(), request, start, end,
        extra_filters=False)
    query = request.GET.get('q', '').strip()
    if query:
        animal_type_matches = _period_case_filter(
            AnimalBiteCase.objects.all().filter(
                animal_type__icontains=query),
            start, end,
        )
        cases = (cases | animal_type_matches).distinct()
    return cases


@reports_access
def barangay_report(request):
    if getattr(request.user, 'role', '') not in {'admin', 'nurse'}:
        raise PermissionDenied
    start, end, report_type, label = _parse_dates(request, simplified=True)
    qs = _filter_cases(
        AnimalBiteCase.objects.all(), request, start, end, extra_filters=False)
    distribution = barangay_distribution(qs)
    rows = sorted(
        distribution['rows'], key=lambda row: row['name'].casefold())
    export = request.GET.get('export', '')
    headers = ['No.', 'Barangay', 'Total Cases']
    export_rows = [
        [index, row['name'], row['count']]
        for index, row in enumerate(rows, start=1)
    ]
    if export == 'csv':
        return _csv_response(
            f"ABTC_Barangay_{date.today().isoformat()}.csv",
            headers, export_rows)
    if export in ('pdf', 'print'):
        summary_lines = _case_filter_summary(
            request, label, extra_filters=False) + [
                ('Total cases', str(distribution['total'])),
                ('Barangays', str(len(rows))),
            ]
        if export == 'print':
            return _render_report_print(
                request, 'Cases by Barangay Report', label,
                headers, export_rows, summary_lines)
        pdf = _build_pdf(
            'Cases by Barangay Report', label, headers, export_rows,
            summary_lines=summary_lines)
        s, e = _export_period(start, end)
        inline = request.GET.get('preview') == '1' or request.GET.get(
            'inline') == '1'
        return _pdf_response(
            pdf, f'ABTC_Barangay_{s}_{e}.pdf', inline=inline)
    if export == 'print-sheet':
        groups = _barangay_case_groups(qs)
        return render(request, 'reports/barangay_print_sheet.html', {
            'document_title': 'CASES BY BARANGAY REPORT',
            'groups': groups,
            'grand_total': sum(group['total'] for group in groups),
            'reporting_period': label,
            'search_keyword': request.GET.get('q', '').strip(),
            'printed_at': timezone.localtime(),
        })
    context = _distribution_context(
        request, distribution, label, report_type)
    context['rows'] = rows
    context['period_options'] = RECORD_REPORT_PERIODS
    context['search_field'] = _search_field(request)
    context['pdf_title'] = 'Cases by Barangay Report'
    return render(request, 'reports/barangay_report.html', context)


@reports_access
def barangay_detail_report(request, barangay):
    if getattr(request.user, 'role', '') not in {'admin', 'nurse'}:
        raise PermissionDenied
    start, end, report_type, label = _parse_dates(request, simplified=True)
    cases = _filter_cases(
        AnimalBiteCase.objects.select_related('patient'),
        request, start, end, extra_filters=False,
    )
    distribution = barangay_distribution(cases)
    selected_key = (barangay or '').strip().casefold()
    selected = next(
        (row for row in distribution['rows']
         if row['name'].casefold() == selected_key),
        None,
    )
    if selected is None:
        raise Http404

    cases = cases.annotate(
        normalized_incident_barangay=Lower(Trim('incident_barangay')))
    if selected['name'].casefold() == 'unspecified':
        cases = cases.filter(
            Q(normalized_incident_barangay='')
            | Q(normalized_incident_barangay='unspecified'))
    else:
        cases = cases.filter(
            normalized_incident_barangay=selected['name'].strip().casefold())
    cases = cases.order_by('date_time_bite', 'case_number', 'pk')

    if request.GET.get('export') == 'print':
        headers = [
            'No.', 'Case Number', 'Patient Name', 'Date of Incident',
            'Animal Type', 'Exposure Type', 'Case Status',
        ]
        rows = [
            [
                index, case.case_number, case.patient.full_name(),
                timezone.localtime(case.date_time_bite).strftime('%b %d, %Y')
                if case.date_time_bite else '',
                case.get_animal_type_display(),
                case.get_exposure_type_display(),
                case.get_case_status_display(),
            ]
            for index, case in enumerate(cases, start=1)
        ]
        summary_lines = [
            ('Selected Barangay', selected['name']),
            ('Reporting Period', label),
            ('Date Generated', timezone.localtime().strftime(
                '%B %d, %Y · %I:%M %p')),
            ('Total Number of Cases', str(len(rows))),
        ]
        return _render_report_print(
            request, 'CASES BY BARANGAY REPORT', label,
            headers, rows, summary_lines)

    return render(request, 'reports/barangay_detail_report.html', {
        'barangay': selected['name'],
        'cases': cases,
        'total': cases.count(),
        'label': label,
        'report_type': report_type,
        'q': request.GET.get('q', '').strip(),
        'search_field': _search_field(request),
    })


@reports_access
def animal_type_report(request):
    start, end, report_type, label = _parse_dates(request, simplified=True)
    qs = _animal_report_cases(request, start, end)
    groups = _animal_type_case_groups(qs)
    total = sum(group['total'] for group in groups)
    rows = [
        {'key': group['key'], 'name': group['name'], 'count': group['total']}
        for group in groups
    ]
    export = request.GET.get('export', '')
    preview = request.GET.get('preview', '')
    headers = ['No.', 'Animal Type', 'Total Cases']
    export_rows = [
        [index, row['name'], row['count']]
        for index, row in enumerate(rows, start=1)
    ]

    if export == 'csv':
        return _csv_response(
            f"ABTC_Animal_Type_{date.today().isoformat()}.csv",
            headers, export_rows)

    if export in ('pdf', 'print'):
        summary_lines = _case_filter_summary(
            request, label, extra_filters=False) + [
            ('Total cases', str(total)),
            ('Animal types', str(len(rows))),
        ]
        if export == 'print':
            return _render_report_print(
                request, "Cases by Animal Type Report", label, headers,
                export_rows, summary_lines)
        pdf = _build_pdf(
            "Cases by Animal Type Report", label, headers, export_rows,
            summary_lines=summary_lines)
        s, e = _export_period(start, end)
        inline = preview == '1' or request.GET.get('inline') == '1'
        return _pdf_response(
            pdf, f"ABTC_Animal_Type_{s}_{e}.pdf", inline=inline)

    if export == 'print-sheet':
        return render(request, 'reports/animal_type_print_sheet.html', {
            'groups': groups,
            'grand_total': total,
            'reporting_period': label,
            'search_keyword': request.GET.get('q', '').strip(),
            'printed_at': timezone.localtime(),
            'document_title': 'CASES BY ANIMAL TYPE REPORT',
        })

    return render(request, 'reports/animal_type_report.html', {
        'rows': rows,
        'total': total,
        'label': label,
        'report_type': report_type,
        'period_options': RECORD_REPORT_PERIODS,
        'q': request.GET.get('q', '').strip(),
        'search_field': 'all',
    })


@reports_access
def animal_type_detail_report(request, animal_type):
    animal_labels = dict(AnimalBiteCase.ANIMAL_TYPES)
    if animal_type not in animal_labels:
        raise Http404

    start, end, report_type, label = _parse_dates(request, simplified=True)
    cases = _animal_report_cases(request, start, end).filter(
        animal_type=animal_type).select_related('patient')
    if not cases.exists():
        raise Http404
    cases = cases.order_by('date_time_bite', 'case_number', 'pk')

    if request.GET.get('export') == 'print-sheet':
        group = _animal_type_case_groups(cases)[0]
        return render(request, 'reports/animal_type_print_sheet.html', {
            'groups': [group],
            'grand_total': group['total'],
            'reporting_period': label,
            'search_keyword': request.GET.get('q', '').strip(),
            'printed_at': timezone.localtime(),
            'document_title': 'CASES BY ANIMAL TYPE REPORT',
        })

    return render(request, 'reports/animal_type_detail_report.html', {
        'animal_type': animal_type,
        'animal_label': animal_labels[animal_type],
        'cases': cases,
        'total': cases.count(),
        'label': label,
        'report_type': report_type,
        'q': request.GET.get('q', '').strip(),
        'search_field': 'all',
    })
