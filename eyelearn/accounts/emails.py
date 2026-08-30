import requests
from django.conf import settings

RESEND_API_URL = 'https://api.resend.com/emails'

_PASSWORD_RESET_MESSAGES = {
    'en': {
        'subject': 'Reset your Eye Learn password',
        'greeting': 'Hi {name},',
        'body': (
            'Someone requested a password reset for your Eye Learn account. '
            'Click the link below to choose a new password:'
        ),
        'footer': 'If you did not request this, you can safely ignore this email.',
    },
    'pt-BR': {
        'subject': 'Redefina sua senha do Eye Learn',
        'greeting': 'Olá, {name},',
        'body': (
            'Alguém solicitou a redefinição de senha da sua conta no Eye Learn. '
            'Clique no link abaixo para escolher uma nova senha:'
        ),
        'footer': 'Se você não solicitou isso, pode ignorar este e-mail com segurança.',
    },
}

_VERIFICATION_MESSAGES = {
    'en': {
        'subject': 'Verify your Eye Learn email',
        'greeting': 'Hi {name},',
        'body': (
            'Thanks for signing up for Eye Learn. Click the link below to verify '
            'your email and activate your account:'
        ),
        'footer': 'If you did not create this account, you can safely ignore this email.',
    },
    'pt-BR': {
        'subject': 'Verifique seu e-mail do Eye Learn',
        'greeting': 'Olá, {name},',
        'body': (
            'Obrigado por se cadastrar no Eye Learn. Clique no link abaixo para '
            'verificar seu e-mail e ativar sua conta:'
        ),
        'footer': 'Se você não criou esta conta, pode ignorar este e-mail com segurança.',
    },
}


def _send_via_resend(*, to_email, subject, html):
    """Raises requests.HTTPError on a non-2xx response -- callers decide
    whether a delivery failure should be surfaced or swallowed."""
    response = requests.post(
        RESEND_API_URL,
        headers={'Authorization': f'Bearer {settings.RESEND_API_KEY}'},
        json={
            'from': settings.RESEND_FROM_EMAIL,
            'to': [to_email],
            'subject': subject,
            'html': html,
        },
        timeout=10,
    )
    response.raise_for_status()


def send_password_reset_email(user, reset_url, locale='en'):
    """Sends the password-reset link via Resend's HTTP API, in the same
    locale the user was browsing in when they requested it (see
    PasswordResetService.request_reset)."""
    copy = _PASSWORD_RESET_MESSAGES.get(locale, _PASSWORD_RESET_MESSAGES['en'])
    display_name = user.first_name or user.username

    _send_via_resend(
        to_email=user.email,
        subject=copy['subject'],
        html=(
            f'<p>{copy["greeting"].format(name=display_name)}</p>'
            f'<p>{copy["body"]}</p>'
            f'<p><a href="{reset_url}">{reset_url}</a></p>'
            f'<p>{copy["footer"]}</p>'
        ),
    )


def send_verification_email(user, verify_url, locale='en'):
    """Sends the email-verification link via Resend's HTTP API, in the same
    locale the user was browsing in when they registered (see
    EmailVerificationService.send_verification_email)."""
    copy = _VERIFICATION_MESSAGES.get(locale, _VERIFICATION_MESSAGES['en'])
    display_name = user.first_name or user.username

    _send_via_resend(
        to_email=user.email,
        subject=copy['subject'],
        html=(
            f'<p>{copy["greeting"].format(name=display_name)}</p>'
            f'<p>{copy["body"]}</p>'
            f'<p><a href="{verify_url}">{verify_url}</a></p>'
            f'<p>{copy["footer"]}</p>'
        ),
    )
