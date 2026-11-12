#!/usr/bin/env python3
"""Catch new-series premieres on major OTA channels and optionally add a season pass.

A premiere of a 1-hour show may be a 2-hour slot. Default is notify-only; --execute
creates a New-Airings-Only season pass (not a one-shot). Added series are printed
in a prominent banner so they cannot be missed in the digest email.

  python3 plex-catch-series.py
  python3 plex-catch-series.py --execute
  python3 plex-catch-series.py --verbose
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

DIR = Path(__file__).resolve().parent
CACHE_PATH = DIR / "series-cache.json"
ADDED_PATH = DIR / "series-added.json"

def parse_majors(raw: str) -> tuple[set[str], dict[str, int]]:
    """Parse SERIES_MAJORS: comma-separated call signs, optional :rank (lower = preferred).

    Example: WNBCDT,WABCDT,WCBSDT,WFOXDT,WCWDT:1,WPBSDT:2
    """
    majors: set[str] = set()
    ranks: dict[str, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            call, _, rank_s = part.partition(":")
            call = call.strip().upper()
            try:
                rank = int(rank_s.strip())
            except ValueError:
                rank = 0
        else:
            call = part.upper()
            rank = 0
        if call:
            majors.add(call)
            ranks[call] = rank
    return majors, ranks
SKIP_GENRES = {
    "news",
    "sports",
    "shopping",
    "talk",
    "talk show",
    "weather",
    "paid programming",
    "religion",
    "children",
    "kids",
}
SKIP_TITLE = re.compile(
    r"(paid programming|infomercial|news at |news \d|eyewitness news|good day |"
    r"good morning|tonight show|late show|late night|nightline|world news|"
    r"cbs news|nbc news|abc news|local news|weather|meet the press|"
    r"face the nation|this week with|nightly news|early today|first look|"
    r"roundup|infomercial|bath remodel|safer shower)",
    re.I,
)
ONE_OFF = re.compile(
    r"(tv movie|television movie|one-night|two-night|one night only|"
    r"two-night event|television event|movie event)",
    re.I,
)
PILOT_TITLE = re.compile(r"^(pilot|series premiere)\b|\bseries premiere\b", re.I)
KIDS_RATING = {"TV-Y", "TV-Y7"}
YEAR_RE = re.compile(r"(19|20)\d{2}")
SKIP_LABELS = {
    "channel": "off-network / subchannel",
    "sd": "not HD",
    "news": "news/talk/shopping",
    "kids": "kids",
    "notpremiere": "not S01E01 / series premiere",
    "rerun": "not first-run",
    "runtime": "runtime out of range",
    "owned": "in library",
    "scheduled": "already have a pass",
    "old": "established series",
    "oneoff": "one-off / TV movie",
    "cap": "weekly cap",
}


def _movies():
    spec = importlib.util.spec_from_file_location("plex_record_movies", DIR / "plex-record-movies.py")
    if spec is None or spec.loader is None:
        raise SystemExit("ERROR: cannot load plex-record-movies.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prm = _movies()


def load_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tv_section_id(plex, forced: str) -> str:
    if forced:
        return forced
    for sec in plex.container("/library/sections"):
        if sec.get("type") == "show":
            return str(sec.get("key") or sec.get("id") or "")
    raise SystemExit("ERROR: no TV library section found")


def epg_episodes_key(plex, forced: str) -> str:
    if forced:
        return forced
    providers = plex.request("/media/providers")
    mc = providers.get("MediaContainer") or providers
    for prov in mc.get("MediaProvider") or []:
        ident = str(prov.get("identifier") or "")
        if "epg" not in ident.lower():
            continue
        for feat in prov.get("Feature") or []:
            if feat.get("type") != "content":
                continue
            for d in feat.get("Directory") or []:
                if d.get("type") == "show" and str(d.get("title") or "").lower() == "shows":
                    key = d.get("key") or ""
                    if key:
                        if not key.startswith("/"):
                            key = "/" + ident + "/" + key.lstrip("/")
                        return key.rstrip("/") + "/all"
        ident_path = ident if ident.startswith("/") else "/" + ident
        return f"{ident_path}/sections/2/all"
    raise SystemExit("ERROR: could not find EPG shows key; set EPG_SHOWS_KEY")


def library_show_titles(plex, section_id: str) -> set[str]:
    owned: set[str] = set()
    start = 0
    while True:
        data = plex.request(
            f"/library/sections/{section_id}/all",
            params={"type": 2, "X-Plex-Container-Start": start, "X-Plex-Container-Size": 200},
        )
        mc = data.get("MediaContainer") or {}
        items = prm._as_dicts(mc.get("Metadata") or [])
        for it in items:
            owned.add(prm.norm_title(it.get("title") or ""))
        start += len(items)
        if not items or start >= int(mc.get("totalSize") or start):
            break
    return owned


def subscription_show_titles(plex) -> set[str]:
    titles: set[str] = set()
    data = plex.request("/media/subscriptions")
    mc = data.get("MediaContainer") or data
    for sub in prm._as_dicts(mc.get("MediaSubscription") or mc.get("Metadata") or []):
        if str(sub.get("type")) not in {"2", "show"}:
            continue
        for d in prm._as_dicts(sub.get("Directory") or [sub]):
            t = d.get("title") or sub.get("title") or ""
            if t and t != "All Episodes":
                titles.add(prm.norm_title(t))
    return titles


def pick_media(item: dict, majors: set[str]) -> dict:
    medias = prm.media_list(item)
    for m in medias:
        if (m.get("channelCallSign") or "").upper() in majors:
            return m
    return medias[0] if medias else {}


def media_hd(media: dict) -> bool:
    res = str(media.get("videoResolution") or media.get("videoQuality") or "").lower()
    if res in {"720", "1080", "1440", "4k", "2160", "hd"}:
        return True
    if "hd" in res:
        return True
    try:
        if int(media.get("height") or 0) >= 720:
            return True
    except (TypeError, ValueError):
        pass
    return False


def genres(item: dict) -> list[str]:
    tags = []
    for g in prm._as_dicts(item.get("Genre") or []):
        tag = str(g.get("tag") or "").strip().lower()
        if tag:
            tags.append(tag)
    return tags


def show_title(item: dict) -> str:
    return (item.get("grandparentTitle") or item.get("title") or "").strip()


def is_premiere_episode(item: dict) -> bool:
    parent = item.get("parentIndex")
    index = item.get("index")
    title = item.get("title") or ""
    if parent == 1 and index == 1:
        return True
    if parent == 1 and index in (None, 0) and PILOT_TITLE.search(title):
        return True
    if PILOT_TITLE.search(title) and parent in (1, None):
        return True
    return False


def first_run(item: dict, media: dict, slack_days: int = 2) -> bool:
    oaa = item.get("originallyAvailableAt") or ""
    begins = int(media.get("beginsAt") or 0)
    if not oaa or not begins:
        return False
    try:
        od = datetime.fromisoformat(str(oaa).replace(" ", "T")).date()
    except ValueError:
        return False
    bd = datetime.fromtimestamp(begins).date()
    return abs((bd - od).days) <= slack_days


def parse_start_year(raw: str) -> int:
    m = YEAR_RE.search(raw or "")
    return int(m.group(0)) if m else 0


def omdb_series(title: str, year: str, key: str, cache: dict) -> dict:
    ck = f"{prm.norm_title(title)}|{year}|series"
    if ck in cache and isinstance(cache[ck], dict):
        return cache[ck]
    info: dict = {"found": False}
    if not key or not title:
        cache[ck] = info
        return info
    q = {"t": title, "apikey": key, "type": "series"}
    if year:
        q["y"] = year
    url = "https://www.omdbapi.com/?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        cache[ck] = info
        return info
    if str(data.get("Response")) != "True":
        q.pop("type", None)
        url = "https://www.omdbapi.com/?" + urllib.parse.urlencode(q)
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            cache[ck] = info
            return info
        if str(data.get("Response")) != "True":
            cache[ck] = info
            return info
    seasons_raw = data.get("totalSeasons") or ""
    try:
        seasons = int(seasons_raw)
    except (TypeError, ValueError):
        seasons = 0
    ep_count = 0
    if str(data.get("Type") or "").lower() == "series":
        sq = {"t": title, "apikey": key, "Season": "1"}
        try:
            with urllib.request.urlopen(
                "https://www.omdbapi.com/?" + urllib.parse.urlencode(sq), timeout=20
            ) as resp:
                season = json.loads(resp.read().decode("utf-8"))
            ep_count = len(season.get("Episodes") or [])
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            ep_count = 0
    info = {
        "found": True,
        "type": str(data.get("Type") or "").lower(),
        "year": str(data.get("Year") or ""),
        "plot": str(data.get("Plot") or ""),
        "seasons": seasons,
        "episodes": ep_count,
        "title": data.get("Title") or title,
    }
    cache[ck] = info
    return info


def one_off_or_old(info: dict, epg_year: int, now_year: int) -> str:
    """Return skip reason or empty string to keep."""
    if not info.get("found"):
        if epg_year and epg_year < now_year - 1:
            return "old"
        return ""
    blob = " ".join([str(info.get("plot") or ""), str(info.get("title") or "")])
    if ONE_OFF.search(blob):
        return "oneoff"
    typ = info.get("type") or ""
    if typ == "movie":
        return "oneoff"
    start = parse_start_year(str(info.get("year") or ""))
    # Same title, different work (2003 Line of Fire vs 2026 NBC reboot).
    if epg_year and start and abs(start - epg_year) > 1:
        return ""
    if start and start < now_year - 1:
        return "old"
    seasons = int(info.get("seasons") or 0)
    if seasons >= 2 and start and start < now_year:
        return "old"
    # Finished 1-season mini / two-nighter (Year is "2009", not "2026–").
    year_s = str(info.get("year") or "")
    ongoing = bool(re.search(r"[–-]\s*$", year_s))
    eps = int(info.get("episodes") or 0)
    if typ == "series" and seasons <= 1 and 0 < eps <= 2 and not ongoing and start and start < now_year:
        return "oneoff"
    return ""


def record_series(plex, item: dict, media: dict, section_id: str, hd_only: bool) -> str:
    """Compact season pass. Extra DVR prefs (onlyNewAirings, minVideoQuality, …) 400 on TV."""
    title = show_title(item)
    year = item.get("year") or ""
    guid = prm.unescape_plex_id(str(item.get("grandparentGuid") or item.get("guid") or ""))
    thumb = item.get("grandparentThumb") or item.get("thumb") or ""
    rating_key = prm.unescape_plex_id(str(item.get("grandparentRatingKey") or ""))
    params = {
        "type": "2",
        "hints[type]": "2",
        "hints[title]": title,
        "hints[guid]": guid,
        "params[libraryType]": "2",
        "prefs[oneShot]": "false",
        "targetLibrarySectionID": section_id,
        "includeGrabs": "1",
    }
    if year:
        params["hints[year]"] = str(year)
    if thumb:
        params["hints[thumb]"] = thumb
    if rating_key:
        params["hints[ratingKey]"] = rating_key
    try:
        tmpl = prm.template_params(plex, guid)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        tmpl = {}
    provider = tmpl.get("params[mediaProviderID]") or ""
    if provider:
        params["params[mediaProviderID]"] = str(provider)
    try:
        created = plex.request("/media/subscriptions", method="POST", params=params)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return "exists"
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {body[:300]}") from exc
    sub_id = ""
    mc = created.get("MediaContainer") or created
    for sub in prm._as_dicts(mc.get("MediaSubscription") or []):
        sub_id = str(sub.get("key") or "")
        break
    return f"ok:{sub_id}" if sub_id else "ok"


def _record_ok(status: str) -> bool:
    return status == "exists" or status == "ok" or str(status).startswith("ok")


def _newly_added(status: str) -> bool:
    return status == "ok" or str(status).startswith("ok:")


def format_show_block(item: dict, media: dict, extra: str = "") -> list[str]:
    title = show_title(item)
    year = str(item.get("year") or "")
    ep = (item.get("title") or "").strip()
    begins = int(media.get("beginsAt") or 0)
    day, clock = prm.day_and_clock(begins)
    ch = (media.get("channelCallSign") or prm.channel_name(item) or "—")[:12]
    net = media.get("channelTitle") or ""
    if "(" in net and net.endswith(")"):
        net = net[net.rfind("(") + 1 : -1]
    mins = prm.runtime_min(item) or int(media.get("duration") or 0) // 60000
    parent = item.get("parentIndex")
    index = item.get("index")
    se = f"S{parent}E{index}" if parent is not None and index is not None else (item.get("parentTitle") or "")
    dur_note = f"{mins}m"
    if 75 <= mins <= 130:
        dur_note += " premiere window"
    head = f"  {title}" + (f" ({year})" if year else "")
    when = f"  {day:<12} {clock:<8} {ch:<12} {net}"
    detail = f"  {se}  {ep}  ·  {dur_note}"
    lines = [head, when, detail]
    if extra:
        lines.append(f"  {extra}")
    return lines


def print_banner(heading: str, blocks: list[list[str]]) -> None:
    bar = "=" * 64
    print()
    print(bar)
    print(f">>> {heading}")
    print(bar)
    print()
    for i, block in enumerate(blocks):
        if i:
            print()
        for line in block:
            print(line)
    print()
    print(bar)
    print()


def iter_upcoming_episodes(plex, epg_path: str, extra: dict, horizon_ts: int, page: int = 400):
    start = 0
    while True:
        params = dict(extra)
        params["X-Plex-Container-Start"] = start
        params["X-Plex-Container-Size"] = page
        data = plex.request(epg_path, params=params)
        mc = data.get("MediaContainer") or {}
        items = prm._as_dicts(mc.get("Metadata") or [])
        if not items:
            break
        stop = False
        for it in items:
            begins = prm.begins_at(it)
            if begins and begins > horizon_ts:
                stop = True
                break
            yield it
        start += len(items)
        if stop or start >= int(mc.get("totalSize") or start):
            break


def added_this_week(state: dict, now: float, days: int = 7) -> int:
    cut = now - days * 86400
    n = 0
    for rec in state.values():
        if not isinstance(rec, dict):
            continue
        try:
            ts = float(rec.get("added_at") or 0)
        except (TypeError, ValueError):
            continue
        if ts >= cut:
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    secrets = prm.load_env(DIR / "secrets.env")
    for k, v in secrets.items():
        os.environ.setdefault(k, v)

    parser = argparse.ArgumentParser(description="Catch new series premieres on major OTA channels")
    parser.add_argument("--execute", action="store_true", help="Create season passes (New Airings Only)")
    parser.add_argument("--verbose", action="store_true", help="Print every skip")
    parser.add_argument("--max", type=int, default=int(prm.cfg("SERIES_MAX_PER_RUN", "8") or 8))
    parser.add_argument("--max-week", type=int, default=int(prm.cfg("SERIES_MAX_PER_WEEK", "8") or 8))
    parser.add_argument("--min-runtime", type=int, default=int(prm.cfg("SERIES_MIN_RUNTIME_MIN", "20") or 20))
    parser.add_argument(
        "--max-runtime",
        type=int,
        default=int(prm.cfg("SERIES_MAX_RUNTIME_MIN", "130") or 130),
        help="Upper bound in minutes; 2-hour premieres of 1-hour shows are allowed",
    )
    parser.add_argument("--horizon-days", type=int, default=int(prm.cfg("SERIES_HORIZON_DAYS", "14") or 14))
    parser.add_argument("--debug-title", metavar="TITLE", help="Explain why one title was kept/skipped")
    args = parser.parse_args(argv)

    token = prm.cfg("PLEX_TOKEN")
    if not token:
        print("ERROR: set PLEX_TOKEN in secrets.env", file=sys.stderr)
        return 1

    majors, network_rank = parse_majors(prm.cfg("SERIES_MAJORS"))
    if not majors:
        print(
            "ERROR: set SERIES_MAJORS in secrets.env to your local HD call signs "
            "(comma-separated, optional :rank, lower = preferred)",
            file=sys.stderr,
        )
        return 1
    hd_only = prm.cfg("SERIES_HD_ONLY", prm.cfg("HD_ONLY", "1")) not in {"0", "false", "no"}
    now_year = datetime.now().year
    now_ts = time.time()
    horizon_ts = int(now_ts) + args.horizon_days * 86400

    plex = prm.Plex(prm.discover_plex_url(token, prm.cfg("PLEX_URL", "http://127.0.0.1:32400")), token)
    section_id = tv_section_id(plex, prm.cfg("TV_SECTION_ID"))
    epg_key = epg_episodes_key(plex, prm.cfg("EPG_SHOWS_KEY"))
    extra = {"type": "4", "sort": "beginsAt"}
    if "?" in epg_key:
        epg_path, _, qs = epg_key.partition("?")
        extra.update(urllib.parse.parse_qsl(qs))
        extra.setdefault("type", "4")
        extra.setdefault("sort", "beginsAt")
    else:
        epg_path = epg_key

    owned = library_show_titles(plex, section_id)
    scheduled = subscription_show_titles(plex)
    cache = load_json(CACHE_PATH, {})
    added_state = load_json(ADDED_PATH, {})
    omdb_key = prm.cfg("OMDB_KEY") or prm.cfg("OMDB_API_KEY")
    week_used = added_this_week(added_state, now_ts)

    skip_counts: Counter = Counter()
    skips: list[str] = []
    picks: list[tuple] = []
    scanned = 0
    debug = (args.debug_title or "").strip().lower()

    for item in iter_upcoming_episodes(plex, epg_path, extra, horizon_ts):
        if item.get("type") not in (None, "episode", 4, "4"):
            continue
        scanned += 1
        title = show_title(item)
        if not title:
            continue
        if debug and debug not in title.lower() and debug not in (item.get("title") or "").lower():
            continue
        media = pick_media(item, majors)
        call = (media.get("channelCallSign") or "").upper()
        year = str(item.get("year") or "")
        try:
            epg_year = int(year) if year else 0
        except ValueError:
            epg_year = 0
        reason = ""
        detail = ""
        blob = " ".join([title, item.get("title") or "", item.get("summary") or ""])
        if call not in majors:
            reason, detail = "channel", f"[channel {call or '—'}] {title}"
        elif hd_only and not media_hd(media):
            reason, detail = "sd", f"[sd] {title}"
        elif SKIP_TITLE.search(blob) or any(g in SKIP_GENRES for g in genres(item)):
            reason, detail = "news", f"[news/talk] {title}"
        elif str(item.get("contentRating") or "") in KIDS_RATING:
            reason, detail = "kids", f"[kids] {title}"
        elif not is_premiere_episode(item):
            reason, detail = "notpremiere", f"[not premiere S{item.get('parentIndex')}E{item.get('index')}] {title}"
        elif not first_run(item, media):
            reason, detail = "rerun", f"[rerun OAA={item.get('originallyAvailableAt')}] {title}"
        else:
            mins = prm.runtime_min(item) or int(media.get("duration") or 0) // 60000
            if mins and (mins < args.min_runtime or mins > args.max_runtime):
                reason, detail = "runtime", f"[runtime {mins}m] {title}"
            elif prm.norm_title(title) in owned:
                reason, detail = "owned", f"[owned] {title}"
            elif prm.norm_title(title) in scheduled or prm.norm_title(title) in added_state:
                reason, detail = "scheduled", f"[already scheduled] {title}"
        if not reason:
            info = omdb_series(title, year if epg_year else "", omdb_key, cache)
            reason = one_off_or_old(info, epg_year, now_year)
            if reason:
                detail = f"[{reason} omdb={info.get('year') or 'n/a'} {info.get('type') or 'miss'}] {title}"
        if reason:
            skip_counts[reason] += 1
            if args.verbose or debug:
                skips.append(detail)
            continue
        begins = int(media.get("beginsAt") or prm.begins_at(item) or 0)
        picks.append((begins, title, item, media))

    # One row per show, earliest major-channel airing.
    best: dict[str, tuple] = {}
    for row in picks:
        key = prm.norm_title(row[1])
        prev = best.get(key)
        if prev is None or row[0] < prev[0]:
            best[key] = row
    def _rank(row: tuple) -> tuple:
        media = row[3]
        call = (media.get("channelCallSign") or "").upper()
        return (network_rank.get(call, 9), row[0] or 10**12)

    picks = sorted(best.values(), key=_rank)

    room = max(0, args.max_week - week_used)
    if args.max > 0:
        room = min(room, args.max)
    held_cap: list[tuple] = []
    to_record = picks
    if args.execute and len(picks) > room:
        held_cap = picks[room:]
        to_record = picks[:room]
        skip_counts["cap"] += len(held_cap)

    statuses: list[str] | None = None
    if args.execute:
        statuses = []
        for _begins, title, item, media in to_record:
            try:
                st = record_series(plex, item, media, section_id, hd_only)
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, RuntimeError) as exc:
                st = f"FAIL {prm.safe_exc(exc)}"
            statuses.append(st)
            if _newly_added(st):
                added_state[prm.norm_title(title)] = {
                    "title": title,
                    "added_at": int(now_ts),
                    "sub_id": st.split(":", 1)[-1] if ":" in st else "",
                    "channel": (media.get("channelCallSign") or ""),
                }
        if any(_record_ok(s) for s in statuses):
            try:
                plex.request("/media/subscriptions/process", method="POST")
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
                print(f"subscription process: {prm.safe_exc(exc)}", file=sys.stderr)
        save_json(ADDED_PATH, added_state)

    save_json(CACHE_PATH, cache)
    if args.verbose or debug:
        for line in skips:
            print(line, file=sys.stderr)

    added_blocks: list[list[str]] = []
    found_blocks: list[list[str]] = []
    if statuses:
        for i, (_begins, _title, item, media) in enumerate(to_record):
            st = statuses[i]
            if _newly_added(st):
                extra = f"season pass {st}"
                added_blocks.append(format_show_block(item, media, extra))
            else:
                found_blocks.append(format_show_block(item, media, st))
    else:
        for _begins, _title, item, media in picks:
            found_blocks.append(format_show_block(item, media))

    if added_blocks:
        print_banner("NEW SERIES ADDED", added_blocks)
    if found_blocks:
        heading = (
            "NEW SERIES FOUND (preview — not recording yet)"
            if not args.execute
            else "NEW SERIES (already had a pass / not added)"
        )
        print_banner(heading, found_blocks)
    if held_cap:
        blocks = [
            format_show_block(item, media, "held — run/week cap")
            for _b, _t, item, media in held_cap
        ]
        print_banner("NEW SERIES HELD (run/week cap)", blocks)

    n_found = len(picks)
    n_added = len(added_blocks)
    if args.execute and n_added:
        head = f"Plex DVR — added {n_added} new series season pass{'es' if n_added != 1 else ''}"
    elif n_found:
        head = f"Plex DVR — {n_found} new series premiere{'s' if n_found != 1 else ''} worth a season pass"
    else:
        head = "Plex DVR — no new series premieres"
    print(head)
    bits = [
        f"{scanned} airings scanned",
        f"cap {args.max}/run {args.max_week}/week",
        f"{week_used} added in last 7 days",
    ]
    for key, label in SKIP_LABELS.items():
        c = skip_counts.get(key, 0)
        if c:
            bits.append(f"{c} {label}")
    print(" · ".join(bits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
