import datetime
import csv
import re

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from bite_cases.models import AnimalBiteCase, ExposureClassification
from inventory.models import (
    MedicineInventory, MedicineStockMovement, VaccineInventory,
    VaccineStockMovement,
)
from patients.models import Patient
from vaccination.models import VaccinationSchedule, VaccineDose

from .services import (
    animal_type_distribution, barangay_distribution, completed_schedule_case_ids,
    grouped_medicine_products, grouped_vaccine_products, inventory_summary,
    patient_treatment_summary,
)

U = get_user_model()

ALL_REPORT_URLS = (
    'reports_index',
    'cases_report',
    'patient_treatment_report',
    'patients_report',
    'animal_bite_cases_report',
    'vaccine_report',
    'vaccine_inventory_report',
    'medicine_inventory_report',
    'stock_alerts_report',
    'barangay_report',
    'animal_type_report',
    'missed_appointments',
    'completed_vaccinations',
    'under_treatment_report',
)


def make_user(username, role):
    return U.objects.create_user(username, password='pw12345678', role=role)


def make_patient(number, first, last):
    return Patient.objects.create(
        patient_number=number, first_name=first, last_name=last,
        birthdate=datetime.date(1995, 5, 5), sex='female',
        contact_number='09170000000', address='Test address')


def make_case(number, patient, bite_date, barangay='Poblacion',
              animal='dog', status='under_treatment', animal_other='',
              created_by=None):
    return AnimalBiteCase.objects.create(
        case_number=number, patient=patient, created_by=created_by,
        date_time_bite=bite_date,
        place_of_incident=f'{barangay} road', incident_barangay=barangay,
        animal_type=animal, animal_other=animal_other,
        animal_ownership='owned', animal_vax_status='unknown',
        animal_condition='under_observation', exposure_type='bite',
        body_part_affected='left hand', number_of_wounds=1,
        case_status=status)


class ReportAccessTests(TestCase):

    def setUp(self):
        self.staff = {
            role: make_user(f'role.{role}', role)
            for role in ('admin', 'nurse', 'health_worker', 'encoder')
        }
        self.doctor = make_user('role.doctor', 'doctor')
        self.patient_user = make_user('role.patient', 'patient')

    def test_authorized_staff_can_open_every_report(self):
        for role, user in self.staff.items():
            self.client.force_login(user)
            for name in ALL_REPORT_URLS:
                with self.subTest(role=role, url=name):
                    expected_status = (
                        403 if name == 'barangay_report'
                        and role not in {'admin', 'nurse'} else 200)
                    self.assertEqual(
                        self.client.get(reverse(f'reports:{name}')).status_code,
                        expected_status)

    def test_doctors_and_patients_are_denied(self):
        self.client.force_login(self.doctor)
        for name in ALL_REPORT_URLS:
            with self.subTest(role='doctor', url=name):
                self.assertEqual(
                    self.client.get(reverse(f'reports:{name}')).status_code, 403)

        # Patient accounts are redirected to their portal by middleware.
        self.client.force_login(self.patient_user)
        for name in ALL_REPORT_URLS:
            with self.subTest(role='patient', url=name):
                self.assertIn(
                    self.client.get(reverse(f'reports:{name}')).status_code,
                    (302, 403))

    def test_anonymous_users_are_redirected_to_login(self):
        response = self.client.get(reverse('reports:reports_index'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])


