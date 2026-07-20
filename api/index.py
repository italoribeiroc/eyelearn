import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR / 'eyelearn'

sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'eyelearn.settings')

from django.core.wsgi import get_wsgi_application

app = get_wsgi_application()
