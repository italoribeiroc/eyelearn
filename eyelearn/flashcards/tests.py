import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import groq
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from eyelearn.test_utils import ApiTestCase
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from billing.models import PaymentCustomer, Subscription
from flashcards import ai_generation, document_extraction
from flashcards.ai_providers import generate_with_fallback
from flashcards.ai_providers.base import AiGenerationError, BasicCardAutoBatch, BasicCardBatch, ProviderUnavailableError
from flashcards.models import (
    Collection,
    CollectionGoal,
    Flashcard,
    FlashcardGenerationDraft,
    FlashcardMedia,
    GenerationSourceDocument,
    ReviewLog,
    ReviewState,
    StudyDay,
)
from flashcards.services import (
    AI_GENERATION_BATCH_SIZE,
    AiFlashcardGenerationService,
    CollectionCycleError,
    CollectionService,
    CrossOwnerParentError,
    DocumentExtractionFailedError,
    FREE_FLASHCARD_LIMIT,
    FlashcardService,
    GoalService,
    MAX_AI_GENERATE_COUNT,
    MAX_AI_REGENERATE_COUNT,
    MAX_EXISTING_CARDS_CONTEXT,
    MAX_EXTRACTED_CHARS_PER_DOCUMENT,
    MAX_LEARNING_REQUEST_LENGTH,
    MAX_PENDING_SOURCE_DOCUMENTS_PER_USER,
    MAX_SOURCE_DOCUMENTS_PER_REQUEST,
    MAX_TOTAL_SOURCE_DOCUMENT_CHARS,
    ReviewService,
    SourceDocumentService,
    StreakService,
    TooManySourceDocumentsError,
    UnsupportedDocumentError,
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


def _make_pro_user(username='alice', email='alice@example.com'):
    user = _make_user(username=username, email=email)
    customer = PaymentCustomer.objects.create(user=user, provider_customer_id=f'cus_{username}')
    Subscription.objects.create(
        customer=customer, provider_subscription_id=f'sub_{username}', provider_price_id='price_test',
        plan=Subscription.Plan.MONTHLY, currency='usd', status=Subscription.Status.ACTIVE,
    )
    return user


def _basic_draft_cards(count, prefix='Q'):
    from flashcards.ai_providers.base import BasicCardDraft
    return [BasicCardDraft(prompt=f'{prefix}{i}?', answer=f'A{i}') for i in range(count)]


def _build_minimal_pdf(page_texts):
    """Builds a genuine, minimal, byte-valid PDF (real xref table, real
    offsets) for exercising pypdf/pypdfium2 against real bytes rather than
    mocking them away. `page_texts` is a list of str|None: a str embeds real
    extractable text via a `Tj` content-stream operator; None yields a page
    with an empty content stream (pypdf's extract_text() returns '' for it),
    simulating a scanned/image-only page that has no embedded text layer.
    """
    n = len(page_texts)
    first_page_obj_num = 4  # 1=catalog, 2=pages, 3=font
    kids = ' '.join(f'{first_page_obj_num + i} 0 R' for i in range(n))
    objects = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        f'<< /Type /Pages /Kids [{kids}] /Count {n} >>',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    page_objs, content_objs = [], []
    next_num = first_page_obj_num + n
    for i, text in enumerate(page_texts):
        content_num = next_num + i
        stream = f'BT /F1 12 Tf 20 100 Td ({text}) Tj ET' if text else ''
        content_objs.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
        page_objs.append(
            f'<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> '
            f'/MediaBox [0 0 200 200] /Contents {content_num} 0 R >>',
        )
    all_objects = objects + page_objs + content_objs

    buf = io.BytesIO()
    buf.write(b'%PDF-1.4\n')
    offsets = [0]
    for idx, body in enumerate(all_objects, start=1):
        offsets.append(buf.tell())
        buf.write(f'{idx} 0 obj\n{body}\nendobj\n'.encode('latin-1'))
    xref_offset = buf.tell()
    total_objs = len(all_objects) + 1
    buf.write(f'xref\n0 {total_objs}\n'.encode('latin-1'))
    buf.write(b'0000000000 65535 f \n')
    for off in offsets[1:]:
        buf.write(f'{off:010d} 00000 n \n'.encode('latin-1'))
    buf.write(f'trailer\n<< /Size {total_objs} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF'.encode('latin-1'))
    return buf.getvalue()


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


class FlashcardBulkCreateTests(ApiTestCase):
    def setUp(self):
        self.user = _make_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    def _bulk(self, cards):
        return self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/flashcards/bulk/',
            data=json.dumps({'cards': cards}),
            content_type='application/json', **self.headers,
        )

    def test_creates_all_valid_cards_in_one_request(self):
        cards = [{'card_type': 'basic', 'prompt': f'q{i}', 'answer': f'a{i}'} for i in range(10)]

        response = self._bulk(cards)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['created']), 10)
        self.assertEqual(body['errors'], [])
        self.assertFalse(body['limit_reached'])
        self.assertEqual(Flashcard.objects.filter(collection=self.collection).count(), 10)

    def test_invalid_card_reported_but_others_still_created(self):
        cards = [
            {'card_type': 'basic', 'prompt': 'good one', 'answer': 'a'},
            {'card_type': 'basic', 'prompt': '', 'answer': 'a'},  # blank prompt -> invalid
            {'card_type': 'basic', 'prompt': 'another good one', 'answer': 'b'},
        ]

        response = self._bulk(cards)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['created']), 2)
        self.assertEqual(len(body['errors']), 1)
        self.assertEqual(body['errors'][0]['index'], 1)

    def test_stops_creating_once_free_plan_limit_hit_but_reports_every_remaining_card(self):
        Flashcard.objects.bulk_create([
            Flashcard(collection=self.collection, card_type=Flashcard.CardType.BASIC, prompt=f'q{i}', answer=f'a{i}')
            for i in range(FREE_FLASHCARD_LIMIT - 2)
        ])
        cards = [{'card_type': 'basic', 'prompt': f'new{i}', 'answer': 'a'} for i in range(5)]

        response = self._bulk(cards)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['created']), 2)
        self.assertEqual(len(body['errors']), 3)
        self.assertTrue(body['limit_reached'])
        self.assertEqual(Flashcard.objects.filter(collection=self.collection).count(), FREE_FLASHCARD_LIMIT)

    def test_empty_list_is_400(self):
        response = self._bulk([])
        self.assertEqual(response.status_code, 400)

    def test_over_max_batch_size_is_400(self):
        cards = [{'card_type': 'basic', 'prompt': f'q{i}', 'answer': 'a'} for i in range(101)]

        response = self._bulk(cards)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Flashcard.objects.filter(collection=self.collection).exists())

    def test_other_users_collection_is_404(self):
        other = _make_user(username='bob', email='bob@example.com')

        response = self.client.post(
            f'/api/flashcards/collections/{_make_collection(other).id}/flashcards/bulk/',
            data=json.dumps({'cards': [{'card_type': 'basic', 'prompt': 'q', 'answer': 'a'}]}),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 404)

    def test_many_cards_in_one_request_does_not_trip_the_per_user_throttle(self):
        # The whole point of this endpoint: importing a large deck must not
        # cost one Django request per card (see its own docstring) -- a
        # single request creating a full batch should never come anywhere
        # near the 'user' scope's 120/min default rate limit on its own.
        cards = [{'card_type': 'basic', 'prompt': f'q{i}', 'answer': f'a{i}'} for i in range(100)]

        response = self._bulk(cards)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['created']), 100)


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


