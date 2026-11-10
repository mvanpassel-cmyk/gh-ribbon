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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import unescape

import feedparser

OUT           = pathlib.Path("data/feeds.json")
ARCHIVE_DIR   = pathlib.Path("data/archive")      # één CSV per maand (YYYY-MM.csv), opent in Excel/Numbers
ARCHIVE_INDEX = ARCHIVE_DIR / "index.json"        # lijst van maanden; de pagina laadt alleen wat nodig is
ARCHIVE_COLS  = ["seen_date", "published", "category", "source", "feed", "title", "url"]
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
# Tags waarop we de algemene feeds van Politico en Euractiv filteren (deel van het woord volstaat).
GEZONDHEIDSTAGS = ["health", "pharma", "medicin", "vaccin", "pandemic", "antimicrobial", "disease",
                   "cancer", "hera", "ema ", "drug", "hospital", "epidemic", "outbreak", "mpox", "ebola"]

FEEDS = [
    # --- WHO ----------------------------------------------------------------
    # De RSS van WHO-nieuws serveert sinds een tijd stukken uit 2020 (die vallen
    # weg op leeftijd); de API die who.int zelf gebruikt is wel actueel.
    {"label": "WHO News", "cat": "who", "urls": [
        {"api": "who", "basis": "https://www.who.int/news/item",
         "url": "https://www.who.int/api/news/newsitems?sf_culture=en&$orderby=PublicationDateAndTime%20desc&$top=12"},
        "https://www.who.int/rss-feeds/news-english.xml"]},
    {"label": "WHO Disease Outbreak News", "cat": "who", "urls": [
        # De DON-RSS geeft 404 (gemeten 10-11-2026); de API werkt.
        {"api": "who", "basis": "https://www.who.int/emergencies/disease-outbreak-news/item",
         "url": "https://www.who.int/api/news/diseaseoutbreaknews?sf_culture=en&$orderby=PublicationDateAndTime%20desc"
                "&$top=12&$select=Title,OverrideTitle,UseOverrideTitle,ItemDefaultUrl,UrlName,PublicationDateAndTime,Summary"},
        "https://www.who.int/rss-feeds/disease-outbreak-news-english.xml",
        gnews("site:who.int disease outbreak news")]},
    {"label": "WHO Alert", "cat": "who", "urls": [
        "https://www.google.com/alerts/feeds/07358569115421849873/153340034560442073"]},
    {"label": "UN News Health", "cat": "who", "urls": [
        "https://news.un.org/feed/subscribe/en/news/topic/health/feed/rss.xml",
        gnews("site:news.un.org health")]},

    # --- Outbreak / surveillance --------------------------------------------
    # ProMED: geen feed meer sinds de nieuwe (betaalde) site; /feed/ geeft 404
    # (gemeten 10-11-2026). De Google News-zoekopdracht "ProMED-mail outbreak"
    # leverde stukken óver ProMED van willekeurige sites. Daarom uit.
    {"label": "CIDRAP Public Health", "cat": "promed", "urls": [
        "https://www.cidrap.umn.edu/news/91/rss"]},          # geverifieerd
    {"label": "CIDRAP Pandemic Influenza", "cat": "promed", "urls": [
        "https://www.cidrap.umn.edu/news/86/rss"]},          # geverifieerd
    {"label": "Eurosurveillance", "cat": "promed", "urls": [
        # eurosurveillance.org zit achter een Cloudflare-uitdaging; Crossref heeft
        # elk nieuw artikel binnen een dag (ISSN 1560-7917).
        {"api": "crossref", "basis": "https://www.eurosurveillance.org/content/",
         "url": "https://api.crossref.org/journals/1560-7917/works?sort=published&order=desc&rows=12"
                "&select=DOI,title,published,issued,abstract"},
        gnews("site:eurosurveillance.org")]},

    # --- Health emergencies ---------------------------------------------------
    {"label": "Health Emergency", "cat": "emerg", "urls": [
        "https://www.google.com/alerts/feeds/07358569115421849873/3291854287981540699"]},
    {"label": "ReliefWeb Health", "cat": "emerg", "urls": [
        # De RSS geeft een lege 202 (botuitdaging); de API v2 wil een goedgekeurde
        # appname, als secret RELIEFWEB_APPNAME. Tot die er is: Google News.
        {"api": "reliefweb",
         "url": "https://api.reliefweb.int/v2/reports?appname={appname}&preset=latest&limit=12"
                "&filter[field]=theme.name&filter[value]=Health"
                "&fields[include][]=url_alias&fields[include][]=date.original&fields[include][]=source.shortname"},
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
        "https://africacdc.org/news-item/feed/",       # /news/feed/ en /feed/ zijn leeg
        gnews("site:africacdc.org")]},
    {"label": "Think Global Health", "cat": "global", "urls": [
        # Geen RSS meer (alle feed-adressen 404, gemeten 10-11-2026).
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
        # De RSS en de WordPress-API zijn leeg (gemeten 10-11-2026).
        gnews("site:reactgroup.org")]},

    # --- Neglected tropical diseases ------------------------------------------
    {"label": "PLOS NTDs", "cat": "ntd", "urls": [
        "https://journals.plos.org/plosntds/feed/atom"]},
    {"label": "NTD news", "cat": "ntd", "urls": [
        gnews('"neglected tropical diseases" OR schistosomiasis OR trachoma OR onchocerciasis OR leishmaniasis OR "sleeping sickness" OR "lymphatic filariasis"')]},
    {"label": "WHO NTDs", "cat": "ntd", "urls": [
        gnews('WHO "neglected tropical disease" eliminated OR elimination')]},
    {"label": "DNDi", "cat": "ntd", "urls": [
        {"api": "wp", "timeout": 60, "url": "https://dndi.org/wp-json/wp/v2/news?per_page=12&_fields=title,link,excerpt,date_gmt"},   # /feed/ is leeg
        gnews("site:dndi.org")]},
    {"label": "Uniting to Combat NTDs", "cat": "ntd", "urls": [
        # Alles achter een 403 voor datacenter-IP's (gemeten 10-11-2026).
        gnews("site:unitingtocombatntds.org")]},

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
        # Sinds maart 2026 op Ghost (newsletter.genevahealthfiles.com); Substack blokkeert.
        "https://newsletter.genevahealthfiles.com/rss/",
        gnews("site:genevahealthfiles.com")]},
    {"label": "Politico EU Health", "cat": "policy", "urls": [
        # De sectiefeed geeft HTML, ?cat=96 een Cloudflare-uitdaging. De hoofdfeed
        # werkt; daaruit alleen de gezondheidsstukken.
        {"url": "https://www.politico.eu/feed/", "tags": GEZONDHEIDSTAGS},
        gnews("site:politico.eu health")]},
    # Euractiv heet sinds eind september 2026 Rapporteur (rapporteur.com); de oude
    # hoofdfeed op euractiv.com levert die stukken. Sectiefeeds: Cloudflare-uitdaging.
    {"label": "Rapporteur (ex-Euractiv)", "cat": "policy", "urls": [
        {"url": "https://www.euractiv.com/feed/", "tags": GEZONDHEIDSTAGS},
        gnews("site:euractiv.com health")]},

    # --- News ------------------------------------------------------------------------------------
    {"label": "STAT News", "cat": "news", "urls": [
        "https://www.statnews.com/feed/"]},
    {"label": "Devex Global Health", "cat": "news", "urls": [
        "https://www.devex.com/news/feed.rss",          # /news/feed zit achter een captcha
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
    "Devex Global Health": 2, "ECDC": 3, "ECDC Epidemiological updates": 3, "ECDC Threats report": 3,
    "ECDC Risk assessments": 3, "Eurosurveillance": 2,
    "Politico EU Health": 2, "Rapporteur (ex-Euractiv)": 2, "CIDRAP Public Health": 2,
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
        "pandemic fund", "pathogen access", "neglected tropical",
        # paraatheidsbeoordelingen (JEE, SPAR, PHEPA, IHR benchmarks): eigen tools in de toolbox
        "preparedness assessment", "phepa", "joint external evaluation", "states parties self-assessment",
        "ihr benchmark", "emergency preparedness", "preparedness and response"],
    3: ["who ", "world health organization", "antimicrobial resistance", " amr ",
        "pandemic preparedness", "health security", "health emergency", "outbreak",
        "one health", "universal health coverage", "health financing",
        "global fund", "gavi", "unitaid", "wto trips", "equity", "low- and middle-income"],
    2: ["vaccine", "epidemic", "surveillance", "cholera", "mpox", "measles", "polio",
        "ebola", "influenza", "tuberculosis", "malaria", "hiv", "climate and health",
        "africa cdc", "ecdc", "hera", "eu health", "european commission",
        "schistosomiasis", "trachoma", "onchocerciasis", "river blindness", "leishmaniasis",
        "sleeping sickness", "lymphatic filariasis", "chagas", "snakebite", "leprosy", "yaws",
        "superbug", "drug-resistant", "antibiotic", "climate",
        "indonesia", "south africa", "kenya", "india", "china"],
    # internationale reikwijdte: daar gaat het om
    1: ["international", "worldwide", "cross-border", "multilateral", "countries", "regional",
        "united nations", " un ", "g20", "g7", "african union", "member states", "global"],
    # ruis: beroemdheden, entertainment, sport en lokale incidenten zonder internationale impact
    -8: ["singer", "actress", "actor", "rapper", "celebrity", "pop star", "rock star", "tour dates",
         "concert", "cancels shows", "postpone shows", "hospitalized", "hospitalised", "rushed to hospital",
         "plane incident", "arrested", "arrest", "page six", "tmz", "royal family", "footballer",
         "nfl", "nba", "premier league", "jet lag", "horoscope", "recipe",
         # ruis: studiegidsen, vacatures en archiefstukken ("On this day") zonder nieuwswaarde
         "unit of study", "course outline", "course code", "syllabus", "postgraduate", "undergraduate",
         "master of", "tuition", "scholarship", "job opening", "internship",
         "on this day", "from the archives", "scmp archive", "years ago today",
         # lokale meldingen zonder internationale impact
         "possible measles exposure", "measles exposure reported", "measles exposure at"],
    # ruis: klinisch/commercieel VS-nieuws dat hier zelden toe doet
    -3: ["earnings", "ipo", "stock", "shares", "acquisition", "medicare",
         "medicaid", "obesity drug", "series a", "series b", "funding round"],
}


