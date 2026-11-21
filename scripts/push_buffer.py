"""Send new drafts to Buffer as drafts on X, Threads and Bluesky.

Runs after collect.py in the same GitHub Actions job. Needs the BUFFER_API_KEY secret.
Nothing is published: every post is saved with saveToDraft=true for manual review.
The key is never printed or written to the repo (the repo and its logs are public).
"""
import json
import re
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DRAFTS_PATH = os.path.join(DATA, "drafts.json")
SENT_PATH = os.path.join(DATA, "buffer_sent.json")
CHANNELS_PATH = os.path.join(DATA, "buffer_channels.json")
STATUS_PATH = os.path.join(DATA, "buffer_status.json")
API = "https://api.buffer.com"

# Buffer allows 1,800 requests a day; at 96 runs a day this keeps the worst case under ~1,650
MAX_DRAFTS_PER_RUN = 5
STORE_DAILY_CAP = 6  # most drafts per day from one non-Amazon store, so no single store floods the feed
# 30 posts a day per account = 30 deals a day (each deal becomes 3 drafts, 90 in total)
DAILY_LIMIT = 30
LOCAL = ZoneInfo("America/Toronto")
DAY_START_HOUR = 6  # runs start at 6 AM local
# don't send stale deals: only drafts found within this window
MAX_AGE = timedelta(hours=6)
# which draft text goes to which Buffer service
SERVICE_TEXT = {"twitter": "x", "x": "x", "threads": "threads", "bluesky": "bluesky"}