class AiGenerationEndpointTests(ApiTestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    def _generate(self, payload=None, headers=None):
        payload = payload or {'card_type': 'basic', 'count': 3, 'learning_request': 'Learn about photosynthesis'}
        return self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/ai-generate/',
            data=json.dumps(payload), content_type='application/json', **(headers or self.headers),
        )

    @patch('flashcards.ai_generation.generate_cards')
    def test_non_pro_user_gets_402(self, mock_generate):
        free_user = _make_user(username='free', email='free@example.com')
        collection = _make_collection(free_user)

        response = self.client.post(
            f'/api/flashcards/collections/{collection.id}/ai-generate/',
            data=json.dumps({'card_type': 'basic', 'count': 3, 'learning_request': 'x'}),
            content_type='application/json', **_auth_headers(free_user),
        )

        self.assertEqual(response.status_code, 402)
        mock_generate.assert_not_called()

    @patch('flashcards.ai_generation.generate_cards')
    def test_pro_user_generates_draft(self, mock_generate):
        mock_generate.return_value = _basic_draft_cards(3)

        response = self._generate()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['card_type'], 'basic')
        self.assertEqual(body['status'], 'pending')
        self.assertEqual([c['id'] for c in body['cards']], [1, 2, 3])
        self.assertEqual(body['cards'][0]['prompt'], 'Q0?')
        self.assertTrue(FlashcardGenerationDraft.objects.filter(id=body['id'], user=self.user).exists())

    @patch('flashcards.ai_providers.claude_provider.ClaudeProvider.generate')
    def test_generate_response_reports_provider_used_header(self, mock_claude):
        # X-AI-Provider is a developer-only signal (never rendered anywhere
        # in the frontend) for checking which provider actually served a
        # given call -- exercised through the real generate_with_fallback
        # orchestration here (mocking the provider itself, not
        # ai_generation.generate_cards), so it reflects the real code path.
        mock_claude.return_value = BasicCardBatch(cards=[{'prompt': 'Q', 'answer': 'A'}])

        response = self._generate({'card_type': 'basic', 'count': 1, 'learning_request': 'Learn about photosynthesis'})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response['X-AI-Provider'], 'claude')

    @patch('flashcards.ai_generation.generate_cards')
    def test_count_over_max_is_rejected(self, mock_generate):
        response = self._generate({
            'card_type': 'basic', 'count': MAX_AI_GENERATE_COUNT + 1, 'learning_request': 'x',
        })

        self.assertEqual(response.status_code, 400)
        mock_generate.assert_not_called()
        self.assertFalse(FlashcardGenerationDraft.objects.exists())

    @patch('flashcards.ai_generation.generate_cards')
    def test_learning_request_too_long_is_rejected(self, mock_generate):
        response = self._generate({
            'card_type': 'basic', 'count': 3, 'learning_request': 'x' * (MAX_LEARNING_REQUEST_LENGTH + 1),
        })

        self.assertEqual(response.status_code, 400)
        mock_generate.assert_not_called()

    @patch('flashcards.ai_generation.generate_cards')
    def test_generating_again_discards_prior_pending_draft(self, mock_generate):
        mock_generate.return_value = _basic_draft_cards(2)
        first = self._generate().json()

        mock_generate.return_value = _basic_draft_cards(2)
        second = self._generate().json()

        self.assertNotEqual(first['id'], second['id'])
        self.assertFalse(FlashcardGenerationDraft.objects.filter(id=first['id']).exists())

    @patch('flashcards.ai_generation.generate_cards')
    def test_claude_error_surfaces_as_502(self, mock_generate):
        mock_generate.side_effect = ai_generation.AiGenerationError('boom')

        response = self._generate()

        self.assertEqual(response.status_code, 502)
        self.assertFalse(FlashcardGenerationDraft.objects.exists())

    @patch('flashcards.ai_generation.generate_cards')
    def test_count_above_batch_size_only_generates_first_batch(self, mock_generate):
        requested = AI_GENERATION_BATCH_SIZE + 10
        mock_generate.return_value = _basic_draft_cards(AI_GENERATION_BATCH_SIZE)

        response = self._generate({'card_type': 'basic', 'count': requested, 'learning_request': 'x'})

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['target_count'], requested)
        self.assertEqual(len(body['cards']), AI_GENERATION_BATCH_SIZE)
        self.assertEqual(body['status'], 'pending')
        mock_generate.assert_called_once()
        self.assertEqual(mock_generate.call_args.kwargs['count'], AI_GENERATION_BATCH_SIZE)

    @patch('flashcards.ai_generation.generate_auto_cards')
    def test_auto_mode_uses_model_recommended_total(self, mock_auto):
        mock_auto.return_value = BasicCardAutoBatch(
            recommended_total=37, cards=[{'prompt': f'Q{i}', 'answer': f'A{i}'} for i in range(5)],
        )

        response = self._generate({'card_type': 'basic', 'auto': True, 'learning_request': 'x'})

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['target_count'], 37)
        self.assertEqual(len(body['cards']), 5)
        mock_auto.assert_called_once()
        self.assertEqual(mock_auto.call_args.kwargs['max_first_batch'], AI_GENERATION_BATCH_SIZE)

    @patch('flashcards.ai_generation.generate_auto_cards')
    def test_auto_mode_target_never_smaller_than_first_batch(self, mock_auto):
        # A deliberately-inconsistent model response (says 5 but returns 8)
        # shouldn't leave target_count smaller than what's already generated.
        mock_auto.return_value = BasicCardAutoBatch(
            recommended_total=5, cards=[{'prompt': f'Q{i}', 'answer': f'A{i}'} for i in range(8)],
        )

        response = self._generate({'card_type': 'basic', 'auto': True, 'learning_request': 'x'})

        self.assertEqual(response.json()['target_count'], 8)


