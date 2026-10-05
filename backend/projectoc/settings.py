from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "projectoc-development-secret-key")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [host for host in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if host]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "apps.core",
    "apps.accounts",
    "apps.canvas",
    "apps.ai_agent",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # SQLite is only a local/E2E fallback. Serialize mutating requests in
    # one Django process to avoid transient writer-lock failures; PostgreSQL
    # production deployments do not use this guard.
    "apps.core.middleware.SQLiteWriteLockMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "projectoc.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
WSGI_APPLICATION = "projectoc.wsgi.application"
ASGI_APPLICATION = "projectoc.asgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql"):
    import urllib.parse
    parsed = urllib.parse.urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username,
            "PASSWORD": parsed.password,
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
        }
    }
else:
    # Allow browser/E2E runs to use an isolated SQLite database without
    # mutating the developer database. Production still uses PostgreSQL
    # through DATABASE_URL.
    SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", str(BASE_DIR / "db.sqlite3"))
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": SQLITE_DB_PATH,
            # Browser E2E exercises concurrent PATCH/sync-state writes.
            # Give SQLite a bounded wait instead of failing immediately with
            # `database is locked`; production uses PostgreSQL.
            "OPTIONS": {"timeout": float(os.getenv("SQLITE_BUSY_TIMEOUT", "20"))},
        }
    }

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOW_ALL_ORIGINS = os.getenv("CORS_ALLOW_ALL_ORIGINS", "0") == "1"
CORS_ALLOWED_ORIGINS = [origin for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin]
CSRF_TRUSTED_ORIGINS = [origin for origin in os.getenv("CSRF_TRUSTED_ORIGINS", ",".join(CORS_ALLOWED_ORIGINS)).split(",") if origin]

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
}

WORLD_REPOS_ROOT = Path(os.getenv("WORLD_REPOS_ROOT", BASE_DIR / "../world_repos")).resolve()
WORLD_REPOS_ROOT.mkdir(parents=True, exist_ok=True)

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "45"))
# Dedicated Fernet key for workspace-scoped credentials. Keep stable and secret in deployment.
WORKSPACE_CREDENTIALS_KEY = os.getenv("WORKSPACE_CREDENTIALS_KEY", "")

# Self-hosted deployment hardening. Terminate TLS at the trusted reverse proxy;
# set DJANGO_SECURE_PROXY_SSL_HEADER only when that proxy overwrites the header.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if os.getenv("DJANGO_SECURE_PROXY_SSL_HEADER"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
if os.getenv("DJANGO_SECURE_SSL_REDIRECT", "0") == "1":
    SECURE_SSL_REDIRECT = True

SYNC_SERVICE_URL = os.getenv("SYNC_SERVICE_URL", "ws://127.0.0.1:8787")
SYNC_INTERNAL_SECRET = os.getenv("SYNC_INTERNAL_SECRET", "projectoc-sync-local-secret")

AGENT_MAX_CONCURRENT_WORKSPACE = int(os.getenv("AGENT_MAX_CONCURRENT_WORKSPACE", "2"))
AGENT_MAX_CONCURRENT_USER = int(os.getenv("AGENT_MAX_CONCURRENT_USER", "1"))
AGENT_MAX_CONCURRENT_BRANCH = int(os.getenv("AGENT_MAX_CONCURRENT_BRANCH", "1"))
AGENT_MAX_TOKEN_BUDGET = int(os.getenv("AGENT_MAX_TOKEN_BUDGET", "32000"))
AGENT_LLM_ENABLED = os.getenv("AGENT_LLM_ENABLED", "1") == "1"
AGENT_FALLBACK_MODEL = os.getenv("AGENT_FALLBACK_MODEL", "")
