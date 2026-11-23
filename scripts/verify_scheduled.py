"""Re-check every scheduled deal post shortly before it goes out (runs inside each pipeline run).

For each deal with a post due in the next WINDOW minutes, the deal is checked live at its store.
- Still live at the posted price: nothing happens, it posts.
- Price went up / out of stock / deal ended / price dropped / couldn't be checked: every scheduled post of that deal
  (X, Threads, Bluesky) is moved back to Drafts and tagged in Buffer with the reason, and listed in data/attention.md.
Nothing is ever published or deleted by this script.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402
import push_buffer as pb  # noqa: E402

WINDOW = timedelta(minutes=25)
CARDED_PATH = os.path.join(pb.DATA, "reply_cards_set.json")
SMALL_DROP = 0.05  # drops under 5% are corrected automatically; 5% or more are held for review
NOW = datetime.now(timezone.utc)
LOCAL = ZoneInfo("America/Toronto")
ORG = "6aff0fe7278b8418e606868b"
TAGS = pb.TAGS

POSTS_Q = ("query($input: PostsInput!) { posts(first: 100, input: $input) { edges { node { "
           + pb.POST_FIELDS + " } } } }")
EDIT = """mutation($input: EditPostInput!) { editPost(input: $input) {
  __typename ... on PostActionSuccess { post { id status } } ... on MutationError { message } } }"""


class Browser:
    """Opens Chrome only if a check needs a real page, and closes it at the end."""
    def __init__(self):
        self.pw = self.b = self.page = None

    def get(self):
        if not self.page:
            from playwright.sync_api import sync_playwright
            import snap
            self.pw = sync_playwright().start()
            self.b = self.pw.chromium.launch(channel="chrome", headless=True)
            self.page = self.b.new_context(viewport={"width": 1200, "height": 1200}, locale="en-US",
                                           timezone_id="America/New_York", user_agent=snap.BROWSER_UA).new_page()
        return self.page

    def close(self):
        if self.b:
            self.b.close()
            self.pw.stop()


def jget(url, ua=None):
    return json.loads(collect.fetch(url, ua=ua or collect.APP_UA))


def num(x):
    return collect.price_num(x)


# --------------------------------------------------------------------------- live checks: (state, live_price, detail)
# state: "live" (price is live_price), "out_of_stock", "ended", "unknown"

def check_steam(d):
    app = re.search(r"/app/(\d+)", d["url"]).group(1)
    info = jget(f"https://store.steampowered.com/api/appdetails?appids={app}&cc=us&filters=price_overview")
    po = ((info.get(app) or {}).get("data") or {}).get("price_overview")
    if not po:
        return "unknown", None, "Steam gave no price"
    return "live", po["final"] / 100, ""


def check_gog(d):
    gid = (d.get("verify") or {}).get("gog_id")
    if not gid:  # older drafts: find the product by its store link
        slug = d["url"].rstrip("/").split("/game/")[-1]
        name = d.get("item") or slug.replace("_", " ")
        res = jget("https://catalog.gog.com/v1/catalog?limit=10&countryCode=US&currencyCode=USD&query=like:"
                   + urllib.parse.quote(re.sub(r"[™®©]", "", name)))
        for p in res.get("products", []):
            if p.get("slug") == slug:
                amount = ((p.get("price") or {}).get("finalMoney") or {}).get("amount")
                return ("live", float(amount), "") if amount else ("unknown", None, "GOG gave no price")
        return "unknown", None, "couldn't find the game on GOG"
    data = jget(f"https://api.gog.com/products/{gid}/prices?countryCode=US&currency=USD")
    for p in (data.get("_embedded") or {}).get("prices", []):
        m = re.match(r"(\d+)", str(p.get("finalPrice", "")))
        if m and "USD" in str(p.get("finalPrice")):
            return "live", int(m.group(1)) / 100, ""
    return "unknown", None, "GOG gave no price"


def check_cheapshark(d):
    did = (d.get("verify") or {}).get("cheapshark_deal")
    if not did:
        return "unknown", None, "no deal ID"
    data = jget("https://www.cheapshark.com/api/1.0/deals?id=" + urllib.parse.quote(did))
    sp = (data.get("gameInfo") or {}).get("salePrice")
    return ("live", float(sp), "") if sp else ("ended", None, "the store no longer lists this deal")


def check_epic_free(d):
    import specials
    live = {g["url"].rstrip("/").split("/p/")[-1] for g in specials.free_epic()}
    slug = d["url"].rstrip("/").split("/p/")[-1]
    return ("live", 0.0, "") if slug in live else ("ended", None, "no longer free on Epic")


def check_psn(d):
    pid = d["url"].rstrip("/").split("/product/")[-1]
    for offset in range(0, 200, 50):
        var = json.dumps({"id": "3f772501-f6f8-49b7-abac-874a88ca4897", "pageArgs": {"size": 50, "offset": offset},
                          "sortBy": {"name": "sales30", "isAscending": False}, "filterBy": [], "facetOptions": []})
        ext = json.dumps({"persistedQuery": {"version": 1, "sha256Hash": collect.PS_GRID_HASH}})
        url = ("https://web.np.playstation.com/api/graphql/v1/op?operationName=categoryGridRetrieve&variables="
               + urllib.parse.quote(var) + "&extensions=" + urllib.parse.quote(ext))
        req = urllib.request.Request(url, headers={"User-Agent": collect.BROWSER_UA, "x-psn-store-locale-override": "en-US",
                                                   "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            grid = json.loads(r.read())["data"]["categoryGridRetrieve"]
        for p in grid.get("products") or []:
            if p.get("id") == pid:
                pr = p.get("price") or {}
                return "live", num(pr.get("discountedPrice")), ""
    return "unknown", None, "not in the PS Store's top 200 any more, so it couldn't be checked"


def check_eshop(d):
    nsuid = (d.get("verify") or {}).get("eshop_nsuid")
    if not nsuid:
        return "unknown", None, "no eShop ID"
    body = json.dumps({"requests": [{"indexName": "store_game_en_us", "params": f"query=&filters=nsuid:{nsuid}&hitsPerPage=1"}]}).encode()
    req = urllib.request.Request("https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries", data=body, headers={
        "x-algolia-application-id": "U3B6GR4UA3", "x-algolia-api-key": "a29c6927638bfd8cee23993e51e721c9",
        "content-type": "application/json", "User-Agent": collect.APP_UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        hits = json.loads(r.read())["results"][0]["hits"]
    if not hits:
        return "unknown", None, "the eShop no longer lists it"
    return "live", float((hits[0].get("price") or {}).get("finalPrice")), ""


def check_shopify(d):
    u = urllib.parse.urlparse(d["url"])
    handle = u.path.rstrip("/").split("/products/")[-1]
    for host in (u.netloc, "www." + u.netloc):
        live = collect.shopify_live(host, handle)
        if live:
            return ("live", live[1], "") if live[0] else ("out_of_stock", None, "")
    return "unknown", None, "the shop's product data couldn't be read"


def check_camelmart(d):
    """Walmart blocks automated checks, so (by your decision) camelmart's tracked Walmart price counts as live,
    as long as camelmart scanned the item within the last day."""
    wid = (d.get("verify") or {}).get("camelmart") or re.search(r"/ip/(?:[^/]+/)?(\d+)", d["url"]).group(1)
    return check_camel("camelmart.com", wid)


def check_camel_amazon(d):
    """When Amazon blocks the check: camelcamelcamel's tracked Amazon price (scanned within the last day)."""
    asin = (d.get("verify") or {}).get("asin") or re.search(r"/dp/([A-Z0-9]{10})", d["url"]).group(1)
    return check_camel("camelcamelcamel.com", asin)


