#!/usr/bin/env python3
"""Email digest of highly rated upcoming OTA movies not already in Plex.

Default is notify-only (prints a schedule). Use --execute to create DVR one-shots.

  python3 plex-record-movies.py
  python3 plex-record-movies.py --execute
  python3 plex-record-movies.py --verbose
"""
from __future__ import annotations

import argparse
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
CACHE_PATH = DIR / "ratings-cache.json"
NONE_RETRY_SECS = 24 * 3600
EDITION_RE = re.compile(r"\s*\{edition-[^}]+\}\s*", re.I)
YEAR_RE = re.compile(r"\((\d{4})\)\s*$")
NON_ALNUM = re.compile(r"[^a-z0-9]+")
SKIP_LABELS = {
    "owned": "in library",
    "scheduled": "already scheduled",
    "year": "too old",
    "short": "too short",
    "sd": "not HD",
    "spanish": "Spanish",
    "norating": "no IMDb",
    "low": "below IMDb bar",
    "small": "small box office",
}


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env



def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


_SECRET_QS = re.compile(
    r"(?i)([?&](?:X-Plex-Token|apikey|api_key|api-key|PLEX_TOKEN|OMDB_KEY|token)=)[^&\s]+"
)


def safe_exc(exc: BaseException) -> str:
    """Exception text with tokens and API keys stripped, for logs and digests."""
    return _SECRET_QS.sub(r"\1<redacted>", str(exc))



def norm_title(title: str) -> str:
    title = EDITION_RE.sub(" ", title)
    title = YEAR_RE.sub("", title)
    return NON_ALNUM.sub("", title.lower())


def plex_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": "plex-record-movies",
        "X-Plex-Product": "plex-record-movies",
        "X-Plex-Device-Name": "plex-record-movies",
    }


