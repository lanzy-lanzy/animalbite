import datetime
import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Patient, VitalSign

U = get_user_model()


class VitalFormPrintTests(TestCase):
    """The Admin Add Vital Signs page gains a Print Vital Forms button that opens
    the official A4 form. The form renders the latest saved encounter, the input
    form, other roles, and the Edit page are untouched, and printing never writes
    a VitalSign record."""

    def setUp(self):
        self.admin = U.objects.create_user("admin.vf", password="pw12345678", role="admin")
        self.nurse = U.objects.create_user("nurse.vf", password="pw12345678", role="nurse")
        self.doctor = U.objects.create_user("doc.vf", password="pw12345678", role="doctor")
        self.patient_user = U.objects.create_user("patient.vf", password="pw12345678", role="patient")
        self.patient = Patient.objects.create(
            patient_number="PAT-VF001",
            first_name="Amara",
            last_name="Santos",
            birthdate=datetime.date(1988, 7, 15),
            sex="female",
            contact_number="09171234567",
            address="Zone 1, Poblacion",
        )

    def _add_url(self):
        return reverse("patients:patient_vital_create", args=[self.patient.pk])

    def _print_url(self):
        return reverse("patients:patient_vital_form_print", args=[self.patient.pk])

    def _get(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)

    # ---- Add page header (admin) ----

    def test_admin_add_page_shows_print_button_and_clean_header(self):
        html = self._get(self.admin, self._add_url()).content.decode()
        self.assertIn("Print Vital Forms", html)
        self.assertIn(self._print_url(), html)
        self.assertIn("vital-print-btn", html)
        # Patient ID stays in the main heading.
        self.assertIn(self.patient.patient_number, html)
        self.assertIn("Add Vital Signs", html)
        # Subtitle, separate badge, and View print sheet link are hidden.
        self.assertNotIn("Same layout as print sheet", html)
        self.assertNotIn("View print sheet", html)
        self.assertNotIn("rounded-full bg-sage-50 border border-line text-xs font-bold text-muted", html)

    def test_admin_add_page_preserves_input_form_and_actions(self):
        html = self._get(self.admin, self._add_url()).content.decode()
        for field in ("taken_at", "bp_systolic", "bp_diastolic", "temperature",
                      "pulse_rate", "respiratory_rate", "spo2", "weight", "height",
                      "bmi", "pain_score", "blood_glucose", "glucose_type",
                      "general_appearance", "notes"):
            self.assertIn(f'name="{field}"', html)
        self.assertIn("Save Vital Signs", html)
        self.assertIn(">Cancel</a>", html)

    # ---- Other roles / Edit page keep the original header ----

    def test_nurse_add_page_is_unchanged(self):
        html = self._get(self.nurse, self._add_url()).content.decode()
        self.assertNotIn("Print Vital Forms", html)
        self.assertNotIn(self._print_url(), html)
        self.assertIn("Same layout as print sheet", html)
        self.assertIn("View print sheet", html)

    def test_doctor_add_page_is_unchanged(self):
        html = self._get(self.doctor, self._add_url()).content.decode()
        self.assertNotIn("Print Vital Forms", html)
        self.assertIn("Same layout as print sheet", html)
        self.assertIn("View print sheet", html)

    def test_admin_edit_page_is_unchanged(self):
        vital = VitalSign.objects.create(
            patient=self.patient,
            taken_at=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0)),
            bp_systolic=118, bp_diastolic=76, temperature=36.6,
            taken_by=self.admin,
        )
        url = reverse("patients:patient_vital_edit", args=[vital.pk])
        html = self._get(self.admin, url).content.decode()
        self.assertNotIn("Print Vital Forms", html)
        self.assertIn("Same layout as print sheet", html)
        self.assertIn("Update Vital Signs", html)

    # ---- Print form view ----

    def test_print_form_renders_official_blank_form(self):
        response = self._get(self.admin, self._print_url())
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # Official letterhead + footer.
        self.assertIn("Patient Vital Signs Form", html)
        self.assertIn("Animal Bite Treatment Center", html)
        self.assertIn("Municipal Health Office", html)
        self.assertIn("lgu_dumingag_logo.png", html)
        self.assertIn("abtc-footer", html)
        # Patient information uses the saved record.
        self.assertIn(self.patient.full_name(), html)
        self.assertIn(self.patient.patient_number, html)
        self.assertIn("Female", html)
        self.assertIn("Date of Assessment", html)
        # Every required writable field is present.
        for label in ("Date &amp; Time Taken", "Blood Pressure", "Temperature",
                      "Pulse Rate", "Respiratory Rate", "SpO₂", "Weight", "Height",
                      "BMI", "Pain Scale", "Blood Glucose", "Random", "Fasting",
                      "General Appearance", "Additional Notes / Allergy Re-check"):
            self.assertIn(label, html)
        # A4 portrait formatting.
        self.assertIn("size: A4 portrait", html)

    def test_print_form_fields_are_blank_not_invented(self):
        html = self._get(self.admin, self._print_url()).content.decode()
        # Empty writable boxes, no pre-filled example readings.
        self.assertIn('<div class="vf-box"></div>', html)
        for invented in ("120/80", "36.8", "98.6", "example", "Example"):
            self.assertNotIn(invented, html)
        # No checkbox is selected without a saved value.
        self.assertEqual(html.count('aria-checked="true"'), 0)
        # Placeholder model values never leak into the document.
        for leaked in ("None", "null", "undefined"):
            self.assertNotIn(leaked, html)

    def test_print_form_renders_latest_saved_encounter(self):
        VitalSign.objects.create(
            patient=self.patient,
            taken_at=timezone.make_aware(datetime.datetime(2026, 9, 1, 8, 0)),
            temperature=35.1,
            notes="Earlier encounter note",
            taken_by=self.admin,
        )
        latest = VitalSign.objects.create(
            patient=self.patient,
            taken_at=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0)),
            bp_systolic=128, bp_diastolic=82, temperature=37.4,
            pulse_rate=88, respiratory_rate=18, spo2=98,
            weight=64.5, height=162.0, bmi=24.58, pain_score=3,
            blood_glucose=104.0, glucose_type="fasting",
            general_appearance="ill",
            notes="Latest encounter note",
            taken_by=self.admin,
        )
        html = self._get(self.admin, self._print_url()).content.decode()
        # Values from the newest encounter.
        self.assertIn("10/01/2026", html)
        self.assertIn("128", html)
        self.assertIn("82", html)
        self.assertIn("37.4", html)
        self.assertIn("88", html)
        self.assertIn("18", html)
        self.assertIn("98", html)
        self.assertIn("64.5", html)
        self.assertIn("162", html)
        self.assertIn("24.58", html)
        self.assertIn("104", html)
        self.assertIn("Latest encounter note", html)
        # The older encounter is not mixed into this sheet.
        self.assertNotIn("35.1", html)
        self.assertNotIn("Earlier encounter note", html)
        # Saved checkbox choices are the only ones selected.
        self.assertEqual(
            re.findall(r'aria-checked="(true|false)"', html),
            ["false", "true", "false", "true", "false"],
        )
        self.assertIn("vf-check__box--on", html)
        # The recorder and timestamp are shown in the signature block.
        self.assertIn("admin.vf", html)
        self.assertIn("October 1, 2026 · 9:00 AM", html)
        # Every recorded value came from the saved record, not a copy.
        self.assertEqual(VitalSign.objects.filter(patient=self.patient).count(), 2)
        latest.refresh_from_db()
        self.assertEqual(latest.bp_systolic, 128)

    def test_print_form_leaves_unmeasured_fields_empty(self):
        VitalSign.objects.create(
            patient=self.patient,
            taken_at=timezone.make_aware(datetime.datetime(2026, 10, 2, 10, 30)),
            bp_systolic=118, bp_diastolic=76,
            taken_by=self.admin,
        )
        html = self._get(self.admin, self._print_url()).content.decode()
        self.assertIn("118", html)
        self.assertIn("76", html)
        # Unmeasured boxes stay blank instead of showing None/0.
        self.assertIn('<div class="vf-box"></div>', html)
        for leaked in ("None", "null", "undefined"):
            self.assertNotIn(leaked, html)
        # No checkbox is invented for an unrecorded choice.
        self.assertEqual(html.count('aria-checked="true"'), 0)

    def test_print_form_shows_the_saved_encounter_date(self):
        VitalSign.objects.create(
            patient=self.patient,
            taken_at=timezone.make_aware(datetime.datetime(2026, 9, 12, 10, 0)),
            pulse_rate=74,
            taken_by=self.admin,
        )
        html = self._get(self.admin, self._print_url()).content.decode()
        self.assertIn("Date of Assessment", html)
        self.assertIn("September 12, 2026", html)

    def test_print_form_leaves_assessment_date_blank_without_vitals(self):
        from doctor.models import ClinicalAssessment

        assessment = ClinicalAssessment.objects.create(patient=self.patient)
        ClinicalAssessment.objects.filter(pk=assessment.pk).update(
            encounter_date=timezone.make_aware(datetime.datetime(2026, 9, 12, 10, 0)),
        )
        html = self._get(self.admin, self._print_url()).content.decode()
        # A clinical assessment from another encounter must not fill this sheet.
        self.assertNotIn("September 12, 2026", html)

    def test_print_form_shows_current_local_date_printed(self):
        html = self._get(self.admin, self._print_url()).content.decode()
        self.assertIn("Date Printed:", html)
        # Django renders the date in the configured Asia/Manila timezone.
        local_today = timezone.localtime(timezone.now())
        self.assertIn(str(local_today.year), html)

    def test_print_form_uses_print_safe_a4_css(self):
        html = self._get(self.admin, self._print_url()).content.decode()
        self.assertIn("@page { size: A4 portrait; margin: 8mm; }", html)
        self.assertIn("print-color-adjust: exact", html)
        self.assertIn(".no-print { display: none !important; }", html)
        self.assertIn("box-shadow: none !important", html)

    def test_print_form_works_without_any_vitals_and_creates_none(self):
        self.assertEqual(VitalSign.objects.filter(patient=self.patient).count(), 0)
        response = self._get(self.admin, self._print_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(VitalSign.objects.filter(patient=self.patient).count(), 0)

    def test_print_form_does_not_modify_or_duplicate_existing_vitals(self):
        vital = VitalSign.objects.create(
            patient=self.patient,
            taken_at=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0)),
            bp_systolic=118, bp_diastolic=76, temperature=36.6, notes="Baseline",
            taken_by=self.admin,
        )
        before = VitalSign.objects.filter(patient=self.patient).count()
        self._get(self.admin, self._print_url())
        self._get(self.admin, self._print_url())
        self.assertEqual(VitalSign.objects.filter(patient=self.patient).count(), before)
        vital.refresh_from_db()
        self.assertEqual(vital.bp_systolic, 118)
        self.assertEqual(vital.notes, "Baseline")

    # ---- Access control ----

    def test_print_form_is_admin_only(self):
        # Staff roles reach the view and are denied; the patient role is bounced
        # to its own portal by PatientPortalAccessMiddleware before the view.
        for user in (self.nurse, self.doctor):
            response = self._get(user, self._print_url())
            self.assertEqual(response.status_code, 403)
        response = self._get(self.patient_user, self._print_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:patient_portal"), response.url)

    def test_print_form_requires_login(self):
        response = self.client.get(self._print_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)