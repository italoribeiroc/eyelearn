import json
from unittest.mock import patch

from django.contrib.auth import get_user_model

from eyelearn.test_utils import ApiSimpleTestCase, ApiTestCase

from .models import ContactMessage


class ApiEndpointTests(ApiSimpleTestCase):
    def test_home_endpoint(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['message'], 'API is running')

    def test_hello_endpoint(self):
        response = self.client.get('/api/hello/Italo/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'message': 'Hello, Italo!'})


class ContactFormTests(ApiTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='alice',
            email='alice@example.com',
            password='alicepass123',
            first_name='Alice',
        )
        login_response = self.client.post('/api/auth/login/', {
            'username': 'alice',
            'password': 'alicepass123',
        })
        self.access_token = login_response.json()['access']

    def _post(self, data, auth=True):
        kwargs = {'HTTP_AUTHORIZATION': f'Bearer {self.access_token}'} if auth else {}
        return self.client.post(
            '/api/contact/',
            data=json.dumps(data),
            content_type='application/json',
            **kwargs,
        )

    def test_requires_authentication(self):
        response = self._post({'name': 'Alice', 'email': 'alice@example.com', 'message': 'Hi'}, auth=False)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(ContactMessage.objects.count(), 0)

    @patch('api.views.send_contact_notification_email')
    @patch('api.views.send_contact_confirmation_email')
    def test_saves_message_and_sends_both_emails(self, mock_confirm, mock_notify):
        response = self._post({
            'name': 'Alice',
            'email': 'alice@example.com',
            'message': 'How do I import a big deck?',
        })

        self.assertEqual(response.status_code, 200)
        message = ContactMessage.objects.get()
        self.assertEqual(message.user, self.user)
        self.assertEqual(message.name, 'Alice')
        self.assertEqual(message.email, 'alice@example.com')
        self.assertEqual(message.message, 'How do I import a big deck?')

        mock_confirm.assert_called_once()
        self.assertEqual(mock_confirm.call_args.args[0], message)
        mock_notify.assert_called_once_with(message)

    @patch('api.views.send_contact_notification_email')
    @patch('api.views.send_contact_confirmation_email')
    def test_rejects_missing_message(self, mock_confirm, mock_notify):
        response = self._post({'name': 'Alice', 'email': 'alice@example.com', 'message': ''})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(ContactMessage.objects.count(), 0)
        mock_confirm.assert_not_called()
        mock_notify.assert_not_called()

    @patch('api.views.send_contact_notification_email')
    @patch('api.views.send_contact_confirmation_email')
    def test_still_succeeds_if_email_sending_fails(self, mock_confirm, mock_notify):
        import requests
        mock_confirm.side_effect = requests.RequestException('boom')
        mock_notify.side_effect = requests.RequestException('boom')

        response = self._post({'name': 'Alice', 'email': 'alice@example.com', 'message': 'Hello'})

        # The row is already saved by the time either send is attempted, so
        # a delivery failure shouldn't turn into a 500 for the submitter.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactMessage.objects.count(), 1)