class Plex:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def request(
        self,
        path: str,
        method: str = "GET",
        params: dict | None = None,
        raw_query: str = "",
    ) -> dict:
        q = dict(params or {})
        q["X-Plex-Token"] = self.token
        parts = [raw_query.lstrip("?&")] if raw_query else []
        if q:
            parts.append(urllib.parse.urlencode(q, doseq=True))
        url = self.base + path
        if parts:
            url += ("&" if "?" in path else "?") + "&".join(parts)
        req = urllib.request.Request(
            url,
            data=b"" if method.upper() != "GET" else None,
            method=method,
            headers=plex_headers(self.token),
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def container(self, path: str, **params) -> list[dict]:
        data = self.request(path, params=params)
        mc = data.get("MediaContainer") or data
        return mc.get("Metadata") or mc.get("Directory") or mc.get("Hub") or []


def plex_alive(url: str, token: str) -> bool:
    try:
        Plex(url, token).request("/identity")
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return False


def discover_plex_url(token: str, configured: str) -> str:
    """Use PLEX_URL if it answers; otherwise pick a local owned server from plex.tv."""
    candidates: list[str] = []
    if configured:
        candidates.append(configured.rstrip("/"))
    try:
        req = urllib.request.Request(
            "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1",
            headers=plex_headers(token),
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resources = json.loads(resp.read().decode("utf-8") or "[]")
        for row in resources if isinstance(resources, list) else []:
            if "server" not in str(row.get("provides") or ""):
                continue
            if not row.get("owned"):
                continue
            for conn in row.get("connections") or []:
                if conn.get("relay") or not conn.get("address"):
                    continue
                addr = str(conn.get("address"))
                port = conn.get("port") or 32400
                if conn.get("local"):
                    url = f"http://{addr}:{port}"
                else:
                    url = f"{conn.get('protocol') or 'http'}://{addr}:{port}"
                url = url.rstrip("/")
                if url not in candidates:
                    candidates.append(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"plex.tv discover: {safe_exc(exc)}", file=sys.stderr)
    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if plex_alive(url, token):
            if configured and url.rstrip("/") != configured.rstrip("/"):
                print(f"Plex moved: using {url} (not {configured})", file=sys.stderr)
            return url
    raise SystemExit("ERROR: cannot reach Plex Media Server; set PLEX_URL")


def pref_str(setting: dict) -> str:
    val = setting.get("value", setting.get("default"))
    if setting.get("type") == "bool":
        if isinstance(val, bool):
            return "true" if val else "false"
        return "true" if str(val).lower() in {"1", "true", "yes"} else "false"
    if val is None:
        return ""
    return str(val)


def unescape_plex_id(val: str) -> str:
    text = val or ""
    for _ in range(3):
        nxt = urllib.parse.unquote(text)
        if nxt == text:
            break
        text = nxt
    return text


def movie_section_id(plex: Plex, forced: str) -> str:
    if forced:
        return forced
    for sec in plex.container("/library/sections"):
        if sec.get("type") == "movie":
            return str(sec.get("key") or sec.get("id") or "")
    raise SystemExit("ERROR: no movie library section found")


def epg_movies_key(plex: Plex, forced: str) -> str:
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
                if d.get("type") == "movie" or str(d.get("title") or "").lower() == "movies":
                    key = d.get("key") or ""
                    if key:
                        if not key.startswith("/"):
                            key = "/" + ident + "/" + key.lstrip("/")
                        if "all" not in key:
                            key = key.rstrip("/") + "/all"
                        return key
        ident_path = ident if ident.startswith("/") else "/" + ident
        return f"{ident_path}/sections/1/all"
    raise SystemExit("ERROR: could not find EPG movies key; set EPG_MOVIES_KEY")


def library_index(plex: Plex, section_id: str) -> set[tuple[str, str]]:
    owned: set[tuple[str, str]] = set()
    start = 0
    while True:
        data = plex.request(
            f"/library/sections/{section_id}/all",
            params={
                "type": 1,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": 200,
            },
        )
        mc = data.get("MediaContainer") or {}
        items = mc.get("Metadata") or []
        for it in _as_dicts(items):
            year = str(it.get("year") or "")
            owned.add((norm_title(it.get("title") or ""), year))
        start += len(items)
        if not items or start >= int(mc.get("totalSize") or start):
            break
    return owned


def _as_dicts(value) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def subscription_titles(plex: Plex) -> set[str]:
    titles: set[str] = set()
    data = plex.request("/media/subscriptions")
    mc = data.get("MediaContainer") or data
    if not isinstance(mc, dict):
        return titles
    subs = mc.get("MediaSubscription") or mc.get("Metadata") or []
    for sub in _as_dicts(subs):
        title = sub.get("title") or ""
        if title:
            titles.add(norm_title(title))
        for video in _as_dicts(sub.get("Video") or sub.get("Metadata") or []):
            t = video.get("title") or ""
            if t:
                titles.add(norm_title(t))
    return titles


_OMDB_NOTE = False


def omdb_fetch(title: str, year: str, key: str, *, verbose: bool = False) -> dict:
    global _OMDB_NOTE
    if not key:
        return {}
    q = {"t": title, "apikey": key}
    if year:
        q["y"] = year
    url = "https://www.omdbapi.com/?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace") if exc.fp else ""
        if verbose or not _OMDB_NOTE:
            print(f"OMDb HTTP {exc.code}: {body}", file=sys.stderr)
            _OMDB_NOTE = True
        return {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        if verbose or not _OMDB_NOTE:
            print(f"OMDb error: {safe_exc(exc)}", file=sys.stderr)
            _OMDB_NOTE = True
        return {}
    if verbose or not _OMDB_NOTE:
        print(
            f"OMDb sample '{title}': Response={data.get('Response')} "
            f"Error={data.get('Error')!r} imdbRating={data.get('imdbRating')!r} "
            f"Language={data.get('Language')!r}",
            file=sys.stderr,
        )
        _OMDB_NOTE = True
    if str(data.get("Response")) != "True":
        return {}
    score = None
    raw = data.get("imdbRating") or "N/A"
    try:
        score = float(raw)
    except (TypeError, ValueError):
        pass
    return {
        "score": score,
        "language": data.get("Language") or "",
        "box": parse_money(str(data.get("BoxOffice") or "")),
    }


def omdb_rating(title: str, year: str, key: str, *, verbose: bool = False) -> float | None:
    return omdb_fetch(title, year, key, verbose=verbose).get("score")


def is_spanish(language: str) -> bool:
    parts = [p.strip().lower() for p in re.split(r"[,/]", language or "") if p.strip()]
    if not parts:
        return False
    spa = {"spanish", "español", "espanol", "castilian", "spa"}
    eng = {"english", "eng"}
    return parts[0] in spa or (any(p in spa for p in parts) and not any(p in eng for p in parts))


def parse_money(raw: str) -> int:
    if not raw or raw.upper() in {"N/A", "NA", "NONE"}:
        return 0
    digits = re.sub(r"[^\d]", "", raw)
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def fmt_money(n: int) -> str:
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.0f}M"
    if n >= 1_000:
        return f"${n / 1_000:.0f}K"
    if n > 0:
        return f"${n}"
    return ""


def cache_key(title: str, year: str) -> str:
    return f"{norm_title(title)}|{year}"


def load_cache() -> dict:
    if not CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def plex_search_rating(plex: Plex, title: str, year: str) -> float | None:
    try:
        data = plex.request("/hubs/search", params={"query": title, "limit": 25})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"Plex search failed for '{title}' ({safe_exc(exc)}); skipping", file=sys.stderr)
        return None
    mc = data.get("MediaContainer") or {}
    want = norm_title(title)
    for hub in _as_dicts(mc.get("Hub") or []):
        for it in _as_dicts(hub.get("Metadata") or []):
            if str(it.get("type") or "") not in ("movie", "1", ""):
                continue
            if norm_title(it.get("title") or "") != want:
                continue
            if year and str(it.get("year") or "") not in ("", year):
                continue
            score = plex_rating(it)
            if score is not None:
                return score
    return None


