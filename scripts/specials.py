"""Special posts, saved to Buffer as DRAFTS only (nothing is scheduled or published).

  python scripts/specials.py free      Free games you can keep this week (Epic, Steam, GOG), verified with each store
  python scripts/specials.py roundup   Today's best deals: a thread with a picture grid, one reply per deal

Every fact in these posts comes straight from the store's own data at the time the draft is made.
"""
import io
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402  (fetch, parse_date, APP_UA)
import push_buffer as pb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SHOTS = os.path.join(ROOT, "shots")
SENT_PATH = os.path.join(DATA, "specials_sent.json")
REPO_RAW = "https://raw.githubusercontent.com/" + os.environ.get("GITHUB_REPOSITORY", "jwest1985/price-notes") + "/images/"
LOCAL = ZoneInfo("America/Toronto")
NOW = datetime.now(timezone.utc)
TODAY = NOW.astimezone(LOCAL).date()
EPIC_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=US&allowCountries=US"
LIMITS = {"x": 280, "bluesky": 300, "threads": 500}


def jget(url, ua=None):
    return json.loads(collect.fetch(url, ua=ua or collect.APP_UA))


# --------------------------------------------------------------------------- pictures

def get_image(url):
    from PIL import Image
    if not url:
        return None
    try:
        im = Image.open(io.BytesIO(raw(url)))
        return im.convert("RGB")
    except Exception as e:
        print("image failed:", url[:80], str(e)[:80])
        return None


