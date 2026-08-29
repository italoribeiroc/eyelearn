import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from eyelearn.test_utils import ApiTestCase
from flashcards.models import Collection, Flashcard, FlashcardMedia, ReviewState, StudyDay


class RegisterEndpointTests(ApiTestCase):
    def test_register_creates_user_and_returns_tokens(self):
        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'bob@example.com',
            'password': 'a-strong-password-123',
            'first_name': 'Bob',
        })

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['user']['username'], 'bob')
        self.assertEqual(body['user']['email'], 'bob@example.com')
        self.assertEqual(body['user']['first_name'], 'Bob')
        self.assertNotIn('password', body['user'])
        self.assertIn('access', body)
        self.assertIn('refresh', body)
        self.assertTrue(get_user_model().objects.filter(username='bob').exists())

    def test_register_rejects_missing_first_name(self):
        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'bob@example.com',
            'password': 'a-strong-password-123',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('first_name', response.json())

    def test_register_rejects_duplicate_username(self):
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

    def test_register_rejects_duplicate_email(self):
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

    def test_register_rejects_weak_password(self):
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
