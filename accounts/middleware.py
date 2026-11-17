from django.shortcuts import redirect
from django.urls import reverse


class PatientPortalAccessMiddleware:
    """Keep patient accounts inside patient-facing routes."""

    ALLOWED_PREFIXES = (
        "/patient-portal/",
        "/vaccination/patient-portal/",
        "/patient/report-bite/",
        "/patient/",
        "/pre-register/",
        "/profile/",
        "/logout/",
        "/static/",
        "/media/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if (
            user.is_authenticated
            and user.role == "patient"
            and not request.path.startswith(self.ALLOWED_PREFIXES)
        ):
            return redirect(reverse("accounts:patient_portal"))
        return self.get_response(request)


class PasswordChangeRequiredMiddleware:
    """Hold accounts flagged with a temporary password on the change-password page."""

    EXEMPT_PREFIXES = ("/static/", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if (
            user.is_authenticated
            and user.must_change_password
            and request.path not in (reverse("accounts:change_password"), reverse("accounts:logout"))
            and not request.path.startswith(self.EXEMPT_PREFIXES)
        ):
            return redirect(reverse("accounts:change_password"))
        return self.get_response(request)
