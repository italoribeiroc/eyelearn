import json
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from eyelearn.test_utils import ApiTestCase
from flashcards.models import Collection, Flashcard, FlashcardMedia, ReviewState, StudyDay


@patch('accounts.services.send_verification_email')
class RegisterEndpointTests(ApiTestCase):
    def test_register_creates_inactive_user_and_sends_verification_email(self, mock_send):
        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'bob@example.com',
            'password': 'a-strong-password-123',
            'first_name': 'Bob',
        })

        self.assertEqual(response.status_code, 201)
        self.assertNotIn('access', response.json())
        self.assertNotIn('refresh', response.json())
        user = get_user_model().objects.get(username='bob')
        self.assertEqual(user.email, 'bob@example.com')
        self.assertEqual(user.first_name, 'Bob')
        self.assertFalse(user.is_active)
        mock_send.assert_called_once()

    def test_registered_user_cannot_log_in_before_verifying(self, mock_send):
        self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'bob@example.com',
            'password': 'a-strong-password-123',
            'first_name': 'Bob',
        })

        response = self.client.post('/api/auth/login/', {
            'username': 'bob', 'password': 'a-strong-password-123',
        })

        self.assertEqual(response.status_code, 401)

    def test_register_rejects_missing_first_name(self, mock_send):
        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'bob@example.com',
            'password': 'a-strong-password-123',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('first_name', response.json())

    def test_register_rejects_duplicate_username(self, mock_send):
        get_user_model().objects.create_user(
            username='bob', email='first@example.com', password='a-strong-password-123',
        )

        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'second@example.com',
            'password': 'a-strong-password-123',
            'first_name': 'Bob',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('username', response.json())

    def test_register_rejects_duplicate_email(self, mock_send):
        get_user_model().objects.create_user(
            username='first', email='bob@example.com', password='a-strong-password-123',
        )

        response = self.client.post('/api/auth/register/', {
            'username': 'second',
            'email': 'bob@example.com',
            'password': 'a-strong-password-123',
            'first_name': 'Bob',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.json())

    def test_register_rejects_weak_password(self, mock_send):
        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'bob@example.com',
            'password': 'password',
            'first_name': 'Bob',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.json())
        self.assertFalse(get_user_model().objects.filter(username='bob').exists())


class AuthEndpointTests(ApiTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='alice',
            email='alice@example.com',
            password='alicepass123',
        )

    def test_login_returns_access_and_refresh_tokens(self):
        response = self.client.post('/api/auth/login/', {
            'username': 'alice',
            'password': 'alicepass123',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.json())
        self.assertIn('refresh', response.json())

    def test_login_rejects_wrong_password(self):
        response = self.client.post('/api/auth/login/', {
            'username': 'alice',
            'password': 'wrong-password',
        })

        self.assertEqual(response.status_code, 401)

    def test_me_requires_authentication(self):
        response = self.client.get('/api/auth/me/')

        self.assertEqual(response.status_code, 401)

    def test_me_returns_current_user_with_valid_token(self):
        login_response = self.client.post('/api/auth/login/', {
            'username': 'alice',
            'password': 'alicepass123',
        })
        access_token = login_response.json()['access']

        response = self.client.get(
            '/api/auth/me/',
            HTTP_AUTHORIZATION=f'Bearer {access_token}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'id': self.user.id,
            'username': 'alice',
            'email': 'alice@example.com',
            'first_name': '',
            'has_seen_onboarding': False,
        })


