import math
import secrets
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

MAX_DIGITS = settings.COUNTERS_MAX_DIGITS
DECIMAL_PLACES = settings.COUNTERS_DECIMAL_PLACES

# Reserved on a Display: digits and both decimal separators feed the quantity
# buffer, so they can never be bound to a counter or to an operation.
RESERVED_KEYS = frozenset('0123456789.,')

ZERO = Decimal(0)
QUANTUM = Decimal(1).scaleb(-DECIMAL_PLACES)


def generate_key():
    return secrets.token_urlsafe(32)


def format_value(value):
    """Render a Decimal for humans: 42.000 -> "42", 42.500 -> "42.5"."""
    if value is None:
        return ''
    value = Decimal(value)
    if value == value.to_integral_value():
        return f'{value.quantize(Decimal(1))}'
    return f'{value.normalize()}'


def parse_value(raw):
    """Parse a user-supplied value into a Decimal.

    Accepts both decimal separators so that /add/caffe/1,5 and /add/caffe/1.5
    behave the same. Raises ValueError on anything the database would choke on,
    which callers turn into a 400 instead of a 500.
    """
    if raw is None:
        raise ValueError('missing value')

    text = str(raw).strip().replace(',', '.')
    if not text:
        raise ValueError('missing value')

    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f'not a number: {raw!r}') from None

    # Decimal happily parses "NaN" and "Infinity"; neither can be stored.
    if not value.is_finite():
        raise ValueError(f'not a finite number: {raw!r}')

    try:
        value = value.quantize(QUANTUM)
    except InvalidOperation:
        raise ValueError(f'out of range: {raw!r}') from None

    if len(value.as_tuple().digits) > MAX_DIGITS:
        raise ValueError(f'too many digits: {raw!r}')

    return value


def decimal_field(verbose_name, **kwargs):
    kwargs.setdefault('max_digits', MAX_DIGITS)
    kwargs.setdefault('decimal_places', DECIMAL_PLACES)
    return models.DecimalField(verbose_name, **kwargs)


class BaseModel(models.Model):
    created_at = models.DateTimeField('creato il', auto_now_add=True)
    updated_at = models.DateTimeField('modificato il', auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='creato da', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='modificato da', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )

    class Meta:
        abstract = True


class SluggedModel(BaseModel):
    """Fills an empty slug from the name, keeping it unique."""

    name = models.CharField('nome', max_length=100)
    slug = models.SlugField('slug', max_length=100, unique=True, blank=True)

    class Meta:
        abstract = True
        ordering = ('name',)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug(slugify(self.name) or 'item')
        super().save(*args, **kwargs)

    def _unique_slug(self, base):
        slug = base[:100]
        siblings = type(self).objects.exclude(pk=self.pk)
        suffix = 2
        while siblings.filter(slug=slug).exists():
            tail = f'-{suffix}'
            slug = f'{base[:100 - len(tail)]}{tail}'
            suffix += 1
        return slug


class Tag(SluggedModel):
    class Meta(SluggedModel.Meta):
        verbose_name = 'tag'
        verbose_name_plural = 'tag'


class Counter(SluggedModel):
    value = decimal_field('valore corrente', default=ZERO)
    tag = models.ForeignKey(
        Tag, verbose_name='tag', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='counters',
    )
    color = models.CharField(
        'colore', max_length=7, default='#3b82f6',
        validators=[RegexValidator(r'^#[0-9A-Fa-f]{6}$', 'Usa il formato #RRGGBB.')],
    )
    is_active = models.BooleanField(
        'attivo', default=True,
        help_text='Un counter non attivo resta leggibile ma rifiuta le scritture '
                  'e sparisce dalle griglie dei display.',
    )

    class Meta(SluggedModel.Meta):
        verbose_name = 'counter'
        verbose_name_plural = 'counter'

    @property
    def display_value(self):
        return format_value(self.value)


class Transaction(models.Model):
    """Immutable log of every change applied to a counter."""

    ADD = 'add'
    SUBTRACT = 'subtract'
    SET = 'set'
    TYPE_CHOICES = [(ADD, 'add'), (SUBTRACT, 'subtract'), (SET, 'set')]

    SOURCE_API = 'api'
    SOURCE_DISPLAY = 'display'
    SOURCE_ADMIN = 'admin'
    SOURCE_CHOICES = [
        (SOURCE_API, 'api'),
        (SOURCE_DISPLAY, 'display'),
        (SOURCE_ADMIN, 'admin'),
    ]

    counter = models.ForeignKey(
        Counter, verbose_name='counter', on_delete=models.CASCADE, related_name='transactions',
    )
    type = models.CharField('operazione', max_length=10, choices=TYPE_CHOICES)
    value = decimal_field('valore richiesto')
    value_before = decimal_field('valore precedente')
    value_after = decimal_field('valore risultante')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='utente', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='transactions',
    )
    source = models.CharField('origine', max_length=10, choices=SOURCE_CHOICES, default=SOURCE_API)
    notes = models.TextField('note', blank=True)
    created_at = models.DateTimeField('eseguita il', auto_now_add=True)

    class Meta:
        verbose_name = 'transazione'
        verbose_name_plural = 'transazioni'
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=['counter', '-created_at'])]

    def __str__(self):
        return f'#{self.pk} {self.counter_id} {self.type} {format_value(self.value)}'

    @property
    def delta(self):
        return self.value_after - self.value_before