STOP = {"the", "and", "for", "with", "edition", "standard", "new", "from", "this", "that", "amazon", "deal", "game", "games"}
DEAL_ACCOUNTS = ["wario64.bsky.social", "cheapassgamer.com"]


def _tokens(t):
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) >= 3 and w not in STOP}


def _same_deal(item, price, text):
    """Same product (most of its name words) at exactly the same price."""
    item = re.sub(r"\((?:[^)]*\b(?:physical|digital|Game-Key Card|Steam key)\b[^)]*)\)", "", item or "", flags=re.I)  # our own label
    want = _tokens(item)
    if not want or price is None:
        return False
    overlap = len(want & _tokens(text)) / len(want)
    prices = [num(p) for p in re.findall(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)", text)]
    return overlap >= 0.6 and any(p is not None and abs(p - price) < 0.011 for p in prices)


SLICKDEALS_DAYS = 7


def check_slickdeals(d):
    """A Slickdeals Amazon deal for the same product at the same price, posted in the last 7 days and
    NOT marked expired (read from the deal's own page)."""
    price = num(d.get("price"))
    q = urllib.parse.quote_plus(" ".join(list(_tokens(d.get("item")))[:6]))
    body = collect.fetch(f"https://slickdeals.net/newsearch.php?q={q}&searcharea=deals&searchin=first&rss=1")
    for it in collect.parse_feed(body):
        fresh = it.get("date") and NOW - it["date"] < timedelta(days=SLICKDEALS_DAYS)
        text = it["title"] + " " + (it.get("summary") or "")
        if not (fresh and re.search(r"amazon", text, re.I) and _same_deal(d.get("item"), price, text)):
            continue
        try:
            page = collect.fetch(re.sub(r"\?.*$", "", it["link"]))
            page = page.decode("utf-8", "replace") if isinstance(page, bytes) else page
        except Exception:
            continue
        m = re.search(r'"isExpiredDeal"\s*:\s*"(Yes|No)"', page)
        if m and m.group(1) == "No":
            return "live", price, "per Slickdeals (active deal)"
    return "unknown", None, "no active Slickdeals deal at this price"


def check_deal_accounts(d):
    """Wario64 or CheapAssGamer posted the same product on Amazon at the same price in the last 24 hours."""
    price = num(d.get("price"))
    for actor in DEAL_ACCOUNTS:
        feed = jget("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?filter=posts_no_replies&limit=100&actor=" + actor)
        for f in feed.get("feed", []):
            rec = f["post"]["record"]
            when = collect.parse_date(rec.get("createdAt"))
            recent = when and NOW - when < timedelta(hours=24)
            if recent and re.search(r"amazon|amzn", rec.get("text", ""), re.I) \
                    and _same_deal(d.get("item"), price, rec.get("text", "")):
                return "live", price, f"per {actor.split('.')[0]}"
    return "unknown", None, "not posted by Wario64 or CheapAssGamer in the last 24 hours"


def amazon_backup(d):
    """Amazon blocked our check. Any ONE of these confirms the price: camelcamelcamel (scanned within a day),
    an active Slickdeals deal (last 7 days), or Wario64/CheapAssGamer on Bluesky (last 24 hours)."""
    notes = []
    for fn in (check_camel_amazon, check_slickdeals, check_deal_accounts):
        try:
            state, live, detail = fn(d)
        except Exception as e:
            state, live, detail = "unknown", None, f"{fn.__name__} failed ({type(e).__name__})"
        if state in ("live", "out_of_stock"):
            return state, live, detail
        notes.append(detail)
    return "unknown", None, "couldn't confirm: " + "; ".join(notes)


def check_camel(site, pid):
    page = collect.fetch(f"https://{site}/product/{pid}", ua=collect.APP_UA)
    if isinstance(page, bytes):
        page = page.decode("utf-8", "replace")
    scan = re.search(r"Last update scan\s*(?:</strong>)?\s*</t[dh]>\s*<td[^>]*>\s*([^<]+?)\s*<|Last update scan\s+(\d+ \w+) ago", page)
    age = (scan.group(1) or scan.group(2)) if scan else ""
    n = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)", age or "")
    if not n or (n.group(2) in ("day", "week", "month", "year")):
        return "unknown", None, f"{site}'s last check is too old ({age.strip() or 'unknown'})"
    m = re.search(r'class="\s*bgp\s*">\s*([^<]+?)\s*<', page)
    if not m:
        return "unknown", None, f"{site} shows no current price"
    if "$" not in m.group(1):
        return ("out_of_stock", None, "") if re.search(r"stock|unavailable", m.group(1), re.I) else ("unknown", None, m.group(1))
    return "live", num(m.group(1)), f"per {site}"


