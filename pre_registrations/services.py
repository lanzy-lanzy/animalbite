from copy import copy

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.forms.models import model_to_dict
from django.utils import timezone

from audit.models import AuditLog
from bite_cases.models import AnimalBiteCase
from patients.models import Barangay, Patient
from utils.names import format_person_name

from .models import PreRegistration


def generate_patient_number():
    last = Patient.objects.filter(patient_number__startswith="PAT-").order_by("-id").first()
    sequence = int(last.patient_number.split("-")[1]) + 1 if last else 1
    return f"PAT-{sequence:05d}"


def generate_case_number():
    last = AnimalBiteCase.objects.filter(case_number__startswith="ABC-").order_by("-id").first()
    sequence = int(last.case_number.split("-")[1]) + 1 if last else 1
    return f"ABC-{sequence:05d}"


PENDING_STATUSES = (
    PreRegistration.Status.SUBMITTED,
    PreRegistration.Status.FOR_VERIFICATION,
)
CONVERTIBLE_STATUSES = (*PENDING_STATUSES, PreRegistration.Status.VERIFIED)


def require_review_admin(user):
    if not user.is_authenticated or getattr(user, "role", "") != "admin":
        raise PermissionDenied("Only an Admin can approve or reject online registrations.")


class DuplicateConfirmationRequired(Exception):
    def __init__(self, record, duplicates):
        self.record = record
        self.duplicates = duplicates
        super().__init__("Review possible duplicates before confirming conversion.")


def validate_conversion_source(record):
    """Run intake validators on a copy, never normalizing the saved submission."""
    from .forms import PreRegistrationForm

    candidate = copy(record)
    candidate.full_clean(validate_unique=False)
    data = model_to_dict(record, fields=PreRegistrationForm.Meta.fields)
    local_bite = timezone.localtime(record.bite_datetime)
    data.update(bite_date=local_bite.date(), bite_time=local_bite.time())
    form = PreRegistrationForm(data, instance=candidate)
    if not form.is_valid():
        raise ValidationError({field: list(errors) for field, errors in form.errors.items()})


def conversion_patient(record):
    """Check fresh state/links; a patient-only link can be reused, not a case-only link."""
    if record.is_archived:
        raise ValidationError("Archived registrations cannot be converted.")
    if record.status not in (*CONVERTIBLE_STATUSES, PreRegistration.Status.CONVERTED):
        raise ValidationError("This registration is not eligible for conversion.")
    patient = record.converted_patient if record.converted_patient_id else None
    if patient and record.account_id and patient.account_id != record.account_id:
        raise ValidationError("The linked patient does not match the registration account.")
    if record.converted_case_id:
        if (
            not patient
            or record.converted_case.patient_id != patient.pk
            or record.status != PreRegistration.Status.CONVERTED
        ):
            raise ValidationError("The registration has inconsistent official record links.")
    elif record.status == PreRegistration.Status.CONVERTED:
        raise ValidationError("The converted registration is missing official record links.")
    return patient


@transaction.atomic
def convert_to_official_case(pre_registration, user, *, confirm_duplicates=False):
    require_review_admin(user)
    # Never trust the caller's instance: approve, reject and edit share this lock.
    pre_registration = PreRegistration.objects.select_for_update().get(pk=pre_registration.pk)
    patient = conversion_patient(pre_registration)
    if pre_registration.converted_case_id:
        return patient, pre_registration.converted_case

    # Serialize different registrations for the same existing account before
    # looking up its one-to-one patient, including when no patient exists yet.
    if pre_registration.account_id:
        get_user_model().objects.select_for_update().get(pk=pre_registration.account_id)
        if patient is None:
            patient = Patient.objects.filter(account_id=pre_registration.account_id).first()
    if patient and patient.is_archived:
        raise ValidationError("The linked patient is archived.")
    validate_conversion_source(pre_registration)
    duplicates = list(pre_registration.possible_duplicates())
    if duplicates and not confirm_duplicates:
        raise DuplicateConfirmationRequired(pre_registration, duplicates)

    if patient is None:
        barangay, _ = Barangay.objects.get_or_create(name=pre_registration.barangay, defaults={"is_active": True})
        # Use guardian contact as fallback for emergency if emergency not provided
        emergency_name = pre_registration.emergency_contact_name or pre_registration.guardian_name
        emergency_number = pre_registration.emergency_contact_number or pre_registration.guardian_contact_number
        patient = Patient.objects.create(
            patient_number=generate_patient_number(),
            account=pre_registration.account,
            first_name=format_person_name(pre_registration.first_name),
            middle_name=format_person_name(pre_registration.middle_name),
            last_name=format_person_name(pre_registration.last_name),
            suffix=format_person_name(pre_registration.suffix),
            birthdate=pre_registration.birthdate,
            sex=pre_registration.sex,
            civil_status=pre_registration.civil_status or "single",
            contact_number=pre_registration.contact_number,
            address=pre_registration.address,
            barangay=barangay,
            parent_guardian=pre_registration.guardian_name,
            emergency_contact_name=emergency_name,
            emergency_contact_number=emergency_number,
            medical_history=pre_registration.medical_history,
            allergy_info=pre_registration.allergy_info,
            previous_rabies_vax=pre_registration.previous_rabies_vax,
            remarks=pre_registration.remarks,
            created_by=user,
        )
        # Auto-create patient login if no account linked (e.g., staff-entered pre-reg)
        # No SMS for credentials here: public pre-registrations already provide their own
        # username/password during signup, so SMS is redundant. Staff can share temp
        # creds verbally via the one-time display on the detail page.
        if patient.account_id is None:
            from utils.credentials import create_patient_user
            new_user, temp_pwd, _ = create_patient_user(patient, created_by=user)
            # Credential/number collisions must roll back the entire conversion.
            patient._temp_username = new_user.username
            patient._temp_password = temp_pwd
    case = AnimalBiteCase.objects.create(
        case_number=generate_case_number(),
        patient=patient,
        date_time_bite=pre_registration.bite_datetime,
        place_of_incident=pre_registration.incident_place,
        incident_barangay=pre_registration.incident_barangay,
        animal_type=pre_registration.animal_type,
        animal_ownership=pre_registration.animal_ownership,
        animal_vax_status=pre_registration.animal_vaccination_status,
        animal_condition=pre_registration.animal_condition,
        exposure_type=pre_registration.exposure_type,
        body_part_affected=pre_registration.body_part_affected,
        number_of_wounds=pre_registration.number_of_wounds,
        wound_washed_immediately=pre_registration.wound_washed,
        first_aid_given=pre_registration.first_aid_given,
        initial_consultation_notes=pre_registration.remarks,
        created_by=user,
    )
    # NOTE: the vaccination schedule is intentionally NOT created here.
    # A nurse triggers it once the case has been assessed.
    # Update review metadata only: model.save() also normalizes submitted fields.
    now = timezone.now()
    PreRegistration.objects.filter(pk=pre_registration.pk).update(
        account_id=pre_registration.account_id or patient.account_id,
        status=PreRegistration.Status.CONVERTED,
        verified_by=user,
        verified_at=now,
        converted_patient=patient,
        converted_case=case,
        updated_at=now,
    )
    AuditLog.objects.create(
        user=user,
        action="CONVERT_PRE_REGISTRATION",
        target_type="pre_registration",
        target_id=pre_registration.pk,
        description=f"Converted online registration {pre_registration.pre_registration_number} to case {case.case_number}",
    )
    return patient, case