class PatientReportTests(TestCase):

    def setUp(self):
        self.nurse = make_user('rep.nurse', 'nurse')
        self.patient = make_patient('PAT-R0001', 'Maria', 'Santos')
        self.other_patient = make_patient('PAT-R0002', 'Jose', 'Reyes')
        # Same patient, two separate incidents: two cases, one patient.
        self.complete_case = make_case(
            'ABC-R0001', self.patient,
            datetime.datetime(2026, 11, 1, 9, 0),
            barangay='Poblacion', status='under_treatment',
            created_by=self.nurse)
        self.ongoing_case = make_case(
            'ABC-R0002', self.patient,
            datetime.datetime(2026, 11, 3, 10, 0),
            barangay='San Isidro', animal='cat', status='under_treatment',
            created_by=self.nurse)
        self.new_case = make_case(
            'ABC-R0003', self.other_patient,
            datetime.datetime(2026, 10, 20, 8, 0),
            barangay='Poblacion', status='new', created_by=self.nurse)

        # Completed plan: every prescribed dose administered.
        complete_schedule = VaccinationSchedule.objects.create(
            bite_case=self.complete_case, created_by=self.nurse)
        for index in (1, 2):
            VaccineDose.objects.create(
                schedule=complete_schedule, dose_label=f'Dose {index}',
                dose_status='completed', actual_date=datetime.date(2026, 11, index))

        # One dose administered out of two: still under treatment.
        ongoing_schedule = VaccinationSchedule.objects.create(
            bite_case=self.ongoing_case, created_by=self.nurse)
        VaccineDose.objects.create(
            schedule=ongoing_schedule, dose_label='Dose 1',
            dose_status='completed', actual_date=datetime.date(2026, 11, 3))
        VaccineDose.objects.create(
            schedule=ongoing_schedule, dose_label='Dose 2',
            dose_status='scheduled', scheduled_date=datetime.date(2026, 11, 10))

    def _login(self):
        self.client.force_login(self.nurse)

    def test_patients_report_counts_unique_records_and_searches(self):
        unlinked_patient = make_patient(
            'PAT-NC0001', 'NoCase', 'Patient')
        self._login()
        response = self.client.get(reverse('reports:patients_report'), {
            'type': 'all',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], 3)
        self.assertContains(response, 'Patients Report')
        self.assertContains(response, 'Complete list of registered patients.')
        self.assertContains(response, 'Total Patients: <strong class="text-ink">3</strong>', html=False)
        self.assertContains(response, self.patient.patient_number)
        self.assertContains(response, self.other_patient.patient_number)
        self.assertContains(response, unlinked_patient.patient_number)
        self.assertContains(response, 'Actions')
        self.assertContains(response, reverse('patients:patient_detail', args=[self.patient.pk]))
        self.assertContains(response, reverse('patients:patient_print', args=[self.patient.pk]))

        self.patient.sex = 'male'
        self.patient.save(update_fields=['sex'])
        male = self.client.get(reverse('reports:patients_report'), {
            'type': 'all', 'q': 'Male',
        })
        self.assertEqual(male.context['total'], 1)
        self.assertContains(male, self.patient.patient_number)
        self.assertNotContains(male, self.other_patient.patient_number)

    def test_patients_report_period_uses_registration_date(self):
        today = timezone.localdate()
        Patient.objects.filter(pk=self.patient.pk).update(
            created_at=timezone.make_aware(datetime.datetime.combine(
                today, datetime.time(10, 0))))
        Patient.objects.filter(pk=self.other_patient.pk).update(
            created_at=timezone.make_aware(datetime.datetime.combine(
                today - datetime.timedelta(days=1), datetime.time(10, 0))))
        self._login()

        today_response = self.client.get(reverse('reports:patients_report'), {
            'type': 'daily',
        })
        self.assertEqual(today_response.context['total'], 1)
        self.assertContains(today_response, 'Registration date period:')
        all_time_response = self.client.get(reverse('reports:patients_report'), {
            'type': 'all',
        })
        self.assertEqual(all_time_response.context['total'], 2)

    def test_patients_print_includes_complete_filtered_list_and_total(self):
        for index in range(3, 29):
            make_patient(f'PAT-PR{index:04d}', f'Patient{index}', 'Print')
        self._login()
        response = self.client.get(reverse('reports:patients_report'), {
            'type': 'all', 'page': 2, 'export': 'print',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['rows']), 28)
        self.assertContains(response, 'PAT-PR0028')
        self.assertContains(response, 'PATIENTS REPORT')
        self.assertContains(response, 'Total Patients')
        self.assertContains(response, 'Municipal Health Office')
        self.assertContains(response, 'lgu_dumingag_logo.png')

    def test_animal_bite_cases_report_keeps_each_incident(self):
        self._login()
        response = self.client.get(reverse('reports:animal_bite_cases_report'), {
            'type': 'all',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], 3)
        self.assertContains(response, 'Animal Bite Cases Report')
        self.assertContains(response, 'Total Animal Bite Cases:')
        self.assertContains(response, self.complete_case.case_number)
        self.assertContains(response, self.ongoing_case.case_number)
        self.assertEqual(
            self.client.get(reverse('reports:animal_bite_cases_report'), {
                'type': 'all', 'q': 'ABC-R0002',
            }).context['total'],
            1,
        )

    def test_animal_bite_cases_period_uses_incident_date(self):
        today = timezone.localdate()
        make_case(
            'ABC-TODAY1', self.patient,
            datetime.datetime.combine(today, datetime.time(10, 0)),
            created_by=self.nurse,
        )
        self._login()

        today_response = self.client.get(
            reverse('reports:animal_bite_cases_report'), {'type': 'daily'})
        self.assertEqual(today_response.context['total'], 1)
        self.assertContains(today_response, 'Incident date period:')
        all_time_response = self.client.get(
            reverse('reports:animal_bite_cases_report'), {'type': 'all'})
        self.assertEqual(all_time_response.context['total'], 4)

    def test_animal_bite_cases_print_includes_all_incidents_not_current_page(self):
        for index in range(4, 28):
            make_case(
                f'ABC-PR{index:04d}', self.patient,
                datetime.datetime(2026, 11, 5, 9, 0),
                created_by=self.nurse,
            )
        self._login()
        response = self.client.get(reverse('reports:animal_bite_cases_report'), {
            'type': 'all', 'page': 2, 'export': 'print',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['rows']), 27)
        self.assertContains(response, 'ABC-PR0027')
        self.assertContains(response, 'ANIMAL BITE CASES REPORT')
        self.assertContains(response, 'Total Animal Bite Cases')
        self.assertContains(response, 'Municipal Health Office')

    def test_completed_requires_the_full_dose_plan(self):
        completed = completed_schedule_case_ids()
        self.assertIn(self.complete_case.pk, completed)
        self.assertNotIn(self.ongoing_case.pk, completed)

    def test_summary_counts_unique_patients_and_workflow_status(self):
        summary = patient_treatment_summary(AnimalBiteCase.objects.all())
        self.assertEqual(summary['total_patients'], 2)
        self.assertEqual(summary['total_cases'], 3)
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(summary['under_treatment'], 1)

    def test_reports_center_shows_the_six_summary_cards(self):
        self._login()
        response = self.client.get(reverse('reports:reports_index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Total Patients')
        self.assertContains(response, 'Total Bite Cases')
        self.assertContains(response, 'Out of Stock')
        self.assertContains(response, 'Expired Batches')
        self.assertContains(response, 'Patients')
        self.assertContains(response, 'Animal Bite Cases')
        self.assertContains(
            response,
            'Complete list of registered patients and total patient count.')
        self.assertContains(
            response,
            'Complete list of recorded animal bite cases and total case count.')
        self.assertContains(response, reverse('reports:patients_report'))
        self.assertContains(response, reverse('reports:animal_bite_cases_report'))
        self.assertNotContains(response, 'Patient and Treatment')
        for report_name in (
            'vaccine_inventory_report',
            'medicine_inventory_report',
            'stock_alerts_report',
            'vaccine_report',
        ):
            self.assertContains(response, reverse(f'reports:{report_name}'))
        self.assertContains(response, 'Animal Bite Statistics')
        self.assertEqual(response.context['summary']['total_patients'], 2)
        self.assertEqual(response.context['summary']['total_cases'], 3)
        self.assertEqual(response.context['summary']['completed'], 1)

    def test_simplified_patient_report_ignores_custom_date_parameters(self):
        self._login()
        url = reverse('reports:patient_treatment_report')
        response = self.client.get(
            url, {'type': 'custom', 'start_date': '2026-11-01',
                  'end_date': '2026-11-02'})
        self.assertEqual(response.context['summary']['total_cases'], 3)
        self.assertContains(response, 'ABC-R0001')
        self.assertContains(response, 'ABC-R0002')
        self.assertContains(response, 'ABC-R0003')

    def test_simplified_filters_show_six_periods(self):
        self._login()
        response = self.client.get(reverse('reports:patient_treatment_report'))
        for label in ('Daily', 'Weekly', 'Monthly', 'Last 7 days',
                      'Last 30 days', 'Annual'):
            self.assertContains(response, label)
        self.assertNotContains(response, 'All Time')

    def test_cases_report_uses_simplified_filters(self):
        self._login()
        response = self.client.get(reverse('reports:cases_report'), {
            'type': 'all', 'barangay': 'San Isidro', 'animal': 'cat',
            'status': 'completed', 'exposure': 'scratch'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], 3)
        for text in ('Custom range', 'Start date', 'End date',
                     'name="barangay"', 'name="animal"', 'name="status"',
                     'name="exposure"', '>Clear<', '>Apply Filters<'):
            self.assertNotContains(response, text)
        for label in ('Daily', 'Weekly', 'Monthly', 'Last 7 days',
                      'Last 30 days', 'Annual'):
            self.assertContains(response, label)
        self.assertContains(response, 'Search case number or patient')

    def test_cases_report_exports_respect_search(self):
        self._login()
        url = reverse('reports:cases_report')
        params = {'type': 'all', 'q': 'ABC-R0002'}
        csv_response = self.client.get(url, {**params, 'export': 'csv'})
        body = csv_response.content.decode()
        self.assertIn('ABC-R0002', body)
        self.assertNotIn('ABC-R0003', body)
        pdf_response = self.client.get(url, {**params, 'export': 'pdf'})
        self.assertTrue(pdf_response.content.startswith(b'%PDF'))

    def test_missed_report_uses_simplified_filters(self):
        self._login()
        response = self.client.get(
            reverse('reports:missed_appointments'), {'type': 'all'})
        self.assertEqual(response.status_code, 200)
        for text in ('Start date', 'End date', 'Custom', '>Clear<', '>Apply<'):
            self.assertNotContains(response, text)
        for label in ('Today', 'This Week', 'This Month', 'All Time'):
            self.assertContains(response, label)
        self.assertContains(response, 'placeholder="Search..."')

    def test_patient_report_lists_required_columns_and_statuses(self):
        self._login()
        response = self.client.get(
            reverse('reports:patient_treatment_report'), {'type': 'all'})
        self.assertEqual(response.status_code, 200)
        for header in ('Patient Name', 'Patient ID', 'Case Number', 'Barangay',
                       'Animal Type', 'Date of Bite Incident',
                       'Treatment Status'):
            self.assertContains(response, header)
        statuses = {
            case.case_number: case.report_status
            for case in response.context['page_obj']
        }
        self.assertEqual(statuses['ABC-R0001'], 'Completed')
        self.assertEqual(statuses['ABC-R0002'], 'Under Treatment')
        self.assertEqual(statuses['ABC-R0003'], 'New Case')

    def test_patient_report_exports_match_search(self):
        self._login()
        url = reverse('reports:patient_treatment_report')
        params = {'type': 'all', 'q': 'ABC-R0001', 'export': 'csv'}
        csv_response = self.client.get(url, params)
        body = csv_response.content.decode()
        self.assertIn('text/csv', csv_response['Content-Type'])
        self.assertIn('ABC-R0001', body)
        self.assertNotIn('ABC-R0003', body)

        pdf_response = self.client.get(url, {**params, 'export': 'pdf'})
        self.assertEqual(pdf_response['Content-Type'], 'application/pdf')
        self.assertTrue(pdf_response.content.startswith(b'%PDF'))

    def test_simplified_search_ignores_stale_filter_values(self):
        self._login()
        response = self.client.get(reverse('reports:patient_treatment_report'), {
            'type': 'all', 'barangay': 'San Isidro', 'animal': 'cat',
            'status': 'completed'})
        self.assertEqual(response.context['summary']['total_cases'], 3)

    def test_simplified_search_matches_patient_number_and_case_number(self):
        self._login()
        url = reverse('reports:patient_treatment_report')
        by_patient_number = self.client.get(url, {'type': 'all', 'q': 'PAT-R0002'})
        self.assertEqual(by_patient_number.context['summary']['total_cases'], 1)
        self.assertContains(by_patient_number, 'ABC-R0003')

        by_case_number = self.client.get(url, {'type': 'all', 'q': 'ABC-R0002'})
        self.assertEqual(by_case_number.context['summary']['total_cases'], 1)
        self.assertContains(by_case_number, 'ABC-R0002')

    def test_simplified_interface_hides_removed_controls(self):
        self._login()
        response = self.client.get(
            reverse('reports:patient_treatment_report'), {'type': 'all'})
        for text in ('Custom Range', 'Start date', 'End date',
                     'name="barangay"', 'name="animal"', 'name="status"',
                     '>Clear<', '>Apply<'):
            self.assertNotContains(response, text)
        self.assertContains(response, 'Search case number or patient')
        self.assertContains(response, 'e.g., ABC-00001 or Juan Dela Cruz')

    def test_reports_center_hides_removed_controls(self):
        self._login()
        response = self.client.get(reverse('reports:reports_index'), {
            'q': 'Maria', 'type': 'all'})
        for text in ('Custom Range', 'Start date', 'End date',
                     'name="barangay"', 'name="animal"', 'name="status"',
                     '>Clear<', '>Apply<', 'reports-search-field',
                     'Search Field', 'All Fields', 'Clear Search'):
            self.assertNotContains(response, text)
        self.assertContains(response, 'Reporting Period')
        self.assertContains(response, 'Search All Records')
        self.assertContains(
            response,
            'Search patient, sex, barangay, case, animal, status...')
        form = response.content.decode().split(
            '<form id="global-search-form"', 1)[1].split('</form>', 1)[0]
        self.assertEqual(form.count('type="search"'), 1)
        self.assertEqual(form.count('<button'), 1)
        self.assertNotIn('name="search_field"', form)
        self.assertLess(response.content.index(b'Search Results'),
                        response.content.index(b'Reporting Period'))
        self.assertLess(
            response.content.index(b'Search All Records'),
            response.content.index(b'Search Results'))
        self.assertLess(
            response.content.index(b'Reporting Period'),
            response.content.index(b'Total Patients'))

    def test_existing_reports_still_open(self):
        self._login()
        for name in ('cases_report', 'vaccine_report', 'missed_appointments'):
            with self.subTest(url=name):
                self.assertEqual(
                    self.client.get(reverse(f'reports:{name}')).status_code, 200)


class FollowUpReportTests(TestCase):

    def setUp(self):
        self.nurse = make_user('followup.nurse', 'nurse')
        self.today = datetime.date(2026, 11, 12)
        self.patient = make_patient('PAT-FU0001', 'Juan', 'Dela Cruz')
        self.case = make_case(
            'ABC-FU0001', self.patient,
            datetime.datetime(2026, 11, 10, 9, 0),
            status='under_treatment', created_by=self.nurse)
        self.schedule = VaccinationSchedule.objects.create(
            bite_case=self.case, created_by=self.nurse)
        self.missed = VaccineDose.objects.create(
            schedule=self.schedule, dose_label='Dose 1',
            dose_status='scheduled',
            scheduled_date=self.today - datetime.timedelta(days=1),
            vaccine_brand='Verorab')
        self.second_case = make_case(
            'ABC-FU0004', self.patient,
            datetime.datetime(2026, 11, 8, 9, 0),
            status='under_treatment', created_by=self.nurse)
        self.second_schedule = VaccinationSchedule.objects.create(
            bite_case=self.second_case, created_by=self.nurse)
        VaccineDose.objects.create(
            schedule=self.second_schedule, dose_label='Dose 1',
            dose_status='completed',
            scheduled_date=self.today - datetime.timedelta(days=5),
            actual_date=self.today - datetime.timedelta(days=5))
        self.second_missed = VaccineDose.objects.create(
            schedule=self.second_schedule, dose_label='Dose 2',
            dose_status='scheduled',
            scheduled_date=self.today - datetime.timedelta(days=3),
            vaccine_brand='Verorab')
        self.administered = VaccineDose.objects.create(
            schedule=self.schedule, dose_label='Dose 2',
            dose_status='completed',
            scheduled_date=self.today - datetime.timedelta(days=2),
            actual_date=self.today - datetime.timedelta(days=1),
            vaccine_brand='Verorab', administered_by=self.nurse)
        self.due_today = VaccineDose.objects.create(
            schedule=self.schedule, dose_label='Dose 3',
            dose_status='scheduled', scheduled_date=self.today,
            vaccine_brand='Verorab')
        self.cancelled = VaccineDose.objects.create(
            schedule=self.schedule, dose_label='Dose 4',
            dose_status='cancelled',
            scheduled_date=self.today - datetime.timedelta(days=1))
        self.rescheduled_future = VaccineDose.objects.create(
            schedule=self.schedule, dose_label='Dose 5',
            dose_status='rescheduled',
            scheduled_date=self.today + datetime.timedelta(days=1))
        self.late_case = make_case(
            'ABC-FU0002', self.patient,
            datetime.datetime(2026, 11, 11, 9, 0),
            status='under_treatment', created_by=self.nurse)
        self.late_schedule = VaccinationSchedule.objects.create(
            bite_case=self.late_case, created_by=self.nurse)
        self.administered_late = VaccineDose.objects.create(
            schedule=self.late_schedule, dose_label='Dose 1',
            dose_status='completed',
            scheduled_date=self.today - datetime.timedelta(days=5),
            actual_date=self.today, vaccine_brand='Verorab',
            administered_by=self.nurse)
        VaccineDose.objects.create(
            schedule=self.late_schedule, dose_label='Dose 2',
            dose_status='scheduled',
            scheduled_date=self.today + datetime.timedelta(days=3))
        self.complete_case = make_case(
            'ABC-FU0003', self.patient,
            datetime.datetime(2026, 11, 9, 9, 0),
            status='completed', created_by=self.nurse)
        complete_schedule = VaccinationSchedule.objects.create(
            bite_case=self.complete_case, created_by=self.nurse)
        VaccineDose.objects.create(
            schedule=complete_schedule, dose_label='Dose 1',
            dose_status='completed',
            scheduled_date=self.today - datetime.timedelta(days=4),
            actual_date=self.today - datetime.timedelta(days=3),
            vaccine_brand='Verorab', administered_by=self.nurse)
        self.client.force_login(self.nurse)

    def _report(self, name, params=None):
        with patch('reports.views.timezone.localdate', return_value=self.today):
            return self.client.get(reverse(f'reports:{name}'), params or {
                'type': 'all',
            })

    def test_missed_report_only_lists_past_unadministered_uncancelled_doses(self):
        response = self._report('missed_appointments')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], 2)
        self.assertContains(response, 'Missed Appointment')
        self.assertContains(response, '1st Dose')
        self.assertContains(response, '2nd Dose')
        self.assertContains(response, 'ABC-FU0001')
        self.assertContains(response, 'ABC-FU0004')
        self.assertNotContains(response, 'ABC-FU0002')
        self.assertNotContains(response, '3rd Dose')
        self.assertNotContains(response, '4th Dose')
        self.assertNotContains(response, '5th Dose')

    def test_missed_search_matches_all_columns_dates_and_numeric_values(self):
        searches = (
            ('Juan', {'ABC-FU0001', 'ABC-FU0004'}),
            ('ABC-FU0004', {'ABC-FU0004'}),
            ('0917', {'ABC-FU0001', 'ABC-FU0004'}),
            ('2nd Dose', {'ABC-FU0004'}),
            ('Nov 09, 2026', {'ABC-FU0004'}),
            ('11/09/2026', {'ABC-FU0004'}),
            ('2026-11-09', {'ABC-FU0004'}),
            ('Missed', {'ABC-FU0001', 'ABC-FU0004'}),
            ('3', {'ABC-FU0004'}),
            ('13', set()),
            ('2', {'ABC-FU0004'}),
            ('1', {'ABC-FU0001', 'ABC-FU0004'}),
        )
        for keyword, expected_cases in searches:
            with self.subTest(keyword=keyword):
                response = self._report('missed_appointments', {
                    'type': 'all', 'q': keyword,
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['total'], len(expected_cases))
                rows = list(response.context['rows'])
                self.assertEqual(
                    {row['cells'][2] for row in rows}, expected_cases)

    def test_missed_search_ui_empty_state_and_print_respect_search(self):
        no_matches = self._report('missed_appointments', {
            'type': 'all', 'q': 'not found',
        })
        self.assertEqual(no_matches.context['total'], 0)
        self.assertContains(no_matches, 'No matching missed appointments found.')
        self.assertContains(no_matches, '<label', html=False)
        self.assertContains(no_matches, '>Search</label>', html=False)
        self.assertContains(no_matches, 'placeholder="Search..."')
        self.assertEqual(no_matches.content.count(b'type="search"'), 1)
        search_form = no_matches.content.decode().split(
            '<form method="get"', 1)[1].split('</form>', 1)[0]
        self.assertEqual(search_form.count('<button'), 1)
        self.assertContains(no_matches, 'value="not found"')

        printed = self._report('missed_appointments', {
            'type': 'all', 'q': 'ABC-FU0004', 'export': 'print',
        })
        self.assertEqual(printed.status_code, 200)
        self.assertContains(printed, 'ABC-FU0004')
        self.assertNotContains(printed, 'ABC-FU0001')

    def test_completed_search_matches_all_nine_columns(self):
        for keyword in ('Juan', 'ABC-FU0003', 'Verorab', '1st Dose',
                        'followup.nurse', 'Completed'):
            with self.subTest(keyword=keyword):
                response = self._report('completed_vaccinations', {'type': 'all', 'q': keyword})
                self.assertEqual(response.context['total'], 1)
                self.assertEqual(list(response.context['rows'])[0]['cells'][2], 'ABC-FU0003')
        response = self._report('completed_vaccinations', {'type': 'all', 'q': 'ABC-FU0001'})
        self.assertEqual(response.context['total'], 0)

    def test_completed_search_paginates_and_prints_matching_period_records(self):
        schedule = self.complete_case.vaccination_schedule
        for index in range(26):
            VaccineDose.objects.create(
                schedule=schedule, dose_label=f'Extra Dose {index + 1}',
                dose_status='completed', scheduled_date=self.today,
                actual_date=self.today, administered_by=self.nurse,
                vaccine_brand='Rabies Vaccine')
        before = list(VaccineDose.objects.values_list('pk', 'dose_status', 'actual_date'))
        response = self._report('completed_vaccinations', {'type': 'all', 'page': 2})
        self.assertEqual(response.context['total'], 27)
        self.assertEqual(len(list(response.context['rows'])), 2)
        matching = self._report('completed_vaccinations', {'type': 'monthly', 'q': 'Rabies'})
        self.assertEqual(matching.context['total'], 26)
        printed = self._report('completed_vaccinations', {'type': 'all', 'export': 'print'})
        self.assertContains(printed, 'ABC-FU0003')
        self.assertNotContains(printed, 'ABC-FU0001')
        self.assertEqual(before, list(VaccineDose.objects.values_list('pk', 'dose_status', 'actual_date')))

    def test_completed_report_uses_actual_dates_and_preserves_dose_numbers(self):
        response = self._report('completed_vaccinations', {'type': 'all'})
        self.assertEqual(response.context['total'], 1)
        row = list(response.context['rows'])[0]['cells']
        self.assertEqual(row[2], 'ABC-FU0003')
        self.assertEqual(row[4], '1st Dose')
        self.assertEqual(row[8], 'Treatment Course Completed')
        # A case-status flag alone cannot complete outstanding required doses.
        self.case.case_status = 'completed'
        self.case.save(update_fields=['case_status'])
        response = self._report('completed_vaccinations', {'type': 'all'})
        self.assertEqual(response.context['total'], 1)
        # A completed flag without a recorded administration date is insufficient.
        dose = self.complete_case.vaccination_schedule.doses.first()
        dose.actual_date = None
        dose.save(update_fields=['actual_date'])
        response = self._report('completed_vaccinations', {'type': 'all'})
        self.assertEqual(response.context['total'], 0)

    def test_completed_excludes_unscheduled_required_doses(self):
        schedule = self.complete_case.vaccination_schedule
        pending = VaccineDose.objects.create(
            schedule=schedule, dose_label='Dose 2', dose_status='pending')
        response = self._report('completed_vaccinations', {'type': 'all'})
        self.assertEqual(response.context['total'], 0)
        pending.dose_status = 'cancelled'
        pending.save(update_fields=['dose_status'])
        response = self._report('completed_vaccinations', {'type': 'all'})
        self.assertEqual(response.context['total'], 1)

    def test_completed_checks_doses_outside_the_selected_period(self):
        # One dose falls today, but another required dose is still pending.
        response = self._report('completed_vaccinations', {'type': 'daily'})
        self.assertEqual(response.context['total'], 0)
        outstanding = self.late_schedule.doses.exclude(pk=self.administered_late.pk).get()
        outstanding.dose_status = 'completed'
        outstanding.actual_date = self.today - datetime.timedelta(days=1)
        outstanding.save(update_fields=['dose_status', 'actual_date'])
        response = self._report('completed_vaccinations', {'type': 'daily'})
        self.assertEqual(response.context['total'], 1)
        self.assertEqual(list(response.context['rows'])[0]['cells'][2], 'ABC-FU0002')

    def test_under_treatment_counts_remaining_doses_per_case(self):
        response = self._report('under_treatment_report', {'type': 'all'})

        self.assertEqual(response.status_code, 200)
        rows = list(response.context['rows'])
        cases = {row['cells'][2]: row['cells'] for row in rows}
        self.assertEqual(
            set(cases), {'ABC-FU0001', 'ABC-FU0002', 'ABC-FU0004'})
        self.assertEqual(cases['ABC-FU0001'][4:7], [4, 1, 3])
        self.assertEqual(cases['ABC-FU0001'][7], '1st Dose')
        self.assertEqual(cases['ABC-FU0002'][4:7], [2, 1, 1])
        self.assertEqual(cases['ABC-FU0004'][4:7], [2, 1, 1])

    def test_search_period_and_print_use_all_matching_records(self):
        response = self._report('completed_vaccinations', {'type': 'all', 'q': 'ABC-FU0002'})
        self.assertEqual(response.context['total'], 0)
        today_only = self._report('completed_vaccinations', {'type': 'daily'})
        self.assertEqual(today_only.context['total'], 0)
        for export in ('print', 'csv'):
            response = self._report('completed_vaccinations', {'type': 'all', 'export': export})
            self.assertContains(response, 'ABC-FU0003')
            for incomplete in ('ABC-FU0001', 'ABC-FU0002', 'ABC-FU0004'):
                self.assertNotContains(response, incomplete)
        self.assertContains(self._report('completed_vaccinations', {'type': 'all', 'export': 'print'}), 'Municipal Health Office')

    def test_report_cards_and_each_detail_page_are_available(self):
        index = self.client.get(reverse('reports:reports_index'))
        self.assertContains(index, 'Missed Appointments')
        self.assertContains(index, 'Completed Vaccinations')
        self.assertContains(index, 'Under Treatment')
        self.assertContains(index, 'Vaccinated Patients by Dose')
        self.assertContains(index, 'rep-grid--4')
        for name in (
            'missed_appointments',
            'completed_vaccinations',
            'under_treatment_report',
            'vaccinated_by_dose_report',
        ):
            response = self._report(name)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Back to Reports Center')
            self.assertContains(response, 'Print Sheet')
            self.assertEqual(response.content.count(b'type="search"'), 1)

    def test_vaccinated_by_dose_report_groups_and_filters_by_dose(self):
        all_resp = self._report('vaccinated_by_dose_report', {'type': 'all'})
        self.assertEqual(all_resp.status_code, 200)
        self.assertContains(all_resp, 'Vaccinated Patients by Dose')
        self.assertContains(all_resp, '1st Dose')
        self.assertContains(all_resp, '2nd Dose')
        self.assertContains(all_resp, 'Administered')
        breakdown = {item['number']: item for item in all_resp.context['dose_breakdown']}
        self.assertGreaterEqual(breakdown['1']['count'], 1)

        dose_one = self._report('vaccinated_by_dose_report', {'type': 'all', 'dose': '1'})
        self.assertEqual(dose_one.status_code, 200)
        self.assertEqual(dose_one.context['total'], breakdown['1']['count'])

        printed = self._report('vaccinated_by_dose_report', {'type': 'all', 'export': 'print'})
        self.assertEqual(printed.status_code, 200)
        self.assertContains(printed, 'VACCINATED PATIENTS BY DOSE')


class GlobalReportSearchTests(TestCase):

    def setUp(self):
        self.nurse = make_user('global.search.nurse', 'nurse')
        self.male = make_patient('PAT-G0001', 'Aiden', 'Cruz')
        self.male.sex = 'male'
        self.male.middle_name = 'Luis'
        self.male.save(update_fields=['sex', 'middle_name'])
        self.male.barangay = self._barangay('San Pedro')
        self.male.save(update_fields=['barangay'])
        self.female = make_patient('PAT-G0002', 'Maria', 'Santos')
        self.female.barangay = self._barangay('San Isidro')
        self.female.save(update_fields=['barangay'])
        today = timezone.localdate()
        self.male_case = make_case(
            'ABC-G0001', self.male,
            datetime.datetime.combine(today, datetime.time(10, 0)),
            barangay='San Pedro', animal='dog', created_by=self.nurse)
        self.female_case = make_case(
            'ABC-G0002', self.female,
            datetime.datetime.combine(today, datetime.time(11, 0)),
            barangay='San Isidro', animal='cat', status='new',
            created_by=self.nurse)
        self.client.force_login(self.nurse)

    @staticmethod
    def _barangay(name):
        from patients.models import Barangay
        barangay, _ = Barangay.objects.get_or_create(name=name)
        return barangay

    def test_sex_search_is_exact_and_case_insensitive(self):
        url = reverse('reports:reports_index')
        for keyword in ('Male', 'male', 'MALE'):
            with self.subTest(keyword=keyword):
                response = self.client.get(url, {'q': keyword, 'type': 'all'})
                self.assertEqual(response.context['summary']['total_patients'], 1)
                self.assertEqual(response.context['summary']['total_cases'], 1)
                self.assertEqual(
                    [case.case_number for case in response.context['case_page']],
                    ['ABC-G0001'])
                self.assertEqual(
                    [patient.pk for patient in response.context['patient_page']],
                    [self.male.pk])
                self.assertNotIn(
                    self.female.pk,
                    [patient.pk for patient in response.context['patient_page']])

        response = self.client.get(url, {'q': 'Female', 'type': 'all'})
        self.assertEqual(response.context['summary']['total_patients'], 1)
        self.assertEqual(
            [case.case_number for case in response.context['case_page']],
            ['ABC-G0002'])
        self.assertEqual(
            [patient.pk for patient in response.context['patient_page']],
            [self.female.pk])
        self.assertNotIn(
            self.male.pk,
            [patient.pk for patient in response.context['patient_page']])

    def test_patient_report_export_uses_exact_sex_search(self):
        response = self.client.get(
            reverse('reports:patient_treatment_report'),
            {'q': 'male', 'type': 'all', 'export': 'csv'})
        body = response.content.decode()
        self.assertIn('ABC-G0001', body)
        self.assertNotIn('ABC-G0002', body)

    def test_global_search_matches_names_case_barangay_animal_and_status(self):
        url = reverse('reports:reports_index')
        cases = {
            'Aiden Luis Cruz': 'ABC-G0001',
            'ABC-G0002': 'ABC-G0002',
            'San Pedro': 'ABC-G0001',
            'Dog': 'ABC-G0001',
            'Under Treatment': 'ABC-G0001',
        }
        for query, matching_case in cases.items():
            with self.subTest(query=query):
                response = self.client.get(
                    url, {'q': query, 'type': 'all'})
                self.assertEqual(
                    [case.case_number for case in response.context['case_page']],
                    [matching_case])

    def test_global_search_automatically_searches_all_fields(self):
        self.female.first_name = self.male_case.case_number
        self.female.save(update_fields=['first_name'])
        url = reverse('reports:reports_index')

        response = self.client.get(url, {
            'q': self.male_case.case_number,
            'search_field': 'patient_name',
            'type': 'all',
        })
        self.assertEqual(response.context['result_count'], 3)
        self.assertEqual(
            {case.case_number for case in response.context['case_page']},
            {self.male_case.case_number, self.female_case.case_number})
        self.assertEqual(
            [patient.pk for patient in response.context['patient_page']],
            [self.female.pk])

        export = self.client.get(
            reverse('reports:patient_treatment_report'),
            {'q': self.male_case.case_number, 'search_field': 'patient_name',
             'type': 'all', 'export': 'csv'})
        lines = export.content.decode().splitlines()
        self.assertEqual(
            lines[0],
            'Patient,Patient ID,Sex,Case Number,Barangay,Animal,Treatment Status')
        rows = list(csv.reader(lines))
        self.assertEqual(
            [row[3] for row in rows[1:]], [self.female_case.case_number])

    def test_exact_barangay_match_is_case_insensitive_and_not_a_substring(self):
        self.male_case.incident_barangay = 'Balugo'
        self.male_case.save(update_fields=['incident_barangay'])
        self.female_case.incident_barangay = 'Balugo East'
        self.female_case.save(update_fields=['incident_barangay'])
        url = reverse('reports:reports_index')

        for value in ('Balugo', 'balugo', 'BALUGO'):
            with self.subTest(value=value):
                response = self.client.get(url, {
                    'q': value, 'search_field': 'barangay', 'type': 'all'})
                self.assertEqual(response.context['result_count'], 1)
                self.assertEqual(
                    [case.case_number for case in response.context['case_page']],
                    [self.male_case.case_number])
                self.assertContains(response, '<td class="table-cell">Balugo</td>')

        all_fields = self.client.get(url, {'q': 'balugo', 'type': 'all'})
        self.assertEqual(all_fields.context['result_count'], 1)
        self.assertEqual(
            [case.case_number for case in all_fields.context['case_page']],
            [self.male_case.case_number])

    def test_patient_name_returns_each_case_for_that_patient(self):
        make_case(
            'ABC-G0003', self.male,
            datetime.datetime.combine(
                timezone.localdate(), datetime.time(12, 0)),
            barangay='San Pedro', animal='cat', created_by=self.nurse)
        response = self.client.get(reverse('reports:reports_index'), {
            'q': 'aIdEn', 'search_field': 'patient_name', 'type': 'all'})
        self.assertEqual(response.context['result_count'], 3)
        self.assertEqual(
            {case.case_number for case in response.context['case_page']},
            {'ABC-G0001', 'ABC-G0003'})
        self.assertEqual(
            [patient.pk for patient in response.context['patient_page']],
            [self.male.pk])

    def test_reporting_period_links_preserve_search_without_search_category(self):
        response = self.client.get(reverse('reports:reports_index'), {
            'q': 'male', 'search_field': 'sex', 'type': 'monthly'})
        self.assertContains(response, 'href="?type=monthly&q=male"')
        self.assertContains(response, 'href="?type=weekly&q=male"')
        self.assertNotContains(response, 'search_field=')
        self.assertNotContains(response, 'Clear Search')

    def test_reporting_period_filters_matching_cases_by_incident_date(self):
        older_date = timezone.localdate() - datetime.timedelta(days=45)
        old_case = make_case(
            'ABC-G0003', self.male, datetime.datetime.combine(
                older_date, datetime.time(9, 0)),
            barangay='San Pedro', created_by=self.nurse)
        response = self.client.get(reverse('reports:reports_index'), {
            'q': 'Male', 'type': 'monthly'})
        self.assertEqual(response.context['summary']['total_cases'], 1)
        self.assertContains(response, self.male_case.case_number)
        self.assertNotContains(response, old_case.case_number)

    def test_category_and_vaccination_search_use_related_records(self):
        ExposureClassification.objects.create(
            bite_case=self.male_case, category='category_iii')
        response = self.client.get(reverse('reports:reports_index'), {
            'q': 'WHO Category III', 'type': 'all'})
        self.assertContains(response, self.male_case.case_number)
        self.assertNotContains(response, self.female_case.case_number)

        older_date = timezone.localdate() - datetime.timedelta(days=45)
        treated_case = make_case(
            'ABC-G0003', self.male, datetime.datetime.combine(
                older_date, datetime.time(9, 0)),
            barangay='San Pedro', created_by=self.nurse)
        schedule = VaccinationSchedule.objects.create(
            bite_case=treated_case, created_by=self.nurse)
        VaccineDose.objects.create(
            schedule=schedule, dose_label='Dose 1', vaccine_brand='Rabivax Z',
            dose_status='scheduled', scheduled_date=timezone.localdate())

        response = self.client.get(reverse('reports:reports_index'), {
            'q': 'Rabivax Z', 'type': 'monthly'})
        self.assertEqual(response.context['summary']['total_cases'], 1)
        self.assertEqual(
            [case.case_number for case in response.context['case_page']],
            [treated_case.case_number])
        self.assertEqual(response.context['dose_page'].paginator.count, 1)
        self.assertContains(response, 'Vaccination Records')
        self.assertContains(response, 'Rabivax Z')

    def test_inventory_search_returns_actual_batches_without_changing_snapshot(self):
        expiry = timezone.localdate() + datetime.timedelta(days=365)
        vaccine = VaccineInventory.objects.create(
            vaccine_name='Rabies Vaccine', brand='Brand X',
            batch_number='VX-G0001', expiration_date=expiry,
            quantity_received=20, quantity_available=12,
            source_supplier='Acme Supply')
        medicine = MedicineInventory.objects.create(
            medicine_name='Amoxicillin', generic_name='Amoxicillin',
            strength='500 mg', dosage_form='capsule', batch_number='MX-G0001',
            expiration_date=expiry, quantity_received=20,
            quantity_available=12, source_supplier='Acme Supply')
        baseline = self.client.get(reverse('reports:reports_index'), {
            'type': 'all'}).context['inventory']

        response = self.client.get(
            reverse('reports:vaccine_inventory_report'), {'q': 'Rabies Vaccine'})
        self.assertEqual(len(response.context['products']), 1)
        self.assertIn(vaccine, response.context['products'][0]['batches'])

        response = self.client.get(
            reverse('reports:medicine_inventory_report'), {'q': 'Amoxicillin'})
        self.assertEqual(len(response.context['products']), 1)
        self.assertIn(medicine, response.context['products'][0]['batches'])

        for keyword, group, record in (
                ('Rabies Vaccine', 'vaccine_page', vaccine),
                ('Amoxicillin', 'medicine_page', medicine)):
            with self.subTest(keyword=keyword):
                result = self.client.get(
                    reverse('reports:reports_index'),
                    {'q': keyword, 'type': 'all'})
                self.assertIn(record, result.context[group])
                self.assertContains(result, 'Inventory Records')
                self.assertContains(result, keyword)

        current_snapshot = self.client.get(
            reverse('reports:reports_index'), {'type': 'all'}
        ).context['inventory']
        self.assertEqual(current_snapshot, baseline)

    def test_empty_search_results_are_explicit(self):
        response = self.client.get(reverse('reports:reports_index'), {
            'q': 'not-a-real-record', 'type': 'all'})
        self.assertContains(response, 'No matching records found.')
        self.assertContains(response, 'Please try another search keyword.')


class DistributionTests(TestCase):

    def setUp(self):
        self.nurse = make_user('dist.nurse', 'nurse')
        self.patient = make_patient('PAT-D0001', 'Ana', 'Cruz')
        for number, barangay, animal, bite, animal_other in (
            ('ABC-D0001', 'Poblacion', 'dog', datetime.datetime(2026, 11, 1, 9), ''),
            ('ABC-D0002', 'Poblacion', 'dog', datetime.datetime(2026, 11, 2, 9), ''),
            ('ABC-D0003', 'San Isidro', 'cat', datetime.datetime(2026, 11, 3, 9), ''),
            ('ABC-D0004', '', 'other', datetime.datetime(2026, 11, 4, 9), 'Bat'),
        ):
            make_case(number, self.patient, bite, barangay=barangay,
                      animal=animal, animal_other=animal_other,
                      status='new', created_by=self.nurse)

    def test_barangay_distribution_is_ranked_and_handles_blanks(self):
        result = barangay_distribution(AnimalBiteCase.objects.all())
        self.assertEqual(result['rows'][0]['name'], 'Poblacion')
        self.assertEqual(result['rows'][0]['count'], 2)
        self.assertEqual(result['rows'][0]['pct'], 50.0)
        names = [row['name'] for row in result['rows']]
        self.assertIn('Unspecified', names)
        self.assertEqual(result['total'], 4)

    def test_animal_distribution_uses_saved_types(self):
        result = animal_type_distribution(AnimalBiteCase.objects.all())
        self.assertEqual(result['rows'][0]['name'], 'Dog')
        self.assertEqual(result['rows'][0]['count'], 2)
        names = [row['name'] for row in result['rows']]
        self.assertIn('Cat', names)
        self.assertIn('Other', names)
        self.assertEqual(result['total'], 4)

    def test_barangay_and_animal_pages_render(self):
        self.client.force_login(self.nurse)
        barangay = self.client.get(reverse('reports:barangay_report'), {
            'type': 'all',
        })
        self.assertEqual(barangay.status_code, 200)
        self.assertContains(barangay, 'Cases by Barangay Report')
        for heading in ('No.', 'Barangay', 'Total Cases'):
            self.assertContains(barangay, heading)
        summary_table = barangay.content.decode().split(
            '<table', 1)[1].split('</table>', 1)[0]
        self.assertIn('<th class="table-header">Action</th>', summary_table)
        self.assertContains(barangay, 'View cases in Poblacion')
        self.assertContains(barangay, 'View cases in San Isidro')
        self.assertContains(barangay, 'Print Sheet')
        self.assertContains(barangay, 'class="barangay-view-button"')
        self.assertContains(barangay, 'background-color: #0D382C !important')
        self.assertContains(barangay, 'background-color: #0C543F !important')
        self.assertContains(barangay, 'color: #FFFFFF !important')
        self.assertContains(barangay, 'padding: 10px 22px')
        self.assertContains(barangay, 'gap: 8px')
        self.assertContains(barangay, 'Print Preview')
        self.assertContains(barangay, 'Download PDF')
        self.assertContains(barangay, 'Export CSV')
        self.assertContains(barangay, 'Excel')
        self.assertContains(barangay, 'export=xlsx')
        self.assertContains(barangay, 'export=print-sheet')
        self.assertNotContains(barangay, 'Percentage')
        self.assertNotContains(barangay, 'Distribution — horizontal bars')
        self.assertNotContains(barangay, 'ABC-D0001')
        rows = barangay.context['rows']
        self.assertEqual(
            [row['name'] for row in rows],
            ['Poblacion', 'San Isidro', 'Unspecified'])
        self.assertEqual(barangay.context['total'], 4)

        animal = self.client.get(reverse('reports:animal_type_report'))
        self.assertEqual(animal.status_code, 200)
        self.assertContains(animal, 'Cases by Animal Type Report')
        self.assertContains(animal, 'Animal Type Case Summary')
        self.assertContains(animal, 'Action')
        self.assertContains(animal, 'Print Sheet')
        self.assertContains(animal, 'Print Preview')
        self.assertContains(animal, 'Download PDF')
        self.assertContains(animal, 'Export CSV')
        self.assertContains(animal, 'Excel')
        self.assertContains(animal, 'export=xlsx')
        self.assertNotContains(animal, 'Percentage')
        self.assertNotContains(animal, 'Distribution — horizontal bars')

    def test_animal_type_search_summary_and_detail_filtering(self):
        self.client.force_login(self.nurse)
        url = reverse('reports:animal_type_report')
        response = self.client.get(url, {'type': 'all', 'q': 'dog'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], 2)
        self.assertEqual(
            [row['name'] for row in response.context['rows']], ['Dog'])
        self.assertContains(response, 'Search animal type, case number, or patient...')
        self.assertContains(response, 'animal-type/dog/')

        today_response = self.client.get(url, {
            'type': 'daily', 'q': 'dog',
        })
        self.assertEqual(today_response.status_code, 200)
        self.assertEqual(today_response.context['total'], 0)

        detail = self.client.get(reverse(
            'reports:animal_type_detail_report',
            kwargs={'animal_type': 'dog'},
        ), {'type': 'all', 'q': 'ABC-D0001'})
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context['total'], 1)
        self.assertContains(detail, 'ABC-D0001')
        self.assertContains(detail, 'Barangay')
        self.assertNotContains(detail, 'ABC-D0002')
        self.assertNotContains(detail, 'ABC-D0003')
        self.assertContains(detail, 'type=all')
        self.assertContains(detail, 'q=ABC-D0001')

    def test_animal_type_print_sheets_group_all_and_detail_records(self):
        self.client.force_login(self.nurse)
        summary_sheet = self.client.get(reverse('reports:animal_type_report'), {
            'type': 'all', 'export': 'print-sheet',
        })
        self.assertEqual(summary_sheet.status_code, 200)
        self.assertTemplateUsed(
            summary_sheet, 'reports/animal_type_print_sheet.html')
        self.assertEqual(summary_sheet.context['grand_total'], 4)
        self.assertEqual(
            [(group['name'], group['total'])
             for group in summary_sheet.context['groups']],
            [('Cat', 1), ('Dog', 2), ('Other', 1)],
        )
        for text in (
            'Animal Bite Treatment Center', 'Municipal Health Office',
            'CASES BY ANIMAL TYPE REPORT', 'Reporting Period: All time',
            'Date Generated:', 'Cat', 'ABC-D0003', 'Dog', 'ABC-D0001',
            'Other', 'ABC-D0004', 'GRAND TOTAL: 4 CASES',
        ):
            self.assertContains(summary_sheet, text)

        detail_sheet = self.client.get(reverse(
            'reports:animal_type_detail_report',
            kwargs={'animal_type': 'dog'},
        ), {'type': 'all', 'export': 'print-sheet'})
        self.assertEqual(detail_sheet.status_code, 200)
        self.assertEqual(detail_sheet.context['grand_total'], 2)
        self.assertContains(detail_sheet, 'Total Dog Cases: 2')
        self.assertContains(detail_sheet, 'ABC-D0001')
        self.assertContains(detail_sheet, 'ABC-D0002')
        self.assertNotContains(detail_sheet, 'ABC-D0003')

    def test_barangay_detail_shows_only_selected_cases_and_preserves_filters(self):
        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse('reports:barangay_detail_report', kwargs={
                'barangay': 'Poblacion',
            }),
            {'type': 'all', 'q': 'ABC-D0001'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total'], 1)
        self.assertContains(response, 'ABC-D0001')
        self.assertNotContains(response, 'ABC-D0002')
        self.assertNotContains(response, 'ABC-D0003')
        self.assertContains(response, 'Ana Cruz')
        self.assertContains(response, 'Date of Incident')
        self.assertContains(response, 'Exposure Type')
        self.assertContains(response, 'type=all')
        self.assertContains(response, 'q=ABC-D0001')
        self.assertContains(response, 'Print Report')

    def test_barangay_detail_prints_all_selected_period_cases(self):
        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse('reports:barangay_detail_report', kwargs={
                'barangay': 'Poblacion',
            }),
            {'type': 'monthly', 'export': 'print'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'reports/print_official.html')
        for text in (
            'CASES BY BARANGAY REPORT', 'Selected Barangay', 'Poblacion',
            'Reporting Period', 'Date Generated', 'Total Number of Cases',
            'ABC-D0001', 'ABC-D0002', 'Animal Bite Treatment Center',
            'Municipal Health Office',
        ):
            self.assertContains(response, text)
        self.assertNotContains(response, 'ABC-D0003')

    def test_barangay_print_sheet_groups_all_cases_and_totals(self):
        self.client.force_login(self.nurse)
        response = self.client.get(reverse('reports:barangay_report'), {
            'type': 'all', 'export': 'print-sheet',
        })

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'reports/barangay_print_sheet.html')
        self.assertEqual(response.context['grand_total'], 4)
        self.assertEqual(
            [(group['name'], group['total'])
             for group in response.context['groups']],
            [('Poblacion', 2), ('San Isidro', 1), ('Unspecified', 1)],
        )
        for text in (
            'CASES BY BARANGAY REPORT', 'Animal Bite Treatment Center',
            'Municipal Health Office', 'Reporting Period: All time',
            'Date Generated:', 'Barangay: Poblacion', 'ABC-D0001',
            'ABC-D0002', 'Barangay: San Isidro', 'ABC-D0003',
            'GRAND TOTAL: 4 CASES', 'Case Status',
        ):
            self.assertContains(response, text)

    def test_barangay_print_sheet_obeys_period_and_search(self):
        self.client.force_login(self.nurse)
        response = self.client.get(reverse('reports:barangay_report'), {
            'type': 'monthly', 'q': 'ABC-D0001', 'export': 'print-sheet',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['grand_total'], 1)
        self.assertEqual(len(response.context['groups']), 1)
        self.assertEqual(response.context['groups'][0]['name'], 'Poblacion')
        self.assertContains(response, 'Search: ABC-D0001')
        self.assertContains(response, 'ABC-D0001')
        self.assertNotContains(response, 'ABC-D0002')
        self.assertNotContains(response, 'ABC-D0003')

    def test_barangay_detail_rejects_unknown_barangays_and_unauthorized_roles(self):
        self.client.force_login(self.nurse)
        unknown = self.client.get(reverse(
            'reports:barangay_detail_report',
            kwargs={'barangay': 'No Such Barangay'},
        ), {'type': 'all'})
        self.assertEqual(unknown.status_code, 404)

        health_worker = make_user('dist.health.worker', 'health_worker')
        self.client.force_login(health_worker)
        forbidden = self.client.get(reverse('reports:barangay_report'))
        self.assertEqual(forbidden.status_code, 403)
        forbidden_detail = self.client.get(reverse(
            'reports:barangay_detail_report',
            kwargs={'barangay': 'Poblacion'},
        ), {'type': 'all'})
        self.assertEqual(forbidden_detail.status_code, 403)

    def test_distribution_reports_ignore_custom_date_parameters(self):
        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse('reports:barangay_report'),
            {'type': 'custom', 'start_date': '2026-11-01',
             'end_date': '2026-11-02'})
        self.assertEqual(response.context['total'], 4)
        self.assertEqual(response.context['top']['name'], 'Poblacion')

    def test_distribution_reports_ignore_stale_filters(self):
        self.client.force_login(self.nurse)
        response = self.client.get(
            reverse('reports:barangay_report'),
            {'type': 'all', 'animal': 'cat', 'barangay': 'San Isidro'})
        self.assertEqual(response.context['total'], 4)


