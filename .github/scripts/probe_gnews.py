#!/usr/bin/env python3
"""Droogtest van de echte harvester: draait harvest() op elke bron (of alleen
de labels die je meegeeft) en lost een paar Google News-links op. Schrijft niets."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import fetch_feeds as F

alleen = set(sys.argv[1:])
gn = []
for feed in F.FEEDS:
    if alleen and feed["label"] not in alleen:
        continue
    try:
        items, st = F.harvest(feed)
        soort = "FALLB" if st.get("fallback") else ("SEARCH" if st.get("search") else "DIRECT")
        voorbeeld = f"{items[0]['title'][:60]} | {items[0]['url'][:70]}" if items else ""
        print(f"{soort:<6} {feed['label']:<30} {st['msg']:<40} {voorbeeld}")
        if st.get("errors"):
            print(f"{'':<38}eerder: {st['errors']}")
        gn += [i for i in items if "news.google.com" in i["url"]][:1]
    except Exception as e:
        print(f"DEAD   {feed['label']:<30} {e}"[:420])

print("\nGoogle News-links oplossen:")
cache = {}
n, mis = F.los_gnews_links_op(gn[:6], cache)
for i in gn[:6]:
    print(f"  {'OK ' if 'news.google.com' not in i['url'] else 'MIS'} {i['url'][:110]}")
print(f"  {n} opgelost, {mis} mislukt")