MIN_INHOUD_OPEN_WEB = 4   # minstens een kernwoord (gewicht 3) of twee lichtere

# Vacatures en persoonsprofielen (bio's, staff-pagina's) zijn geen nieuws. Ze zakken
# PROFIEL_STRAF punten, behalve een bio met nieuwswaarde: een benoeming, verkiezing of
# vertrek (bijv. de nieuwe DG van de WHO).
PROFIEL_STRAF = 20
PROFIEL_PAD = re.compile(r"/(people|person|profiles?|staff|faculty|our-team|team|members?|experts?|"
                         r"directory|bios?|biography|speakers?)/", re.I)
VACATURE_PAD = re.compile(r"/(jobs?|careers?|vacanc(?:y|ies)|vacatures?|recruitment|job-?openings?)(?:/|$|[?#])", re.I)
BIO_TITEL = re.compile(r"^(?:Dr\.? |Prof\.? )?[A-Z][a-z][\w.'’-]*(?: [A-Z][\w.'’-]*){1,4},\s*"
                       r"(?:MD|PhD|MPH|MSc|MS|DrPH|MBBS|FRCP|RN|PharmD|DVM)\b")
VACATURE_WOORDEN = ["vacature", "we are hiring", "we're hiring", "job description",
                    "apply now", "call for applications", "position available", "job posting",
                    "career opportunity", "postdoctoral position", "phd position"]
BIO_WOORDEN = ["curriculum vitae", "biography", "faculty profile", "staff profile", "meet our", "about the author"]
NIEUWS_OVER_PERSOON = ["director-general", "director general", "appointed", "appoints", "elected", "elects",
                       "nominated", "nominee", "named", "names", "new head", "takes office", "assumes", "steps down", "resigns",
                       "dies", "died", "obituary"]


