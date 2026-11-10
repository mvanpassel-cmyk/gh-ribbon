#!/usr/bin/env python3
"""Test: lost de Google News-links uit probe-gnews.txt op met de harvester-code."""
import pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import fetch_feeds as F

for url in open(pathlib.Path(__file__).with_name("probe-gnews.txt")).read().split():
    m = F._GN_ID.search(url)
    oud = F._gn_base64(m.group(1)) if m else ""
    try:
        nieuw = F._gn_batchexecute(m.group(1)) if m and not oud else ""
        fout = ""
    except Exception as e:
        nieuw, fout = "", f"{type(e).__name__}: {e}"[:160]
    print(f"{'base64' if oud else 'batch '} {oud or nieuw or 'MISLUKT ' + fout}")
    time.sleep(1)