def raw(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": collect.APP_UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def collage(images, cell_w=640):
    """Pictures side by side on white, each shown whole (no cropping), evenly spaced."""
    from PIL import Image
    images = [i for i in images if i][:4]
    if not images:
        return None
    aspects = sorted(i.width / i.height for i in images)
    a = min(1.8, max(0.7, aspects[len(aspects) // 2]))
    cw, ch = cell_w, int(cell_w / a)
    cols = 1 if len(images) == 1 else 2
    rows = (len(images) + cols - 1) // cols
    gap = 16
    W, H = cols * cw + (cols + 1) * gap, rows * ch + (rows + 1) * gap
    out = Image.new("RGB", (W, H), (255, 255, 255))
    for n, im in enumerate(images):
        r, c = divmod(n, cols)
        if r == rows - 1 and len(images) % cols and cols > 1:  # centre a lone last picture
            x0 = (W - cw) // 2
        else:
            x0 = gap + c * (cw + gap)
        fitted = im.copy()
        fitted.thumbnail((cw, ch))
        out.paste(fitted, (x0 + (cw - fitted.width) // 2, gap + r * (ch + gap) + (ch - fitted.height) // 2))
    return out


def save_picture(img, name):
    import snap
    os.makedirs(SHOTS, exist_ok=True)
    img = snap.pad_to_shape(img, 0.75, 2.0)
    snap.save_fit(img, os.path.join(SHOTS, name))
    return REPO_RAW + name


# --------------------------------------------------------------------------- posting

def x_len(text):
    return len(re.sub(r"https?://\S+", "x" * 23, text))  # X counts every link as 23


def fits(texts):
    return all((x_len(t) if k == "x" else len(t)) <= LIMITS[k] for k, t in texts.items())


PENDING_PATH = os.path.join(DATA, "specials_pending.json")
SPECIAL_POSTS_PATH = os.path.join(DATA, "specials_posts.json")


def send(parts, label):
    """Step 1 (before pictures are uploaded): only remember what to send. Step 2 (`send`) sends it."""
    pending = pb.load(PENDING_PATH, [])
    pending.append({"parts": parts, "label": label})
    pb.save(PENDING_PATH, pending)
    print(f"{label}: prepared ({len(parts)} part(s))")
    return True


def send_now(parts, label):
    """parts: list of {"x":..,"threads":..,"bluesky":..} texts; the first part may carry a picture.
    One part = a normal draft; several = a thread draft (falls back to separate numbered drafts)."""
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    if not key:
        print("no BUFFER_API_KEY; would send:", json.dumps(parts, indent=1)[:3000])
        return False
    paused = set((pb.load(os.path.join(ROOT, "config.json"), {}) or {}).get("paused_apps", []))
    channels = [c for c in (pb.load(pb.CHANNELS_PATH, []) or pb.get_channels(key)) if c["service"] not in paused]
    ok = True
    for c in channels:
        svc = pb.SERVICE_TEXT[c["service"]]
        meta_key = {"x": "twitter", "threads": "threads", "bluesky": "bluesky"}[svc]
        texts = [p[svc] for p in parts]
        if pb.needs_disclosure(" ".join(texts)):  # affiliate links only: the disclosure goes on the first post
            texts = [pb.with_disclosure(texts[0], c["service"])] + texts[1:]
        pic = parts[0].get("picture")
        assets = [{"image": {"url": pic, "metadata": {"altText": parts[0].get("alt", "")[:1000]}}}] if pic else []
        try:
            inp = {"text": texts[0], "channelId": c["id"], "schedulingType": "automatic", "mode": "addToQueue",
                   "saveToDraft": True, "assets": assets, "source": "price-notes"}
            if "amazon.com" not in " ".join(texts) and re.search(r"https?://", " ".join(texts)) and "Turn on shortening link" in pb.TAGS:
                inp["tagIds"] = [pb.TAGS["Turn on shortening link"]]
            if len(texts) > 1:
                thread = [{"text": t, "assets": assets if i == 0 else []} for i, t in enumerate(texts)]
                inp["metadata"] = {meta_key: {"thread": thread}}
            if meta_key == "threads" and pb.THREADS_TOPIC:
                inp.setdefault("metadata", {}).setdefault("threads", {}).update(
                    {"type": "post", **({"topic": pb.threads_topic_for(texts[0])} if pb.threads_use_topic() else {})})
            data = pb.gql(key, pb.CREATE, {"input": inp})
            res = data["createPost"]
            if res.get("__typename") != "PostActionSuccess":
                raise RuntimeError(res.get("message", res.get("__typename")))
            print(f"{label} -> {c['service']} draft {res['post']['id']} ({len(texts)} part(s))")
            if parts[0].get("games"):  # so the free games can be re-checked before this post goes out
                sp = pb.load(SPECIAL_POSTS_PATH, {})
                sp[res["post"]["id"]] = {"label": label, "games": parts[0]["games"], "at": NOW.isoformat(timespec="seconds")}
                pb.save(SPECIAL_POSTS_PATH, sp)
        except Exception as e:
            print(f"{label} {c['service']} thread failed: {str(e)[:200]}; sending numbered drafts instead")
            for i, t in enumerate(texts):
                try:
                    pb._create(key, c["id"], f"({i + 1}/{len(texts)}) {t}" if len(texts) > 1 else t,
                               assets if i == 0 else [], inp.get("tagIds", []))
                except Exception as e2:
                    print("   failed:", str(e2)[:200])
                    ok = False
    return ok


# --------------------------------------------------------------------------- free games

def free_epic():
    out = []
    data = jget(EPIC_URL)
    for g in data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", []):
        offers = [o for p in ((g.get("promotions") or {}).get("promotionalOffers") or [])
                  for o in p.get("promotionalOffers", [])]
        live = [o for o in offers if (o.get("discountSetting") or {}).get("discountPercentage") == 0
                and collect.parse_date(o.get("startDate")) and collect.parse_date(o.get("startDate")) <= NOW
                <= (collect.parse_date(o.get("endDate")) or NOW)]
        price = ((g.get("price") or {}).get("totalPrice") or {})
        if not live or price.get("discountPrice") not in (0, None) or not price.get("originalPrice"):
            continue  # verified: on a 100%-off promo right now, normally paid
        slug = g.get("productSlug") or ((g.get("catalogNs") or {}).get("mappings") or [{}])[0].get("pageSlug")
        if not slug:
            continue
        imgs = {i.get("type"): i.get("url") for i in g.get("keyImages") or []}
        out.append({"title": g["title"].strip(), "url": f"https://store.epicgames.com/en-US/p/{slug}",
                    "until": collect.parse_date(live[0].get("endDate")),
                    "image": imgs.get("OfferImageWide") or imgs.get("DieselStoreFrontWide") or ""})
    return out


def free_cheapshark(store_id):
    return jget(f"https://www.cheapshark.com/api/1.0/deals?storeID={store_id}&upperPrice=0&pageSize=20")


def free_steam():
    out = []
    for d in free_cheapshark(1):
        app = d.get("steamAppID")
        if not app:
            continue
        info = jget(f"https://store.steampowered.com/api/appdetails?appids={app}&cc=us&filters=price_overview,basic")
        po = ((info.get(str(app)) or {}).get("data") or {}).get("price_overview") or {}
        if po.get("final") == 0 and (po.get("initial") or 0) > 0:  # verified by Steam: paid game at $0 now
            name = (((info.get(str(app)) or {}).get("data") or {}).get("name") or d["title"]).strip()
            out.append({"title": name, "url": f"https://store.steampowered.com/app/{app}",
                        "image": f"https://cdn.akamai.steamstatic.com/steam/apps/{app}/header.jpg", "until": None})
    return out


def free_gog():
    out = []
    for d in free_cheapshark(7):
        q = urllib.parse.quote(d["title"])
        res = jget(f"https://catalog.gog.com/v1/catalog?limit=5&query=like:{q}&countryCode=US&currencyCode=USD")
        for p in res.get("products", []):
            pr = p.get("price") or {}
            final = (pr.get("finalMoney") or {}).get("amount")
            base = (pr.get("baseMoney") or {}).get("amount")
            if p.get("title", "").lower() == d["title"].lower() and final in ("0.00", "0") and base not in ("0.00", "0", None):
                img = p.get("coverHorizontal") or ""
                out.append({"title": p["title"].strip(), "image": ("https:" + img) if img.startswith("//") else img,
                            "url": p.get("storeLink") or f"https://www.gog.com/en/game/{p.get('slug')}", "until": None})
                break
    return out


def and_list(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def free_games():
    stores = []
    for name, fn in [("Epic Games Store", free_epic), ("Steam", free_steam), ("GOG", free_gog)]:
        try:
            games = fn()
        except Exception as e:
            print(f"{name}: couldn't check ({str(e)[:120]}); left out")
            games = []
        print(f"{name}: {[g['title'] for g in games]}")
        if games:
            stores.append((name, games))
    if not stores:
        print("No verified free games right now; no draft.")
        return
    lines = []
    for name, games in stores:
        ends = {g["until"].astimezone(LOCAL).strftime("%b %-d") for g in games if g.get("until")}
        until = f" (free until {ends.pop()})" if len(ends) == 1 else ""
        if name == "Epic Games Store" and len(games) > 1:
            lines.append(f"{name}: {and_list([g['title'] for g in games])}{until} https://store.epicgames.com/en-US/free-games")
        else:
            lines += [f"{name}: {g['title']}{until} {g['url']}" for g in games]
    head = "Free games you can keep right now:"
    single = head + "\n\n" + "\n".join(lines)
    games_all = [g for _, gs in stores for g in gs]
    pic = collage([get_image(g["image"]) for g in games_all])
    picture = save_picture(pic, f"{TODAY:%Y%m%d}-free-games.jpg") if pic else ""
    alt = and_list([g["title"] for g in games_all[:4]]) + " store artwork"
    listed = [{"store": name, "title": g["title"]} for name, gs in stores for g in gs]
    if fits({"x": single, "bluesky": single, "threads": single}):
        parts = [{"x": single, "threads": single, "bluesky": single, "picture": picture, "alt": alt}]
    else:
        parts = [{"x": head + "\n\n" + lines[0], "threads": head + "\n\n" + lines[0], "bluesky": head + "\n\n" + lines[0],
                  "picture": picture, "alt": alt}] + [{"x": l, "threads": l, "bluesky": l} for l in lines[1:]]
    parts[0]["games"] = listed
    send(parts, "free games")


# --------------------------------------------------------------------------- evening round-up

def pct_off(text):
    m = re.search(r"(\d+)% off", text, re.I)
    return int(m.group(1)) if m else 0


POSTS_Q = """query($input: PostsInput!) { posts(first: 100, input: $input) { edges { node { id } } } }"""


def kept_x_post_ids(statuses):
    """IDs of X posts that still exist in Buffer with these statuses (deleted drafts don't count)."""
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    channels = pb.load(pb.CHANNELS_PATH, [])
    x = [c["id"] for c in channels if c["service"] in ("twitter", "x")]
    org = os.environ.get("BUFFER_ORG_ID", "6aff0fe7278b8418e606868b")
    data = pb.gql(key, POSTS_Q, {"input": {"organizationId": org, "filter": {"status": statuses, "channelIds": x}}})
    return {e["node"]["id"] for e in data["posts"]["edges"]}


def roundup():
    sent = pb.load(pb.SENT_PATH, {})
    drafts = {d["id"]: d for d in pb.load(pb.DRAFTS_PATH, [])}
    # only deals you kept: scheduled or posted (and, for hand-started test runs, still-waiting drafts)
    statuses = ["sent", "scheduled"] + (["draft"] if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" else [])
    try:
        kept = kept_x_post_ids(statuses)
    except Exception as e:
        print("couldn't read Buffer posts:", str(e)[:200], "- no round-up")
        return
    todays = [drafts[k] for k, v in sent.items() if "posts" in v and k in drafts
              and datetime.fromisoformat(v["at"]).astimezone(LOCAL).date() == TODAY
              and (v["posts"].get("twitter") or v["posts"].get("x")) in kept]
    if len(todays) < 3:
        print(f"Only {len(todays)} deals today; no round-up.")
        return
    todays.sort(key=lambda d: d["found_at"], reverse=True)
    todays.sort(key=lambda d: (0 if d.get("store") == "Amazon" else 1, -pct_off(d["posts"]["x"])))
    top = todays[:5]
    with_pics = [d for d in top if d.get("picture") and d.get("store") != "Amazon"][:4]  # never Amazon images
    pic = collage([get_image(d["picture"]) for d in with_pics])
    picture = save_picture(pic, f"{TODAY:%Y%m%d}-roundup.jpg") if pic else ""
    alt = (and_list([d.get("item") or re.split(r" (?:is|are) | now | up for ", d["posts"]["x"])[0] for d in with_pics])
           + " deal pictures") if with_pics else ""
    head = "Some of today's best deals, all in one thread:"
    parts = [{"x": head, "threads": head, "bluesky": head, "picture": picture, "alt": alt}]
    parts += [{"x": d["posts"]["x"], "threads": d["posts"].get("threads") or d["posts"]["x"],
               "bluesky": d["posts"].get("bluesky") or d["posts"]["x"]} for d in top]
    send(parts, "round-up")


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if mode == "send":  # after the pictures are published
        for job in pb.load(PENDING_PATH, []):
            send_now(job["parts"], job["label"])
        if os.path.exists(PENDING_PATH):
            os.remove(PENDING_PATH)
        sys.exit(0)
    if mode not in ("free", "roundup"):
        sys.exit("usage: specials.py free|roundup|send")
    done = pb.load(SENT_PATH, {})
    tag = f"{mode}:{TODAY.isoformat()}" if mode == "roundup" else f"{mode}:{TODAY.isocalendar()[0]}-W{TODAY.isocalendar()[1]}"
    if tag in done and os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        print("already made:", tag)
        sys.exit(0)
    {"free": free_games, "roundup": roundup}[mode]()
    done[tag] = NOW.isoformat(timespec="seconds")
    pb.save(SENT_PATH, done)
