import datetime
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Patient, VitalSign

U = get_user_model()


class AddVitalSignsFormTests(TestCase):
    """Two-column Add Vital Signs entry form for the Admin portal."""

    def setUp(self):
        self.admin = U.objects.create_user(
            "admin.vitals", password="pw12345678", role="admin",
            first_name="Ada", last_name="Admin")
        self.nurse = U.objects.create_user(
            "nurse.vitals", password="pw12345678", role="nurse",
            first_name="Nora", last_name="Nurse")
        self.patient = Patient.objects.create(
            patient_number="PAT-VL001",
            first_name="Amara",
            last_name="Santos",
            birthdate=datetime.date(1988, 7, 15),
            sex="female",
            contact_number="09171234567",
            address="Zone 1, Poblacion",
        )

    def _add_url(self):
        return reverse("patients:patient_vital_create", args=[self.patient.pk])

    def _get(self, user=None, url=None, params=None):
        self.client.force_login(user or self.admin)
        return self.client.get(url or self._add_url(), params or {})

    def _payload(self, **overrides):
        data = {
            "taken_at": "2026-11-14T09:30",
            "bp_systolic": "120",
            "bp_diastolic": "80",
            "temperature": "36.8",
            "pulse_rate": "72",
            "respiratory_rate": "18",
            "spo2": "98",
            "weight": "68.5",
            "height": "165",
            "bmi": "",
            "pain_score": "2",
            "blood_glucose": "95",
            "glucose_type": "random",
            "general_appearance": "well",
            "notes": "Allergy re-check complete.",
        }
        data.update(overrides)
        return data

    def _bmi_input(self, html):
        return re.search(r'<input[^>]*name="bmi"[^>]*>', html).group(0)

    # ---- Layout ----

    def test_page_uses_the_two_column_section_layout(self):
        html = self._get().content.decode()
        self.assertIn("Add Vital Signs", html)
        self.assertIn(self.patient.patient_number, html)
        self.assertIn("Vital Signs &amp; Baseline", html)
        self.assertIn("Admin Input", html)
        self.assertIn("Vital Measurements", html)
        self.assertIn("Additional Assessment", html)
        self.assertIn("Print Vital Forms", html)
        self.assertIn(
            reverse("patients:patient_vital_form_print", args=[self.patient.pk]),
            html,
        )
        self.assertIn('class="vf-top-grid"', html)
        self.assertEqual(html.count('class="vf-grid"'), 2)

    def test_blood_pressure_fields_are_side_by_side_and_grid_order_is_kept(self):
        html = self._get().content.decode()
        order = [
            "bp_systolic", "bp_diastolic",
            "temperature", "pulse_rate",
            "respiratory_rate", "spo2",
            "weight", "height",
            "bmi", "pain_score",
            "blood_glucose", "glucose_type",
            "general_appearance", "notes",
        ]
        positions = [html.index(f'name="{name}"') for name in order]
        self.assertEqual(positions, sorted(positions))

    def test_taken_by_is_the_authenticated_user_and_read_only(self):
        html = self._get().content.decode()
        self.assertIn("Taken By", html)
        self.assertIn("Ada Admin", html)
        self.assertIn('aria-readonly="true"', html)
        self.assertNotIn("Nora Nurse", html)

        nurse_html = self._get(self.nurse).content.decode()
        self.assertIn("Nora Nurse", nurse_html)

    def test_bmi_is_marked_auto_calculated_read_only(self):
        html = self._get().content.decode()
        bmi_tag = self._bmi_input(html)
        self.assertIn("readonly", bmi_tag)
        self.assertIn("(Auto-calculated)", html)
        self.assertIn("Auto-calculated from Weight and Height.", html)

    def test_print_sheet_info_box_and_pill_actions_are_present(self):
        html = self._get().content.decode()
        self.assertIn(
            "Saved values will automatically appear on the print sheet. "
            "Blank fields will remain empty for manual writing when needed.",
            html,
        )
        self.assertIn('id="vf-save-btn"', html)
        self.assertIn("vf-btn--save", html)
        self.assertIn("vf-btn--cancel", html)
        self.assertIn(">Cancel</a>", html)
        self.assertIn("saveBtn.disabled = true", html)

    def test_cancel_returns_to_the_next_url(self):
        next_url = reverse("patients:patient_detail", args=[self.patient.pk]) + "#vitals"
        response = self._get(params={"next": next_url})
        self.assertContains(response, f'href="{next_url}"')

    def test_nurse_page_keeps_the_existing_header(self):
        html = self._get(self.nurse).content.decode()
        self.assertNotIn("Print Vital Forms", html)
        self.assertIn("Same layout as print sheet", html)
        self.assertIn("View print sheet", html)

    # ---- BMI ----

    def test_bmi_is_calculated_from_weight_and_height(self):
        self.client.force_login(self.admin)
        response = self.client.post(self._add_url(), self._payload())
        self.assertEqual(response.status_code, 302)
        vital = VitalSign.objects.get(patient=self.patient)
        self.assertEqual(vital.bmi, Decimal("25.16"))

    def test_posted_bmi_is_replaced_by_the_calculated_value(self):
        self.client.force_login(self.admin)
        self.client.post(self._add_url(), self._payload(bmi="99"))
        vital = VitalSign.objects.get(patient=self.patient)
        self.assertEqual(vital.bmi, Decimal("25.16"))

    def test_bmi_stays_empty_when_weight_or_height_is_missing(self):
        self.client.force_login(self.admin)
        self.client.post(self._add_url(), self._payload(height="", bmi=""))
        vital = VitalSign.objects.get(patient=self.patient)
        self.assertIsNone(vital.bmi)

    # ---- Validation ----

    def test_out_of_range_measurements_are_rejected_with_field_errors(self):
        cases = {
            "pain_score": ("11", "Pain score must be between 0 and 10."),
            "spo2": ("101", "SpO₂ must be between 70 and 100."),
            "weight": ("500", "Weight must be between 1 and 300."),
            "temperature": ("50", "Temperature must be between 30 and 45."),
        }
        self.client.force_login(self.admin)
        for field, (value, message) in cases.items():
            with self.subTest(field=field):
                VitalSign.objects.all().delete()
                response = self.client.post(
                    self._add_url(), self._payload(**{field: value}))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
                self.assertFalse(
                    VitalSign.objects.filter(patient=self.patient).exists())

    def test_invalid_datetime_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self._add_url(), self._payload(taken_at="not-a-date"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            VitalSign.objects.filter(patient=self.patient).exists())

    def test_entered_values_survive_a_validation_error(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self._add_url(),
            self._payload(weight="500", temperature="37.5"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="37.5"')
        self.assertContains(response, 'value="500"')
        self.assertContains(
            response, "Allergy re-check complete.")

    def test_optional_fields_can_stay_blank(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self._add_url(),
            {
                "taken_at": "2026-11-14T09:30",
                "bp_systolic": "",
                "bp_diastolic": "",
                "temperature": "",
                "pulse_rate": "",
                "respiratory_rate": "",
                "spo2": "",
                "weight": "",
                "height": "",
                "bmi": "",
                "pain_score": "",
                "blood_glucose": "",
                "glucose_type": "",
                "general_appearance": "",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(VitalSign.objects.filter(patient=self.patient).exists())

    # ---- Saving and editing ----

    def test_save_records_values_and_the_signed_in_user(self):
        self.client.force_login(self.admin)
        self.client.post(self._add_url(), self._payload())
        vital = VitalSign.objects.get(patient=self.patient)
        self.assertEqual(vital.bp_systolic, 120)
        self.assertEqual(vital.bp_diastolic, 80)
        self.assertEqual(vital.taken_by, self.admin)
        self.assertEqual(vital.notes, "Allergy re-check complete.")
        self.assertEqual(vital.glucose_type, "random")

    def test_edit_form_shows_saved_values_and_updates_them(self):
        vital = VitalSign.objects.create(
            patient=self.patient,
            taken_at=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0)),
            bp_systolic=118, bp_diastolic=76, temperature=Decimal("36.6"),
            weight=Decimal("68.0"), height=Decimal("165.0"),
            pain_score=2, taken_by=self.admin,
        )
        url = reverse("patients:patient_vital_edit", args=[vital.pk])
        response = self._get(url=url)
        form = response.context["form"]
        self.assertEqual(form["bp_systolic"].value(), 118)
        self.assertEqual(form["bp_diastolic"].value(), 76)
        self.assertIn("Update Vital Signs", response.content.decode())

        self.client.post(url, self._payload(bp_systolic="122"))
        vital.refresh_from_db()
        self.assertEqual(vital.bp_systolic, 122)
        self.assertEqual(vital.bp_diastolic, 80)
        self.assertEqual(vital.taken_by, self.admin)
