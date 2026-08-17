import json

from django.utils import timezone

from .auth import api_view
from .models import Counter, Transaction, parse_value
from .operations import UnknownCounter, apply_batch, apply_operation


@api_view(write=False)
def get_counter(request, slug, *, user, source):
    try:
        counter = Counter.objects.get(slug=slug)
    except Counter.DoesNotExist:
        raise UnknownCounter(slug) from None

    return {
        'slug': counter.slug,
        'name': counter.name,
        'value': float(counter.value),
        'is_active': counter.is_active,
    }, 200


@api_view(write=False)
def get_batch(request, *, user, source):
    """Read several counters at once. This is what a display polls."""
    slugs = [s for s in request.GET.get('counters', '').split(',') if s.strip()]
    found = Counter.objects.filter(slug__in=slugs)

    return {
        'counters': {c.slug: float(c.value) for c in found},
        'server_time': timezone.now().isoformat(),
    }, 200


def _write_view(operation):
    @api_view(write=True)
    def view(request, slug, value, *, user, source):
        result = apply_operation(
            slug, operation, parse_value(value), user=user, source=source,
        )
        return result.as_dict(), 200

    view.__name__ = f'{operation}_counter'
    return view


add_counter = _write_view(Transaction.ADD)
subtract_counter = _write_view(Transaction.SUBTRACT)
set_counter = _write_view(Transaction.SET)


@api_view(write=True, methods=('POST',))
def set_batch(request, *, user, source):
    """Apply one operation per counter, in a single database transaction.

    Operation and value are separate fields on purpose: no "+1"/"-5" strings to
    parse, no ambiguity about negative values, and the logged transaction type
    always matches what the caller asked for.
    """
    body = json.loads(request.body.decode() or '{}')
    if not isinstance(body, dict):
        raise ValueError('body must be a json object')

    items = {}
    for slug, spec in body.items():
        if not isinstance(spec, dict):
            raise ValueError(f'{slug}: expected {{"operation": ..., "value": ...}}')
        try:
            operation = spec['operation']
            raw_value = spec['value']
        except KeyError as exc:
            raise ValueError(f'{slug}: missing {exc.args[0]!r}') from None
        items[slug] = (operation, parse_value(raw_value))

    partial = request.GET.get('partial') in {'1', 'true', 'yes'}
    results, errors = apply_batch(items, user=user, source=source, partial=partial)

    payload = {
        'results': {slug: result.as_dict() for slug, result in results.items()},
        'errors': errors,
    }
    return payload, 207 if errors else 200
