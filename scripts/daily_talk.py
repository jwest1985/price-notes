"""One plain post a day with no link (a question for followers), on X, Threads and Bluesky.

It breaks up the feed between deal posts. Runs on every timer run but only posts once a day:
from TALK_HOUR (Toronto time) on, it schedules the next unused line from talk_posts.json at a :15 or :45
time (deal posts use :00 and :30), X first, then Threads +5 and Bluesky +10 minutes, like the deals.
Weekend lines are used Friday to Sunday. Lines come in types (question, this-or-that, niche, hot take,
nostalgia, collecting...) and two days in a row never get the same type.
About every other day it posts a talking point from the day's gaming news instead (a headline already
posted with its link, here with no link and a short question), when there is one. Lines don't repeat until the whole list has been used.
It does not count toward the day's 30 deals.
"""
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push_buffer as pb  # noqa: E402

POOL_PATH = os.path.join(pb.ROOT, "talk_posts.json")
STATE_PATH = os.path.join(pb.DATA, "talk_sent.json")
TALK_HOUR = 13  # 1 PM Toronto: posts at 1:15 PM, or the next :15/:45 if the first run after 1 PM is later
LAST = (21, 45)
OFFSET = {"twitter": 0, "threads": 5, "bluesky": 10}


def pick(pool, used, weekend, last_type=None):
    """Next unused line, never the same type (question, this-or-that, niche, hot take...) as the day before."""
    lines = pool["weekend"] if weekend else pool["any_day"]
    fresh = [e for e in lines if e["text"] not in used]
    if not fresh:  # whole list used: start over, but not with the line used last
        texts = [e["text"] for e in lines]
        last = next((t for t in reversed(used) if t in texts), None)
        used[:] = [t for t in used if t not in texts]
        fresh = [e for e in lines if e["text"] != last] or lines
    pick_ = next((e for e in fresh if e["type"] != last_type), None)
    if pick_ is None:  # only one type left unused: borrow a not-recent line of another type instead
        pos = {t: i for i, t in enumerate(used)}
        other = [e for e in lines if e["type"] != last_type]
        pick_ = min(other, key=lambda e: pos.get(e["text"], -1)) if other else fresh[0]  # least recently used
    return pick_


NEWS_ENDINGS = ["Thoughts?", "What do you think?", "Are you in?", "Hyped or not?", "Good news or bad news?"]


def news_talk(state):
    """A talking point from today's gaming news (a headline already posted with its link), with no link.
    Used about every other day when there is one; otherwise the regular list is used."""
    drafts = {d["id"]: d for d in pb.load(pb.DRAFTS_PATH, [])}
    sent = pb.load(pb.SENT_PATH, {})
    used = set(state.get("news_used", []))
    picks = []
    for did, v in sent.items():
        d = drafts.get(did)
        if not d or d.get("kind") != "news" or "posts" not in v or did in used:
            continue
        try:
            at = pb.datetime.fromisoformat(v["at"])
        except Exception:
            continue
        if pb.NOW - at <= timedelta(hours=24):
            title = pb.re.sub(r"\s+", " ", d.get("item") or "").strip().rstrip(".")
            if 12 <= len(title) <= 180:
                picks.append((at, did, title))
    if not picks:
        return None
    at, did, title = max(picks)  # the newest headline
    ending = NEWS_ENDINGS[len(state.get("news_used", [])) % len(NEWS_ENDINGS)]
    return {"type": "news", "text": f"{title}.\n\n{ending}", "news_id": did}


def main():
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    cfg = pb.load(os.path.join(pb.ROOT, "config.json"), {})
    if not key or not cfg.get("daily_talk_post", True):
        print("daily talk post: off")
        return
    local = pb.NOW.astimezone(pb.LOCAL)
    state = pb.load(STATE_PATH, {"days": {}, "used": []})
    today = local.date().isoformat()
    if today in state["days"]:
        print("daily talk post: already done today")
        return
    if local.hour < TALK_HOUR:
        print("daily talk post: waits until 1 PM")
        return
    due = local.replace(second=0, microsecond=0) + timedelta(minutes=10)
    due += timedelta(minutes=(15 - due.minute % 30) % 30)  # next :15 or :45
    if (due.hour, due.minute) > LAST or due.date() != local.date():
        print("daily talk post: too late in the day, skipped")
        state["days"][today] = {"skipped": "too late"}
        pb.save(STATE_PATH, state)
        return
    pool = json.load(open(POOL_PATH, encoding="utf-8"))
    entry = None
    if state.get("last_type") != "news" and cfg.get("daily_talk_news", True):
        entry = news_talk(state)
    if entry is None:
        entry = pick(pool, state["used"], local.weekday() >= 4, state.get("last_type"))
    text = entry["text"]
    channels = pb.load(pb.CHANNELS_PATH, []) or pb.get_channels(key)
    ids = {}
    for c in channels:
        inp = {"text": text, "channelId": c["id"], "schedulingType": "automatic", "mode": "customScheduled",
               "dueAt": (due + timedelta(minutes=OFFSET.get(c["service"], 0))).isoformat(timespec="seconds"),
               "saveToDraft": False, "assets": [], "tagIds": [], "source": "price-notes"}
        if c["service"] == "threads":
            inp["metadata"] = {"threads": {"type": "post", "topic": pb.THREADS_TOPIC}}
        try:
            res = pb.gql(key, pb.CREATE, {"input": inp})["createPost"]
            if res.get("__typename") == "PostActionSuccess":
                ids[c["service"]] = res["post"]["id"]
            else:
                print("daily talk post: Buffer refused", c["service"], res.get("message"))
        except Exception as e:
            print("daily talk post: error", c["service"], str(e)[:200])
    if ids:
        state["days"][today] = {"text": text, "at": due.isoformat(timespec="minutes"), "posts": ids}
        if entry.get("news_id"):
            state["news_used"] = (state.get("news_used", []) + [entry["news_id"]])[-100:]
        else:
            state["used"].append(text)
        state["last_type"] = entry["type"]
        state["days"] = dict(sorted(state["days"].items())[-60:])
        pb.save(STATE_PATH, state)
        print(f"daily talk post: scheduled for {due:%H:%M}: {text}")


if __name__ == "__main__":
    main()
