#!/usr/bin/env python3
"""Meet de voorpagina: hoeveel van de top 30 is relevant volgens de handlabels.

    python3 tests/eval_ranking.py            # voor/na op de vaste momentopname
    python3 tests/eval_ranking.py --top 40   # langere lijst tonen

'Voor' = de scores zoals ze in de momentopname staan (oude scoring).
'Na'   = dezelfde items opnieuw gescoord met de huidige fetch_feeds.py.
Items zonder label worden met ? getoond: label ze in tests/labels.json."""
import json, os, sys
from datetime import datetime

HIER = os.path.dirname(os.path.abspath(__file__))
os.chdir(os.path.dirname(HIER))
sys.path[:0] = [HIER, os.path.dirname(HIER)]
import fetch_feeds as F
from rangorde import prioriteer

snap = json.load(open(os.path.join(HIER, "fixtures", "feeds-2026-11-10.json")))
labels = json.load(open(os.path.join(HIER, "labels.json")))["labels"]
nu = datetime.fromisoformat(snap["generated"])
TOP = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 30


def officieel(label):
    for feed in F.FEEDS:
        if feed["label"] == label:
            k = (feed.get("urls") or [feed.get("url")])[0]
            return not F.is_gnews(k) or bool(F.gnews_site(k))
    return False


def herscoor(items):
    uit = []
    for i in items:
        i = dict(i)
        if ("gnews" in i or "news.google.com" in i.get("url", "")) and officieel(i.get("feed")):
            i["fallback"] = True
        uit.append(i)
    return F.herbeoordeel(uit)[0]


def meet(naam, items, **drempels):
    top = prioriteer(items, nu, max_cards=TOP, **drempels)
    goed = fout = onbekend = 0
    print(f"\n== {naam} ==")
    for n, i in enumerate(top):
        lab = labels.get(i["title"])
        teken = "?" if lab is None else ("+" if lab["relevant"] else "-")
        if n < 30:
            if lab is None: onbekend += 1
            elif lab["relevant"]: goed += 1
            else: fout += 1
        print(f"{n:>2} {teken} {i.get('score', 0):>4} {i.get('feed', '')[:18]:<18} {i.get('domain', '')[:22]:<22} {i['title'][:84]}")
    print(f"-> top 30: {goed} relevant, {fout} irrelevant, {onbekend} zonder label"
          f"  (precisie {goed / max(1, goed + fout):.0%})")
    return fout, onbekend


meet("VOOR (oude scoring, oude drempels)", snap["items"], vers_min=5, min_rank=0)
fout, onbekend = meet("NA (huidige scoring en drempels)", herscoor(snap["items"]))
sys.exit(0 if fout <= 3 and onbekend == 0 else 1)
