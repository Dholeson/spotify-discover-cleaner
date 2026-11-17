# How Discover Cleaner is built

One Python process (Flask + a few daemon threads) and a static, dependency-free ES-module
interface. Everything runs on `127.0.0.1`; state lives in SQLite and two small files.

```
browser (static/js)  ──/api/status every 3 s, /api/v2/*──▶  Flask (app.py + src/web/*)
                                                               │
             poll loop (app.py) ──▶ clearing engine ──▶ Spotify guard ──▶ Spotify Web API
             backlog index worker ─────────────────────▶ Spotify guard
             Last.fm importer ──────────────────────────────────────────▶ Last.fm API
                                                               │
                                       SQLite: events ─▶ plays ledger ─▶ reports, artist pages
```

## Layout

| Path | What lives there |
|---|---|
| `app.py` | The server. Configuration (`.env`, data folder, `--demo`/`--port`), Spotify sign-in, the **Spotify guard** (`GuardedSpotify`), the playback **poll loop**, Last.fm helpers, settings and the original `/api/*` routes, `main()`. Modules under `src/` reach it lazily with `import app as core` inside functions, so they always see the live module. |
| `src/persistence/` | SQLite. `db.py`: data folder, per-thread connections, schema and versioned migrations (with a full backup before any data-changing step). `events.py`: the append-only event log. `plays.py`: the **plays ledger**. `migrate.py`: one-time import of the old JSON history. `research.py`: the watchlist. `sessions.py`: listening sessions. |
| `src/services/` | Logic with no HTTP in it. `backlog_index.py`: which tracks are in the backlog playlist. `clearing.py`: listening accounting, the fate of the current track, removals, undo. `reports.py`, `artist_stats.py`, `backlog_insights.py`: everything Insights and artist pages show. `discography.py`: artist catalogs. `importers.py`: the Last.fm history import and sync. |
| `src/web/` | One Flask blueprint per area, all under `/api/v2`: `now_api`, `backlog_api`, `insights_api`, `artist_api`, `library_api`, `data_api`, `setup_api`. Registered automatically by `src/web/__init__.py`. |
| `src/devtools/` | `fake_spotify.py`: an offline stand-in for spotipy (simulated playback over a backlog playlist). `demo.py`: the `--demo` mode built on it. |
| `templates/index.html`, `static/` | The interface. `static/js/core/` (API client, status store, hash router, safe HTML templating, charts, icons), `static/js/components/` (player bar, search, status pill, dialogs, settings sections), `static/js/views/` (one module per page: now, insights, artist, artists, library, settings, setup). CSS: `static/css/tokens.css` (design tokens), `base.css`, `charts.css`, `views/<view>.css`. `templates/classic.html` + `static/classic/` are the old interface. |
| `demo/snapshot/`, `dev/make_snapshot.py` | The offline demo's data and the script that builds it. |
| `tests/` | stdlib `unittest`. |

### Data files

All in the **data folder**: the app folder by default, or `DISCOVER_DATA_DIR`.

- `music_explorer.db`: SQLite (WAL). The event log, the plays ledger and derived stats, the backlog
  index, pending removals, the watchlist, the Spotify guard's persisted lockouts. Upgrade backups
  sit next to it as `music_explorer.db.bak-v<n>`.
- `.config.json`: app state (backlog playlist, threshold, favorite artists, counters, caches).
  Every write is atomic (temp file + fsync + rename) and the previous version is kept as
  `.config.json.bak`; if the file can't be parsed, saving stops instead of overwriting history.
