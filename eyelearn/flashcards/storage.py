import os
from uuid import uuid4

import boto3
from django.conf import settings

# Only this file talks to the storage SDK. R2 and AWS S3 speak the identical
# S3 API, so switching providers later is an env var change (STORAGE_ENDPOINT_URL,
# STORAGE_REGION), not a code change.


def _client():
    return boto3.client(
        's3',
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
        region_name=settings.STORAGE_REGION,
    )


def build_storage_key(*, user_id, flashcard_id, media_type, filename):
    ext = os.path.splitext(filename)[1].lower()
    return f'flashcards/{user_id}/{flashcard_id}/{media_type}/{uuid4().hex}{ext}'


def generate_upload_url(*, key, content_type, expires_in=3600):
    return _client().generate_presigned_url(
        'put_object',
        Params={'Bucket': settings.STORAGE_BUCKET_NAME, 'Key': key, 'ContentType': content_type},
        ExpiresIn=expires_in,
    )


def generate_download_url(*, key, expires_in=3600):
    return _client().generate_presigned_url(
        'get_object',
        Params={'Bucket': settings.STORAGE_BUCKET_NAME, 'Key': key},
        ExpiresIn=expires_in,
    )


def delete_object(*, key):
    _client().delete_object(Bucket=settings.STORAGE_BUCKET_NAME, Key=key)


def build_document_storage_key(*, user_id, collection_id, filename):
    # Nested under flashcards/ (not a top-level source-documents/ prefix) so
    # this stays within the same bucket-key-prefix restriction the storage
    # credentials already enforce for every other upload this app makes --
    # confirmed live against the real bucket: a top-level source-documents/
    # key was rejected with AccessDenied ("not entitled") while anything
    # under flashcards/ succeeds.
    ext = os.path.splitext(filename)[1].lower()
    return f'flashcards/{user_id}/source-documents/{collection_id}/{uuid4().hex}{ext}'


def download_object(*, key):
    return _client().get_object(Bucket=settings.STORAGE_BUCKET_NAME, Key=key)['Body'].read()
