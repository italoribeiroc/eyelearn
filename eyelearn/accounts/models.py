from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    email = models.EmailField('email address', unique=True)
    google_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
