# counters

`counters` è un'applicazione web per gestire contatori numerici tramite semplici chiamate HTTP e per visualizzarli su schermi in stile KDS (Kitchen Display System), con supporto a input da tastiera fisica.

Obiettivi di progetto:

- API richiamabile da script, curl, shortcut iOS, ESP32, bottoni fisici: URL semplici, nessun header obbligatorio.
- Ogni modifica è tracciata (chi, quando, cosa, perché) tramite transazioni.
- Configurazione interamente da Django admin, zero file di configurazione per l'uso quotidiano.
- Codice minimale e leggibile.

---

## 1. Avvio rapido

```bash
uv sync
cd src
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Da `http://localhost:8000/admin/`: crea un `Counter`, un `ApiToken` (la chiave la genera il backend) e un `Display` con i counter e i tasti associati. Poi:

```bash
curl "http://localhost:8000/add/caffe/1?token=<chiave>"
```

Il display è su `http://localhost:8000/display/<slug>`, e `http://localhost:8000/` elenca quelli configurati. Chi deve solo guardare o usare un display **non serve che sia staff**: gli basta un utente Django qualsiasi, che accede da `/login`.

### Configurazione

Tutto opzionale, con default da sviluppo:

| Variabile | Default | Note |
|---|---|---|
| `SECRET_KEY` | chiave insicura di sviluppo | obbligatoria in produzione |
| `DEBUG` | `true` | |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | separati da virgola |
| `CSRF_TRUSTED_ORIGINS` | vuoto | separati da virgola |
| `DATABASE_URL` | SQLite in `src/db.sqlite3` | `postgres://user:pw@host:5432/db` |
| `TIME_ZONE` | `Europe/Rome` | |
| `COUNTERS_AUTOCREATE` | `true` | crea al volo un counter nominato da una scrittura ma inesistente, vedi §5.2 |
| `SECURE_SSL_REDIRECT` | `true` se `DEBUG=false` | |
| `SECURE_HSTS_SECONDS` | `0` | opt-in: il browser lo rispetta per tutta la durata indicata, quindi un valore dato in fretta è scomodo da annullare |

Con `DEBUG=false` i cookie diventano `Secure` e viene forzato HTTPS.

---

## 2. Stack

| Componente | Scelta |
|---|---|
| Backend | Django 5.2 |
| DB | SQLite (single-node) o PostgreSQL, via `DATABASE_URL` |
| API | view Django classiche — gli endpoint sono pochi e non REST-standard, DRF non servirebbe a niente |
| Frontend display | template Django + Alpine.js vendorizzato, **nessun build step** |
| Admin | Django admin standard |

Su SQLite le connessioni usano `transaction_mode=IMMEDIATE` e journal WAL: SQLite non ha lock di riga, quindi `select_for_update()` lì è un no-op e la correttezza del read-modify-write dipende dal fatto che il lock di scrittura venga preso all'apertura della transazione anziché alla prima scrittura.

---

## 3. Modello dati

Tutti i modelli configurabili ereditano da `BaseModel` (`created_at`, `updated_at`, `created_by`, `updated_by`). `Tag`, `Counter` e `Display` ereditano da `SluggedModel`, che riempie lo slug vuoto partendo dal nome (minuscolo, spazi → `-`, accenti rimossi) aggiungendo un suffisso numerico in caso di collisione.

### Tag

`name`, `slug`. Raggruppa i counter per filtrarli in admin.

### Counter

| Campo | Tipo | Note |
|---|---|---|
| `name` | CharField(100) | |
| `slug` | SlugField(100), unique | identificatore usato nelle API; **readonly dopo la creazione**, perché gli script hanno l'URL scritto dentro |
| `value` | DecimalField(12, 3) | valore corrente, **può essere negativo**, nessun clamp |
| `tag` | FK → Tag, `SET_NULL` | |
| `color` | CharField(7) | `#RRGGBB`, validato |
| `is_active` | BooleanField, default `True` | vedi sotto |

