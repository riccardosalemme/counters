from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.shortcuts import render
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    RESERVED_KEYS,
    ApiToken,
    Counter,
    Display,
    DisplayCounter,
    Hotkey,
    Tag,
    Transaction,
    display_key_map,
    format_value,
    normalize_key,
)
from .operations import OperationError, apply_operation


class BaseModelAdmin(admin.ModelAdmin):
    """Stamps created_by / updated_by on every save made through the admin."""

    readonly_fields = ('created_at', 'updated_at', 'created_by', 'updated_by')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Tag)
class TagAdmin(BaseModelAdmin):
    list_display = ('name', 'slug', 'counter_count')
    search_fields = ('name', 'slug')

    @admin.display(description='counter')
    def counter_count(self, obj):
        return obj.counters.count()


class TransactionInline(admin.TabularInline):
    model = Transaction
    extra = 0
    max_num = 0
    can_delete = False
    fields = ('created_at', 'type', 'value', 'value_before', 'value_after', 'user', 'source', 'notes')
    readonly_fields = fields
    verbose_name_plural = 'ultime transazioni'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')


class ResetForm(forms.Form):
    value = forms.DecimalField(
        label='Nuovo valore', initial=Decimal(0),
        max_digits=Counter._meta.get_field('value').max_digits,
        decimal_places=Counter._meta.get_field('value').decimal_places,
    )
    notes = forms.CharField(
        label='Nota', widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Resta allegata alle transazioni create, per sapere poi perché il reset è stato fatto.',
    )


@admin.register(Counter)
class CounterAdmin(BaseModelAdmin):
    list_display = ('swatch', 'name', 'slug', 'current_value', 'tag', 'is_active')
    list_display_links = ('name',)
    # created_at in the filters: with auto-creation on, this is how a counter
    # born from a typo gets spotted.
    list_filter = ('is_active', 'tag', 'created_at')
    search_fields = ('name', 'slug')
    autocomplete_fields = ('tag',)
    inlines = (TransactionInline,)
    actions = ('reset_counters',)

    @admin.display(description='valore')
    def current_value(self, obj):
        return format_value(obj.value)

    @admin.display(description='')
    def swatch(self, obj):
        return format_html(
            '<span style="display:inline-block;width:1em;height:1em;border-radius:3px;'
            'background:{}"></span>',
            obj.color,
        )

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        # Scripts hard-code the slug in their URLs, so it must not move once
        # the counter exists.
        return fields + ('slug',) if obj else fields

    @admin.action(description='Reset dei counter selezionati')
    def reset_counters(self, request, queryset):
        if 'apply' in request.POST:
            form = ResetForm(request.POST)
            if form.is_valid():
                value = form.cleaned_data['value']
                notes = form.cleaned_data['notes']
                done, failed = 0, []
                for counter in queryset:
                    try:
                        apply_operation(
                            counter.slug, Transaction.SET, value,
                            user=request.user, source=Transaction.SOURCE_ADMIN, notes=notes,
                        )
                        done += 1
                    except OperationError as exc:
                        failed.append(f'{counter.slug}: {exc.code}')
                if done:
                    self.message_user(
                        request, f'{done} counter portati a {format_value(value)}.',
                        messages.SUCCESS,
                    )
                for failure in failed:
                    self.message_user(request, failure, messages.WARNING)
                return None
        else:
            form = ResetForm()

        return render(request, 'admin/counters/counter/reset_action.html', {
            **self.admin_site.each_context(request),
            'title': 'Reset dei counter selezionati',
            'counters': queryset,
            'form': form,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
            'opts': self.model._meta,
        })


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'counter', 'type', 'shown_value', 'shown_after', 'user', 'source', 'notes')
    list_filter = ('type', 'source', 'counter')
    search_fields = ('counter__name', 'counter__slug', 'notes')
    date_hierarchy = 'created_at'
    actions = ('undo_transactions',)

    @admin.display(description='valore')
    def shown_value(self, obj):
        return format_value(obj.value)

    @admin.display(description='risultato')
    def shown_after(self, obj):
        return format_value(obj.value_after)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('counter', 'user')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.action(description='Annulla le transazioni selezionate')
    def undo_transactions(self, request, queryset):
        """Compensate rather than delete: the original row stays in the log."""
        done, failed = 0, []
        for entry in queryset.select_related('counter'):
            if entry.type == Transaction.ADD:
                operation, value = Transaction.SUBTRACT, entry.value
            elif entry.type == Transaction.SUBTRACT:
                operation, value = Transaction.ADD, entry.value
            else:
                # Restoring value_before is only exact if nothing else has
                # touched the counter since; the note makes that traceable.
                operation, value = Transaction.SET, entry.value_before
            try:
                apply_operation(
                    entry.counter.slug, operation, value,
                    user=request.user, source=Transaction.SOURCE_ADMIN,
                    notes=f'undo #{entry.pk}',
                )
                done += 1
            except OperationError as exc:
                failed.append(f'#{entry.pk} {entry.counter.slug}: {exc.code}')

        if done:
            self.message_user(request, f'{done} transazioni annullate.', messages.SUCCESS)
        for failure in failed:
            self.message_user(request, failure, messages.WARNING)


def live_rows(formset):
    """The forms of a formset that will actually end up in the database."""
    for form in formset.forms:
        if form.cleaned_data and not form.cleaned_data.get('DELETE'):
            yield form


