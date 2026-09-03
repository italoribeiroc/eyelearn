from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    email = models.EmailField('email address', unique=True)
    google_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    has_seen_onboarding = models.BooleanField(default=False)
    # NULL means "never accepted" -- true for every account that registered
    # before this field existed (deliberately not backfilled, see the
    # migration) and stays NULL until a real registration sets it.
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