class UpdateProfileEndpointTests(ApiTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='alice',
            email='alice@example.com',
            password='alicepass123',
        )
        get_user_model().objects.create_user(
            username='bob',
            email='bob@example.com',
            password='bobpass123',
        )
        login_response = self.client.post('/api/auth/login/', {
            'username': 'alice',
            'password': 'alicepass123',
        })
        self.access_token = login_response.json()['access']

    def _patch(self, data):
        return self.client.patch(
            '/api/auth/me/',
            data=json.dumps(data),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.access_token}',
        )

    def test_requires_authentication(self):
        response = self.client.patch(
            '/api/auth/me/',
            data=json.dumps({'username': 'newname'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 401)

    def test_updates_username_and_email(self):
        response = self._patch({'username': 'alice2', 'email': 'alice2@example.com'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'id': self.user.id,
            'username': 'alice2',
            'email': 'alice2@example.com',
            'first_name': '',
            'has_seen_onboarding': False,
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'alice2')
        self.assertEqual(self.user.email, 'alice2@example.com')

    def test_partial_update_only_changes_given_field(self):
        response = self._patch({'username': 'alice2'})

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'alice2')
        self.assertEqual(self.user.email, 'alice@example.com')

    def test_rejects_username_already_taken(self):
        response = self._patch({'username': 'bob'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('username', response.json())
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'alice')

    def test_rejects_email_already_taken(self):
        response = self._patch({'email': 'bob@example.com'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.json())

    def test_keeping_own_current_username_is_not_rejected_as_taken(self):
        response = self._patch({'username': 'alice', 'email': 'alice@example.com'})

        self.assertEqual(response.status_code, 200)

    def test_new_registration_defaults_has_seen_onboarding_to_false(self):
        self.assertFalse(self.user.has_seen_onboarding)

    def test_can_mark_onboarding_as_seen(self):
        response = self._patch({'has_seen_onboarding': True})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['has_seen_onboarding'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_seen_onboarding)


class GoogleAuthEndpointTests(ApiTestCase):
    def _idinfo(self, **overrides):
        return {
            'sub': 'google-sub-123',
            'email': 'newuser@example.com',
            'email_verified': True,
            'given_name': 'New',
            **overrides,
        }

    @patch('accounts.views.google_id_token.verify_oauth2_token')
    def test_creates_new_user_from_google_token(self, mock_verify):
        mock_verify.return_value = self._idinfo()

        response = self.client.post('/api/auth/google/', {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['user']['email'], 'newuser@example.com')
        self.assertEqual(body['user']['username'], 'newuser')
        self.assertEqual(body['user']['first_name'], 'New')
        self.assertFalse(body['user']['has_seen_onboarding'])
        self.assertIn('access', body)
        self.assertIn('refresh', body)

        user = get_user_model().objects.get(email='newuser@example.com')
        self.assertEqual(user.google_id, 'google-sub-123')
        self.assertEqual(user.first_name, 'New')
        self.assertFalse(user.has_usable_password())

    @patch('accounts.views.google_id_token.verify_oauth2_token')
    def test_links_existing_password_account_by_email(self, mock_verify):
        existing = get_user_model().objects.create_user(
            username='alice', email='alice@example.com', password='alicepass123',
        )
        mock_verify.return_value = self._idinfo(email='alice@example.com')

        response = self.client.post('/api/auth/google/', {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['user']['username'], 'alice')

        existing.refresh_from_db()
        self.assertEqual(existing.google_id, 'google-sub-123')
        self.assertTrue(existing.has_usable_password())
        self.assertTrue(existing.check_password('alicepass123'))

    @patch('accounts.views.google_id_token.verify_oauth2_token')
    def test_logs_in_existing_google_linked_user(self, mock_verify):
        get_user_model().objects.create_user(
            username='newuser', email='newuser@example.com', password=None,
        )
        get_user_model().objects.filter(email='newuser@example.com').update(google_id='google-sub-123')
        mock_verify.return_value = self._idinfo()

        response = self.client.post('/api/auth/google/', {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_user_model().objects.filter(email='newuser@example.com').count(), 1)

    @patch('accounts.views.google_id_token.verify_oauth2_token')
    def test_rejects_invalid_token(self, mock_verify):
        mock_verify.side_effect = ValueError('invalid token')

        response = self.client.post('/api/auth/google/', {'id_token': 'bad-token'})

        self.assertEqual(response.status_code, 401)

    @patch('accounts.views.google_id_token.verify_oauth2_token')
    def test_rejects_unverified_email(self, mock_verify):
        mock_verify.return_value = self._idinfo(email_verified=False)

        response = self.client.post('/api/auth/google/', {'id_token': 'fake-token'})

        self.assertEqual(response.status_code, 401)
        self.assertFalse(get_user_model().objects.filter(email='newuser@example.com').exists())

    def test_requires_id_token(self):
        response = self.client.post('/api/auth/google/', {})

        self.assertEqual(response.status_code, 400)


class PasswordResetTests(ApiTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='alice', email='alice@example.com', password='old-password-123',
        )

    @patch('accounts.services.send_password_reset_email')
    def test_request_with_existing_email_sends_link(self, mock_send):
        response = self.client.post('/api/auth/password-reset/', {'email': 'alice@example.com'})

        self.assertEqual(response.status_code, 200)
        mock_send.assert_called_once()
        sent_user, reset_url = mock_send.call_args[0]
        self.assertEqual(sent_user.id, self.user.id)
        self.assertIn('uid=', reset_url)
        self.assertIn('token=', reset_url)

    @patch('accounts.services.send_password_reset_email')
    def test_request_defaults_to_english_unprefixed_link(self, mock_send):
        self.client.post('/api/auth/password-reset/', {'email': 'alice@example.com'})

        _sent_user, reset_url = mock_send.call_args[0]
        self.assertTrue(reset_url.startswith(f'{settings.FRONTEND_URL}/reset-password?'))
        self.assertEqual(mock_send.call_args.kwargs['locale'], 'en')

    @patch('accounts.services.send_password_reset_email')
    def test_request_with_pt_br_locale_prefixes_link(self, mock_send):
        self.client.post('/api/auth/password-reset/', {'email': 'alice@example.com', 'locale': 'pt-BR'})

        _sent_user, reset_url = mock_send.call_args[0]
        self.assertTrue(reset_url.startswith(f'{settings.FRONTEND_URL}/pt-BR/reset-password?'))
        self.assertEqual(mock_send.call_args.kwargs['locale'], 'pt-BR')

    @patch('accounts.emails.requests.post')
    def test_email_greets_user_by_first_name_when_set(self, mock_post):
        self.user.first_name = 'Alice'
        self.user.save(update_fields=['first_name'])

        self.client.post('/api/auth/password-reset/', {'email': 'alice@example.com'})

        html = mock_post.call_args.kwargs['json']['html']
        self.assertIn('Hi Alice,', html)

    @patch('accounts.emails.requests.post')
    def test_email_falls_back_to_username_without_first_name(self, mock_post):
        self.client.post('/api/auth/password-reset/', {'email': 'alice@example.com'})

        html = mock_post.call_args.kwargs['json']['html']
        self.assertIn('Hi alice,', html)

    @patch('accounts.services.send_password_reset_email')
    def test_request_with_unknown_email_is_silent(self, mock_send):
        response = self.client.post('/api/auth/password-reset/', {'email': 'nobody@example.com'})

        self.assertEqual(response.status_code, 200)
        mock_send.assert_not_called()

    def _extract_uid_token(self, mock_send):
        reset_url = mock_send.call_args[0][1]
        query = reset_url.split('?', 1)[1]
        params = dict(pair.split('=') for pair in query.split('&'))
        return params['uid'], params['token']

    @patch('accounts.services.send_password_reset_email')
    def test_confirm_with_valid_token_changes_password(self, mock_send):
        self.client.post('/api/auth/password-reset/', {'email': 'alice@example.com'})
        uid, token = self._extract_uid_token(mock_send)

        response = self.client.post('/api/auth/password-reset/confirm/', {
            'uid': uid, 'token': token, 'new_password': 'brand-new-password-456',
        })

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('brand-new-password-456'))
        self.assertFalse(self.user.check_password('old-password-123'))

    @patch('accounts.services.send_password_reset_email')
    def test_confirm_with_tampered_token_is_rejected(self, mock_send):
        self.client.post('/api/auth/password-reset/', {'email': 'alice@example.com'})
        uid, _token = self._extract_uid_token(mock_send)

        response = self.client.post('/api/auth/password-reset/confirm/', {
            'uid': uid, 'token': 'not-a-real-token', 'new_password': 'brand-new-password-456',
        })

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('old-password-123'))

    @patch('accounts.services.send_password_reset_email')
    def test_token_cannot_be_reused_after_success(self, mock_send):
        self.client.post('/api/auth/password-reset/', {'email': 'alice@example.com'})
        uid, token = self._extract_uid_token(mock_send)

        first = self.client.post('/api/auth/password-reset/confirm/', {
            'uid': uid, 'token': token, 'new_password': 'brand-new-password-456',
        })
        second = self.client.post('/api/auth/password-reset/confirm/', {
            'uid': uid, 'token': token, 'new_password': 'another-password-789',
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('brand-new-password-456'))


class AccountDeletionTests(ApiTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='alice', email='alice@example.com', password='alicepass123',
        )
        login_response = self.client.post('/api/auth/login/', {
            'username': 'alice', 'password': 'alicepass123',
        })
        self.access_token = login_response.json()['access']

    def _delete(self, data, auth=True):
        kwargs = {'HTTP_AUTHORIZATION': f'Bearer {self.access_token}'} if auth else {}
        return self.client.delete(
            '/api/auth/me/',
            data=json.dumps(data),
            content_type='application/json',
            **kwargs,
        )

    def test_requires_authentication(self):
        response = self._delete({'username': 'alice'}, auth=False)

        self.assertEqual(response.status_code, 401)
        self.assertTrue(get_user_model().objects.filter(username='alice').exists())

    def test_rejects_wrong_username_confirmation(self):
        response = self._delete({'username': 'not-alice'})

        self.assertEqual(response.status_code, 400)
        self.assertTrue(get_user_model().objects.filter(username='alice').exists())

    def test_deletes_user_and_cascades_flashcard_data(self):
        collection = Collection.objects.create(user=self.user, name='Deck')
        flashcard = Flashcard.objects.create(
            collection=collection, card_type=Flashcard.CardType.BASIC, prompt='q', answer='a',
        )
        ReviewState.objects.create(user=self.user, flashcard=flashcard, due=timezone.now())
        StudyDay.objects.create(user=self.user, date=timezone.now().date())

        response = self._delete({'username': 'alice'})

        self.assertEqual(response.status_code, 204)
        self.assertFalse(get_user_model().objects.filter(username='alice').exists())
        self.assertFalse(Collection.objects.filter(id=collection.id).exists())
        self.assertFalse(Flashcard.objects.filter(id=flashcard.id).exists())
        self.assertFalse(ReviewState.objects.filter(user_id=self.user.id).exists())
        self.assertFalse(StudyDay.objects.filter(user_id=self.user.id).exists())

    @patch('billing.services.BillingService.cancel_active_subscription')
    def test_cancels_active_subscription(self, mock_cancel):
        self._delete({'username': 'alice'})

        mock_cancel.assert_called_once()
        # user.delete() clears .id/.pk on the same in-memory instance
        # afterward, so compare a field delete() doesn't touch.
        self.assertEqual(mock_cancel.call_args.kwargs['user'].username, 'alice')

    @patch('flashcards.services._cleanup_storage_keys')
    def test_cleans_up_flashcard_media_storage(self, mock_cleanup):
        collection = Collection.objects.create(user=self.user, name='Deck')
        flashcard = Flashcard.objects.create(
            collection=collection, card_type=Flashcard.CardType.BASIC, prompt='q', answer='a',
        )
        media = FlashcardMedia.objects.create(
            flashcard=flashcard, media_type=FlashcardMedia.MediaType.IMAGE, side=FlashcardMedia.Side.PROMPT,
            storage_key='flashcards/1/1/image/test.png', content_type='image/png', size_bytes=100,
        )

        # transaction.on_commit callbacks don't fire inside TestCase's atomic
        # wrapper by default -- this context manager captures and runs them.
        with self.captureOnCommitCallbacks(execute=True):
            self._delete({'username': 'alice'})

        mock_cleanup.assert_called_once_with([media.storage_key])


class EmailVerificationTests(ApiTestCase):
    def _register(self, mock_send, **overrides):
        payload = {
            'username': 'bob', 'email': 'bob@example.com',
            'password': 'a-strong-password-123', 'first_name': 'Bob',
        }
        payload.update(overrides)
        response = self.client.post('/api/auth/register/', payload)
        uid, token = self._extract_uid_token(mock_send)
        return response, uid, token

    def _extract_uid_token(self, mock_send):
        url = mock_send.call_args[0][1]
        query = url.split('?', 1)[1]
        params = dict(pair.split('=') for pair in query.split('&'))
        return params['uid'], params['token']

    @patch('accounts.services.send_verification_email')
    def test_confirm_activates_account_and_login_then_works(self, mock_send):
        _response, uid, token = self._register(mock_send)

        confirm = self.client.post('/api/auth/verify-email/', {'uid': uid, 'token': token})

        self.assertEqual(confirm.status_code, 200)
        user = get_user_model().objects.get(username='bob')
        self.assertTrue(user.is_active)

        login = self.client.post('/api/auth/login/', {
            'username': 'bob', 'password': 'a-strong-password-123',
        })
        self.assertEqual(login.status_code, 200)

    @patch('accounts.services.send_verification_email')
    def test_confirm_token_cannot_be_reused_after_activation(self, mock_send):
        _response, uid, token = self._register(mock_send)

        first = self.client.post('/api/auth/verify-email/', {'uid': uid, 'token': token})
        second = self.client.post('/api/auth/verify-email/', {'uid': uid, 'token': token})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    @patch('accounts.services.send_verification_email')
    def test_confirm_with_tampered_token_is_rejected(self, mock_send):
        _response, uid, _token = self._register(mock_send)

        confirm = self.client.post('/api/auth/verify-email/', {'uid': uid, 'token': 'not-a-real-token'})

        self.assertEqual(confirm.status_code, 400)
        user = get_user_model().objects.get(username='bob')
        self.assertFalse(user.is_active)

    @patch('accounts.services.send_password_reset_email')
    @patch('accounts.services.send_verification_email')
    def test_verification_and_password_reset_tokens_are_not_interchangeable(self, mock_verify, mock_reset):
        _response, verify_uid, verify_token = self._register(mock_verify)
        # Force-activate so a password-reset request is meaningful.
        get_user_model().objects.filter(username='bob').update(is_active=True)

        self.client.post('/api/auth/password-reset/', {'email': 'bob@example.com'})
        reset_url = mock_reset.call_args[0][1]
        reset_params = dict(pair.split('=') for pair in reset_url.split('?', 1)[1].split('&'))

        # A verification token must not work as a password-reset token...
        reset_attempt = self.client.post('/api/auth/password-reset/confirm/', {
            'uid': verify_uid, 'token': verify_token, 'new_password': 'another-password-456',
        })
        self.assertEqual(reset_attempt.status_code, 400)

        # ...and a password-reset token must not work as a verification token.
        verify_attempt = self.client.post('/api/auth/verify-email/', {
            'uid': reset_params['uid'], 'token': reset_params['token'],
        })
        self.assertEqual(verify_attempt.status_code, 400)

    @patch('accounts.services.send_verification_email')
    def test_reclaims_stale_unverified_signup(self, mock_send):
        stale = get_user_model().objects.create_user(
            username='bob', email='bob@example.com', password='old-password-123', is_active=False,
        )
        get_user_model().objects.filter(pk=stale.pk).update(
            date_joined=timezone.now() - timedelta(hours=25),
        )

        response, _uid, _token = self._register(mock_send)

        self.assertEqual(response.status_code, 201)
        self.assertFalse(get_user_model().objects.filter(pk=stale.pk).exists())
        self.assertTrue(get_user_model().objects.filter(username='bob').exists())

    def test_does_not_reclaim_fresh_unverified_signup(self):
        get_user_model().objects.create_user(
            username='bob', email='bob@example.com', password='old-password-123', is_active=False,
        )

        response = self.client.post('/api/auth/register/', {
            'username': 'bob', 'email': 'bob@example.com',
            'password': 'a-strong-password-123', 'first_name': 'Bob',
        })

        self.assertEqual(response.status_code, 400)

    @patch('accounts.services.send_verification_email')
    def test_resend_is_silent_for_unknown_email(self, mock_send):
        response = self.client.post('/api/auth/verify-email/resend/', {'email': 'nobody@example.com'})

        self.assertEqual(response.status_code, 200)
        mock_send.assert_not_called()

    @patch('accounts.services.send_verification_email')
    def test_resend_is_silent_for_already_verified_email(self, mock_send):
        get_user_model().objects.create_user(
            username='bob', email='bob@example.com', password='a-strong-password-123',
        )

        response = self.client.post('/api/auth/verify-email/resend/', {'email': 'bob@example.com'})

        self.assertEqual(response.status_code, 200)
        mock_send.assert_not_called()

    @patch('accounts.services.send_verification_email')
    def test_resend_sends_again_for_pending_signup(self, mock_send):
        self._register(mock_send)

        response = self.client.post('/api/auth/verify-email/resend/', {'email': 'bob@example.com'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_send.call_count, 2)
