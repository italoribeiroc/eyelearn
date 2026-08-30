import logging

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator, default_token_generator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .emails import send_password_reset_email, send_verification_email

logger = logging.getLogger(__name__)

User = get_user_model()


class InvalidResetTokenError(Exception):
    pass


class InvalidVerificationTokenError(Exception):
    pass


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """Same stateless timestamp/hash mechanism as PasswordResetTokenGenerator,
    but with a distinct key_salt so a verification link can never also
    validate as a password-reset link for the same user (and vice versa) --
    otherwise both tokens would be identical, since they're derived from the
    same (pk, password, last_login, email, timestamp) inputs."""
    key_salt = 'eyelearn.accounts.EmailVerificationTokenGenerator'

    def _make_hash_value(self, user, timestamp):
        # Bind the token to is_active too, so it stops validating once the
        # account has actually been activated. Unlike password reset (where
        # the password itself changes on use, naturally invalidating the
        # token), verifying doesn't touch password/last_login, so without
        # this a verification link would stay replayable for its full
        # timeout window even after the account is already verified.
        return f'{super()._make_hash_value(user, timestamp)}{user.is_active}'


email_verification_token_generator = EmailVerificationTokenGenerator()


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


class EmailVerificationService:
    RECLAIM_AFTER = timezone.timedelta(hours=24)

    def reclaim_stale_signup(self, *, username, email):
        """Deletes an abandoned (never-verified) signup that's squatting the
        same username/email, so a typo'd registration doesn't permanently
        block a retry. Fresh (<24h old) inactive rows are left alone --
        those are still within their normal verification window."""
        if not username and not email:
            return
        cutoff = timezone.now() - self.RECLAIM_AFTER
        query = Q()
        if username:
            query |= Q(username=username)
        if email:
            query |= Q(email__iexact=email)
        User.objects.filter(is_active=False, date_joined__lt=cutoff).filter(query).delete()

    def send_verification_email(self, *, user, locale='en', plan=None):
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = email_verification_token_generator.make_token(user)
        path_prefix = '' if locale == 'en' else f'/{locale}'
        verify_url = f'{settings.FRONTEND_URL}{path_prefix}/verify-email?uid={uid}&token={token}'
        if plan:
            # Carries a pricing-card CTA's plan intent through to the login
            # page after the link is clicked, so the existing plan-intent-
            # through-login handoff (see login-form.tsx) still completes a
            # pending checkout, the same way it already does for Google
            # sign-in's same-browser redirect flow.
            verify_url += f'&plan={plan}'

        try:
            send_verification_email(user, verify_url, locale=locale)
        except requests.RequestException:
            logger.exception('Failed to send verification email to user %s', user.id)

    def confirm(self, *, uid, token):
        try:
            pk = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=pk)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            raise InvalidVerificationTokenError()

        if not email_verification_token_generator.check_token(user, token):
            raise InvalidVerificationTokenError()

        user.is_active = True
        user.save(update_fields=['is_active'])

    def resend(self, *, email, locale='en'):
        user = User.objects.filter(email__iexact=email, is_active=False).first()
        if user is None:
            return
        self.send_verification_email(user=user, locale=locale)


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