**`is_active = False`** rende il counter di sola lettura: `GET /get/<slug>` continua a funzionare, ogni scrittura risponde `409`, e la card sparisce dalle griglie dei display senza dover toccare la configurazione del `Display`. È il modo corretto di togliere di mezzo un counter: cancellarlo distruggerebbe il suo storico.

### Transaction

| Campo | Tipo | Note |
|---|---|---|
| `counter` | FK → Counter, `CASCADE` | |
| `type` | `add` \| `subtract` \| `set` | |
| `value` | Decimal | valore richiesto dall'operazione |
| `value_before` | Decimal | valore prima |
| `value_after` | Decimal | valore dopo |
| `user` | FK → User, `SET_NULL` | proprietario del token, o utente loggato sul display |
| `source` | `api` \| `display` \| `admin` | |
| `notes` | TextField, blank | motivo dell'operazione |
| `created_at` | DateTimeField | |

Le transazioni sono immutabili: in admin non si creano né si modificano a mano. `value_before` / `value_after` rendono il log ricostruibile e permettono l'undo.

`notes` è pensato per i reset mirati fatti da admin ("chiusura turno 17/08"), ed è compilato automaticamente dall'azione di undo.

### Display

| Campo | Tipo | Note |
|---|---|---|
| `name`, `slug` | | |
| `refresh_interval` | ms, default 2000 | frequenza di polling |
| `delta_highlight_duration` | ms, default 4000 | permanenza del badge +/- |
| `grid_columns` | nullable | forza il numero di colonne; se vuoto, `ceil(sqrt(n))` |
| `counters` | M2M via `DisplayCounter` | |
| `add_key`, `subtract_key`, `set_key` | CharField(1) | tasti che confermano l'operazione |

> Il campo `alert_lifetime` della specifica iniziale si chiama ora `delta_highlight_duration`: non è un alert (non segnala un errore) ed è utile che l'unità di misura stia nel nome.

### DisplayCounter

`display`, `counter`, `key` (tasto che seleziona la card), `position` (ordine in griglia). Unicità su `(display, counter)` e su `(display, key)`.

Cifre `0-9`, `.` e `,` sono **riservate** al buffer della quantità e non possono essere assegnate né a un counter né a un'operazione. La validazione sta in `Display.clean()` e nel formset dell'inline, che verifica anche che le key dei counter non collidano con i tasti operazione del display.

### Hotkey

Un tasto che porta con sé tutta l'operazione, per i gesti che si ripetono per tutto il turno.

| Campo | Tipo | Note |
|---|---|---|
| `display` | FK → Display, `CASCADE` | |
| `counter` | FK → Counter, `CASCADE` | deve essere fra i counter del display |
| `key` | CharField(20) | un carattere, oppure `F1`, `ArrowUp`, `Enter`, `Space` |
| `action` | `add` \| `subtract` \| `set` | riusa `Transaction.TYPE_CHOICES` |
| `value` | Decimal, default 1 | |
| `label` | CharField(60), blank | promemoria in admin, es. "Fritto +1" |

Unicità su `(display, key)`. Il `counter` deve già essere sul display: premere una hotkey deve produrre un effetto visibile, e un counter fuori griglia non ha una card da far lampeggiare.

`key` è lungo 20 e non 1 perché le lettere finiscono in fretta fra counter e operazioni; il confronto avviene su `event.key` normalizzato a minuscolo su entrambi i lati, quindi `F1` in admin diventa `f1` a database e combacia con `F1` premuto.

**Un display ha una sola tastiera ma quattro sorgenti di tasti**: selezione counter (`DisplayCounter.key`), operazioni (`add_key`/`subtract_key`/`set_key`), cifre e separatori riservati al buffer, e hotkey. `display_key_map()` in [models.py](src/counters/models.py) è l'unica definizione di "questo tasto è occupato", e la usano `Hotkey.clean()`, i due inline admin e i test. Il controllo sta **sul modello** e non solo in admin, così nemmeno uno script può creare una hotkey che oscura un tasto esistente.

