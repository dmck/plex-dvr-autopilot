# plex-dvr-autopilot

An autopilot for Plex DVR: auto-record high-rated upcoming OTA movies and new TV series premieres.

Two small Python scripts talk to your Plex Media Server, read the Live TV guide, and either print a digest or create DVR subscriptions. Default is notify-only. Nothing is scheduled until you pass `--execute`.

## Features

- **Movies** (`plex-record-movies.py`): finds upcoming OTA movies that are not already in your library or on the DVR schedule, scores them from the Plex guide (and optionally OMDb), and can create one-shot recordings.
- **Series** (`plex-catch-series.py`): watches the guide for new-series premieres on channels you list, skips news, shopping, kids blocks, and reruns, and can create a season pass. Adds are capped per run and per rolling week.
- Dry-run by default, with a concise digest on stdout (easy to email from cron or launchd).
- Discovers the Plex server if `PLEX_URL` is wrong, as long as the token is valid.
- Stdlib only — no pip packages.

## Requirements

- [Plex Pass](https://www.plex.tv/plex-pass/) with a working [DVR tuner](https://support.plex.tv/articles/225877427-supported-dvr-tuners-and-antennas/) and Live TV guide
- Python 3.8 or later (3.10+ recommended), on a machine that can reach PMS
- A [Plex authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- Optional: an [OMDb API key](https://www.omdbapi.com/apikey.aspx) for IMDb scores and series metadata when the guide has none (see the [OMDb terms](https://www.omdbapi.com/legal.htm); the free key is limited to 1,000 requests per day)

## Install

```bash
git clone <your-repo-url> plex-dvr-autopilot
cd plex-dvr-autopilot
cp secrets.env.example secrets.env
chmod 600 secrets.env
```

Edit `secrets.env`: set `PLEX_TOKEN`, and set `SERIES_MAJORS` to the HD call signs from your Plex Live TV guide if you want the series job. `PLEX_URL` can stay at `http://127.0.0.1:32400` if the scripts run on the same machine as PMS.

No `pip install` is required.

## Configuration

Values in `secrets.env` are loaded if the same name is not already in the environment. CLI flags override the matching movie or series defaults.

| Variable | Default | Used by | Purpose |
| --- | --- | --- | --- |
| `PLEX_TOKEN` | *(required)* | both | Plex auth token |
| `PLEX_URL` | `http://127.0.0.1:32400` | both | PMS base URL |
| `OMDB_KEY` or `OMDB_API_KEY` | empty | both | Optional OMDb key |
| `MIN_IMDB` | `7.0` | movies | Minimum IMDb score to record |
| `ALWAYS_IMDB` | `8.0` | movies | Record regardless of box office |
| `MAX_PER_RUN` | `25` | movies | Cap on recordings per run |
| `MIN_RUNTIME_MIN` | `80` | movies | Skip shorter titles |
| `MIN_YEAR` | `1970` | movies | Skip older titles |
| `MIN_BOX_OFFICE` | `50000000` | movies | Box-office floor unless score ≥ `ALWAYS_IMDB` |
| `HD_ONLY` | `1` | movies (and series fallback) | Require HD |
| `MOVIES_SECTION_ID` | auto-detect | movies | Plex movie library section |
| `EPG_MOVIES_KEY` | auto-detect | movies | Guide path for upcoming movies |
| `TV_SECTION_ID` | auto-detect | series | Plex TV library section |
| `EPG_SHOWS_KEY` | auto-detect | series | Guide path for upcoming episodes |
| `SERIES_MAJORS` | *(required for series)* | series | HD call signs, comma-separated; optional `:rank` (lower is preferred) |
| `SERIES_HD_ONLY` | `HD_ONLY` | series | Require HD |
| `SERIES_MAX_PER_RUN` | `8` | series | Cap on new passes per run |
| `SERIES_MAX_PER_WEEK` | `8` | series | Cap on new passes per rolling 7 days |
| `SERIES_MIN_RUNTIME_MIN` | `20` | series | Skip shorter airings |
| `SERIES_MAX_RUNTIME_MIN` | `130` | series | Upper bound (allows a 2-hour premiere slot) |
| `SERIES_HORIZON_DAYS` | `14` | series | How far ahead to scan the guide |

## Usage

```bash
# Preview only — no recordings are created
python3 plex-record-movies.py
python3 plex-catch-series.py

python3 plex-record-movies.py --verbose
python3 plex-catch-series.py --verbose

# Create DVR one-shots / season passes
python3 plex-record-movies.py --execute
python3 plex-catch-series.py --execute

# One-off lookups
python3 plex-record-movies.py --debug-rating "Some Title"
python3 plex-catch-series.py --debug-title "Some Show"
```

Movie flags: `--min-imdb`, `--always-imdb`, `--max`, `--min-runtime`, `--min-year`. Series flags: `--max`, `--max-week`, `--min-runtime`, `--max-runtime`, `--horizon-days`.

Confirm a dry-run digest looks right before using `--execute`.

## Scheduling

**macOS launchd:** copy the examples, edit the Python and working-directory paths, then load them.

```bash
cp examples/launchd/com.example.plex-dvr-autopilot.movies.plist.example ~/Library/LaunchAgents/com.example.plex-dvr-autopilot.movies.plist
cp examples/launchd/com.example.plex-dvr-autopilot.series.plist.example ~/Library/LaunchAgents/com.example.plex-dvr-autopilot.series.plist
# edit the paths, then:
launchctl load ~/Library/LaunchAgents/com.example.plex-dvr-autopilot.movies.plist
launchctl load ~/Library/LaunchAgents/com.example.plex-dvr-autopilot.series.plist
```

The examples run at 06:00 and 06:15 local time. Point `StandardOutPath` at a log, or wrap the job so stdout is emailed.

**cron** (Linux/macOS):

```cron
0 6 * * * cd /path/to/plex-dvr-autopilot && /usr/bin/python3 plex-record-movies.py --execute
15 6 * * * cd /path/to/plex-dvr-autopilot && /usr/bin/python3 plex-catch-series.py --execute
```

**Windows Task Scheduler:** create a daily task that starts in the repo folder and runs `python plex-record-movies.py --execute` (and a second task for the series script).

## How it works

Both scripts authenticate to PMS with `X-Plex-Token` and read the Live TV EPG plus your library and existing DVR subscriptions.

The movie job keeps titles that clear the IMDb / box-office bar, then `--execute` POSTs a one-shot to `/media/subscriptions` using the same airing template as Plex's Record button, and runs `/media/subscriptions/process`. It also tries to repair one-shots that were saved without airings.

The series job keeps first-run S01E01 / "series premiere" airings on `SERIES_MAJORS` channels, then `--execute` creates a season pass (not a one-shot). Call signs earlier in the list, or with a lower `:rank`, win when the same show airs on more than one channel.

## Troubleshooting

- `ERROR: set PLEX_TOKEN` — copy `secrets.env.example` to `secrets.env` and add a token ([how to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)).
- `cannot reach Plex Media Server` — set `PLEX_URL` to the machine that runs PMS, including port `32400`. The script will try plex.tv discovery if the configured URL is down.
- `no movie library section` / `no TV library section` — set `MOVIES_SECTION_ID` or `TV_SECTION_ID`.
- `could not find EPG … key` — set `EPG_MOVIES_KEY` or `EPG_SHOWS_KEY` from your Live TV provider path.
- `ERROR: set SERIES_MAJORS` — fill in HD call signs exactly as they appear in the Plex guide.
- Dry-run looked right but nothing recorded — add `--execute`.
- Ratings are missing — add `OMDB_KEY`. The movie job caches results in `ratings-cache.json` (gitignored).

## License

MIT — see [LICENSE](LICENSE).