def profiel_of_vacature(item) -> bool:
    """Een vacature of een persoonsprofiel zonder nieuwswaarde?"""
    titel = item.get("title") or ""
    tl, tekst = titel.lower(), (item.get("summary") or "").lower()
    pad = urllib.parse.urlparse(item.get("url") or "").path
    nieuws = any(w in tl or w in tekst for w in NIEUWS_OVER_PERSOON)
    if any(w in tl for w in VACATURE_WOORDEN):
        return True
    nieuwspad = re.search(r"/(?:news|events?|press|stories|articles?)/", pad, re.I)   # bijv. /staff/events/...
    if VACATURE_PAD.search(pad) or (PROFIEL_PAD.search(pad) and not nieuwspad) or BIO_TITEL.match(titel) \
            or any(w in tl for w in BIO_WOORDEN):
        return not nieuws
    return False


def score_item(item, feed_label: str) -> int:
    # Een officiële bron die op Google News terugviel is een zoekopdracht
    # geworden: dan geen bonus voor de naam van die bron.
    s = 1 if item.get("fallback") else BRONGEWICHT.get(feed_label, 1)
    if profiel_of_vacature(item):
        s -= PROFIEL_STRAF
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
# Google News-links. Een Google News-feed geeft geen artikel-URL maar een
# versleutelde omleiding (news.google.com/rss/articles/CBMi...). Oudere ids
# bevatten de echte URL gewoon in de base64; nieuwere ("AU_yqL...") niet, en
# dan vraagt Google News het zelf op bij zijn eigen batchexecute-endpoint.
# Dat kost twee requests per link, dus: alleen voor nieuwe links, een plafond
# per run, en alles wat gelukt is komt in een cache (data/gnews-urls.json).
# ---------------------------------------------------------------------------
GNEWS_CACHE = pathlib.Path("data/gnews-urls.json")
GNEWS_MAX_PER_RUN = 60
GNEWS_PAUZE = 1.0
_GN_ID = re.compile(r"news\.google\.com/(?:rss/)?(?:articles|read)/([A-Za-z0-9_-]+)")


def _gn_base64(gn_id: str) -> str:
    """Oud formaat: de URL staat letterlijk in de protobuf."""
    import base64
    try:
        ruw = base64.urlsafe_b64decode(gn_id + "=" * (-len(gn_id) % 4))
    except Exception:
        return ""
    m = re.search(rb"https?://[\x21-\x7e]+", ruw)
    if not m or m.group(0).startswith(b"https://news.google.com"):
        return ""
    url = m.group(0).decode("ascii", "ignore")
    # In de protobuf volgt soms direct een lengte-byte met nog een AMP-URL erachter.
    return re.split(r"[\x00-\x1f]|\xd2\x01", url)[0]