- `.spotify_cache`: the Spotify sign-in (spotipy's token cache).

`.env` (credentials and settings) always stays next to `app.py`. The offline demo uses
`.demo-data/` for all of these, including its own `.env`.

## The poll loop

`poll_loop()` asks Spotify what's playing every `POLL_INTERVAL` seconds (3 s; 10 s while paused).
Each answer goes to the clearing engine's `PlayTracker`, updates the shared `state` that
`/api/status` returns, records finished plays as `SONG_PLAYED` events, and kicks off enrichment
(artist info, lyrics) in the background. It idles while no one is signed in and makes no calls
while the guard has Spotify locked.

The browser polls `GET /api/status` (`static/js/core/store.js`) and every view reacts to changes.
`/api/status` is assembled from `state` plus **status extensions** that blueprints register
(`backlog`, `auto_clean`, `today`, `setup`, ...); each must be cheap and never raise.

## The plays ledger

`events` keeps everything that happened: Last.fm scrobbles, live plays from the poll loop, clears,
likes, watchlist changes. Reports never read it directly. `plays` (`src/persistence/plays.py`) is
the derived, query-friendly view, **one canonical row per listen**:

- Last.fm scrobbles are canonical. A live play of the same listen is merged into its scrobble
  (the Spotify id, seconds heard and backlog flag are copied over) instead of counting twice, in
  either arrival order; a later scrobble supersedes a provisional live play (`superseded_by`).
- A live play under 30 s with no scrobble is not a play (`plays_skipped` says why).
- `ts_ms` is the play's start; local day, hour, weekday and month come from the OS time zone.
- Offline scrobble batches (runs of 5+ plays less than 10 s apart) are flagged `ts_quality='bulk'`.
- Canonical plays are `SELECT ... FROM plays WHERE superseded_by IS NULL`.

New events are folded in incrementally. Changing a normalization rule means bumping
`RULES_VERSION`: a background worker then rebuilds the whole ledger (about 3 s for 50,000 events)
and `ensure_plays_ready()` reports False meanwhile, so pages show "getting your history ready"
instead of half-built numbers. `artist_stats`, `track_stats` and `album_stats` are rebuilt with it;
"hide from stats" flags are also kept in `app_meta`, so they survive a rebuild.

## The backlog index and the clearing engine

Auto-clear has to answer, on every poll, "is the track playing now in the backlog playlist?" for
playlists well over 1,000 tracks. `src/services/backlog_index.py` owns that answer:

- One background worker pages the playlist resumably and swaps the finished result in atomically,
  only after the last page arrived. A guard block sleeps and retries the same page.
- It never adopts the snapshot id Spotify returns for the app's own removals; it marks itself dirty
  and re-validates with a cheap metadata check (every 60 s, or early when an unknown track plays
  from the backlog's context).
- The committed index is persisted to SQLite, so a restart is usable at once (as "provisional"
  until Spotify confirms it).

`src/services/clearing.py` does the rest. Its safety rules:

1. **Nothing is removed while membership is unknown.** `contains()` answers True, False, or None
   (still loading); None means fate "syncing" and no removal. A persisted index that Spotify hasn't
   confirmed this session doesn't count for automatic removals. With no backlog playlist chosen,
   nothing is in the backlog.
2. **Listening time is real.** `PlayTracker` credits time from the monotonic clock, bounded by the
   progress Spotify reports, so slow polls neither under- nor over-credit. Pausing stops the count,
   seeking forward adds nothing, and a play ends after 2 minutes without playback.
3. **Once per play.** A track clears at most once per play, after `min(threshold, 80% of the track)`.
   Membership is judged when the play starts: a track added to the backlog while playing is "kept"
   and clears next time.
4. **The listener decides.** Keep, the Auto-clear switch and Undo always win. Undo puts the track
   back at its old position within 30 minutes.
5. **Failures are retried, then dropped.** A removal Spotify refuses goes to `pending_removals` and
   is retried with backoff; permanent errors (400/403/404), 50 failed attempts or 24 hours end it.
   Removals are batched (up to 100 ids per request) and recorded as events with the playlist id,
   so Insights → Backlog only counts the active playlist's clears.

## The Spotify guard

Every Spotify call goes through `GuardedSpotify` (in `app.py`), a wrapper around the spotipy
client. Spotify's limits are rolling and opaque, and a 429 can come with a lockout of hours, so the
guard is strict:

- **Pacing**: at least 350 ms between any two live calls, with per-lane spacing (realtime playback
  polls, interactive page requests, background syncs).
- **Budgets**: each method has a per-minute cap (`_SPOTIFY_GUARD_POLICIES`); past it, the call is
  refused locally instead of being sent.
- **Caching and single-flight**: read calls are cached with a fresh and a stale lifetime; a stale
  value is served while one background refresh runs, and identical concurrent calls share one
  request. Writes invalidate the affected caches.
- **429s**: the method, its category and all traffic are locked for `Retry-After`; non-essential
  calls stay off for at least 5 more minutes ("protect mode"). The lockout is persisted in
  `app_meta`, shown in the status pill on restart, and re-checked with one cheap call rather than
  trusted blindly.
- **403s**: "user not registered" and "Active premium subscription required for the owner of the
  app" pause all calls for a while and explain the fix in plain words. An endpoint Spotify removed
  for new apps (`GET /artists`, `/artists/{id}/top-tracks`, `/browse/*`, ...) is switched off for the
  session after its first 403/404 (`disable_spotify_feature`) and the app falls back (single
  `artist()` calls within budget, search, Last.fm).
- Responses are parsed in both the legacy and the 2026 shapes (`tracks`/`items`, `track`/`item`).
  spotipy 2.26+ is required: it calls `/playlists/{id}/items` and `/me/library*`.

`/api/dev/spotify-calls` lists the last calls with their outcome (ok, cached, stale, blocked).

## The v2 API

Everything the interface uses is under `/api/v2` (plus a few original routes such as
`/api/status`, `/api/settings`, `/api/set-playlist` and the playback controls).

- Success: HTTP 200 with the payload object. Failure: HTTP 4xx/5xx with
  `{"error": {"code": "snake_case", "message": "Human sentence."}}`, plus fields such as
  `retry_after` or `field`. `static/js/core/api.js` turns both into an `ApiError` with
  `status`, `code` and `message`.
- Blueprints live in `src/web/<area>_api.py`, export `bp`, and may export `init(core)` to register
  status extensions or background workers. They import nothing from `app.py` at module level.
- Request hardening applies to every route: only loopback `Host` headers are answered (blocks DNS
  rebinding), and every state-changing `/api/*` request must come from the app's own origin
  (`Origin`, else `Referer`). Bodies must be JSON objects. Secrets are never sent to the browser.
- The first-run Setup screen uses `GET/POST /api/v2/setup`; `POST` validates everything before it
  writes `.env` (atomically, keeping other lines) and applies the new credentials without a restart.

## The interface

`templates/index.html` is a shell: rail, top bar, `<main id="view">`, player bar. `static/js/main.js`
boots the UI kit, the status store, the player and the search, then lets the hash router
(`core/router.js`) mount one view module at a time (`views/<id>.js`, contract documented at the top
of `main.js`). All markup goes through the `html` tagged template (`core/ui-safe.js`), which escapes
every interpolated value. Charts are dependency-free SVG with a table twin. There is no build step
and no inline script (config is a JSON `<script>` block read by `boot.js`), so the interface works
under a strict Content-Security-Policy.

First run is part of the same flow: until Spotify credentials exist, `#/now` shows the Setup view;
once signed in without a backlog playlist, it shows the playlist picker step.

## Offline demo

`python app.py --demo` (or `DISCOVER_DEMO=1`) is decided at the very top of `app.py`, before any
path or setting is read: the data folder becomes `.demo-data/`, the app's `.env` is ignored, every
request to `*.spotify.com` is refused, and `src/devtools/demo.py` seeds the folder from
`demo/snapshot/` on first start (moving dates forward by whole weeks so the history ends this
week). After import, `get_sp()` returns `GuardedSpotify(FakeSpotify(...))`, so the real guard, poll
loop, clearing engine and interface run unchanged. Setup and credential changes are refused.

`dev/make_snapshot.py --source <data folder>` builds the snapshot from a copy of real data:
allowlisted tables and payload fields only, playlist ids replaced, notes dropped, gzip-compressed
and byte-reproducible; it refuses to write if a source playlist id, an email address or a local
path survived.

## Testing

```
.venv/Scripts/python.exe -m unittest discover -s tests     # Windows
.venv/bin/python -m unittest discover -s tests             # macOS / Linux
```

- Pure logic (ledger rules, clearing, index, reports, guard policies) is tested directly on
  in-memory or temp databases.
- `tests/platform_harness.py` copies `app.py`, `src/` and `templates/` to a temp folder with a fake
  `.env`, blocks all network access and imports that copy once per run; config IO, request security,
  settings and setup tests use it, so they can never touch real files.
- Tests that depend on import-time decisions (no credentials, `DISCOVER_DATA_DIR`, demo mode) start
  a separate Python process on their own temp copy.
- `FakeSpotify` (both the legacy and the `FAKE_API_MODE=2026` API) backs the integration tests and
  the demo; `test_frontend_charts.py` statically pins the chart module's API and colors.