NOW = datetime.now(timezone.utc)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def gql(key, query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "PriceNotes/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    if out.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "") for e in out["errors"])[:400])
    return out["data"]


def get_channels(key):
    data = gql(key, "query { account { organizations { id name } } }")
    orgs = data["account"]["organizations"]
    channels = []
    for org in orgs:
        d = gql(key, "query($input: ChannelsInput!) { channels(input: $input) { id name service } }",
                {"input": {"organizationId": org["id"]}})
        for c in d["channels"]:
            if c["service"].lower() in SERVICE_TEXT:
                channels.append({"id": c["id"], "name": c["name"], "service": c["service"].lower()})
    return channels


CREATE = """
mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id } }
    ... on MutationError { message }
  }
}"""


DISCLOSURE = {"twitter": "#ad", "bluesky": "#ad", "threads": "[ad]", "x": "#ad"}
HAS_DISCLOSURE = re.compile(r"(?:^|\s)(?:#ad|\[ad\])(?=\s|$|[.,!?)])", re.I)


AFFILIATE_STORES = {x.lower() for x in ((load(os.path.join(ROOT, "config.json"), {}) or {}).get("affiliate_stores") or ["Amazon"])}
AFFILIATE_LINK = re.compile(r"https?://(?:www\.)?(?:amazon\.com|amzn\.to|link\.amazon|a\.co)/", re.I)


def needs_disclosure(text="", store=""):
    """Only posts that can earn you money: an affiliate store (Amazon for now, see config "affiliate_stores")
    or an Amazon link in the text."""
    return (store or "").lower() in AFFILIATE_STORES or bool(AFFILIATE_LINK.search(text or ""))


def with_disclosure(text, service):
    """Every deal post carries the ad disclosure: #ad on X and Bluesky, [ad] on Threads. Never removed, never doubled."""
    text = text or ""
    if HAS_DISCLOSURE.search(text):
        return text
    return text.rstrip() + " " + DISCLOSURE.get(service, "#ad")


def create_draft(key, channel_id, text, picture="", alt="", tag_ids=(), pictures=None, alts=None):
    # Amazon links go into drafts without a tracking ID (your amzn.to link carries it)
    text = re.sub(r"(https://www\.amazon\.com/[^\s?]+)\?tag=[^\s&]+(?:&(\S*))?", lambda m: m.group(1) + ("?" + m.group(2) if m.group(2) else ""), text)
    if pictures and len(pictures) > 1:  # sale pages: up to 4 pictures, each with its own alt text
        many = [{"image": {"url": u, "metadata": {"altText": (alts[i] if alts and i < len(alts) else alt)[:1000]}}}
                for i, u in enumerate(pictures[:4])]
        try:
            return _create(key, channel_id, text, many, tag_ids)
        except RuntimeError as e:
            print("multiple pictures refused, sending the first only:", str(e)[:150])
    assets = [{"image": {"url": picture}}] if picture else []
    if assets and alt:
        try:
            return _create(key, channel_id, text, [{"image": {"url": picture, "metadata": {"altText": alt[:1000]}}}], tag_ids)
        except RuntimeError as e:
            print("alt text refused, sending without it:", str(e)[:150])
    try:
        res = _create(key, channel_id, text, assets, tag_ids)
    except RuntimeError:
        if not assets:
            raise
        res = _create(key, channel_id, text, [], tag_ids)  # picture rejected: send the text anyway
        res["_no_picture"] = True
    return res


def _create(key, channel_id, text, assets, tag_ids=()):
    data = gql(key, CREATE, {"input": {
        "text": text,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "saveToDraft": True,
        "assets": assets,
        "tagIds": list(tag_ids),
        "source": "price-notes",
    }})
    res = data["createPost"]
    if res.get("__typename") != "PostActionSuccess":
        raise RuntimeError(f"{res.get('__typename')}: {res.get('message', '')}"[:300])
    return {"id": res["post"]["id"]}


TAGS = (load(os.path.join(ROOT, "config.json"), {}) or {}).get("buffer_tags", {})
ATTENTION_PATH = os.path.join(DATA, "attention.json")
HELD_PATH = os.path.join(DATA, "held_deals.json")


def mark_held(deal_id):
    """A deal pulled back to Drafts with a hold tag: another deal takes its place in the day's count."""
    held = load(HELD_PATH, [])
    if deal_id and deal_id not in held:
        held = (held + [deal_id])[-500:]
        save(HELD_PATH, held)


def draft_tags(d):
    """What you need to do with this draft, as Buffer tag names."""
    names = list(d.get("flags") or [])
    if d.get("store") == "Amazon":
        names.append("Swap Amazon link")
    elif d.get("kind") == "deal" and re.search(r"https?://", " ".join((d.get("posts") or {}).values())):
        names.append("Turn on shortening link")  # Buffer's link shortener: never on Amazon links
    if str(d.get("picture_type", "")).startswith("needs picture") and d.get("store") != "Amazon":
        names.append("Needs picture")  # never on Amazon posts: they don't get pictures
    return [n for n in dict.fromkeys(names) if n in TAGS]


def camel_link(d):
    """One-click price check page for Amazon (camelcamelcamel) and Walmart (camelmart) products."""
    import re
    m = re.search(r"amazon\.com/(?:[^/]+/)?dp/([A-Z0-9]{10})", d.get("url") or "")
    if m:
        return f"https://camelcamelcamel.com/product/{m.group(1)}"
    m = re.search(r"walmart\.com/ip/(?:[^/]+/)?(\d+)", d.get("url") or "")
    return f"https://camelmart.com/product/{m.group(1)}" if m else ""


SHORT = {"Swap Amazon link": "Swap Amazon link", "Turn on shortening link": "Turn on shortening",
         "Price went up": "Price went up", "Out of stock": "Out of stock", "Deal ended": "Deal ended",
         "Couldn't verify": "Couldn't verify", "Price dropped": "Price dropped", "Check promo code": "Check promo code",
         "Needs picture": "Needs picture"}


def notify_phone(entries):
    """Push a note to your phone (ntfy app) when drafts need you. Topic is the private NTFY_TOPIC secret."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not entries:
        return
    seen, lines = set(), []
    for a in entries:
        k = (a.get("tag"), a.get("item"))
        if k in seen:
            continue
        seen.add(k)
        price = f" ${a['price']}" if a.get("price") else ""
        lines.append(f"{SHORT.get(a.get('tag'), a.get('tag'))}: {a.get('item', '')[:70]}{price}")
    n = len(lines)
    body = "\n".join(lines[:12]) + (f"\n...and {n - 12} more" if n > 12 else "")
    title = f"{n} draft{'s' if n != 1 else ''} need{'s' if n == 1 else ''} you" if n else "Drafts need you"
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), method="POST",
                                 headers={"Title": title, "Click": "https://publish.buffer.com/drafts", "Tags": "bell"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("phone note failed:", type(e).__name__)


def note_attention(entries):
    """Keep a list of posts that need you, grouped by what needs doing (data/attention.json + .md)."""
    notify_phone(entries)
    att = load(ATTENTION_PATH, [])
    att = entries + att
    cutoff = NOW - timedelta(days=3)
    att = [a for a in att if datetime.fromisoformat(a["at"]) > cutoff][:300]
    save(ATTENTION_PATH, att)
    lines = ["# Needs your attention", "", "Newest first. Each of these posts is tagged in Buffer with the same name, "
             "so you can filter Drafts by tag.", ""]
    for tag in TAGS:
        group = [a for a in att if a["tag"] == tag]
        if not group:
            continue
        lines += [f"## {tag} ({len(group)})", ""]
        for a in group:
            when = datetime.fromisoformat(a["at"]).astimezone(ZoneInfo("America/Toronto")).strftime("%b %-d %-I:%M %p")
            check = f" - check: {a['check']}" if a.get("check") else ""
            lines.append(f"- {when}: {a['item']} ({a.get('store', '')}) - {a['reason']}{check}")
        lines.append("")
    with open(os.path.join(DATA, "attention.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    status = {"run_at": NOW.isoformat(timespec="seconds"), "sent": 0, "errors": []}
    if not key:
        status["errors"].append("BUFFER_API_KEY secret is not set; skipped")
        save(STATUS_PATH, status)
        return

    first_run = not os.path.exists(SENT_PATH)
    sent = load(SENT_PATH, {})
    drafts = load(DRAFTS_PATH, [])

    channels = load(CHANNELS_PATH, [])
    if not channels or os.environ.get("BUFFER_REFRESH_CHANNELS"):
        try:
            channels = get_channels(key)
            save(CHANNELS_PATH, channels)
        except Exception as e:
            status["errors"].append(f"channels: {e}")
            save(STATUS_PATH, status)
            print("Buffer channel lookup failed:", e)
            return
    status["channels"] = [f"{c['service']}:{c['name']}" for c in channels]
    if not channels:
        status["errors"].append("No X, Threads or Bluesky channels found in Buffer")
        save(STATUS_PATH, status)
        return

    # first time: don't flood Buffer with the backlog, only send the newest few
    candidates = []
    for d in drafts:
        if d["id"] in sent or d.get("status") != "new":
            continue
        if d.get("kind") == "deal" and "picture" not in d:
            continue  # wait until the picture step has handled it
        if d.get("kind") == "deal" and len(re.sub(r"[^A-Za-z0-9]", "", d.get("item") or "")) < 3:
            d["skip"] = "no product name"
        if d.get("skip"):
            sent[d["id"]] = {"at": NOW.isoformat(timespec="seconds"), "skipped": d["skip"]}
            continue
        try:
            found = datetime.fromisoformat(d["found_at"])
        except Exception:
            found = NOW
        if NOW - found > MAX_AGE:
            continue
        candidates.append(d)
    # Amazon first (the only affiliate program for now), but other stores get turns too, like Wario64's feed:
    # Amazon, other store, Amazon, other store... The other stores take turns (GOG, Steam, Walmart, PlayStation,
    # eShop, Fangamer...), newest first, and no single non-Amazon store gets more than STORE_DAILY_CAP a day.
    candidates.sort(key=lambda d: d["found_at"], reverse=True)
    by_id = {d["id"]: d for d in drafts}
    today_local = NOW.astimezone(LOCAL).date()
    per_store = {}
    for did, v in sent.items():
        if "posts" in v and datetime.fromisoformat(v["at"]).astimezone(LOCAL).date() == today_local:
            st = (by_id.get(did) or {}).get("store", "")
            per_store[st] = per_store.get(st, 0) + 1
    amazon = [d for d in candidates if d.get("store") == "Amazon"]
    queues = {}
    for d in candidates:
        if d.get("store") != "Amazon" and per_store.get(d.get("store", ""), 0) < STORE_DAILY_CAP:
            queues.setdefault(d.get("store", ""), []).append(d)
    others = []
    while any(queues.values()):  # one from each store in turn
        for st in list(queues):
            if queues[st]:
                others.append(queues[st].pop(0))
    mixed = []
    while amazon or others:
        if amazon:
            mixed.append(amazon.pop(0))
        if others:
            mixed.append(others.pop(0))
    candidates = mixed
    if first_run:
        for d in drafts:
            if d not in candidates[:3]:
                sent.setdefault(d["id"], {"at": NOW.isoformat(timespec="seconds"), "skipped": "backlog"})
        candidates = candidates[:3]

    # daily limit, paced so evening deals still get room: about 4 at 6 AM, then ~1.6 more each hour
    local_now = NOW.astimezone(LOCAL)
    today = local_now.date()
    held = set(load(HELD_PATH, []))  # held deals (price went up, out of stock...) don't use up the day's 30
    sent_today = sum(1 for did, v in sent.items() if "posts" in v and did not in held
                     and datetime.fromisoformat(v["at"]).astimezone(LOCAL).date() == today)
    hours_in = max(0.0, (local_now.hour + local_now.minute / 60) - DAY_START_HOUR)
    allowed_now = min(DAILY_LIMIT, 4 + int(1.6 * hours_in + 0.999))
    room = max(0, allowed_now - sent_today)
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and os.environ.get("TIMER_RUN") != "true":
        room = MAX_DRAFTS_PER_RUN  # hand-started test runs aren't held back by the daily pace
    status["sent_today"] = sent_today
    status["daily_limit"] = DAILY_LIMIT
    if room == 0 and candidates:
        status["errors"].append(f"daily pace reached ({sent_today}/{DAILY_LIMIT}); {len(candidates)} waiting")

    attention = []
    for d in candidates[:min(MAX_DRAFTS_PER_RUN, room)]:
        posted = {}
        for c in channels:
            text = d["posts"].get(SERVICE_TEXT[c["service"]]) or d["posts"].get("x")
            if needs_disclosure(text, d.get("store")):
                text = with_disclosure(text, c["service"])
            try:
                r = create_draft(key, c["id"], text, d.get("picture") or "", d.get("alt") or "",
                                 [TAGS[n] for n in draft_tags(d)], d.get("pictures"), d.get("alts"))
                posted[c["service"]] = r["id"]
                if r.get("_no_picture"):
                    status["errors"].append(f"{d['id']} {c['service']}: Buffer refused the picture, sent text only")
            except Exception as e:
                status["errors"].append(f"{d['id']} {c['service']}: {e}")
            time.sleep(0.5)
        if posted:
            sent[d["id"]] = {"at": NOW.isoformat(timespec="seconds"), "posts": posted}
            status["sent"] += 1
            reasons = {"Swap Amazon link": "replace the long Amazon link with your amzn.to link (X post; copy to the others)",
                       "Needs picture": str(d.get("picture_type", "")).replace("needs picture", "no picture").strip(),
                       "Check promo code": f"code {d.get('code')} wasn't found on the store's own page: confirm it there or remove it",
                       "Couldn't verify": d.get("verify_note") or "the price couldn't be confirmed at the store"}
            attention += [{"at": NOW.isoformat(timespec="seconds"), "deal": d["id"], "tag": n, "reason": reasons.get(n, n),
                           "item": d.get("item") or d["posts"]["x"][:60], "store": d.get("store", ""), "posts": posted,
                           "check": camel_link(d), "price": d.get("price")}
                          for n in draft_tags(d)]
    status["sent_today"] = sent_today + status["sent"]

    # forget entries older than two weeks
    cutoff = NOW - timedelta(days=14)
    sent = {k: v for k, v in sent.items() if datetime.fromisoformat(v["at"]) > cutoff}
    save(SENT_PATH, sent)
    save(STATUS_PATH, status)
    if attention:
        note_attention(attention)
    print(f"Buffer: sent {status['sent']} drafts to {len(channels)} channels; {len(status['errors'])} errors")
    for e in status["errors"]:
        print(" -", e)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the whole job over Buffer
        print("Buffer step error:", e)
        sys.exit(0)
