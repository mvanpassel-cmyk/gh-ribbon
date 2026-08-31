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

import csv
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
ARCHIVE       = pathlib.Path("data/archive.csv")  # groeit dagelijks aan, opent in Excel/Numbers
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
#   who | promed | emerg | global | amr | journal | policy | news
# Een feed die blijft falen kun je gewoon uitcommentariëren.
# ---------------------------------------------------------------------------
def gnews(zoekterm: str) -> str:
    """Google News RSS als vangnet. Werkt waar Cloudflare de directe feed
    blokkeert (Politico, Euractiv, Eurosurveillance) of waar een site
    helemaal geen RSS meer aanbiedt. Levert kop + link, geen samenvatting."""
    return ("https://news.google.com/rss/search?q=" +
            urllib.parse.quote(zoekterm) + "&hl=en-GB&gl=GB&ceid=GB:en")


# ---------------------------------------------------------------------------
# Bronnen. Elke bron heeft een lijst kandidaat-URL's: de eerste die werkt
# wint. De laatste is meestal een Google News-zoekopdracht, zodat een bron
# nooit helemaal stilvalt. In de Actions-log zie je welke kandidaat het werd.
# ---------------------------------------------------------------------------
FEEDS = [
    # --- WHO ----------------------------------------------------------------
    {"label": "WHO News", "cat": "who", "urls": [
        "https://www.who.int/rss-feeds/news-english.xml"]},
    {"label": "WHO Disease Outbreak News", "cat": "who", "urls": [
        "https://www.who.int/rss-feeds/disease-outbreak-news-english.xml",
        "https://www.who.int/feeds/entity/csr/don/en/rss.xml",
        gnews("WHO disease outbreak news")]},
    {"label": "WHO Alert", "cat": "who", "urls": [
        "https://www.google.com/alerts/feeds/07358569115421849873/153340034560442073"]},
    {"label": "UN News Health", "cat": "who", "urls": [
        "https://news.un.org/feed/subscribe/en/news/topic/health/feed/rss.xml",
        "https://news.un.org/en/rss/health.xml",
        gnews("site:news.un.org health")]},

    # --- Outbreak / surveillance --------------------------------------------
    {"label": "ProMED", "cat": "promed", "urls": [
        "https://promedmail.org/feed/",
        gnews("ProMED-mail outbreak")]},
    {"label": "CIDRAP Public Health", "cat": "promed", "urls": [
        "https://www.cidrap.umn.edu/news/91/rss"]},          # geverifieerd
    {"label": "CIDRAP Pandemic Influenza", "cat": "promed", "urls": [
        "https://www.cidrap.umn.edu/news/86/rss"]},          # geverifieerd
    {"label": "Eurosurveillance", "cat": "promed", "urls": [
        "https://www.eurosurveillance.org/rss/current.xml",
        gnews("site:eurosurveillance.org")]},

    # --- Health emergencies ---------------------------------------------------
    {"label": "Health Emergency", "cat": "emerg", "urls": [
        "https://www.google.com/alerts/feeds/07358569115421849873/3291854287981540699"]},
    {"label": "ReliefWeb Health", "cat": "emerg", "urls": [
        "https://reliefweb.int/updates/rss.xml",
        "https://reliefweb.int/disasters/rss.xml",
        gnews("site:reliefweb.int health emergency")]},

    # --- Global health ----------------------------------------------------------
    {"label": "Global Health", "cat": "global", "urls": [
        "https://www.google.com/alerts/feeds/07358569115421849873/5068632798327940500"]},
    {"label": "ECDC", "cat": "global", "urls": [
        "https://www.ecdc.europa.eu/en/news-events/rss",
        "https://www.ecdc.europa.eu/en/rss",
        gnews("site:ecdc.europa.eu")]},
    {"label": "Africa CDC", "cat": "global", "urls": [
        "https://africacdc.org/news/feed/",
        "https://africacdc.org/feed/",
        gnews("site:africacdc.org")]},
    {"label": "Think Global Health", "cat": "global", "urls": [
        "https://www.thinkglobalhealth.org/rss.xml",
        "https://www.thinkglobalhealth.org/feed",
        gnews("site:thinkglobalhealth.org")]},

    # --- AMR ----------------------------------------------------------------------
    {"label": "CIDRAP Stewardship", "cat": "amr", "urls": [
        "https://www.cidrap.umn.edu/news/48/rss"]},          # geverifieerd
    {"label": "AMR Industry / news", "cat": "amr", "urls": [
        "https://www.news-medical.net/tag/feed/Antimicrobial-Resistance.aspx",
        gnews("antimicrobial resistance policy")]},

    # --- Journals --------------------------------------------------------------------
    {"label": "The Lancet", "cat": "journal", "urls": [
        "https://www.thelancet.com/rssfeed/lancet_online.xml"]},
    {"label": "Lancet Global Health", "cat": "journal", "urls": [
        "https://www.thelancet.com/rssfeed/langlo_online.xml"]},
    {"label": "BMJ", "cat": "journal", "urls": [
        "https://www.bmj.com/rss/current.xml"]},
    {"label": "BMJ Global Health", "cat": "journal", "urls": [
        "https://gh.bmj.com/rss/current.xml"]},
    {"label": "PLOS Global Public Health", "cat": "journal", "urls": [
        "https://journals.plos.org/globalpublichealth/feed/atom"]},

    # --- Policy -----------------------------------------------------------------------------
    {"label": "Health Policy Watch", "cat": "policy", "urls": [
        "https://healthpolicy-watch.news/feed/"]},
    {"label": "Geneva Health Files", "cat": "policy", "urls": [
        "https://genevahealthfiles.substack.com/feed",
        gnews("Geneva Health Files pandemic treaty")]},
    {"label": "Politico EU Health", "cat": "policy", "urls": [
        "https://www.politico.eu/feed/?cat=96",
        gnews("site:politico.eu health")]},
    {"label": "Euractiv Health", "cat": "policy", "urls": [
        "https://www.euractiv.com/sections/health-consumers/feed/",
        gnews("site:euractiv.com health")]},

    # --- News ------------------------------------------------------------------------------------
    {"label": "STAT News", "cat": "news", "urls": [
        "https://www.statnews.com/feed/"]},
    {"label": "Devex Global Health", "cat": "news", "urls": [
        "https://www.devex.com/news/feed",
        gnews("site:devex.com global health")]},
]