def lookup_score(
    plex: Plex, item: dict, title: str, year: str, cache: dict, omdb_key: str
) -> tuple[float | None, str, str, int, bool]:
    ck = cache_key(title, year)
    if ck in cache:
        ent = cache[ck] if isinstance(cache[ck], dict) else {"score": cache[ck], "source": "cache"}
        raw = ent.get("score")
        src = str(ent.get("source") or "cache")
        lang = str(ent.get("language") or "")
        box = int(ent.get("box") or 0)
        if raw is not None:
            try:
                score = float(raw)
            except (TypeError, ValueError):
                score = None
            else:
                return score, src, lang, box, True
        # A missing score may be a transient OMDb/Plex failure: retry after a day.
        if time.time() - int(ent.get("ts") or 0) < NONE_RETRY_SECS:
            return None, "cache", lang, box, True
    score = plex_rating(item)
    source = "epg"
    lang = str(item.get("originalLanguage") or item.get("language") or "")
    box = 0
    if score is None and omdb_key:
        info = omdb_fetch(title, year, omdb_key)
        score = info.get("score")
        source = "omdb"
        lang = lang or str(info.get("language") or "")
        box = int(info.get("box") or 0)
    if score is None:
        score = plex_search_rating(plex, title, year)
        source = "plex-search"
    cache[ck] = {
        "score": score,
        "source": source if score is not None else ("omdb" if omdb_key else "none"),
        "language": lang,
        "box": box,
        "ts": int(time.time()),
    }
    return score, source if score is not None else "none", lang, box, False


def plex_rating(item: dict) -> float | None:
    for field in ("audienceRating", "rating", "userRating"):
        val = item.get(field)
        if val is None:
            continue
        try:
            n = float(val)
        except (TypeError, ValueError):
            continue
        if n > 10:
            n = n / 10.0
        if 0 < n <= 10:
            return n
    return None


