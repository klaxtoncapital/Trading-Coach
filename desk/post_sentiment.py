"""Queue a post for Dave's #macro-sentiment Discord channel.

Run from the root of a desk-data clone:  python3 desk/post_sentiment.py item.json
item.json: {"id": "<unique, e.g. 2026-10-29-fomc>", "embeds": [<Discord embeds>]}
The Railway desk service polls desk/sentiment.json and posts new items within ~5 minutes.
Checks the item, appends it (keeps the last 60), commits and pushes with retries."""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

FEED = "desk/sentiment.json"


def sh(*a, check=True):
    return subprocess.run(a, check=check, capture_output=True, text=True)


def size(e):
    n = len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))
    return n + sum(len(f.get("name", "")) + len(f.get("value", "")) for f in e.get("fields", []))


def check(item):
    assert isinstance(item.get("id"), str) and item["id"], "item needs an id"
    emb = item.get("embeds")
    assert isinstance(emb, list) and 1 <= len(emb) <= 20, "1-20 embeds"
    for e in emb:
        assert e.get("title") and len(e["title"]) <= 256, "every embed needs a title under 256 chars"
        assert len(e.get("description", "")) <= 4000, f"description too long in {e['title']}"
        assert len(e.get("fields", [])) <= 25
        for f in e.get("fields", []):
            assert f.get("name") and f.get("value"), "fields need a name and value"
            assert len(f["value"]) <= 1024, f"field '{f['name']}' over 1,024 chars"
        assert size(e) <= 5800, f"embed '{e['title']}' over 5,800 chars"


def main(path):
    item = json.load(open(path))
    item.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    check(item)
    for attempt in range(4):
        sh("git", "fetch", "-q", "origin", "desk-data")
        sh("git", "reset", "-q", "--hard", "origin/desk-data")
        try:
            feed = json.load(open(FEED))
        except (OSError, ValueError):
            feed = []
        feed = [x for x in feed if x.get("id") != item["id"]] + [item]
        json.dump(feed[-60:], open(FEED, "w"), ensure_ascii=False, indent=1)
        sh("git", "add", FEED)
        sh("git", "-c", "user.name=Klaxton Desk", "-c", "user.email=klaxtoncapital@gmail.com",
           "commit", "-q", "-m", f"Sentiment: {item['id']}")
        if sh("git", "push", "-q", "origin", "HEAD:desk-data", check=False).returncode == 0:
            print("Queued for #macro-sentiment:", item["id"])
            return
        time.sleep(5 * (attempt + 1))
    sys.exit("Push failed 4 times: the post was not queued")


if __name__ == "__main__":
    main(sys.argv[1])
