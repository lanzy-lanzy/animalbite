from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate, get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST
from utils.credentials import generate_temp_password
from .forms import AccountPasswordChangeForm, LoginForm, ProfileEditForm, UserCreateForm, UserEditForm

User = get_user_model()

def is_admin(user):
    return user.is_authenticated and user.role == 'admin'

def landing_view(request):
    if request.user.is_authenticated:
        if request.user.role == 'patient':
            return redirect('accounts:patient_portal')
        if request.user.role == 'doctor':
            return redirect('doctor:dashboard')
        if request.user.role == 'nurse':
            return redirect('nurse:dashboard')
        return redirect('dashboard:index')
    return render(request, 'accounts/landing.html')


def login_view(request):
    if request.user.is_authenticated:
        if request.user.role == 'patient':
            return redirect('accounts:patient_portal')
        if request.user.role == 'doctor':
            return redirect('doctor:dashboard')
        if request.user.role == 'nurse':
            return redirect('nurse:dashboard')
        return redirect('dashboard:index')
    form = LoginForm()
    if request.method == 'POST':
        form = LoginForm(data=request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            user = authenticate(username=username, password=password)
            if user and user.is_active:
                login(request, user)
                from audit.models import AuditLog
                AuditLog.objects.create(user=user, action='LOGIN', description=f'User {user.username} logged in')
                messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
                if user.role == 'patient':
                    return redirect('accounts:patient_portal')
                if user.role == 'doctor':
                    return redirect('doctor:dashboard')
                if user.role == 'nurse':
                    return redirect('nurse:dashboard')
                return redirect('dashboard:index')
            else:
                messages.error(request, 'Account is inactive.')
        else:
            messages.error(request, 'Invalid username or password.')
    return render(request, 'accounts/login.html', {'form': form})

def logout_view(request):
    if request.user.is_authenticated:
        from audit.models import AuditLog
        AuditLog.objects.create(user=request.user, action='LOGOUT', description=f'User {request.user.username} logged out')
    logout(request)
    return redirect('accounts:login')

@login_required
def profile_view(request):
    return render(request, 'accounts/profile.html', {'user_obj': request.user})

@login_required
def account_settings(request):
    """Unified account settings page — profile info + quick actions."""
    return render(request, 'accounts/account_settings.html', {'user_obj': request.user})

@login_required
def profile_edit(request):
    user = request.user
    if request.method == 'POST':
        form = ProfileEditForm(request.POST, instance=user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully.')
            return redirect('accounts:profile')
    else:
        form = ProfileEditForm(instance=user)
    return render(request, 'accounts/profile_edit.html', {'form': form})


@login_required
def patient_portal(request):
    if request.user.role != 'patient':
        return redirect('dashboard:index')

    from django.utils import timezone
    from notifications.models import Notification

    patient = getattr(request.user, 'patient_profile', None)
    patient_age = None
    if patient and patient.birthdate:
        today = timezone.localdate()
        b = patient.birthdate
        patient_age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    return render(request, 'accounts/patient_portal.html', {
        'patient': patient,
        'patient_age': patient_age,
        'appointment_notifications': Notification.objects.filter(
            recipient=request.user,
            dose__schedule__bite_case__patient__account=request.user,
            dose__schedule__bite_case__patient__is_archived=False,
        ).select_related('dose').order_by('-created_at')[:20],
    })


@login_required
def patient_bite_records(request):
    if request.user.role != 'patient':
        return redirect('dashboard:index')

    patient = getattr(request.user, 'patient_profile', None)
    cases = patient.bite_cases.all().order_by('-created_at') if patient else []
    pre_registrations = request.user.pre_registrations.select_related(
        'converted_case', 'converted_patient'
    ).order_by('-submitted_at')
    return render(request, 'accounts/patient_bite_records.html', {
        'cases': cases,
        'pre_registrations': pre_registrations,
    })


@login_required
def patient_vaccination_schedule(request):
    if request.user.role != 'patient':
        return redirect('dashboard:index')

    from django.utils import timezone
    from django.db.models import F
    from vaccination.models import VaccineDose

    doses = VaccineDose.objects.filter(
        schedule__bite_case__patient__account=request.user
    ).select_related('schedule__bite_case').order_by(F('scheduled_date').asc(nulls_last=True), 'id')
    return render(request, 'accounts/patient_vaccination_schedule.html', {
        'doses': doses,
        'today': timezone.localdate(),
    })


@login_required
def patient_vaccination_history(request):
    if request.user.role != 'patient':
        return redirect('dashboard:index')

    from vaccination.models import VaccineDose

    doses = VaccineDose.objects.filter(
        schedule__bite_case__patient__account=request.user,
        dose_status='completed',
        actual_date__isnull=False,
    ).select_related('schedule__bite_case').order_by('-actual_date', '-id')
    return render(request, 'accounts/patient_vaccination_history.html', {'doses': doses})


@login_required
def patient_qr(request):
    if request.user.role != 'patient':
        return redirect('dashboard:index')

    patient = getattr(request.user, 'patient_profile', None)
    qr_uri = None
    if patient:
        from utils.qr import build_patient_payload, qr_data_uri
        try:
            qr_uri = qr_data_uri(build_patient_payload(patient))
        except Exception:
            qr_uri = None
    return render(request, 'accounts/patient_qr.html', {
        'patient': patient,
        'qr_data_uri': qr_uri,
    })

@sensitive_post_parameters('old_password', 'new_password1', 'new_password2')
@login_required
def change_password(request):
    if request.method == 'POST':
        form = AccountPasswordChangeForm(request.user, request.POST)
        # Style widgets to match app design system
        for fname, field in form.fields.items():
            field.widget.attrs.update({
                'class': 'form-input pr-10',
                'placeholder': '••••••••',
                'autocomplete': 'new-password' if 'new' in fname else 'current-password',
            })
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, 'Password changed successfully.')
            return redirect('accounts:profile')
    else:
        form = AccountPasswordChangeForm(request.user)
        for fname, field in form.fields.items():
            field.widget.attrs.update({
                'class': 'form-input pr-10',
                'placeholder': '••••••••',
                'autocomplete': 'new-password' if 'new' in fname else 'current-password',
            })
            if fname == 'old_password':
                field.widget.attrs['autocomplete'] = 'current-password'
                field.widget.attrs['placeholder'] = 'Current password'
            if fname == 'new_password1':
                field.widget.attrs['placeholder'] = 'New password (min 8 chars)'
            if fname == 'new_password2':
                field.widget.attrs['placeholder'] = 'Confirm new password'
    return render(request, 'accounts/change_password.html', {'form': form})

@login_required
@user_passes_test(is_admin)
def user_list(request):
    users = User.objects.all().order_by('-date_joined')
    return render(request, 'accounts/user_list.html', {'users': users})

@login_required
@user_passes_test(is_admin)
def user_detail(request, pk):
    account = get_object_or_404(User, pk=pk)
    return _render_user_detail(request, account)


def _render_user_detail(request, account, temp_password=None, password_event=''):
    patient = getattr(account, 'patient_profile', None) if account.role == 'patient' else None
    portal_login_url = (
        request.build_absolute_uri(reverse('accounts:login'))
        if account.role == 'patient' else ''
    )
    response = render(request, 'accounts/user_detail.html', {
        'account': account,
        'patient': patient,
        'full_name': (patient.full_name() if patient else '') or account.get_full_name() or account.username,
        'portal_login_url': portal_login_url,
        'temp_password': temp_password,
        'password_event': password_event,
    })
    if temp_password:
        add_never_cache_headers(response)
    return response


def _client_ip(request):
    return request.META.get('REMOTE_ADDR') or None


@sensitive_variables('temp_password')
@login_required
@user_passes_test(is_admin)
@require_POST
def user_reset_password(request, pk):
    account = get_object_or_404(User, pk=pk)
    if account.role != 'patient':
        raise PermissionDenied('Only patient portal passwords can be reset here.')
    temp_password = generate_temp_password()
    account.set_password(temp_password)
    account.must_change_password = True
    account.save(update_fields=['password', 'must_change_password', 'updated_at'])

    from audit.models import AuditLog
    patient = getattr(account, 'patient_profile', None)
    subject = f'{patient.patient_number} ({patient.full_name()})' if patient else account.get_full_name() or account.username
    AuditLog.objects.create(
        user=request.user,
        action='RESET_PATIENT_PASSWORD',
        description=f'Reset portal password for {subject} -> {account.username}; patient must change it at next login',
        target_type='patient' if patient else '',
        target_id=patient.pk if patient else None,
        ip_address=_client_ip(request),
    )
    return _render_user_detail(request, account, temp_password=temp_password, password_event='reset')

@sensitive_post_parameters('password1', 'password2')
@login_required
@user_passes_test(is_admin)
def user_create(request):
    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            from audit.models import AuditLog
            AuditLog.objects.create(user=request.user, action='CREATE_USER', description=f'Created user {user.username}')
            messages.success(request, f'User {user.username} created successfully.')
            if user.role == 'patient':
                return _render_user_detail(
                    request, user,
                    temp_password=form.cleaned_data['password1'],
                    password_event='created',
                )
            return redirect('accounts:user_list')
    else:
        form = UserCreateForm()
    return render(request, 'accounts/user_form.html', {'form': form, 'title': 'Add User'})

@login_required
@user_passes_test(is_admin)
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        form = UserEditForm(request.POST, instance=user)
        if form.is_valid():
            form.save()
            from audit.models import AuditLog
            AuditLog.objects.create(user=request.user, action='EDIT_USER', description=f'Edited user {user.username}')
            messages.success(request, f'User {user.username} updated successfully.')
            return redirect('accounts:user_list')
    else:
        form = UserEditForm(instance=user)
    return render(request, 'accounts/user_form.html', {'form': form, 'title': 'Edit User'})

@login_required
@user_passes_test(is_admin)
def user_toggle_active(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user == request.user:
        messages.error(request, 'You cannot deactivate your own account.')
        return redirect('accounts:user_list')
    user.is_active = not user.is_active
    user.save()
    status = 'activated' if user.is_active else 'deactivated'
    from audit.models import AuditLog
    AuditLog.objects.create(user=request.user, action='TOGGLE_USER', description=f'{status} user {user.username}')
    messages.success(request, f'User {user.username} {status}.')
    return redirect('accounts:user_list')