class AiGenerationBatchContinuationTests(ApiTestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)
        self.target_count = AI_GENERATION_BATCH_SIZE + 10
        self.draft = FlashcardGenerationDraft.objects.create(
            user=self.user, collection=self.collection, card_type=Flashcard.CardType.BASIC,
            learning_request='Learn X', target_count=self.target_count,
            cards=[
                {'id': i, 'prompt': f'Q{i}', 'answer': f'A{i}', 'options': [], 'accepted_answers': []}
                for i in range(1, AI_GENERATION_BATCH_SIZE + 1)
            ],
        )

    def _continue(self):
        return self.client.post(
            f'/api/flashcards/ai-generate/{self.draft.id}/generate-next-batch/', **self.headers,
        )

    @patch('flashcards.ai_generation.generate_cards')
    def test_continues_toward_target_with_ids_after_existing(self, mock_generate):
        remaining = self.target_count - AI_GENERATION_BATCH_SIZE  # 10, safely under one batch
        mock_generate.return_value = _basic_draft_cards(remaining, prefix='NEW')

        response = self._continue()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['cards']), self.target_count)
        new_ids = [c['id'] for c in body['cards'][AI_GENERATION_BATCH_SIZE:]]
        self.assertEqual(new_ids, list(range(AI_GENERATION_BATCH_SIZE + 1, self.target_count + 1)))
        mock_generate.assert_called_once()
        self.assertEqual(mock_generate.call_args.kwargs['count'], remaining)
        # Dedup context includes what's already in the draft, not just
        # persisted collection cards.
        self.assertIn('Q1', mock_generate.call_args.kwargs['existing_card_prompts'])

    @patch('flashcards.ai_generation.generate_cards')
    def test_batch_capped_at_ai_generation_batch_size(self, mock_generate):
        self.draft.target_count = 500
        self.draft.save(update_fields=['target_count'])
        mock_generate.return_value = _basic_draft_cards(AI_GENERATION_BATCH_SIZE, prefix='NEW')

        self._continue()

        self.assertEqual(mock_generate.call_args.kwargs['count'], AI_GENERATION_BATCH_SIZE)

    def test_already_complete_draft_is_400(self):
        self.draft.target_count = AI_GENERATION_BATCH_SIZE
        self.draft.save(update_fields=['target_count'])

        response = self._continue()

        self.assertEqual(response.status_code, 400)

    def test_non_pro_user_gets_402(self):
        free_user = _make_user(username='free', email='free@example.com')
        draft = FlashcardGenerationDraft.objects.create(
            user=free_user, collection=_make_collection(free_user), card_type=Flashcard.CardType.BASIC,
            learning_request='x', target_count=50, cards=[],
        )

        response = self.client.post(
            f'/api/flashcards/ai-generate/{draft.id}/generate-next-batch/', **_auth_headers(free_user),
        )

        self.assertEqual(response.status_code, 402)

    def test_other_users_draft_404s(self):
        other = _make_user(username='bob', email='bob@example.com')

        response = self.client.post(
            f'/api/flashcards/ai-generate/{self.draft.id}/generate-next-batch/', **_auth_headers(other),
        )

        self.assertEqual(response.status_code, 404)

    @patch('flashcards.ai_generation.generate_cards')
    def test_batch_dropped_if_draft_confirmed_while_generating(self, mock_generate):
        # The AI call is slow and unlocked -- simulate a concurrent request
        # (e.g. the user hit Save on the review page) completing while it
        # was still in flight, by mutating the draft from inside the mock.
        def fake_generate(**kwargs):
            self.draft.status = FlashcardGenerationDraft.Status.CONFIRMED
            self.draft.save(update_fields=['status'])
            return _basic_draft_cards(5, prefix='NEW')

        mock_generate.side_effect = fake_generate

        response = self._continue()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'confirmed')
        # The batch generated during the race is dropped, not appended to
        # an already-confirmed draft.
        self.assertEqual(len(body['cards']), AI_GENERATION_BATCH_SIZE)


