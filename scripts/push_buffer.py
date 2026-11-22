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
import urllib.parse
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
NEWS_DAILY_CAP = 6  # gaming news posts per day (they share the day's 30 with the deals)
NEWS_PER_SOURCE_CAP = 3
NEWS_SOURCES = {"PlayStation Blog", "Xbox Wire", "Gematsu"}
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


LEGO_GAME = re.compile(r"\((?:PS5|PS4|Xbox|Switch|PC|Steam)\b|\b(?:PS5|PS4|Xbox Series|Nintendo Switch|Switch 2|Steam key)\b", re.I)


GAME_LABEL = re.compile(r"\((?:[^)]*\b(?:PS5|PS4|Xbox|Switch|PC|Steam|Game-Key Card|digital|physical)\b)[^)]*\)", re.I)
DIGITAL_STORES = {"GOG", "Steam", "US PSN", "US eShop", "Fanatical", "Gamesplanet", "Epic", "Humble"}
CATEGORY_RULES = [  # (category, pattern), checked in this order after LEGO and games
    ("collectibles", r"\bfigures?\b|figurine|plush|statue|funko|\bamiibo\b|ornament|puzzle|poster|replica|\bpins?\b|keychain|\bmug\b|"
                     r"t-?shirt|hoodie|\bhat\b|apparel|socks|backpack|\bvinyl\b|soundtrack|art ?book|booster|trading card|\btcg\b|"
                     r"\bcards?\b.*\b(?:pokemon|pokémon|magic|yu-?gi-?oh)|collectible|youtooz|nendoroid|blanket|lamp|light"),
    ("accessories", r"controller|gamepad|joy-?con|dualsense|headset|keyboard|gaming mouse|wireless mouse|\bmouse\b|mouse ?pad|"
                    r"charg|\bdock\b|carrying case|travel case|protective case|\bcase for\b|\bgrips?\b|\bcable\b|adapter|"
                    r"\bstand\b|enclosure|\bssd\b|nvme|micro ?sd|memory card|storage expansion|hard drive|racing wheel|steering wheel|"
                    r"fight ?stick|arcade stick|capture card|webcam|microphone|\bmic\b|gaming chair|screen protector|thumb ?sticks?|"
                    r"\bpedals?\b|power bank|battery pack|cooling fan"),
    ("consoles", r"\bconsole\b|legion go|rog ally|steam deck|msi claw|switch 2 (?:system|bundle)|ps5 (?:pro|slim|digital|bundle)|"
                 r"playstation 5 (?:pro|slim|console|bundle)|xbox series [xs] (?:console|bundle)|playstation portal|meta quest|psvr"),
    ("movies_tv", r"blu-?ray|\b4k ultra hd\b|\bdvd\b|steelbook|criterion|complete series|\bseason\b|digital hd"),
    ("tech", r"\btv\b|television|monitor|laptop|macbook|chromebook|notebook|motherboard|graphics card|\bgpu\b|geforce|radeon|"
             r"\bcpu\b|processor|ryzen|intel core|router|wi-?fi|\bram\b|ddr[45]|power supply|\bpsu\b|pc case|ipad|tablet|"
             r"projector|soundbar|speaker|earbuds|headphones|airpods|smart ?watch|camera|desktop|gaming pc|\bmini pc\b|printer|kindle|echo"),
]
CATEGORY_RES = [(c, re.compile(p, re.I)) for c, p in CATEGORY_RULES]


def deal_category(d):
    """lego, game, collectibles, accessories, consoles, movies_tv, tech, other (news is counted separately)."""
    if d.get("kind") != "deal":
        return "news"
    if is_lego_set(d):
        return "lego"
    text = f"{d.get('item') or ''} {(d.get('posts') or {}).get('x', '')}"
    rx = dict(CATEGORY_RES)
    if rx["movies_tv"].search(text):
        return "movies_tv"
    if d.get("store") in DIGITAL_STORES or (GAME_LABEL.search(text) and not rx["accessories"].search(d.get("item") or "")):
        return "game"
    for cat in ("collectibles", "consoles", "tech", "accessories"):
        if rx[cat].search(text):
            return cat
    return "other"


