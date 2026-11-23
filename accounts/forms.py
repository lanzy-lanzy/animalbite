from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm, PasswordChangeForm, PasswordResetForm
from django.contrib.auth import get_user_model
from django.views.decorators.debug import sensitive_variables

from utils.credentials import generate_temp_password

User = get_user_model()

class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Username'}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-input', 'placeholder': 'Password'}))

class UserCreateForm(UserCreationForm):
    """Patient accounts may leave both passwords blank to receive a generated
    temporary password; staff accounts still require one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.generated_password = None
        self.fields['password1'].required = False
        self.fields['password2'].required = False
        self.fields['password2'].help_text = (
            'Enter the same password as before, for verification. For a Patient '
            'account, leave both password fields blank to generate a secure '
            'temporary password.'
        )

    @sensitive_variables('temp_password')
    def clean(self):
        password1 = self.cleaned_data.get('password1')
        password2 = self.cleaned_data.get('password2')
        if not password1 and not password2 and self.cleaned_data.get('role') == 'patient':
            temp_password = generate_temp_password()
            self.cleaned_data['password1'] = temp_password
            self.cleaned_data['password2'] = temp_password
            self.generated_password = temp_password
        else:
            for name, value in (('password1', password1), ('password2', password2)):
                if not value and name not in self.errors:
                    self.add_error(name, self.fields[name].error_messages['required'])
        return super().clean()

    def save(self, commit=True):
        user = super().save(commit=False)
        user.must_change_password = user.role == 'patient'
        if commit:
            user.save()
            self.save_m2m()
        return user

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'mobile', 'role')
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-input'}),
            'first_name': forms.TextInput(attrs={'class': 'form-input'}),
            'last_name': forms.TextInput(attrs={'class': 'form-input'}),
            'mobile': forms.TextInput(attrs={'class': 'form-input'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
        }

class AccountPasswordChangeForm(PasswordChangeForm):
    @sensitive_variables('new_password')
    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get('new_password1')
        if new_password and 'new_password1' not in self.errors and self.user.check_password(new_password):
            self.add_error('new_password1', 'Your new password must be different from your current password.')
        return cleaned_data

    def save(self, commit=True):
        self.user.must_change_password = False
        return super().save(commit=commit)


class UserEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'mobile', 'role', 'is_active')
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-input'}),
            'first_name': forms.TextInput(attrs={'class': 'form-input'}),
            'last_name': forms.TextInput(attrs={'class': 'form-input'}),
            'mobile': forms.TextInput(attrs={'class': 'form-input'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check h-4 w-4 rounded border-cream-300'}),
        }


class ProfileEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'mobile')
        widgets = {
            field: forms.TextInput(attrs={'class': 'form-input'})
            for field in fields
        }