def _gn_batchexecute(gn_id: str) -> str:
    """Nieuw formaat: handtekening en tijdstempel van de artikelpagina halen
    en daarmee de echte URL opvragen."""
    req = urllib.request.Request(f"https://news.google.com/rss/articles/{gn_id}",
                                 headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        html = r.read().decode("utf-8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not (sg and ts):
        return ""
    verzoek = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
               '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]' % (gn_id, ts.group(1), sg.group(1)))
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", verzoek, None, "generic"]]]))
    req = urllib.request.Request("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                                 data=body.encode(), headers={
                                     "User-Agent": UA,
                                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        tekst = r.read().decode("utf-8", "replace")
    deel = json.loads(tekst.split("\n\n", 1)[1])
    url = json.loads(deel[0][2])[1]
    return url if isinstance(url, str) and url.startswith("http") else ""


def los_gnews_op(url: str) -> str:
    """Echte artikel-URL achter een Google News-link, of "" als dat niet lukt."""
    m = _GN_ID.search(url or "")
    if not m:
        return ""
    gevonden = _gn_base64(m.group(1))
    if gevonden:
        return gevonden
    try:
        return _gn_batchexecute(m.group(1))
    except Exception:
        return ""


def laad_gnews_cache() -> dict:
    try:
        return json.loads(GNEWS_CACHE.read_text())
    except Exception:
        return {}


def los_gnews_links_op(items: list, cache: dict) -> tuple[int, int]:
    """Vervangt Google News-links door de echte URL (in place). Geeft
    (opgelost uit cache of nieuw, mislukt) terug. Een mislukte link blijft
    gewoon de Google-link: die werkt in de browser ook."""
    nieuw = mislukt = 0
    vandaag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for item in items:
        url = item.get("url") or ""
        if "news.google.com" not in url:
            continue
        hit = cache.get(url)
        if hit is None and nieuw + mislukt < GNEWS_MAX_PER_RUN:
            echt = los_gnews_op(url)
            time.sleep(GNEWS_PAUZE)
            hit = cache[url] = {"url": echt, "seen": vandaag}
            if echt:
                nieuw += 1
            else:
                mislukt += 1
        if hit:
            hit["seen"] = vandaag
            if hit.get("url") and not hit["url"].startswith("https://news.google.com"):
                item["gnews"] = url
                item["url"] = hit["url"]
                item["domain"] = host_van(hit["url"])
    return nieuw, mislukt


def bewaar_gnews_cache(cache: dict) -> None:
    """Houdt alleen links die de afgelopen 30 dagen nog langskwamen. Mislukte
    pogingen (lege url) gaan na 3 dagen weg, zodat ze opnieuw geprobeerd worden."""
    nu = datetime.now(timezone.utc)
    def vers(v, dagen):
        try:
            return nu - datetime.fromisoformat(v["seen"] + "T00:00:00+00:00") <= timedelta(days=dagen)
        except Exception:
            return False
    schoon = {k: v for k, v in cache.items() if vers(v, 30 if v.get("url") else 3)}
    GNEWS_CACHE.write_text(json.dumps(schoon, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


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
}  # LinkedIn mag blijven (daar posten instituten zelf), maar zakt: zie LAGE_WAARDE

# Domeinen die mogen blijven maar flink zakken in de ranking: meestal persoonlijke
# posts, zelden nieuws. Een sterk relevant bericht kan nog steeds de top 30 halen.
LAGE_WAARDE = {"linkedin.com": 8}

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
    "politico.eu", "politico.com", "euractiv.com", "rapporteur.com", "devex.com", "statnews.com",
    "npr.org", "aljazeera.com", "france24.com", "dw.com", "lemonde.fr",
    "telegraph.co.uk", "independent.co.uk", "cbc.ca", "abc.net.au", "news.sky.com",
    "theglobeandmail.com", "smithsonianmag.com", "time.com", "axios.com",
    "healthpolicy-watch.news", "genevahealthfiles.com", "nos.nl", "nrc.nl",
    "volkskrant.nl", "trouw.nl", "scmp.com", "thehindu.com", "indianexpress.com",
    "timesofindia.indiatimes.com", "hindustantimes.com", "thejakartapost.com",
    "tempo.co", "dailymaverick.co.za", "news24.com", "nation.africa",
    "theeastafrican.co.ke", "allafrica.com", "chinadaily.com.cn", "kff.org",
    "kffhealthnews.org", "fiercepharma.com", "firstwordpharma.com", "endpts.com",
    "outbreaknewstoday.substack.com", "ourworldindata.org",
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
    pad = (p.path or "").lower()
    if re.search(r"/(sponsored|partner|promoted|paid)-?(content|post)s?/", pad):
        return False, 0, "gesponsorde inhoud"
    jaar = re.search(r"/(?:year-)?((?:19|20)\d\d)/", pad)
    if jaar and int(jaar.group(1)) < datetime.now(timezone.utc).year - 1:
        return False, 0, "oud artikel (jaartal in URL)"
    if open_web:
        if _in(host, SOCIAL):
            return False, 0, "social media"
        for d, straf in LAGE_WAARDE.items():
            if _in(host, [d]):
                return True, straf, ""
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


def download(url: str, timeout: int = 0) -> bytes:
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
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
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


def is_gnews(kandidaat) -> bool:
    url = kandidaat if isinstance(kandidaat, str) else kandidaat.get("url", "")
    return url.startswith("https://news.google.com/rss/search")


def gnews_site(kandidaat) -> str:
    """Het domein uit een 'site:'-zoekopdracht, of ""."""
    url = kandidaat if isinstance(kandidaat, str) else kandidaat.get("url", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
    m = re.search(r"\bsite:([a-z0-9.-]+)", q, re.I)
    return m.group(1).lower().removeprefix("www.") if m else ""


# ---------------------------------------------------------------------------
# Bronnen zonder (werkende) RSS: een paar officiële API's. Elke adapter geeft
# dezelfde "entries" terug als feedparser: dicts met title, link, summary,
# date (iso) en eventueel tags. Een kandidaat is dan een dict, bijvoorbeeld
# {"api": "wp", "url": "https://dndi.org/wp-json/wp/v2/news?per_page=12"}.
# ---------------------------------------------------------------------------
def _json(url: str, timeout: int = 0):
    return json.loads(download(url, timeout))


def api_who(kandidaat) -> list:
    """WHO's eigen Sitefinity-API (dezelfde die who.int zelf gebruikt)."""
    basis = kandidaat["basis"]
    uit = []
    for x in _json(kandidaat["url"]).get("value", []):
        titel = x.get("OverrideTitle") if x.get("UseOverrideTitle") and x.get("OverrideTitle") else x.get("Title")
        pad = x.get("ItemDefaultUrl") or ("/" + (x.get("UrlName") or ""))
        uit.append({"title": titel or "", "link": basis + pad, "summary": x.get("Summary") or "",
                    "date": (x.get("PublicationDateAndTime") or "").replace("Z", "+00:00")})
    return uit


def api_wp(kandidaat) -> list:
    """WordPress REST-API, voor sites waarvan de RSS leeg of geblokkeerd is."""
    uit = []
    for x in _json(kandidaat["url"], kandidaat.get("timeout", 0)):
        uit.append({"title": (x.get("title") or {}).get("rendered", ""), "link": x.get("link", ""),
                    "summary": (x.get("excerpt") or {}).get("rendered", ""),
                    "date": (x.get("date_gmt") or "") + ("+00:00" if x.get("date_gmt") else "")})
    return uit


def api_crossref(kandidaat) -> list:
    """Nieuwste artikelen van een tijdschrift via Crossref (op ISSN). Voor
    Eurosurveillance, waarvan de site achter een Cloudflare-uitdaging zit."""
    uit = []
    for x in _json(kandidaat["url"]).get("message", {}).get("items", []):
        delen = ((x.get("published") or x.get("issued") or {}).get("date-parts") or [[]])[0]
        datum = "-".join(f"{d:02d}" for d in delen) if len(delen) == 3 else ""
        uit.append({"title": " ".join(x.get("title") or []),
                    "link": kandidaat.get("basis", "https://doi.org/") + x.get("DOI", ""),
                    "summary": x.get("abstract") or "",
                    "date": datum + "T00:00:00+00:00" if datum else ""})
    return uit


def api_reliefweb(kandidaat) -> list:
    """ReliefWeb API v2. Vraagt een goedgekeurde appname (gratis aan te vragen,
    zie README); zonder secret RELIEFWEB_APPNAME slaan we deze kandidaat over."""
    import os
    app = os.environ.get("RELIEFWEB_APPNAME", "").strip()
    if not app:
        raise ValueError("geen RELIEFWEB_APPNAME")
    url = kandidaat["url"].replace("{appname}", urllib.parse.quote(app))
    uit = []
    for x in _json(url).get("data", []):
        f = x.get("fields", {})
        bron = ", ".join(s.get("shortname", "") for s in f.get("source", [])[:2])
        uit.append({"title": f.get("title", ""), "link": f.get("url_alias") or f.get("url", ""),
                    "summary": bron, "date": (f.get("date") or {}).get("original", "")})
    return uit


API = {"who": api_who, "wp": api_wp, "crossref": api_crossref, "reliefweb": api_reliefweb}


def entries_van(kandidaat) -> list:
    if isinstance(kandidaat, dict) and kandidaat.get("api"):
        return API[kandidaat["api"]](kandidaat)
    url = kandidaat if isinstance(kandidaat, str) else kandidaat["url"]
    ruw = download(url)
    parsed = feedparser.parse(ruw)
    if not parsed.entries and b"<html" in ruw[:600].lower():
        raise ValueError("HTML in plaats van een feed")
    uit = []
    for e in parsed.entries or []:
        uit.append({"title": e.get("title", ""), "link": e.get("link") or "",
                    "summary": e.get("summary") or (e.get("content", [{}])[0].get("value") if e.get("content") else ""),
                    "date": iso(e), "tags": [t.get("term", "") for t in e.get("tags", [])],
                    "source": e.get("source", {})})
    return uit


def harvest(feed) -> tuple[list, dict]:
    """Probeert de kandidaten op volgorde. De eerste die bruikbare items
    oplevert wint. Zo overleeft een bron een verhuisde feed-URL, en hebben
    de Cloudflare-sites een Google News-vangnet.

    Geeft (items, status) terug. status["fallback"] is True als een officiële
    bron het niet deed en we op Google News terugvielen: dat telt als een
    zwakke bron (gewicht 1, nooit URGENT) en de pagina toont hem oranje."""
    kandidaten = feed.get("urls") or [feed["url"]]
    zoekbron = is_gnews(kandidaten[0])           # een Google News-zoekopdracht by design
    fouten = []
    for n, kandidaat in enumerate(kandidaten):
        try:
            entries = entries_van(kandidaat)
            if isinstance(kandidaat, dict) and kandidaat.get("tags"):
                # Algemene feed (heel Euractiv, heel Politico): alleen de gezondheidsstukken.
                # Nul treffers is dan gewoon een rustig uur, geen reden om terug te vallen.
                if not any(e.get("tags") for e in entries):
                    raise ValueError("feed heeft geen tags om op te filteren")
                wil = [t.lower() for t in kandidaat["tags"]]
                entries = [e for e in entries if any(w in t.lower() for t in e.get("tags", []) for w in wil)]
                if not entries:
                    return [], {"ok": True, "msg": "0 items (geen gezondheidsstukken dit uur)", "cat": feed["cat"],
                                "via": n + 1, "fallback": False}
            if not entries:
                raise ValueError("0 entries")

            site = gnews_site(kandidaat)
            # Terugval = Google News staat in voor een officiële bron: ofwel na een
            # mislukte eigen feed, ofwel een 'site:'-zoekopdracht op die bron.
            terugval = is_gnews(kandidaat) and (not zoekbron or bool(site))
            items, buiten_site = [], 0
            for e in entries[:MAX_PER_FEED]:
                title = strip_html(e.get("title", ""))
                # Google News hangt " - Uitgever" achter elke kop. De bron
                # staat al apart in het <source>-element, dus knip dat eraf.
                bron = e.get("source") or {}
                bron_el = bron.get("title", "")
                if bron_el and title.endswith(" - " + bron_el):
                    title = title[: -(len(bron_el) + 3)].strip()
                link = unwrap((e.get("link") or "").strip())
                if not title or not link.startswith("http"):
                    continue
                if re.search(r"\[(promoted|sponsored)( content)?\]|^sponsored:", title, re.I):
                    continue
                dom = host_van(bron.get("href") or "") if "news.google.com" in link else host_van(link)
                if site and not _in(dom, [site]):
                    buiten_site += 1              # 'site:who.int' leverde een stuk van elders
                    continue
                if site and len(title.split()) < 5:
                    continue                      # 'Contact us', 'All Jobs', 'Our governance': pagina's, geen nieuws
                src = strip_html(bron_el) if bron_el else feed["label"]
                item = {
                    "title":   title[:180],
                    "url":     link,
                    "src":     src,
                    "feed":    feed["label"],
                    "cat":     feed["cat"],
                    "date":    e.get("date") or "",
                    "summary": strip_html(e.get("summary") or "")[:300],
                    "domain":  dom,
                }
                if terugval:
                    item["fallback"] = True
                item["score"] = score_item(item, feed["label"])
                items.append(item)
            if not items:
                raise ValueError("entries zonder bruikbare titel/link" +
                                 (f" ({buiten_site} van buiten {site})" if buiten_site else ""))

            msg = f"{len(items)} items" + ("" if n == 0 else f" (via kandidaat {n + 1})")
            if buiten_site:
                msg += f", {buiten_site} van buiten {site} weg"
            status = {"ok": True, "msg": msg, "cat": feed["cat"], "via": n + 1, "fallback": terugval}
            if zoekbron and not site:
                status["search"] = True
            if fouten:
                status["errors"] = " | ".join(fouten)[:300]
            return items, status
        except Exception as e:
            fouten.append(f"[{n + 1}] {type(e).__name__}: {e}"[:160])
    raise RuntimeError(" | ".join(fouten))


def load_previous() -> list:
    if not OUT.exists():
        return []
    try:
        return json.loads(OUT.read_text()).get("items", [])
    except Exception:
        return []


def archive_rows():
    """Alle regels uit alle maandbestanden, oudste maand eerst."""
    for pad in sorted(ARCHIVE_DIR.glob("*.csv")):
        try:
            with pad.open(newline="", encoding="utf-8-sig") as f:
                yield from csv.DictReader(f)
        except Exception as e:
            print(f"  {pad} onleesbaar, overgeslagen: {e}", file=sys.stderr)


def write_archive_index() -> None:
    maanden = []
    for pad in sorted(ARCHIVE_DIR.glob("*.csv"), reverse=True):
        with pad.open(newline="", encoding="utf-8-sig") as f:
            rows = max(0, sum(1 for _ in f) - 1)
        maanden.append({"month": pad.stem, "file": pad.name, "rows": rows, "bytes": pad.stat().st_size})
    ARCHIVE_INDEX.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "columns": ARCHIVE_COLS,
        "months": maanden,
    }, indent=1))


def append_archive(items) -> int:
    """Append-only dagboek van alles wat langskwam, één bestand per maand.
    Dedupe op URL over alle maanden, zodat een item dat drie dagen in een feed
    blijft staan maar één regel krijgt. utf-8-sig omdat Excel anders de
    accenten sloopt."""
    seen = {row.get("url", "") for row in archive_rows()}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    nieuw = [i for i in items if i.get("url") and i["url"] not in seen]

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    pad = ARCHIVE_DIR / f"{today[:7]}.csv"
    write_header = not pad.exists()
    with pad.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(ARCHIVE_COLS)
        for i in nieuw:
            w.writerow([today, (i.get("date") or "")[:10], i.get("cat", ""),
                        i.get("src", ""), i.get("feed", ""), i.get("title", ""),
                        i.get("url", "")])
    write_archive_index()
    return len(nieuw)


# ---------------------------------------------------------------------------
# Ontdubbelen. Hetzelfde bericht komt vaak via meerdere feeds binnen, onder
# een andere URL en met een net andere kop: afgekapt ("..."), met " - Uitgever"
# erachter, "U.S." tegen "US", "Congo" tegen "DRC", of in een andere volgorde
# ("WHO chief: X" tegen "X: WHO chief"). Daarom vergelijken we de inhouds-
# woorden van de kop, niet de letterlijke tekst.
# ---------------------------------------------------------------------------
STOPWOORDEN = set("""a an the of in on at to for from by with and or as is are was were be been being
its it this that these those after amid over into than about how what why when will can could may might
would should has have had says said say via per""".split())

LANDNAMEN = [
    (r"\b(?:the )?democratic republic of (?:the )?congo\b|\bdr ?congo\b|\bdrc\b"
     r"|\b(?:the )?republic of (?:the )?congo\b", "congo"),
    (r"\bturkiye\b", "turkey"), (r"\bcote d ?ivoire\b", "ivory coast"), (r"\bkiev\b", "kyiv"),
    (r"\bczech republic\b", "czechia"), (r"\bburma\b", "myanmar"), (r"\bholland\b", "netherlands"),
    (r"\bmonkeypox\b", "mpox"), (r"\bcovid 19\b|\bsars cov 2\b", "covid"),
    (r"\bunited states(?: of america)?\b|\busa\b", "us"),
    (r"\b(?:the )?united kingdom\b|\b(?:great )?britain\b", "uk"),
    (r"\b(?:the )?united nations\b", "un"),
    (r"\b(?:the )?european union\b", "eu"),
    (r"\bworld health organi[sz]ation\b", "who"),
]
# Rangtelwoorden tellen als getallen: 'sixth meeting' en 'ninth meeting' zijn
# verschillende vergaderingen. "one" bewust niet (One Health).
GETALWOORDEN = dict(zip(
    "two three four five six seven eight nine ten eleven twelve".split(), map(str, range(2, 13))))
GETALWOORDEN.update(zip(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth".split(),
    map(str, range(1, 13))))
# Woorden die een bericht definiëren. Heeft elke kop er een die de andere mist
# ('mpox' tegen 'cholera', 'Kenya' tegen 'Uganda'), dan zijn het verschillende
# berichten, hoe gelijk de rest van de kop ook is.
_ZIEKTEN = """ebola marburg mpox monkeypox cholera measles polio poliovirus dengue malaria tuberculosis tb
hiv aids covid sars mers coronavirus influenza flu h5n1 h5n5 h7n9 h1n1 h3n2 avian nipah zika chikungunya
oropouche lassa plague anthrax rabies diphtheria pertussis meningitis typhoid hepatitis yellow rift crimean
hantavirus legionella salmonella listeria norovirus rotavirus rsv mumps rubella smallpox varicella leprosy
scabies trachoma schistosomiasis leishmaniasis onchocerciasis filariasis chagas yaws dracunculiasis"""
_LANDEN = """afghanistan albania algeria angola argentina armenia australia austria azerbaijan bahamas bahrain
bangladesh barbados belarus belgium belize benin bhutan bolivia bosnia botswana brazil brunei bulgaria burkina
burundi cambodia cameroon canada chad chile china colombia comoros congo croatia cuba cyprus czechia czech
denmark djibouti dominica dominican ecuador egypt salvador eritrea estonia eswatini ethiopia fiji finland
france gabon gambia georgia germany ghana greece guatemala guinea guyana haiti honduras hungary iceland india
indonesia iran iraq ireland israel italy ivory jamaica japan jordan kazakhstan kenya kiribati korea kosovo
kuwait kyrgyzstan laos latvia lebanon lesotho liberia libya lithuania luxembourg madagascar malawi malaysia
maldives mali malta mauritania mauritius mexico micronesia moldova mongolia montenegro morocco mozambique
myanmar namibia nepal netherlands dutch zealand nicaragua niger nigeria macedonia norway oman pakistan palau
palestine gaza panama papua paraguay peru philippines poland portugal qatar romania russia rwanda samoa saudi
senegal serbia seychelles sierra singapore slovakia slovenia somalia sudan spain lanka suriname sweden
switzerland syria taiwan tajikistan tanzania thailand timor togo tonga trinidad tunisia turkey turkiye
turkmenistan tuvalu uganda ukraine emirates uae uk us uruguay uzbekistan vanuatu venezuela vietnam yemen
zambia zimbabwe africa asia europe americas caribbean pacific"""
_UITGEVER = re.compile(r"\s+[-|–—]\s+([^-|–—]{2,60})$")
_AFGEKAPT = re.compile(r"(\.\.\.|…)\s*$")
_ALGEMEEN_LABEL = {"com", "org", "net", "gov", "int", "edu", "www", "news", "co", "ac", "go"}
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_|cmpid$|ito$|at_|rss$|cid$|smid$|taid$)", re.I)