Nell'admin i due inline non possono vedersi le righe non ancora salvate, quindi si scambiano i tasti pendenti tramite la `request`: `DisplayCounterInline` è validato per primo e li deposita, `HotkeyInline` li legge. Il conflitto viene segnalato sulla riga della hotkey — da un lato solo, ma il salvataggio è bloccato comunque. Lo stesso canale permette di aggiungere un counter alla griglia e la sua hotkey in un unico salvataggio.

### ApiToken

`user`, `name`, `key`, `is_active`, `last_used_at`.

- La chiave è **generata dal backend** e non è modificabile: compare readonly in admin dopo il salvataggio, pronta da copiare.
- Un utente può avere quanti token vuole; in admin si sceglie a quale utente associarli, quindi un amministratore può crearli per conto di altri.
- Nessuno scope: ogni token può operare su qualunque counter. La revoca è per token (`is_active`), così si spegne un singolo script senza toccare gli altri.
- La chiave è salvata in chiaro perché deve restare rileggibile in admin. È una credenziale a basso valore: consente solo di muovere contatori.
- `last_used_at` viene aggiornato al massimo una volta al minuto, altrimenti ogni lettura autenticata costerebbe una scrittura.

---

## 4. Autenticazione

Token nella query string, parametro `token`:

```
https://counters.example.com/add/caffe/12?token=<chiave>
```

In alternativa `Authorization: Bearer <chiave>`, per i client che preferiscono tenere il segreto fuori dall'URL.

### La regola non negoziabile

**Un endpoint di scrittura raggiunto in GET non accetta mai il cookie di sessione.** Django non applica protezione CSRF ai GET: se lo accettasse, qualunque pagina web potrebbe eseguire un'operazione sul tuo account con un `<img src="https://counters.example.com/add/caffe/9999">`. Richiedendo il token esplicito, l'attaccante dovrebbe conoscerlo.

Le scritture in POST possono usare la sessione, perché lì il CSRF token c'è. Le view API sono `csrf_exempt` (così uno script può fare POST con il solo token), ma il ramo "sessione" **riesegue il controllo CSRF a mano** — vedi `check_csrf()` in [src/counters/auth.py](src/counters/auth.py). È così che il display scrive senza avere un token in pagina.

`SESSION_COOKIE_SAMESITE = "Strict"` fa da seconda linea.

### Rischi accettati del token in query string

Il token finisce nei log di nginx/Django, nella cronologia del browser, e in un URL condiviso per sbaglio. Combinato con i GET di scrittura, questo significa che **un URL di scrittura incollato in una chat viene aperto dall'unfurler di Slack/WhatsApp/iMessage e il contatore si muove da solo**.

Mitigazioni attive:

- `Cache-Control: no-store` e `Referrer-Policy: no-referrer` su ogni risposta API;
- un token per device/script, revocabile singolarmente;
- HTTPS forzato quando `DEBUG=false`.

Da fare prima di esporre l'app su internet: filtro sui log per oscurare `token=`, e rate limit per token (vedi §9).

La rete di sicurezza vera resta il log completo delle transazioni: qualunque movimento errato è identificabile e annullabile.

---

## 5. API

Base URL negli esempi: `https://counters.example.com`. Ogni chiamata richiede `?token=<chiave>`, omesso qui sotto per leggibilità.

I valori viaggiano come **numeri JSON**, non stringhe: con 12 cifre significative il round-trip in `double` è esatto e gli script possono fare aritmetica senza conversioni.

In input sono accettati sia `.` sia `,` come separatore decimale, quindi `/add/caffe/1.5` e `/add/caffe/1,5` sono equivalenti.

### 5.1 Singolo counter (GET)

| Endpoint | Effetto |
|---|---|
| `GET /add/<slug>/<n>` | `value += n` |
| `GET /subtract/<slug>/<n>` | `value -= n` |
| `GET /set/<slug>/<n>` | `value = n`, **sempre assoluto** |
| `GET /get/<slug>` | legge il valore, nessuna transazione |

