# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All Django management commands run against `eyelearn/manage.py` (the Django project root is `eyelearn/`, one level below the repo root — see Architecture).

```bash
# Install dependencies
pip install -r requirements.txt

# Run the dev server
python eyelearn/manage.py runserver

# Run all tests
python eyelearn/manage.py test

# Run only the api + accounts app tests (what CI runs)
python eyelearn/manage.py test api.tests accounts.tests

# Run a single test case
python eyelearn/manage.py test api.tests.ApiEndpointTests.test_home_endpoint

# Django system checks (also run in CI before tests)
python eyelearn/manage.py check

# Migrations
python eyelearn/manage.py makemigrations
python eyelearn/manage.py migrate

# Start local Postgres (Docker), matches the Neon/Vercel Postgres used in prod
docker compose up -d db
```

## Local database

There is no `docker-compose` service for Django itself — only Postgres runs in Docker; run `manage.py` directly against the venv for fast reload. `DATABASES` (`eyelearn/eyelearn/settings.py`) reads `DATABASE_URL` from the environment via `dj-database-url`, loaded from a repo-root `.env` file (gitignored; copy `.env.example` to start) via `python-dotenv`. Three tiers, same code path:
- No `DATABASE_URL` set → falls back to a local SQLite file (used by CI/tests, zero setup).
- `.env` with `DATABASE_URL` pointing at `localhost:5432` → the Dockerized Postgres (`docker-compose.yml`, credentials `eyelearn`/`eyelearn`).
- `DATABASE_URL` injected by Vercel at deploy/build time → real Neon Postgres. No `.env` file exists there, so it's skipped automatically.

## Architecture

**Repo root vs. Django project root are different directories.** The repo root is the Vercel deployment root and holds the serverless entrypoint (`api/index.py`), `vercel.json`, `requirements.txt`, and `.python-version`. The actual Django project (`manage.py`, project package, app package) lives nested one level down at `eyelearn/`. Don't duplicate the root-level deploy/config files into the nested `eyelearn/` directory — that duplication existed before and was removed as dead weight; only one copy of each should exist, at the repo root.

- `eyelearn/eyelearn/` — the Django **project** package: `settings.py`, root `urls.py`, `wsgi.py`, `asgi.py`.
- `eyelearn/api/` — the Django **app** package: `models.py`, `views.py`, `urls.py`, `admin.py`, `tests.py`. Registered in `INSTALLED_APPS` alongside `rest_framework`. General-purpose endpoints; not auth-related.
- `eyelearn/accounts/` — owns identity: the custom `User` model (`AUTH_USER_MODEL = 'accounts.User'`, subclasses `AbstractUser` with a unique `email`) and the JWT auth endpoints (`djangorestframework-simplejwt`). Kept separate from `api` on purpose — it's the natural place for future registration and Google OAuth login, without entangling with unrelated app endpoints.
- Root `api/index.py` is the Vercel serverless WSGI entrypoint. It adds `eyelearn/` (the nested project dir) to `sys.path` and sets `DJANGO_SETTINGS_MODULE=eyelearn.settings`, then exposes `app = get_wsgi_application()`. Root `vercel.json` builds this file with `@vercel/python` and routes all paths (`/(.*)`) to it.
- URL delegation: `eyelearn/eyelearn/urls.py` includes `api.urls` under `/api/`, `accounts.urls` under `/api/auth/` (`register/`, `login/`, `refresh/`, `me/`), plus a root `home` view served directly from `api.views`.
- Settings are environment-driven (`eyelearn/eyelearn/settings.py`): `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL` all read from env vars with sane defaults; `DEBUG` defaults to `False` when the `VERCEL` env var is present (i.e. running on Vercel) and `True` otherwise.
- Python version is pinned once, at the repo root (`.python-version`, currently 3.12), consumed by both `actions/setup-python` in CI, Vercel's `@vercel/python` runtime, and the local venv (`eyelearn/.venv`) — keep the venv's interpreter matched to this pin, since `psycopg2-binary` and other C-extension deps only ship prebuilt wheels for released Python versions.

## CI/CD

Three GitHub Actions workflows in `.github/workflows/`:
- `ci.yml` — runs Django checks + `api.tests accounts.tests` on every PR and on push to `main`/`develop`. Uses the SQLite fallback (no `DATABASE_URL` set in CI).
- `deploy-staging.yml` — on push to `develop`, runs the same test job, then deploys to Vercel Preview via the Vercel CLI (`vercel pull` → `vercel build` → `vercel deploy --prebuilt`).
- `deploy-production.yml` — on push to `main`, same test gate, then deploys to Vercel Production (`--prod` flag).

Both deploy workflows require `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, `VERCEL_TOKEN` secrets and validate the token against the Vercel API before pulling/building/deploying.
