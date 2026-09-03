from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone as django_timezone
from rest_framework_simplejwt.tokens import RefreshToken

from eyelearn.test_utils import ApiTestCase

from billing.models import PaymentCustomer, ProcessedWebhookEvent, Subscription, WITHDRAWAL_WINDOW_DAYS
from billing.providers.base import (
    CheckoutSession,
    CustomerNotFound,
    InvalidWebhookSignature,
    NormalizedEvent,
    NormalizedEventType,
    PaymentProvider,
    PortalSession,
    RefundTargetNotFoundError,
)
from billing.services import (
    AlreadySubscribedError,
    BillingService,
    NoActiveSubscriptionError,
    NoPaymentCustomerError,
    RefundWindowExpiredError,
)

User = get_user_model()


class FakePaymentProvider(PaymentProvider):
    """Test double standing in for a real connector -- proves BillingService needs
    nothing Stripe-specific to work, which is the whole point of the abstraction."""

    def __init__(self):
        self.customers = {}
        self._next_customer_id = 1
        self.checkout_calls = []
        self.portal_calls = []
        self.events = {}
        self.missing_customer_refs = set()
        self.canceled_subscription_refs = []
        self.refunded_subscription_refs = []
        # Set to an Exception instance to make the next refund_subscription_payment
        # call raise it, simulating a provider-side failure.
        self.refund_failure = None

    @property
    def name(self):
        return 'fake'

    def create_customer(self, *, email, user_id):
        ref = f'cus_fake_{self._next_customer_id}'
        self._next_customer_id += 1
        self.customers[ref] = {'email': email, 'user_id': user_id}
        return ref

    def create_checkout_session(self, *, customer_ref, plan, currency, success_url, cancel_url, user_id):
        if customer_ref in self.missing_customer_refs:
            raise CustomerNotFound(f'No such customer: {customer_ref!r}')
        self.checkout_calls.append({
            'customer_ref': customer_ref, 'plan': plan, 'currency': currency, 'user_id': user_id,
        })
        return CheckoutSession(url='https://fake.test/checkout/session')

    def create_portal_session(self, *, customer_ref, return_url, subscription_ref=None):
        if customer_ref in self.missing_customer_refs:
            raise CustomerNotFound(f'No such customer: {customer_ref!r}')
        self.portal_calls.append({
            'customer_ref': customer_ref, 'return_url': return_url, 'subscription_ref': subscription_ref,
        })
        return PortalSession(url='https://fake.test/portal/session')

    def queue_event(self, key, event):
        self.events[key] = event

    def parse_webhook_event(self, *, payload, headers):
        return self.events[payload]

    def cancel_subscription(self, *, subscription_ref):
        self.canceled_subscription_refs.append(subscription_ref)

    def refund_subscription_payment(self, *, subscription_ref):
        if self.refund_failure is not None:
            failure, self.refund_failure = self.refund_failure, None
            raise failure
        self.refunded_subscription_refs.append(subscription_ref)


def _make_user(username='alice', email='alice@example.com'):
    return User.objects.create_user(username=username, email=email, password='irrelevant123')


def _auth_headers(user):
    token = str(RefreshToken.for_user(user).access_token)
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


def _backdate_subscription(subscription, *, days):
    """auto_now_add blocks a normal .save() from changing created_at --
    this bypasses it the same way a real "subscribed a while ago" row
    would look, for testing withdrawal-window boundaries."""
    Subscription.objects.filter(pk=subscription.pk).update(
        created_at=django_timezone.now() - timedelta(days=days),
    )
    subscription.refresh_from_db()
    return subscription