def is_lego_set(d):
    """A LEGO building set (not a LEGO video game, which carries a console label)."""
    if d.get("kind") != "deal":
        return False
    text = f"{d.get('item') or ''} {(d.get('posts') or {}).get('x', '')}"
    return bool(re.search(r"\blego\b", text, re.I)) and not LEGO_GAME.search(text)


def create_draft(key, channel_id, text, picture="", alt="", tag_ids=(), pictures=None, alts=None, reply_link=False):
    # Amazon links go into drafts without a tracking ID (your amzn.to link carries it)
    text = re.sub(r"(https://www\.amazon\.com/[^\s?]+)\?tag=[^\s&]+(?:&(\S*))?", lambda m: m.group(1) + ("?" + m.group(2) if m.group(2) else ""), text)
    if pictures and len(pictures) > 1:  # sale pages: up to 4 pictures, each with its own alt text
        many = [{"image": {"url": u, "metadata": {"altText": (alts[i] if alts and i < len(alts) else alt)[:1000]}}}
                for i, u in enumerate(pictures[:4])]
        try:
            return _create(key, channel_id, text, many, tag_ids, reply_link)
        except RuntimeError as e:
            print("multiple pictures refused, sending the first only:", str(e)[:150])
    assets = [{"image": {"url": picture}}] if picture else []
    if assets and alt:
        try:
            return _create(key, channel_id, text, [{"image": {"url": picture, "metadata": {"altText": alt[:1000]}}}], tag_ids, reply_link)
        except RuntimeError as e:
            print("alt text refused, sending without it:", str(e)[:150])
    try:
        res = _create(key, channel_id, text, assets, tag_ids, reply_link)
    except RuntimeError:
        if not assets:
            raise
        res = _create(key, channel_id, text, [], tag_ids, reply_link)  # picture rejected: send the text anyway
        res["_no_picture"] = True
    return res


THREADS_TOPIC = (load(os.path.join(ROOT, "config.json"), {}) or {}).get("threads_topic", "Gaming Threads")
# deal-specific Threads communities, e.g. LEGO deals go to the LEGO community: [{"match": regex, "topic": name}]
THREADS_TOPICS = (load(os.path.join(ROOT, "config.json"), {}) or {}).get("threads_topics", [])


def threads_topic_for(text):
    for rule in THREADS_TOPICS:
        if re.search(rule.get("match", "$^"), text or "", re.I):
            return rule.get("topic") or THREADS_TOPIC
    return THREADS_TOPIC


def _service_of(channel_id):
    for c in load(CHANNELS_PATH, []) or []:
        if c.get("id") == channel_id:
            return c.get("service")
    return ""


POST_FIELDS = ("id text dueAt status channelService tags { id name } metadata { ... on TwitterPostMetadata { thread { text } } "
               "... on ThreadsPostMetadata { thread { text } topic } ... on BlueskyPostMetadata { thread { text } } }")
BASIC_FIELDS = "id text dueAt status channelService tags { id name }"


def gql_posts(key, query, variables):
    """Posts query with X thread parts; if Buffer ever rejects those fields, the same query without them."""
    try:
        return gql(key, query, variables)
    except Exception as e:
        print("posts query with thread parts failed, retrying without:", str(e)[:150])
        return gql(key, query.replace(POST_FIELDS, BASIC_FIELDS), variables)
URL_RE = re.compile(r"https?://\S+")


def full_text(node):
    """The post's text plus any thread parts (X posts with the link in the first reply)."""
    parts = [node.get("text") or ""]
    for t in ((node.get("metadata") or {}).get("thread") or [])[1:]:
        parts.append(t.get("text") or "")
    return "\n".join(parts)