class PeriodParsingTests(TestCase):

    def test_simplified_periods_resolve_to_expected_ranges(self):
        from .views import _parse_dates

        factory = RequestFactory()
        expected = {
            'daily': (datetime.date(2026, 11, 5), datetime.date(2026, 11, 5)),
            'weekly': (datetime.date(2026, 11, 2), datetime.date(2026, 11, 5)),
            'monthly': (datetime.date(2026, 11, 1), datetime.date(2026, 11, 5)),
            'last7': (datetime.date(2026, 10, 30), datetime.date(2026, 11, 5)),
            'last30': (datetime.date(2026, 10, 7), datetime.date(2026, 11, 5)),
            'annual': (datetime.date(2026, 1, 1), datetime.date(2026, 11, 5)),
        }
        with patch('reports.views.timezone.localdate',
                   return_value=datetime.date(2026, 11, 5)):
            for period, (start, end) in expected.items():
                with self.subTest(period=period):
                    request = factory.get('/reports/', {'type': period})
                    parsed = _parse_dates(request, simplified=True)
                    self.assertEqual(parsed[:3], (start, end, period))

            request = factory.get('/reports/', {
                'type': 'custom', 'start_date': '2026-11-01',
                'end_date': '2026-11-02'})
            start, end, report_type, _label = _parse_dates(
                request, simplified=True)
            self.assertIsNone(start)
            self.assertIsNone(end)
            self.assertEqual(report_type, 'all')