`set` imposta soltanto: la sintassi `+n` / `-n` non esiste più, per incrementare c'è `add`. Quindi `GET /set/caffe/-10` porta il counter **a -10**, senza ambiguità. `GET /add/caffe/-3` funziona ed equivale a un `subtract` di 3, ma sporca il log: meglio usare l'endpoint giusto.

```json
{
  "slug": "caffe",
  "name": "Caffè",
  "value": 51.5,
  "previous_value": 54,
  "transaction_id": 1187
}
```

### 5.2 Creazione automatica

Con `COUNTERS_AUTOCREATE` attivo (default), **una scrittura su uno slug inesistente crea il counter** invece di rispondere `404`. Serve per aggiungere un contatore da uno script senza passare dall'admin:

```bash
curl "https://counters.example.com/add/caffe-lungo/1?token=<chiave>"   # 201
```

Il counter nasce con `value` 0 prima dell'operazione, nome derivato dallo slug (`caffe-lungo` → "Caffe lungo"), colore di default e nessun tag; `created_by` è il proprietario del token. Sono tutte cose da rifinire in admin, ma il conteggio parte subito.

Regole:

- **Solo in scrittura.** `GET /get/<slug>` su uno slug inesistente resta `404` e la lettura batch continua a omettere gli slug che non conosce: una lettura non deve creare righe, altrimenti un display mal configurato che fa polling ogni due secondi si seminerebbe counter da solo.
- **Status `201`** invece di `200` quando la creazione avviene davvero, e il campo **`"created": true`** nella risposta (anche dentro `results` nel batch).
- Un `subtract` su un counter appena creato parte da 0 e finisce **negativo**, coerentemente con il resto.
- Un counter **inattivo non viene ricreato**: risponde `409`, come qualunque altra scrittura su un counter inattivo.
- Lo slug deve essere utilizzabile (`[-a-zA-Z0-9_]+`, max 100 caratteri) o la risposta è `400`. Il controllo conta soprattutto per il batch, dove gli slug arrivano da chiavi JSON che nessun converter di URL ha filtrato.

**Il prezzo**: sparisce il `404` che oggi ti dice che uno script ha lo slug sbagliato. Con la creazione automatica attiva, `/add/caffè/1` scritto per errore al posto di `/add/caffe/1` risponde `201` e da quel momento metà dei tuoi conteggi finisce in un counter fantasma, in silenzio. Per questo la lista dei counter in admin è filtrabile per data di creazione — è così che si scovano — e per questo la funzione si spegne con `COUNTERS_AUTOCREATE=false` una volta che l'elenco dei contatori si è stabilizzato.

### 5.3 Scrittura batch (POST)

```
POST /set          (alias: POST /batch)
Content-Type: application/json

{
  "counter-1": {"operation": "set",      "value": 12},
  "counter-3": {"operation": "add",      "value": 1.5},
  "counter-7": {"operation": "subtract", "value": 5}
}
```

Operazione e valore sono **campi separati**: niente stringhe `+1` da interpretare, niente ambiguità sui negativi, e il `type` della transazione salvata coincide sempre con `operation`.

```json
{
  "results": {
    "counter-1": {"slug": "counter-1", "name": "…", "value": 12, "previous_value": 9, "transaction_id": 1188}
  },
  "errors": {}
}
```

Il batch gira in una **singola transazione DB**, con i counter bloccati in ordine di slug per evitare deadlock fra batch concorrenti. Di default un solo elemento non valido fa rollback dell'intero batch e la risposta porta lo status dell'errore incontrato (`404` slug inesistente, `409` counter inattivo, `400` valore o operazione non validi).

Con `?partial=1` le operazioni valide vengono applicate, le altre finiscono in `errors` e lo status è `207`.

Questo endpoint accetta anche la sola sessione Django, purché arrivi il CSRF token: è la via che usa il display.

### 5.4 Lettura batch (GET)

```
GET /get?counters=counter-1,counter-3,counter-7
```

```json
{
  "counters": {"counter-1": 12, "counter-3": 4.5, "counter-7": -2},
  "server_time": "2026-08-17T10:15:00Z"
}
```