def is_hd(item: dict) -> bool:
    for media in item.get("Media") or []:
        res = str(media.get("videoResolution") or media.get("videoQuality") or "").lower()
        if res in {"720", "1080", "1440", "4k", "2160", "hd"}:
            return True
        if "hd" in res:
            return True
        h = media.get("height")
        try:
            if int(h) >= 720:
                return True
        except (TypeError, ValueError):
            pass
    return False


def runtime_min(item: dict) -> int:
    dur = item.get("duration")
    try:
        ms = int(dur)
        return ms // 60000
    except (TypeError, ValueError):
        return 0


def media_list(item: dict) -> list[dict]:
    return _as_dicts(item.get("Media") or [])


def begins_at(item: dict) -> int:
    for media in media_list(item):
        for field in ("beginsAt", "begins"):
            try:
                n = int(media.get(field))
            except (TypeError, ValueError):
                continue
            if n > 1_000_000_000:
                return n
    try:
        n = int(item.get("beginsAt"))
    except (TypeError, ValueError):
        return 0
    return n if n > 1_000_000_000 else 0


def channel_name(item: dict) -> str:
    for media in media_list(item):
        for field in ("channelCallSign", "channelTitle", "channelIdentifier"):
            val = str(media.get(field) or "").strip()
            if val:
                return val
    return ""


def airing_channel_param(item: dict) -> str:
    for media in media_list(item):
        ident = str(media.get("channelIdentifier") or "").strip()
        title = str(media.get("channelTitle") or media.get("channelCallSign") or "").strip()
        if ident and title:
            return f"{ident}={title}"
        if ident:
            return ident
    return ""


def day_and_clock(ts: int) -> tuple[str, str]:
    if ts <= 0:
        return "When unknown", ""
    dt = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    d = dt.date()
    if d == today:
        day = "Today"
    elif d == today + timedelta(days=1):
        day = "Tomorrow"
    else:
        day = dt.strftime("%a %b %d").replace(" 0", " ")
    clock = dt.strftime("%I:%M%p").lstrip("0").lower()
    return day, clock


def pick_line(item: dict, score: float, box: int, extra: str = "") -> str:
    title = (item.get("title") or "").strip()
    year = str(item.get("year") or "")
    _, clock = day_and_clock(begins_at(item))
    ch = (channel_name(item) or "—")[:12]
    clock = f"{clock:<7}" if clock else "       "
    money = fmt_money(box)
    line = f"  {clock}  {ch:<12}  {title} ({year})  {score:.1f}"
    if money:
        line += f"  {money}"
    if extra:
        line += f"  {extra}"
    return line


def template_params(plex: Plex, guid: str) -> dict[str, str]:
    if not guid:
        return {}
    data = plex.request("/media/subscriptions/template", params={"guid": guid})
    mc = data.get("MediaContainer") or data
    for tmpl in _as_dicts(mc.get("SubscriptionTemplate") or []):
        subs = tmpl.get("MediaSubscription") or [tmpl]
        for sub in _as_dicts(subs):
            raw = sub.get("parameters") or ""
            parsed = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True)) if raw else {}
            if sub.get("type") is not None:
                parsed["type"] = str(sub.get("type"))
            if sub.get("targetLibrarySectionID") is not None:
                parsed["targetLibrarySectionID"] = str(sub.get("targetLibrarySectionID"))
            for setting in _as_dicts(sub.get("Setting") or []):
                sid = setting.get("id")
                if sid:
                    parsed[f"prefs[{sid}]"] = pref_str(setting)
            if parsed:
                return parsed
    return {}