def canon_url(url: str) -> str:
    """URL zonder tracking-parameters, fragment, www. en slash aan het eind."""
    try:
        p = urllib.parse.urlparse(url or "")
    except Exception:
        return url or ""
    host = host_van(url)
    if host == "news.google.com":
        return url                            # het pad is een ondoorzichtige id
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    pad = re.sub(r"/amp$", "", p.path.rstrip("/")) or "/"
    return host + pad + ("?" + urllib.parse.urlencode(sorted(q)) if q else "")


def zonder_uitgever(titel: str, url: str = "", src: str = "", domein: str = "") -> str:
    """Knipt ' - Uitgever' van de kop, maar alleen als dat stuk echt de
    uitgever is (past bij het domein of de bronnaam). Zo blijft het land in
    'Ebola disease - Democratic Republic of the Congo' (WHO DON) gewoon staan."""
    m = _UITGEVER.search(titel or "")
    if not m:
        return titel or ""
    staart = m.group(1).lower()
    compact = re.sub(r"[^a-z0-9]+", "", staart)
    woorden = set(re.findall(r"[a-z0-9]+", staart))
    labels = set()
    for host in {host_van(url), domein or ""} - {"", "news.google.com"}:
        labels |= {l for l in host.split(".") if len(l) >= 3 and l not in _ALGEMEEN_LABEL}
    bron = re.sub(r"[^a-z0-9]+", "", (src or "").lower())
    uitgever = (bron and bron == compact) or any(
        l in woorden or l == compact or (len(l) >= 4 and l in compact) or (len(compact) >= 4 and compact in l)
        for l in labels)
    return titel[: m.start()] if uitgever else titel