Gli slug inesistenti sono semplicemente assenti dalla risposta, senza errore: un display mal configurato deve continuare a mostrare gli altri counter.

### 5.5 Codici di stato

`200` ok · `201` counter creato al volo · `207` batch parziale · `400` valore o operazione non validi · `403` token assente, non valido o disattivato (e scrittura GET tentata con la sola sessione) · `404` counter inesistente · `405` metodo sbagliato · `409` counter inattivo.

### 5.6 Atomicità

Tutto passa da `apply_operation()` / `apply_batch()` in [src/counters/operations.py](src/counters/operations.py), che sono l'unico punto in cui un valore cambia. Ogni operazione è `select_for_update()` dentro `transaction.atomic()`, così due chiamate concorrenti non perdono un incremento. Un risultato che sforerebbe le 12 cifre viene rifiutato con `400` invece di finire troncato nel database.

---

## 6. Display (interfaccia KDS)

`GET /display/<slug>`, protetta da login. Il kiosk si autentica una volta e resta loggato.

L'app ha un **login proprio** su `/login`, separato da quello dell'admin: quello dell'admin respinge chiunque non abbia `is_staff`, e un operatore che deve solo battere i counter non ha motivo di essere staff. Dopo l'accesso, `/` elenca i display disponibili. In admin, la lista dei display ha una colonna **apri** che porta direttamente allo schermo corrispondente.

La pagina legge con `GET /get?counters=…` usando la sessione e scrive con `POST /set` usando sessione e CSRF token. Nessun token API viene mai renderizzato dentro una pagina che gira su uno schermo non presidiato.

### Layout

I counter occupano tutti lo stesso spazio riempiendo lo schermo: `colonne = grid_columns or ceil(sqrt(n))`, `righe = ceil(n / colonne)`. Quindi 1 → 1×1, 2 → 2×1, 4 → 2×2, 6 → 3×2, 9 → 3×3. Con 3, 5 o 7 counter la griglia lascia una cella vuota; `grid_columns` permette di forzare un layout diverso.

Lo **sfondo è bianco** e i numeri sono neri: questi schermi stanno dove c'è luce diurna, e a piena luminosità il nero su bianco resta leggibile all'aperto in un modo che un tema scuro non regge. Il colore lo porta il bordo, spesso, non il fondo.

Ogni card ha quindi il bordo del colore del counter, il tasto associato in alto a destra, il valore in grande (font in `cqi`, quindi scala con la cella e non con la finestra) e, quando serve, il badge di variazione.

Selezionando una card questa **si riempie del colore del counter e inverte l'inchiostro**. Il contrasto non è invertito a caso: bianco o nero vengono scelti dalla luminanza percepita del colore (`r*0.299 + g*0.587 + b*0.114`), così una card resta leggibile qualunque colore le sia stato dato.

I valori sono formattati con `toLocaleString('it-IT')`: `42`, `42,5`, `-3,25`. Un valore senza decimali si legge come intero.

### Aggiornamento

Polling ogni `refresh_interval` ms. Ogni differenza rispetto al valore precedente accende un badge `+3` / `−2,5` verde o rosso per `delta_highlight_duration` ms. Al **primo** polling i badge sono soppressi: non esiste ancora un "prima" reale.

Se una richiesta fallisce compare una barra "connessione persa" e il polling entra in backoff esponenziale fino a 30 s, senza svuotare i valori a schermo. Il display richiede inoltre un Wake Lock per non far spegnere lo schermo.

### Tastiera

```
QUALSIASI STATO
 └─ tasto di una hotkey                → esegue counter+azione+quantità configurati,
                                         torna a IDLE

IDLE
 └─ tasto di un counter                → SELECTED(counter)
SELECTED(counter)
 ├─ cifre 0-9, "." o ","               → buffer quantità
 ├─ tasto di un altro counter          → SELECTED(altro), buffer azzerato
 ├─ Backspace                          → cancella l'ultima cifra
 ├─ Escape, o 10 s di inattività       → IDLE
 └─ add_key / subtract_key / set_key   → esegue l'operazione, torna a IDLE
```

