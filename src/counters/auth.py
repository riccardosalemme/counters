"""Token authentication for the API.

The token travels in the query string (`?token=...`) because that is the one
form every client speaks without extra ceremony: curl, an iOS shortcut, an
ESP32, a browser address bar. An Authorization header is accepted too, for
clients that would rather keep the secret out of the URL.

One rule is not negotiable: a write reached over GET never accepts the session
cookie. Django applies no CSRF protection to GET, so if it did, any page on the
web could fire an operation with the victim's cookie via a plain <img> tag.
Writes over POST may use the session, since those are CSRF-protected.
"""

import functools
import json

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import ApiToken
from .operations import OperationError


class AuthError(Exception):
    status = 403
    code = 'authentication required'


def resolve_actor(request, *, write):
    """Return (user, source) for the request, or raise AuthError."""
    key = request.GET.get('token')
    if not key:
        header = request.headers.get('Authorization', '')
        if header.startswith('Bearer '):
            key = header[len('Bearer '):].strip()

    if key:
        token = (
            ApiToken.objects.filter(key=key, is_active=True)
            .select_related('user')
            .first()
        )
        if token is None or not token.user.is_active:
            raise AuthError('invalid token')
        touch_last_used(token)
        return token.user, 'api'

    # Session auth: reads always, writes only over POST.
    if request.user.is_authenticated and (not write or request.method == 'POST'):
        if write:
            check_csrf(request)
        return request.user, 'display'

    raise AuthError('missing token')


def check_csrf(request):
    """Run Django's CSRF check by hand.

    The API views are csrf_exempt so that a script can POST with nothing but a
    token. That exemption must not extend to the session: when a write is
    authenticated by cookie the CSRF token is what stands between the display
    and any other site, so it is verified explicitly here.
    """
    reason = CsrfViewMiddleware(lambda r: None).process_view(request, None, (), {})
    if reason is not None:
        raise AuthError('csrf verification failed')


def touch_last_used(token):
    """Record the token's last use, at most once per interval.

    Without the throttle every authenticated read would cost a database write.
    """
    interval = settings.COUNTERS_TOKEN_TOUCH_INTERVAL
    cache_key = f'apitoken-touched:{token.pk}'
    if cache.get(cache_key):
        return
    cache.set(cache_key, True, interval)
    ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())


def api_response(payload, status=200):
    response = JsonResponse(payload, status=status)
    # These URLs carry a token and, over GET, cause writes. Keep them out of
    # every cache and out of the Referer header of any page that links them.
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


def api_view(*, write, methods=('GET',)):
    """Resolve the actor, map domain errors to status codes, set headers."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return api_response({'error': 'method not allowed'}, status=405)
            try:
                user, source = resolve_actor(request, write=write)
            except AuthError as exc:
                return api_response({'error': exc.code, 'detail': str(exc)}, status=exc.status)

            try:
                payload, status = view(request, *args, user=user, source=source, **kwargs)
            except OperationError as exc:
                return api_response(
                    {'error': exc.code, 'detail': str(exc)}, status=exc.status,
                )
            except ValueError as exc:
                return api_response({'error': 'invalid value', 'detail': str(exc)}, status=400)
            except json.JSONDecodeError as exc:
                return api_response({'error': 'invalid json', 'detail': str(exc)}, status=400)

            return api_response(payload, status=status)

        # Exempt so a token-only client can POST without a cookie; the session
        # path re-imposes the check itself, see check_csrf().
        return csrf_exempt(wrapper)

    return decorator