def check_page(d, page):
    """Amazon and other stores' product pages, in the real browser."""
    import snap
    if "amazon." in d["url"]:
        # never opens Amazon pages (Amazon doesn't allow automated access): backups only
        r = amazon_backup(d)
        snap.count_amazon_check("backup" if r[0] in ("live", "out_of_stock") else "unconfirmed")
        return r
    if not snap.open_page(page, d["url"]):
        return "unknown", None, "the store blocked the check"
    # most stores publish price and stock in the page's product data (schema.org)
    offers = page.evaluate("""() => {
      const out = [];
      for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
          const walk = o => { if (!o || typeof o !== 'object') return; if (Array.isArray(o)) return o.forEach(walk);
            if (o.offers) [].concat(o.offers).forEach(x => out.push({price: x.price || x.lowPrice, availability: x.availability || ''}));
            Object.values(o).forEach(v => typeof v === 'object' && walk(v)); };
          walk(JSON.parse(s.textContent));
        } catch (e) {}
      }
      return out;
    }""")
    offers = [o for o in offers if o.get("price") not in (None, "")]
    if not offers:
        return "unknown", None, "the store page has no readable price"
    o = offers[0]
    if re.search(r"OutOfStock|SoldOut|Discontinued", str(o.get("availability")), re.I):
        return "out_of_stock", None, ""
    return "live", float(o["price"]), ""