class CancelActiveSubscriptionTests(TestCase):
    def setUp(self):
        self.provider = FakePaymentProvider()
        self.service = BillingService(provider=self.provider)
        self.user = _make_user()

    def _make_subscription(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        return Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )

    def test_cancels_active_subscription(self):
        self._make_subscription()

        self.service.cancel_active_subscription(user=self.user)

        self.assertEqual(self.provider.canceled_subscription_refs, ['sub_1'])

    def test_no_op_without_active_subscription(self):
        self.service.cancel_active_subscription(user=self.user)

        self.assertEqual(self.provider.canceled_subscription_refs, [])

    def test_attempts_refund_within_window_before_canceling(self):
        self._make_subscription()  # created_at defaults to now -- within the window

        self.service.cancel_active_subscription(user=self.user)

        self.assertEqual(self.provider.refunded_subscription_refs, ['sub_1'])
        self.assertEqual(self.provider.canceled_subscription_refs, ['sub_1'])

    def test_swallows_refund_failure_and_still_cancels(self):
        self._make_subscription()
        self.provider.refund_failure = RuntimeError('Stripe is down')

        self.service.cancel_active_subscription(user=self.user)  # must not raise

        self.assertEqual(self.provider.refunded_subscription_refs, [])
        self.assertEqual(self.provider.canceled_subscription_refs, ['sub_1'])

    def test_skips_refund_outside_window(self):
        subscription = self._make_subscription()
        _backdate_subscription(subscription, days=WITHDRAWAL_WINDOW_DAYS + 1)

        self.service.cancel_active_subscription(user=self.user)

        self.assertEqual(self.provider.refunded_subscription_refs, [])
        self.assertEqual(self.provider.canceled_subscription_refs, ['sub_1'])


class CancelWithRefundTests(TestCase):
    def setUp(self):
        self.provider = FakePaymentProvider()
        self.service = BillingService(provider=self.provider)
        self.user = _make_user()

    def _make_subscription(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        return Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )

    def test_refunds_and_cancels_within_window(self):
        self._make_subscription()

        result = self.service.cancel_with_refund(user=self.user)

        self.assertEqual(self.provider.refunded_subscription_refs, ['sub_1'])
        self.assertEqual(self.provider.canceled_subscription_refs, ['sub_1'])
        self.assertEqual(result.status, Subscription.Status.CANCELED)
        result.refresh_from_db()
        self.assertEqual(result.status, Subscription.Status.CANCELED)

    def test_raises_when_no_active_subscription(self):
        with self.assertRaises(NoActiveSubscriptionError):
            self.service.cancel_with_refund(user=self.user)

        self.assertEqual(self.provider.refunded_subscription_refs, [])
        self.assertEqual(self.provider.canceled_subscription_refs, [])

    def test_raises_when_window_expired(self):
        subscription = self._make_subscription()
        _backdate_subscription(subscription, days=WITHDRAWAL_WINDOW_DAYS + 1)

        with self.assertRaises(RefundWindowExpiredError):
            self.service.cancel_with_refund(user=self.user)

        self.assertEqual(self.provider.refunded_subscription_refs, [])
        self.assertEqual(self.provider.canceled_subscription_refs, [])

    def test_propagates_provider_error_and_does_not_cancel(self):
        self._make_subscription()
        self.provider.refund_failure = RuntimeError('Stripe is down')

        with self.assertRaises(RuntimeError):
            self.service.cancel_with_refund(user=self.user)

        self.assertEqual(self.provider.canceled_subscription_refs, [])


class StartCheckoutTests(TestCase):
    def setUp(self):
        self.provider = FakePaymentProvider()
        self.service = BillingService(provider=self.provider)
        self.user = _make_user()

    def test_creates_customer_once_across_calls(self):
        self.service.start_checkout(
            user=self.user, plan='monthly', currency='usd',
            success_url='https://app.test/success', cancel_url='https://app.test/cancel',
        )
        self.service.open_portal(user=self.user, return_url='https://app.test/billing')

        self.assertEqual(len(self.provider.customers), 1)
        self.assertEqual(PaymentCustomer.objects.filter(user=self.user).count(), 1)

    def test_rejects_when_already_subscribed(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )

        with self.assertRaises(AlreadySubscribedError):
            self.service.start_checkout(
                user=self.user, plan='annual', currency='usd',
                success_url='https://app.test/success', cancel_url='https://app.test/cancel',
            )

    def test_returns_checkout_url(self):
        url = self.service.start_checkout(
            user=self.user, plan='monthly', currency='brl',
            success_url='https://app.test/success', cancel_url='https://app.test/cancel',
        )
        self.assertEqual(url, 'https://fake.test/checkout/session')

    def test_recreates_customer_when_provider_reports_it_missing(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_stale',
        )
        self.provider.missing_customer_refs.add('cus_stale')

        url = self.service.start_checkout(
            user=self.user, plan='monthly', currency='usd',
            success_url='https://app.test/success', cancel_url='https://app.test/cancel',
        )

        self.assertEqual(url, 'https://fake.test/checkout/session')
        customer.refresh_from_db()
        self.assertNotEqual(customer.provider_customer_id, 'cus_stale')
        self.assertEqual(PaymentCustomer.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self.provider.checkout_calls[-1]['customer_ref'], customer.provider_customer_id)