class AiProviderFallbackTests(TestCase):
    """generate_with_fallback (the Claude -> Gemini -> Groq orchestration)
    tested directly against mocked providers -- no real API calls."""

    @patch('flashcards.ai_providers.groq_provider.GroqProvider.generate')
    @patch('flashcards.ai_providers.gemini_provider.GeminiProvider.generate')
    @patch('flashcards.ai_providers.claude_provider.ClaudeProvider.generate')
    def test_falls_back_to_gemini_when_claude_unavailable(self, mock_claude, mock_gemini, mock_groq):
        mock_claude.side_effect = ProviderUnavailableError('rate limited')
        mock_gemini.return_value = BasicCardBatch(cards=[{'prompt': 'Q', 'answer': 'A'}])

        result = generate_with_fallback(system='sys', user_content='usr', response_model=BasicCardBatch)

        self.assertEqual(len(result.cards), 1)
        mock_claude.assert_called_once()
        mock_gemini.assert_called_once()
        mock_groq.assert_not_called()

    @patch('flashcards.ai_providers.groq_provider.GroqProvider.generate')
    @patch('flashcards.ai_providers.gemini_provider.GeminiProvider.generate')
    @patch('flashcards.ai_providers.claude_provider.ClaudeProvider.generate')
    def test_falls_back_to_groq_when_claude_and_gemini_unavailable(self, mock_claude, mock_gemini, mock_groq):
        mock_claude.side_effect = ProviderUnavailableError('rate limited')
        mock_gemini.side_effect = ProviderUnavailableError('rate limited')
        mock_groq.return_value = BasicCardBatch(cards=[{'prompt': 'Q', 'answer': 'A'}])

        result = generate_with_fallback(system='sys', user_content='usr', response_model=BasicCardBatch)

        self.assertEqual(len(result.cards), 1)
        mock_claude.assert_called_once()
        mock_gemini.assert_called_once()
        mock_groq.assert_called_once()

    @patch('flashcards.ai_providers.groq_provider.GroqProvider.generate')
    @patch('flashcards.ai_providers.gemini_provider.GeminiProvider.generate')
    @patch('flashcards.ai_providers.claude_provider.ClaudeProvider.generate')
    def test_does_not_fall_back_on_claude_success(self, mock_claude, mock_gemini, mock_groq):
        mock_claude.return_value = BasicCardBatch(cards=[{'prompt': 'Q', 'answer': 'A'}])

        generate_with_fallback(system='sys', user_content='usr', response_model=BasicCardBatch)

        mock_gemini.assert_not_called()
        mock_groq.assert_not_called()

    @patch('flashcards.ai_providers.groq_provider.GroqProvider.generate')
    @patch('flashcards.ai_providers.gemini_provider.GeminiProvider.generate')
    @patch('flashcards.ai_providers.claude_provider.ClaudeProvider.generate')
    def test_raises_when_every_provider_unavailable(self, mock_claude, mock_gemini, mock_groq):
        mock_claude.side_effect = ProviderUnavailableError('rate limited')
        mock_gemini.side_effect = ProviderUnavailableError('also unavailable')
        mock_groq.side_effect = ProviderUnavailableError('also unavailable')

        with self.assertRaises(ai_generation.AiGenerationError):
            generate_with_fallback(system='sys', user_content='usr', response_model=BasicCardBatch)

    @patch('flashcards.ai_providers.groq_provider.GroqProvider.generate')
    @patch('flashcards.ai_providers.gemini_provider.GeminiProvider.generate')
    @patch('flashcards.ai_providers.claude_provider.ClaudeProvider.generate')
    def test_does_not_fall_back_on_claude_content_error(self, mock_claude, mock_gemini, mock_groq):
        # A genuine validation/content failure isn't "unavailable" -- it
        # would likely fail identically on the next provider too, so it
        # should surface immediately rather than trying Gemini/Groq.
        mock_claude.side_effect = ai_generation.AiGenerationError('schema mismatch')

        with self.assertRaises(ai_generation.AiGenerationError):
            generate_with_fallback(system='sys', user_content='usr', response_model=BasicCardBatch)

        mock_gemini.assert_not_called()
        mock_groq.assert_not_called()

    def test_claude_provider_unavailable_when_key_unset(self):
        from flashcards.ai_providers.claude_provider import ClaudeProvider
        with self.settings(CLAUDE_API_KEY=''):
            with self.assertRaises(ProviderUnavailableError):
                ClaudeProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)

    @patch('anthropic.resources.messages.Messages.parse')
    def test_claude_insufficient_credit_balance_falls_back(self, mock_parse):
        # Anthropic reports an exhausted credit balance as a plain 400
        # invalid_request_error, not 429/402 -- must still be treated as
        # "unavailable" (try Gemini next), not a terminal error, since a
        # 400 alone would otherwise look like a malformed request.
        import httpx2
        from flashcards.ai_providers.claude_provider import ClaudeProvider

        response = httpx2.Response(400, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
        mock_parse.side_effect = anthropic.APIStatusError(
            'bad request', response=response,
            body={'type': 'error', 'error': {
                'type': 'invalid_request_error',
                'message': 'Your credit balance is too low to access the Anthropic API.',
            }},
        )

        with self.settings(CLAUDE_API_KEY='sk-ant-test'):
            with self.assertRaises(ProviderUnavailableError):
                ClaudeProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)

    @patch('anthropic.resources.messages.Messages.parse')
    def test_claude_genuine_bad_request_does_not_fall_back(self, mock_parse):
        import httpx2

        response = httpx2.Response(400, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
        mock_parse.side_effect = anthropic.APIStatusError(
            'bad request', response=response,
            body={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'model: field required'}},
        )

        from flashcards.ai_providers.claude_provider import ClaudeProvider
        with self.settings(CLAUDE_API_KEY='sk-ant-test'):
            with self.assertRaises(ai_generation.AiGenerationError):
                ClaudeProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)

    def test_gemini_provider_unavailable_when_key_unset(self):
        from flashcards.ai_providers.gemini_provider import GeminiProvider
        with self.settings(GEMINI_API_KEY=''):
            with self.assertRaises(ProviderUnavailableError):
                GeminiProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)

    def test_groq_provider_unavailable_when_key_unset(self):
        from flashcards.ai_providers.groq_provider import GroqProvider
        with self.settings(GROQ_API_KEY=''):
            with self.assertRaises(ProviderUnavailableError):
                GroqProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)

    @patch('groq.resources.chat.completions.Completions.create')
    def test_groq_parses_valid_json_object_response(self, mock_create):
        from flashcards.ai_providers.groq_provider import GroqProvider

        mock_create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({'cards': [{'prompt': 'Q', 'answer': 'A'}]}),
            ))],
        )

        with self.settings(GROQ_API_KEY='gsk-test'):
            result = GroqProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)

        self.assertEqual(len(result.cards), 1)
        self.assertEqual(result.cards[0].prompt, 'Q')

    @patch('groq.resources.chat.completions.Completions.create')
    def test_groq_malformed_json_is_ai_generation_error(self, mock_create):
        # A response that isn't valid JSON, or doesn't match the schema, is
        # a genuine content failure -- surfaced as AiGenerationError (not
        # ProviderUnavailableError), since json_object mode gives no
        # structural guarantee the way Claude/Gemini's native structured
        # output does (see groq_provider.py's module docstring).
        from flashcards.ai_providers.groq_provider import GroqProvider

        mock_create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='not valid json'))],
        )

        with self.settings(GROQ_API_KEY='gsk-test'):
            with self.assertRaises(ai_generation.AiGenerationError):
                GroqProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)

    @patch('groq.resources.chat.completions.Completions.create')
    def test_groq_rate_limit_falls_back(self, mock_create):
        import httpx
        from flashcards.ai_providers.groq_provider import GroqProvider

        response = httpx.Response(429, request=httpx.Request('POST', 'https://api.groq.com/openai/v1/chat/completions'))
        mock_create.side_effect = groq.RateLimitError('rate limited', response=response, body=None)

        with self.settings(GROQ_API_KEY='gsk-test'):
            with self.assertRaises(ProviderUnavailableError):
                GroqProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)

    @patch('groq.resources.chat.completions.Completions.create')
    def test_groq_bad_request_does_not_fall_back(self, mock_create):
        import httpx
        from flashcards.ai_providers.groq_provider import GroqProvider

        response = httpx.Response(400, request=httpx.Request('POST', 'https://api.groq.com/openai/v1/chat/completions'))
        mock_create.side_effect = groq.APIStatusError(
            'bad request', response=response, body={'error': {'message': 'model: field required'}},
        )

        with self.settings(GROQ_API_KEY='gsk-test'):
            with self.assertRaises(ai_generation.AiGenerationError):
                GroqProvider().generate(system='sys', user_content='usr', response_model=BasicCardBatch)


