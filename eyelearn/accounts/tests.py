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
