from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(['GET'])
def home(request):
    return Response({
        'message': 'API is running',
        'hello_url': '/api/hello/Italo/'
    })

@api_view(['GET'])
def hello_user(request, username):
    return Response({
        'message': f'Hello, {username}! Welcome to Eyelearn'
    })
