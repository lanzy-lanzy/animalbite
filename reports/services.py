"""Read-only data builders for the Reports Center.

Everything here derives numbers from the live tables. No report function
writes to the database or alters inventory.
"""

from collections import OrderedDict

from django.db.models import Count, Q
from django.utils import timezone

from bite_cases.models import AnimalBiteCase
from inventory.models import (
    MedicineInventory, MedicineStockMovement, VaccineInventory,
    VaccineStockMovement,
)
from inventory.notifications import get_inventory_alerts
from vaccination.models import VaccinationSchedule

STOCK_STATUS_CLASSES = {
    'Available': 'bg-emerald-100 text-emerald-800',
    'Low Stock': 'bg-amber-100 text-amber-800',
    'Out of Stock': 'bg-red-100 text-red-800',
    'Expired': 'bg-red-100 text-red-800',
}

ALERT_STATUS_LABELS = {
    'low_stock': 'Low Stock',
    'out_of_stock': 'Out of Stock',
    'expiring': 'Expiring Soon',
    'expired': 'Expired',
}

ALERT_STATUS_CLASSES = {
    'low_stock': 'bg-amber-100 text-amber-800',
    'out_of_stock': 'bg-red-100 text-red-800',
    'expiring': 'bg-amber-100 text-amber-800',
    'expired': 'bg-red-100 text-red-800',
}

ALERT_SECTION_ORDER = [
    ('low_stock', 'Low Stock', 'Products at or below their configured minimum.'),
    ('out_of_stock', 'Out of Stock', 'Products with zero usable available stock.'),
    ('expiring', 'Expiring Soon', 'Batches inside the expiry warning window.'),
    ('expired', 'Expired', 'Expired batches that still hold stock.'),
    (
        'pulled_out',
        'Pulled Out Stocks',
        'Completed medicine and vaccine stock pull-out records.',
    ),
]


# ---------- treatment completion ----------

def completed_schedule_case_ids():
    """Case ids whose prescribed (non-cancelled) doses are all administered.

    A case with a single completed dose is only complete when that is the
    whole prescribed plan; one dose out of many never completes a case.
    """
    completed = set()
    schedules = VaccinationSchedule.objects.prefetch_related('doses')
    for schedule in schedules:
        doses = [
            dose for dose in schedule.doses.all()
            if dose.dose_status != 'cancelled'
        ]
        if doses and all(dose.dose_status == 'completed' for dose in doses):
            completed.add(schedule.bite_case_id)
    return completed


def effective_status(bite_case, completed_ids):
    """Workflow-aware label used in the patient and treatment report."""
    if bite_case.case_status == 'archived':
        return 'Archived'
    if bite_case.pk in completed_ids:
        return 'Completed'
    return bite_case.get_case_status_display()


def patient_treatment_summary(cases_qs, completed_ids=None):
    if completed_ids is None:
        completed_ids = completed_schedule_case_ids()
    return {
        'total_patients': cases_qs.values('patient_id').distinct().count(),
        'total_cases': cases_qs.count(),
        'completed': (
            cases_qs.exclude(case_status='archived')
            .filter(Q(case_status='completed') | Q(pk__in=completed_ids))
            .distinct()
            .count()
        ),
        'under_treatment': (
            cases_qs.filter(case_status='under_treatment')
            .exclude(pk__in=completed_ids)
            .count()
        ),
    }


# ---------- inventory summary ----------

def inventory_summary():
    """Current snapshot counts; independent of the reporting period."""
    alerts = get_inventory_alerts()
    out_of_stock = [a for a in alerts if a['kind'] == 'out_of_stock']
    expired = [a for a in alerts if a['kind'] == 'expired']
    return {
        'out_of_stock': len(out_of_stock),
        'expired_batches': len(expired),
        'expired_vaccine': sum(1 for a in expired if a['module'] == 'vaccine'),
        'expired_medicine': sum(1 for a in expired if a['module'] == 'medicine'),
        'low_stock': sum(1 for a in alerts if a['kind'] == 'low_stock'),
        'expiring': sum(1 for a in alerts if a['kind'] == 'expiring'),
        'total': len(alerts),
    }


def _stock_status(usable, threshold, has_live_batches):
    if not has_live_batches:
        return 'Expired'
    if usable <= 0:
        return 'Out of Stock'
    if usable <= threshold:
        return 'Low Stock'
    return 'Available'


def _finish_product(product, today):
    live = product['live']
    batches = product['batches']
    product['batch_count'] = len(batches)
    product['total_available'] = product['usable_available']
    product['threshold'] = max(
        (batch.reorder_level for batch in live), default=0)
    if live:
        product['nearest_expiration'] = min(
            batch.expiration_date for batch in live)
    else:
        product['nearest_expiration'] = min(
            (batch.expiration_date for batch in batches), default=None)
    product['stock_status'] = _stock_status(
        product['usable_available'], product['threshold'], bool(live))
    product['status_class'] = STOCK_STATUS_CLASSES[product['stock_status']]
    product['has_expired_batches'] = any(
        batch.expiration_date < today for batch in batches)
    return product


