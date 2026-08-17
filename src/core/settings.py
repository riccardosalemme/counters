"""
Django settings for the counters project.

Every value that changes between installations is read from the environment,
so the same code runs on SQLite for a single-node install and on PostgreSQL
for a shared one. See README.md for the functional specification.
"""

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_list(name, default=()):
    value = os.environ.get(name)
    if not value:
        return list(default)
    return [item.strip() for item in value.split(',') if item.strip()]


SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    'django-insecure-*u100*2rq77so=!_d()0t4twpq(57)-1@(zlkp++penme1(h53',
)

DEBUG = env_bool('DEBUG', default=True)

ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1', '192.168.100.21'] if DEBUG else [])

CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'counters',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'


# Database
#
# DATABASE_URL wins when set, otherwise a local SQLite file. Parsed by hand to
# avoid pulling in a dependency for fifteen lines of code.

def database_from_url(url):
    parsed = urlparse(url)
    engines = {
        'postgres': 'django.db.backends.postgresql',
        'postgresql': 'django.db.backends.postgresql',
        'sqlite': 'django.db.backends.sqlite3',
    }
    if parsed.scheme not in engines:
        raise ValueError(f'unsupported DATABASE_URL scheme: {parsed.scheme!r}')

    if parsed.scheme == 'sqlite':
        return {'ENGINE': engines[parsed.scheme], 'NAME': parsed.path or ':memory:'}

    return {
        'ENGINE': engines[parsed.scheme],
        'NAME': parsed.path.lstrip('/'),
        'USER': unquote(parsed.username or ''),
        'PASSWORD': unquote(parsed.password or ''),
        'HOST': parsed.hostname or '',
        'PORT': str(parsed.port or ''),
        'CONN_MAX_AGE': 60,
    }


DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    DATABASES = {'default': database_from_url(DATABASE_URL)}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {
                # SQLite has no row-level locking, so select_for_update() is a
                # no-op there and concurrency relies on the write lock instead:
                # IMMEDIATE takes it when the transaction opens rather than at
                # the first write, which is what makes read-modify-write on a
                # counter safe. WAL keeps readers from blocking meanwhile.
                'transaction_mode': 'IMMEDIATE',
                'init_command': (
                    'PRAGMA journal_mode=WAL;'
                    'PRAGMA synchronous=NORMAL;'
                    'PRAGMA busy_timeout=5000;'
                ),
            },
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization

LANGUAGE_CODE = 'it-it'

TIME_ZONE = os.environ.get('TIME_ZONE', 'Europe/Rome')

USE_I18N = True

USE_TZ = True


# Static files

STATIC_URL = 'static/'

STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Sessions and CSRF
#
# Strict SameSite means a cross-site request never carries the session cookie.
# It is a second line of defence: the first one is that write endpoints reached
# over GET refuse session authentication outright (see counters/auth.py).

SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_HTTPONLY = True

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', default=True)
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # Off unless asked for: a browser honours HSTS for the whole duration it
    # was given, so a hasty value is painful to walk back.
    SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '0'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS')

# The admin login refuses anyone without is_staff, so a plain operator could
# not even open a display. The display has its own login instead.
LOGIN_URL = '/login'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login'


# counters

# Precision of Counter.value and of every value stored on a Transaction.
COUNTERS_MAX_DIGITS = 12
COUNTERS_DECIMAL_PLACES = 3

# How often an ApiToken.last_used_at is refreshed, in seconds. Without this
# every authenticated read would cost a write.
COUNTERS_TOKEN_TOUCH_INTERVAL = 60