class OpenPortalTests(TestCase):
    def setUp(self):
        self.provider = FakePaymentProvider()
        self.service = BillingService(provider=self.provider)
        self.user = _make_user()

    def test_raises_without_existing_customer(self):
        with self.assertRaises(NoPaymentCustomerError):
            self.service.open_portal(user=self.user, return_url='https://app.test/billing')

    def test_returns_portal_url_for_existing_customer(self):
        PaymentCustomer.objects.create(user=self.user, provider='fake', provider_customer_id='cus_fake_1')

        url = self.service.open_portal(user=self.user, return_url='https://app.test/billing')
        self.assertEqual(url, 'https://fake.test/portal/session')

    def test_recreates_customer_when_provider_reports_it_missing(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_stale',
        )
        self.provider.missing_customer_refs.add('cus_stale')

        url = self.service.open_portal(user=self.user, return_url='https://app.test/billing')

        self.assertEqual(url, 'https://fake.test/portal/session')
        customer.refresh_from_db()
        self.assertNotEqual(customer.provider_customer_id, 'cus_stale')

    def test_change_plan_passes_active_subscription_ref_through(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_fake_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )

        self.service.open_portal(user=self.user, return_url='https://app.test/billing', change_plan=True)

        self.assertEqual(self.provider.portal_calls[-1]['subscription_ref'], 'sub_fake_1')

    def test_change_plan_without_active_subscription_omits_subscription_ref(self):
        PaymentCustomer.objects.create(user=self.user, provider='fake', provider_customer_id='cus_fake_1')

        self.service.open_portal(user=self.user, return_url='https://app.test/billing', change_plan=True)

        self.assertIsNone(self.provider.portal_calls[-1]['subscription_ref'])

    def test_default_call_omits_subscription_ref(self):
        PaymentCustomer.objects.create(user=self.user, provider='fake', provider_customer_id='cus_fake_1')

        self.service.open_portal(user=self.user, return_url='https://app.test/billing')

        self.assertIsNone(self.provider.portal_calls[-1]['subscription_ref'])


class GetStatusTests(TestCase):
    def setUp(self):
        self.service = BillingService(provider=FakePaymentProvider())
        self.user = _make_user()

    def test_free_when_no_subscription(self):
        result = self.service.get_status(user=self.user)
        self.assertEqual(result.plan, 'free')
        self.assertIsNone(result.status)
        self.assertIsNone(result.refund_eligible_until)

    def test_returns_active_subscription_fields(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        period_end = datetime(2026, 1, 1, tzinfo=timezone.utc)
        Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='annual', currency='usd',
            status=Subscription.Status.ACTIVE, current_period_end=period_end,
        )

        result = self.service.get_status(user=self.user)
        self.assertEqual(result.plan, 'annual')
        self.assertEqual(result.status, 'active')
        self.assertEqual(result.current_period_end, period_end)

    def test_refund_eligible_until_set_for_recent_subscription(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        subscription = Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )

        result = self.service.get_status(user=self.user)

        self.assertEqual(
            result.refund_eligible_until,
            subscription.created_at + timedelta(days=WITHDRAWAL_WINDOW_DAYS),
        )

    def test_refund_eligible_until_null_for_old_subscription(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        subscription = Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )
        _backdate_subscription(subscription, days=WITHDRAWAL_WINDOW_DAYS + 1)

        result = self.service.get_status(user=self.user)

        self.assertIsNone(result.refund_eligible_until)