def grouped_vaccine_products():
    """One row per vaccine product (name + brand), batches kept inside."""
    today = timezone.localdate()
    groups = OrderedDict()
    queryset = VaccineInventory.objects.all().order_by(
        'vaccine_name', 'brand', 'expiration_date', 'id')
    for batch in queryset:
        key = (
            batch.vaccine_name.strip().casefold(),
            batch.brand.strip().casefold(),
        )
        product = groups.get(key)
        if product is None:
            product = groups[key] = {
                'name': batch.vaccine_name,
                'brand': batch.brand,
                'batches': [],
                'total_received': 0,
                'total_used': 0,
                'usable_available': 0,
                'live': [],
            }
        product['batches'].append(batch)
        product['total_received'] += batch.quantity_received
        product['total_used'] += batch.quantity_used
        if batch.expiration_date >= today:
            product['usable_available'] += batch.quantity_available
            product['live'].append(batch)
    return [_finish_product(product, today) for product in groups.values()]


def grouped_medicine_products():
    """One row per medicine product (name, strength, form and unit)."""
    today = timezone.localdate()
    groups = OrderedDict()
    queryset = MedicineInventory.objects.all().order_by(
        'medicine_name', 'generic_name', 'strength', 'dosage_form', 'unit',
        'expiration_date', 'id')
    for batch in queryset:
        key = (
            batch.medicine_name.strip().casefold(),
            batch.generic_name.strip().casefold(),
            batch.strength.strip().casefold(),
            batch.dosage_form,
            (batch.unit or '').strip().casefold(),
        )
        product = groups.get(key)
        if product is None:
            product = groups[key] = {
                'name': batch.medicine_name,
                'display_name': ' '.join(
                    part for part in (batch.medicine_name, batch.strength)
                    if part),
                'generic_name': batch.generic_name,
                'strength': batch.strength,
                'dosage_form': batch.get_dosage_form_display(),
                'unit': batch.unit,
                'batches': [],
                'total_received': 0,
                'total_dispensed': 0,
                'usable_available': 0,
                'live': [],
            }
        product['batches'].append(batch)
        product['total_received'] += batch.quantity_received
        product['total_dispensed'] += batch.quantity_dispensed
        if batch.expiration_date >= today:
            product['usable_available'] += batch.quantity_available
            product['live'].append(batch)
    return [_finish_product(product, today) for product in groups.values()]


def grouped_totals(products, available_key='total_available'):
    return {
        'products': len(products),
        'batches': sum(product['batch_count'] for product in products),
        'received': sum(product['total_received'] for product in products),
        'used': sum(product.get('total_used', 0) for product in products),
        'dispensed': sum(
            product.get('total_dispensed', 0) for product in products),
        'available': sum(product[available_key] for product in products),
    }


# ---------- stock alerts ----------

def stock_alert_sections():
    alerts = get_inventory_alerts()
    sections = []
    for kind, title, description in ALERT_SECTION_ORDER:
        rows = []
        if kind == 'pulled_out':
            rows = _completed_pull_out_rows()
            sections.append({
                'key': kind,
                'title': title,
                'description': description,
                'rows': rows,
            })
            continue
        for alert in alerts:
            if alert['kind'] != kind:
                continue
            batch_details = alert.get('batch_details', [])
            for detail in batch_details:
                detail['pending_pull_out'] = detail.get(
                    'pending_pull_out', 0)
            batch_summary = '; '.join(
                (
                    f"{detail['batch']} · "
                    f"{detail['pending_pull_out']} {alert['unit']} pending pull-out"
                    if kind == 'expired'
                    else f"{detail['batch']} · {detail['available']} {alert['unit']}"
                )
                for detail in batch_details if detail['batch']
            ) or alert['batch'] or '—'
            expiry_summary = '; '.join(
                f"{detail['batch']}: {detail['expiration']:%b %d, %Y}"
                for detail in batch_details if detail['expiration']
            )
            if not expiry_summary and alert['expiration']:
                expiry_summary = alert['expiration'].strftime('%b %d, %Y')
            rows.append({
                'module': alert['module'],
                'module_label': (
                    'Vaccine' if alert['module'] == 'vaccine' else 'Medicine'),
                'product': alert['product'],
                'batch': alert['batch'] or 'All batches',
                'quantity': alert['quantity_label'],
                'available': alert.get('available', alert['quantity']),
                'pending_pull_out': alert.get('pending_pull_out', 0),
                'batch_details': alert.get('batch_details', []),
                'batch_summary': batch_summary,
                'expiry_summary': expiry_summary or '—',
                'threshold': alert['threshold'],
                'expiration': alert['expiration'],
                'status': ALERT_STATUS_LABELS[kind],
                'status_class': ALERT_STATUS_CLASSES[kind],
                'url': alert['url'],
                'form': alert.get('form', ''),
                'unit': alert['unit'],
                'pull_out_date': None,
                'reason': '',
                'responsible': '',
                'reference': '',
            })
        sections.append({
            'key': kind,
            'title': title,
            'description': description,
            'rows': rows,
        })
    return sections