def kopwoorden(titel: str, url: str = "", src: str = "", domein: str = "") -> list:
    t = zonder_uitgever(titel, url, src, domein).strip()
    afgekapt = bool(_AFGEKAPT.search(t))
    t = _AFGEKAPT.sub("", t)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\b((?:[a-z]\.){1,3}[a-z])\.?(?![a-z])", lambda m: m.group(1).replace(".", ""), t)  # u.s. -> us
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", t)        # 6th -> 6
    t = re.sub(r"[^a-z0-9]+", " ", t)
    for patroon, vervang in LANDNAMEN:
        t = re.sub(patroon, vervang, t)
    woorden = t.split()
    if afgekapt and woorden and not re.search(r"\s(\.\.\.|…)\s*$", titel or ""):
        woorden = woorden[:-1]                # midden in een woord afgekapt ("Glob...")
    return [_stam(w) for w in woorden if w not in STOPWOORDEN]


def _stam(w: str) -> str:
    w = GETALWOORDEN.get(w, w)
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]                            # pandemics ~ pandemic
    return w


SLEUTELWOORDEN = {_stam(w) for w in (_ZIEKTEN + " " + _LANDEN).split()}


def titel_sleutel(titel: str, url: str = "", src: str = "", domein: str = "") -> str:
    """Kop als gesorteerde set inhoudswoorden; leeg als hij te kort is om op te vertrouwen."""
    woorden = sorted(set(kopwoorden(titel, url, src, domein)))
    return " ".join(woorden) if len(woorden) >= 4 else ""


