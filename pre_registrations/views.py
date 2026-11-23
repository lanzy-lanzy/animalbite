import re
from copy import copy

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from audit.models import AuditLog
from utils.qr import (
    qr_png_bytes,
    qr_slip_filename,
    qr_slip_png_bytes,
    build_pre_registration_payload,
)
from utils.redirects import redirect_back

from .forms import (
    PatientSignupPreRegistrationForm,
    RejectPreRegistrationForm,
    StaffPreRegistrationForm,
    StatusLookupForm,
)
from .models import PreRegistration
from .services import (
    PENDING_STATUSES,
    DuplicateConfirmationRequired,
    RegistrationDeletionBlocked,
    archive_pre_registration,
    conversion_patient,
    convert_to_official_case,
    delete_pre_registration,
    registration_retention_blockers,
    reject_pre_registration,
    require_review_admin,
    retrieve_pre_registration,
)


def is_authorized_staff(user):
    return user.is_authenticated and user.role in {"admin", "encoder", "health_worker", "doctor", "nurse"}


def public_create(request):
    # If logged-in patient on GET, redirect to direct case report (no need to refill personal info)
    if request.method == 'GET' and request.user.is_authenticated and getattr(request.user, 'role', '') == 'patient':
        try:
            if hasattr(request.user, 'patient_profile') and request.user.patient_profile:
                return redirect('pre_registrations:patient_report')
        except Exception:
            pass
    account = request.user if request.user.is_authenticated and request.user.role == "patient" else None
    if request.method == "POST":
        form = PatientSignupPreRegistrationForm(request.POST, account=account)
        if form.is_valid():
            record = form.save()
            if not request.user.is_authenticated:
                login(request, form.account, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, "Your account and online registration are ready. You can now track treatment online.")
            return redirect("pre_registrations:public_success", number=record.pre_registration_number)
    else:
        form = PatientSignupPreRegistrationForm(account=account)
    return render(request, "pre_registrations/public_form.html", {"form": form, "existing_account": account})


@login_required
def patient_report_bite(request):
    """For logged-in patients: report another bite directly as a case without refilling personal info."""
    if getattr(request.user, 'role', '') != 'patient':
        messages.info(request, "Please use the public online registration form.")
        return redirect('pre_registrations:public_create')
    try:
        patient = request.user.patient_profile
    except Exception:
        messages.error(request, "No patient profile found. Your account is not linked to a patient record. Please contact the health office.")
        return redirect('accounts:patient_portal')
    if not patient:
        messages.error(request, "No patient profile found.")
        return redirect('accounts:patient_portal')

    from bite_cases.forms import PatientBiteReportForm
    from bite_cases.models import AnimalBiteCase
    from patients.models import Patient as PatientModel

    # Helper to generate case number
    def _gen_case():
        last = AnimalBiteCase.objects.filter(case_number__startswith='ABC-').order_by('-id').first()
        return f"ABC-{int(last.case_number.split('-')[1]) + 1:05d}" if last else "ABC-00001"

    if request.method == 'POST':
        form = PatientBiteReportForm(request.POST)
        if form.is_valid():
            case = form.save(commit=False)
            case.patient = patient
            case.case_number = _gen_case()
            case.created_by = request.user
            # Also set patient contact for SMS fallback? Already linked
            case.save()
            # NOTE: the vaccination schedule is NOT auto-created. A nurse sets
            # it after assessing the case, so doses are never dated from an
            # unset consultation date.
            AuditLog.objects.create(user=request.user, action='PATIENT_REPORT_BITE', description=f'Patient {patient.patient_number} reported new bite as {case.case_number}')
            messages.success(request, f'Bite reported successfully as {case.case_number}. Staff will review it and a nurse will set your vaccine schedule. Check My Dashboard for updates.')
            return redirect('accounts:patient_portal')
    else:
        form = PatientBiteReportForm()
    return render(request, 'pre_registrations/patient_report.html', {'form': form, 'patient': patient})