def template_parameter_query(plex: Plex, guid: str) -> str:
    """Raw Record-button query. Rebuilding it changes Plex's encoding and drops the airings."""
    if not guid:
        return ""
    try:
        data = plex.request("/media/subscriptions/template", params={"guid": guid})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return ""
    mc = data.get("MediaContainer") or data
    found = ""
    for tmpl in _as_dicts(mc.get("SubscriptionTemplate") or []):
        subs = tmpl.get("MediaSubscription") or [tmpl]
        for sub in _as_dicts(subs):
            raw = str(sub.get("parameters") or "")
            if "airingTimes" not in raw:
                continue
            if sub.get("selected") in (True, 1, "1"):
                return raw
            if not found:
                found = raw
    return found


def movie_subscriptions(plex: Plex) -> list[dict]:
    data = plex.request("/media/subscriptions", params={"includeGrabs": 1})
    mc = data.get("MediaContainer") or data
    if not isinstance(mc, dict):
        return []
    return [s for s in _as_dicts(mc.get("MediaSubscription") or []) if str(s.get("type")) == "1"]


def grab_ops(sub: dict) -> list[dict]:
    grabs = sub.get("MediaGrabOperation")
    if isinstance(grabs, dict):
        return [grabs]
    if isinstance(grabs, list):
        return [g for g in grabs if isinstance(g, dict)]
    return []


def sub_item(sub: dict) -> dict:
    video = sub.get("Video") or {}
    if isinstance(video, list):
        video = video[0] if video else {}
    if not isinstance(video, dict):
        video = {}
    return {
        "title": video.get("title") or sub.get("title") or "",
        "year": video.get("year") or "",
        "guid": unescape_plex_id(str(video.get("guid") or "")),
        "ratingKey": unescape_plex_id(str(video.get("ratingKey") or "")),
        "thumb": video.get("thumb") or "",
    }


def ungrabbed_movie_id(plex: Plex, guid: str) -> str:
    want = unescape_plex_id(guid)
    for sub in movie_subscriptions(plex):
        item = sub_item(sub)
        if item["guid"] == want and not grab_ops(sub):
            return str(sub.get("key") or "")
    return ""


