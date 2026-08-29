import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from eyelearn.test_utils import ApiTestCase
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from billing.models import PaymentCustomer, Subscription
from flashcards.models import Collection, CollectionGoal, Flashcard, FlashcardMedia, ReviewLog, ReviewState, StudyDay
from flashcards.services import (
    CollectionCycleError,
    CollectionService,
    CrossOwnerParentError,
    FREE_FLASHCARD_LIMIT,
    FlashcardService,
    GoalService,
    ReviewService,
    StreakService,
)

User = get_user_model()


def _make_user(username='alice', email='alice@example.com'):
    return User.objects.create_user(username=username, email=email, password='irrelevant123')


def _make_collection(user, name='Deck', parent=None):
    return Collection.objects.create(user=user, name=name, parent=parent)


def _make_flashcard(collection, card_type=Flashcard.CardType.BASIC, **kwargs):
    defaults = {'prompt': 'question', 'answer': 'answer'}
    defaults.update(kwargs)
    return Flashcard.objects.create(collection=collection, card_type=card_type, **defaults)


def _auth_headers(user):
    token = str(RefreshToken.for_user(user).access_token)
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


class CollectionCRUDTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.other_user = _make_user(username='bob', email='bob@example.com')
        self.headers = _auth_headers(self.user)

    def test_create_collection(self):
        response = self.client.post('/api/flashcards/collections/', {'name': 'Spanish'}, **self.headers)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['name'], 'Spanish')
        self.assertTrue(Collection.objects.filter(user=self.user, name='Spanish').exists())

    def test_list_collections_scoped_to_user(self):
        _make_collection(self.user, name='Mine')
        _make_collection(self.other_user, name='Theirs')

        response = self.client.get('/api/flashcards/collections/', **self.headers)

        self.assertEqual([c['name'] for c in response.json()], ['Mine'])

    def test_retrieve_other_users_collection_404s(self):
        collection = _make_collection(self.other_user)

        response = self.client.get(f'/api/flashcards/collections/{collection.id}/', **self.headers)

        self.assertEqual(response.status_code, 404)

    def test_update_collection_name(self):
        collection = _make_collection(self.user, name='Old')

        response = self.client.patch(
            f'/api/flashcards/collections/{collection.id}/',
            data=json.dumps({'name': 'New'}), content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        collection.refresh_from_db()
        self.assertEqual(collection.name, 'New')

    def test_delete_collection_cascades_to_children_and_flashcards(self):
        parent = _make_collection(self.user, name='Parent')
        child = _make_collection(self.user, name='Child', parent=parent)
        flashcard = _make_flashcard(child)

        response = self.client.delete(f'/api/flashcards/collections/{parent.id}/', **self.headers)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Collection.objects.filter(id=child.id).exists())
        self.assertFalse(Flashcard.objects.filter(id=flashcard.id).exists())


class CollectionNestingTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.other_user = _make_user(username='bob', email='bob@example.com')
        self.service = CollectionService()

    def test_deep_nesting_get_subtree_ids(self):
        root = _make_collection(self.user, name='Root')
        mid = _make_collection(self.user, name='Mid', parent=root)
        leaf = _make_collection(self.user, name='Leaf', parent=mid)
        sibling = _make_collection(self.user, name='Sibling')

        subtree = self.service.get_subtree_ids(collection=root)

        self.assertEqual(subtree, {root.id, mid.id, leaf.id})
        self.assertNotIn(sibling.id, subtree)

    def test_reparent_rejects_self_as_parent(self):
        collection = _make_collection(self.user)

        with self.assertRaises(CollectionCycleError):
            self.service.update_collection(collection=collection, parent=collection)

    def test_reparent_rejects_descendant_as_parent(self):
        root = _make_collection(self.user, name='Root')
        child = _make_collection(self.user, name='Child', parent=root)

        with self.assertRaises(CollectionCycleError):
            self.service.update_collection(collection=root, parent=child)

    def test_reparent_rejects_cross_owner_parent(self):
        collection = _make_collection(self.user)
        other_collection = _make_collection(self.other_user)

        with self.assertRaises(CrossOwnerParentError):
            self.service.update_collection(collection=collection, parent=other_collection)


class FlashcardCRUDTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    def _post(self, payload):
        return self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/flashcards/',
            data=json.dumps(payload), content_type='application/json', **self.headers,
        )

    def test_create_basic_card(self):
        response = self._post({'card_type': 'basic', 'prompt': 'Capital of France?', 'answer': 'Paris'})
        self.assertEqual(response.status_code, 201)

    def test_create_multiple_choice_card(self):
        response = self._post({
            'card_type': 'multiple_choice',
            'prompt': 'Capital of France?',
            'options': [{'text': 'Paris', 'is_correct': True}, {'text': 'Lyon', 'is_correct': False}],
        })
        self.assertEqual(response.status_code, 201)

    def test_multiple_choice_requires_exactly_one_correct_option(self):
        response = self._post({
            'card_type': 'multiple_choice',
            'prompt': 'Capital of France?',
            'options': [{'text': 'Paris', 'is_correct': True}, {'text': 'Lyon', 'is_correct': True}],
        })
        self.assertEqual(response.status_code, 400)

    def test_multiple_choice_requires_at_least_two_options(self):
        response = self._post({
            'card_type': 'multiple_choice',
            'prompt': 'Capital of France?',
            'options': [{'text': 'Paris', 'is_correct': True}],
        })
        self.assertEqual(response.status_code, 400)

    def test_create_typed_answer_card(self):
        response = self._post({
            'card_type': 'typed_answer', 'prompt': 'Capital of France?', 'answer': 'Paris',
            'accepted_answers': ['paris, france'],
        })
        self.assertEqual(response.status_code, 201)

    def test_typed_answer_requires_canonical_answer(self):
        response = self._post({'card_type': 'typed_answer', 'prompt': 'Capital of France?'})
        self.assertEqual(response.status_code, 400)

    def test_other_users_flashcard_404s(self):
        other_user = _make_user(username='bob', email='bob@example.com')
        other_collection = _make_collection(other_user)
        flashcard = _make_flashcard(other_collection)

        response = self.client.get(f'/api/flashcards/flashcards/{flashcard.id}/', **self.headers)

        self.assertEqual(response.status_code, 404)


class FlashcardLimitTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    def _bulk_create_flashcards(self, count):
        Flashcard.objects.bulk_create([
            Flashcard(collection=self.collection, card_type=Flashcard.CardType.BASIC, prompt=f'q{i}', answer=f'a{i}')
            for i in range(count)
        ])

    def _post_flashcard(self, prompt='One more?'):
        return self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/flashcards/',
            data=json.dumps({'card_type': 'basic', 'prompt': prompt, 'answer': 'Yes'}),
            content_type='application/json', **self.headers,
        )

    def test_creation_allowed_just_under_limit(self):
        self._bulk_create_flashcards(FREE_FLASHCARD_LIMIT - 1)

        response = self._post_flashcard()

        self.assertEqual(response.status_code, 201)

    def test_creation_blocked_at_limit(self):
        self._bulk_create_flashcards(FREE_FLASHCARD_LIMIT)

        response = self._post_flashcard('One too many?')

        self.assertEqual(response.status_code, 402)
        self.assertIn(str(FREE_FLASHCARD_LIMIT), response.json()['detail'])

    def test_subscribed_user_not_capped(self):
        customer = PaymentCustomer.objects.create(user=self.user, provider_customer_id='cus_test')
        Subscription.objects.create(
            customer=customer, provider_subscription_id='sub_test', provider_price_id='price_test',
            plan=Subscription.Plan.MONTHLY, currency='usd', status=Subscription.Status.ACTIVE,
        )
        self._bulk_create_flashcards(FREE_FLASHCARD_LIMIT + 5)

        response = self._post_flashcard('Pro card?')

        self.assertEqual(response.status_code, 201)


class MediaUploadFlowTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)
        self.flashcard = _make_flashcard(self.collection)

    @patch('flashcards.services.storage.generate_upload_url', return_value='https://bucket.example/presigned-put')
    def test_upload_url_request_returns_presigned_url_and_key(self, mock_generate):
        response = self.client.post(
            f'/api/flashcards/flashcards/{self.flashcard.id}/media/upload-url/',
            data=json.dumps({
                'media_type': 'image', 'side': 'prompt', 'content_type': 'image/png',
                'filename': 'diagram.png', 'size_bytes': 1024,
            }),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['upload_url'], 'https://bucket.example/presigned-put')
        self.assertTrue(body['storage_key'].startswith(f'flashcards/{self.user.id}/{self.flashcard.id}/image/'))
        mock_generate.assert_called_once()

    def test_upload_url_rejects_unsupported_content_type(self):
        response = self.client.post(
            f'/api/flashcards/flashcards/{self.flashcard.id}/media/upload-url/',
            data=json.dumps({
                'media_type': 'image', 'side': 'prompt', 'content_type': 'application/zip',
                'filename': 'file.zip', 'size_bytes': 1024,
            }),
            content_type='application/json', **self.headers,
        )
        self.assertEqual(response.status_code, 400)

    @patch('flashcards.serializers.storage.generate_download_url', return_value='https://bucket.example/get')
    def test_confirm_creates_media_row(self, mock_generate):
        response = self.client.post(
            f'/api/flashcards/flashcards/{self.flashcard.id}/media/confirm/',
            data=json.dumps({
                'storage_key': 'flashcards/1/1/image/abc.png', 'media_type': 'image',
                'side': 'prompt', 'content_type': 'image/png', 'size_bytes': 1024,
            }),
            content_type='application/json', **self.headers,
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(FlashcardMedia.objects.filter(flashcard=self.flashcard).exists())

    @patch('flashcards.services.storage.delete_object')
    def test_delete_removes_row_and_triggers_cleanup(self, mock_delete):
        media = FlashcardMedia.objects.create(
            flashcard=self.flashcard, media_type='image', side='prompt',
            storage_key='flashcards/1/1/image/abc.png', content_type='image/png', size_bytes=1024,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f'/api/flashcards/media/{media.id}/', **self.headers)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(FlashcardMedia.objects.filter(id=media.id).exists())
        mock_delete.assert_called_once_with(key='flashcards/1/1/image/abc.png')


class ReviewSchedulingTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.collection = _make_collection(self.user)
        self.service = ReviewService()

    def test_first_review_creates_review_state(self):
        flashcard = _make_flashcard(self.collection)
        self.assertFalse(ReviewState.objects.filter(user=self.user, flashcard=flashcard).exists())

        review_state, correct = self.service.submit_review(
            user=self.user, flashcard=flashcard, rating=ReviewLog.Rating.GOOD,
        )

        self.assertIsNone(correct)
        self.assertEqual(review_state.reps, 1)
        self.assertIsNotNone(review_state.last_review)
        self.assertGreater(review_state.due, review_state.last_review)
        self.assertEqual(ReviewLog.objects.filter(review_state=review_state).count(), 1)

    def test_again_rating_from_review_state_increments_lapses(self):
        flashcard = _make_flashcard(self.collection)
        review_state, _ = self.service.submit_review(
            user=self.user, flashcard=flashcard, rating=ReviewLog.Rating.GOOD,
        )
        # Force into REVIEW state so the next "Again" counts as a lapse (forgetting a known card).
        review_state.state = ReviewState.State.REVIEW
        review_state.save()

        review_state, _ = self.service.submit_review(
            user=self.user, flashcard=flashcard, rating=ReviewLog.Rating.AGAIN,
        )

        self.assertEqual(review_state.lapses, 1)

    def test_multiple_choice_correct_answer_maps_to_good_rating(self):
        flashcard = _make_flashcard(
            self.collection, card_type=Flashcard.CardType.MULTIPLE_CHOICE, answer='',
            options=[{'text': 'Paris', 'is_correct': True}, {'text': 'Lyon', 'is_correct': False}],
        )

        review_state, correct = self.service.submit_review(user=self.user, flashcard=flashcard, selected_option=0)

        self.assertTrue(correct)
        self.assertEqual(ReviewLog.objects.get(review_state=review_state).rating, ReviewLog.Rating.GOOD)

    def test_multiple_choice_incorrect_answer_maps_to_again_rating(self):
        flashcard = _make_flashcard(
            self.collection, card_type=Flashcard.CardType.MULTIPLE_CHOICE, answer='',
            options=[{'text': 'Paris', 'is_correct': True}, {'text': 'Lyon', 'is_correct': False}],
        )

        review_state, correct = self.service.submit_review(user=self.user, flashcard=flashcard, selected_option=1)

        self.assertFalse(correct)
        self.assertEqual(ReviewLog.objects.get(review_state=review_state).rating, ReviewLog.Rating.AGAIN)

    def test_typed_answer_accepts_case_and_whitespace_insensitive_match(self):
        flashcard = _make_flashcard(
            self.collection, card_type=Flashcard.CardType.TYPED_ANSWER, answer='Paris',
            accepted_answers=['Paris, France'],
        )

        _, correct = self.service.submit_review(user=self.user, flashcard=flashcard, submitted_answer='  paris  ')

        self.assertTrue(correct)

    def test_typed_answer_rejects_wrong_text(self):
        flashcard = _make_flashcard(self.collection, card_type=Flashcard.CardType.TYPED_ANSWER, answer='Paris')

        _, correct = self.service.submit_review(user=self.user, flashcard=flashcard, submitted_answer='London')

        self.assertFalse(correct)

    def test_review_endpoint_basic_card(self):
        flashcard = _make_flashcard(self.collection)

        response = self.client.post(
            f'/api/flashcards/flashcards/{flashcard.id}/review/',
            data=json.dumps({'rating': 3}), content_type='application/json', **_auth_headers(self.user),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body['correct'])
        self.assertEqual(body['reps'], 1)


class StudyQueueTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.other_user = _make_user(username='bob', email='bob@example.com')
        self.root = _make_collection(self.user, name='Root')
        self.child = _make_collection(self.user, name='Child', parent=self.root)
        self.service = ReviewService()

    def test_new_cards_are_due_immediately(self):
        flashcard = _make_flashcard(self.root)

        queue = self.service.build_study_queue(user=self.user, collection=self.root)

        self.assertEqual([rs.flashcard_id for rs in queue], [flashcard.id])

    def test_includes_cards_from_nested_child_collections(self):
        child_flashcard = _make_flashcard(self.child)

        queue = self.service.build_study_queue(user=self.user, collection=self.root)

        self.assertIn(child_flashcard.id, [rs.flashcard_id for rs in queue])

    def test_excludes_not_yet_due_cards(self):
        flashcard = _make_flashcard(self.root)
        ReviewState.objects.create(user=self.user, flashcard=flashcard, due=timezone.now() + timedelta(days=5))

        queue = self.service.build_study_queue(user=self.user, collection=self.root)

        self.assertEqual(queue, [])

    def test_orders_by_due_ascending(self):
        later = _make_flashcard(self.root, prompt='later')
        sooner = _make_flashcard(self.root, prompt='sooner')
        now = timezone.now()
        ReviewState.objects.create(user=self.user, flashcard=later, due=now - timedelta(minutes=5))
        ReviewState.objects.create(user=self.user, flashcard=sooner, due=now - timedelta(minutes=10))

        queue = self.service.build_study_queue(user=self.user, collection=self.root, now=now)

        self.assertEqual([rs.flashcard_id for rs in queue], [sooner.id, later.id])

    def test_scoped_to_requesting_user(self):
        flashcard = _make_flashcard(self.root)
        ReviewState.objects.create(user=self.other_user, flashcard=flashcard, due=timezone.now())

        queue = self.service.build_study_queue(user=self.user, collection=self.root)

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].user_id, self.user.id)

    def test_respects_limit(self):
        _make_flashcard(self.root, prompt='a')
        _make_flashcard(self.root, prompt='b')

        queue = self.service.build_study_queue(user=self.user, collection=self.root, limit=1)

        self.assertEqual(len(queue), 1)

    def test_endpoint_returns_due_cards(self):
        flashcard = _make_flashcard(self.root)

        response = self.client.get(
            f'/api/flashcards/collections/{self.root.id}/study-queue/', **_auth_headers(self.user),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]['flashcard']['id'], flashcard.id)

    def test_count_due_matches_build_study_queue_length(self):
        _make_flashcard(self.root)
        _make_flashcard(self.child)
        not_yet_due = _make_flashcard(self.root, prompt='future')
        ReviewState.objects.create(user=self.user, flashcard=not_yet_due, due=timezone.now() + timedelta(days=5))

        self.assertEqual(self.service.count_due(user=self.user, collection=self.root), 2)

    def test_count_due_zero_when_no_flashcards(self):
        self.assertEqual(self.service.count_due(user=self.user, collection=self.root), 0)

    def test_collection_list_endpoint_includes_due_count(self):
        _make_flashcard(self.root)
        _make_flashcard(self.root)

        response = self.client.get('/api/flashcards/collections/', **_auth_headers(self.user))

        body = response.json()
        root_entry = next(item for item in body if item['id'] == self.root.id)
        self.assertEqual(root_entry['due_count'], 2)
        self.assertEqual(root_entry['flashcard_count'], 2)


