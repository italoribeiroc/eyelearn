from django.urls import path

from .views import (
    ThrottledTokenObtainPairView,
    ThrottledTokenRefreshView,
    google_auth,
    me,
    password_reset_confirm,
    password_reset_request,
    register,
    resend_verification,
    verify_email,
)

urlpatterns = [
    path('register/', register, name='register'),
    path('login/', ThrottledTokenObtainPairView.as_view(), name='login'),
    path('refresh/', ThrottledTokenRefreshView.as_view(), name='token_refresh'),
    path('me/', me, name='me'),
    path('google/', google_auth, name='google_auth'),
    path('password-reset/', password_reset_request, name='password_reset_request'),
    path('password-reset/confirm/', password_reset_confirm, name='password_reset_confirm'),
    path('verify-email/', verify_email, name='verify_email'),
    path('verify-email/resend/', resend_verification, name='resend_verification'),
]
