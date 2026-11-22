"""One plain post a day with no link (a question for followers), on X, Threads and Bluesky.

It breaks up the feed between deal posts. Runs on every timer run but only posts once a day:
from TALK_HOUR (Toronto time) on, it schedules the next unused line from talk_posts.json at a :15 or :45
time (deal posts use :00 and :30), X first, then Threads +5 and Bluesky +10 minutes, like the deals.
Weekend lines are used Friday to Sunday. Lines don't repeat until the whole list has been used.
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


def pick(pool, used, weekend):
    lines = pool["weekend"] if weekend else pool["any_day"]
    fresh = [t for t in lines if t not in used]
    if not fresh:  # whole list used: start over, but not with the line used last
        last = next((t for t in reversed(used) if t in lines), None)
        used[:] = [t for t in used if t not in lines]
        fresh = [t for t in lines if t != last] or lines
    return fresh[0]


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
    text = pick(pool, state["used"], local.weekday() >= 4)
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
        state["used"].append(text)
        state["days"] = dict(sorted(state["days"].items())[-60:])
        pb.save(STATE_PATH, state)
        print(f"daily talk post: scheduled for {due:%H:%M}: {text}")


if __name__ == "__main__":
    main()