class HandleWebhookTests(TestCase):
    def setUp(self):
        self.provider = FakePaymentProvider()
        self.service = BillingService(provider=self.provider)
        self.user = _make_user()
        self.get_provider_patcher = patch('billing.services.get_provider', return_value=self.provider)
        self.get_provider_patcher.start()
        self.addCleanup(self.get_provider_patcher.stop)

    def test_checkout_completed_creates_subscription(self):
        self.provider.queue_event(b'evt_1', NormalizedEvent(
            provider_event_id='evt_1',
            type=NormalizedEventType.CHECKOUT_COMPLETED,
            customer_ref='cus_fake_1',
            subscription_ref='sub_1',
            price_ref='price_1',
            plan='monthly',
            currency='usd',
            status='active',
            user_id=self.user.id,
        ))

        self.service.handle_webhook(payload=b'evt_1', headers={}, provider_name='fake')

        subscription = Subscription.objects.get(provider_subscription_id='sub_1')
        self.assertEqual(subscription.plan, 'monthly')
        self.assertEqual(subscription.status, 'active')
        self.assertEqual(subscription.customer.user, self.user)

    def test_is_idempotent_on_repeated_event_id(self):
        event = NormalizedEvent(
            provider_event_id='evt_1', type=NormalizedEventType.CHECKOUT_COMPLETED,
            customer_ref='cus_fake_1', subscription_ref='sub_1', price_ref='price_1',
            plan='monthly', currency='usd', status='active', user_id=self.user.id,
        )
        self.provider.queue_event(b'evt_1', event)

        self.service.handle_webhook(payload=b'evt_1', headers={}, provider_name='fake')
        self.service.handle_webhook(payload=b'evt_1', headers={}, provider_name='fake')

        self.assertEqual(ProcessedWebhookEvent.objects.count(), 1)
        self.assertEqual(Subscription.objects.count(), 1)

    def test_subscription_updated_changes_status(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )
        self.provider.queue_event(b'evt_2', NormalizedEvent(
            provider_event_id='evt_2', type=NormalizedEventType.SUBSCRIPTION_UPDATED,
            subscription_ref='sub_1', status='past_due', cancel_at_period_end=True,
        ))

        self.service.handle_webhook(payload=b'evt_2', headers={}, provider_name='fake')

        subscription = Subscription.objects.get(provider_subscription_id='sub_1')
        self.assertEqual(subscription.status, 'past_due')
        self.assertTrue(subscription.cancel_at_period_end)

    def test_subscription_canceled_marks_canceled(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )
        self.provider.queue_event(b'evt_3', NormalizedEvent(
            provider_event_id='evt_3', type=NormalizedEventType.SUBSCRIPTION_CANCELED,
            subscription_ref='sub_1',
        ))

        self.service.handle_webhook(payload=b'evt_3', headers={}, provider_name='fake')

        subscription = Subscription.objects.get(provider_subscription_id='sub_1')
        self.assertEqual(subscription.status, Subscription.Status.CANCELED)

    def test_payment_failed_marks_past_due(self):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='fake', provider_customer_id='cus_fake_1',
        )
        Subscription.objects.create(
            customer=customer, provider='fake', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.ACTIVE,
        )
        self.provider.queue_event(b'evt_4', NormalizedEvent(
            provider_event_id='evt_4', type=NormalizedEventType.PAYMENT_FAILED,
            subscription_ref='sub_1',
        ))

        self.service.handle_webhook(payload=b'evt_4', headers={}, provider_name='fake')

        subscription = Subscription.objects.get(provider_subscription_id='sub_1')
        self.assertEqual(subscription.status, Subscription.Status.PAST_DUE)


