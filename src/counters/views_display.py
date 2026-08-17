from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from .models import Display


@login_required
def index(request):
    """Landing page: where a login without a `next` ends up."""
    return render(request, 'counters/index.html', {
        'displays': Display.objects.all(),
    })


@login_required
def display(request, slug):
    """The KDS page.

    It authenticates with the session: reads go to /get, writes to POST /set
    with the CSRF token. That way no API token is ever rendered into a page
    running unattended on a kiosk screen.
    """
    display = get_object_or_404(Display, slug=slug)
    rows = list(display.active_counters())
    columns, row_count = display.layout(len(rows))

    cards = [
        {
            'slug': row.counter.slug,
            'name': row.counter.name,
            'value': float(row.counter.value),
            'color': row.counter.color,
            'key': row.key,
        }
        for row in rows
    ]

    config = {
        'refreshInterval': display.refresh_interval,
        'highlightDuration': display.delta_highlight_duration,
        'addKey': display.add_key,
        'subtractKey': display.subtract_key,
        'setKey': display.set_key,
        'cards': cards,
        'hotkeys': [
            {
                'key': hotkey.key,
                'slug': hotkey.counter.slug,
                'action': hotkey.action,
                'value': float(hotkey.value),
                'label': hotkey.label,
            }
            for hotkey in display.active_hotkeys()
        ],
    }

    return render(request, 'counters/display.html', {
        'display': display,
        'cards': cards,
        'columns': columns,
        'rows': row_count,
        # Serialised by the json_script filter in the template, which escapes it
        # safely for embedding.
        'config': config,
    })
