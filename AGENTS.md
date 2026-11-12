# Agent notes

Guidance for AI coding agents working in this repo (`plex-dvr-autopilot`).

## Layout

- `plex-record-movies.py` — movie one-shots, shared Plex/OMDb helpers (`load_env`, `cfg`, `Plex`, `safe_exc`, …).
- `plex-catch-series.py` — new-series season passes. Loads the movie script with `importlib` (`prm = _movies()`); do not rename or split the movie file without updating that loader.
- `secrets.env.example` — template. Real secrets live in `secrets.env` (gitignored).
- `examples/launchd/` — placeholder launchd plists, not live job definitions.
- `ratings-cache.json`, `series-cache.json`, `series-added.json` — created at runtime, gitignored. Do not commit them.

There are no third-party Python dependencies (`requirements.txt` is a note, not a pin list).

## Running locally / dry-run

Always dry-run first. Neither script schedules recordings unless `--execute` is passed.

```bash
cp -n secrets.env.example secrets.env   # then fill PLEX_TOKEN (and SERIES_MAJORS for the series job)
python3 plex-record-movies.py
python3 plex-catch-series.py
python3 plex-record-movies.py --verbose
python3 plex-catch-series.py --verbose
python3 plex-record-movies.py --debug-rating "Some Title"
python3 plex-catch-series.py --debug-title "Some Show"
```

Use a working `PLEX_TOKEN` and a reachable `PLEX_URL`. Do not point a test `--execute` run at a production DVR unless the user asked for that.

## Secrets

- Never commit `secrets.env`, caches, logs, or `__pycache__`.
- Never hardcode tokens, API keys, LAN IPs, hostnames, home paths, or personal library names. Configuration goes in the environment / `secrets.env`.
- Never log request URLs or exception text that may contain `X-Plex-Token` or an OMDb `apikey`. Use `safe_exc()` (and `prm.safe_exc()` in the series script) when printing errors. If you add logging, redact those query parameters.
- The `Plex.request` helper currently also puts the token in the query string. Leave that as-is unless you are deliberately changing auth and have tested it; still never print that URL.

## Code style

- Python 3.8-compatible (the files already use `from __future__ import annotations`).
- Stdlib only unless the user agrees to add a dependency.
- Keep docstrings and comments free of personal machine names, usernames, and local call signs.
- Prefer small, obvious helpers consistent with the existing style.

## Testing changes safely

- Prefer unit-style checks you can run without PMS (parsing, ranking, `safe_exc`, skip-reason helpers).
- `python3 -m py_compile plex-record-movies.py plex-catch-series.py`
- Against a live server: dry-run only, then inspect the digest. Do not pass `--execute` unless the user wants real subscriptions created.
- `--debug-rating` / `--debug-title` are safe; they do not create subscriptions.
- Do not delete or rewrite existing DVR subscriptions in exploratory tests.

## Do not change (unless the user asks and you re-test against Plex)

- The movie Record flow: the raw template query must be passed through unchanged (`params[airingTimes]` / `params[airingChannels]`). Rebuilding it drops airings. Replacing an empty subscription requires DELETE, not PUT.
- `/media/subscriptions/process` after a successful create.
- TV season-pass prefs: extra DVR prefs such as `onlyNewAirings` and `minVideoQuality` have been observed to 400 on TV subscriptions.
- Default-is-dry-run behavior of `--execute`.
- Cache file names and the `secrets.env` loading pattern (`os.environ.setdefault` so a real environment wins).

## Release hygiene

If you are preparing a public copy, grep the tree for tokens, private IPs, hostnames, `/Users/` home paths, and personal names. Keep launchd examples under `examples/` with `com.example.plex-dvr-autopilot.*` labels and placeholder paths. License is MIT, see LICENSE. Do not copy a live `secrets.env`.
