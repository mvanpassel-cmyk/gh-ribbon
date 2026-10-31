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
# Trage onderwerpen: hier verschijnt minder nieuws, dus items mogen langer blijven.
MAX_AGE_PER_CAT = {"pandemic": 60, "ntd": 60, "amr": 45}
MAX_PER_FEED  = 12     # per bron meenemen
MAX_TOTAL     = 700    # harde bovengrens op het bestand
TIMEOUT       = 25
RETRIES       = 2

# Een echte browser-User-Agent. Dit is de reden dat dit server-side wél lukt:
# Cloudflare weigert de generieke UA's van gratis CORS-proxies.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Feeds. cat moet overeenkomen met de filterknoppen in index.html:
#   who | promed | emerg | global | amr | ntd | pandemic | journal | policy | news
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
    # ECDC: officiele taxonomie-feeds (zie https://www.ecdc.europa.eu/en/rss-feeds).
    # De oude adressen /en/news-events/rss en /en/rss bestaan niet; daardoor viel deze bron
    # terug op Google News. Er is geen feed voor de pagina "Public health events".
    {"label": "ECDC", "cat": "global", "urls": [
        "https://www.ecdc.europa.eu/en/taxonomy/term/1307/feed",       # nieuws en persberichten
        gnews("site:ecdc.europa.eu")]},
    {"label": "ECDC Epidemiological updates", "cat": "emerg", "urls": [
        "https://www.ecdc.europa.eu/en/taxonomy/term/1310/feed",
        gnews("site:ecdc.europa.eu epidemiological update")]},
    {"label": "ECDC Threats report", "cat": "emerg", "urls": [
        "https://www.ecdc.europa.eu/en/taxonomy/term/1505/feed",       # wekelijks CDTR
        gnews("site:ecdc.europa.eu communicable disease threats report")]},
    {"label": "ECDC Risk assessments", "cat": "emerg", "urls": [
        "https://www.ecdc.europa.eu/en/taxonomy/term/1295/feed",
        gnews("site:ecdc.europa.eu risk assessment")]},
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
    {"label": "AMR policy & research", "cat": "amr", "urls": [
        gnews('"antimicrobial resistance" OR "drug-resistant" OR superbugs')]},
    {"label": "AMR global governance", "cat": "amr", "urls": [
        gnews('Quadripartite AMR OR "Global Leaders Group" antimicrobial OR "Independent Panel on Evidence" antimicrobial')]},
    {"label": "Antibiotic R&D (GARDP, CARB-X)", "cat": "amr", "urls": [
        gnews('GARDP OR CARB-X OR "AMR Action Fund" antibiotic')]},
    {"label": "ReAct", "cat": "amr", "urls": [
        "https://www.reactgroup.org/feed/",
        gnews("ReAct antibiotic resistance")]},

    # --- Neglected tropical diseases ------------------------------------------
    {"label": "PLOS NTDs", "cat": "ntd", "urls": [
        "https://journals.plos.org/plosntds/feed/atom"]},
    {"label": "NTD news", "cat": "ntd", "urls": [
        gnews('"neglected tropical diseases" OR schistosomiasis OR trachoma OR onchocerciasis OR leishmaniasis OR "sleeping sickness" OR "lymphatic filariasis"')]},
    {"label": "WHO NTDs", "cat": "ntd", "urls": [
        gnews('WHO "neglected tropical disease" eliminated OR elimination')]},
    {"label": "DNDi", "cat": "ntd", "urls": [
        "https://dndi.org/feed/",
        gnews("DNDi drugs for neglected diseases")]},
    {"label": "Uniting to Combat NTDs", "cat": "ntd", "urls": [
        "https://unitingtocombatntds.org/en/feed/",
        gnews('"Uniting to Combat" NTDs')]},

    # --- Pandemic agreement, PABS, IHR -----------------------------------------
    {"label": "Pandemic Agreement", "cat": "pandemic", "urls": [
        gnews('"pandemic agreement" WHO')]},
    {"label": "PABS / IGWG", "cat": "pandemic", "urls": [
        gnews('PABS pathogen access benefit sharing OR IGWG pandemic')]},
    {"label": "IHR amendments", "cat": "pandemic", "urls": [
        gnews('"International Health Regulations" amendments OR "pandemic emergency"')]},
    {"label": "Pandemic Fund & financing", "cat": "pandemic", "urls": [
        gnews('"Pandemic Fund" OR "pandemic preparedness" financing WHO')]},

    {"label": "The Lancet", "cat": "journal", "urls": [
        "https://www.thelancet.com/rssfeed/lancet_online.xml"]},
    {"label": "Lancet Global Health", "cat": "journal", "urls": [
        "https://www.thelancet.com/rssfeed/langlo_online.xml"]},
    {"label": "BMJ", "cat": "journal", "urls": [
        "https://feeds.bmj.com/bmj/recent",
        "https://www.bmj.com/rss/recent.xml",
        gnews("site:bmj.com research")]},
    {"label": "BMJ Global Health", "cat": "journal", "urls": [
        "https://gh.bmj.com/rss/current.xml",
        "https://feeds.bmj.com/bmjgh/recent",
        gnews("site:gh.bmj.com")]},
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
    # Gates Foundation Grand Challenges: geen eigen RSS, dus via Google News
    {"label": "Grand Challenges", "cat": "news", "urls": [
        gnews("site:grandchallenges.org"),
        gnews('"Grand Challenges" Gates Foundation')]},
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
    "Devex Global Health": 2, "ECDC": 2, "ECDC Epidemiological updates": 3, "ECDC Threats report": 3,
    "ECDC Risk assessments": 3, "Eurosurveillance": 2,
    "Politico EU Health": 2, "Euractiv Health": 2, "CIDRAP Public Health": 2,
    "CIDRAP Pandemic Influenza": 3,
    "CIDRAP Stewardship": 2, "ProMED": 2, "AMR Industry / news": 2,
    "The Lancet": 1, "BMJ": 1, "STAT News": 1,
    "Grand Challenges": 2,
    "AMR policy & research": 2, "AMR global governance": 3, "Antibiotic R&D (GARDP, CARB-X)": 2, "ReAct": 3,
    "PLOS NTDs": 2, "NTD news": 2, "WHO NTDs": 3, "DNDi": 3, "Uniting to Combat NTDs": 3,
    "Pandemic Agreement": 4, "PABS / IGWG": 4, "IHR amendments": 3, "Pandemic Fund & financing": 3,
}