class AiGenerationDraftMutationTests(ApiTestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)
        self.draft = FlashcardGenerationDraft.objects.create(
            user=self.user, collection=self.collection, card_type=Flashcard.CardType.BASIC,
            learning_request='Learn X',
            cards=[
                {'id': 1, 'prompt': 'Q1', 'answer': 'A1', 'options': [], 'accepted_answers': []},
                {'id': 2, 'prompt': 'Q2', 'answer': 'A2', 'options': [], 'accepted_answers': []},
                {'id': 3, 'prompt': 'Q3', 'answer': 'A3', 'options': [], 'accepted_answers': []},
            ],
        )

    def _post(self, path, payload):
        return self.client.post(
            f'/api/flashcards/ai-generate/{self.draft.id}/{path}',
            data=json.dumps(payload), content_type='application/json', **self.headers,
        )

    def test_get_draft(self):
        response = self.client.get(f'/api/flashcards/ai-generate/{self.draft.id}/', **self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['cards']), 3)

    def test_other_users_draft_404s(self):
        other = _make_user(username='bob', email='bob@example.com')

        response = self.client.get(f'/api/flashcards/ai-generate/{self.draft.id}/', **_auth_headers(other))

        self.assertEqual(response.status_code, 404)

    @patch('flashcards.ai_generation.regenerate_cards')
    def test_regenerate_only_replaces_selected(self, mock_regenerate):
        mock_regenerate.return_value = _basic_draft_cards(2, prefix='NEW')

        response = self._post('regenerate/', {'selected_ids': [1, 3], 'instruction': 'Make harder'})

        self.assertEqual(response.status_code, 200)
        cards = {c['id']: c for c in response.json()['cards']}
        self.assertEqual(cards[1]['prompt'], 'NEW0?')
        self.assertEqual(cards[3]['prompt'], 'NEW1?')
        # The non-selected card is left completely untouched.
        self.assertEqual(cards[2], {'id': 2, 'prompt': 'Q2', 'answer': 'A2', 'options': [], 'accepted_answers': []})

    @patch('flashcards.ai_generation.regenerate_cards')
    def test_regenerate_claude_error_leaves_draft_unchanged(self, mock_regenerate):
        mock_regenerate.side_effect = ai_generation.AiGenerationError('wrong count')

        response = self._post('regenerate/', {'selected_ids': [1], 'instruction': 'x'})

        self.assertEqual(response.status_code, 502)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.cards[0]['prompt'], 'Q1')

    @patch('flashcards.ai_generation.regenerate_cards')
    def test_regenerate_skips_concurrently_removed_card(self, mock_regenerate):
        # Simulate a concurrent remove-cards call finishing while the (slow,
        # unlocked) regenerate call for card 2 was still in flight.
        def fake_regenerate(**kwargs):
            self.draft.cards = [c for c in self.draft.cards if c['id'] != 2]
            self.draft.save(update_fields=['cards'])
            return _basic_draft_cards(2, prefix='NEW')

        mock_regenerate.side_effect = fake_regenerate

        response = self._post('regenerate/', {'selected_ids': [1, 2], 'instruction': 'x'})

        self.assertEqual(response.status_code, 200)
        ids = [c['id'] for c in response.json()['cards']]
        self.assertNotIn(2, ids)
        self.assertIn(1, ids)
        self.assertIn(3, ids)

    def test_regenerate_over_max_selected_is_400(self):
        response = self._post('regenerate/', {
            'selected_ids': list(range(1, MAX_AI_REGENERATE_COUNT + 2)), 'instruction': 'x',
        })

        self.assertEqual(response.status_code, 400)

    def test_regenerate_empty_selection_is_400(self):
        response = self._post('regenerate/', {'selected_ids': [], 'instruction': 'x'})

        self.assertEqual(response.status_code, 400)

    def test_regenerate_unknown_id_is_400(self):
        response = self._post('regenerate/', {'selected_ids': [999], 'instruction': 'x'})

        self.assertEqual(response.status_code, 400)

    def test_regenerate_on_non_pending_draft_is_400(self):
        self.draft.status = FlashcardGenerationDraft.Status.CONFIRMED
        self.draft.save()

        response = self._post('regenerate/', {'selected_ids': [1], 'instruction': 'x'})

        self.assertEqual(response.status_code, 400)

    def test_remove_cards(self):
        response = self._post('remove-cards/', {'card_ids': [2]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([c['id'] for c in response.json()['cards']], [1, 3])
        self.draft.refresh_from_db()
        self.assertEqual([c['id'] for c in self.draft.cards], [1, 3])

    def test_remove_cards_on_confirmed_draft_is_400(self):
        self.draft.status = FlashcardGenerationDraft.Status.CONFIRMED
        self.draft.save()

        response = self._post('remove-cards/', {'card_ids': [1]})

        self.assertEqual(response.status_code, 400)

    def test_confirm_creates_real_flashcards(self):
        response = self._post('confirm/', {})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['created']), 3)
        self.assertEqual(body['errors'], [])
        self.assertEqual(Flashcard.objects.filter(collection=self.collection).count(), 3)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, FlashcardGenerationDraft.Status.CONFIRMED)

    def test_confirm_twice_is_400(self):
        self._post('confirm/', {})

        response = self._post('confirm/', {})

        self.assertEqual(response.status_code, 400)

    def test_confirm_partial_failure_still_creates_valid_cards(self):
        self.draft.cards[1]['prompt'] = ''  # blank prompt fails FlashcardSerializer validation
        self.draft.save(update_fields=['cards'])

        response = self._post('confirm/', {})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['created']), 2)
        self.assertEqual(len(body['errors']), 1)
        self.assertEqual(body['errors'][0]['id'], 2)
        self.assertEqual(Flashcard.objects.filter(collection=self.collection).count(), 2)

    def test_discard(self):
        response = self._post('discard/', {})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'discarded')
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, FlashcardGenerationDraft.Status.DISCARDED)

    def test_discard_confirmed_draft_is_400(self):
        self.draft.status = FlashcardGenerationDraft.Status.CONFIRMED
        self.draft.save()

        response = self._post('discard/', {})

        self.assertEqual(response.status_code, 400)

    def test_confirm_clears_source_document_text_but_keeps_metadata(self):
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, draft=self.draft,
            filename='notes.txt', content_type='text/plain', size_bytes=10,
            extracted_text='some extracted text', char_count=20,
        )

        response = self._post('confirm/', {})

        self.assertEqual(response.status_code, 200)
        document.refresh_from_db()
        self.assertEqual(document.extracted_text, '')
        self.assertEqual(document.filename, 'notes.txt')
        self.assertEqual(document.char_count, 20)

    def test_discard_clears_source_document_text_but_keeps_metadata(self):
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, draft=self.draft,
            filename='notes.txt', content_type='text/plain', size_bytes=10,
            extracted_text='some extracted text', char_count=20,
        )

        response = self._post('discard/', {})

        self.assertEqual(response.status_code, 200)
        document.refresh_from_db()
        self.assertEqual(document.extracted_text, '')
        self.assertEqual(document.filename, 'notes.txt')