def public_success(request, number):
    record = get_object_or_404(PreRegistration, pre_registration_number=number)
    return render(request, "pre_registrations/public_success.html", {"record": record})


def public_slip(request, number):
    record = get_object_or_404(PreRegistration, pre_registration_number=number)
    return render(request, "pre_registrations/slip.html", {"record": record, "public": True})


def public_status(request):
    form = StatusLookupForm(request.POST or request.GET or None)
    record = None
    missing = False
    if form.is_bound and form.is_valid():
        number = form.cleaned_data["pre_registration_number"].strip().upper()
        record = PreRegistration.objects.filter(pre_registration_number=number).first()
        missing = record is None
    return render(
        request,
        "pre_registrations/public_status.html",
        {"form": form, "record": record, "missing": missing},
    )


def qr_code(request, number):
    """Serve the pre-registration slip QR as a spec-compliant PNG.

    Payload policy (fixes codes that scanned but opened the wrong machine):
    - With a canonical public SITE_URL configured, encode the absolute status
      page URL so any external phone camera opens the live registration
      status directly.
    - Otherwise encode the portable `ABTC-PRE-REG:<number>` token, which the
      in-app scanner resolves against the live database on every scan.
    The current request host is never baked in — slips generated on localhost
    used to embed http://localhost:8000/... and were useless off the machine.
    """
    record = get_object_or_404(PreRegistration, pre_registration_number=number)
    site_url = getattr(settings, "SITE_URL", "")
    if site_url:
        payload = f"{site_url.rstrip('/')}/{reverse('pre_registrations:public_success', args=[record.pre_registration_number]).lstrip('/')}"
    else:
        payload = build_pre_registration_payload(record.pre_registration_number)
    import qrcode.constants
    if request.GET.get("download"):
        submitted = timezone.localtime(record.submitted_at).strftime("%b %d, %Y %I:%M %p")
        png = qr_slip_png_bytes(
            payload,
            title="Online Registration QR Slip",
            full_name=record.full_name(),
            reference_label="Registration No.",
            reference_value=record.pre_registration_number,
            details=[
                ("Registration No.", record.pre_registration_number),
                ("Full Name", record.full_name()),
                ("Age / Sex", f"{record.age} / {record.get_sex_display()}"),
                ("Contact Number", record.contact_number),
                ("Barangay", record.barangay),
                ("Submitted", submitted),
                ("Status", record.get_status_display()),
            ],
            footer="Present this QR slip at the ABTC registration desk.",
            error_correction=qrcode.constants.ERROR_CORRECT_Q,
        )
        filename = qr_slip_filename(record.pre_registration_number, record.full_name())
        response = HttpResponse(png, content_type="image/png")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    png = qr_png_bytes(payload, error_correction=qrcode.constants.ERROR_CORRECT_Q)
    return HttpResponse(png, content_type="image/png")


