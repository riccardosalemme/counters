/* KDS display: polls the batch read endpoint, highlights changes, and turns
 * keystrokes into batch writes. Authenticates with the session cookie, so no
 * API token is ever rendered into a page left running on an unattended screen.
 */

function displayApp(csrfToken) {
  const config = JSON.parse(document.getElementById('display-config').textContent);

  return {
    cards: config.cards,
    selected: null,
    buffer: '',
    deltas: {},
    flashing: null,
    offline: false,

    _primed: false,
    _timers: {},
    _idleTimer: null,
    _backoff: config.refreshInterval,

    start() {
      window.addEventListener('keydown', (event) => this.onKey(event));
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') this.keepAwake();
      });
      this.keepAwake();
      this.poll();
    },

    /* ---- rendering ---- */

    format(value) {
      return Number(value).toLocaleString('it-IT', { maximumFractionDigits: 3 });
    },

    formatDelta(delta) {
      return (delta > 0 ? '+' : '−') + this.format(Math.abs(delta));
    },

    // The CSS caps the value against the card's width and height, but not
    // against how long the number is: "1.234,5" needs less type than "8" to
    // fit the same card. Three characters keep full size, longer shrinks.
    valueScale(card) {
      const length = this.format(card.value).length;
      return length <= 3 ? 1 : Math.max(0.45, 3 / length);
    },

    // Pick black or white ink from the perceived brightness of the card colour,
    // so a selected card stays readable whatever colour it was given.
    inkFor(color) {
      const hex = color.replace('#', '');
      const r = parseInt(hex.slice(0, 2), 16);
      const g = parseInt(hex.slice(2, 4), 16);
      const b = parseInt(hex.slice(4, 6), 16);
      return (r * 0.299 + g * 0.587 + b * 0.114) / 255 > 0.6 ? '#000' : '#fff';
    },

    /* ---- polling ---- */

    async poll() {
      const slugs = this.cards.map((card) => card.slug).join(',');
      try {
        const response = await fetch(`/get?counters=${encodeURIComponent(slugs)}`, {
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error(response.status);
        const data = await response.json();

        for (const card of this.cards) {
          const value = data.counters[card.slug];
          if (value === undefined || value === card.value) continue;
          // No badge on the very first poll: there is no real "before" yet.
          if (this._primed) this.showDelta(card.slug, value - card.value);
          card.value = value;
        }

        this._primed = true;
        this.offline = false;
        this._backoff = config.refreshInterval;
      } catch (error) {
        this.offline = true;
        this._backoff = Math.min(this._backoff * 2, 30000);
      }
      setTimeout(() => this.poll(), this._backoff);
    },

    showDelta(slug, delta) {
      if (!delta) return;
      this.deltas[slug] = delta;
      clearTimeout(this._timers[slug]);
      this._timers[slug] = setTimeout(() => {
        delete this.deltas[slug];
      }, config.highlightDuration);
    },

    /* ---- keyboard ---- */

    onKey(event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const key = event.key.toLowerCase();

      // Hotkeys come first, and fire from any state: a hotkey always does
      // exactly what it was configured to do, which is the only behaviour that
      // stays predictable for someone hitting it without looking. A quantity
      // half-typed on another counter is dropped.
      const hotkey = (config.hotkeys || []).find((item) => item.key === key);
      if (hotkey) {
        event.preventDefault();
        this.clear();
        this.flash(hotkey.slug);
        this.send(hotkey.slug, hotkey.action, hotkey.value);
        return;
      }

      const target = this.cards.find((card) => card.key && card.key.toLowerCase() === key);
      if (target) {
        this.select(target.slug);
        return event.preventDefault();
      }

      if (key === 'escape') {
        this.clear();
        return event.preventDefault();
      }

      if (!this.selected) return;

      if (key === 'backspace') {
        this.buffer = this.buffer.slice(0, -1);
        return this.touch(event);
      }

      if (/^[0-9]$/.test(key)) {
        this.buffer += key;
        return this.touch(event);
      }

      // Both separators are accepted, and only one of them per number.
      if ((key === '.' || key === ',') && !this.buffer.includes(',')) {
        this.buffer = (this.buffer || '0') + ',';
        return this.touch(event);
      }

      if (key === config.addKey) return this.submit('add', event);
      if (key === config.subtractKey) return this.submit('subtract', event);
      if (key === config.setKey) return this.submit('set', event);
    },

    select(slug) {
      this.selected = slug;
      this.buffer = '';
      this.resetIdle();
    },

    clear() {
      this.selected = null;
      this.buffer = '';
      clearTimeout(this._idleTimer);
    },

    touch(event) {
      this.resetIdle();
      event.preventDefault();
    },

    // A key pressed by accident should not leave a card armed forever.
    resetIdle() {
      clearTimeout(this._idleTimer);
      this._idleTimer = setTimeout(() => this.clear(), 10000);
    },

    /* ---- writing ---- */

    submit(operation, event) {
      event.preventDefault();
      // An empty buffer means "one of these", the common case at a counter.
      // Not for set, where the intended value is never implicit.
      if (!this.buffer && operation === 'set') return;
      const value = this.buffer ? Number(this.buffer.replace(',', '.')) : 1;
      if (Number.isNaN(value)) return this.clear();

      const slug = this.selected;
      this.flash(slug);
      this.clear();
      this.send(slug, operation, value);
    },

    flash(slug) {
      this.flashing = slug;
      setTimeout(() => {
        if (this.flashing === slug) this.flashing = null;
      }, 300);
    },

    async send(slug, operation, value) {
      try {
        const response = await fetch('/set', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
          body: JSON.stringify({ [slug]: { operation, value } }),
        });
        const data = await response.json();
        const result = data.results && data.results[slug];
        if (!result) throw new Error(data.error || response.status);

        // Apply straight away instead of waiting for the next poll.
        const card = this.cards.find((item) => item.slug === slug);
        if (card) {
          this.showDelta(slug, result.value - card.value);
          card.value = result.value;
        }
        this.offline = false;
      } catch (error) {
        this.offline = true;
      }
    },

    /* ---- kiosk ---- */

    async keepAwake() {
      if (!('wakeLock' in navigator)) return;
      try {
        await navigator.wakeLock.request('screen');
      } catch (error) {
        /* Screen sleep is a nuisance, not a failure. */
      }
    },
  };
}
