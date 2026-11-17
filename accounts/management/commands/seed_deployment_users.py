import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Create admin, doctor and nurse accounts from SEED_* environment variables."

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        usernames = set()
        for role in ("admin", "doctor", "nurse"):
            prefix = f"SEED_{role.upper()}"
            username = os.environ.get(f"{prefix}_USERNAME", "").strip()
            password = os.environ.get(f"{prefix}_PASSWORD", "")
            email = os.environ.get(f"{prefix}_EMAIL", "").strip()
            if not username and not password and not email:
                self.stdout.write(f"{role}: credentials not configured; skipping.")
                continue
            if not username or not password:
                raise CommandError(f"{prefix}_USERNAME and {prefix}_PASSWORD are both required.")
            if username.casefold() in usernames:
                raise CommandError("Each seed role must use a different username.")
            usernames.add(username.casefold())
            existing = User.objects.filter(username__iexact=username).first()
            if existing:
                if existing.role != role:
                    raise CommandError(f"{role}: username belongs to another role; choose a different username.")
                self.stdout.write(f"{role}: account already exists; unchanged.")
                continue
            user = User(username=username, email=email, role=role)
            try:
                user.full_clean(exclude=["password"])
                validate_password(password, user=user)
            except ValidationError:
                # Never echo validator messages or environment values into logs.
                raise CommandError(f"{role}: invalid account details or password; use a valid username and a strong password.") from None
            User.objects.create_user(
                username=username, email=email, password=password, role=role,
                is_active=True, is_staff=role == "admin", is_superuser=role == "admin",
            )
            self.stdout.write(self.style.SUCCESS(f"{role}: account created."))
