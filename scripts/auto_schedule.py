"""Schedule finished deal drafts automatically.

A deal is finished when every one of its draft posts has the link you put in:
  - Amazon: your link.amazon or amzn.to link (no long amazon.com link left)
  - other stores: a buff.ly link (Buffer's link shortening turned on)
and none of its posts has a hold tag (Price went up, Out of stock, Couldn't verify, ...).

Each finished deal gets its price checked live again; if it passes it is scheduled in the next free slot
(X on the :00 or :30, Threads 5 minutes later, Bluesky 10 minutes later). If it fails, it stays in Drafts with
the matching tag. Nothing else is ever scheduled.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push_buffer as pb  # noqa: E402
import verify_scheduled as vs  # noqa: E402

LOCAL = ZoneInfo("America/Toronto")
NOW = datetime.now(timezone.utc)
FIRST_SLOT_HOUR, LAST_SLOT = 6, (23, 30)   # posts go out 6:00 AM to 11:30 PM Toronto time
LEAD = timedelta(minutes=10)                # never schedule sooner than this
MAX_PER_RUN = 4
MAX_AHEAD = timedelta(hours=6)              # keep prices fresh: don't book slots far ahead
OFFSET = {"twitter": 0, "threads": 5, "bluesky": 10}
CHANNEL_KEY = {"twitter": "x", "threads": "threads", "bluesky": "bluesky"}
WORK_TAGS = {"Swap Amazon link", "Turn on shortening link"}   # reminders only, not holds

DRAFTS_Q = ("query($input: PostsInput!, $after: String) { posts(first: 100, after: $after, input: $input) { edges { node { "
            + pb.POST_FIELDS + " } } pageInfo { hasNextPage endCursor } } }")


def all_posts(key, status):
    out, after = [], None
    while True:
        data = pb.gql_posts(key, DRAFTS_Q, {"input": {"organizationId": vs.ORG, "filter": {"status": status}}, "after": after})["posts"]
        out += [e["node"] for e in data["edges"]]
        if not data["pageInfo"]["hasNextPage"]:
            return out
        after = data["pageInfo"]["endCursor"]


def finished(d, posts):
    for p in posts:
        text = pb.full_text(p)
        if any(t["name"] not in WORK_TAGS for t in p.get("tags") or []):
            return False, "has a hold tag"
        if d.get("store") == "Amazon":
            if "amazon.com/" in text or not ("link.amazon/" in text or "amzn.to/" in text):
                return False, "Amazon link not swapped yet"
        elif d.get("kind") == "deal" and "http" in text and "buff.ly/" not in text:
            return False, "link shortening not on yet"  # news keeps its direct link to the source
    return True, ""


def free_slots(taken):
    """X slot times (UTC) from now on, skipping ones already used by a scheduled X post."""
    t = (NOW + LEAD).astimezone(LOCAL)
    t = t.replace(second=0, microsecond=0)
    t += timedelta(minutes=(30 - t.minute % 30) % 30)
    while t - NOW.astimezone(LOCAL) <= MAX_AHEAD:
        ok_hours = t.hour >= FIRST_SLOT_HOUR and (t.hour, t.minute) <= LAST_SLOT
        if ok_hours and t.astimezone(timezone.utc).replace(second=0) not in taken:
            yield t
        t += timedelta(minutes=30)


def main():
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    cfg = pb.load(os.path.join(pb.ROOT, "config.json"), {})
    if not key or not cfg.get("auto_schedule", False):
        print("auto-schedule: off")
        return
    sent = pb.load(pb.SENT_PATH, {})
    drafts = {d["id"]: d for d in pb.load(pb.DRAFTS_PATH, [])}
    post_to_deal = {pid: did for did, v in sent.items() for pid in (v.get("posts") or {}).values()}

    scheduled = all_posts(key, ["scheduled"])
    taken = set()
    for p in scheduled:
        if p["channelService"] == "twitter" and p.get("dueAt"):
            due = datetime.fromisoformat(p["dueAt"].replace("Z", "+00:00")).replace(second=0, microsecond=0)
            taken.add(due)

    by_deal = {}
    for p in all_posts(key, ["draft"]):
        did = post_to_deal.get(p["id"])
        if did and did in drafts:
            by_deal.setdefault(did, []).append(p)
    if not by_deal:
        print("auto-schedule: no deal drafts")
        return

    # oldest first, so deals don't wait behind newer ones
    order = sorted(by_deal, key=lambda did: sent.get(did, {}).get("at", ""))
    slots = free_slots(taken)
    browser = vs.Browser()
    done = 0
    try:
        for did in order:
            if done >= MAX_PER_RUN:
                break
            d, posts = drafts[did], by_deal[did]
            name = d.get("item") or d["posts"]["x"][:60]
            ok, why = finished(d, posts)
            if not ok:
                print(f"auto-schedule: waiting  {name}: {why}")
                continue
            if d.get("kind") != "deal":  # news: nothing to price-check
                state, live, detail = "live", None, "news"
            else:
                state, live, detail = vs.check_deal(d, browser.get)
            v = None if d.get("kind") != "deal" else vs.verdict(state, live, vs.posted_price(d, posts[0].get("text") or ""), detail, d.get("store", ""))
            if v and v[0] == "UPDATE":
                new, old = v[1], vs.posted_price(d, posts[0].get("text") or "")
                for p in posts:
                    text = p.get("text") or ""
                    fixed = vs.re.sub(r"\$([\d,]+(?:\.\d+)?)",
                                      lambda m: f"${new:.2f}" if abs(vs.num(m.group(1)) - old) < 0.009 else m.group(0), text, count=1)
                    if fixed != text:
                        pb.edit_main_text(key, vs.EDIT, p, fixed)
                d["price"] = f"{new:.2f}"
                print(f"auto-schedule: price fixed {name}: now ${new:.2f}")
                v = None
            if v:
                tag, reason = v
                print(f"auto-schedule: HOLD {name}: {tag} - {reason}")
                pb.mark_held(did)
                pb.note_attention([{"at": NOW.isoformat(timespec="seconds"), "deal": did, "tag": tag, "reason": reason,
                                    "item": name, "store": d.get("store", ""), "check": pb.camel_link(d)}])
                for p in posts:
                    ids = list(dict.fromkeys([t["id"] for t in p.get("tags") or []] + [vs.TAGS[tag]]))
                    pb.gql(key, vs.EDIT, {"input": {"id": p["id"], "tagIds": ids}})
                continue
            for p in posts:  # the ad disclosure on every affiliate post
                if pb.needs_disclosure(pb.full_text(p), d.get("store")) and pb.disclosure_missing(p):
                    pb.add_disclosure(key, vs.EDIT, p)
            for p in posts:  # link cards on Threads/Bluesky replies point at your link, not the plain store link
                try:
                    pb.set_reply_cards(key, vs.EDIT, p)
                except Exception as e:
                    print("   couldn't set the reply link card", p["id"], str(e)[:120])
            slot = next(slots, None)
            if slot is None:
                print("auto-schedule: no free slot in the next hours; the rest waits for the next run")
                break
            for p in posts:
                due = slot + timedelta(minutes=OFFSET.get(p["channelService"], 0))
                r = pb.gql(key, vs.EDIT, {"input": {"id": p["id"], "mode": "customScheduled", "saveToDraft": False,
                                                    "dueAt": due.isoformat(timespec="seconds")}})["editPost"]
                if r.get("__typename") != "PostActionSuccess":
                    print("   couldn't schedule", p["id"], r.get("message"))
            print(f"auto-schedule: scheduled {name} at {slot:%H:%M} ({detail or 'checked live'})")
            done += 1
    finally:
        browser.close()
    pb.save(pb.DRAFTS_PATH, list(drafts.values()))


if __name__ == "__main__":
    main()
