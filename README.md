# Discover Cleaner

A local companion for Spotify + Last.fm that does three things:

1. **Clears your "to listen" backlog as you listen.** Point it at a playlist (for example the one your
   Discover Weekly finds pile up in). When a track from that playlist has actually played for a few
   seconds, it is removed from the playlist, so the backlog shrinks by itself. You always see what will
   happen to the current track and when, and you can **keep** it, **clear it now**, **like** it, or
   **undo** a clear.
2. **Shows you your listening like Last.fm Pro.** Reports for any week, month, year or all time, built
   from your full Last.fm history plus what the app hears: top artists, albums and tracks with change
   against the previous period, how much was new, your listening clock and weekly rhythm, streaks,
   discoveries that stuck, obsessions, artists to rediscover, and a "year in music" page.
3. **Gives every artist a personal page.** Your plays, rank, first and last listen, plays per year, your
   top tracks and albums, what you haven't heard yet, how much of their discography you've explored, and
   similar artists with your own play counts next to them.

Everything runs on your computer at <http://127.0.0.1:8888>. Your history lives in a local SQLite file.

## Setup (once, about 5 minutes)

1. **Spotify API credentials.** Go to <https://developer.spotify.com/dashboard>, create an app (any name),
   and set its **Redirect URI** to exactly `http://127.0.0.1:8888/callback`. Copy the Client ID and
   Client Secret.
2. **Last.fm (optional, recommended).** Get a free API key at <https://www.last.fm/api/account/create>.
   With a key and your username, your whole scrobble history is imported and kept in sync every 30 minutes.
3. **Python 3.10+** (`python --version`).
4. Copy `.env.example` to `.env` and fill in the Spotify values (and Last.fm, if you have it). Every
   setting is explained in that file; most can be changed later in the app under **Settings**.

## Running

**Windows:** double-click `run.bat`. The first run creates a virtual environment and installs the three
dependencies (Flask, spotipy, python-dotenv).

**Other systems:**

```
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:8888> and connect Spotify when asked. The login is cached in `.spotify_cache`.

On the first start after an upgrade the app updates its database automatically. Before each upgrade
step it saves a full copy next to it (`music_explorer.db.bak-v2`, ...), and it never deletes those
copies on its own. It then builds its listening ledger in the background (a few seconds) and starts
the Last.fm sync; until the ledger is ready, Insights and artist pages show a short "getting your
history ready" note.

## Using it

### Now
What's playing and what happens to it. The panel under the track says one of:

| You see | Meaning |
|---|---|
| **Clears from "…" in 7 s** | The track is in your backlog and will be removed after the listening threshold. |
| **Cleared · Undo** | Removed. Undo puts it back at its old position (for 30 minutes). |
| **Keeping this one** | You pressed Keep: it stays in the backlog this time. |
| **Auto-clear is paused** | The Auto-clear switch is off; nothing is removed automatically. |
| **Not in your backlog** | Nothing will be removed. |
| **Checking your backlog** | The app is still reading the playlist; nothing is removed until it knows for sure. |
| **Couldn't clear yet** | Spotify was busy; the removal is retried automatically. |

Buttons: **Like** (saves it to Liked Songs and clears it), **Keep in backlog**, **Clear now** (clears and
skips to the next track), **Next**. Also on this page: today's numbers, the backlog size with how fast it
is growing or shrinking and when it would be empty at your pace, recently cleared tracks (with undo), a
"catch up" helper for backlog tracks you already heard elsewhere, synced lyrics, and a short "you and this
artist" card.

**How clearing works:** a track is cleared once per play, after it has really played for the threshold
(default 15–30 s, adjustable on the Now page and in Settings; never more than 80% of a short track).
Pausing stops the count, seeking forward doesn't add time, and only tracks the app has confirmed are in the
playlist are ever removed. Clearing happens whenever the app is running and **Auto-clear** is on — use the
switch to pause it. (Older versions only cleared while the browser tab was focused; that rule silently
blocked clears and is gone.)

### Insights
- **Reports** — pick Week, Month, Year or All time and step back through time with the arrows. Plays,
  listening time, artists, albums and tracks (with how many were new to you), top lists with change
  against the previous period (**NEW** = first time ever, **BACK** = returning after a break), your
  listening clock, weekday rhythm, quick facts and the artists you discovered. For a period that is still
  running, the comparison uses the same number of days of the previous period.
- **All time** — five years at a glance: plays per year, streaks, how concentrated your taste is,
  discoveries and how many stuck, obsessions, and artists you loved but haven't played in a while.
- **Year in music** — a summary card for any year.
- **Backlog** — how the backlog grew and shrank, how many tracks come in and go out per week, how much of
  what you clear you actually listen to past the threshold, and which genres land for you.

Every number has an ⓘ explaining exactly what it counts. If your Last.fm history is behind, a banner says
so instead of showing a fake drop. Podcasts or other non-music "artists" can be hidden from all stats from
their artist page (**Hide from stats**).

### Artists
Search or browse all your artists (`#/artists`), or click any artist name anywhere. The artist page shows
your history first (it's instant), then fills in Spotify and Last.fm details.

### Library
Recent plays (back to the beginning of your history), the backlog itself (searchable, with clear/undo),
Liked Songs, favorite artists with a new-release radar, and a **Watchlist** of tracks, artists and albums
you want to come back to (with reasons and notes).

### Settings
Which playlist is the backlog, auto-clear and threshold, Spotify and Last.fm accounts, history sync and data
health (how current your history is, backups), connection status and diagnostics, and advanced settings.
Secrets are never shown back to the browser.

### Keyboard shortcuts
`space` play/pause · `n` / `p` next/previous · `l` like · `k` keep · `c` clear now · `u` undo last clear ·
`/` search · `g` then `n` `i` `a` `l` `s` to jump to Now, Insights, Artists, Library, Settings · `?` shows all shortcuts.

### The old interface
The previous single-page interface is still available at <http://127.0.0.1:8888/classic>, and the
pop-out mini player at `/widget` (also reachable from Settings).

## Troubleshooting

- **The status pill says "Rate limited"** — Spotify asked the app to slow down. The pill shows how long,
  and what still works meanwhile (insights, history, artist pages from local data, lyrics). The app waits it
  out on its own.
- **"INVALID_CLIENT: Invalid redirect URI"** — the Redirect URI in your Spotify dashboard must be exactly
  `http://127.0.0.1:8888/callback` (http, no trailing slash).
- **Nothing is being cleared** — check the panel under the track: it tells you why (not in the backlog,
  auto-clear paused, still checking the playlist, Spotify busy). The playlist must be yours or collaborative.
- **Insights stop at some date** — open Settings → History & data and press **Sync now**.
- **"insufficient client scope"** — delete `.spotify_cache` and connect again.

## Files

```
app.py                 server: Spotify connection + rate-limit guard, playback polling, legacy routes
src/persistence/       SQLite: schema + migrations (db.py), event log, the listening ledger (plays.py)
src/services/          backlog index + clearing engine, reports, artist stats, discography, importers
src/web/               the /api/v2 endpoints used by the interface
templates/, static/    the interface (index.html + ES modules in static/js, styles in static/css)
                       and the old interface (classic.html, static/classic/)
tests/                 unit tests: .venv/Scripts/python.exe -m unittest discover -s tests
music_explorer.db      your history (plus .bak-v* upgrade copies)
.config.json           app state (threshold, playlist, favorites, recent activity)
.env                   your settings and credentials (keep private)
```
