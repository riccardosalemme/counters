import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import ApiToken, Counter, Display, DisplayCounter, Tag, Transaction, format_value, parse_value
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

    def test_unknown_counter_and_bad_value(self):
        self.assertEqual(self.client.get(self.url('/add/ignoto/1')).status_code, 404)
        self.assertEqual(self.client.get(self.url('/add/caffe/abc')).status_code, 400)

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
