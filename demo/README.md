# Demo data

`snapshot/` is the data behind the offline demo (`python app.py --demo`, `run.bat --demo`,
`./run.sh --demo`). It is the author's real listening history, frozen on **2026-10-04**: about
51,000 Last.fm scrobbles going back to 2021, plus the plays, clears and likes Discover Cleaner
recorded itself, and the ~500 tracks it saw play (the demo's simulated backlog playlist is made
of those).

Account details are not in it: no Spotify credentials or tokens, no playlist ids or names (the
demo's playlist is "Discover Backlog (demo)"), no notes. `dev/make_snapshot.py` builds it and
documents exactly which tables and fields are kept; it refuses to write a snapshot that still
contains a source playlist id, an email address or a local file path.

On first start the demo copies the snapshot into `.demo-data/` (ignored by git) and moves every
date forward by whole weeks, so the history ends in the current week and weekdays stay the same.
Backlog sizes are offset so the last one matches the simulated playlist; day-to-day changes are
unchanged. `--demo --reset` starts over from the snapshot.

Files: `meta.json` (date, counts), `config.json.gz` (app settings and caches), and one
`<table>.jsonl.gz` per database table: a header line `{"table", "columns"}`, then one JSON array
per row.
