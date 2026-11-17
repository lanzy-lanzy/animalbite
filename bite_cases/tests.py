"""Tests for admin-only bite case deletion.

A case with clinical or treatment history must be archived so assessments,
schedules, doses and prescriptions survive; only empty cases may be deleted.
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import formats, timezone

from audit.models import AuditLog
from doctor.models import ClinicalAssessment
from patients.models import Patient
from vaccination.models import VaccinationSchedule, VaccineDose

from .models import AnimalBiteCase, ExposureClassification

U = get_user_model()


class BiteCaseDeleteTestCase(TestCase):

    def setUp(self):
        self.admin = U.objects.create_user("admin.cases", password="pw12345678", role="admin")
        self.doctor = U.objects.create_user("doc.cases", password="pw12345678", role="doctor")
        self.nurse = U.objects.create_user("nurse.cases", password="pw12345678", role="nurse")
        self.patient = self._make_patient("PAT-QACASE-01", "Casey")
        self.other_patient = self._make_patient("PAT-QACASE-02", "Other")
        self.case = self._make_case("ABC-QACASE-01", self.patient)
        self.other_case = self._make_case("ABC-QACASE-02", self.other_patient)

    def _make_patient(self, number, first_name):
        return Patient.objects.create(
            patient_number=number, first_name=first_name, last_name="Fixture",
            birthdate=datetime.date(1990, 1, 1), contact_number="09170000000",
            sex="female",
        )

    def _make_case(self, number, patient, **overrides):
        data = {
            "case_number": number,
            "patient": patient,
            "created_by": self.nurse,
            "animal_type": "dog",
            "animal_ownership": "owned",
            "animal_vax_status": "unknown",
            "animal_condition": "under_observation",
            "exposure_type": "bite",
            "body_part_affected": "right hand",
            "number_of_wounds": 1,
            "date_time_bite": timezone.make_aware(datetime.datetime(2026, 10, 30, 9, 0)),
            "date_time_consultation": timezone.make_aware(datetime.datetime(2026, 10, 30, 10, 0)),
        }
        data.update(overrides)
        return AnimalBiteCase.objects.create(**data)

    def _delete(self, user=None, case=None, follow=True, **post):
        self.client.force_login(user or self.admin)
        return self.client.post(
            reverse("bite_cases:case_delete", args=[(case or self.case).pk]),
            post, follow=follow,
        )

    def _messages(self, response):
        return [str(message) for message in response.context["messages"]]


class BiteCaseDeletePermissionTests(BiteCaseDeleteTestCase):

    def test_only_admin_sees_the_delete_button_and_modal(self):
        url = reverse("bite_cases:case_list")
        delete_url = reverse("bite_cases:case_delete", args=[self.case.pk])
        for user, visible in ((self.admin, True), (self.doctor, False), (self.nurse, False)):
            self.client.force_login(user)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            self.assertEqual(delete_url in html, visible, user.role)
            self.assertEqual("Delete Bite Case Record?" in html, visible, user.role)

    def test_non_admin_cannot_delete(self):
        for user in (self.doctor, self.nurse):
            response = self._delete(user=user)
            self.assertRedirects(response, reverse("bite_cases:case_list"))
            self.case.refresh_from_db()
            self.assertEqual(self.case.case_status, "new")
            self.assertFalse(AuditLog.objects.filter(action__endswith="_BITE_CASE").exists())

    def test_get_request_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("bite_cases:case_delete", args=[self.case.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(AnimalBiteCase.objects.filter(pk=self.case.pk).exists())

    def test_login_required(self):
        response = self.client.post(
            reverse("bite_cases:case_delete", args=[self.case.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertTrue(AnimalBiteCase.objects.filter(pk=self.case.pk).exists())


class BiteCaseDeleteBehaviourTests(BiteCaseDeleteTestCase):

    def test_case_without_clinical_records_is_deleted_permanently(self):
        response = self._delete(**{"reason": "Duplicate entry"})
        self.assertRedirects(response, reverse("bite_cases:case_list"))
        self.assertFalse(AnimalBiteCase.objects.filter(pk=self.case.pk).exists())

        log = AuditLog.objects.get(action="DELETE_BITE_CASE")
        self.assertEqual(log.user, self.admin)
        self.assertIsNotNone(log.created_at)
        self.assertIn(self.case.case_number, log.description)
        self.assertIn("Duplicate entry", log.description)
        self.assertTrue(any("permanently deleted" in m for m in self._messages(response)))

    def test_patient_and_other_cases_survive_permanent_deletion(self):
        self._delete()
        self.assertTrue(Patient.objects.filter(pk=self.patient.pk).exists())
        self.assertTrue(AnimalBiteCase.objects.filter(pk=self.other_case.pk).exists())

    def test_case_with_clinical_assessment_is_archived(self):
        assessment = ClinicalAssessment.objects.create(
            patient=self.patient, bite_case=self.case, assessed_by=self.doctor,
            chief_complaint="QA fixture", category_confirmed="category_ii", status="draft",
        )
        response = self._delete(**{"reason": "Erroneous duplicate case"})
        self.assertRedirects(response, reverse("bite_cases:case_list"))

        self.case.refresh_from_db()
        self.assertEqual(self.case.case_status, "archived")
        self.assertTrue(ClinicalAssessment.objects.filter(pk=assessment.pk).exists())

        log = AuditLog.objects.get(action="ARCHIVE_BITE_CASE")
        self.assertEqual(log.user, self.admin)
        self.assertIn(self.case.case_number, log.description)
        self.assertIn("clinical assessment", log.description)
        self.assertIn("Erroneous duplicate case", log.description)
        self.assertTrue(any("archived" in m for m in self._messages(response)))

    def test_vaccination_history_is_preserved_when_archiving(self):
        schedule = VaccinationSchedule.objects.create(
            bite_case=self.case, created_by=self.nurse)
        dose = VaccineDose.objects.create(
            schedule=schedule, dose_label="Day 0",
            scheduled_date=datetime.date(2026, 10, 30),
            actual_date=datetime.date(2026, 10, 30), dose_status="completed",
        )

        self._delete()

        self.case.refresh_from_db()
        self.assertEqual(self.case.case_status, "archived")
        self.assertTrue(VaccinationSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertTrue(VaccineDose.objects.filter(pk=dose.pk).exists())
        log = AuditLog.objects.get(action="ARCHIVE_BITE_CASE")
        self.assertIn("vaccination schedule", log.description)
        self.assertIn("administered vaccine doses", log.description)

    def test_archived_case_leaves_the_active_list_and_admin_can_filter_it(self):
        ClinicalAssessment.objects.create(
            patient=self.patient, bite_case=self.case, assessed_by=self.doctor,
            chief_complaint="QA fixture", category_confirmed="category_ii", status="draft",
        )
        self._delete()

        self.client.force_login(self.admin)
        active = self.client.get(reverse("bite_cases:case_list"))
        self.assertNotContains(active, self.case.case_number)
        self.assertContains(active, self.other_case.case_number)

        archived = self.client.get(reverse("bite_cases:case_list"), {"status": "archived"})
        self.assertContains(archived, self.case.case_number)
        self.assertNotContains(archived, self.other_case.case_number)
        self.assertNotContains(
            archived, reverse("bite_cases:case_delete", args=[self.case.pk]))

    def test_non_admin_cannot_list_or_open_archived_cases(self):
        ClinicalAssessment.objects.create(
            patient=self.patient, bite_case=self.case, assessed_by=self.doctor,
            chief_complaint="QA fixture", category_confirmed="category_ii", status="draft",
        )
        self._delete()

        self.client.force_login(self.doctor)
        listing = self.client.get(reverse("bite_cases:case_list"), {"status": "archived"})
        self.assertNotContains(listing, self.case.case_number)
        detail = self.client.get(
            reverse("bite_cases:case_detail", args=[self.case.pk]), follow=True)
        self.assertRedirects(detail, reverse("bite_cases:case_list"))
        self.assertTrue(any("archived" in m for m in self._messages(detail)))

        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("bite_cases:case_detail", args=[self.case.pk])).status_code,
            200,
        )

    def test_archived_case_cannot_be_edited(self):
        ClinicalAssessment.objects.create(
            patient=self.patient, bite_case=self.case, assessed_by=self.doctor,
            chief_complaint="QA fixture", category_confirmed="category_ii", status="draft",
        )
        self._delete()
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("bite_cases:case_edit", args=[self.case.pk]), follow=True)
        self.assertRedirects(
            response, reverse("bite_cases:case_detail", args=[self.case.pk]))
        self.assertTrue(any("cannot be edited" in m for m in self._messages(response)))
        detail = self.client.get(reverse("bite_cases:case_detail", args=[self.case.pk]))
        self.assertNotContains(
            detail, reverse("bite_cases:case_edit", args=[self.case.pk]))

    def test_already_archived_case_is_not_touched_twice(self):
        self.case.case_status = "archived"
        self.case.save(update_fields=["case_status", "updated_at"])
        response = self._delete()
        self.assertRedirects(response, reverse("bite_cases:case_list"))
        self.assertFalse(AuditLog.objects.filter(action__endswith="_BITE_CASE").exists())
        self.assertTrue(any("already archived" in m for m in self._messages(response)))


class BiteCasePortalAccessVisibilityTests(BiteCaseDeleteTestCase):
    """Portal credentials on the case page are administrator-only. Nurses and
    doctors must receive neither account details nor management actions."""

    def setUp(self):
        super().setUp()
        self.portal = U.objects.create_user(
            "casey.portal", password="pw12345678", role="patient",
            email="casey@example.com")
        self.patient.account = self.portal
        self.patient.save(update_fields=["account"])
        self.reset_url = reverse(
            "patients:patient_reset_password", args=[self.patient.pk])

    def _detail(self, user):
        self.client.force_login(user)
        return self.client.get(
            reverse("bite_cases:case_detail", args=[self.case.pk]))

    def test_admin_case_view_hides_portal_details_and_actions(self):
        content = self._detail(self.admin).content.decode()
        self.assertNotIn(f"Patient Portal: {self.patient.full_name()}", content)
        self.assertNotIn(self.portal.username, content)
        self.assertNotIn(self.reset_url, content)
        self.assertNotIn("No portal login yet", content)

    def test_admin_case_view_keeps_only_bite_incident_details(self):
        content = self._detail(self.admin).content.decode()
        self.assertIn("Bite Location / Body Part", content)
        self.assertIn(self.case.case_number, content)
        self.assertIn("&larr; Back", content)
        for marker in (
            "Quick Actions",
            "Exposure Classification",
            'id="clinical-assessment"',
            "Medical Notes",
            "Set Vaccination Schedule",
            "Vaccination Progress",
            reverse("bite_cases:classify_exposure", args=[self.case.pk]),
            reverse("bite_cases:add_medical_note", args=[self.case.pk]),
        ):
            self.assertNotIn(marker, content)

    def test_admin_case_view_uses_the_three_section_panel(self):
        content = self._detail(self.admin).content.decode()
        for marker in (
            "css/bite_case_sheet.css",
            'class="bcs-wrap"',
            'class="bcs-panel"',
            'class="bcs-header"',
            '<h1 class="bcs-title">Bite Case Details</h1>',
            'class="bcs-headline"',
            'class="bcs-status"',
            'class="bcs-desc"',
            'class="bcs-section"',
            'class="bcs-section-title"',
            'class="bcs-icon"',
            'class="bcs-fields"',
            'class="bcs-field"',
            'class="bcs-label"',
            'class="bcs-value"',
            'class="bcs-btn bcs-btn--primary"',
            'class="bcs-btn-icon"',
            "&larr; Back to Bite Cases",
            "Print Sheet",
            "window.print()",
        ):
            self.assertIn(marker, content)
        for legacy in (
            "patient_detail.css", "pd-tile", "pd-badge", "pd-grid", "pd-section",
            "bcs-doc", "bcs-grid", "bcs-row", "bcs-note",
            "Recent Bite Cases", "View Case",
        ):
            self.assertNotIn(legacy, content)

    def test_admin_case_header_shows_case_number_status_and_summary(self):
        content = self._detail(self.admin).content.decode()
        self.assertIn(
            'class="bcs-head-label">Case Number:</span> '
            f'<span class="bcs-head-value">{self.case.case_number}</span>',
            content,
        )
        self.assertIn(
            'class="bcs-head-label">Status:</span> '
            f'<span class="bcs-status">{self.case.get_case_status_display()}</span>',
            content,
        )
        summary = "%s %s &middot; %s at %s" % (
            self.case.get_animal_type_display(),
            self.case.get_exposure_type_display(),
            formats.date_format(self.case.date_time_bite, "F j, Y"),
            formats.time_format(self.case.date_time_bite, "g:i A"),
        )
        self.assertIn(f'class="bcs-desc">{summary}</p>', content)

    def test_admin_case_sections_appear_in_order(self):
        content = self._detail(self.admin).content.decode()
        positions = [
            content.index(title)
            for title in (
                "Incident Information",
                "Animal Information",
                "Exposure &amp; Wound Information",
            )
        ]
        self.assertEqual(positions, sorted(positions))

    def test_admin_case_fields_follow_their_section_order(self):
        content = self._detail(self.admin).content.decode()
        positions = [
            content.index(f'class="bcs-label">{label}</span>')
            for label in (
                "Patient Name", "Patient Number", "Place of Incident",
                "Barangay", "Date of Consultation", "Animal Type", "Ownership",
                "Animal Condition", "Animal Vaccination Status", "Exposure Type",
                "Bite Location / Body Part", "Number of Wounds",
                "Wound Washed Immediately", "Wound Description",
                "First Aid Given",
            )
        ]
        self.assertEqual(positions, sorted(positions))

    def test_admin_case_view_hides_incident_date_and_time(self):
        content = self._detail(self.admin).content.decode()
        for hidden in ("Date of Incident", "Time of Incident"):
            self.assertNotIn(f'class="bcs-label">{hidden}</span>', content)

    def test_admin_case_view_shows_only_the_selected_case(self):
        content = self._detail(self.admin).content.decode()
        self.assertIn(self.case.case_number, content)
        self.assertNotIn(self.other_case.case_number, content)

    def test_admin_case_view_hides_recording_and_classification_fields(self):
        self.case.initial_consultation_notes = "Referred for anti-rabies vaccination"
        self.case.save(update_fields=["initial_consultation_notes"])
        ExposureClassification.objects.create(
            bite_case=self.case, category="category_ii", classified_by=self.doctor,
        )
        content = self._detail(self.admin).content.decode()
        for hidden in (
            "Recorded By", "Date Recorded", "Last Updated",
            "Initial Consultation Notes", "WHO Bite Category",
        ):
            self.assertNotIn(f'class="bcs-label">{hidden}</span>', content)
        self.assertNotIn("Referred for anti-rabies vaccination", content)
        self.assertNotIn("Category II", content)

    def test_admin_case_view_shows_every_saved_case_field(self):
        self.case.place_of_incident = "Boarding House"
        self.case.incident_barangay = "Caridad"
        self.case.animal_other = "Ferret"
        self.case.body_part_affected = "Left leg"
        self.case.wound_description = "Puncture wounds on the calf"
        self.case.first_aid_given = "Washed with soap and water"
        self.case.save()
        content = self._detail(self.admin).content.decode()
        for value in (
            self.case.case_number,
            self.case.get_case_status_display(),
            self.patient.full_name(),
            self.patient.patient_number,
            "Boarding House", "Caridad", "Dog (Ferret)", "Owned",
            "Under Observation", "Unknown", "Bite", "Left leg",
            "Puncture wounds on the calf", "Washed with soap and water",
        ):
            self.assertIn(value, content)
        for label in (
            "Patient Name", "Patient Number", "Place of Incident", "Barangay",
            "Date of Consultation", "Animal Type", "Ownership", "Animal Condition",
            "Animal Vaccination Status", "Exposure Type",
            "Bite Location / Body Part", "Number of Wounds",
            "Wound Washed Immediately", "Wound Description", "First Aid Given",
        ):
            self.assertIn(f'class="bcs-label">{label}</span>', content)

    def test_admin_case_view_marks_missing_details_instead_of_blank_cells(self):
        content = self._detail(self.admin).content.decode()
        self.assertIn('class="bcs-value bcs-empty">Not Recorded</span>', content)

    def test_nurse_case_view_keeps_full_sections(self):
        content = self._detail(self.nurse).content.decode()
        self.assertIn("Bite Incident Details", content)
        self.assertIn("Quick Actions", content)
        self.assertIn("Exposure Classification", content)
        self.assertIn('id="clinical-assessment"', content)
        self.assertIn("Medical Notes", content)
        self.assertNotIn("pd-section-num", content)
        self.assertNotIn("pd-grid pd-grid-2", content)

    def test_doctor_case_view_uses_the_shared_admin_sheet(self):
        content = self._detail(self.doctor).content.decode()
        for marker in (
            "css/bite_case_sheet.css",
            'class="bcs-wrap"',
            'class="bcs-panel"',
            'class="bcs-header"',
            '<h1 class="bcs-title">Bite Case Details</h1>',
            'class="bcs-headline"',
            'class="bcs-status"',
            'class="bcs-desc"',
            'class="bcs-section"',
            'class="bcs-section-title"',
            'class="bcs-icon"',
            'class="bcs-fields"',
            'class="bcs-field"',
            'class="bcs-label"',
            'class="bcs-value"',
            "Incident Information", "Animal Information",
            "Exposure &amp; Wound Information",
        ):
            self.assertIn(marker, content)
        for label in (
            "Patient Name", "Patient Number", "Place of Incident", "Barangay",
            "Date of Consultation", "Animal Type", "Ownership",
            "Animal Condition", "Animal Vaccination Status", "Exposure Type",
            "Bite Location / Body Part", "Number of Wounds",
            "Wound Washed Immediately", "Wound Description", "First Aid Given",
        ):
            self.assertIn(f'class="bcs-label">{label}</span>', content)
        for removed in (
            "Bite Incident Details", "Quick Actions", "Exposure Classification",
            'id="clinical-assessment"', "Vaccination Progress",
            "Doctor Actions", "bcs-actions-label", "Medical Notes",
            "No notes yet.", "Add medical note", "Add Note",
            "bcs-panel--notes", 'id="medical-notes"',
        ):
            self.assertNotIn(removed, content)
        for url in (
            reverse("doctor:assess_case", args=[self.case.pk]),
            reverse("bite_cases:classify_exposure", args=[self.case.pk]),
            reverse("vaccination:follow_up_create", args=[self.case.pk]),
            reverse("vaccination:observation_create", args=[self.case.pk]),
            reverse("bite_cases:add_medical_note", args=[self.case.pk]),
        ):
            self.assertNotIn(url, content)
        self.assertIn("Back to Bite Cases", content)
        self.assertIn("Print Sheet", content)

    def test_nurse_and_doctor_do_not_see_portal_details_or_actions(self):
        for user in (self.nurse, self.doctor):
            with self.subTest(role=user.role):
                content = self._detail(user).content.decode()
                self.assertNotIn(self.portal.username, content)
                self.assertNotIn(self.portal.email, content)
                self.assertNotIn(self.reset_url, content)
                self.assertNotIn("Patient Portal:", content)
                self.assertNotIn("No portal login yet", content)

    def test_nurse_one_time_credentials_are_not_rendered_or_retained(self):
        self.client.force_login(self.nurse)
        session = self.client.session
        session[f"temp_creds_{self.patient.pk}"] = {
            "username": "secret.portal", "password": "secret-password",
        }
        session.save()

        content = self.client.get(
            reverse("bite_cases:case_detail", args=[self.case.pk])).content.decode()
        self.assertNotIn("secret.portal", content)
        self.assertNotIn("secret-password", content)
        self.assertNotIn(f"temp_creds_{self.patient.pk}", self.client.session)


class BiteCaseListActionTests(BiteCaseDeleteTestCase):
    """The doctor's Bite Cases table exposes one emerald View pill per row."""

    def _list(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("bite_cases:case_list")).content.decode()

    def _view_pill(self, case):
        return '<a href="%s" class="btn-view"><svg' % reverse(
            "bite_cases:case_detail", args=[case.pk])

    def test_doctor_rows_use_the_shared_view_pill_without_edit(self):
        html = self._list(self.doctor)
        for case in (self.case, self.other_case):
            self.assertIn(self._view_pill(case), html)
        self.assertNotIn(reverse("bite_cases:case_edit", args=[self.case.pk]), html)
        self.assertNotIn(">Edit</a>", html)
        for style in ("#157A61", "#0C543F", "9999px", "8px 16px"):
            self.assertIn(style, html)

    def test_admin_keeps_edit_and_the_shared_view_pill(self):
        html = self._list(self.admin)
        self.assertIn(reverse("bite_cases:case_edit", args=[self.case.pk]), html)
        self.assertIn(self._view_pill(self.case), html)

    def test_nurse_keeps_the_doctor_findings_button(self):
        html = self._list(self.nurse)
        self.assertIn("findings-view-btn", html)
        self.assertNotIn('class="btn-view"', html)