def eigennamen(titel: str, url: str = "", src: str = "", domein: str = "") -> frozenset:
    """Woorden die midden in een kop met een hoofdletter staan (Stearns,
    Hamilton). Alleen bij koppen in zinsopbouw: in Title Case Heeft Elk Woord
    Een Hoofdletter en zie je niet wat een naam is."""
    woorden = re.findall(r"[^\W\d_][\w'’.-]*", zonder_uitgever(titel or "", url, src, domein))
    if len(woorden) < 4:
        return frozenset()
    hoofd = [w for w in woorden[1:] if w[0].isupper()]
    if len(hoofd) > 0.5 * (len(woorden) - 1):
        return frozenset()
    return frozenset(k for w in hoofd for k in kopwoorden(w))


def uitgever_van(item: dict) -> str:
    """Wie publiceerde dit? Bij een directe link de host; bij een Google
    News-link (waar de echte URL verborgen zit) de bronnaam."""
    host = host_van(item.get("url") or "")
    if host != "news.google.com":
        return host
    return "src:" + re.sub(r"[^a-z0-9]+", "", (item.get("src") or "").lower())


def handtekening(item: dict) -> dict:
    url = item.get("url") or ""
    host = host_van(url)
    args = (item.get("title", ""), url, item.get("src", ""), item.get("domain", ""))
    reeks = tuple(kopwoorden(*args))
    woorden = frozenset(reeks)
    return {"reeks": reeks,
            "woorden": woorden,
            "namen": eigennamen(*args),
            "getallen": frozenset(w for w in woorden if w.isdigit()),
            "uitgever": uitgever_van(item),
            "url": canon_url(url)}


def zelfde_bericht(a: dict, b: dict) -> bool:
    if a["url"] == b["url"]:
        return True
    if a["uitgever"] and a["uitgever"] == b["uitgever"]:
        return False      # zelfde uitgever, andere pagina: een vervolg in een reeks (WHO DON's houden vaak dezelfde kop)
    if a["getallen"] and b["getallen"] and a["getallen"] != b["getallen"]:
        return False      # situation report 12 vs 13, of een ander dodental
    A, B = a["woorden"], b["woorden"]
    alleen_a, alleen_b = A - B, B - A
    if alleen_a & SLEUTELWOORDEN and alleen_b & SLEUTELWOORDEN:
        return False      # andere ziekte of ander land
    if len(alleen_a) == len(alleen_b) == 1 and alleen_a <= a["namen"] and alleen_b <= b["namen"]:
        return False      # precies één naam verwisseld: 'Stearns County jail' tegen 'Hamilton County jail'
    klein = min(len(A), len(B))
    if klein < 4:
        return False
    gedeeld = len(A & B)
    if gedeeld / len(A | B) >= 0.7:
        return True
    # De ene kop zit (bijna) helemaal in de andere: afgekapt, of er staat een
    # label voor ("STAT+:", "[Articles]"). Maar 'Ninth meeting of the IGWG' is
    # niet hetzelfde als 'DG's opening remarks at the Ninth meeting of the IGWG':
    # dan moet de korte het begin van de lange zijn, of de lange mag maar een
    # paar woorden extra hebben.
    if klein < 6 or gedeeld / klein < 0.9:
        return False
    kort, lang = sorted((a["reeks"], b["reeks"]), key=len)
    return len(B if len(A) == klein else A) - klein <= 3 or lang[:len(kort)] == kort


def past_in_groep(h: dict, leden: list) -> bool:
    """Lijkt op minstens één lid, en botst met geen enkel lid: een andere
    pagina van dezelfde uitgever in de groep betekent een ander bericht, ook
    als het via de kopie van een derde site toch op de groep lijkt."""
    if any(h["uitgever"] and h["uitgever"] == lid["uitgever"] and h["url"] != lid["url"] for lid in leden):
        return False
    return any(zelfde_bericht(h, lid) for lid in leden)


def kwaliteit(item: dict) -> tuple:
    """Welke versie van een dubbel bericht tonen we?"""
    host = host_van(item.get("url") or "")
    titel = zonder_uitgever((item.get("title") or "").strip(), item.get("url", ""),
                            item.get("src", ""), item.get("domain", "")).strip()
    return (not (_in(host, SOCIAL) or _in(host, LAGE_WAARDE)),                           # nieuwsbericht boven een Facebook- of LinkedIn-post
            host != "news.google.com",                       # directe link boven de Google-omweg
            titel == unicodedata.normalize("NFKC", titel),   # gewone letters, geen 𝐯𝐞𝐭𝐭𝐞 unicode
            not _AFGEKAPT.search(titel),                     # volledige kop boven afgekapte
            BRONGEWICHT.get(item.get("feed", ""), 1),
            min(len(titel), 140))


