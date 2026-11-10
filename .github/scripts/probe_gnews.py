#!/usr/bin/env python3
"""Test: draait harvest() van de echte harvester op een paar bronnen en toont
per kandidaat waarom hij faalde."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import fetch_feeds as F

for feed in F.FEEDS:
    if feed["label"] in ("UN News Health", "WHO News", "BMJ", "Geneva Health Files", "ReAct", "DNDi"):
        try:
            items, msg = F.harvest(feed)
            print(f"{feed['label']:<24} OK {msg} | {items[0]['url'][:90]}")
        except Exception as e:
            print(f"{feed['label']:<24} FAIL {e}"[:400])