@override_settings(
    STRIPE_PRICE_ID_MONTHLY_USD='price_monthly_usd',
    STRIPE_PRICE_ID_MONTHLY_BRL='price_monthly_brl',
    STRIPE_PRICE_ID_ANNUAL_USD='price_annual_usd',
    STRIPE_PRICE_ID_ANNUAL_BRL='price_annual_brl',
    STRIPE_WEBHOOK_SECRET='whsec_test',
)
class StripeProviderTests(TestCase):
    """Thin translation tests for the connector -- only file allowed to mock `stripe.*`."""

    def _provider(self):
        from billing.providers.stripe_provider import StripeProvider
        return StripeProvider()

    @patch('billing.providers.stripe_provider.stripe.Subscription.cancel')
    def test_cancel_subscription_calls_stripe_cancel(self, mock_cancel):
        self._provider().cancel_subscription(subscription_ref='sub_123')

        mock_cancel.assert_called_once_with('sub_123')

    @patch('billing.providers.stripe_provider.stripe.Subscription.cancel')
    def test_cancel_subscription_swallows_stripe_error(self, mock_cancel):
        # Best-effort by design (see cancel_subscription's docstring): used
        # when an account is being deleted, so a Stripe-side failure (e.g.
        # the subscription is already canceled/gone) must never propagate
        # and block that deletion.
        import stripe as stripe_sdk
        mock_cancel.side_effect = stripe_sdk.error.InvalidRequestError(
            "No such subscription: 'sub_123'", param='id', code='resource_missing',
        )

        self._provider().cancel_subscription(subscription_ref='sub_123')  # must not raise

    @patch('billing.providers.stripe_provider.stripe.Refund.create')
    @patch('billing.providers.stripe_provider.stripe.Subscription.retrieve')
    def test_refund_subscription_payment_uses_charge_when_payment_type_is_charge(
        self, mock_retrieve, mock_refund,
    ):
        mock_retrieve.return_value = {
            'latest_invoice': {
                'payments': {'data': [{'payment': {'type': 'charge', 'charge': 'ch_123'}}]},
            },
        }

        self._provider().refund_subscription_payment(subscription_ref='sub_123')

        mock_retrieve.assert_called_once_with('sub_123', expand=['latest_invoice.payments'])
        mock_refund.assert_called_once_with(charge='ch_123', reason='requested_by_customer')

    @patch('billing.providers.stripe_provider.stripe.Refund.create')
    @patch('billing.providers.stripe_provider.stripe.Subscription.retrieve')
    def test_refund_subscription_payment_uses_payment_intent_when_payment_type_is_payment_intent(
        self, mock_retrieve, mock_refund,
    ):
        mock_retrieve.return_value = {
            'latest_invoice': {
                'payments': {'data': [{'payment': {'type': 'payment_intent', 'payment_intent': 'pi_123'}}]},
            },
        }

        self._provider().refund_subscription_payment(subscription_ref='sub_123')

        mock_refund.assert_called_once_with(payment_intent='pi_123', reason='requested_by_customer')

    @patch('billing.providers.stripe_provider.stripe.Refund.create')
    @patch('billing.providers.stripe_provider.stripe.Subscription.retrieve')
    def test_refund_subscription_payment_raises_when_no_payments_found(self, mock_retrieve, mock_refund):
        mock_retrieve.return_value = {'latest_invoice': {'payments': {'data': []}}}

        with self.assertRaises(RefundTargetNotFoundError):
            self._provider().refund_subscription_payment(subscription_ref='sub_123')

        mock_refund.assert_not_called()

    @patch('billing.providers.stripe_provider.stripe.Refund.create')
    @patch('billing.providers.stripe_provider.stripe.Subscription.retrieve')
    def test_refund_subscription_payment_propagates_stripe_errors(self, mock_retrieve, mock_refund):
        # Contrast with cancel_subscription's swallow test above -- a refund
        # failure must NOT be swallowed here, callers need to know it failed.
        import stripe as stripe_sdk
        mock_retrieve.return_value = {
            'latest_invoice': {
                'payments': {'data': [{'payment': {'type': 'charge', 'charge': 'ch_123'}}]},
            },
        }
        mock_refund.side_effect = stripe_sdk.error.StripeError('Stripe is down')

        with self.assertRaises(stripe_sdk.error.StripeError):
            self._provider().refund_subscription_payment(subscription_ref='sub_123')

    @patch('billing.providers.stripe_provider.stripe.checkout.Session.create')
    def test_create_checkout_session_raises_customer_not_found_when_stripe_rejects_customer(self, mock_create):
        import stripe as stripe_sdk
        mock_create.side_effect = stripe_sdk.error.InvalidRequestError(
            "No such customer: 'cus_stale'", param='customer', code='resource_missing',
        )

        with self.assertRaises(CustomerNotFound):
            self._provider().create_checkout_session(
                customer_ref='cus_stale', plan='monthly', currency='usd',
                success_url='https://app.test/success', cancel_url='https://app.test/cancel', user_id=7,
            )

    @patch('billing.providers.stripe_provider.stripe.billing_portal.Session.create')
    def test_create_portal_session_without_subscription_ref_omits_flow_data(self, mock_create):
        mock_create.return_value = MagicMock(url='https://billing.stripe.test/session')

        self._provider().create_portal_session(customer_ref='cus_1', return_url='https://app.test/billing')

        _, kwargs = mock_create.call_args
        self.assertNotIn('flow_data', kwargs)

    @patch('billing.providers.stripe_provider.stripe.billing_portal.Session.create')
    def test_create_portal_session_with_subscription_ref_deep_links_to_plan_update(self, mock_create):
        mock_create.return_value = MagicMock(url='https://billing.stripe.test/session')

        self._provider().create_portal_session(
            customer_ref='cus_1', return_url='https://app.test/billing', subscription_ref='sub_1',
        )

        _, kwargs = mock_create.call_args
        self.assertEqual(kwargs['flow_data'], {
            'type': 'subscription_update',
            'subscription_update': {'subscription': 'sub_1'},
        })

    @patch('billing.providers.stripe_provider.stripe.checkout.Session.create')
    def test_create_checkout_session_maps_plan_and_currency_to_price_id(self, mock_create):
        mock_create.return_value = MagicMock(url='https://checkout.stripe.test/session')

        self._provider().create_checkout_session(
            customer_ref='cus_1', plan='annual', currency='brl',
            success_url='https://app.test/success', cancel_url='https://app.test/cancel', user_id=7,
        )

        _, kwargs = mock_create.call_args
        self.assertEqual(kwargs['line_items'][0]['price'], 'price_annual_brl')
        self.assertEqual(kwargs['client_reference_id'], '7')
        self.assertTrue(kwargs['allow_promotion_codes'])
        self.assertEqual(kwargs['payment_method_collection'], 'if_required')

    @patch('billing.providers.stripe_provider.stripe.Webhook.construct_event')
    def test_parse_webhook_event_raises_on_invalid_signature(self, mock_construct):
        import stripe as stripe_sdk
        mock_construct.side_effect = stripe_sdk.error.SignatureVerificationError('bad sig', 'sig_header')

        with self.assertRaises(InvalidWebhookSignature):
            self._provider().parse_webhook_event(payload=b'{}', headers={'Stripe-Signature': 'bad'})

    @patch('billing.providers.stripe_provider.stripe.Subscription.retrieve')
    @patch('billing.providers.stripe_provider.stripe.Webhook.construct_event')
    def test_parse_webhook_event_normalizes_checkout_completed(self, mock_construct, mock_retrieve):
        mock_construct.return_value = MagicMock(
            id='evt_1',
            type='checkout.session.completed',
            data=MagicMock(object={
                'mode': 'subscription', 'customer': 'cus_1', 'subscription': 'sub_1',
                'client_reference_id': '7', 'metadata': {'user_id': '7'},
            }),
        )
        mock_retrieve.return_value = {
            'id': 'sub_1',
            'status': 'active',
            'cancel_at_period_end': False,
            'cancel_at': None,
            'items': {'data': [{
                'price': {'id': 'price_monthly_usd'},
                'current_period_end': 1893456000,
            }]},
        }

        event = self._provider().parse_webhook_event(payload=b'{}', headers={'Stripe-Signature': 'sig'})

        self.assertEqual(event.type, NormalizedEventType.CHECKOUT_COMPLETED)
        self.assertEqual(event.plan, 'monthly')
        self.assertEqual(event.currency, 'usd')
        self.assertEqual(event.user_id, 7)

    @patch('billing.providers.stripe_provider.stripe.Webhook.construct_event')
    def test_parse_webhook_event_detects_cancel_via_cancel_at_timestamp(self, mock_construct):
        # Newer Stripe API versions leave cancel_at_period_end=False even when
        # a Billing Portal cancellation is scheduled, signalling it via
        # cancel_at (a timestamp) instead. Regression test for that mismatch.
        mock_construct.return_value = MagicMock(
            id='evt_1',
            type='customer.subscription.updated',
            data=MagicMock(object={
                'customer': 'cus_1',
                'id': 'sub_1',
                'status': 'active',
                'cancel_at_period_end': False,
                'cancel_at': 1893456000,
                'items': {'data': [{
                    'price': {'id': 'price_monthly_usd'},
                    'current_period_end': 1893456000,
                }]},
            }),
        )

        event = self._provider().parse_webhook_event(payload=b'{}', headers={'Stripe-Signature': 'sig'})

        self.assertTrue(event.cancel_at_period_end)


