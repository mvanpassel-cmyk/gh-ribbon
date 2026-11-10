#!/usr/bin/env python3
"""Proeft kandidaat-feeds vanaf een GitHub-runner (hetzelfde netwerk als de
harvester). Gebruik: python .github/scripts/probe_feeds.py urls.txt
Elke regel: label<TAB>url. Drukt per URL: HTTP-status, bytes, Cloudflare ja/nee,
aantal items en de eerste twee koppen. Wijzigt niets."""
import json, sys, time, urllib.request, urllib.error
import feedparser

UAS = {
    "browser": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "eerlijk": "GH-Ribbon/1.0 (+https://mvanpassel-cmyk.github.io/gh-ribbon/)",
}


def haal(url, ua):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua, "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:2000], dict(e.headers)
    except Exception as e:
        return type(e).__name__, b"", {}


def inhoud(body):
    t = body.lstrip()[:1]
    if t in (b"{", b"["):
        try:
            d = json.loads(body)
        except Exception:
            return 0, ["(json onleesbaar)"]
        lijst = d.get("value") or d.get("data") or d.get("items") or [] if isinstance(d, dict) else d
        koppen = []
        for x in lijst[:2]:
            if isinstance(x, dict):
                f = x.get("fields", x)
                titel = f.get("title") or f.get("Title") or f.get("OverrideTitle") or f.get("TitleSuffix")
                if isinstance(titel, dict):
                    titel = titel.get("rendered")
                koppen.append(f"{str(titel)[:90]} | {f.get('url') or f.get('link') or f.get('ItemDefaultUrl') or f.get('url_alias') or ''}")
        if lijst and isinstance(lijst[0], dict):
            koppen.append("velden: " + ", ".join(list(lijst[0].keys())[:30]))
        elif isinstance(d, dict):
            koppen.append("sleutels: " + ", ".join(list(d.keys())[:30]))
        return len(lijst), koppen
    p = feedparser.parse(body)
    koppen = [f"{e.get('title','')[:90]} | {e.get('link','')[:90]}" for e in p.entries[:2]]
    tags = sorted({t.get("term", "") for e in p.entries for t in e.get("tags", [])})
    if tags:
        koppen.append("tags: " + ", ".join(tags)[:400])
    if not p.entries:
        koppen.append("begin: " + body[:160].decode("utf-8", "replace").replace("\n", " "))
    return len(p.entries), koppen


for regel in open(sys.argv[1]):
    if not regel.strip() or regel.startswith("#"):
        continue
    label, url = regel.rstrip("\n").split("\t", 1)
    for naam, ua in UAS.items():
        status, body, h = haal(url, ua)
        cf = "cf" if any(k.lower() == "cf-ray" for k in h) else "--"
        n, koppen = inhoud(body) if status == 200 else (0, [body[:120].decode("utf-8", "replace").replace("\n", " ")])
        print(f"{label:<22} {naam:<7} {status!s:<5} {cf} {len(body):>7}B {n:>3} items  {url}")
        for k in koppen:
            print(f"{'':<36}{k}")
        if status == 200 and n:
            break                     # de browser-UA werkt al: de tweede is niet nodig
        time.sleep(1.5)