class InventoryReportTests(TestCase):

    def setUp(self):
        self.nurse = make_user('inv.rep.nurse', 'nurse')
        today = timezone.localdate()
        self.future = today + datetime.timedelta(days=180)
        self.past = today - datetime.timedelta(days=10)
        self.live_date = today + datetime.timedelta(days=365)

        # Two batches of the same product: one product row.
        self.v1 = VaccineInventory.objects.create(
            vaccine_name='Verorab', brand='Sanofi', batch_number='V-1',
            expiration_date=self.future, quantity_received=10,
            quantity_available=8, quantity_used=2, reorder_level=5)
        self.v2 = VaccineInventory.objects.create(
            vaccine_name='Verorab', brand='Sanofi', batch_number='V-2',
            expiration_date=self.live_date, quantity_received=5,
            quantity_available=5, quantity_used=0, reorder_level=5)
        self.expired = VaccineInventory.objects.create(
            vaccine_name='Speeda', brand='Other', batch_number='V-3',
            expiration_date=self.past, quantity_received=4,
            quantity_available=4, quantity_used=0, reorder_level=2)
        self.low = VaccineInventory.objects.create(
            vaccine_name='Rabipur', brand='GSK', batch_number='V-4',
            expiration_date=self.future, quantity_received=6,
            quantity_available=2, quantity_used=4, reorder_level=5)

        # Medicines keep different strengths apart and count dispensed units.
        self.med_a = MedicineInventory.objects.create(
            medicine_name='Amoxicillin', generic_name='Amoxicillin',
            strength='500 mg', dosage_form='capsule', unit='capsule',
            batch_number='M-1', expiration_date=self.future,
            quantity_received=20, quantity_available=12,
            quantity_dispensed=8, reorder_level=5)
        self.med_b = MedicineInventory.objects.create(
            medicine_name='Amoxicillin', generic_name='Amoxicillin',
            strength='250 mg', dosage_form='capsule', unit='capsule',
            batch_number='M-2', expiration_date=self.future,
            quantity_received=10, quantity_available=10,
            quantity_dispensed=0, reorder_level=5)
        self.med_empty = MedicineInventory.objects.create(
            medicine_name='Paracetamol', generic_name='Paracetamol',
            strength='500 mg', dosage_form='tablet', unit='tablet',
            batch_number='M-3', expiration_date=self.future,
            quantity_received=10, quantity_available=0,
            quantity_dispensed=10, reorder_level=5)

    def test_vaccine_products_group_batches_and_exclude_expired_availability(self):
        products = grouped_vaccine_products()
        verorab = next(p for p in products if p['name'] == 'Verorab')
        self.assertEqual(verorab['batch_count'], 2)
        self.assertEqual(verorab['total_received'], 15)
        self.assertEqual(verorab['total_used'], 2)
        self.assertEqual(verorab['total_available'], 13)
        self.assertEqual(verorab['stock_status'], 'Available')

        speeda = next(p for p in products if p['name'] == 'Speeda')
        self.assertEqual(speeda['total_available'], 0)
        self.assertEqual(speeda['stock_status'], 'Expired')

        rabipur = next(p for p in products if p['name'] == 'Rabipur')
        self.assertEqual(rabipur['stock_status'], 'Low Stock')

    def test_vaccine_usage_search_matches_name_brand_and_batch(self):
        self.client.force_login(self.nurse)
        url = reverse('reports:vaccine_report')

        by_name = self.client.get(url, {'type': 'all', 'q': 'vErOrAb'})
        self.assertEqual(by_name.status_code, 200)
        self.assertEqual(by_name.context['page_obj'].paginator.count, 2)
        self.assertEqual(by_name.context['total_received'], 15)

        by_brand = self.client.get(url, {'type': 'all', 'q': 'sAnOfI'})
        self.assertEqual(by_brand.context['page_obj'].paginator.count, 2)
        by_batch = self.client.get(url, {'type': 'all', 'q': 'v-2'})
        self.assertEqual(by_batch.context['page_obj'].paginator.count, 1)
        self.assertContains(by_batch, 'V-2')
        self.assertNotContains(by_batch, 'V-1')

    def test_vaccine_usage_search_matches_exact_quantities_and_expiry_dates(self):
        exact_quantity = VaccineInventory.objects.create(
            vaccine_name='Quantity Match', brand='Exact', batch_number='Q-MATCH',
            expiration_date=datetime.date(2027, 11, 9), quantity_received=100,
            quantity_available=98, quantity_used=2,
        )
        nonmatching_quantities = VaccineInventory.objects.create(
            vaccine_name='Quantity Near Match', brand='Exact',
            batch_number='Q-NEAR', expiration_date=self.live_date,
            quantity_received=200, quantity_available=20, quantity_used=12,
        )
        self.client.force_login(self.nurse)
        url = reverse('reports:vaccine_report')

        quantity_response = self.client.get(url, {'type': 'all', 'q': '2'})
        quantity_ids = {
            item.pk for item in quantity_response.context['items'].object_list
        }
        self.assertIn(exact_quantity.pk, quantity_ids)
        self.assertIn(self.low.pk, quantity_ids)
        self.assertNotIn(nonmatching_quantities.pk, quantity_ids)

        for date_query in ('Nov 09, 2027', '2027-11-09', '11/09/2027'):
            with self.subTest(date_query=date_query):
                response = self.client.get(url, {
                    'type': 'all', 'q': date_query,
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['page_obj'].paginator.count, 1)
                self.assertContains(response, 'Q-MATCH')

    def test_vaccine_usage_search_paginates_matching_database_records(self):
        for number in range(21):
            VaccineInventory.objects.create(
                vaccine_name='Pagination Vaccine', brand='Page Test',
                batch_number=f'PAGE-{number}',
                expiration_date=self.live_date, quantity_received=10,
                quantity_available=10,
            )
        VaccineInventory.objects.create(
            vaccine_name='Z Pagination Vaccine', brand='Page Test',
            batch_number='PAGE-TAIL', expiration_date=self.live_date,
            quantity_received=10, quantity_available=10,
        )
        self.client.force_login(self.nurse)

        response = self.client.get(reverse('reports:vaccine_report'), {
            'type': 'all', 'q': 'Pagination Vaccine', 'page': 2,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 22)
        self.assertEqual(len(response.context['items']), 2)
        self.assertContains(response, 'PAGE-TAIL')

    def test_vaccine_usage_search_combines_with_reporting_period(self):
        old_date = timezone.localdate() - datetime.timedelta(days=40)
        self.v1.date_received = timezone.localdate()
        self.v1.save(update_fields=['date_received'])
        self.v2.date_received = old_date
        self.v2.save(update_fields=['date_received'])
        self.client.force_login(self.nurse)

        response = self.client.get(reverse('reports:vaccine_report'), {
            'type': 'last7', 'q': 'verorab',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(response.context['total_received'], 10)
        self.assertContains(response, 'V-1')
        self.assertNotContains(response, 'V-2')

    def test_vaccine_usage_search_ui_and_empty_state(self):
        self.client.force_login(self.nurse)
        response = self.client.get(reverse('reports:vaccine_report'), {
            'type': 'all', 'q': 'not-a-vaccine',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Search All Vaccine Records')
        self.assertContains(response, 'Reporting Period')
        self.assertContains(response, 'No matching vaccine records found.')
        self.assertEqual(response.content.count(b'type="search"'), 1)
        search_form = response.content.decode().split(
            'role="search"', 1)[1].split('</form>', 1)[0]
        self.assertEqual(search_form.count('<button'), 1)
        self.assertIn('name="q"', search_form)
        self.assertIn('name="type"', search_form)
        self.assertNotIn('name="start_date"', search_form)

    def test_medicine_products_keep_strengths_separate(self):
        products = grouped_medicine_products()
        amoxicillin = [p for p in products if p['name'] == 'Amoxicillin']
        self.assertEqual(len(amoxicillin), 2)
        strengths = {p['strength'] for p in amoxicillin}
        self.assertEqual(strengths, {'500 mg', '250 mg'})
        dose_500 = next(p for p in amoxicillin if p['strength'] == '500 mg')
        self.assertEqual(dose_500['total_dispensed'], 8)
        paracetamol = next(p for p in products if p['name'] == 'Paracetamol')
        self.assertEqual(paracetamol['stock_status'], 'Out of Stock')

    def test_inventory_summary_counts_out_of_stock_and_expired_batches(self):
        summary = inventory_summary()
        self.assertEqual(summary['expired_batches'], 1)
        self.assertEqual(summary['out_of_stock'], 2)
        self.assertEqual(summary['low_stock'], 1)

    def test_inventory_pages_render_one_row_per_product(self):
        self.client.force_login(self.nurse)
        response = self.client.get(reverse('reports:vaccine_inventory_report'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verorab')
        self.assertContains(response, 'Batch detail')
        self.assertEqual(
            [p['name'] for p in response.context['products']].count('Verorab'), 1)
        self.assertEqual(response.context['totals']['products'], 3)
        self.assertEqual(response.context['totals']['received'], 25)

        medicine = self.client.get(reverse('reports:medicine_inventory_report'))
        self.assertEqual(medicine.status_code, 200)
        self.assertEqual(medicine.context['totals']['products'], 3)
        self.assertEqual(medicine.context['totals']['dispensed'], 18)

    def test_stock_alert_report_groups_without_changing_stock(self):
        before = list(VaccineInventory.objects.values_list(
            'pk', 'quantity_available', 'quantity_used'))
        self.client.force_login(self.nurse)
        response = self.client.get(reverse('reports:stock_alerts_report'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Low Stock')
        self.assertContains(response, 'Out of Stock')
        self.assertContains(response, 'Expiring Soon')
        self.assertContains(response, 'Expired')
        sections = {s['key']: s for s in response.context['sections']}
        self.assertEqual(len(sections['expired']['rows']), 1)
        self.assertEqual(sections['expired']['rows'][0]['batch'], 'V-3')
        after = list(VaccineInventory.objects.values_list(
            'pk', 'quantity_available', 'quantity_used'))
        self.assertEqual(before, after)

    def test_stock_alert_categories_include_complete_batch_information(self):
        expiring = VaccineInventory.objects.create(
            vaccine_name='Imovax', brand='Sanofi',
            dosage_form='injectable', unit='vial', batch_number='V-SOON',
            expiration_date=timezone.localdate() + datetime.timedelta(days=5),
            quantity_received=8, quantity_available=6, quantity_used=2,
        )
        self.client.force_login(self.nurse)
        response = self.client.get(reverse('reports:stock_alerts_report'))
        self.assertEqual(response.status_code, 200)
        sections = {section['key']: section for section in response.context['sections']}

        low_row = next(
            row for row in sections['low_stock']['rows']
            if row['product'] == 'Rabipur')
        self.assertEqual(low_row['form'], 'Injectable')
        self.assertEqual(low_row['unit'], 'vial')
        self.assertEqual(low_row['available'], 2)
        self.assertEqual(low_row['batch_details'][0]['batch'], 'V-4')
        self.assertEqual(low_row['batch_details'][0]['expiration'], self.future)

        out_row = next(
            row for row in sections['out_of_stock']['rows']
            if row['product'] == 'Paracetamol 500 mg')
        self.assertEqual(out_row['form'], 'Tablet')
        self.assertEqual(out_row['available'], 0)
        self.assertEqual(out_row['batch_details'][0]['batch'], 'M-3')
        self.assertEqual(out_row['batch_details'][0]['expiration'], self.future)

        soon_row = next(
            row for row in sections['expiring']['rows']
            if row['batch'] == 'V-SOON')
        self.assertEqual(soon_row['form'], 'Injectable')
        self.assertEqual(soon_row['unit'], 'vial')
        self.assertEqual(soon_row['available'], 6)
        self.assertEqual(soon_row['expiration'], expiring.expiration_date)

        expired_row = next(
            row for row in sections['expired']['rows']
            if row['batch'] == 'V-3')
        self.assertEqual(expired_row['available'], 0)
        self.assertEqual(expired_row['pending_pull_out'], 4)
        self.assertEqual(expired_row['expiration'], self.past)

        content = response.content.decode()
        for heading in (
                'Medicine / Vaccine', 'Form', 'Unit', 'Batches',
                'Available', 'Expiry'):
            self.assertIn(heading, content)
            self.assertEqual(content.count(f'>{heading}</th>'),
                             sum(bool(section['rows']) for section in sections.values()))
        self.assertIn('pending pull-out', content)

    def test_batch_expiring_today_is_expired_not_expiring_soon(self):
        today = timezone.localdate()
        batch = VaccineInventory.objects.create(
            vaccine_name='Anti-Rabies Vaccine', brand='Verorab',
            dosage_form='injectable', unit='vial', batch_number='TODAY-1',
            expiration_date=today, quantity_received=3, quantity_available=2,
        )
        self.client.force_login(self.nurse)
        response = self.client.get(reverse('reports:stock_alerts_report'))
        sections = {section['key']: section for section in response.context['sections']}
        expired = next(row for row in sections['expired']['rows']
                       if row['batch'] == 'TODAY-1')
        self.assertEqual(expired['available'], 0)
        self.assertEqual(expired['pending_pull_out'], 2)
        self.assertEqual(expired['expiration'], batch.expiration_date)
        self.assertNotIn(
            'TODAY-1',
            [row['batch'] for row in sections['expiring']['rows']],
        )

    def test_pulled_out_stocks_report_includes_medicine_and_vaccine_transactions(self):
        partial_medicine = MedicineInventory.objects.create(
            medicine_name='Paracetamol', generic_name='Acetaminophen',
            strength='500 mg', dosage_form='tablet', unit='tablet',
            batch_number='M-OLD-PARTIAL', expiration_date=self.past,
            quantity_received=10, quantity_available=4,
        )
        MedicineStockMovement.objects.create(
            medicine=partial_medicine, movement_type='expired_pull_out',
            quantity=6, batch_number=partial_medicine.batch_number,
            expiration_date=partial_medicine.expiration_date,
            moved_by=self.nurse, notes='Reason: Expired Stock',
        )
        self.expired.quantity_available = 0
        self.expired.save(update_fields=['quantity_available'])
        VaccineStockMovement.objects.create(
            vaccine=self.expired, movement_type='expired_pull_out',
            quantity=4, batch_number=self.expired.batch_number,
            expiration_date=self.expired.expiration_date,
            moved_by=self.nurse, notes='Reason: Damaged packaging',
        )

        self.client.force_login(self.nurse)
        response = self.client.get(reverse('reports:stock_alerts_report'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Pulled Out Stocks')
        self.assertNotContains(response, 'Completed medicine and vaccine stock pull-out records.')
        sections = {section['key']: section for section in response.context['sections']}
        pulled_out = sections['pulled_out']['rows']
        self.assertEqual(len(pulled_out), 2)
        medicine_row = next(row for row in pulled_out
                            if row['batch'] == 'M-OLD-PARTIAL')
        vaccine_row = next(row for row in pulled_out
                           if row['batch'] == 'V-3')
        self.assertEqual(medicine_row['quantity'], 6)
        self.assertEqual(medicine_row['form'], 'Tablet')
        self.assertEqual(medicine_row['reason'], 'Expired Stock')
        self.assertEqual(vaccine_row['quantity'], 4)
        self.assertEqual(vaccine_row['status'], 'Pulled Out')
        self.assertNotIn(
            'V-3', [row['batch'] for row in sections['expired']['rows']])
        self.assertEqual(
            'M-OLD-PARTIAL',
            [row['batch'] for row in sections['expired']['rows']][0],
        )

    def test_stock_alert_exports_include_completed_pull_out_records(self):
        MedicineStockMovement.objects.create(
            medicine=self.med_a, movement_type='expired_pull_out',
            quantity=3, batch_number='M-1',
            expiration_date=self.med_a.expiration_date, moved_by=self.nurse,
            notes='Reason: Expired Stock',
        )
        self.client.force_login(self.nurse)
        url = reverse('reports:stock_alerts_report')
        csv_response = self.client.get(url, {'export': 'csv'})
        self.assertContains(csv_response, 'Pull-out date:')
        self.assertContains(csv_response, 'Amoxicillin')
        self.assertContains(csv_response, 'Pulled Out')
        for heading in ('Medicine / Vaccine', 'Form', 'Unit', 'Batches',
                        'Available', 'Expiry'):
            self.assertContains(csv_response, heading)
        csv_rows = list(csv.reader(csv_response.content.decode().splitlines()))
        self.assertEqual(csv_rows[0], [
            'Medicine / Vaccine', 'Form', 'Unit', 'Batches', 'Available',
            'Expiry',
        ])
        self.assertTrue(all(len(row) == 6 for row in csv_rows))
        print_response = self.client.get(url, {'export': 'print'})
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, 'Amoxicillin')
        self.assertContains(print_response, 'M-1')
        self.assertContains(print_response, 'Pull-out Date')
        self.assertContains(print_response, 'Transaction Reference')
        pdf_response = self.client.get(url, {'export': 'pdf'})
        self.assertEqual(pdf_response.status_code, 200)
        self.assertTrue(pdf_response.content.startswith(b'%PDF'))

    def test_inventory_exports_are_available(self):
        self.client.force_login(self.nurse)
        for name in ('vaccine_inventory_report', 'medicine_inventory_report',
                     'stock_alerts_report'):
            with self.subTest(url=name):
                csv_response = self.client.get(
                    reverse(f'reports:{name}'), {'export': 'csv'})
                self.assertIn('text/csv', csv_response['Content-Type'])
                pdf_response = self.client.get(
                    reverse(f'reports:{name}'), {'export': 'pdf'})
                self.assertTrue(pdf_response.content.startswith(b'%PDF'))


class OfficialPrintTests(TestCase):
    """Every report Print action and PDF must use the shared ABTC template."""

    def setUp(self):
        self.nurse = make_user('print.nurse', 'nurse')
        self.doctor = make_user('print.doctor', 'doctor')
        self.patient_user = make_user('print.patient', 'patient')
        self.client.force_login(self.nurse)

    def test_every_report_print_route_renders_the_official_document(self):
        for name in ALL_REPORT_URLS[1:]:
            with self.subTest(url=name):
                response = self.client.get(
                    reverse(f'reports:{name}'), {'export': 'print'})
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                for marker in (
                    'Animal Bite Treatment Center',
                    'Municipal Health Office',
                    'print_official.css',
                    'lgu_dumingag_logo.png',
                    'abtc-letterhead',
                    'abtc-footer',
                    'System-Generated Document',
                    'Report Details',
                ):
                    self.assertIn(marker, content)
                self.assertNotIn('<aside', content)

    def test_every_report_pdf_is_a4_portrait(self):
        for name in ALL_REPORT_URLS[1:]:
            with self.subTest(url=name):
                response = self.client.get(
                    reverse(f'reports:{name}'), {'export': 'pdf'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content.startswith(b'%PDF'))
                match = re.search(
                    rb'/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)',
                    response.content)
                self.assertIsNotNone(match)
                width, height = (float(value) for value in match.groups())
                self.assertAlmostEqual(width, 595.28, delta=1)
                self.assertAlmostEqual(height, 841.89, delta=1)
                self.assertLess(width, height)

    def test_print_routes_keep_report_access_control(self):
        url = reverse('reports:cases_report')
        self.client.force_login(self.doctor)
        self.assertEqual(
            self.client.get(url, {'export': 'print'}).status_code, 403)
        self.client.force_login(self.patient_user)
        self.assertIn(
            self.client.get(url, {'export': 'print'}).status_code, (302, 403))
        self.client.logout()
        self.assertEqual(
            self.client.get(url, {'export': 'print'}).status_code, 302)


class ReportToolbarExcelAndSignatoryTests(TestCase):
    """Verify top search bar on vaccinated-by-dose, Print Preview/Export/Excel, and signatories."""

    def setUp(self):
        self.nurse = make_user('report.staff', 'nurse')
        self.nurse.first_name = 'Elena'
        self.nurse.last_name = 'Reyes'
        self.nurse.save(update_fields=['first_name', 'last_name'])
        self.patient = make_patient('PAT-R9001', 'Carlos', 'Mendoza')
        self.case = make_case(
            'ABC-R9001', self.patient, timezone.now(),
            barangay='Poblacion', animal='dog',
        )
        self.client.force_login(self.nurse)

    def _all_report_page_urls(self):
        names = list(ALL_REPORT_URLS[1:]) + ['vaccinated_by_dose_report']
        urls = [reverse(f'reports:{name}') for name in names]
        urls.append(reverse(
            'reports:barangay_detail_report', kwargs={'barangay': 'Poblacion'}))
        urls.append(reverse(
            'reports:animal_type_detail_report', kwargs={'animal_type': 'dog'}))
        return urls

    def test_vaccinated_by_dose_search_bar_is_on_top(self):
        response = self.client.get(
            reverse('reports:vaccinated_by_dose_report'), {'type': 'all'})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('type="search"'), 1)
        search_pos = html.index('id="vaccinated-dose-search"')
        cards_pos = html.index('<!-- Dose summary cards -->')
        period_pos = html.index('Reporting Period')
        dose_filter_pos = html.index('Filter by Dose')
        self.assertLess(search_pos, cards_pos)
        self.assertLess(search_pos, period_pos)
        self.assertLess(search_pos, dose_filter_pos)

    def test_every_report_has_print_preview_export_and_excel_toolbar(self):
        for url in self._all_report_page_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                content = response.content.decode()
                for marker in (
                    'Print Preview', 'openPrintPreview', 'Download PDF',
                    'Export CSV', 'Excel', 'export=xlsx',
                ):
                    self.assertIn(marker, content)

    def test_every_report_exports_valid_xlsx_with_signatories(self):
        import io
        from openpyxl import load_workbook

        for url in self._all_report_page_urls():
            with self.subTest(url=url):
                response = self.client.get(url, {'export': 'xlsx'})
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    'application/vnd.openxmlformats-officedocument.'
                    'spreadsheetml.sheet',
                    response['Content-Type'],
                )
                self.assertIn('.xlsx', response['Content-Disposition'])
                wb = load_workbook(io.BytesIO(response.content))
                ws = wb.active
                values = [
                    str(cell.value)
                    for row in ws.iter_rows()
                    for cell in row
                    if cell.value is not None
                ]
                joined = '\n'.join(values)
                self.assertIn('ANIMAL BITE TREATMENT CENTER', joined)
                self.assertIn('Prepared by:', joined)
                self.assertIn('Approved by:', joined)
                self.assertIn('Elena Reyes', joined)
                self.assertIn('Municipal Health Officer', joined)

    def test_every_report_csv_export_succeeds(self):
        for url in self._all_report_page_urls():
            with self.subTest(url=url):
                response = self.client.get(url, {'export': 'csv'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('text/csv', response['Content-Type'])

    def test_print_preview_and_print_sheets_include_signatories(self):
        # Standard official print preview
        preview = self.client.get(
            reverse('reports:patients_report'),
            {'export': 'print', 'preview': '1'},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview['X-Frame-Options'], 'SAMEORIGIN')
        html = preview.content.decode()
        self.assertNotIn('openPrintDialog', html)
        self.assertIn('Prepared by:', html)
        self.assertIn('Approved by:', html)
        self.assertIn('Elena Reyes', html)
        self.assertIn('Municipal Health Officer', html)

        # Barangay & Animal Type print sheets
        for name in ('barangay_report', 'animal_type_report'):
            sheet = self.client.get(
                reverse(f'reports:{name}'),
                {'export': 'print-sheet', 'preview': '1'},
            )
            self.assertEqual(sheet.status_code, 200)
            self.assertEqual(sheet['X-Frame-Options'], 'SAMEORIGIN')
            sheet_html = sheet.content.decode()
            self.assertNotIn('openPrintDialog', sheet_html)
            self.assertIn('Prepared by:', sheet_html)
            self.assertIn('Approved by:', sheet_html)
            self.assertIn('Elena Reyes', sheet_html)
            self.assertIn('Municipal Health Officer', sheet_html)

    def test_pdf_export_includes_signatories_block(self):
        import base64
        import zlib

        response = self.client.get(
            reverse('reports:patients_report'), {'export': 'pdf'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF'))
        # Decompress PDF content streams (ReportLab uses ASCII85 + FlateDecode)
        extracted = []
        for raw in re.findall(rb'stream\s+(.*?)\s*endstream', response.content, re.S):
            data = raw.strip()
            if data.endswith(b'~>'):
                try:
                    decoded = base64.a85decode(data[:-2])
                    extracted.append(
                        zlib.decompress(decoded).decode('latin-1', errors='ignore'))
                    continue
                except Exception:
                    pass
            try:
                extracted.append(
                    zlib.decompress(data).decode('latin-1', errors='ignore'))
            except zlib.error:
                extracted.append(data.decode('latin-1', errors='ignore'))
        full_text = '\n'.join(extracted)
        self.assertIn('Prepared by:', full_text)
        self.assertIn('Approved by:', full_text)
        self.assertIn('Elena Reyes', full_text)
        self.assertIn('Municipal Health Officer', full_text)
