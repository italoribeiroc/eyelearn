from django.urls import path

from .views import create_checkout_session, create_portal_session, subscription_status, webhook

urlpatterns = [
    path('checkout-session/', create_checkout_session, name='create_checkout_session'),
    path('portal-session/', create_portal_session, name='create_portal_session'),
    path('subscription/', subscription_status, name='subscription_status'),
    path('webhook/<str:provider>/', webhook, name='webhook'),
]
