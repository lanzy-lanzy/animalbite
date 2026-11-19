from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import VaccineInventory
from vaccination import tests as vaccination_tests


class OptionalTetanusAdministrationTests(TestCase):
    def setUp(self):
        vaccination_tests.NurseVaccinationManageTests.setUp(self)
        self.tetanus = VaccineInventory.objects.create(
            vaccine_name='Tetanus Toxoid', brand='TT', batch_number='TT-01',
            expiration_date=timezone.localdate() + timedelta(days=90),
            quantity_received=5, quantity_available=5,
        )
        self.assessment.tt_indicated = True
        self.assessment.tetanus_inventory = self.tetanus
        self.assessment.save(update_fields=['tt_indicated', 'tetanus_inventory'])
        self.client.force_login(self.nurse)

    def administer(self, selected=False, lot=None, dose=None):
        payload = {
            'actual_date': timezone.localdate().isoformat(),
            'vaccine_brand': 'Verorab', 'batch_number': 'LOT-V1',
            'tetanus_inventory': str((lot or self.tetanus).pk),
        }
        if selected:
            payload['include_tetanus'] = 'on'
        return self.client.post(reverse('vaccination:dose_administer',
                                       args=[(dose or self.first_dose).pk]), payload)

    def stocks(self):
        self.vaccine.refresh_from_db()
        self.tetanus.refresh_from_db()
        self.first_dose.refresh_from_db()
        return self.vaccine.quantity_available, self.tetanus.quantity_available

    def test_rabies_only_ignores_posted_tetanus_lot(self):
        self.administer()
        self.assertEqual(self.stocks(), (9, 5))
        self.assertFalse(self.first_dose.tetanus_administered)

    def test_selected_tetanus_deducts_both_once_and_records_exact_lot(self):
        self.administer(selected=True)
        self.assertEqual(self.stocks(), (9, 4))
        self.assertTrue(self.first_dose.tetanus_administered)
        self.assertEqual(self.first_dose.tetanus_inventory, self.tetanus)
        self.assertEqual(self.first_dose.tetanus_batch_number, 'TT-01')
        self.assertEqual(self.first_dose.tetanus_vaccine_name, 'Tetanus Toxoid')
        self.schedule.refresh_from_db()
        self.assertTrue(self.schedule.tetanus_toxoid_given)
        self.administer(selected=True)
        self.assertEqual(self.stocks(), (9, 4))

    def test_empty_expired_or_wrong_product_rolls_back_both(self):
        for changes in (
            {'quantity_available': 0},
            {'expiration_date': timezone.localdate()},
            {'vaccine_name': 'Rabies Vaccine'},
        ):
            with self.subTest(changes=changes):
                VaccineInventory.objects.filter(pk=self.tetanus.pk).update(
                    quantity_available=5, expiration_date=timezone.localdate() + timedelta(days=90),
                    vaccine_name='Tetanus Toxoid')
                VaccineInventory.objects.filter(pk=self.tetanus.pk).update(**changes)
                before = VaccineInventory.objects.get(pk=self.tetanus.pk).quantity_available
                self.administer(selected=True)
                self.assertEqual(self.stocks(), (10, before))
                self.assertNotEqual(self.first_dose.dose_status, 'completed')
                self.assertIsNone(self.first_dose.actual_date)

    def test_missing_rabies_stock_rolls_back_completion_and_tetanus(self):
        self.vaccine.quantity_available = 0
        self.vaccine.save(update_fields=['quantity_available'])
        self.administer(selected=True)
        self.assertEqual(self.stocks(), (0, 5))
        self.assertIsNone(self.first_dose.actual_date)

    def test_tetanus_requires_doctor_selection(self):
        self.assessment.tt_indicated = False
        self.assessment.save(update_fields=['tt_indicated'])
        self.administer(selected=True)
        self.assertEqual(self.stocks(), (10, 5))

    def test_each_administration_requires_separate_selection(self):
        self.administer(selected=True)
        second = self.schedule.doses.exclude(pk=self.first_dose.pk).order_by('pk').first()
        second.scheduled_date = timezone.localdate()
        second.save(update_fields=['scheduled_date'])
        self.administer(selected=False, dose=second)
        self.assertEqual(self.stocks(), (8, 4))
        second.refresh_from_db()
        self.assertFalse(second.tetanus_administered)

    def test_viewing_management_does_not_deduct_stock(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Rabies + Tetanus for this administration')
        self.assertContains(response, 'TT-01')
        self.assertEqual(self.stocks(), (10, 5))