class BiteCaseDeleteModalTests(BiteCaseDeleteTestCase):

    def test_modal_copy_and_case_fields_are_rendered(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("bite_cases:case_list"))
        html = response.content.decode()
        self.assertIn("Delete Bite Case Record?", html)
        self.assertIn(
            "Are you sure you want to delete this animal bite case record? "
            "Please confirm before continuing.",
            html,
        )
        for label in ("Case Number", "Patient Name", "Animal Type", "Date of Incident"):
            self.assertIn(label, html)
        self.assertIn("Cancel", html)
        self.assertIn("Confirm Delete", html)
        self.assertIn("csrfmiddlewaretoken", html)


class BiteCaseSidebarBadgeTests(TestCase):
    """The Admin sidebar badge counts only bite cases whose status is New Case."""

    def setUp(self):
        self.admin = U.objects.create_user("admin.badge", password="pw12345678", role="admin")
        self.patient = self._make_patient("PAT-BADGE-01", "Badge")
        self.other_patient = self._make_patient("PAT-BADGE-02", "Other")

    def _make_patient(self, number, first_name):
        return Patient.objects.create(
            patient_number=number, first_name=first_name, last_name="Fixture",
            birthdate=datetime.date(1990, 1, 1), contact_number="09170000000",
            sex="female",
        )

    def _make_case(self, number, patient, status="new"):
        return AnimalBiteCase.objects.create(
            case_number=number,
            patient=patient,
            created_by=self.admin,
            animal_type="dog",
            animal_ownership="owned",
            animal_vax_status="unknown",
            animal_condition="under_observation",
            exposure_type="bite",
            body_part_affected="right hand",
            number_of_wounds=1,
            date_time_bite=timezone.make_aware(datetime.datetime(2026, 10, 30, 9, 0)),
            date_time_consultation=timezone.make_aware(datetime.datetime(2026, 10, 30, 10, 0)),
            case_status=status,
        )

    def _page(self, url_name="bite_cases:case_list", *args):
        self.client.force_login(self.admin)
        return self.client.get(reverse(url_name, args=args))

    def _count(self, response):
        return response.context["new_bite_case_count"]

    def test_single_new_case_shows_a_count_of_one(self):
        self._make_case("ABC-BADGE-01", self.patient)
        response = self._page()
        self.assertEqual(self._count(response), 1)
        self.assertContains(response, 'aria-label="1 new bite case">1</span>')

    def test_each_new_case_is_counted_once(self):
        self._make_case("ABC-BADGE-01", self.patient)
        self._make_case("ABC-BADGE-02", self.patient)
        self._make_case("ABC-BADGE-03", self.other_patient)
        response = self._page()
        self.assertEqual(self._count(response), 3)
        self.assertContains(response, 'aria-label="3 new bite cases">3</span>')

    def test_badge_is_hidden_when_no_case_is_new(self):
        self._make_case("ABC-BADGE-01", self.patient, status="under_treatment")
        self._make_case("ABC-BADGE-02", self.other_patient, status="completed")
        response = self._page()
        self.assertEqual(self._count(response), 0)
        self.assertNotIn("new bite case", response.content.decode())

    def test_non_new_statuses_are_not_counted(self):
        for index, status in enumerate(
            ("under_treatment", "completed", "missed", "referred", "archived"), 1
        ):
            self._make_case(f"ABC-BADGE-{index:02d}", self.patient, status=status)
        self._make_case("ABC-BADGE-99", self.other_patient)
        response = self._page()
        self.assertEqual(self._count(response), 1)
        self.assertContains(response, 'aria-label="1 new bite case">1</span>')

    def test_creating_a_case_through_the_view_increases_the_badge(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._count(self._page()), 0)
        response = self.client.post(reverse("bite_cases:case_create"), {
            "patient": self.patient.pk,
            "date_time_bite": "2026-10-30T09:00",
            "place_of_incident": "Basketball court",
            "incident_barangay": "Poblacion",
            "animal_type": "dog",
            "animal_ownership": "stray",
            "animal_vax_status": "unknown",
            "animal_condition": "missing",
            "exposure_type": "bite",
            "body_part_affected": "Left ankle",
            "number_of_wounds": 1,
        })
        self.assertEqual(response.status_code, 302)
        created = AnimalBiteCase.objects.get()
        self.assertEqual(created.case_status, "new")
        self.assertEqual(self._count(self._page()), 1)

    def test_status_change_decreases_the_badge(self):
        case = self._make_case("ABC-BADGE-01", self.patient)
        self._make_case("ABC-BADGE-02", self.other_patient)
        self.assertEqual(self._count(self._page()), 2)
        case.case_status = "under_treatment"
        case.save(update_fields=["case_status", "updated_at"])
        response = self._page()
        self.assertEqual(self._count(response), 1)
        self.assertNotIn("2 new bite cases", response.content.decode())

    def test_viewing_the_case_list_and_detail_keeps_the_badge(self):
        case = self._make_case("ABC-BADGE-01", self.patient)
        self.assertEqual(self._count(self._page()), 1)
        self.assertEqual(self._count(self._page()), 1)
        detail = self._page("bite_cases:case_detail", case.pk)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(self._count(detail), 1)
        self.assertContains(detail, 'aria-label="1 new bite case">1</span>')

    def test_patients_badge_stays_independent_from_bite_cases(self):
        from pre_registrations.models import PreRegistration

        PreRegistration.objects.create(
            first_name="Pending", last_name="Online",
            birthdate=datetime.date(2000, 1, 1), sex="female",
            contact_number="09170000001", address="Zone 1", barangay="Poblacion",
            medical_history="None", allergy_info="None", previous_rabies_vax="None",
            bite_datetime=timezone.now(), incident_place="Street",
            incident_barangay="Poblacion", animal_type="dog",
            animal_ownership="stray", animal_vaccination_status="unknown",
            animal_condition="alive", exposure_type="bite",
            body_part_affected="Left hand", consent_given=True,
        )
        self._make_case("ABC-BADGE-01", self.patient)
        self._make_case("ABC-BADGE-02", self.other_patient)
        response = self._page()
        self.assertEqual(response.context["pending_online_registration_count"], 1)
        self.assertEqual(self._count(response), 2)
        self.assertContains(response, 'aria-label="1 pending online registration">1</span>')
        self.assertContains(response, 'aria-label="2 new bite cases">2</span>')

    def test_count_is_consistent_across_admin_pages(self):
        self._make_case("ABC-BADGE-01", self.patient)
        self._make_case("ABC-BADGE-02", self.other_patient)
        for url_name in (
            "dashboard:index",
            "patients:patient_list",
            "bite_cases:case_list",
            "vaccination:patient_schedule_list",
            "vaccination:sms_log_list",
            "inventory:vaccine_list",
            "reports:reports_index",
        ):
            with self.subTest(page=url_name):
                response = self._page(url_name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self._count(response), 2)
                self.assertContains(response, 'aria-label="2 new bite cases">2</span>')