def _pull_out_reason(notes):
    return next(
        (
            line.partition(':')[2].strip()
            for line in (notes or '').splitlines()
            if line.casefold().startswith('reason:')
        ),
        '—',
    )


def _completed_pull_out_rows():
    rows = []
    medicine_movements = (
        MedicineStockMovement.objects
        .filter(movement_type='expired_pull_out')
        .select_related('medicine', 'moved_by')
    )
    for movement in medicine_movements:
        item = movement.medicine
        rows.append({
            'module': 'medicine',
            'module_label': 'Medicine',
            'product': ' '.join(
                part for part in (item.medicine_name, item.strength) if part),
            'form': item.get_dosage_form_display(),
            'unit': item.unit,
            'batch': movement.batch_number or item.batch_number,
            'quantity': movement.quantity,
            'expiration': movement.expiration_date or item.expiration_date,
            'pull_out_date': timezone.localtime(movement.created_at).date(),
            'reason': _pull_out_reason(movement.notes),
            'responsible': (
                movement.moved_by.get_full_name()
                or movement.moved_by.username
                if movement.moved_by else '—'
            ),
            'reference': movement.reference or '—',
            'status': 'Pulled Out',
            'status_class': 'bg-emerald-100 text-emerald-800',
            'url': '',
            'threshold': None,
            'available': 0,
            'pending_pull_out': 0,
            'batch_details': [],
            'batch_summary': (
                f"{movement.batch_number or item.batch_number} · "
                f"{movement.quantity} {item.unit} pulled out"
            ),
            'expiry_summary': (
                (movement.expiration_date or item.expiration_date)
                .strftime('%b %d, %Y')
            ),
            '_sort_datetime': movement.created_at,
        })

    vaccine_movements = (
        VaccineStockMovement.objects
        .filter(movement_type='expired_pull_out')
        .select_related('vaccine', 'moved_by')
    )
    for movement in vaccine_movements:
        item = movement.vaccine
        rows.append({
            'module': 'vaccine',
            'module_label': 'Vaccine',
            'product': ' '.join(
                part for part in (item.vaccine_name, item.brand) if part),
            'form': item.get_dosage_form_display(),
            'unit': item.unit,
            'batch': movement.batch_number or item.batch_number,
            'quantity': movement.quantity,
            'expiration': movement.expiration_date or item.expiration_date,
            'pull_out_date': timezone.localtime(movement.created_at).date(),
            'reason': _pull_out_reason(movement.notes),
            'responsible': (
                movement.moved_by.get_full_name()
                or movement.moved_by.username
                if movement.moved_by else '—'
            ),
            'reference': movement.reference or '—',
            'status': 'Pulled Out',
            'status_class': 'bg-emerald-100 text-emerald-800',
            'url': '',
            'threshold': None,
            'available': 0,
            'pending_pull_out': 0,
            'batch_details': [],
            'batch_summary': (
                f"{movement.batch_number or item.batch_number} · "
                f"{movement.quantity} {item.unit} pulled out"
            ),
            'expiry_summary': (
                (movement.expiration_date or item.expiration_date)
                .strftime('%b %d, %Y')
            ),
            '_sort_datetime': movement.created_at,
        })

    rows.sort(key=lambda row: row['_sort_datetime'], reverse=True)
    for row in rows:
        del row['_sort_datetime']
    return rows


def stock_alert_total():
    return sum(
        len(section['rows'])
        for section in stock_alert_sections()
        if section['key'] != 'pulled_out'
    )


# ---------- animal bite statistics ----------

def _pct(count, total):
    if not total:
        return 0.0
    return round(count * 100 / total, 1)


def barangay_distribution(cases_qs):
    counts = {}
    labels = {}
    for value in cases_qs.values_list('incident_barangay', flat=True):
        name = (value or '').strip() or 'Unspecified'
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1
        labels.setdefault(key, name)
    total = sum(counts.values())
    rows = [
        {'name': labels[key], 'count': count, 'pct': _pct(count, total)}
        for key, count in counts.items()
    ]
    rows.sort(key=lambda row: (-row['count'], row['name'].casefold()))
    return {'rows': rows, 'total': total, 'top': rows[0] if rows else None}


def animal_type_distribution(cases_qs):
    labels = dict(AnimalBiteCase.ANIMAL_TYPES)
    counts = {}
    for value, count in (
        cases_qs.values('animal_type')
        .annotate(count=Count('id'))
        .values_list('animal_type', 'count')
    ):
        name = labels.get(value, value or 'Unspecified')
        counts[name] = counts.get(name, 0) + count
    total = sum(counts.values())
    rows = [
        {'name': name, 'count': count, 'pct': _pct(count, total)}
        for name, count in counts.items()
    ]
    rows.sort(key=lambda row: (-row['count'], row['name'].casefold()))
    return {'rows': rows, 'total': total, 'top': rows[0] if rows else None}


def period_note(start, end, label):
    """Small helper for report subtitles that include the selected period."""
    if label:
        return label
    if start and end:
        return f'{start:%b %d, %Y} to {end:%b %d, %Y}'
    return 'All time'
