#!/usr/bin/env python3
"""Keep a permanent archive of past World Calendar events.

events.json only holds events from about a month ago onwards: the weekly refresh
drops anything that ended more than 30 days ago. Run this script after every
change to events.json (a GitHub Action does that): it copies every event that has
already ended into archive.json, so the page can always show earlier months.
Nothing is ever removed from archive.json; an event already archived (same start
and title) is updated with the latest version from events.json.
"""
import datetime, json, pathlib

HERE = pathlib.Path(__file__).resolve().parent
EVENTS, ARCHIVE = HERE / "events.json", HERE / "archive.json"


def key(e):
    return (e["start"], e["title"])


def write(path, data):
    lines = ",\n".join("    " + json.dumps(e, ensure_ascii=False) for e in data["events"])
    head = ",\n".join(f'  "{k}": {json.dumps(v, ensure_ascii=False)}' for k, v in data.items() if k != "events")
    path.write_text("{\n" + head + ',\n  "events": [\n' + lines + "\n  ]\n}\n", encoding="utf-8")


def main(today=None):
    today = today or datetime.date.today().isoformat()
    events = json.loads(EVENTS.read_text(encoding="utf-8"))["events"]
    arch = json.loads(ARCHIVE.read_text(encoding="utf-8")) if ARCHIVE.exists() else {"events": []}
    have = {key(e): i for i, e in enumerate(arch["events"])}
    changed = 0
    for e in events:
        if (e.get("end") or e["start"]) >= today:
            continue
        e = {k: v for k, v in e.items() if k != "keep"}
        if key(e) in have:
            if arch["events"][have[key(e)]] != e:
                arch["events"][have[key(e)]] = e
                changed += 1
        else:
            have[key(e)] = len(arch["events"])
            arch["events"].append(e)
            changed += 1
    if changed or not ARCHIVE.exists():
        arch["events"].sort(key=lambda e: (e["start"], e.get("end") or e["start"], e["title"]))
        out = {"updated": today,
               "note": "Past events of the World Calendar. Filled automatically from events.json; never pruned."}
        out["events"] = arch["events"]
        write(ARCHIVE, out)
    print(f"archive: {changed} added or updated, {len(arch['events'])} in total")


if __name__ == "__main__":
    main()