@transaction.atomic
def reject_pre_registration(record, user, reason):
    """Reject only pending intake; retries retain the first review verbatim."""
    from .forms import validate_rejection_reason

    require_review_admin(user)
    record = PreRegistration.objects.select_for_update().get(pk=record.pk)
    if record.status == PreRegistration.Status.REJECTED:
        return record, False
    if (
        record.is_archived
        or record.status not in PENDING_STATUSES
        or record.converted_patient_id
        or record.converted_case_id
    ):
        raise ValidationError("Only unlinked, pending registrations can be rejected.")
    validate_rejection_reason(reason)
    now = timezone.now()
    PreRegistration.objects.filter(pk=record.pk).update(
        status=PreRegistration.Status.REJECTED,
        rejection_reason=reason,
        rejected_by=user,
        rejected_at=now,
        updated_at=now,
    )
    AuditLog.objects.create(
        user=user,
        action="REJECT_PRE_REGISTRATION",
        target_type="pre_registration",
        target_id=record.pk,
        description=f"Rejected online registration {record.pre_registration_number}: {reason}",
    )
    record.refresh_from_db()
    return record, True


class RegistrationDeletionBlocked(Exception):
    def __init__(self, labels):
        self.labels = labels
        super().__init__("; ".join(labels))


def registration_retention_blockers(record):
    """Why this registration must be retained instead of permanently deleted.

    Only unconverted pending submissions may be erased. Anything approved,
    linked to an official patient/case, or already finalised is an intake
    record the clinic keeps, so it can only be archived.
    """
    labels = []
    if record.converted_patient_id:
        labels.append("linked patient profile")
    if record.converted_case_id:
        labels.append("official bite case")
    if record.status == PreRegistration.Status.VERIFIED:
        labels.append("approved registration awaiting conversion")
    elif record.status not in PENDING_STATUSES:
        labels.append(f"{record.get_status_display().lower()} registration")
    return labels


@transaction.atomic
def delete_pre_registration(record, deleted_by):
    record = PreRegistration.objects.select_for_update().get(pk=record.pk)
    blockers = registration_retention_blockers(record)
    if blockers:
        raise RegistrationDeletionBlocked(blockers)
    number = record.pre_registration_number
    record.delete()
    AuditLog.objects.create(
        user=deleted_by,
        action="DELETE_PRE_REGISTRATION",
        description=(
            f"Permanently deleted online registration {number} by admin "
            f"{deleted_by.username}. Linked patient and clinical records were preserved."
        ),
    )
    return number


@transaction.atomic
def archive_pre_registration(record, archived_by):
    record = PreRegistration.objects.select_for_update().get(pk=record.pk)
    already_archived = record.is_archived
    if not already_archived:
        record.is_archived = True
        record.save(update_fields=["is_archived", "updated_at"])
        AuditLog.objects.create(
            user=archived_by,
            action="ARCHIVE_PRE_REGISTRATION",
            target_type="pre_registration",
            target_id=record.pk,
            description=(
                f"Archived online registration {record.pre_registration_number} by admin "
                f"{archived_by.username}. The record was retained and removed from active lists."
            ),
        )
    return already_archived


@transaction.atomic
def retrieve_pre_registration(record, retrieved_by):
    record = PreRegistration.objects.select_for_update().get(pk=record.pk)
    was_archived = record.is_archived
    if was_archived:
        record.is_archived = False
        record.save(update_fields=["is_archived", "updated_at"])
        AuditLog.objects.create(
            user=retrieved_by,
            action="RETRIEVE_PRE_REGISTRATION",
            target_type="pre_registration",
            target_id=record.pk,
            description=(
                f"Retrieved online registration {record.pre_registration_number} by admin "
                f"{retrieved_by.username}. The record was restored to the active list."
            ),
        )
    return was_archived
