from django.urls import path
from .views import hello_user

urlpatterns = [
    path('hello/<str:username>/', hello_user, name='hello_user'),
]