class AiFlashcardGenerationServiceUnitTests(TestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.collection = _make_collection(self.user)
        self.service = AiFlashcardGenerationService()

    def test_existing_card_prompts_capped_and_prefers_recent(self):
        for i in range(MAX_EXISTING_CARDS_CONTEXT + 10):
            _make_flashcard(self.collection, prompt=f'q{i}')

        prompts = self.service._existing_card_prompts(self.collection)

        self.assertEqual(len(prompts), MAX_EXISTING_CARDS_CONTEXT)
        self.assertNotIn('q0', prompts)
        self.assertIn(f'q{MAX_EXISTING_CARDS_CONTEXT + 9}', prompts)

    def test_learning_context_empty_collection(self):
        context = self.service._build_learning_context(user=self.user, collection=self.collection)

        self.assertIn('no existing flashcards', context)

    def test_learning_context_unstudied_cards(self):
        _make_flashcard(self.collection)

        context = self.service._build_learning_context(user=self.user, collection=self.collection)

        self.assertIn('none of which the user has studied', context)

    def test_learning_context_reflects_review_states(self):
        card = _make_flashcard(self.collection)
        ReviewState.objects.create(
            user=self.user, flashcard=card, due=timezone.now(),
            state=ReviewState.State.REVIEW, reps=3, lapses=1,
        )

        context = self.service._build_learning_context(user=self.user, collection=self.collection)

        self.assertIn('0 never studied', context)
        self.assertIn('1 mastered/in regular review', context)
        self.assertIn('1 total lapses', context)


class SourceDocumentUploadFlowTests(ApiTestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    @patch('flashcards.services.storage.generate_upload_url', return_value='https://bucket.example/presigned-put')
    def test_upload_url_request_returns_presigned_url_and_key(self, mock_generate):
        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/source-documents/upload-url/',
            data=json.dumps({'content_type': 'application/pdf', 'filename': 'book.pdf', 'size_bytes': 1024}),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['upload_url'], 'https://bucket.example/presigned-put')
        self.assertTrue(
            body['storage_key'].startswith(f'flashcards/{self.user.id}/source-documents/{self.collection.id}/'),
        )
        mock_generate.assert_called_once()

    def test_upload_url_rejects_unsupported_content_type(self):
        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/source-documents/upload-url/',
            data=json.dumps({'content_type': 'application/zip', 'filename': 'file.zip', 'size_bytes': 1024}),
            content_type='application/json', **self.headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_url_rejects_oversized_file(self):
        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/source-documents/upload-url/',
            data=json.dumps({
                'content_type': 'text/plain', 'filename': 'notes.txt',
                'size_bytes': document_extraction.MAX_SIZE_BYTES['text/plain'] + 1,
            }),
            content_type='application/json', **self.headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_non_pro_user_gets_402(self):
        free_user = _make_user(username='free', email='free@example.com')
        collection = _make_collection(free_user)

        response = self.client.post(
            f'/api/flashcards/collections/{collection.id}/source-documents/upload-url/',
            data=json.dumps({'content_type': 'text/plain', 'filename': 'notes.txt', 'size_bytes': 10}),
            content_type='application/json', **_auth_headers(free_user),
        )
        self.assertEqual(response.status_code, 402)


class SourceDocumentConfirmTests(ApiTestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    def _confirm(self, **overrides):
        payload = {
            'storage_key': 'source-documents/1/1/abc.txt', 'content_type': 'text/plain',
            'filename': 'notes.txt', 'size_bytes': 11,
        }
        payload.update(overrides)
        return self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/source-documents/confirm/',
            data=json.dumps(payload), content_type='application/json', **self.headers,
        )

    @patch('flashcards.services.storage.delete_object')
    @patch('flashcards.services.storage.download_object', return_value=b'hello world')
    def test_confirm_txt_creates_row_with_correct_char_count(self, mock_download, mock_delete):
        response = self._confirm()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['char_count'], len('hello world'))
        self.assertEqual(body['filename'], 'notes.txt')
        document = GenerationSourceDocument.objects.get(id=body['id'])
        self.assertEqual(document.extracted_text, 'hello world')
        mock_delete.assert_called_once_with(key='source-documents/1/1/abc.txt')

    @patch('flashcards.services.storage.delete_object')
    @patch('flashcards.services.storage.download_object')
    def test_confirm_pdf_with_real_text_layer(self, mock_download, mock_delete):
        mock_download.return_value = _build_minimal_pdf(['Hello World, this is a real embedded text layer.'])

        response = self._confirm(content_type='application/pdf', filename='book.pdf', size_bytes=1000)

        self.assertEqual(response.status_code, 201)
        document = GenerationSourceDocument.objects.get(id=response.json()['id'])
        self.assertIn('Hello World', document.extracted_text)
        mock_delete.assert_called_once()

    @patch('flashcards.services.storage.delete_object')
    @patch('flashcards.services.storage.download_object', return_value=b'not a real pdf')
    def test_confirm_corrupt_pdf_is_400_and_still_deletes_object(self, mock_download, mock_delete):
        response = self._confirm(content_type='application/pdf', filename='book.pdf', size_bytes=100)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(GenerationSourceDocument.objects.exists())
        mock_delete.assert_called_once_with(key='source-documents/1/1/abc.txt')

    @patch('flashcards.services.storage.delete_object')
    @patch('flashcards.services.storage.download_object', return_value=b'x' * (MAX_EXTRACTED_CHARS_PER_DOCUMENT + 500))
    def test_confirm_truncates_extremely_long_text(self, mock_download, mock_delete):
        response = self._confirm(size_bytes=MAX_EXTRACTED_CHARS_PER_DOCUMENT + 500)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['char_count'], MAX_EXTRACTED_CHARS_PER_DOCUMENT)

    @patch('flashcards.services.storage.delete_object')
    @patch('flashcards.services.storage.download_object', return_value=b'x')
    def test_too_many_pending_documents_is_400(self, mock_download, mock_delete):
        for i in range(MAX_PENDING_SOURCE_DOCUMENTS_PER_USER):
            GenerationSourceDocument.objects.create(
                user=self.user, collection=self.collection, filename=f'f{i}.txt',
                content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
            )

        response = self._confirm()

        self.assertEqual(response.status_code, 400)

    def test_non_pro_user_gets_402(self):
        free_user = _make_user(username='free', email='free@example.com')
        collection = _make_collection(free_user)

        response = self.client.post(
            f'/api/flashcards/collections/{collection.id}/source-documents/confirm/',
            data=json.dumps({
                'storage_key': 'x', 'content_type': 'text/plain', 'filename': 'a.txt', 'size_bytes': 1,
            }),
            content_type='application/json', **_auth_headers(free_user),
        )
        self.assertEqual(response.status_code, 402)


