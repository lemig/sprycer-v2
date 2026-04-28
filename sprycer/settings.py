"""Django settings for sprycer."""
from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = config('DEBUG', default=False, cast=bool)
# Dev fallback only when DEBUG=True. Prod (DEBUG=False) must set SECRET_KEY
# explicitly — silently shipping the dev fallback is the kind of bug that
# leaks in someone's commit history and stays.
SECRET_KEY = config(
    'SECRET_KEY',
    default='django-insecure-dev-only-replace-in-prod' if DEBUG else '',
)
if not SECRET_KEY:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured('SECRET_KEY must be set when DEBUG=False.')
# Same shape: '*' is dev-only. Prod requires an explicit host list.
ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1' if DEBUG else '',
    cast=Csv(),
)

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Whitenoise serves staticfiles directly from the app, so we don't need a
    # CDN or separate static server for the Django admin CSS / login page.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'sprycer.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'sprycer.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('POSTGRES_DB', default='sprycer'),
        'USER': config('POSTGRES_USER', default='sprycer'),
        'PASSWORD': config('POSTGRES_PASSWORD', default=''),
        'HOST': config('POSTGRES_HOST', default='localhost'),
        'PORT': config('POSTGRES_PORT', default='5432'),
        'CONN_MAX_AGE': 0,
        'OPTIONS': {
            'sslmode': config('POSTGRES_SSLMODE', default='prefer'),
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Europe/Brussels'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

MEDIA_URL = 'media/'
MEDIA_ROOT = config('MEDIA_ROOT', default=str(BASE_DIR / 'media'))

# Fly terminates TLS at the edge; Django sees plain HTTP from the proxy.
# Without this, request.is_secure() returns False and Django would emit
# http:// links + reject the CSRF cookie on POSTs from the HTTPS site.
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='' if not DEBUG else 'http://localhost:8000,http://127.0.0.1:8000',
    cast=Csv(),
)
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Fly's edge already redirects HTTP→HTTPS via fly.toml force_https=true,
    # but SECURE_SSL_REDIRECT closes the gap if Fly is ever swapped for a
    # proxy that doesn't enforce HTTPS at the edge. Healthz must stay HTTP-
    # callable from inside the machine for the probe to work.
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r'^healthz$']
    # HSTS: tell browsers to remember the HTTPS-only choice for a year, on
    # all subdomains. Single-tenant internal tool, always HTTPS via Fly —
    # the lock-in risk doesn't apply.
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

OPENAI_API_KEY = config('OPENAI_API_KEY', default='')
EMBEDDING_MODEL = 'text-embedding-3-small'
EMBEDDING_DIMS = 1536
LLM_MATCH_MODEL = 'gpt-4o-mini'

SLACK_WEBHOOK_URL = config('SLACK_WEBHOOK_URL', default='')

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/imports/'
LOGOUT_REDIRECT_URL = '/accounts/login/'