def disclosure_missing(node):
    """True if the post that carries the link has no disclosure (the reply, for link-in-reply posts)."""
    thread = (node.get("metadata") or {}).get("thread") or []
    if len(thread) > 1:
        with_link = [t.get("text") or "" for t in thread if URL_RE.search(t.get("text") or "")]
        return any(not HAS_DISCLOSURE.search(t) for t in with_link)
    return not HAS_DISCLOSURE.search(node.get("text") or "")


def add_disclosure(key, mutation, node):
    """Add the disclosure where the link is: the reply for link-in-reply posts, else the post itself."""
    svc = node.get("channelService")
    meta = node.get("metadata") or {}
    thread = meta.get("thread") or []
    if len(thread) > 1:
        parts = [{"text": (with_disclosure(t.get("text") or "", svc) if URL_RE.search(t.get("text") or "") else (t.get("text") or ""))}
                 for t in thread]
        inp = {"id": node["id"], "text": parts[0]["text"]}
        if svc == "threads":
            inp["metadata"] = {"threads": {"type": "post", "thread": parts, **({"topic": meta["topic"]} if meta.get("topic") else {})}}
        else:
            inp["metadata"] = {svc or "twitter": {"thread": parts}}
        return gql(key, mutation, {"input": inp})
    return gql(key, mutation, {"input": {"id": node["id"], "text": with_disclosure(node.get("text") or "", svc)}})


def set_reply_cards(key, mutation, node):
    """Threads and Bluesky make a link card for a reply's link, but Buffer fills it with the plain store link
    (your tracking ID stripped). Point the card at the exact link in the reply instead, with no picture."""
    svc = node.get("channelService")
    meta = node.get("metadata") or {}
    thread = meta.get("thread") or []
    if svc not in ("threads", "bluesky") or len(thread) < 2:
        return False
    main = thread[0].get("text") or ""
    parts = [{"text": main}]
    for t in thread[1:]:
        text = t.get("text") or ""
        part = {"text": text}
        m = URL_RE.search(text)
        if m:
            host = urllib.parse.urlparse(m.group(0)).netloc.lower()
            site = "Amazon.com" if "amazon" in host else host.replace("www.", "")
            part["metadata"] = {svc: {"linkAttachment": {"url": m.group(0), "title": main[:200] or site,
                                                          "description": site, "thumbnail": ""}}}
        parts.append(part)
    body = {"thread": parts}
    if svc == "threads":
        body = {"type": "post", "thread": parts, **({"topic": meta["topic"]} if meta.get("topic") else {})}
    gql(key, mutation, {"input": {"id": node["id"], "metadata": {svc: body}}})
    return True


def edit_main_text(key, mutation, node, new_text):
    """Change the main text of a post; for X threads the first thread part changes with it."""
    inp = {"id": node["id"], "text": new_text}
    meta = node.get("metadata") or {}
    thread = meta.get("thread") or []
    if len(thread) > 1:
        parts = [{"text": new_text}] + [{"text": t.get("text") or ""} for t in thread[1:]]
        if node.get("channelService") == "threads":
            inp["metadata"] = {"threads": {"type": "post", "thread": parts, **({"topic": meta["topic"]} if meta.get("topic") else {})}}
        else:
            inp["metadata"] = {node.get("channelService") or "twitter": {"thread": parts}}
    return gql(key, mutation, {"input": inp})


def split_link(text):
    """Main post without the link (disclosure kept), and the link on its own for the first reply."""
    m = URL_RE.search(text or "")
    if not m:
        return text, ""
    main = re.sub(r"\s{2,}", " ", (text[:m.start()] + text[m.end():])).strip()
    return main, m.group(0)


