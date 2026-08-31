#!/usr/bin/env python3
"""
GH Ribbon feed harvester.

Draait in GitHub Actions, niet in de browser. Haalt alle feeds server-side op
en schrijft data/feeds.json weg. De pagina laadt alleen dat bestand, van
dezelfde origin -- geen CORS, geen proxy, geen rate limits.

Ontwerpkeuzes die ertoe doen:
  * Een falende feed laat de rest ongemoeid en gooit oude items niet weg.
  * Items van een vorige run blijven staan (tot MAX_AGE_DAYS), zodat een
    tijdelijk kapotte bron geen gat in de ribbon slaat.
  * Per feed wordt de status meegeschreven, zodat de pagina kan tonen wat
    er misging in plaats van stil leeg te blijven.
"""

import json
import pathlib
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape

import feedparser

OUT           = pathlib.Path("data/feeds.json")
MAX_AGE_DAYS  = 21     # ouder dan dit valt uit de cache
MAX_PER_FEED  = 12     # per bron meenemen
MAX_TOTAL     = 400    # harde bovengrens op het bestand
TIMEOUT       = 25
RETRIES       = 2

# Een echte browser-User-Agent. Dit is de reden dat dit server-side wél lukt:
# Cloudflare weigert de generieke UA's van gratis CORS-proxies.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Feeds. cat moet overeenkomen met de filterknoppen in index.html:
#   who | promed | emerg | global | journal | policy | news
# Een feed die blijft falen kun je gewoon uitcommentariëren.
# ---------------------------------------------------------------------------
FEEDS = [
    # --- WHO -------------------------------------------------------------
    {"label": "WHO News",             "cat": "who",
     "url": "https://www.who.int/rss-feeds/news-english.xml"},
    {"label": "WHO Disease Outbreak News", "cat": "who",
     "url": "https://www.who.int/feeds/entity/csr/don/en/rss.xml"},
    {"label": "WHO Alert",            "cat": "who",
     "url": "https://www.google.com/alerts/feeds/07358569115421849873/153340034560442073"},

    # --- Outbreak / ProMED ------------------------------------------------
    {"label": "ProMED",               "cat": "promed",
     "url": "https://www.google.com/alerts/feeds/07358569115421849873/14881210468558991559"},
    {"label": "CIDRAP",               "cat": "promed",
     "url": "https://www.cidrap.umn.edu/news/rss.xml"},

    # --- Health emergencies ------------------------------------------------
    {"label": "Health Emergency",     "cat": "emerg",
     "url": "https://www.google.com/alerts/feeds/07358569115421849873/3291854287981540699"},
    {"label": "ReliefWeb Health",     "cat": "emerg",
     "url": "https://reliefweb.int/updates/rss.xml?view=headlines"},

    # --- Global health ------------------------------------------------------
    {"label": "Global Health",        "cat": "global",
     "url": "https://www.google.com/alerts/feeds/07358569115421849873/5068632798327940500"},
    {"label": "ECDC",                 "cat": "global",
     "url": "https://www.ecdc.europa.eu/en/rss"},

    # --- Journals ------------------------------------------------------------
    {"label": "The Lancet",           "cat": "journal",
     "url": "https://www.thelancet.com/rssfeed/lancet_online.xml"},
    {"label": "Lancet Global Health", "cat": "journal",
     "url": "https://www.thelancet.com/rssfeed/langlo_online.xml"},
    {"label": "BMJ",                  "cat": "journal",
     "url": "https://www.bmj.com/rss/current.xml"},

    # --- Policy ---------------------------------------------------------------
    {"label": "Health Policy Watch",  "cat": "policy",
     "url": "https://healthpolicy-watch.news/feed/"},
    {"label": "Politico EU Health",   "cat": "policy",
     "url": "https://www.politico.eu/feed/?cat=96"},

    # --- News ------------------------------------------------------------------
    {"label": "STAT News",            "cat": "news",
     "url": "https://www.statnews.com/feed/"},
]


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", unescape(txt)).strip()


def unwrap(url: str) -> str:
    """Google Alerts verpakt de echte link in een redirect-parameter."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        for key in ("url", "q"):
            if key in q and q[key][0].startswith("http"):
                return q[key][0]
    except Exception:
        pass
    return url


def iso(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return ""


def download(url: str) -> bytes:
    last = None
    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError) as e:
            last = e
            if attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
    raise last


def harvest(feed) -> tuple[list, str]:
    raw = download(feed["url"])
    parsed = feedparser.parse(raw)
    entries = parsed.entries or []
    if not entries:
        raise ValueError("0 entries (bron gaf geen bruikbare XML terug)")

    items = []
    for e in entries[:MAX_PER_FEED]:
        title = strip_html(e.get("title", ""))
        link = unwrap((e.get("link") or "").strip())
        if not title or not link.startswith("http"):
            continue
        summary = strip_html(
            e.get("summary") or (e.get("content", [{}])[0].get("value") if e.get("content") else "")
        )
        src = feed["label"]
        if e.get("source", {}).get("title"):
            src = strip_html(e["source"]["title"])
        items.append({
            "title":   title[:180],
            "url":     link,
            "src":     src,
            "feed":    feed["label"],
            "cat":     feed["cat"],
            "date":    iso(e),
            "summary": summary[:300],
        })
    if not items:
        raise ValueError("entries gevonden maar geen bruikbare titel/link")
    return items, f"{len(items)} items"


def load_previous() -> list:
    if not OUT.exists():
        return []
    try:
        return json.loads(OUT.read_text()).get("items", [])
    except Exception:
        return []


def main() -> int:
    fresh, status = [], {}
    for feed in FEEDS:
        try:
            items, msg = harvest(feed)
            fresh.extend(items)
            status[feed["label"]] = {"ok": True, "msg": msg, "cat": feed["cat"]}
            print(f"  OK    {feed['label']:<26} {msg}")
        except Exception as e:
            status[feed["label"]] = {"ok": False,
                                     "msg": f"{type(e).__name__}: {e}"[:200],
                                     "cat": feed["cat"]}
            print(f"  FAIL  {feed['label']:<26} {type(e).__name__}: {e}", file=sys.stderr)

    # Nieuw wint van oud, maar oude items blijven bestaan zolang ze vers zijn.
    merged, seen = [], set()
    for item in fresh + load_previous():
        key = item.get("url")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    def recent(i):
        if not i.get("date"):
            return True                      # datum onbekend: laten staan
        try:
            return datetime.fromisoformat(i["date"]) >= cutoff
        except Exception:
            return True

    merged = [i for i in merged if recent(i)]
    merged.sort(key=lambda i: i.get("date") or "", reverse=True)
    merged = merged[:MAX_TOTAL]

    ok = sum(1 for v in status.values() if v["ok"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feeds_ok":  ok,
        "feeds_tot": len(FEEDS),
        "status":    status,
        "items":     merged,
    }, ensure_ascii=False, indent=1))

    print(f"\n{ok}/{len(FEEDS)} feeds OK -> {len(merged)} items in {OUT}")
    # Nooit falen op een kapotte bron: dan zou de workflow rood staan en de
    # oude feeds.json blijven hangen. Alleen falen als er niets overblijft.
    return 0 if merged else 1


if __name__ == "__main__":
    sys.exit(main())
