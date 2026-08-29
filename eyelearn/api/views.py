from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

@api_view(['GET'])
@permission_classes([AllowAny])
def home(request):
    return Response({
        'message': 'API is running',
        'hello_url': '/api/hello/Italo/'
    })

@api_view(['GET'])
@permission_classes([AllowAny])
def hello_user(request, username):
    return Response({
        'message': f'Hello, {username}!'
    })