def _create(key, channel_id, text, assets, tag_ids=(), reply_link=False):
    inp = {
        "text": text,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "saveToDraft": True,
        "assets": assets,
        "tagIds": list(tag_ids),
        "source": "price-notes",
    }
    if THREADS_TOPIC and _service_of(channel_id) == "threads":
        inp["metadata"] = {"threads": {"type": "post", "topic": threads_topic_for(text)}}  # Threads topic tag / community
    svc = _service_of(channel_id)
    if reply_link and svc in ("twitter", "threads", "bluesky"):
        main, link = split_link(text)
        if link:  # deal posts on X and Threads: the link goes in the first reply (link posts are shown to fewer people)
            had = bool(HAS_DISCLOSURE.search(main))
            main = re.sub(r"\s*(?:#ad|\[ad\])(?=\s|$)", "", main, flags=re.I).strip()  # disclosure goes with the link
            inp["text"] = main
            reply = with_disclosure(link, svc) if had else link
            thread = [{"text": main, "assets": assets}, {"text": reply}]
            if svc == "threads":
                inp["metadata"] = {"threads": {"type": "post", "topic": threads_topic_for(text), "thread": thread}}
            else:  # twitter / bluesky
                inp["metadata"] = {svc: {"thread": thread}}
    data = gql(key, CREATE, {"input": inp})
    res = data["createPost"]
    if res.get("__typename") != "PostActionSuccess":
        raise RuntimeError(f"{res.get('__typename')}: {res.get('message', '')}"[:300])
    return {"id": res["post"]["id"]}


TAGS = (load(os.path.join(ROOT, "config.json"), {}) or {}).get("buffer_tags", {})
ATTENTION_PATH = os.path.join(DATA, "attention.json")
HELD_PATH = os.path.join(DATA, "held_deals.json")
# deal posts with the link in the first reply instead of the main post (config "link_in_reply": apps)
LINK_IN_REPLY = set((load(os.path.join(ROOT, "config.json"), {}) or {}).get("link_in_reply", []))


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
        if d.get("store") == "Walmart":
            d["skip"] = "Walmart removed (no pictures)"
        elif d.get("kind") == "deal" and d.get("store") != "Amazon" and str(d.get("picture_type", "")).startswith(("needs picture", "no picture")):
            d["skip"] = "no picture could be made"
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
    news_today = sum(n for st, n in per_store.items() if st in NEWS_SOURCES)
    # every kind of deal has its own daily cap (config "category_caps"; LEGO sets 1), so no one kind floods the feed
    cfg_all = load(os.path.join(ROOT, "config.json"), {}) or {}
    caps = dict(cfg_all.get("category_caps", {}))
    caps.setdefault("lego", int(cfg_all.get("lego_daily_cap", 1)))
    cat_today = {}
    for did, v in sent.items():
        if "posts" in v and did in by_id and datetime.fromisoformat(v["at"]).astimezone(LOCAL).date() == today_local:
            c = deal_category(by_id[did])
            cat_today[c] = cat_today.get(c, 0) + 1
    kept = []
    for d in candidates:
        c = deal_category(d)
        if c in caps and cat_today.get(c, 0) >= caps[c]:
            continue  # this kind is full for today: left for later (it ages out if the day stays full)
        cat_today[c] = cat_today.get(c, 0) + 1
        kept.append(d)
    candidates = kept
    amazon = [d for d in candidates if d.get("store") == "Amazon"]
    queues = {}
    for d in candidates:
        if d.get("kind") == "news":
            if news_today >= NEWS_DAILY_CAP or per_store.get(d.get("store", ""), 0) >= NEWS_PER_SOURCE_CAP:
                continue
        elif d.get("store") == "Amazon" or per_store.get(d.get("store", ""), 0) >= STORE_DAILY_CAP:
            continue
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
            reply_link = d.get("kind") == "deal" and c["service"] in LINK_IN_REPLY
            try:
                r = create_draft(key, c["id"], text, d.get("picture") or "", d.get("alt") or "",
                                 [TAGS[n] for n in draft_tags(d)], d.get("pictures"), d.get("alts"), reply_link)
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
