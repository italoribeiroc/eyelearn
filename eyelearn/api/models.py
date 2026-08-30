from django.conf import settings
from django.db import models


class ContactMessage(models.Model):
    """A submission from the Help page's contact form. Persisted as a
    fallback record in case either the submitter's confirmation email or
    our own notification email fails to send -- the row itself is always
    saved first, so a delivery hiccup never loses the message."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='contact_messages')
    name = models.CharField(max_length=150)
    email = models.EmailField()
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'ContactMessage(id={self.id}, email={self.email})'