def eerste_publicatie() -> dict:
    """Per kop-sleutel de publicatiedagen uit het archief. Zo komt een bericht
    dat dagen later onder een nieuwe URL opduikt niet opnieuw als 'vers' bovenaan."""
    oudste = {}
    for row in archive_rows():
        url = row.get("url", "")
        sleutel = titel_sleutel(row.get("title", ""), url, row.get("source", ""))
        dag = (row.get("published") or "")[:10]
        if not sleutel or len(dag) != 10:
            continue
        uitgever = uitgever_van({"url": url, "src": row.get("source", "")})
        oudste.setdefault(sleutel, []).append((dag, uitgever, canon_url(url)))
    return oudste


def ontdubbel_op_titel(items: list, oudste: dict) -> list:
    """Voegt items samen die hetzelfde bericht zijn (zie zelfde_bericht). Per
    groep tonen we de beste versie (kwaliteit), met de vroegste bekende
    publicatiedatum en de hoogste score van de groep."""
    groepen, woord_index, url_index = [], {}, {}
    for item in items:
        item = dict(item)                     # kopie: het archief houdt zijn eigen data
        h = handtekening(item)

        sleutel = titel_sleutel(item.get("title", ""), item.get("url", ""), item.get("src", ""), item.get("domain", ""))
        dagen = [dag for dag, uitgever, url in oudste.get(sleutel, ())
                 if url == h["url"] or uitgever != h["uitgever"]]
        if dagen:
            vroegste = min(dagen)
            if not item.get("date") or vroegste < item["date"][:10]:
                item["date"] = vroegste + "T00:00:00+00:00"

        kandidaten = Counter()
        for w in h["woorden"]:
            for g in woord_index.get(w, ()):
                kandidaten[g] += 1
        for g in url_index.get(h["url"], ()):
            kandidaten[g] += 1000
        gevonden = next((g for g, n in kandidaten.most_common()
                         if n >= 3 and past_in_groep(h, groepen[g]["leden"])), None)

        if gevonden is None:
            gevonden = len(groepen)
            groepen.append({"item": item, "leden": [h]})
        else:
            groep = groepen[gevonden]
            bewaard = groep["item"]
            datums = [d for d in (bewaard.get("date"), item.get("date")) if d]
            score = max(bewaard.get("score") or 0, item.get("score") or 0)
            if kwaliteit(item) > kwaliteit(bewaard):
                bewaard = groep["item"] = item
            bewaard["date"] = min(datums) if datums else bewaard.get("date", "")
            bewaard["score"] = score
            groep["leden"].append(h)
        for w in h["woorden"]:
            woord_index.setdefault(w, set()).add(gevonden)
        url_index.setdefault(h["url"], set()).add(gevonden)
    return [g["item"] for g in groepen]


def main() -> int:
    fresh, status = [], {}
    for feed in FEEDS:
        try:
            items, st = harvest(feed)
            fresh.extend(items)
            status[feed["label"]] = st
            label = "FALLB" if st.get("fallback") else "OK   "
            print(f"  {label} {feed['label']:<26} {st['msg']}" + (f"   [{st['errors']}]" if st.get("errors") else ""))
        except Exception as e:
            status[feed["label"]] = {"ok": False,
                                     "msg": f"{type(e).__name__}: {e}"[:200],
                                     "cat": feed["cat"]}
            print(f"  FAIL  {feed['label']:<26} {type(e).__name__}: {e}", file=sys.stderr)

    # Veel officiele feeds (ECDC, WHO) geven alleen een datum, geen tijd: dan lijkt
    # een bericht dat 's middags verschijnt bij binnenkomst al 13 uur oud en zakt het
    # meteen uit de verse laag. Bij zulke datum-zonder-tijd-items rekenen we vanaf
    # het moment dat de harvester ze voor het eerst zag.
    vorige = load_previous()
    al_gezien = {i.get("url") for i in vorige}
    nu = datetime.now(timezone.utc)
    for item in fresh:
        d = item.get("date") or ""
        if item.get("url") in al_gezien or "T00:00:00" not in d:
            continue
        try:
            leeftijd = nu - datetime.fromisoformat(d)
        except Exception:
            continue
        if timedelta(0) <= leeftijd < timedelta(hours=36):
            item["date"] = nu.isoformat(timespec="seconds")

    # Nieuw wint van oud, maar oude items blijven bestaan zolang ze vers zijn.
    merged, seen = [], set()
    for item in fresh + vorige:
        key = item.get("url")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)

    # Google News-links vervangen door de echte artikel-URL (gecachet), vóór de
    # linkcheck: dan zien bloklijst, social-filter en uitgeverstier de echte site.
    gn_cache = laad_gnews_cache()
    opgelost, mislukt = los_gnews_links_op(merged, gn_cache)
    bewaar_gnews_cache(gn_cache)
    nog = sum(1 for i in merged if "news.google.com" in (i.get("url") or ""))
    print(f"  Google News-links: {opgelost} nieuw opgelost, {mislukt} mislukt, {nog} nog niet")
    merged = list({(i.get("url") or ""): i for i in reversed(merged)}.values())[::-1]   # opgelost kan dubbel zijn

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
        # Google Alerts en andere open-web feeds vangen veel ruis ("medical emergency"
        # van een zanger). Die houden we alleen als de inhoud zelf relevant is.
        if open_web and item["score"] - BRONGEWICHT.get(item.get("feed", ""), 1) + straf < MIN_INHOUD_OPEN_WEB:
            weg["open web, te weinig relevant"] = weg.get("open web, te weinig relevant", 0) + 1
            continue
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

    toegevoegd = append_archive(fresh)

    print(f"\n{ok}/{len(FEEDS)} feeds OK -> {len(merged)} items in {OUT}")
    print(f"archief: {toegevoegd} nieuwe regels in {ARCHIVE_DIR}/")
    # Nooit falen op een kapotte bron: dan zou de workflow rood staan en de
    # oude feeds.json blijven hangen. Alleen falen als er niets overblijft.
    return 0 if merged else 1


if __name__ == "__main__":
    sys.exit(main())