class SourceDocumentDeleteTests(ApiTestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    def test_delete_removes_unlinked_document(self):
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, filename='a.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )

        response = self.client.delete(f'/api/flashcards/source-documents/{document.id}/', **self.headers)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(GenerationSourceDocument.objects.filter(id=document.id).exists())

    def test_delete_other_users_document_is_404(self):
        other = _make_pro_user(username='bob', email='bob@example.com')
        document = GenerationSourceDocument.objects.create(
            user=other, collection=_make_collection(other), filename='a.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )

        response = self.client.delete(f'/api/flashcards/source-documents/{document.id}/', **self.headers)

        self.assertEqual(response.status_code, 404)

    def test_delete_already_used_document_is_404(self):
        draft = FlashcardGenerationDraft.objects.create(
            user=self.user, collection=self.collection, card_type=Flashcard.CardType.BASIC, learning_request='x',
        )
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, draft=draft, filename='a.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )

        response = self.client.delete(f'/api/flashcards/source-documents/{document.id}/', **self.headers)

        self.assertEqual(response.status_code, 404)

    def test_update_text_corrects_extraction_and_recomputes_char_count(self):
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, filename='photo.jpg',
            content_type='image/jpeg', size_bytes=1, extracted_text='mis-read tex t', char_count=15,
        )

        response = self.client.patch(
            f'/api/flashcards/source-documents/{document.id}/',
            data=json.dumps({'extracted_text': 'corrected text'}),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['extracted_text'], 'corrected text')
        self.assertEqual(body['char_count'], len('corrected text'))
        document.refresh_from_db()
        self.assertEqual(document.extracted_text, 'corrected text')
        self.assertEqual(document.char_count, len('corrected text'))

    def test_update_text_truncates_to_max_length(self):
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, filename='notes.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )

        response = self.client.patch(
            f'/api/flashcards/source-documents/{document.id}/',
            data=json.dumps({'extracted_text': 'y' * (MAX_EXTRACTED_CHARS_PER_DOCUMENT + 100)}),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['char_count'], MAX_EXTRACTED_CHARS_PER_DOCUMENT)

    def test_update_text_on_already_used_document_is_404(self):
        draft = FlashcardGenerationDraft.objects.create(
            user=self.user, collection=self.collection, card_type=Flashcard.CardType.BASIC, learning_request='x',
        )
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, draft=draft, filename='a.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )

        response = self.client.patch(
            f'/api/flashcards/source-documents/{document.id}/',
            data=json.dumps({'extracted_text': 'new text'}),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 404)

    def test_update_other_users_document_is_404(self):
        other = _make_pro_user(username='carol', email='carol@example.com')
        document = GenerationSourceDocument.objects.create(
            user=other, collection=_make_collection(other), filename='a.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )

        response = self.client.patch(
            f'/api/flashcards/source-documents/{document.id}/',
            data=json.dumps({'extracted_text': 'new text'}),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 404)


class DescribeImageWithFallbackTests(TestCase):
    @patch('flashcards.ai_providers.GeminiProvider.describe_image')
    @patch('flashcards.ai_providers.ClaudeProvider.describe_image')
    def test_claude_success_gemini_never_called(self, mock_claude, mock_gemini):
        mock_claude.return_value = 'transcribed text'

        from flashcards import ai_providers
        result = ai_providers.describe_image_with_fallback(image_bytes=b'x', media_type='image/png')

        self.assertEqual(result, 'transcribed text')
        mock_gemini.assert_not_called()

    @patch('flashcards.ai_providers.GeminiProvider.describe_image')
    @patch('flashcards.ai_providers.ClaudeProvider.describe_image')
    def test_claude_unavailable_falls_back_to_gemini(self, mock_claude, mock_gemini):
        mock_claude.side_effect = ProviderUnavailableError('rate limited')
        mock_gemini.return_value = 'gemini text'

        from flashcards import ai_providers
        result = ai_providers.describe_image_with_fallback(image_bytes=b'x', media_type='image/png')

        self.assertEqual(result, 'gemini text')

    @patch('flashcards.ai_providers.GeminiProvider.describe_image')
    @patch('flashcards.ai_providers.ClaudeProvider.describe_image')
    def test_both_unavailable_raises_clear_error(self, mock_claude, mock_gemini):
        mock_claude.side_effect = ProviderUnavailableError('rate limited')
        mock_gemini.side_effect = ProviderUnavailableError('also unavailable')

        from flashcards import ai_providers
        with self.assertRaises(AiGenerationError):
            ai_providers.describe_image_with_fallback(image_bytes=b'x', media_type='image/png')


class ScannedPdfExtractionTests(TestCase):
    @patch('flashcards.document_extraction.ai_providers.describe_image_with_fallback')
    def test_scanned_page_triggers_vision_fallback(self, mock_describe):
        mock_describe.return_value = 'transcribed handwriting'
        data = _build_minimal_pdf([None])

        text = document_extraction.extract_text(content_type='application/pdf', data=data)

        self.assertEqual(text, 'transcribed handwriting')
        mock_describe.assert_called_once()
        self.assertEqual(mock_describe.call_args.kwargs['media_type'], 'image/png')

    @patch('flashcards.document_extraction.ai_providers.describe_image_with_fallback')
    def test_mixed_pdf_only_ocrs_the_blank_page(self, mock_describe):
        mock_describe.return_value = 'ocr text'
        data = _build_minimal_pdf(['Real embedded text here', None])

        text = document_extraction.extract_text(content_type='application/pdf', data=data)

        mock_describe.assert_called_once()
        self.assertIn('Real embedded text here', text)
        self.assertIn('ocr text', text)

    @patch('flashcards.document_extraction.ai_providers.describe_image_with_fallback')
    def test_ocr_page_cap_is_respected(self, mock_describe):
        mock_describe.return_value = 'ocr text'
        page_count = document_extraction.MAX_OCR_PAGES_PER_DOCUMENT + 2
        data = _build_minimal_pdf([None] * page_count)

        text = document_extraction.extract_text(content_type='application/pdf', data=data)

        self.assertEqual(mock_describe.call_count, document_extraction.MAX_OCR_PAGES_PER_DOCUMENT)
        self.assertTrue(text)

    def test_encrypted_pdf_is_rejected(self):
        reader_data = _build_minimal_pdf(['secret'])
        with patch('flashcards.document_extraction.pypdf.PdfReader') as mock_reader_cls:
            mock_reader = SimpleNamespace(is_encrypted=True, pages=[])
            mock_reader_cls.return_value = mock_reader
            with self.assertRaises(document_extraction.DocumentExtractionError):
                document_extraction.extract_text(content_type='application/pdf', data=reader_data)

    @patch('flashcards.document_extraction.ai_providers.describe_image_with_fallback')
    def test_vision_failure_on_all_providers_raises_extraction_error(self, mock_describe):
        mock_describe.side_effect = AiGenerationError('all providers down')
        data = _build_minimal_pdf([None])

        with self.assertRaises(document_extraction.DocumentExtractionError):
            document_extraction.extract_text(content_type='application/pdf', data=data)


class StandaloneImageExtractionTests(ApiTestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    @patch('flashcards.services.storage.delete_object')
    @patch('flashcards.services.storage.download_object', return_value=b'fake-image-bytes')
    @patch('flashcards.document_extraction.ai_providers.describe_image_with_fallback')
    def test_image_confirm_returns_transcription_as_extracted_text(self, mock_describe, mock_download, mock_delete):
        mock_describe.return_value = 'a page of handwritten notes about photosynthesis'

        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/source-documents/confirm/',
            data=json.dumps({
                'storage_key': 'source-documents/1/1/photo.png', 'content_type': 'image/png',
                'filename': 'photo.png', 'size_bytes': 500,
            }),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        document = GenerationSourceDocument.objects.get(id=response.json()['id'])
        self.assertEqual(document.extracted_text, 'a page of handwritten notes about photosynthesis')

    @patch('flashcards.services.storage.delete_object')
    @patch('flashcards.services.storage.download_object', return_value=b'fake-image-bytes')
    @patch('flashcards.document_extraction.ai_providers.describe_image_with_fallback')
    def test_image_confirm_both_providers_failing_is_400_no_row_created(self, mock_describe, mock_download, mock_delete):
        mock_describe.side_effect = AiGenerationError('all vision providers down')

        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/source-documents/confirm/',
            data=json.dumps({
                'storage_key': 'source-documents/1/1/photo.jpg', 'content_type': 'image/jpeg',
                'filename': 'photo.jpg', 'size_bytes': 500,
            }),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(GenerationSourceDocument.objects.exists())
        mock_delete.assert_called_once()


class AiGenerationWithSourceDocumentsTests(ApiTestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.headers = _auth_headers(self.user)
        self.collection = _make_collection(self.user)

    def _make_document(self, text='Extracted book chapter text', **kwargs):
        defaults = {
            'user': self.user, 'collection': self.collection, 'filename': 'chapter.pdf',
            'content_type': 'application/pdf', 'size_bytes': 100,
            'extracted_text': text, 'char_count': len(text),
        }
        defaults.update(kwargs)
        return GenerationSourceDocument.objects.create(**defaults)

    @patch('flashcards.ai_generation.generate_cards')
    def test_generate_with_only_documents_succeeds(self, mock_generate):
        document = self._make_document()
        mock_generate.return_value = _basic_draft_cards(2)

        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/ai-generate/',
            data=json.dumps({
                'card_type': 'basic', 'count': 2, 'learning_request': '',
                'source_document_ids': [document.id],
            }),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        document.refresh_from_db()
        self.assertEqual(document.draft_id, response.json()['id'])
        self.assertEqual(response.json()['source_documents'][0]['filename'], 'chapter.pdf')

    @patch('flashcards.ai_generation.generate_cards')
    def test_generate_with_no_request_and_no_documents_is_400(self, mock_generate):
        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/ai-generate/',
            data=json.dumps({'card_type': 'basic', 'count': 2, 'learning_request': ''}),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        mock_generate.assert_not_called()

    @patch('flashcards.ai_generation.generate_cards')
    def test_too_many_documents_per_request_is_400(self, mock_generate):
        ids = [self._make_document(filename=f'f{i}.pdf').id for i in range(MAX_SOURCE_DOCUMENTS_PER_REQUEST + 1)]

        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/ai-generate/',
            data=json.dumps({
                'card_type': 'basic', 'count': 2, 'learning_request': 'x', 'source_document_ids': ids,
            }),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        mock_generate.assert_not_called()

    @patch('flashcards.ai_generation.generate_cards')
    def test_combined_char_limit_over_max_is_400(self, mock_generate):
        document = self._make_document(text='x' * (MAX_TOTAL_SOURCE_DOCUMENT_CHARS + 1))

        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/ai-generate/',
            data=json.dumps({
                'card_type': 'basic', 'count': 2, 'learning_request': 'x',
                'source_document_ids': [document.id],
            }),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        mock_generate.assert_not_called()

    @patch('flashcards.ai_generation.generate_with_fallback')
    def test_prompt_includes_source_documents_tag(self, mock_fallback):
        document = self._make_document(text='Photosynthesis converts light into chemical energy.')
        mock_fallback.return_value = BasicCardBatch(cards=[{'prompt': 'Q', 'answer': 'A'}])

        response = self.client.post(
            f'/api/flashcards/collections/{self.collection.id}/ai-generate/',
            data=json.dumps({
                'card_type': 'basic', 'count': 1, 'learning_request': '',
                'source_document_ids': [document.id],
            }),
            content_type='application/json', **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        user_content = mock_fallback.call_args.kwargs['user_content']
        self.assertIn('<source_documents untrusted="true">', user_content)
        self.assertIn('Photosynthesis converts light into chemical energy.', user_content)
        self.assertIn('chapter.pdf', user_content)

    def test_non_pro_user_gets_402(self):
        free_user = _make_user(username='free', email='free@example.com')
        collection = _make_collection(free_user)

        response = self.client.post(
            f'/api/flashcards/collections/{collection.id}/ai-generate/',
            data=json.dumps({'card_type': 'basic', 'count': 2, 'learning_request': 'x'}),
            content_type='application/json', **_auth_headers(free_user),
        )

        self.assertEqual(response.status_code, 402)


class CleanupStaleSourceDocumentsCommandTests(TestCase):
    def setUp(self):
        self.user = _make_pro_user()
        self.collection = _make_collection(self.user)

    def test_old_unlinked_document_is_deleted(self):
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, filename='old.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )
        GenerationSourceDocument.objects.filter(pk=document.pk).update(
            created_at=timezone.now() - timedelta(hours=48),
        )

        call_command('cleanup_stale_source_documents', older_than_hours=24)

        self.assertFalse(GenerationSourceDocument.objects.filter(pk=document.pk).exists())

    def test_fresh_unlinked_document_survives(self):
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, filename='new.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )

        call_command('cleanup_stale_source_documents', older_than_hours=24)

        self.assertTrue(GenerationSourceDocument.objects.filter(pk=document.pk).exists())

    def test_old_linked_document_survives(self):
        draft = FlashcardGenerationDraft.objects.create(
            user=self.user, collection=self.collection, card_type=Flashcard.CardType.BASIC, learning_request='x',
        )
        document = GenerationSourceDocument.objects.create(
            user=self.user, collection=self.collection, draft=draft, filename='old.txt',
            content_type='text/plain', size_bytes=1, extracted_text='x', char_count=1,
        )
        GenerationSourceDocument.objects.filter(pk=document.pk).update(
            created_at=timezone.now() - timedelta(hours=48),
        )

        call_command('cleanup_stale_source_documents', older_than_hours=24)

        self.assertTrue(GenerationSourceDocument.objects.filter(pk=document.pk).exists())