# ---------------------------------------------------------------------------
# Relevantiescore. Bepaalt welke 30 items de pagina laat zien.
# Twee componenten: hoe global-health-gericht is de bron, en hoeveel
# relevante termen staan er in titel (dubbel gewicht) en samenvatting.
# Trefwoorden aanpassen aan je portefeuille is de makkelijkste manier om
# de ribbon scherper te krijgen -- pas gerust deze twee tabellen aan.
# ---------------------------------------------------------------------------
BRONGEWICHT = {
    "Health Policy Watch": 4, "Geneva Health Files": 4, "WHO News": 4,
    "WHO Disease Outbreak News": 4, "Lancet Global Health": 4,
    "Think Global Health": 4, "BMJ Global Health": 3, "WHO Alert": 3,
    "PLOS Global Public Health": 3, "Africa CDC": 3, "UN News Health": 3,
    "Global Health": 3, "Health Emergency": 3, "ReliefWeb Health": 2,
    "Devex Global Health": 2, "ECDC": 2, "Eurosurveillance": 2,
    "Politico EU Health": 2, "Euractiv Health": 2, "CIDRAP Public Health": 2,
    "CIDRAP Pandemic Influenza": 3,
    "CIDRAP Stewardship": 2, "ProMED": 2, "AMR Industry / news": 2,
    "The Lancet": 1, "BMJ": 1, "STAT News": 1,
}

TERMEN = {
    # kern van de portefeuille
    5: ["pandemic agreement", "pandemic treaty", "pabs", "igwg", "pandemisch",
        "international health regulations", "world health assembly", "global health"],
    3: ["who ", "world health organization", "antimicrobial resistance", " amr ",
        "pandemic preparedness", "health security", "health emergency", "outbreak",
        "one health", "universal health coverage", "health financing",
        "global fund", "gavi", "unitaid", "wto trips", "equity", "low- and middle-income"],
    2: ["vaccine", "epidemic", "surveillance", "cholera", "mpox", "measles", "polio",
        "ebola", "influenza", "tuberculosis", "malaria", "hiv", "climate and health",
        "africa cdc", "ecdc", "hera", "eu health", "european commission",
        "indonesia", "south africa", "kenya", "india", "china"],
    # ruis: klinisch/commercieel VS-nieuws dat hier zelden toe doet
    -3: ["earnings", "ipo", "stock", "shares", "acquisition", "medicare",
         "medicaid", "obesity drug", "series a", "series b", "funding round"],
}


