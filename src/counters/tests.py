import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction as db_transaction
from django.test import Client, TestCase, override_settings

from .models import (
    ApiToken,
    Counter,
    Display,
    DisplayCounter,
    Hotkey,
    Tag,
    Transaction,
    display_key_map,
    format_value,
    parse_value,
)
from .operations import InactiveCounter, apply_batch, apply_operation


class ValueParsingTests(TestCase):
    def test_accepts_both_separators(self):
        self.assertEqual(parse_value('1.5'), Decimal('1.500'))
        self.assertEqual(parse_value('1,5'), Decimal('1.500'))

    def test_accepts_negative(self):
        self.assertEqual(parse_value('-10'), Decimal('-10.000'))

    def test_rejects_non_finite(self):
        # Decimal parses these happily; the database cannot store them.
        for raw in ('NaN', 'Infinity', '-inf'):
            with self.assertRaises(ValueError, msg=raw):
                parse_value(raw)

    def test_rejects_garbage_and_overflow(self):
        for raw in ('', 'abc', '1e999', '9' * 13):
            with self.assertRaises(ValueError, msg=raw):
                parse_value(raw)

    def test_format_drops_trailing_zeros(self):
        self.assertEqual(format_value(Decimal('42.000')), '42')
        self.assertEqual(format_value(Decimal('42.500')), '42.5')
        self.assertEqual(format_value(Decimal('-3.250')), '-3.25')


class SlugTests(TestCase):
    def test_slug_filled_from_name(self):
        self.assertEqual(Tag.objects.create(name='Bevande Calde').slug, 'bevande-calde')

    def test_slug_made_unique(self):
        Counter.objects.create(name='Caffè')
        self.assertEqual(Counter.objects.create(name='Caffè').slug, 'caffe-2')


class OperationTests(TestCase):
    def setUp(self):
        self.counter = Counter.objects.create(name='Caffè', value=Decimal('10'))

    def test_operations_log_before_and_after(self):
        result = apply_operation('caffe', Transaction.ADD, Decimal('2.5'))
        self.assertEqual(result.counter.value, Decimal('12.500'))
        self.assertEqual(result.transaction.value_before, Decimal('10.000'))
        self.assertEqual(result.transaction.value_after, Decimal('12.500'))

    def test_set_is_absolute_and_may_go_negative(self):
        apply_operation('caffe', Transaction.SET, Decimal('-10'))
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('-10.000'))

    def test_subtract_below_zero_is_allowed(self):
        apply_operation('caffe', Transaction.SUBTRACT, Decimal('30'))
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('-20.000'))

    def test_inactive_counter_refuses_writes(self):
        Counter.objects.filter(pk=self.counter.pk).update(is_active=False)
        with self.assertRaises(InactiveCounter):
            apply_operation('caffe', Transaction.ADD, Decimal('1'))

    def test_batch_rolls_back_on_unknown_slug(self):
        with self.assertRaises(Exception):
            apply_batch({
                'caffe': (Transaction.ADD, Decimal('1')),
                'ignoto': (Transaction.SET, Decimal('0')),
            })
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('10.000'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_batch_partial_applies_the_valid_ones(self):
        results, errors = apply_batch(
            {
                'caffe': (Transaction.ADD, Decimal('1')),
                'ignoto': (Transaction.SET, Decimal('0')),
            },
            partial=True,
        )
        self.assertEqual(set(results), {'caffe'})
        self.assertEqual(errors, {'ignoto': 'not found'})
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('11.000'))


class AutoCreateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('operatore', password='x')
        self.token = ApiToken.objects.create(user=self.user, name='script')

    def url(self, path):
        return f'{path}?token={self.token.key}'

    def test_write_creates_the_counter(self):
        response = self.client.get(self.url('/add/caffe-lungo/3'))
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['created'])
        self.assertEqual(response.json()['value'], 3.0)

        counter = Counter.objects.get(slug='caffe-lungo')
        self.assertEqual(counter.name, 'Caffe lungo')
        self.assertEqual(counter.created_by, self.user)
        self.assertTrue(counter.is_active)

    def test_second_write_does_not_recreate(self):
        self.client.get(self.url('/add/caffe/1'))
        response = self.client.get(self.url('/add/caffe/1'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['created'])
        self.assertEqual(Counter.objects.filter(slug='caffe').count(), 1)

    def test_subtract_on_a_new_counter_goes_negative(self):
        response = self.client.get(self.url('/subtract/caffe/2'))
        self.assertEqual(response.json()['value'], -2.0)

    def test_reading_never_creates(self):
        """A read must not write, or a mis-configured display would seed rows."""
        self.assertEqual(self.client.get(self.url('/get/ignoto')).status_code, 404)
        self.client.get(self.url('/get') + '&counters=ignoto')
        self.assertFalse(Counter.objects.exists())

    def test_batch_creates(self):
        response = self.client.post(
            self.url('/set'),
            data=json.dumps({'nuovo': {'operation': 'set', 'value': 5}}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['results']['nuovo']['created'])
        self.assertEqual(Counter.objects.get(slug='nuovo').value, Decimal('5.000'))

    def test_batch_rejects_an_unusable_slug(self):
        """Batch slugs come from JSON keys, which no URL converter has filtered."""
        response = self.client.post(
            self.url('/set'),
            data=json.dumps({'Caffè Lungo!': {'operation': 'add', 'value': 1}}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Counter.objects.exists())

    def test_inactive_counter_is_not_recreated(self):
        Counter.objects.create(name='Caffè', is_active=False)
        self.assertEqual(self.client.get(self.url('/add/caffe/1')).status_code, 409)
        self.assertEqual(Counter.objects.count(), 1)

    @override_settings(COUNTERS_AUTOCREATE=False)
    def test_can_be_switched_off(self):
        self.assertEqual(self.client.get(self.url('/add/ignoto/1')).status_code, 404)
        self.assertFalse(Counter.objects.exists())

    def test_creation_survives_a_concurrent_insert(self):
        """Two writers can both miss the row; the loser must reuse the winner's.

        The rival's row is already committed and only our first lookup misses
        it — which is what a writer sees when the rival commits between its
        SELECT and its INSERT.
        """
        Counter.objects.create(name='Caffè', slug='caffe', value=Decimal('10'))

        real = Counter.objects.select_for_update
        missed = []

        def lookup(*args, **kwargs):
            queryset = real(*args, **kwargs)
            if missed:
                return queryset
            missed.append(True)
            blind = mock.Mock(wraps=queryset)
            blind.get.side_effect = Counter.DoesNotExist
            return blind

        with mock.patch.object(Counter.objects, 'select_for_update', lookup):
            result = apply_operation(
                'caffe', Transaction.ADD, Decimal('2'), create_missing=True,
            )

        self.assertFalse(result.created)
        self.assertEqual(result.counter.value, Decimal('12.000'))
        self.assertEqual(Counter.objects.filter(slug='caffe').count(), 1)


class ApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('operatore', password='x')
        self.token = ApiToken.objects.create(user=self.user, name='script')
        self.counter = Counter.objects.create(name='Caffè', value=Decimal('10'))
        self.client = Client()

    def url(self, path, token=True):
        return f'{path}?token={self.token.key}' if token else path

    def test_get_returns_value(self):
        response = self.client.get(self.url('/get/caffe'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['value'], 10.0)

    def test_add_subtract_set(self):
        self.client.get(self.url('/add/caffe/12'))
        self.client.get(self.url('/subtract/caffe/2,5'))
        response = self.client.get(self.url('/set/caffe/-10'))
        self.assertEqual(response.json()['value'], -10.0)
        self.assertEqual(Transaction.objects.count(), 3)

    def test_transaction_records_the_token_owner(self):
        self.client.get(self.url('/add/caffe/1'))
        self.assertEqual(Transaction.objects.get().user, self.user)

    def test_responses_are_never_cached(self):
        response = self.client.get(self.url('/get/caffe'))
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')

    def test_missing_token_is_refused(self):
        self.assertEqual(self.client.get('/add/caffe/1').status_code, 403)
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('10.000'))

    def test_revoked_token_is_refused(self):
        ApiToken.objects.filter(pk=self.token.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.url('/add/caffe/1')).status_code, 403)

    def test_bearer_header_is_accepted(self):
        response = self.client.get('/add/caffe/1', headers={'authorization': f'Bearer {self.token.key}'})
        self.assertEqual(response.status_code, 200)

    def test_session_cannot_write_over_get(self):
        """A GET write with only a cookie is what a CSRF attack looks like."""
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/add/caffe/1').status_code, 403)
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('10.000'))

    def test_inactive_counter_reads_but_does_not_write(self):
        Counter.objects.filter(pk=self.counter.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.url('/get/caffe')).status_code, 200)
        self.assertEqual(self.client.get(self.url('/add/caffe/1')).status_code, 409)

    def test_bad_value_is_refused(self):
        self.assertEqual(self.client.get(self.url('/add/caffe/abc')).status_code, 400)

    @override_settings(COUNTERS_AUTOCREATE=False)
    def test_unknown_counter_without_autocreate(self):
        self.assertEqual(self.client.get(self.url('/add/ignoto/1')).status_code, 404)

    def test_batch_write(self):
        Counter.objects.create(name='Tè', value=Decimal('5'))
        response = self.client.post(
            self.url('/set'),
            data=json.dumps({
                'caffe': {'operation': 'add', 'value': 1.5},
                'te': {'operation': 'set', 'value': 0},
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results']['caffe']['value'], 11.5)

    def test_batch_requires_operation(self):
        response = self.client.post(
            self.url('/set'),
            data=json.dumps({'caffe': 12}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_get_batch(self):
        Counter.objects.create(name='Tè', value=Decimal('5'))
        response = self.client.get(self.url('/get') + '&counters=caffe,te')
        self.assertEqual(response.json()['counters'], {'caffe': 10.0, 'te': 5.0})


class AdminActionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('capo', 'capo@example.com', 'x')
        self.client.force_login(self.admin)
        self.counter = Counter.objects.create(name='Caffè', value=Decimal('42'))

    def test_reset_action_logs_the_note(self):
        response = self.client.post('/admin/counters/counter/', {
            'action': 'reset_counters',
            '_selected_action': [self.counter.pk],
            'apply': '1',
            'value': '0',
            'notes': 'chiusura turno',
        }, follow=True)
        self.assertEqual(response.status_code, 200)

        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('0.000'))

        entry = Transaction.objects.get()
        self.assertEqual(entry.type, Transaction.SET)
        self.assertEqual(entry.source, Transaction.SOURCE_ADMIN)
        self.assertEqual(entry.notes, 'chiusura turno')
        self.assertEqual(entry.user, self.admin)

    def test_reset_action_requires_a_note(self):
        response = self.client.post('/admin/counters/counter/', {
            'action': 'reset_counters',
            '_selected_action': [self.counter.pk],
            'apply': '1',
            'value': '0',
            'notes': '',
        })
        self.assertEqual(response.status_code, 200)
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('42.000'))

    def test_display_list_links_to_the_display(self):
        Display.objects.create(name='Bancone')
        response = self.client.get('/admin/counters/display/')
        self.assertContains(response, 'href="/display/bancone"')

    def test_undo_compensates_without_deleting(self):
        original = apply_operation('caffe', Transaction.ADD, Decimal('5')).transaction

        self.client.post('/admin/counters/transaction/', {
            'action': 'undo_transactions',
            '_selected_action': [original.pk],
        }, follow=True)

        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, Decimal('42.000'))
        self.assertTrue(Transaction.objects.filter(pk=original.pk).exists())

        compensation = Transaction.objects.exclude(pk=original.pk).get()
        self.assertEqual(compensation.type, Transaction.SUBTRACT)
        self.assertEqual(compensation.notes, f'undo #{original.pk}')


class HotkeyTests(TestCase):
    def setUp(self):
        self.display = Display.objects.create(name='Cucina', add_key='+', subtract_key='-')
        self.counter = Counter.objects.create(name='Fritto misto')
        self.other = Counter.objects.create(name='Salamella')
        DisplayCounter.objects.create(display=self.display, counter=self.counter, key='q')

    def hotkey(self, **kwargs):
        return Hotkey(**{
            'display': self.display, 'counter': self.counter,
            'action': Transaction.ADD, 'value': Decimal('1'), **kwargs,
        })

    def test_key_is_normalised(self):
        entry = self.hotkey(key='  F1 ')
        entry.full_clean()
        self.assertEqual(entry.key, 'f1')

    def test_digits_and_separators_are_refused(self):
        for key in ('3', '.', ','):
            with self.assertRaises(ValidationError, msg=key):
                self.hotkey(key=key).full_clean()

    def test_clashing_keys_are_refused_by_the_model_itself(self):
        """Not only by the admin: a shell or a script must not shadow a key."""
        with self.assertRaises(ValidationError):
            self.hotkey(key='q').full_clean()      # taken by a counter
        with self.assertRaises(ValidationError):
            self.hotkey(key='+').full_clean()      # taken by an operation

    def test_a_hotkey_may_keep_its_own_key_when_edited(self):
        entry = Hotkey.objects.create(
            display=self.display, counter=self.counter, key='f1',
            action=Transaction.ADD, value=Decimal('1'),
        )
        entry.value = Decimal('5')
        entry.full_clean()  # must not report a clash with itself

    def test_counter_must_be_on_the_display(self):
        with self.assertRaises(ValidationError):
            self.hotkey(key='f2', counter=self.other).full_clean()

    def test_one_key_per_display(self):
        Hotkey.objects.create(
            display=self.display, counter=self.counter, key='f1',
            action=Transaction.ADD, value=Decimal('1'),
        )
        # atomic(): the failed insert would otherwise leave the test's own
        # transaction broken for anything added after this line.
        with self.assertRaises(IntegrityError), db_transaction.atomic():
            Hotkey.objects.create(
                display=self.display, counter=self.counter, key='f1',
                action=Transaction.SET, value=Decimal('0'),
            )

    def test_key_map_covers_every_source(self):
        Hotkey.objects.create(
            display=self.display, counter=self.counter, key='f1',
            action=Transaction.ADD, value=Decimal('1'), label='Fritto +1',
        )
        taken = display_key_map(self.display)
        self.assertEqual(set(taken), {'+', '-', '*', 'q', 'f1'})
        self.assertIn('Fritto misto', taken['q'])
        self.assertIn('Fritto +1', taken['f1'])

    def test_hotkeys_reach_the_page(self):
        Hotkey.objects.create(
            display=self.display, counter=self.counter, key='f1',
            action=Transaction.ADD, value=Decimal('2.5'), label='Fritto +2,5',
        )
        user = User.objects.create_user('kiosk', password='x')
        self.client.force_login(user)

        html = self.client.get('/display/cucina').content.decode()
        raw = html.split('id="display-config" type="application/json">')[1].split('</script>')[0]
        config = json.loads(raw)

        self.assertEqual(config['hotkeys'], [{
            'key': 'f1', 'slug': 'fritto-misto', 'action': 'add',
            'value': 2.5, 'label': 'Fritto +2,5',
        }])

    def test_hotkeys_on_inactive_counters_are_left_out(self):
        """No card to flash and the write would 409: the key would look broken."""
        Hotkey.objects.create(
            display=self.display, counter=self.counter, key='f1',
            action=Transaction.ADD, value=Decimal('1'),
        )
        Counter.objects.filter(pk=self.counter.pk).update(is_active=False)
        self.assertEqual(list(self.display.active_hotkeys()), [])


class HotkeyAdminTests(TestCase):
    """The two inlines share one keyboard but not one formset."""

    def setUp(self):
        self.admin = User.objects.create_superuser('capo', 'capo@example.com', 'x')
        self.client.force_login(self.admin)
        self.display = Display.objects.create(name='Cucina')
        self.counter = Counter.objects.create(name='Fritto misto')
        self.link = DisplayCounter.objects.create(
            display=self.display, counter=self.counter, key='q',
        )

    def post(self, hotkey_key, counter_key='q'):
        return self.client.post(f'/admin/counters/display/{self.display.pk}/change/', {
            'name': 'Cucina', 'slug': 'cucina',
            'refresh_interval': 2000, 'delta_highlight_duration': 4000,
            'grid_columns': '', 'add_key': '+', 'subtract_key': '-', 'set_key': '*',

            'displaycounter_set-TOTAL_FORMS': '1',
            'displaycounter_set-INITIAL_FORMS': '1',
            'displaycounter_set-0-id': self.link.pk,
            'displaycounter_set-0-display': self.display.pk,
            'displaycounter_set-0-counter': self.counter.pk,
            'displaycounter_set-0-key': counter_key,
            'displaycounter_set-0-position': '0',

            'hotkey_set-TOTAL_FORMS': '1',
            'hotkey_set-INITIAL_FORMS': '0',
            'hotkey_set-0-display': self.display.pk,
            'hotkey_set-0-counter': self.counter.pk,
            'hotkey_set-0-key': hotkey_key,
            'hotkey_set-0-action': 'add',
            'hotkey_set-0-value': '1',
            'hotkey_set-0-label': '',
        })

    def test_a_free_key_is_accepted(self):
        response = self.post('f1')
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None))
        self.assertEqual(Hotkey.objects.get().key, 'f1')

    def test_a_key_already_used_by_a_counter_is_refused(self):
        """Neither formset sees the other's rows; the request carries them."""
        response = self.post('q')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Hotkey.objects.exists())

    def test_a_key_moved_onto_a_hotkey_in_the_same_save_is_refused(self):
        response = self.post('z', counter_key='z')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Hotkey.objects.exists())

    def test_an_operation_key_is_refused(self):
        response = self.post('+')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Hotkey.objects.exists())


class DisplayTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('kiosk', password='x')
        self.display = Display.objects.create(name='Bar')
        self.active = Counter.objects.create(name='Caffè')
        self.hidden = Counter.objects.create(name='Tè', is_active=False)
        DisplayCounter.objects.create(display=self.display, counter=self.active, key='c')
        DisplayCounter.objects.create(display=self.display, counter=self.hidden, key='t')

    def test_requires_login(self):
        response = self.client.get('/display/bar')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/login'))

    def test_plain_operator_can_open_a_display(self):
        """A counter operator has no reason to be staff."""
        self.assertFalse(self.user.is_staff)
        logged_in = self.client.login(username='kiosk', password='x')
        self.assertTrue(logged_in)
        self.assertEqual(self.client.get('/display/bar').status_code, 200)

    def test_login_page_accepts_a_non_staff_user(self):
        response = self.client.post(
            '/login', {'username': 'kiosk', 'password': 'x', 'next': '/display/bar'},
        )
        self.assertRedirects(response, '/display/bar')

    def test_index_lists_the_displays(self):
        self.client.force_login(self.user)
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/display/bar')

    def test_inactive_counters_are_left_out(self):
        self.client.force_login(self.user)
        response = self.client.get('/display/bar')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([card['slug'] for card in response.context['cards']], ['caffe'])

    def test_config_is_embedded_as_a_json_object(self):
        """json_script must receive the dict, not an already-encoded string."""
        self.client.force_login(self.user)
        html = self.client.get('/display/bar').content.decode()
        raw = html.split('id="display-config" type="application/json">')[1].split('</script>')[0]
        config = json.loads(raw)
        self.assertIsInstance(config, dict)
        self.assertEqual([card['slug'] for card in config['cards']], ['caffe'])
        self.assertEqual(config['refreshInterval'], self.display.refresh_interval)

    def test_layout_fills_the_screen(self):
        self.assertEqual(self.display.layout(1), (1, 1))
        self.assertEqual(self.display.layout(2), (2, 1))
        self.assertEqual(self.display.layout(4), (2, 2))
        self.assertEqual(self.display.layout(6), (3, 2))
        self.assertEqual(self.display.layout(9), (3, 3))

    def test_grid_columns_override(self):
        self.display.grid_columns = 2
        self.assertEqual(self.display.layout(6), (2, 3))
