from datetime import datetime, timedelta, timezone

from scanner import sentiment_relay as sr

NOW = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)


def item(i, minutes_ago, n=1, long=0):
    return {"id": i, "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "embeds": [{"title": f"t{k}", "description": "x" * long, "fields": [{"name": "a", "value": "b" * 2000}]}
                       for k in range(n)]}


def test_new_items_only_and_no_backlog_after_redeploy():
    started = NOW - timedelta(minutes=10)
    items = [item("old", 300), item("recent", 20), item("seen", 5), item("new", 2), {"bad": 1}]
    got = [x["id"] for x in sr.to_post(items, {"seen"}, NOW, started)]
    assert got == ["recent", "new"]          # old predates the start window; seen is skipped


def test_long_posts_split_and_clamped():
    msgs = sr.messages(item("a", 1, n=6, long=3000))
    assert all(sum(sr._size(e) for e in m) <= sr.CHAR_LIMIT for m in msgs)
    assert sum(len(m) for m in msgs) == 6
    assert all(len(f["value"]) <= 1024 for m in msgs for e in m for f in e.get("fields", []))