Le **hotkey** hanno la precedenza su tutto e funzionano da qualunque stato: una hotkey fa sempre e solo quello che dice la sua configurazione, che è l'unico comportamento prevedibile per chi la preme senza guardare. Se stavi digitando una quantità su un altro counter, quel buffer viene perso.

Sulle hotkey il `preventDefault()` è necessario, non cosmetico: senza, `F1` apre la guida del browser e `Space` fa scorrere la pagina.

Una hotkey che punta a un counter disattivato **non viene nemmeno mandata alla pagina**: non avrebbe una card da far lampeggiare e la scrittura tornerebbe `409`, quindi il tasto sembrerebbe rotto.

Con il **buffer vuoto**, `add_key` e `subtract_key` applicano **quantità 1** — il caso più frequente al banco è "uno in più" — e la card lampeggia per rendere evidente l'operazione. `set_key` con buffer vuoto viene invece ignorato: un valore da impostare non è mai implicito, e per azzerare basta digitare `0`.

I tasti sono confrontati su `event.key`, quindi tastierino numerico e riga superiore funzionano allo stesso modo, e il confronto è case-insensitive. Dopo la POST il valore è aggiornato subito dalla risposta, senza aspettare il polling successivo.

---

## 7. Admin

Oltre alla configurazione, due azioni:

**Reset dei counter selezionati** (su Counter) — pagina intermedia che chiede il nuovo valore e una **nota obbligatoria**, poi crea una transazione `set` per ogni counter con `source=admin` e la nota allegata. È il modo previsto per azzerare a fine turno lasciando traccia del perché.

**Annulla le transazioni selezionate** (su Transaction) — crea una transazione **compensativa**, non cancella l'originale, con nota automatica `undo #<id>`. Per `add` e `subtract` applica l'operazione inversa. Per un `set` ripristina `value_before`, che è esatto solo se nel frattempo nessun altro ha toccato il counter: la nota rende la cosa tracciabile.

---

## 8. Struttura

```
src/
├── manage.py
├── core/                       # settings, urls, wsgi
└── counters/
    ├── models.py               # BaseModel, Tag, Counter, Transaction, Display, DisplayCounter, ApiToken
    ├── operations.py           # apply_operation() / apply_batch(): l'unico punto che modifica un valore
    ├── auth.py                 # risoluzione token, regole CSRF, header delle risposte
    ├── views_api.py
    ├── views_display.py
    ├── urls.py
    ├── admin.py
    ├── tests.py
    ├── templates/counters/     # display.html, index.html, login.html
    └── static/counters/        # alpine.min.js, display.js, display.css, pages.css
```

Test: `cd src && uv run python manage.py test counters`.

---

## 9. Da fare prima di esporre l'app su internet

- **Filtro sui log** per oscurare il parametro `token`, altrimenti ogni chiave finisce in chiaro negli access log.
- **Rate limit per token** (es. 60 operazioni/minuto): limita il danno di un token trapelato o di uno script impazzito.
- **Cache condivisa** (Redis o database) se si gira con più worker: il throttle di `last_used_at` usa la cache locale del processo, che con più worker moltiplica le scritture senza rompere nulla.
- Valutare un `?nonce=<uuid>` con deduplica server-side, se emergono incrementi fantasma da prefetch del browser o unfurl dei link.

---

## 10. Limiti noti

- **Nessuno scope sui token**: qualunque token valido può muovere qualunque counter.
- **Istanza singola condivisa**: niente multi-tenant, chi vede un display li vede tutti. Separare gruppi di utenti richiederebbe di toccare il modello dati, meglio prima di avere dati in produzione.
- **Identità sul display**: le operazioni da tastiera sono attribuite all'utente loggato sul kiosk, che è condiviso da tutto il turno. Il campo `user` di quelle transazioni identifica il display, non la persona.
- **Cancellare un counter** dall'admin ne distrugge lo storico (`CASCADE` sulle transazioni). Per togliere di mezzo un counter si usa `is_active=False`.
