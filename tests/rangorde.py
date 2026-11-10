"""Python-kopie van de rangschikking in index.html (rank, isUrgent, prioriteer),
zodat we de voorpagina kunnen naspelen op een vaste momentopname."""
import re
from datetime import datetime

# Houd gelijk met index.html (MAX_CARDS, PER_BRON, MIN_RANK, VERS_*, VERVAL*).
MAX_CARDS, PER_BRON, MIN_RANK = 30, 5, 6
VERS_UREN, VERS_MIN, VERVAL_UUR, VERVAL = 24, 10, 0.25, 2.5
URGENT_FEEDS = {"WHO Disease Outbreak News", "ECDC Threats report", "ECDC Risk assessments"}
URGENT_RE = re.compile("|".join([
    'pheic', 'public health emergency of international concern', 'pandemic emergency',
    'outbreak declared', 'declares? (an? )?outbreak', 'declared an outbreak',
    'state of emergency', 'emergency declared', 'declares? (a |an )?(national |public health )?emergency',
    'grade 3 emergency', 'health alert', 'death toll', 'deaths? (rise|rises|climb|surge)',
    'cases (surge|soar|spike|double)', r'first (confirmed )?(human )?cases?\b', 'spreads? to',
    'human-to-human', 'new variant', 'novel (virus|pathogen|strain)', 'unknown disease',
    'mystery (illness|disease)', 'marburg', 'nipah', 'h5n1 (in )?human', 'cholera outbreak',
    'ebola outbreak', 'mpox (outbreak|clade)', 'disease outbreak news']), re.I)


def uren(i, nu):
    try:
        return max(0, (nu - datetime.fromisoformat(i["date"])).total_seconds() / 3600)
    except Exception:
        return float("inf")


def urgent(i, nu):
    if uren(i, nu) >= VERS_UREN or i.get("fallback"):
        return False
    officieel = re.search(r"(^|//|\.)(who\.int|ecdc\.europa\.eu)/", i.get("url", ""))
    return bool((i.get("feed") in URGENT_FEEDS and officieel) or URGENT_RE.search(i.get("title", "") + " " + i.get("summary", "")))


def rank(i, nu, vers_min=VERS_MIN):
    s = i.get("score", 1)
    h = uren(i, nu)
    if h == float("inf"):
        return s - 10
    if h < VERS_UREN and s >= vers_min:
        bonus = 15 * (1 - h / VERS_UREN) if urgent(i, nu) else 0
        return 1000 + s + bonus - h * VERVAL_UUR
    return s - (h / 24) * VERVAL


def prioriteer(items, nu, max_cards=MAX_CARDS, vers_min=VERS_MIN, min_rank=MIN_RANK):
    g = sorted(items, key=lambda i: -rank(i, nu, vers_min))
    in_vers = lambda i: uren(i, nu) < VERS_UREN and i.get("score", 1) >= vers_min
    kand = [i for i in g if in_vers(i)] + [i for i in g if not in_vers(i) and rank(i, nu, vers_min) >= min_rank]
    if len(kand) < 8:
        kand = g
    per = {}
    for i in kand:
        per.setdefault(i.get("feed") or i.get("src") or "?", []).append(i)
    groepen = sorted(per.values(), key=lambda x: -rank(x[0], nu, vers_min))
    uit = []
    for ronde in range(PER_BRON):
        for gr in groepen:
            if ronde < len(gr) and len(uit) < max_cards:
                uit.append(gr[ronde])
    return sorted(uit, key=lambda i: -rank(i, nu, vers_min))
