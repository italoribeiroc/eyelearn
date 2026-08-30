import requests
from django.conf import settings
from django.utils.html import escape

RESEND_API_URL = 'https://api.resend.com/emails'

_CONFIRMATION_MESSAGES = {
    'en': {
        'subject': "We've received your message",
        'greeting': 'Hi {name},',
        'body': (
            "Thanks for reaching out to Eye Learn. We've received your message "
            'and will get back to you as soon as we can. For reference, here is '
            'what you sent us:'
        ),
        'footer': "If you didn't send this, you can safely ignore this email.",
    },
    'pt-BR': {
        'subject': 'Recebemos sua mensagem',
        'greeting': 'Olá, {name},',
        'body': (
            'Obrigado por entrar em contato com o Eye Learn. Recebemos sua '
            'mensagem e responderemos assim que possível. Para referência, '
            'aqui está o que você nos enviou:'
        ),
        'footer': 'Se você não enviou isso, pode ignorar este e-mail com segurança.',
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


def _quoted_message_html(message):
    # escape() first so no HTML/script in the user's message can execute in
    # an email client, then turn newlines into <br> for readable formatting.
    return escape(message).replace('\n', '<br>')


def send_contact_confirmation_email(contact_message, locale='en'):
    """Sends the submitter a confirmation that we received their message, in
    the locale they were browsing the Help page in."""
    copy = _CONFIRMATION_MESSAGES.get(locale, _CONFIRMATION_MESSAGES['en'])

    _send_via_resend(
        to_email=contact_message.email,
        subject=copy['subject'],
        html=(
            f'<p>{copy["greeting"].format(name=contact_message.name)}</p>'
            f'<p>{copy["body"]}</p>'
            f'<blockquote>{_quoted_message_html(contact_message.message)}</blockquote>'
            f'<p>{copy["footer"]}</p>'
        ),
    )


def send_contact_notification_email(contact_message):
    """Notifies the Eye Learn team of a new contact-form submission, with
    enough information (name/email/message) to reply directly."""
    if not settings.CONTACT_NOTIFICATION_EMAIL:
        return

    _send_via_resend(
        to_email=settings.CONTACT_NOTIFICATION_EMAIL,
        subject=f'New contact form message from {contact_message.name}',
        html=(
            f'<p><strong>From:</strong> {escape(contact_message.name)} '
            f'(<a href="mailto:{escape(contact_message.email)}">{escape(contact_message.email)}</a>)</p>'
            f'<p><strong>Message:</strong></p>'
            f'<blockquote>{_quoted_message_html(contact_message.message)}</blockquote>'
        ),
    )