@login_required
@user_passes_test(is_authorized_staff)
def staff_list(request):
    query = request.GET.get("q", "")
    status = request.GET.get("status", "")
    barangay = request.GET.get("barangay", "")
    submitted_date = request.GET.get("date", "")
    records = PreRegistration.objects.filter(is_archived=False)
    if query:
        records = records.filter(
            Q(pre_registration_number__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(contact_number__icontains=query)
        )
    if status:
        records = records.filter(status=status)
    if barangay:
        records = records.filter(barangay__icontains=barangay)
    if submitted_date:
        records = records.filter(submitted_at__date=submitted_date)

    paginator = Paginator(records, 20)
    page = paginator.get_page(request.GET.get("page"))
    for record in page:
        record.retention_blocked = bool(registration_retention_blockers(record))
        record.can_convert = _can_convert(record, request.user)
    return render(
        request,
        "pre_registrations/staff_list.html",
        {
            "records": page,
            "statuses": PreRegistration.Status.choices,
            "query": query,
            "selected_status": status,
            "selected_barangay": barangay,
            "selected_date": submitted_date,
        },
    )


def _retention_block_message(number, blockers):
    return (
        f"Permanent deletion of online registration {number} is blocked by the medical "
        f"record-retention policy: {', '.join(blockers)}. Nothing was deleted. "
        "Archive it instead to remove it from active lists while keeping the record."
    )


@login_required
@require_POST
def staff_delete(request, pk):
    if getattr(request.user, "role", "") != "admin":
        messages.error(request, "Only the System Administrator can delete online registrations.")
        return redirect("pre_registrations:staff_list")

    record = get_object_or_404(PreRegistration, pk=pk)
    number = record.pre_registration_number
    blockers = registration_retention_blockers(record)
    if blockers:
        messages.error(request, _retention_block_message(number, blockers))
        return redirect("pre_registrations:staff_list")

    try:
        delete_pre_registration(record, request.user)
    except RegistrationDeletionBlocked as exc:
        messages.error(request, _retention_block_message(number, exc.labels))
        return redirect("pre_registrations:staff_list")

    messages.success(request, f"Online registration {number} was permanently deleted.")
    return redirect("pre_registrations:staff_list")


@login_required
@require_POST
def staff_archive(request, pk):
    if getattr(request.user, "role", "") != "admin":
        messages.error(request, "Only the System Administrator can archive online registrations.")
        return redirect("pre_registrations:staff_list")

    record = get_object_or_404(PreRegistration, pk=pk)
    already_archived = archive_pre_registration(record, request.user)
    if already_archived:
        messages.info(
            request,
            f"Online registration {record.pre_registration_number} is already archived.",
        )
    else:
        messages.success(
            request,
            f"Online registration {record.pre_registration_number} was archived and removed "
            "from the active list. The registration and any linked patient records were preserved.",
        )
    return redirect("pre_registrations:staff_list")


@login_required
@require_POST
def staff_retrieve(request, pk):
    if getattr(request.user, "role", "") != "admin":
        messages.error(request, "Only the System Administrator can retrieve online registrations.")
        return redirect_back(request, "audit:audit_list")

    record = get_object_or_404(PreRegistration, pk=pk)
    was_archived = retrieve_pre_registration(record, request.user)
    if not was_archived:
        messages.info(
            request,
            f"Online registration {record.pre_registration_number} is not archived.",
        )
    else:
        messages.success(
            request,
            f"Online registration {record.pre_registration_number} was retrieved and restored "
            "to the active list.",
        )
    return redirect_back(request, "audit:audit_list")


def _can_convert(record, user):
    if getattr(user, "role", "") != "admin" or record.status == PreRegistration.Status.CONVERTED:
        return False
    try:
        patient = conversion_patient(record)
    except ValidationError:
        return False
    return not patient or not patient.is_archived


def _review_navigation(request):
    source = request.POST if request.method == "POST" else request.GET
    from_patients = source.get("return_to") == "patients" or (
        request.method == "GET" and source.get("from") == "patients"
    )
    target = source.get("next", "") if from_patients else ""
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = ""
    return {
        "return_to": "patients" if from_patients else "",
        "next": target,
        "back_url": (target or reverse("patients:patient_list")) if from_patients
        else reverse("pre_registrations:staff_list"),
    }


def _review_redirect(request, record):
    if request.POST.get("return_to") == "patients":
        return redirect_back(request, "patients:patient_list")
    return redirect("pre_registrations:staff_detail", pk=record.pk)


def _detail_context(request, record, reject_form=None):
    # Exact token boundaries exclude, for example, AB-...-00010 from ...-0001.
    number_pattern = rf"(?<![\w-]){re.escape(record.pre_registration_number)}(?![\w-])"
    history = AuditLog.objects.filter(
        Q(target_type="pre_registration", target_id=record.pk)
        | Q(target_type="", target_id__isnull=True, description__regex=number_pattern)
    ).select_related("user").order_by("-created_at", "-pk")
    can_convert = _can_convert(record, request.user)
    return {
        "record": record,
        "duplicates": record.possible_duplicates() if can_convert and record.bite_datetime else [],
        "reject_form": reject_form if reject_form is not None else RejectPreRegistrationForm(),
        "can_convert": can_convert,
        "can_reject": (
            getattr(request.user, "role", "") == "admin" and not record.is_archived
            and record.status in PENDING_STATUSES
            and not record.converted_patient_id and not record.converted_case_id
        ),
        "can_edit": record.status != PreRegistration.Status.REJECTED and not record.is_archived,
        "history": history,
        **_review_navigation(request),
    }


@login_required
@user_passes_test(is_authorized_staff)
def staff_detail(request, pk):
    record = get_object_or_404(PreRegistration, pk=pk)
    return render(request, "pre_registrations/staff_detail.html", _detail_context(request, record))


@login_required
@transaction.atomic
def staff_edit(request, pk):
    if not is_authorized_staff(request.user):
        raise PermissionDenied
    records = PreRegistration.objects.select_for_update() if request.method == "POST" else PreRegistration.objects
    record = get_object_or_404(records, pk=pk)
    admin = getattr(request.user, "role", "") == "admin"
    if record.is_archived or (record.status == PreRegistration.Status.REJECTED and not admin):
        raise PermissionDenied("This registration is read-only.")
    if request.method == "POST":
        if not admin and any(
            field in request.POST and request.POST[field] != getattr(record, field)
            for field in ("status", "rejection_reason")
        ):
            raise PermissionDenied("Only an Admin can change review status or reason.")
        if (
            request.POST.get("status") == PreRegistration.Status.CONVERTED
            and record.status != PreRegistration.Status.CONVERTED
        ) or (
            record.status == PreRegistration.Status.CONVERTED
            and request.POST.get("status", record.status) != record.status
        ):
            raise PermissionDenied("Official record status can only be set by conversion.")
        if record.status == PreRegistration.Status.REJECTED:
            status = request.POST.get("status")
            if status in PENDING_STATUSES and not record.converted_patient_id and not record.converted_case_id:
                # A rejected intake may itself be invalid. Reopening validates
                # only the transition, never re-cleans or writes its source data.
                PreRegistration.objects.filter(pk=record.pk).update(status=status, updated_at=timezone.now())
                AuditLog.objects.create(
                    user=request.user,
                    action="UPDATE_PRE_REGISTRATION",
                    target_type="pre_registration",
                    target_id=record.pk,
                    description=(
                        f"Reopened online registration {record.pre_registration_number}: rejected -> {status}. "
                        f"Previous rejection by {record.rejected_by} at {record.rejected_at}: {record.rejection_reason}"
                    ),
                )
                messages.success(request, "Online registration reopened as pending. The original rejection history was preserved.")
                return redirect("pre_registrations:staff_detail", pk=record.pk)
            messages.error(request, "Rejected registrations are read-only. An Admin may reopen an unlinked registration as pending.")
            form = StaffPreRegistrationForm(instance=record, user=request.user)
            return render(request, "pre_registrations/staff_form.html", {"form": form, "record": record})
        form = StaffPreRegistrationForm(request.POST, instance=copy(record), user=request.user)
        if form.is_valid():
            status = form.cleaned_data["status"]
            if status == PreRegistration.Status.REJECTED:
                try:
                    reject_pre_registration(record, request.user, form.cleaned_data["rejection_reason"])
                except ValidationError as exc:
                    messages.error(request, " ".join(exc.messages))
                    return _review_redirect(request, record)
            else:
                old_status = record.status
                updated = form.save(commit=False)
                if status == PreRegistration.Status.VERIFIED and old_status != status:
                    updated.verified_by = request.user
                    updated.verified_at = timezone.now()
                updated.save()
                detail = (
                    f"Updated online registration {record.pre_registration_number}: {old_status} -> {status}. "
                    f"Fields: {', '.join(form.changed_data) or 'none'}."
                )
                AuditLog.objects.create(
                    user=request.user,
                    action="UPDATE_PRE_REGISTRATION",
                    target_type="pre_registration",
                    target_id=record.pk,
                    description=detail,
                )
            messages.success(request, "Online registration updated.")
            return redirect("pre_registrations:staff_detail", pk=record.pk)
        messages.error(request, form.errors.as_text())
    else:
        form = StaffPreRegistrationForm(instance=record, user=request.user)
    return render(request, "pre_registrations/staff_form.html", {"form": form, "record": record})


@login_required
@require_POST
def staff_reject(request, pk):
    require_review_admin(request.user)
    record = get_object_or_404(PreRegistration, pk=pk)
    form = RejectPreRegistrationForm(request.POST)
    if form.is_valid():
        try:
            record, changed = reject_pre_registration(record, request.user, form.cleaned_data["reason"])
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            if changed:
                messages.success(
                    request,
                    f"Online registration {record.pre_registration_number} rejected. "
                    "The patient will see the reason when checking its status.",
                )
            else:
                messages.info(request, "This registration was already rejected; the original review was preserved.")
        return _review_redirect(request, record)

    messages.error(request, "Please provide a meaningful reason for rejecting this registration.")
    return render(request, "pre_registrations/staff_detail.html", _detail_context(request, record, form))


@login_required
@require_POST
def convert(request, pk):
    require_review_admin(request.user)
    record = get_object_or_404(PreRegistration, pk=pk)
    try:
        # The service owns one transaction for the fresh state, duplicate check,
        # patient/account lookup, conversion and audit entry.
        patient, case = convert_to_official_case(
            record, request.user, confirm_duplicates=request.POST.get("confirm_duplicates") == "1"
        )
    except DuplicateConfirmationRequired as exc:
        return render(
            request, "pre_registrations/convert_confirm.html",
            {"record": exc.record, "duplicates": exc.duplicates, **_review_navigation(request)},
        )
    except ValidationError as exc:
        messages.error(request, "Conversion blocked: " + " ".join(exc.messages))
        return _review_redirect(request, record)
    except IntegrityError:
        messages.error(request, "Conversion could not be completed because another record changed. Nothing was saved; please review and retry.")
        return _review_redirect(request, record)
    # If auto-created login (patient had no account), surface temp credentials
    # No SMS is sent for pre-registration credentials — patient already provided
    # username/password during public signup; staff shares temp creds verbally if needed.
    temp_user = getattr(patient, "_temp_username", None)
    temp_pass = getattr(patient, "_temp_password", None)
    if temp_user and temp_pass:
        request.session[f'temp_creds_{patient.pk}'] = {
            'username': temp_user,
            'password': temp_pass,
            'created': True,
        }
        messages.success(request, f"Online registration converted to {case.case_number} and patient login created: {temp_user} / Temp: {temp_pass} — please ask patient to change password after login (no SMS sent for credentials).")
        # Also store for case_detail display
        request.session['temp_creds_last'] = {'patient_pk': patient.pk, 'username': temp_user, 'password': temp_pass}
    else:
        messages.success(request, f"Online registration converted to official case {case.case_number}.")
    messages.info(request, "Vaccination schedule is not set automatically — a nurse must trigger it from the case page or Nurse Portal.")
    if request.POST.get("return_to") == "patients":
        return redirect_back(request, "patients:patient_list")
    return redirect("bite_cases:case_detail", pk=case.pk)


@login_required
@user_passes_test(is_authorized_staff)
def staff_slip(request, pk):
    record = get_object_or_404(PreRegistration, pk=pk)
    return render(request, "pre_registrations/slip.html", {"record": record, "public": False})
