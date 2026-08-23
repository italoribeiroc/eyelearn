import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from flashcards.models import Collection, Flashcard, FlashcardMedia, ReviewLog, ReviewState
from flashcards.services import CollectionCycleError, CollectionService, CrossOwnerParentError, ReviewService

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


class CollectionCRUDTests(TestCase):
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


class FlashcardCRUDTests(TestCase):
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


class MediaUploadFlowTests(TestCase):
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


class ReviewSchedulingTests(TestCase):
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


class StudyQueueTests(TestCase):
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