def check_deal(d, page_getter):
    host = urllib.parse.urlparse(d.get("url") or "").netloc.lower()
    store = (d.get("store") or "").lower()
    try:
        if "steampowered.com" in host:
            return check_steam(d)
        if "gog.com" in host:
            return check_gog(d)
        if "epicgames.com" in host:
            return check_epic_free(d)
        if "playstation.com" in host:
            return check_psn(d)
        if "nintendo.com" in host:
            return check_eshop(d)
        if "/products/" in (d.get("url") or "") and "amazon." not in host:
            return check_shopify(d)
        if store in ("fanatical", "gamesplanet"):
            return check_cheapshark(d)
        if "walmart.com" in host:
            return check_camelmart(d)
        return check_page(d, page_getter())
    except Exception as e:
        return "unknown", None, f"check failed ({type(e).__name__})"


def posted_price(d, text):
    """The price the post claims: the $ amount in the post's text (so a price you corrected by hand counts),
    else the deal's own price."""
    if d.get("note") == "free" or re.search(r"\bfree\b|giving away", text, re.I) and "$" not in text:
        return 0.0
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", text or "")
    if m and num(m.group(1)) is not None:
        return num(m.group(1))
    return num(d.get("price"))


def verdict(state, live, claimed, detail, store=""):
    """None if fine to post, else (tag, reason)."""
    if state == "unknown" and (store or "").lower() == "amazon":
        return None  # backups too old/quiet: you checked the price yourself when you made the Amazon link
    if state == "out_of_stock":
        return "Out of stock", "out of stock at the store right now"
    if state == "ended":
        return "Deal ended", detail or "the deal has ended"
    if state == "unknown" or live is None:
        return "Couldn't verify", detail or "couldn't confirm the price"
    if claimed is None:
        return None  # nothing priced to compare; it's live
    if live > claimed + 0.009:
        return "Price went up", f"now ${live:.2f}, the post says ${claimed:.2f}"
    if live < claimed - 0.009:
        if (claimed - live) / claimed < SMALL_DROP:
            return "UPDATE", live  # small drop: fix the price in the post and keep it scheduled
        return "Price dropped", f"now ${live:.2f}, {round((claimed - live) / claimed * 100)}% below the ${claimed:.2f} in the post: review before scheduling"
    return None


