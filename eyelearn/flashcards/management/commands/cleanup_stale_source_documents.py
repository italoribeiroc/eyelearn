"""Deletes GenerationSourceDocument rows that were uploaded (and extracted)
but never used by a generation -- e.g. the user picked a file in
AiGenerateDialog and then closed it without generating. These rows carry no
S3 object (already deleted at confirm time, see SourceDocumentService), just
a stale row holding extracted text nobody will ever use.

This repo has no recurring-job/task-queue infrastructure (see eyelearn/CLAUDE.md);
running this needs a scheduler wired up separately -- e.g. Vercel Cron hitting
a protected endpoint that shells out to this command, or a scheduled GitHub
Actions workflow running `manage.py cleanup_stale_source_documents` against
DATABASE_URL. Shipping the command now, without also inventing that
infrastructure, keeps this change scoped to the document-upload feature itself.

Known accepted gap: a browser that PUTs a file to the presigned upload URL
but never calls confirm leaves an orphaned S3 object with no DB row at all --
this command can't see it, since there's nothing to see in Postgres. No
bucket lifecycle policy exists anywhere in this repo today; closing this gap
means adding one on the `flashcards/*/source-documents/*` key pattern (see
storage.build_document_storage_key), out of scope here.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from flashcards.models import GenerationSourceDocument


class Command(BaseCommand):
    help = 'Deletes source documents that were uploaded but never used by a generation.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--older-than-hours', type=int, default=24,
            help='Delete unlinked documents older than this many hours (default: 24).',
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(hours=options['older_than_hours'])
        deleted_count, _ = GenerationSourceDocument.objects.filter(
            draft__isnull=True, created_at__lt=cutoff,
        ).delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted {deleted_count} stale source document(s).'))