class Display(SluggedModel):
    refresh_interval = models.PositiveIntegerField(
        'intervallo di aggiornamento (ms)', default=2000,
        help_text='Ogni quanti millisecondi il display rilegge i valori.',
    )
    delta_highlight_duration = models.PositiveIntegerField(
        'durata evidenziazione variazione (ms)', default=4000,
        help_text='Per quanti millisecondi resta visibile il badge +/- dopo una variazione.',
    )
    grid_columns = models.PositiveSmallIntegerField(
        'colonne della griglia', null=True, blank=True,
        help_text='Lascia vuoto per calcolarle automaticamente dal numero di counter.',
    )
    counters = models.ManyToManyField(
        Counter, verbose_name='counter', through='DisplayCounter', related_name='displays',
    )
    add_key = models.CharField('tasto somma', max_length=1, default='+')
    subtract_key = models.CharField('tasto sottrazione', max_length=1, default='-')
    set_key = models.CharField('tasto impostazione', max_length=1, default='*')

    class Meta(SluggedModel.Meta):
        verbose_name = 'display'
        verbose_name_plural = 'display'

    def clean(self):
        super().clean()
        errors = {}
        seen = {}
        for field in ('add_key', 'subtract_key', 'set_key'):
            key = (getattr(self, field) or '').lower()
            setattr(self, field, key)
            if not key:
                continue
            if key in RESERVED_KEYS:
                errors[field] = 'Cifre, punto e virgola sono riservati alla quantità.'
            elif key in seen:
                errors[field] = f'Tasto già usato da "{seen[key]}".'
            else:
                seen[key] = field
        if errors:
            raise ValidationError(errors)

    def active_counters(self):
        return (
            self.displaycounter_set.filter(counter__is_active=True)
            .select_related('counter')
            .order_by('position', 'pk')
        )

    def layout(self, count):
        """Columns and rows for `count` cards, all cells the same size."""
        if count <= 0:
            return 1, 1
        columns = self.grid_columns or math.ceil(math.sqrt(count))
        columns = max(1, min(columns, count))
        return columns, math.ceil(count / columns)


class DisplayCounter(models.Model):
    display = models.ForeignKey(Display, verbose_name='display', on_delete=models.CASCADE)
    counter = models.ForeignKey(Counter, verbose_name='counter', on_delete=models.CASCADE)
    key = models.CharField(
        'tasto', max_length=1, blank=True,
        help_text='Tasto che seleziona questo counter. Lascia vuoto per nessuna scorciatoia.',
    )
    position = models.PositiveSmallIntegerField('posizione', default=0)

    class Meta:
        verbose_name = 'counter del display'
        verbose_name_plural = 'counter del display'
        ordering = ('position', 'pk')
        constraints = [
            models.UniqueConstraint(fields=['display', 'counter'], name='unique_display_counter'),
            models.UniqueConstraint(
                fields=['display', 'key'], name='unique_display_key',
                condition=models.Q(key__gt=''),
            ),
        ]

    def __str__(self):
        return f'{self.display_id}/{self.counter_id}'

    def clean(self):
        super().clean()
        self.key = (self.key or '').lower()
        if self.key and self.key in RESERVED_KEYS:
            raise ValidationError(
                {'key': 'Cifre, punto e virgola sono riservati alla quantità.'}
            )


class ApiToken(BaseModel):
    """Opaque per-user key, passed as ?token= or as a Bearer header.

    Stored in the clear on purpose: it has to stay readable in the admin so it
    can be pasted into a script, and it only ever grants the ability to move
    counters. One token per script or device keeps revocation surgical.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='utente',
        on_delete=models.CASCADE, related_name='api_tokens',
    )
    name = models.CharField('nome', max_length=100, help_text='Es. "esp32-cucina", "shortcut-iphone".')
    key = models.CharField('chiave', max_length=64, unique=True, db_index=True,
                           default=generate_key, editable=False)
    is_active = models.BooleanField('attivo', default=True)
    last_used_at = models.DateTimeField('ultimo utilizzo', null=True, blank=True, editable=False)

    class Meta:
        verbose_name = 'token API'
        verbose_name_plural = 'token API'
        ordering = ('user__username', 'name')

    def __str__(self):
        return f'{self.name} ({self.user})'
