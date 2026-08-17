"""The single place where a counter's value is allowed to change.

Every write — single endpoint, batch endpoint, display keyboard, admin action —
goes through here, so atomicity and the transaction log exist in one spot only.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction as db_transaction

from .models import MAX_DIGITS, Counter, Transaction

OPERATIONS = {Transaction.ADD, Transaction.SUBTRACT, Transaction.SET}


class OperationError(Exception):
    """Base class for the failures a caller is expected to report back."""

    status = 400
    code = 'error'


class UnknownCounter(OperationError):
    status = 404
    code = 'not found'


class InactiveCounter(OperationError):
    status = 409
    code = 'counter is inactive'


class UnknownOperation(OperationError):
    status = 400
    code = 'unknown operation'


class InvalidValue(OperationError):
    status = 400
    code = 'invalid value'


@dataclass(frozen=True)
class OperationResult:
    counter: Counter
    transaction: Transaction
    previous_value: Decimal

    @property
    def value(self):
        return self.counter.value

    def as_dict(self):
        return {
            'slug': self.counter.slug,
            'name': self.counter.name,
            'value': float(self.counter.value),
            'previous_value': float(self.previous_value),
            'transaction_id': self.transaction.pk,
        }


def compute(before, operation, value):
    if operation == Transaction.ADD:
        return before + value
    if operation == Transaction.SUBTRACT:
        return before - value
    if operation == Transaction.SET:
        return value
    raise UnknownOperation(operation)


def apply_operation(slug, operation, value, *, user=None, source=Transaction.SOURCE_API, notes=''):
    """Apply one operation to one counter, logging it, in a single transaction."""
    if operation not in OPERATIONS:
        raise UnknownOperation(operation)

    with db_transaction.atomic():
        try:
            counter = Counter.objects.select_for_update().get(slug=slug)
        except Counter.DoesNotExist:
            raise UnknownCounter(slug) from None
        return _apply_locked(counter, operation, value, user=user, source=source, notes=notes)


def apply_batch(items, *, user=None, source=Transaction.SOURCE_API, notes='', partial=False):
    """Apply `{slug: (operation, value)}` in one transaction.

    Counters are locked in slug order so two concurrent batches touching the
    same counters can never deadlock against each other. With partial=False a
    single failure rolls the whole batch back.
    """
    results = {}
    errors = {}

    with db_transaction.atomic():
        for slug in sorted(items):
            operation, value = items[slug]
            try:
                if operation not in OPERATIONS:
                    raise UnknownOperation(operation)
                try:
                    counter = Counter.objects.select_for_update().get(slug=slug)
                except Counter.DoesNotExist:
                    raise UnknownCounter(slug) from None
                results[slug] = _apply_locked(
                    counter, operation, value, user=user, source=source, notes=notes,
                )
            except OperationError as exc:
                if not partial:
                    raise
                errors[slug] = exc.code

    return results, errors


def _apply_locked(counter, operation, value, *, user, source, notes):
    if not counter.is_active:
        raise InactiveCounter(counter.slug)

    before = counter.value
    after = compute(before, operation, value)

    # An add can push the result past what the column holds. PostgreSQL would
    # raise, SQLite would store it anyway; refuse it in both cases.
    if len(after.as_tuple().digits) > MAX_DIGITS:
        raise InvalidValue(f'{counter.slug} would overflow')

    counter.value = after
    if user is not None:
        counter.updated_by = user
    counter.save(update_fields=['value', 'updated_at', 'updated_by'])

    transaction = Transaction.objects.create(
        counter=counter,
        type=operation,
        value=value,
        value_before=before,
        value_after=counter.value,
        user=user,
        source=source,
        notes=notes,
    )
    return OperationResult(counter=counter, transaction=transaction, previous_value=before)
