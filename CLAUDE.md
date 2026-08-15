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
- `eyelearn/accounts/` — owns identity: the custom `User` model (`AUTH_USER_MODEL = 'accounts.User'`, subclasses `AbstractUser` with a unique `email` and a nullable unique `google_id`) and the JWT auth endpoints (`djangorestframework-simplejwt`). Kept separate from `api` on purpose — it's the natural place for registration, login, and Google OAuth, without entangling with unrelated app endpoints.
- Root `api/index.py` is the Vercel serverless WSGI entrypoint. It adds `eyelearn/` (the nested project dir) to `sys.path` and sets `DJANGO_SETTINGS_MODULE=eyelearn.settings`, then exposes `app = get_wsgi_application()`. Root `vercel.json` builds this file with `@vercel/python` and routes all paths (`/(.*)`) to it.
- URL delegation: `eyelearn/eyelearn/urls.py` includes `api.urls` under `/api/`, `accounts.urls` under `/api/auth/` (`register/`, `login/`, `refresh/`, `me/`, `google/`), plus a root `home` view served directly from `api.views`.
- Settings are environment-driven (`eyelearn/eyelearn/settings.py`): `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`, `GOOGLE_OAUTH_CLIENT_ID` all read from env vars with sane defaults; `DEBUG` defaults to `False` when the `VERCEL` env var is present (i.e. running on Vercel) and `True` otherwise.
- Google sign-in (`accounts.views.google_auth`, `POST /api/auth/google/`) takes `{id_token}`, verifies it against `GOOGLE_OAUTH_CLIENT_ID` with `google-auth`, then finds-or-creates a `User` by email and links it via `google_id`. The frontend (separate repo) drives the OAuth Authorization Code redirect flow and only ever sends this endpoint an already-issued Google ID token — Django never talks to Google directly and no CORS config was needed for this.
- Python version is pinned once, at the repo root (`.python-version`, currently 3.12), consumed by both `actions/setup-python` in CI, Vercel's `@vercel/python` runtime, and the local venv (`eyelearn/.venv`) — keep the venv's interpreter matched to this pin, since `psycopg2-binary` and other C-extension deps only ship prebuilt wheels for released Python versions.

## CI/CD

There are two separate Vercel projects: `eyelearn-staging` and `eyelearn` (production). Both GitHub Actions deploy workflows deploy to their target project's **Production** environment (`vercel --prod`) — neither uses Vercel Preview deployments. Which physical project each workflow hits is determined by GitHub **Environment**-scoped secrets (repo Settings → Environments → `Staging` / `Production`), not by the workflow YAML — both workflows read the same secret names (`VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, `VERCEL_TOKEN`) via `environment: name: Staging`/`Production`, and each GitHub Environment holds different values pointing at its respective Vercel project.

Three GitHub Actions workflows in `.github/workflows/`:
- `ci.yml` — runs Django checks + `api.tests accounts.tests` on every PR and on push to `main`/`develop`. Uses the SQLite fallback (no `DATABASE_URL` set in CI).
- `deploy-staging.yml` — on push to `develop`, runs the same test job, then deploys to the `eyelearn-staging` project's Production environment via the Vercel CLI (`vercel pull --environment=production` → `vercel build --prod` → `vercel deploy --prebuilt --prod`).
- `deploy-production.yml` — on push to `main`, same test gate, then deploys to the `eyelearn` project's Production environment the same way.

Both deploy workflows validate the Vercel token against the Vercel API before pulling/building/deploying. Migrations against Neon are run manually (not part of either workflow) — see Local database above for the one-off `DATABASE_URL=... manage.py migrate` pattern; run this against the relevant Neon database whenever models change, before or after deploying.
