from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from doctor.forms import ClinicalAssessmentForm
from doctor import tests as doctor_tests
from inventory.models import VaccineInventory


class TetanusProtocolFormTests(TestCase):
    def setUp(self):
        doctor_tests.EncounterTestCase.setUp(self)
        self.tetanus = VaccineInventory.objects.create(
            vaccine_name='Tetanus Toxoid', batch_number='TT-PLAN',
            expiration_date=timezone.localdate() + timedelta(days=90),
            quantity_received=5, quantity_available=5,
        )

    def form(self, protocol, lot=''):
        data = doctor_tests.EncounterTestCase._payload(self)
        data.update({'assess-vaccine_protocol': protocol, 'assess-tetanus_inventory': str(lot)})
        return ClinicalAssessmentForm(data=data, prefix='assess')

    def test_combined_protocol_records_plan_without_deducting(self):
        form = self.form('rabies_tetanus', self.tetanus.pk)
        self.assertTrue(form.is_valid(), form.errors)
        plan = form.save(commit=False)
        self.assertTrue(plan.tt_indicated)
        self.assertEqual(plan.tetanus_inventory, self.tetanus)
        self.tetanus.refresh_from_db()
        self.assertEqual(self.tetanus.quantity_available, 5)

    def test_rabies_only_clears_optional_tetanus_selection(self):
        form = self.form('rabies_only', self.tetanus.pk)
        self.assertTrue(form.is_valid(), form.errors)
        plan = form.save(commit=False)
        self.assertFalse(plan.tt_indicated)
        self.assertIsNone(plan.tetanus_inventory)

    def test_combined_protocol_requires_tetanus_stock(self):
        form = self.form('rabies_tetanus')
        self.assertFalse(form.is_valid())
        self.assertIn('tetanus_inventory', form.errors)

    def test_stock_selectors_keep_rabies_and_tetanus_separate(self):
        form = self.form('rabies_tetanus', self.inventory.pk)
        self.assertFalse(form.is_valid())
        self.assertIn('tetanus_inventory', form.errors)
        self.assertNotIn(self.tetanus, form.fields['vaccine_inventory'].queryset)
        self.assertNotIn(self.inventory, form.fields['tetanus_inventory'].queryset)
