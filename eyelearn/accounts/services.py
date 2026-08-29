import logging

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .emails import send_password_reset_email

logger = logging.getLogger(__name__)

User = get_user_model()


class InvalidResetTokenError(Exception):
    pass


class PasswordResetService:
    def request_reset(self, *, email, locale='en'):
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            return

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        # Mirrors the frontend's localePrefix: "as-needed" routing (src/i18n/routing.ts):
        # the default locale ("en") is unprefixed, every other locale is path-prefixed.
        path_prefix = '' if locale == 'en' else f'/{locale}'
        reset_url = f'{settings.FRONTEND_URL}{path_prefix}/reset-password?uid={uid}&token={token}'

        try:
            send_password_reset_email(user, reset_url, locale=locale)
        except requests.RequestException:
            # Don't leak delivery failures to the client -- the response
            # must look identical whether or not the email exists/sent.
            logger.exception('Failed to send password reset email to user %s', user.id)

    def confirm_reset(self, *, uid, token, new_password):
        try:
            pk = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=pk)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise InvalidResetTokenError()

        if not default_token_generator.check_token(user, token):
            raise InvalidResetTokenError()

        user.set_password(new_password)
        user.save(update_fields=['password'])


class AccountDeletionService:
    def delete_account(self, *, user):
        # Cancel any active Stripe subscription first so deleting our local
        # records doesn't leave the user stuck being billed with no account
        # left to manage or cancel it from.
        from billing.services import BillingService
        BillingService().cancel_active_subscription(user=user)

        # Flashcard media lives in an external bucket and isn't touched by
        # the DB cascade below -- collect storage keys while the rows still
        # exist, same pattern as CollectionService.delete_collection.
        from flashcards.models import FlashcardMedia
        from flashcards.services import _cleanup_storage_keys
        storage_keys = list(
            FlashcardMedia.objects
            .filter(flashcard__collection__user=user)
            .values_list('storage_key', flat=True)
        )

        with transaction.atomic():
            # Cascades: Collection, Flashcard, FlashcardMedia, ReviewState,
            # ReviewLog, StudyDay, CollectionGoal, PaymentCustomer, Subscription.
            user.delete()
            if storage_keys:
                transaction.on_commit(lambda: _cleanup_storage_keys(storage_keys))