class CancelWithRefundViewTests(ApiTestCase):
    """View-level status-code mapping for POST /api/billing/cancel-with-refund/.

    Patches at the service boundary (matching the pattern used for
    account-deletion's endpoint test in accounts/tests.py) rather than
    mocking Stripe directly -- the Stripe-SDK-level behavior itself is
    already covered by StripeProviderTests above."""

    def setUp(self):
        self.user = _make_user()
        self.headers = _auth_headers(self.user)

    def _post(self):
        return self.client.post('/api/billing/cancel-with-refund/', **self.headers)

    def test_requires_authentication(self):
        response = self.client.post('/api/billing/cancel-with-refund/')

        self.assertEqual(response.status_code, 401)

    @patch('billing.services.BillingService.cancel_with_refund')
    def test_returns_404_without_active_subscription(self, mock_cancel):
        mock_cancel.side_effect = NoActiveSubscriptionError('no subscription')

        response = self._post()

        self.assertEqual(response.status_code, 404)

    @patch('billing.services.BillingService.cancel_with_refund')
    def test_returns_400_when_window_expired(self, mock_cancel):
        mock_cancel.side_effect = RefundWindowExpiredError('window passed')

        response = self._post()

        self.assertEqual(response.status_code, 400)

    @patch('billing.services.BillingService.cancel_with_refund')
    def test_returns_502_on_refund_target_not_found(self, mock_cancel):
        mock_cancel.side_effect = RefundTargetNotFoundError('nothing to refund')

        response = self._post()

        self.assertEqual(response.status_code, 502)

    @patch('billing.services.BillingService.cancel_with_refund')
    def test_returns_502_on_unexpected_provider_error(self, mock_cancel):
        mock_cancel.side_effect = RuntimeError('Stripe is down')

        response = self._post()

        self.assertEqual(response.status_code, 502)

    @patch('billing.services.BillingService.cancel_with_refund')
    def test_returns_200_with_updated_subscription_on_success(self, mock_cancel):
        customer = PaymentCustomer.objects.create(
            user=self.user, provider='stripe', provider_customer_id='cus_1',
        )
        subscription = Subscription.objects.create(
            customer=customer, provider='stripe', provider_subscription_id='sub_1',
            provider_price_id='price_1', plan='monthly', currency='usd',
            status=Subscription.Status.CANCELED,
        )
        mock_cancel.return_value = subscription

        response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['plan'], 'monthly')
        self.assertEqual(body['status'], 'canceled')
        self.assertIsNone(body['refund_eligible_until'])