def record_movie(
    plex: Plex,
    item: dict,
    section_id: str,
    hd_only: bool,
    replace_id: str = "",
) -> str:
    """Create a one-shot that names the guide airings.

    A subscription posted with only the movie guid is stored, then shows
    "no scheduled airings" forever. The template query carries
    params[airingChannels] and params[airingTimes]; pass that string through
    unchanged. Replacing an existing empty subscription has to delete it —
    PUT will not attach airings.
    """
    guid = unescape_plex_id(str(item.get("guid") or ""))
    raw = template_parameter_query(plex, guid)
    if "airingTimes" not in raw:
        return "no-airing"
    if replace_id:
        plex.request(f"/media/subscriptions/{replace_id}", method="DELETE")
    params = {
        "type": "1",
        "targetLibrarySectionID": section_id,
        "includeGrabs": "1",
        "prefs[oneShot]": "true",
        "prefs[replaceLowerQuality]": "false",
        "prefs[recordPartials]": "false",
        "prefs[remoteMedia]": "false",
        "prefs[minVideoQuality]": "720" if hd_only else "0",
    }
    try:
        created = plex.request("/media/subscriptions", method="POST", params=params, raw_query=raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 409 and not replace_id:
            existing = ungrabbed_movie_id(plex, guid)
            if existing:
                return record_movie(plex, item, section_id, hd_only, replace_id=existing)
            return "exists"
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {body[:300]}") from exc
    sub_id = ""
    mc = created.get("MediaContainer") or created
    for sub in _as_dicts(mc.get("MediaSubscription") or []):
        sub_id = str(sub.get("key") or "")
        break
    return f"ok:{sub_id}" if sub_id else "ok"


def grab_when(plex: Plex, sub_id: str) -> str:
    """Soonest airing on a subscription, after /media/subscriptions/process."""
    if not sub_id:
        return "still no airing"
    try:
        data = plex.request(f"/media/subscriptions/{sub_id}", params={"includeGrabs": 1})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return "still no airing"
    mc = data.get("MediaContainer") or data
    subs = _as_dicts(mc.get("MediaSubscription") or []) if isinstance(mc, dict) else []
    grabs = grab_ops(subs[0]) if subs else []
    rows = []
    for grab in grabs:
        meta = grab.get("Metadata") if isinstance(grab.get("Metadata"), dict) else {}
        medias = media_list(meta)
        media = medias[0] if medias else {}
        try:
            begins = int(media.get("beginsAt") or 0)
        except (TypeError, ValueError):
            begins = 0
        res = str(media.get("videoResolution") or "")
        if res.isdigit():
            res = f"{res}p"
        rows.append((begins or 10**12, str(grab.get("status") or ""), str(media.get("channelCallSign") or ""), res))
    if not rows:
        return "still no airing"
    rows.sort()
    begins, status, channel, res = rows[0]
    day, clock = day_and_clock(begins if begins < 10**12 else 0)
    bits = [status, f"{day} {clock}".strip(), channel, res]
    text = " ".join(bit for bit in bits if bit)
    extra = len(rows) - 1
    if extra:
        text += f" (+{extra} later)"
    return text


def fill_box(picks: list[tuple], omdb_key: str, cache: dict) -> list[tuple]:
    filled = []
    for begins, score, box, source, item in picks:
        title = (item.get("title") or "").strip()
        year = str(item.get("year") or "")
        if not box and omdb_key:
            info = omdb_fetch(title, year, omdb_key)
            box = int(info.get("box") or 0)
            ck = cache_key(title, year)
            if ck in cache and isinstance(cache[ck], dict):
                cache[ck]["box"] = box
        filled.append((begins, score, box, source, item))
    return filled


def dedupe_earliest(picks: list[tuple]) -> list[tuple]:
    best: dict[tuple[str, str], tuple] = {}
    for row in picks:
        item = row[4]
        key = (norm_title(item.get("title") or ""), str(item.get("year") or ""))
        prev = best.get(key)
        if prev is None or row[0] < prev[0] or (row[0] == prev[0] and row[1] > prev[1]):
            best[key] = row
    return list(best.values())


def _record_ok(status: str) -> bool:
    return status == "exists" or status == "ok" or str(status).startswith("ok")


def _airing_attached(line: str) -> bool:
    detail = line.strip().rsplit("  ", 1)[-1]
    return detail.startswith(("scheduled", "inprogress", "complete", "postprocessing", "paused"))


def print_digest(
    picks: list[tuple],
    skip_counts: Counter,
    upcoming_n: int,
    statuses: list[str] | None,
    repairs: list[str] | None = None,
) -> None:
    repairs = repairs or []
    n = len(picks)
    executed = statuses is not None
    parts: list[str] = []
    if executed and n:
        ok = sum(1 for s in statuses if _record_ok(s))
        parts.append(f"scheduled {ok} of {n}")
    elif n:
        parts.append(f"{n} movie{'s' if n != 1 else ''} worth recording")
    if executed and repairs:
        ok = sum(1 for line in repairs if _airing_attached(line))
        parts.append(f"reattached {ok} of {len(repairs)}")
    elif repairs:
        parts.append(f"{len(repairs)} scheduled with no airing")
    if parts:
        head = "Plex DVR — " + " · ".join(parts)
    elif executed:
        head = "Plex DVR — nothing to record"
    else:
        head = "Plex DVR — nothing worth recording"
    print(head)
    print()
    last_day = None
    for i, (begins, score, box, _source, item) in enumerate(picks):
        day, _ = day_and_clock(begins)
        if day != last_day:
            if last_day is not None:
                print()
            print(day)
            last_day = day
        extra = ""
        if statuses:
            extra = statuses[i]
        print(pick_line(item, score, box, extra))
    if repairs:
        if picks:
            print()
        print("Airing reattached" if any(_airing_attached(line) for line in repairs) else "No scheduled airing")
        for line in repairs:
            print(line)
    elif not picks:
        print("No upcoming HD movies cleared the IMDb / box-office bar.")
    bits = [f"{upcoming_n} upcoming"]
    for key, label in SKIP_LABELS.items():
        c = skip_counts.get(key, 0)
        if c:
            bits.append(f"{c} {label}")
    print()
    print(" · ".join(bits))


def main(argv: list[str] | None = None) -> int:
    secrets = load_env(DIR / "secrets.env")
    for k, v in secrets.items():
        os.environ.setdefault(k, v)

    parser = argparse.ArgumentParser(description="Digest of highly rated upcoming Plex DVR movies")
    parser.add_argument("--execute", action="store_true", help="Also create DVR one-shot subscriptions")
    parser.add_argument("--verbose", action="store_true", help="Print every skip")
    parser.add_argument("--min-imdb", type=float, default=float(cfg("MIN_IMDB", "7.0") or 7.0))
    parser.add_argument("--always-imdb", type=float, default=float(cfg("ALWAYS_IMDB", "8.0") or 8.0))
    parser.add_argument("--max", type=int, default=int(cfg("MAX_PER_RUN", "25") or 25))
    parser.add_argument("--min-runtime", type=int, default=int(cfg("MIN_RUNTIME_MIN", "80") or 80))
    parser.add_argument("--min-year", type=int, default=int(cfg("MIN_YEAR", "1970") or 1970))
    parser.add_argument("--debug-rating", metavar="TITLE", help="Look up one title and exit")
    args = parser.parse_args(argv)

    if args.debug_rating:
        key = cfg("OMDB_KEY") or cfg("OMDB_API_KEY")
        print(f"omdb={'yes' if key else 'NO'} title={args.debug_rating!r}")
        score = omdb_rating(args.debug_rating, "", key, verbose=True)
        print(f"score={score}")
        return 0 if score is not None else 1

    token = cfg("PLEX_TOKEN")
    if not token:
        print("ERROR: set PLEX_TOKEN in secrets.env", file=sys.stderr)
        return 1
    plex = Plex(discover_plex_url(token, cfg("PLEX_URL", "http://127.0.0.1:32400")), token)
    hd_only = cfg("HD_ONLY", "1") not in {"0", "false", "no"}

    section_id = movie_section_id(plex, cfg("MOVIES_SECTION_ID"))
    epg_key = epg_movies_key(plex, cfg("EPG_MOVIES_KEY"))
    if "?" not in epg_key:
        epg_key = epg_key + ("&" if "?" in epg_key else "?") + "type=1&sort=beginsAt"
    epg_path, _, epg_qs = epg_key.partition("?")
    extra = dict(urllib.parse.parse_qsl(epg_qs))

    owned = library_index(plex, section_id)
    scheduled = subscription_titles(plex)
    upcoming = _as_dicts(plex.container(epg_path, **extra))
    cache = load_cache()
    omdb_key = cfg("OMDB_KEY") or cfg("OMDB_API_KEY")

    skip_counts: Counter = Counter()
    skips: list[str] = []
    picks: list[tuple] = []
    rated = 0
    for item in upcoming:
        if item.get("type") not in (None, "movie", 1, "1"):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        year = str(item.get("year") or "")
        key = (norm_title(title), year)
        reason = ""
        detail = ""
        if key in owned or (norm_title(title), "") in owned:
            reason, detail = "owned", f"[owned] {title} ({year})"
        elif norm_title(title) in scheduled:
            reason, detail = "scheduled", f"[already scheduled] {title} ({year})"
        else:
            try:
                y = int(year) if year else 0
            except ValueError:
                y = 0
            if y and y < args.min_year:
                reason, detail = "year", f"[year] {title} ({year})"
            else:
                mins = runtime_min(item)
                if mins and mins < args.min_runtime:
                    reason, detail = "short", f"[short] {title} ({year}) {mins}m"
                elif hd_only and not is_hd(item):
                    reason, detail = "sd", f"[sd] {title} ({year})"
        if reason:
            skip_counts[reason] += 1
            if args.verbose:
                skips.append(detail)
            continue
        score, source, language, box, cached = lookup_score(plex, item, title, year, cache, omdb_key)
        if not cached:
            rated += 1
            if rated % 15 == 0:
                print(f"... OMDb/search {rated} (uncached)", file=sys.stderr, flush=True)
        if is_spanish(language):
            skip_counts["spanish"] += 1
            if args.verbose:
                skips.append(f"[spanish] {title} ({year})")
            continue
        if score is None:
            skip_counts["norating"] += 1
            if args.verbose:
                skips.append(f"[no rating] {title} ({year})")
            continue
        min_box = int(cfg("MIN_BOX_OFFICE", "50000000") or 0)
        always = score >= args.always_imdb
        big = bool(min_box and box >= min_box)
        decent = score >= args.min_imdb and (not min_box or box == 0 or box >= min_box)
        if not (always or big or decent):
            if score < args.min_imdb:
                skip_counts["low"] += 1
                if args.verbose:
                    skips.append(f"[low {score:.1f} {source}] {title} ({year})")
            else:
                skip_counts["small"] += 1
                if args.verbose:
                    skips.append(f"[small {fmt_money(box)}] {title} ({year}) {score:.1f}")
            continue
        picks.append((begins_at(item), score, box, source, item))

    picks = fill_box(picks, omdb_key, cache)
    picks = dedupe_earliest(picks)
    picks.sort(key=lambda row: (row[0] or 10**12, -row[1], -row[2]))
    if args.max > 0:
        picks = picks[: args.max]

    # Movie one-shots created without airingTimes sit on the schedule forever
    # and block a retry. Recreate those once the guide has an airing again.
    empty_subs = [sub for sub in movie_subscriptions(plex) if not grab_ops(sub)]
    repair_lines: list[str] = []
    statuses: list[str] | None = None
    repair_statuses: list[str] | None = None
    if args.execute:
        statuses = []
        for _begins, _score, _box, _source, item in picks:
            try:
                statuses.append(record_movie(plex, item, section_id, hd_only))
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, RuntimeError) as exc:
                statuses.append(f"FAIL {safe_exc(exc)}")
        repair_statuses = []
        for sub in empty_subs:
            try:
                repair_statuses.append(
                    record_movie(plex, sub_item(sub), section_id, hd_only, replace_id=str(sub.get("key") or ""))
                )
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, RuntimeError) as exc:
                repair_statuses.append(f"FAIL {safe_exc(exc)}")
        if any(_record_ok(s) for s in statuses + repair_statuses):
            try:
                plex.request("/media/subscriptions/process", method="POST")
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
                print(f"subscription process: {safe_exc(exc)}", file=sys.stderr)
        for sub, status in zip(empty_subs, repair_statuses):
            item = sub_item(sub)
            label = f"{item['title']} ({item['year']})"
            if status.startswith("ok:"):
                when = grab_when(plex, status.split(":", 1)[1])
                repair_lines.append(f"  {label}  {when}")
            elif status == "no-airing":
                repair_lines.append(f"  {label}  no upcoming airing")
            else:
                repair_lines.append(f"  {label}  {status}")
    else:
        for sub in empty_subs:
            item = sub_item(sub)
            label = f"{item['title']} ({item['year']})"
            if "airingTimes" in template_parameter_query(plex, item["guid"]):
                repair_lines.append(f"  {label}  would reattach")
            else:
                repair_lines.append(f"  {label}  no upcoming airing")

    save_cache(cache)
    if args.verbose:
        for line in skips:
            print(line, file=sys.stderr)
    print_digest(picks, skip_counts, len(upcoming), statuses, repair_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
