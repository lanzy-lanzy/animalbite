import os
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase


class SeedDeploymentUsersTests(TestCase):
    def run_seed(self, env):
        output = StringIO()
        with patch.dict(os.environ, env, clear=True):
            call_command("seed_deployment_users", stdout=output)
        return output.getvalue()

    def credentials(self):
        return {
            f"SEED_{role}_{field}": value
            for role in ("ADMIN", "DOCTOR", "NURSE")
            for field, value in (("USERNAME", f"clinic-{role.lower()}"),
                                 ("PASSWORD", "Bamboo-Kite-9274-Strong!"))
        }

    def test_creates_roles_with_hashed_passwords_and_minimum_privileges(self):
        env = self.credentials()
        output = self.run_seed(env)
        for role in ("admin", "doctor", "nurse"):
            user = get_user_model().objects.get(role=role)
            self.assertTrue(user.check_password(env[f"SEED_{role.upper()}_PASSWORD"]))
            self.assertEqual(user.is_staff, role == "admin")
            self.assertEqual(user.is_superuser, role == "admin")
            self.assertNotIn(env[f"SEED_{role.upper()}_PASSWORD"], output)
            self.assertTrue(self.client.login(username=user.username, password=env[f"SEED_{role.upper()}_PASSWORD"]))

    def test_repeat_does_not_reset_existing_password_or_privileges(self):
        env = self.credentials()
        self.run_seed(env)
        user = get_user_model().objects.get(role="nurse")
        user.set_password("Changed-Password-9274!")
        user.is_active = False
        user.save()
        self.run_seed(env)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Changed-Password-9274!"))
        self.assertFalse(user.is_active)
        self.assertEqual(get_user_model().objects.count(), 3)

    def test_unconfigured_is_safe(self):
        self.run_seed({})
        self.assertFalse(get_user_model().objects.exists())

    def test_invalid_or_colliding_credentials_roll_back_all_accounts(self):
        for changes in (
            {"SEED_NURSE_PASSWORD": "123"},
            {"SEED_NURSE_PASSWORD": ""},
            {"SEED_NURSE_USERNAME": "clinic-admin"},
        ):
            with self.subTest(changes=changes), self.assertRaises(CommandError):
                self.run_seed(self.credentials() | changes)
            self.assertFalse(get_user_model().objects.exists())

    def test_existing_other_role_cannot_be_promoted(self):
        user = get_user_model().objects.create_user("clinic-admin", role="encoder")
        with self.assertRaises(CommandError):
            self.run_seed(self.credentials())
        user.refresh_from_db()
        self.assertEqual(user.role, "encoder")
        self.assertFalse(user.is_superuser)