def main():
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    if not key:
        print("verify: no BUFFER_API_KEY")
        return
    data = pb.gql_posts(key, POSTS_Q, {"input": {"organizationId": ORG, "filter": {"status": ["scheduled"]}}})
    scheduled = {e["node"]["id"]: e["node"] for e in data["posts"]["edges"]}
    sent = pb.load(pb.SENT_PATH, {})
    drafts = {d["id"]: d for d in pb.load(pb.DRAFTS_PATH, [])}
    post_to_deal = {pid: did for did, v in sent.items() for pid in (v.get("posts") or {}).values()}

    # free-games posts: every game listed must still be free to keep
    special = pb.load(os.path.join(pb.DATA, "specials_posts.json"), {})
    attention = []
    for pid, info in special.items():
        node = scheduled.get(pid)
        when = collect.parse_date((node or {}).get("dueAt"))
        if not node or not when or not (NOW - timedelta(minutes=2) <= when <= NOW + WINDOW):
            continue
        import specials
        try:
            still = {g["title"] for fn in (specials.free_epic, specials.free_steam, specials.free_gog) for g in fn()}
        except Exception as e:
            still = None
            print("verify: free games check failed", e)
        gone = [g["title"] for g in info["games"] if still is None or g["title"] not in still]
        if not gone:
            print("verify: OK  free games post")
            continue
        tag = "Couldn't verify" if still is None else "Deal ended"
        reason = "couldn't check the free games" if still is None else "no longer free: " + ", ".join(gone)
        tag_ids = list(dict.fromkeys([t["id"] for t in node.get("tags") or []] + [TAGS[tag]]))
        pb.gql(key, EDIT, {"input": {"id": pid, "saveToDraft": True, "tagIds": tag_ids}})
        attention.append({"at": NOW.isoformat(timespec="seconds"), "deal": pid, "tag": tag, "reason": reason,
                          "item": "Free games post", "store": node.get("channelService", "")})

    due = {}
    for pid, node in scheduled.items():
        when = collect.parse_date(node.get("dueAt"))
        did = post_to_deal.get(pid)
        if did and when and NOW - timedelta(minutes=2) <= when <= NOW + WINDOW:
            due.setdefault(did, []).append(pid)
    # no affiliate post (Amazon for now) goes out without the ad disclosure: add it to any post due soon that lacks it
    for pid, node in scheduled.items():
        when = collect.parse_date(node.get("dueAt"))
        text = node.get("text") or ""
        store = (drafts.get(post_to_deal.get(pid)) or {}).get("store", "")
        if pb.needs_disclosure(pb.full_text(node), store) and when and when <= NOW + timedelta(hours=12) and pb.disclosure_missing(node):
            try:
                pb.add_disclosure(key, EDIT, node)
                print("verify: added the missing ad disclosure to", pid)
            except Exception as e:
                # can't add it: pull the post back to Drafts rather than let it go out without one
                print("   couldn't add the disclosure to", pid, str(e)[:120], "- moving it to Drafts")
                try:
                    pb.gql(key, EDIT, {"input": {"id": pid, "saveToDraft": True}})
                except Exception:
                    pass
    # reply link cards (Threads/Bluesky): set once per post, shortly before it goes out
    carded = set(pb.load(CARDED_PATH, []))
    for pid, node in scheduled.items():
        when = collect.parse_date(node.get("dueAt"))
        if pid not in carded and when and when <= NOW + WINDOW and pid in post_to_deal:
            try:
                if pb.set_reply_cards(key, EDIT, node):
                    print("verify: reply link card set to your link:", pid)
                carded.add(pid)
            except Exception as e:
                print("   couldn't set the reply link card", pid, str(e)[:120])
    pb.save(CARDED_PATH, sorted(carded)[-500:])
    # an Amazon post must never go out without your amzn.to link (the long link has no tracking ID)
    for pid, node in scheduled.items():
        when = collect.parse_date(node.get("dueAt"))
        text = pb.full_text(node)
        if when and when <= NOW + WINDOW and re.search(r"https?://(?:www\.)?amazon\.com/", text) and "amzn.to/" not in text:
            tag_ids = list(dict.fromkeys([t["id"] for t in node.get("tags") or []] + [TAGS["Swap Amazon link"]]))
            try:
                pb.gql(key, EDIT, {"input": {"id": pid, "saveToDraft": True, "tagIds": tag_ids}})
                print("verify: HOLD Amazon post without the amzn.to link, moved back to Drafts:", pid)
            except Exception as e:
                print("   couldn't move", pid, str(e)[:150])
            for did, pids in list(due.items()):
                if pid in pids:
                    pids.remove(pid)
                    if not pids:
                        due.pop(did)
            attention.append({"at": NOW.isoformat(timespec="seconds"), "deal": pid, "tag": "Swap Amazon link",
                              "reason": "scheduled without the amzn.to link", "item": text[:60], "store": "Amazon"})
    if not due:
        print("verify: no deal posts due in the next", int(WINDOW.total_seconds() // 60), "minutes")

    browser = Browser()
    page_getter = browser.get

    try:
        for did, pids in due.items():
            d = drafts.get(did)
            if not d or d.get("kind") != "deal":
                continue  # news posts have no price to check
            node = scheduled[pids[0]]
            state, live, detail = check_deal(d, page_getter)
            v = verdict(state, live, posted_price(d, node.get("text", "")), detail, d.get("store", ""))
            name = d.get("item") or d["posts"]["x"][:60]
            if not v:
                print(f"verify: OK  {name} ({d.get('store')})" + (f" ${live:.2f}" if live else ""))
                continue
            if v[0] == "UPDATE":
                new = v[1]
                print(f"verify: UPDATE {name} ({d.get('store')}): price now ${new:.2f}")
                for pid in (sent.get(did, {}).get("posts") or {}).values():
                    if pid not in scheduled:
                        continue
                    text = scheduled[pid].get("text") or ""
                    claimed = posted_price(d, text)

                    def swap(m):
                        return f"${new:.2f}" if abs(num(m.group(1)) - claimed) < 0.009 else m.group(0)
                    fixed = re.sub(r"\$([\d,]+(?:\.\d+)?)", swap, text, count=1)
                    if fixed != text:  # the old % off no longer matches the new price
                        fixed = re.sub(r" \(\d{1,3}% Off\)", "", fixed)
                        try:
                            pb.edit_main_text(key, EDIT, scheduled[pid], fixed)
                        except Exception as e:
                            print("   couldn't update", pid, str(e)[:150])
                d["price"] = f"{new:.2f}"
                pb.save(pb.DRAFTS_PATH, list(drafts.values()))
                continue
            tag, reason = v
            if tag == "Out of stock":
                watch_add(d)
            print(f"verify: HOLD {name} ({d.get('store')}): {tag} - {reason}")
            pb.mark_held(did)
            # pull every still-scheduled post of this deal (all 3 apps) back to Drafts, tagged
            for pid in (sent.get(did, {}).get("posts") or {}).values():
                if pid not in scheduled:
                    continue
                tag_ids = list(dict.fromkeys([t["id"] for t in scheduled[pid].get("tags") or []] + [TAGS[tag]]))
                try:
                    r = pb.gql(key, EDIT, {"input": {"id": pid, "saveToDraft": True, "tagIds": tag_ids}})["editPost"]
                    if r.get("__typename") != "PostActionSuccess":
                        print("   couldn't move", pid, r.get("message"))
                except Exception as e:
                    print("   couldn't move", pid, str(e)[:150])
            attention.append({"at": NOW.isoformat(timespec="seconds"), "deal": did, "tag": tag, "reason": reason,
                              "item": name, "store": d.get("store", ""), "posts": sent.get(did, {}).get("posts"),
                              "check": pb.camel_link(d)})
        # deals that were out of stock when first found
        for d in drafts.values():
            if d.get("watch_restock"):
                watch_add(d)
                d.pop("watch_restock")
        check_watch(page_getter, drafts)
        pb.save(pb.DRAFTS_PATH, list(drafts.values()))
    finally:
        browser.close()
    if attention:
        pb.note_attention(attention)


# --------------------------------------------------------------------------- restock watch

WATCH_PATH = os.path.join(pb.DATA, "watch.json")
RESTOCK_AFTER = timedelta(hours=24)
WATCH_DAYS = 14
CHECKS_PER_RUN = 5


def watch_add(d):
    watch = pb.load(WATCH_PATH, {})
    key = d.get("base") or d["id"]
    if key not in watch and num(d.get("price")) is not None:
        watch[key] = {"since": NOW.isoformat(timespec="seconds"), "checked": None, "item": d.get("item") or "",
                      "store": d.get("store", ""), "url": d.get("url", ""), "price": d.get("price"),
                      "verify": d.get("verify") or {}, "image": d.get("image", ""), "deal": d["id"]}
        pb.save(WATCH_PATH, watch)
        print(f"watch: added {watch[key]['item']} ({watch[key]['store']}) - out of stock")


def restock_texts(item, store, price):
    digital = re.search(r"steam|gog|eshop|nintendo|psn|playstation|xbox|epic|fanatical|gamesplanet|amazon", store, re.I)
    prep = "on" if digital else "at"
    p = f"${price:.2f}"
    return {"x": f"{item} is back in stock {prep} {store} for {p}",
            "threads": f"{store} has {item} back in stock for {p}",
            "bluesky": f"Back in stock {prep} {store}: {item}, {p}"}


def check_watch(page_getter, drafts):
    """Out-of-stock deals: a new post once they've been out 24h+ and are back at the deal price or lower."""
    watch = pb.load(WATCH_PATH, {})
    cfg = pb.load(os.path.join(pb.ROOT, "config.json"), {})
    due = [k for k, w in watch.items() if NOW - datetime.fromisoformat(w["since"]) >= RESTOCK_AFTER]
    due.sort(key=lambda k: watch[k].get("checked") or "")
    for k in list(watch):
        if NOW - datetime.fromisoformat(watch[k]["since"]) > timedelta(days=WATCH_DAYS):
            print(f"watch: gave up on {watch[k]['item']} after {WATCH_DAYS} days")
            del watch[k]
    for k in due[:CHECKS_PER_RUN]:
        if k not in watch:
            continue
        w = watch[k]
        w["checked"] = NOW.isoformat(timespec="seconds")
        state, live, _ = check_deal(dict(w), page_getter)
        deal_price = num(w["price"])
        if state != "live" or live is None:
            print(f"watch: still out of stock / unknown: {w['item']}")
            continue
        if live > deal_price + 0.009:
            print(f"watch: back in stock but ${live:.2f} > deal ${deal_price:.2f}: {w['item']}")
            continue
        url, affiliate = collect.clean_link(w["url"], os.environ.get("AMAZON_TAG") or cfg.get("amazon_tag", ""))
        rid = collect.item_id(k, "restock", NOW.date().isoformat())
        drafts[rid] = {"id": rid, "base": rid, "kind": "deal", "source": "restock watch", "store": w["store"],
                       "found_at": NOW.isoformat(timespec="seconds"), "source_link": w["url"], "url": url or w["url"],
                       "link_type": "product", "image": w.get("image", ""), "multi": False, "code": "",
                       "posts": collect.make_posts(restock_texts(w["item"], w["store"], live), url or w["url"],
                                                   affiliate or cfg.get("tag_all_deal_posts", False), cfg),
                       "item": w["item"], "price": f"{live:.2f}", "verify": w.get("verify") or {}, "status": "new"}
        # newest first, so the next run's picture and Buffer steps pick it up
        items = [drafts[rid]] + [v for kk, v in drafts.items() if kk != rid]
        drafts.clear()
        drafts.update({v["id"]: v for v in items})
        print(f"watch: RESTOCKED {w['item']} ({w['store']}) at ${live:.2f} - new draft")
        del watch[k]
    pb.save(WATCH_PATH, watch)


def test_mode():
    """Dry run: check today's drafted deals live and print the verdicts. Changes nothing in Buffer."""
    sent = pb.load(pb.SENT_PATH, {})
    drafts = {d["id"]: d for d in pb.load(pb.DRAFTS_PATH, [])}
    ids = [k for k, v in sent.items() if "posts" in v and k in drafts
           and NOW - datetime.fromisoformat(v["at"]) < timedelta(hours=30)]
    browser = Browser()
    page_getter = browser.get
    for did in ids:
        d = drafts[did]
        st, live, detail = check_deal(d, page_getter)
        v = verdict(st, live, posted_price(d, d["posts"]["x"]), detail)
        print(f"TEST {d.get('store')}: {(d.get('item') or d['posts']['x'])[:60]} | {st} {live} | -> {v or 'OK to post'}")
    browser.close()


if __name__ == "__main__":
    if os.environ.get("VERIFY_TEST"):
        test_mode()
        sys.exit(0)
    try:
        main()
    except Exception as e:  # never break the pipeline
        print("verify error:", e)