TERMEN = {
    # kern van de portefeuille
    5: ["pandemic agreement", "pandemic treaty", "pabs", "igwg", "pandemisch",
        "international health regulations", "world health assembly", "global health",
        "pandemic fund", "pathogen access", "neglected tropical"],
    3: ["who ", "world health organization", "antimicrobial resistance", " amr ",
        "pandemic preparedness", "health security", "health emergency", "outbreak",
        "one health", "universal health coverage", "health financing",
        "global fund", "gavi", "unitaid", "wto trips", "equity", "low- and middle-income"],
    2: ["vaccine", "epidemic", "surveillance", "cholera", "mpox", "measles", "polio",
        "ebola", "influenza", "tuberculosis", "malaria", "hiv", "climate and health",
        "africa cdc", "ecdc", "hera", "eu health", "european commission",
        "schistosomiasis", "trachoma", "onchocerciasis", "river blindness", "leishmaniasis",
        "sleeping sickness", "lymphatic filariasis", "chagas", "snakebite", "leprosy", "yaws",
        "superbug", "drug-resistant", "antibiotic",
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


# ---------------------------------------------------------------------------
# Linkhygiëne. De Google Alerts-feeds halen het open web binnen, en daar zit
# af en toe spam, clickbait of een link met een (tijdelijke) sleutel erin.
#   1. Altijd weg: rare schema's, IP-adressen, inloggegevens in de URL,
#      link-shorteners, goedkope spam-TLD's, pre-signed cloud-URL's (die
#      bevatten een token en zijn na een uur dood) en de BLOKLIJST.
#   2. Alleen bij open-web-feeds: social media weg (behalve LinkedIn), en
#      onbekende domeinen zakken VERTROUWD_STRAF punten in de ranking.
# Een domein vertrouwen of blokkeren = één regel in de lijsten hieronder.
# Matching is op achtervoegsel: "who.int" dekt ook "emro.who.int".
# ---------------------------------------------------------------------------
OPEN_WEB_FEEDS = {"WHO Alert", "Health Emergency", "Global Health"}

BLOKLIJST = {
    "kalkine.com.au", "kalkinemedia.com", "goal.com", "unilad.com",
    "tradingview.com", "indeed.com", "apps.apple.com",
}

SOCIAL = {
    "facebook.com", "fb.com", "instagram.com", "x.com", "twitter.com",
    "youtube.com", "youtu.be", "tiktok.com", "threads.net", "reddit.com",
    "pinterest.com", "bsky.app", "t.me", "telegram.me", "whatsapp.com",
}  # LinkedIn mag blijven: daar posten instituten zelf

SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "ow.ly", "buff.ly", "rebrand.ly",
    "is.gd", "cutt.ly", "shorturl.at", "tiny.cc", "lnkd.in", "goo.gl",
}

SPAM_TLDS = {
    "xyz", "top", "click", "loan", "work", "gq", "tk", "ml", "cf", "ga",
    "icu", "buzz", "cam", "rest", "monster", "cfd", "sbs", "quest", "bond",
    "zip", "mov", "win", "bid", "stream", "download", "racing", "review",
}

VERTROUWD = {
    # VN, overheden, EU, kennisinstituten
    "who.int", "un.org", "unicef.org", "unaids.org", "worldbank.org", "imf.org",
    "europa.eu", "ecdc.europa.eu", "cdc.gov", "nih.gov", "rivm.nl", "rijksoverheid.nl",
    "africacdc.org", "gavi.org", "theglobalfund.org", "unitaid.org", "cepi.net",
    "gatesfoundation.org", "wellcome.org", "msf.org", "icrc.org", "ifrc.org",
    "reliefweb.int", "paho.org", "chp.gov.hk", "nhs.uk", "gov.uk",
    # wetenschap
    "thelancet.com", "bmj.com", "nature.com", "science.org", "nejm.org",
    "jamanetwork.com", "plos.org", "cell.com", "sciencedirect.com", "springer.com",
    "wiley.com", "frontiersin.org", "mdpi.com", "dovepress.com", "medrxiv.org",
    "biorxiv.org", "eurosurveillance.org", "sciencedaily.com", "news-medical.net",
    "medicalxpress.com", "cidrap.umn.edu", "promedmail.org", "thinkglobalhealth.org",
    # nieuws
    "reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "theguardian.com",
    "nytimes.com", "washingtonpost.com", "ft.com", "economist.com", "bloomberg.com",
    "politico.eu", "politico.com", "euractiv.com", "devex.com", "statnews.com",
    "npr.org", "aljazeera.com", "france24.com", "dw.com", "lemonde.fr",
    "telegraph.co.uk", "independent.co.uk", "cbc.ca", "abc.net.au", "news.sky.com",
    "theglobeandmail.com", "smithsonianmag.com", "time.com", "axios.com",
    "healthpolicy-watch.news", "genevahealthfiles.com", "nos.nl", "nrc.nl",
    "volkskrant.nl", "trouw.nl", "scmp.com", "thehindu.com", "indianexpress.com",
    "timesofindia.indiatimes.com", "hindustantimes.com", "thejakartapost.com",
    "tempo.co", "dailymaverick.co.za", "news24.com", "nation.africa",
    "theeastafrican.co.ke", "allafrica.com", "chinadaily.com.cn", "kff.org",
    "kffhealthnews.org", "fiercepharma.com", "firstwordpharma.com", "endpts.com",
    "linkedin.com", "outbreaknewstoday.substack.com", "ourworldindata.org",
    "pbs.org", "doctorswithoutborders.org", "eurekalert.org", "medscape.com",
    "medpagetoday.com", "academic.oup.com", "royalsocietypublishing.org",
    "the-scientist.com", "passblue.com", "annalsofglobalhealth.org", "ijhpm.com",
    "nigeriahealthwatch.com", "thetimes.com", "observer.co.uk", "ctvnews.ca",
    "usnews.com", "iol.co.za", "deccanherald.com", "gulfnews.com",
    "koreajoongangdaily.com", "projecthope.org", "directrelief.org", "oas.org",
    "ejiltalk.org", "theregreview.org", "cureus.com", "pfizer.com", "astrazeneca.com",
}

VERTROUWD_STRAF = 3   # heft precies het brongewicht van de Alerts-feeds op
_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$|^\[?[0-9a-f:]+\]?$")
_PRESIGNED = ("x-amz-signature", "x-amz-security-token", "x-amz-credential",
              "x-goog-signature", "x-goog-credential", "googleaccessid=",
              "sig=", "se=")      # laatste twee: Azure SAS-tokens (samen met sv=)


def host_van(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _in(host: str, lijst) -> bool:
    return any(host == d or host.endswith("." + d) for d in lijst)


def link_oordeel(url: str, open_web: bool) -> tuple[bool, int, str]:
    """(houden?, strafpunten, reden). Reden alleen gevuld bij weggooien."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False, 0, "onleesbare URL"
    host = host_van(url)
    q = (p.query or "").lower()
    if p.scheme not in ("http", "https") or not host:
        return False, 0, "geen http(s)"
    if "@" in p.netloc:
        return False, 0, "inloggegevens in URL"
    if _IP.match(host):
        return False, 0, "IP-adres"
    if any(k in q for k in _PRESIGNED[:6]) or ("sig=" in q and "sv=" in q):
        return False, 0, "pre-signed URL met token"
    if _in(host, BLOKLIJST):
        return False, 0, "bloklijst"
    if _in(host, SHORTENERS):
        return False, 0, "link-shortener"
    if host.rsplit(".", 1)[-1] in SPAM_TLDS:
        return False, 0, "spam-TLD"
    if open_web:
        if _in(host, SOCIAL):
            return False, 0, "social media"
        overheid = re.search(r"(^|\.)(gov|gouv|go|gob|govt|int|edu|ac)(\.[a-z]{2})?$", host)
        if not (_in(host, VERTROUWD) or overheid):
            return True, VERTROUWD_STRAF, ""
    return True, 0, ""


def iso(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return ""


_laatste_hit: dict = {}          # domein -> tijdstip laatste request
HOST_PAUZE = 3.0                 # standaardpauze tussen requests naar hetzelfde domein
DOMEIN_PAUZE = {                 # uitzonderingen voor strenge uitgevers
    "bmj.com": 15.0,             # limiteert over al hun subdomeinen samen
}


def domein(url: str) -> str:
    """www.bmj.com en gh.bmj.com zijn voor rate limiting hetzelfde domein.
    Daarom knippen we terug naar de laatste twee labels."""
    host = urllib.parse.urlparse(url).netloc.lower()
    delen = host.split(".")
    return ".".join(delen[-2:]) if len(delen) >= 2 else host


def download(url: str) -> bytes:
    """Haalt één URL op. Respecteert een pauze per domein, want BMJ gaf 429
    toen we www.bmj.com en gh.bmj.com vlak achter elkaar aanriepen -- die
    delen één rate limit."""
    host = domein(url)
    pauze = DOMEIN_PAUZE.get(host, HOST_PAUZE)
    wacht = pauze - (time.monotonic() - _laatste_hit.get(host, 0))
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
                # Google News hangt " - Uitgever" achter elke kop. De bron
                # staat al apart in het <source>-element, dus knip dat eraf.
                bron_el = e.get("source", {}).get("title", "")
                if bron_el and title.endswith(" - " + bron_el):
                    title = title[: -(len(bron_el) + 3)].strip()
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
                    "domain":  host_van(e.get("source", {}).get("href") or "")
                               if "news.google.com" in link else host_van(link),
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


def titel_sleutel(titel: str) -> str:
    """Genormaliseerde titel om hetzelfde bericht onder een andere URL te
    herkennen (WHO.int vs. ReliefWeb vs. Google News). Een eventuele
    ' - Bronnaam' of ' | Bronnaam' aan het eind valt weg."""
    t = (titel or "").lower()
    t = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,60}$", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


def eerste_publicatie() -> dict:
    """Vroegste publicatiedatum per titel uit het archief. Zo komt een bericht
    dat dagen later onder een nieuwe URL opduikt niet opnieuw als 'vers' bovenaan."""
    oudste = {}
    if not ARCHIVE.exists():
        return oudste
    try:
        with ARCHIVE.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sleutel = titel_sleutel(row.get("titel", ""))
                dag = (row.get("gepubliceerd") or "")[:10]
                if len(sleutel) < 25 or len(dag) != 10:
                    continue
                iso_dag = dag + "T00:00:00+00:00"
                if sleutel not in oudste or iso_dag < oudste[sleutel]:
                    oudste[sleutel] = iso_dag
    except Exception as e:
        print(f"  archief niet bruikbaar voor ontdubbelen: {e}", file=sys.stderr)
    return oudste


def ontdubbel_op_titel(items: list, oudste: dict) -> list:
    """Voegt items met dezelfde titel samen. Het resultaat krijgt de vroegste
    bekende publicatiedatum en de hoogste score van de groep. Titels korter dan
    25 tekens (na normaliseren) worden nooit samengevoegd."""
    uit, per_sleutel = [], {}
    for item in items:
        item = dict(item)                     # kopie: het archief houdt zijn eigen data
        sleutel = titel_sleutel(item.get("title", ""))
        if len(sleutel) < 25:
            uit.append(item)
            continue
        vroegste = oudste.get(sleutel)
        if vroegste and (not item.get("date") or vroegste < item["date"]):
            item["date"] = vroegste
        if sleutel not in per_sleutel:
            per_sleutel[sleutel] = item
            uit.append(item)
            continue
        bewaard = per_sleutel[sleutel]
        if item.get("date") and (not bewaard.get("date") or item["date"] < bewaard["date"]):
            bewaard["date"] = item["date"]
        if (item.get("score") or 0) > (bewaard.get("score") or 0):
            bewaard["score"] = item["score"]
    return uit


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

    schoon, weg = [], {}
    for item in merged:
        open_web = item.get("feed") in OPEN_WEB_FEEDS
        houden, straf, reden = link_oordeel(item["url"], open_web)
        if not houden:
            weg[reden] = weg.get(reden, 0) + 1
            continue
        item = dict(item)
        item.setdefault("domain", host_van(item["url"]))
        item["score"] = score_item(item, item.get("feed", "")) - straf
        schoon.append(item)
    if weg:
        print("  weggefilterd: " + ", ".join(f"{n}x {r}" for r, n in sorted(weg.items())))
    merged = schoon
    fresh = [i for i in fresh if link_oordeel(i["url"], i.get("feed") in OPEN_WEB_FEEDS)[0]]

    merged = ontdubbel_op_titel(merged, eerste_publicatie())

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    def recent(i):
        if not i.get("date"):
            return True                      # datum onbekend: laten staan
        try:
            grens = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_PER_CAT.get(i.get("cat"), MAX_AGE_DAYS))
            return datetime.fromisoformat(i["date"]) >= grens
        except Exception:
            return True

    merged = [i for i in merged if recent(i)]
    merged.sort(key=lambda i: i.get("date") or "", reverse=True)
    # De bovengrens mag de trage onderwerpen niet wegdrukken: die tellen apart.
    traag = [i for i in merged if i.get("cat") in MAX_AGE_PER_CAT]
    rest = [i for i in merged if i.get("cat") not in MAX_AGE_PER_CAT]
    merged = sorted(traag + rest[:max(0, MAX_TOTAL - len(traag))],
                    key=lambda i: i.get("date") or "", reverse=True)

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
