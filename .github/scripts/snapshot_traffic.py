#!/usr/bin/env python3
"""Snapshot GitHub repo traffic into a persistent history.

GitHub retains only the last 14 days of clones/views. This merges each run's
snapshot into JSON history files so the data survives beyond that window.
Designed to run in GitHub Actions; reads GH_TOKEN and GH_REPO from the env.
Standard library only, so there is nothing to install in the runner.
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

REPO = os.environ["GH_REPO"]            # "owner/repo"
TOKEN = os.environ["GH_TOKEN"]
OUT_DIR = os.environ.get("TRAFFIC_DIR", "traffic")
API = "https://api.github.com/repos/" + REPO + "/traffic/"


def fetch(endpoint):
    req = urllib.request.Request(
        API + endpoint,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "missioncache-traffic-snapshot",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def load(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def merge_daily(history, items):
    """Upsert per-day counts keyed by timestamp.

    Today's count grows through the day while past days arrive final, so
    keeping the max per timestamp converges to the final value and never
    loses a previously recorded higher count.
    """
    for it in items:
        ts = it["timestamp"]
        prev = history.get(ts)
        if prev is None:
            history[ts] = {"count": it["count"], "uniques": it["uniques"]}
        else:
            history[ts] = {
                "count": max(prev["count"], it["count"]),
                "uniques": max(prev["uniques"], it["uniques"]),
            }
    return history


def main():
    clones = merge_daily(load(OUT_DIR + "/clones.json"), fetch("clones").get("clones", []))
    views = merge_daily(load(OUT_DIR + "/views.json"), fetch("views").get("views", []))
    write(OUT_DIR + "/clones.json", clones)
    write(OUT_DIR + "/views.json", views)

    # Referrers and paths are a rolling top-10, not per-day series. Store a
    # dated snapshot of each so launch-window sources can be read over time.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for name, endpoint in (("referrers", "popular/referrers"), ("paths", "popular/paths")):
        log = load(OUT_DIR + "/" + name + ".json")
        log[stamp] = fetch(endpoint)
        write(OUT_DIR + "/" + name + ".json", log)

    print("clones days:", len(clones), "| views days:", len(views), "| snapshot:", stamp)


if __name__ == "__main__":
    main()