def score_item(item, feed_label: str) -> int:
    s = BRONGEWICHT.get(feed_label, 1)
    titel = (item.get("title") or "").lower()
    tekst = (item.get("summary") or "").lower()
    for gewicht, woorden in TERMEN.items():
        for w in woorden:
            if w in titel:
                s += gewicht * 2          # titel telt dubbel
            elif w in tekst:
                s += gewicht
    return s


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


_laatste_hit: dict = {}          # host -> tijdstip laatste request
HOST_PAUZE = 3.0                 # seconden tussen twee requests naar dezelfde host


def download(url: str) -> bytes:
    """Haalt één URL op. Respecteert een pauze per host, want BMJ gaf 429
    toen we bmj.com en gh.bmj.com vlak achter elkaar aanriepen."""
    host = urllib.parse.urlparse(url).netloc
    wacht = HOST_PAUZE - (time.monotonic() - _laatste_hit.get(host, 0))
    if wacht > 0:
        time.sleep(wacht)

    last = None
    for poging in range(RETRIES + 1):
        _laatste_hit[host] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:                     # rate limit: ruim wachten
                time.sleep(15 * (poging + 1))
            elif e.code in (403, 404):            # zinloos om te herhalen
                raise
            elif poging < RETRIES:
                time.sleep(2 * (poging + 1))
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            last = e
            if poging < RETRIES:
                time.sleep(2 * (poging + 1))
    raise last


def harvest(feed) -> tuple[list, str]:
    """Probeert de kandidaat-URL's op volgorde. De eerste die bruikbare XML
    oplevert wint. Zo overleeft een bron een verhuisde feed-URL, en hebben
    de Cloudflare-sites een Google News-vangnet."""
    kandidaten = feed.get("urls") or [feed["url"]]
    fouten = []
    for n, url in enumerate(kandidaten):
        try:
            parsed = feedparser.parse(download(url))
            entries = parsed.entries or []
            if not entries:
                raise ValueError("0 entries")

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
                items[-1]["score"] = score_item(items[-1], feed["label"])
            if not items:
                raise ValueError("entries zonder bruikbare titel/link")

            via = "" if n == 0 else f" (via kandidaat {n + 1})"
            return items, f"{len(items)} items{via}"
        except Exception as e:
            fouten.append(f"[{n + 1}] {type(e).__name__}: {e}")
    raise RuntimeError(" | ".join(fouten))


def load_previous() -> list:
    if not OUT.exists():
        return []
    try:
        return json.loads(OUT.read_text()).get("items", [])
    except Exception:
        return []


def append_archive(items) -> int:
    """Append-only dagboek van alles wat langskwam. Dedupe op URL, zodat een
    item dat drie dagen in een feed blijft staan maar één regel krijgt.
    utf-8-sig omdat Excel anders de accenten sloopt."""
    cols = ["datum_gezien", "gepubliceerd", "categorie", "bron", "feed", "titel", "url"]
    seen = set()
    if ARCHIVE.exists():
        try:
            with ARCHIVE.open(newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    seen.add(row.get("url", ""))
        except Exception as e:
            print(f"  archief onleesbaar, begin opnieuw: {e}", file=sys.stderr)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    nieuw = [i for i in items if i.get("url") and i["url"] not in seen]

    write_header = not ARCHIVE.exists()
    with ARCHIVE.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(cols)
        for i in nieuw:
            w.writerow([today, (i.get("date") or "")[:10], i.get("cat", ""),
                        i.get("src", ""), i.get("feed", ""), i.get("title", ""),
                        i.get("url", "")])
    return len(nieuw)


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

    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    toegevoegd = append_archive(fresh)

    print(f"\n{ok}/{len(FEEDS)} feeds OK -> {len(merged)} items in {OUT}")
    print(f"archief: {toegevoegd} nieuwe regels in {ARCHIVE}")
    # Nooit falen op een kapotte bron: dan zou de workflow rood staan en de
    # oude feeds.json blijven hangen. Alleen falen als er niets overblijft.
    return 0 if merged else 1


if __name__ == "__main__":
    sys.exit(main())