def check_keys(formset, request, display, described):
    """Reject a key already spoken for anywhere on this display.

    Keys come from four places — counter selection, the three operation keys,
    the digits reserved for the quantity buffer, and hotkeys — but a display has
    only one keyboard. Two inlines on the same page cannot see each other's
    unsaved rows, so each one leaves its keys on the request for the next to
    read. DisplayCounterInline is validated first (see DisplayAdmin.inlines),
    so a clash is reported on the hotkey row; either way the save is blocked.
    """
    pending = getattr(request, '_pending_display_keys', {})
    # The rows this formset owns are re-checked from the submitted data, so
    # their stored keys must not count as taken. Only skip pks of this
    # formset's own model — the two tables number their rows independently.
    own_pks = [form.instance.pk for form in formset.forms if form.instance.pk]
    is_counter = formset.model is DisplayCounter
    taken = display_key_map(
        display,
        skip_counters=own_pks if is_counter else (),
        skip_hotkeys=() if is_counter else own_pks,
        pending=pending,
    ) if display else dict(pending)

    mine = {}
    for form in live_rows(formset):
        key = normalize_key(form.cleaned_data.get('key'))
        if not key:
            continue
        if key in RESERVED_KEYS:
            form.add_error('key', 'Cifre, punto e virgola sono riservati alla quantità.')
        elif key in mine:
            form.add_error('key', f'Tasto già usato da un altro {described} qui sopra.')
        elif key in taken:
            form.add_error('key', f'Tasto già usato da {taken[key]}.')
        else:
            mine[key] = f'{described} in questa pagina'

    request._pending_display_keys = {**pending, **mine}


class DisplayCounterInline(admin.TabularInline):
    model = DisplayCounter
    extra = 1
    autocomplete_fields = ('counter',)

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        display = obj

        class Formset(formset):
            def clean(self):
                super().clean()
                check_keys(self, request, display, 'counter')
                # Leave the counters behind too, so a hotkey can point at one
                # added in this very save.
                request._pending_display_counters = {
                    form.cleaned_data['counter'].pk
                    for form in live_rows(self)
                    if form.cleaned_data.get('counter')
                }

        return Formset


class HotkeyInline(admin.TabularInline):
    model = Hotkey
    extra = 0
    autocomplete_fields = ('counter',)
    fields = ('key', 'label', 'counter', 'action', 'value')

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        display = obj

        class Formset(formset):
            def clean(self):
                super().clean()
                check_keys(self, request, display, 'hotkey')

                on_display = set(getattr(request, '_pending_display_counters', set()))
                if not on_display and display:
                    on_display = set(
                        display.displaycounter_set.values_list('counter_id', flat=True)
                    )

                for form in live_rows(self):
                    counter = form.cleaned_data.get('counter')
                    if counter and counter.pk not in on_display:
                        form.add_error(
                            'counter', 'Il counter non è fra quelli di questo display.',
                        )

        return Formset


@admin.register(Display)
class DisplayAdmin(BaseModelAdmin):
    list_display = ('name', 'slug', 'counter_count', 'refresh_interval',
                    'delta_highlight_duration', 'open_link')
    search_fields = ('name', 'slug')
    # Order matters: DisplayCounterInline is validated first and leaves its
    # pending keys and counters on the request for HotkeyInline to check.
    inlines = (DisplayCounterInline, HotkeyInline)
    fieldsets = (
        (None, {'fields': ('name', 'slug')}),
        ('Aggiornamento', {'fields': ('refresh_interval', 'delta_highlight_duration')}),
        ('Layout', {'fields': ('grid_columns',)}),
        ('Tasti operazione', {'fields': ('add_key', 'subtract_key', 'set_key')}),
        ('Tracciamento', {'fields': BaseModelAdmin.readonly_fields, 'classes': ('collapse',)}),
    )

    @admin.display(description='counter')
    def counter_count(self, obj):
        return obj.counters.count()

    @admin.display(description='apri')
    def open_link(self, obj):
        if not obj.slug:
            return ''
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">apri &nearr;</a>',
            reverse('display', args=[obj.slug]),
        )

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return fields + ('slug',) if obj else fields


@admin.register(Hotkey)
class HotkeyAdmin(admin.ModelAdmin):
    list_display = ('key', 'label', 'display', 'counter', 'action', 'shown_value')
    list_filter = ('display', 'action')
    search_fields = ('key', 'label', 'counter__name')
    autocomplete_fields = ('counter',)

    @admin.display(description='quantità')
    def shown_value(self, obj):
        return format_value(obj.value)


@admin.register(ApiToken)
class ApiTokenAdmin(BaseModelAdmin):
    list_display = ('name', 'user', 'is_active', 'last_used_at')
    list_filter = ('is_active', 'user')
    search_fields = ('name', 'user__username')
    autocomplete_fields = ('user',)

    def get_readonly_fields(self, request, obj=None):
        return super().get_readonly_fields(request, obj) + ('key', 'last_used_at')

    def get_fields(self, request, obj=None):
        fields = ['user', 'name', 'is_active']
        if obj:
            # Generated by the backend, shown only once it exists so it can be
            # copied into a script.
            fields += ['key', 'last_used_at']
        return fields + list(BaseModelAdmin.readonly_fields)
