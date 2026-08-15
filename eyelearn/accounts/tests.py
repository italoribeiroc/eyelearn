from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase


class RegisterEndpointTests(TestCase):
    def test_register_creates_user_and_returns_tokens(self):
        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'bob@example.com',
            'password': 'a-strong-password-123',
        })

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['user']['username'], 'bob')
        self.assertEqual(body['user']['email'], 'bob@example.com')
        self.assertNotIn('password', body['user'])
        self.assertIn('access', body)
        self.assertIn('refresh', body)
        self.assertTrue(get_user_model().objects.filter(username='bob').exists())

    def test_register_rejects_duplicate_username(self):
        get_user_model().objects.create_user(
            username='bob', email='first@example.com', password='a-strong-password-123',
        )

        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'second@example.com',
            'password': 'a-strong-password-123',
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
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.json())

    def test_register_rejects_weak_password(self):
        response = self.client.post('/api/auth/register/', {
            'username': 'bob',
            'email': 'bob@example.com',
            'password': 'password',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.json())
        self.assertFalse(get_user_model().objects.filter(username='bob').exists())


class AuthEndpointTests(TestCase):
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
        })


class GoogleAuthEndpointTests(TestCase):
    def _idinfo(self, **overrides):
        return {
            'sub': 'google-sub-123',
            'email': 'newuser@example.com',
            'email_verified': True,
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
        self.assertIn('access', body)
        self.assertIn('refresh', body)

        user = get_user_model().objects.get(email='newuser@example.com')
        self.assertEqual(user.google_id, 'google-sub-123')
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
