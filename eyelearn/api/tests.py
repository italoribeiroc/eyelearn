from django.test import SimpleTestCase


class ApiEndpointTests(SimpleTestCase):
    def test_home_endpoint(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['message'], 'API is running')

    def test_hello_endpoint(self):
        response = self.client.get('/api/hello/Italo/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'message': 'Hello, Italo!'})