def _master(user, flashcard):
    """Mark a flashcard as mastered (ReviewState.State.REVIEW) for a user."""
    return ReviewState.objects.create(
        user=user, flashcard=flashcard, due=timezone.now(), state=ReviewState.State.REVIEW,
    )


class CollectionGoalTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.collection = _make_collection(self.user)
        self.headers = _auth_headers(self.user)
        self.today = timezone.now().date()

    def test_get_404s_when_unset(self):
        response = self.client.get(f'/api/flashcards/collections/{self.collection.id}/goal/', **self.headers)
        self.assertEqual(response.status_code, 404)

    def test_put_creates_goal(self):
        target = self.today + timedelta(days=10)
        response = self.client.put(
            f'/api/flashcards/collections/{self.collection.id}/goal/',
            data=json.dumps({'target_date': target.isoformat()}), content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['target_date'], target.isoformat())
        self.assertTrue(CollectionGoal.objects.filter(collection=self.collection).exists())

    def test_delete_clears_goal(self):
        CollectionGoal.objects.create(collection=self.collection, target_date=self.today + timedelta(days=5))

        response = self.client.delete(f'/api/flashcards/collections/{self.collection.id}/goal/', **self.headers)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(CollectionGoal.objects.filter(collection=self.collection).exists())

    def test_today_target_zero_when_nothing_remaining(self):
        goal = CollectionGoal.objects.create(collection=self.collection, target_date=self.today + timedelta(days=5))
        progress = GoalService().get_goal_progress(user=self.user, collection=self.collection, goal=goal, today=self.today)

        self.assertEqual(progress['total'], 0)
        self.assertEqual(progress['today_target'], 0)
        self.assertFalse(progress['overdue'])

    def test_today_target_due_today_equals_remaining(self):
        for _ in range(3):
            _make_flashcard(self.collection)
        goal = CollectionGoal.objects.create(collection=self.collection, target_date=self.today)

        progress = GoalService().get_goal_progress(user=self.user, collection=self.collection, goal=goal, today=self.today)

        self.assertEqual(progress['remaining'], 3)
        self.assertEqual(progress['today_target'], 3)
        self.assertFalse(progress['overdue'])

    def test_overdue_target_date_still_owes_all_remaining(self):
        for _ in range(4):
            _make_flashcard(self.collection)
        goal = CollectionGoal.objects.create(collection=self.collection, target_date=self.today - timedelta(days=3))

        progress = GoalService().get_goal_progress(user=self.user, collection=self.collection, goal=goal, today=self.today)

        self.assertEqual(progress['today_target'], 4)
        self.assertTrue(progress['overdue'])

    def test_today_target_paced_across_remaining_days(self):
        for _ in range(10):
            _make_flashcard(self.collection)
        goal = CollectionGoal.objects.create(collection=self.collection, target_date=self.today + timedelta(days=5))

        progress = GoalService().get_goal_progress(user=self.user, collection=self.collection, goal=goal, today=self.today)

        self.assertEqual(progress['remaining'], 10)
        self.assertEqual(progress['today_target'], 2)  # ceil(10/5)

    def test_today_target_decreases_after_reviewing_today(self):
        cards = [_make_flashcard(self.collection, prompt=f'card-{i}') for i in range(5)]
        goal = CollectionGoal.objects.create(collection=self.collection, target_date=self.today)

        progress = GoalService().get_goal_progress(user=self.user, collection=self.collection, goal=goal, today=self.today)
        self.assertEqual(progress['today_target'], 5)

        for card in cards[:2]:
            ReviewService().submit_review(user=self.user, flashcard=card, rating=ReviewLog.Rating.GOOD)

        progress = GoalService().get_goal_progress(user=self.user, collection=self.collection, goal=goal, today=self.today)
        self.assertEqual(progress['reviewed_today'], 2)
        self.assertEqual(progress['today_target'], 3)


class GoalServiceTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.collection = _make_collection(self.user)

    def test_mastered_counts_only_review_state_cards(self):
        mastered_card = _make_flashcard(self.collection, prompt='mastered')
        _master(self.user, mastered_card)

        learning_card = _make_flashcard(self.collection, prompt='learning')
        ReviewState.objects.create(
            user=self.user, flashcard=learning_card, due=timezone.now(), state=ReviewState.State.LEARNING,
        )

        _make_flashcard(self.collection, prompt='untouched-new')

        goal = CollectionGoal.objects.create(
            collection=self.collection, target_date=timezone.now().date() + timedelta(days=1),
        )
        progress = GoalService().get_goal_progress(user=self.user, collection=self.collection, goal=goal)

        self.assertEqual(progress['total'], 3)
        self.assertEqual(progress['mastered'], 1)
        self.assertEqual(progress['remaining'], 2)


class DailyStudyQueueTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.root = _make_collection(self.user, name='Root')
        self.child = _make_collection(self.user, name='Child', parent=self.root)
        self.today = timezone.now().date()

    def test_parent_and_child_goals_dedupe_overlapping_due_cards(self):
        shared_card = _make_flashcard(self.child, prompt='shared')
        CollectionGoal.objects.create(collection=self.root, target_date=self.today)
        CollectionGoal.objects.create(collection=self.child, target_date=self.today)

        queue = GoalService().build_daily_study_queue(user=self.user)

        self.assertEqual([rs.flashcard_id for rs in queue], [shared_card.id])

    def test_goal_with_zero_target_contributes_nothing(self):
        # No flashcards at all in this collection -> remaining=0 -> today_target=0.
        CollectionGoal.objects.create(collection=self.root, target_date=self.today)

        queue = GoalService().build_daily_study_queue(user=self.user)

        self.assertEqual(queue, [])

    def test_collection_without_goal_is_excluded(self):
        _make_flashcard(self.root)  # no CollectionGoal created

        queue = GoalService().build_daily_study_queue(user=self.user)

        self.assertEqual(queue, [])

    def test_respects_per_goal_today_target_cap(self):
        for i in range(5):
            _make_flashcard(self.root, prompt=f'card-{i}')
        # 5 remaining, due in 5 days -> today_target = 1.
        CollectionGoal.objects.create(collection=self.root, target_date=self.today + timedelta(days=5))

        queue = GoalService().build_daily_study_queue(user=self.user)

        self.assertEqual(len(queue), 1)

    def test_daily_queue_shrinks_after_partial_progress_instead_of_restarting(self):
        for i in range(6):
            _make_flashcard(self.root, prompt=f'card-{i}')
        # 6 remaining, due in 3 days -> today_target = 2.
        CollectionGoal.objects.create(collection=self.root, target_date=self.today + timedelta(days=3))

        first_queue = GoalService().build_daily_study_queue(user=self.user)
        self.assertEqual(len(first_queue), 2)

        # Simulate leaving the study page after reviewing what was shown, then coming back.
        for review_state in first_queue:
            ReviewService().submit_review(user=self.user, flashcard=review_state.flashcard, rating=ReviewLog.Rating.GOOD)

        second_queue = GoalService().build_daily_study_queue(user=self.user)
        reviewed_ids = {rs.flashcard_id for rs in first_queue}
        second_ids = {rs.flashcard_id for rs in second_queue}

        self.assertEqual(len(second_queue), 0)
        self.assertFalse(reviewed_ids & second_ids)


class CustomStudyQueueTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.other_user = _make_user(username='bob', email='bob@example.com')
        self.collection_a = _make_collection(self.user, name='A')
        self.collection_b = _make_collection(self.user, name='B')
        self.headers = _auth_headers(self.user)

    def test_merges_multiple_collections_without_goal_capping(self):
        card_a = _make_flashcard(self.collection_a, prompt='a')
        card_b = _make_flashcard(self.collection_b, prompt='b')

        queue = ReviewService().build_multi_collection_queue(
            user=self.user, collection_ids=[self.collection_a.id, self.collection_b.id],
        )

        self.assertEqual({rs.flashcard_id for rs in queue}, {card_a.id, card_b.id})

    def test_endpoint_silently_drops_other_users_collection_id(self):
        _make_flashcard(self.collection_a)
        other_collection = _make_collection(self.other_user, name='Not yours')
        _make_flashcard(other_collection)

        response = self.client.get(
            f'/api/flashcards/study/custom/?collections={self.collection_a.id},{other_collection.id}',
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_endpoint_requires_at_least_one_collection_id(self):
        response = self.client.get('/api/flashcards/study/custom/?collections=', **self.headers)
        self.assertEqual(response.status_code, 400)


class StreakServiceTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.service = StreakService()
        self.today = timezone.now().date()

    def test_zero_when_no_study_days(self):
        self.assertEqual(self.service.get_current_streak(user=self.user, today=self.today), 0)

    def test_gap_free_run_counts_consecutive_days(self):
        for offset in range(3):
            StudyDay.objects.create(user=self.user, date=self.today - timedelta(days=offset), cards_reviewed=1)

        self.assertEqual(self.service.get_current_streak(user=self.user, today=self.today), 3)

    def test_studied_yesterday_not_today_still_counts(self):
        StudyDay.objects.create(user=self.user, date=self.today - timedelta(days=1), cards_reviewed=1)
        StudyDay.objects.create(user=self.user, date=self.today - timedelta(days=2), cards_reviewed=1)

        self.assertEqual(self.service.get_current_streak(user=self.user, today=self.today), 2)

    def test_broken_streak_stops_at_the_gap(self):
        StudyDay.objects.create(user=self.user, date=self.today, cards_reviewed=1)
        StudyDay.objects.create(user=self.user, date=self.today - timedelta(days=1), cards_reviewed=1)
        # Gap at days=2
        StudyDay.objects.create(user=self.user, date=self.today - timedelta(days=3), cards_reviewed=1)

        self.assertEqual(self.service.get_current_streak(user=self.user, today=self.today), 2)


class StudyDayUpsertTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.collection = _make_collection(self.user)
        self.service = ReviewService()

    def test_two_reviews_same_day_upsert_into_one_row(self):
        card_one = _make_flashcard(self.collection, prompt='one')
        card_two = _make_flashcard(self.collection, prompt='two')
        now = timezone.now()

        self.service.submit_review(user=self.user, flashcard=card_one, rating=ReviewLog.Rating.GOOD, reviewed_at=now)
        self.service.submit_review(user=self.user, flashcard=card_two, rating=ReviewLog.Rating.GOOD, reviewed_at=now)

        self.assertEqual(StudyDay.objects.filter(user=self.user).count(), 1)
        self.assertEqual(StudyDay.objects.get(user=self.user).cards_reviewed, 2)

    def test_reviews_on_different_days_create_separate_rows(self):
        card_one = _make_flashcard(self.collection, prompt='one')
        card_two = _make_flashcard(self.collection, prompt='two')
        day_one = timezone.now()
        day_two = day_one + timedelta(days=1)

        self.service.submit_review(user=self.user, flashcard=card_one, rating=ReviewLog.Rating.GOOD, reviewed_at=day_one)
        self.service.submit_review(user=self.user, flashcard=card_two, rating=ReviewLog.Rating.GOOD, reviewed_at=day_two)

        self.assertEqual(StudyDay.objects.filter(user=self.user).count(), 2)


class StreakCalendarEndpointTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.headers = _auth_headers(self.user)
        self.today = timezone.now().date()

    def test_shape_and_studied_correctness(self):
        StudyDay.objects.create(user=self.user, date=self.today, cards_reviewed=4)
        start = self.today - timedelta(days=1)

        response = self.client.get(
            f'/api/flashcards/streak/?start={start.isoformat()}&end={self.today.isoformat()}', **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['current_streak'], 1)
        self.assertEqual(len(body['days']), 2)
        self.assertEqual(body['days'][0]['studied'], False)
        self.assertEqual(body['days'][1]['studied'], True)
        self.assertEqual(body['days'][1]['cards_reviewed'], 4)

    def test_oversized_range_rejected(self):
        start = self.today - timedelta(days=400)

        response = self.client.get(
            f'/api/flashcards/streak/?start={start.isoformat()}&end={self.today.isoformat()}', **self.headers,
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_params_rejected(self):
        response = self.client.get('/api/flashcards/streak/', **self.headers)
        self.assertEqual(response.status_code, 400)


class GoalsSummaryEndpointTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.collection = _make_collection(self.user)
        self.headers = _auth_headers(self.user)

    def test_summary_reflects_streak_studied_today_and_active_goals(self):
        _make_flashcard(self.collection)
        StudyDay.objects.create(user=self.user, date=timezone.now().date(), cards_reviewed=2)
        CollectionGoal.objects.create(collection=self.collection, target_date=timezone.now().date())

        response = self.client.get('/api/flashcards/goals/summary/', **self.headers)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['streak'], 1)
        self.assertEqual(body['cards_studied_today'], 2)
        self.assertEqual(len(body['active_goals']), 1)
        self.assertEqual(body['active_goals'][0]['collection_name'], self.collection.name)
        self.assertEqual(body['daily_due_count'], 1)
        self.assertEqual(body['daily_target_total'], 1)
