#!/usr/bin/env python3
"""Deal collector.

Reads deal and news feeds, filters and de-duplicates them, and writes
ready-to-review post drafts for X, Bluesky and Threads.

Standard library only, so it runs on a plain GitHub Actions runner.
"""
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "data")
SEEN_PATH = os.path.join(DATA_DIR, "seen.json")
DRAFTS_PATH = os.path.join(DATA_DIR, "drafts.json")
TIPS_PATH = os.path.join(DATA_DIR, "tips.json")
REPORT_PATH = os.path.join(DATA_DIR, "latest.md")
STATUS_PATH = os.path.join(DATA_DIR, "status.json")
PRICES_PATH = os.path.join(DATA_DIR, "shop_prices.json")

UA = "Mozilla/5.0 (compatible; PriceNotes/1.0; +https://github.com/" + os.environ.get("GITHUB_REPOSITORY", "jwest1985/price-notes") + ")"
NOW = datetime.now(timezone.utc)
LOCAL_TZ = ZoneInfo("America/Toronto")

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

# Query parameters that carry someone else's affiliate or tracking credit.
STRIP_PARAMS = {
    "tag", "ascsubtag", "linkcode", "linkid", "creative", "creativeasin", "camp", "ref", "ref_", "psc",
    "irclickid", "irgwc", "clickid", "subid", "sub_id", "utm_source", "utm_medium", "utm_campaign",
    "utm_content", "utm_term", "aff", "affiliate", "afsrc", "partner", "sd", "sdtid", "sdop", "u1",
    "cjevent", "cjdata", "mpid", "wmlspartner", "veh", "sourceid", "affiliates_ad_id", "campaign_id",
    "adid", "srsltid", "gclid", "fbclid", "loc", "acampid",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=isinstance(data, dict))
        f.write("\n")


BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
APP_UA = "PriceNotes/1.0 (+https://github.com/" + os.environ.get("GITHUB_REPOSITORY", "jwest1985/price-notes") + ")"


def fetch(url, timeout=25, ua=None):
    req = urllib.request.Request(url, headers={"User-Agent": ua or UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except ValueError:
        return None


def item_id(*parts):
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:16]


def price_num(price):
    """'$29.99' / '29.99' / 'Free' -> 29.99 / 0.0; None when unreadable."""
    if price is None:
        return None
    if str(price).strip().lower() == "free":
        return 0.0
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(price))
    return float(m.group(0).replace(",", "")) if m else None


def norm_key(title):
    """A loose key so the same deal from two feeds is only posted once."""
    t = (title or "").lower()
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)
    t = re.sub(r"[^a-z0-9$ ]+", " ", t)
    words = [w for w in t.split() if w not in {"the", "a", "an", "at", "on", "for", "is", "and", "with", "w"}]
    return " ".join(words[:8])


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def parse_feed(raw):
    """Return a list of dicts {title, link, summary, html, date} for RSS or Atom."""
    root = ET.fromstring(raw)
    items = []
    if root.tag == ATOM + "feed":
        for e in root.findall(ATOM + "entry"):
            link = ""
            for l in e.findall(ATOM + "link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
            content = e.findtext(ATOM + "content") or e.findtext(ATOM + "summary") or ""
            items.append({
                "title": html.unescape(e.findtext(ATOM + "title") or "").strip(),
                "link": link,
                "html": content,
                "summary": strip_tags(content),
                "date": parse_date(e.findtext(ATOM + "updated") or e.findtext(ATOM + "published")),
            })
    else:
        channel = root.find("channel") if root.find("channel") is not None else root
        for it in channel.findall("item"):
            content = it.findtext(CONTENT + "encoded") or it.findtext("description") or ""
            items.append({
                "title": html.unescape(it.findtext("title") or "").strip(),
                "link": (it.findtext("link") or "").strip(),
                "html": content,
                "summary": strip_tags(it.findtext("description") or content),
                "date": parse_date(it.findtext("pubDate")),
            })
    return items


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def unwrap(url):
    """Pull the real store URL out of known redirect wrappers."""
    for _ in range(3):
        p = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(p.query)
        inner = None
        for key in ("u2", "u", "url", "murl", "dest", "destination", "RD_PARM1"):
            if key in q and q[key][0].startswith("http"):
                inner = q[key][0]
                break
        if inner and inner != url:
            url = inner
        else:
            break
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def resolve(url, timeout=15):
    """Follow redirects to the final store URL (best effort). Stops at an Amazon link without requesting it:
    Amazon pages are never fetched."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(8):
        if is_amazon(urllib.parse.urlparse(url).netloc.lower()):
            return url
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
            with opener.open(req, timeout=timeout) as resp:
                return resp.geturl()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
            if not loc:
                return url
            url = urllib.parse.urljoin(url, loc)
        except Exception:
            return url
    return url


def is_amazon(host):
    return bool(re.search(r"(^|\.)amazon\.com$", host)) or host in {"amzn.to", "a.co", "amzn.com"}


def clean_link(url, amazon_tag):
    """Remove other people's affiliate/tracking parameters (Amazon links stay untagged until the amzn.to swap).

    Returns (url, is_affiliate)."""
    if not url:
        return url, False
    url = unwrap(url)
    pu = urllib.parse.urlparse(url)
    host = pu.netloc.lower()
    if (host in {"amzn.to", "a.co", "bit.ly", "buff.ly", "tinyurl.com"} or "click" in host or "redirect" in host
            or ("slickdeals.net" in host and "/click" in pu.path)):
        url = unwrap(resolve(url))
    # still stuck on a Slickdeals page? we can't credit the real store, so drop the link
    if "slickdeals.net" in urllib.parse.urlparse(url).netloc.lower():
        return "", False
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    if is_amazon(host):
        m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", p.path)
        if m:
            clean = f"https://www.amazon.com/dp/{m.group(1)}"
        else:
            q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k.lower() not in STRIP_PARAMS]
            clean = urllib.parse.urlunparse((p.scheme or "https", p.netloc, p.path, "", urllib.parse.urlencode(q), ""))
        # no tracking ID in drafts: your own clicks (to make the amzn.to link) don't count in your Associates stats.
        # It's still an affiliate post (#ad), because the amzn.to link you swap in carries your tag.
        return clean, True
    strip = STRIP_PARAMS | ({"id", "skuid", "intl"} if host.endswith("bestbuy.com") else set())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k.lower() not in strip]
    clean = urllib.parse.urlunparse((p.scheme, p.netloc, p.path, "", urllib.parse.urlencode(q), ""))
    return clean, False


def store_from_host(host):
    host = host.lower().replace("www.", "")
    names = {
        "amazon.com": "Amazon", "bestbuy.com": "Best Buy", "walmart.com": "Walmart", "target.com": "Target",
        "gamestop.com": "GameStop", "newegg.com": "Newegg", "woot.com": "Woot", "store.steampowered.com": "Steam",
        "gog.com": "GOG", "fanatical.com": "Fanatical", "humblebundle.com": "Humble", "greenmangaming.com": "Green Man Gaming",
        "store.playstation.com": "PlayStation Store", "direct.playstation.com": "PS Direct", "nintendo.com": "Nintendo",
        "xbox.com": "Xbox", "microsoft.com": "Microsoft Store", "videogamesplus.ca": "VGP", "barnesandnoble.com": "B&N",
        "ebay.com": "eBay", "costco.com": "Costco", "dell.com": "Dell", "lenovo.com": "Lenovo", "store.epicgames.com": "Epic Games Store",
        "gruv.com": "GRUV", "us.gamesplanet.com": "Gamesplanet", "gamebillet.com": "Gamebillet",
    }
    for k, v in names.items():
        if host == k or host.endswith("." + k):
            return v
    return host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


SHORTLINK_OK = [True]


def follow_locations(url, max_hops=5, stop_hosts=("bestbuy.com", "walmart.com", "target.com", "gamestop.com", "amazon.com")):
    """Walk redirect Location headers one hop at a time, stopping once we reach a store."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(max_hops):
        host = urllib.parse.urlparse(url).netloc.lower()
        if any(host == h or host.endswith("." + h) for h in stop_hosts):
            return url
        try:
            opener.open(urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD"), timeout=6)
            return url
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urllib.parse.urljoin(url, loc)
                continue
            return url
        except Exception:
            SHORTLINK_OK[0] = False
            return url
    return url


# ---------------------------------------------------------------------------
# Turning feed items into deals
# ---------------------------------------------------------------------------

PRICE_AT = re.compile(r"^(?P<item>.+?)\s*[-–:]?\s*\$(?P<price>[\d,]+(?:\.\d{1,2})?)\s*(?:\+[^@]*?)?\s*(?:at|@|from|via)\s+(?P<store>[A-Za-z0-9&'.\- ]{2,40})", re.I)
PRICE_FIRST = re.compile(r"^\$(?P<price>[\d,]+(?:\.\d{1,2})?)\*?\s*[:\-–|]\s*(?P<item>.+?)(?:\s+(?:at|@)\s+(?P<store>[A-Za-z0-9&'.\- ]{2,40}))?$", re.I)
BRACKET = re.compile(r"^\[(?P<store>[^\]]+)\]\s*(?P<item>.+?)(?:\s*\((?P<paren>[^)]*)\))?\s*$")


def find_outbound(html_text):
    """Find the first outbound (store) link in an item's HTML."""
    for href in re.findall(r'href="([^"]+)"', html_text or ""):
        href = html.unescape(href)
        host = urllib.parse.urlparse(href).netloc.lower()
        if not host:
            continue
        if any(h in host for h in ("reddit.com", "redd.it", "slickdeals.net")) and "click" not in href and "u2=" not in href:
            continue
        return href
    return None


STORE_SEARCH = {
    "best-buy": ("Best Buy", "https://www.bestbuy.com/site/searchpage.jsp?st={q}"),
    "walmart": ("Walmart", "https://www.walmart.com/search?q={q}"),
    "target": ("Target", "https://www.target.com/s?searchTerm={q}"),
    "gamestop": ("GameStop", "https://www.gamestop.com/search/?q={q}"),
    "newegg": ("Newegg", "https://www.newegg.com/p/pl?d={q}"),
}


def search_query(item):
    q = re.sub(r"^(?:[\w' ]+ members|prime members|in-store)\s*:\s*", "", item, flags=re.I)
    q = re.sub(r"\b\d+[- ]?(?:piece|pc|pcs)\b", "", q, flags=re.I)
    q = re.sub(r"[^\w\s:'&-]", " ", q)
    return urllib.parse.quote_plus(re.sub(r"\s+", " ", q).strip()[:80])


# codes that belong to a deal site or creator (they earn that partner money): never posted
CREATOR_CODE_HINTS = r"slick|^sd\d|cag|wario|dealnews|honey|rakuten|retailmenot|twitch|ttv|yt\d|^ig|creator"


def deal_from_slickdeals(it):
    title = it["title"]
    item, price, store = title, None, None
    note_extra = ""
    m = PRICE_AT.match(title)
    if m:
        item, price, store = m.group("item"), m.group("price"), m.group("store")
    else:
        m = PRICE_FIRST.match(title)
        if m:
            item, price, store = m.group("item"), m.group("price"), m.group("store")
        else:
            pm = re.search(r"\$\s?([\d,]+(?:\.\d{1,2})?)", title)
            if pm:
                price = pm.group(1)
                item = title[: pm.start()]
                tail = title[pm.end():].strip(" ,;&")
                if re.search(r"\$\s?\d", tail) and len(tail) < 60:
                    note_extra = re.sub(r"\s*\+\s*free (?:s&h|shipping).*$", "", tail, flags=re.I)
    # Slickdeals puts the store name in the "trd" parameter of its click link,
    # and the Amazon product ID (ASIN) in a data attribute we can build our own link from
    url = find_outbound(it["html"]) or it["link"]
    asin = re.search(r'data-aps-asin="([A-Z0-9]{10})"', it["html"] or "")
    exit_site = re.search(r'data-product-exitWebsite="([^"]+)"', it["html"] or "")
    if asin:
        url = f"https://www.amazon.com/dp/{asin.group(1)}"
        store = "Amazon"
    trd = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("trd", [""])[0]
    if not asin and trd and not trd.lower().startswith("http") and len(trd) < 30:
        store = trd
    if not asin and not store and exit_site:
        store = store_from_host(exit_site.group(1))
    slug = re.search(r'data-store-slug="([^"]+)"', it["html"] or "")
    search = False
    if not asin and slug and slug.group(1) in STORE_SEARCH:
        store, pattern = STORE_SEARCH[slug.group(1)]
        url = pattern.format(q=search_query(re.sub(r"\s*\+\s*free s&h.*$", "", item, flags=re.I)))
        search = True
    if store:
        store = re.sub(r"\s+(?:w|with|via|using)$", "", store.strip(), flags=re.I)
    if store and store.lower() in {"slickdeals.net", "slickdeals"}:
        store = None
    item = re.sub(r"\s*\+\s*free s&h.*$", "", item, flags=re.I)
    item = re.sub(r"\s*(?:\+|w/).*$", "", item) if len(item) > 90 else item
    img = re.search(r'<img[^>]+src="(https?://[^"]+)"', it["html"] or "")
    multi = bool(re.search(r"&\s*more\b|\bsale\b|\bup to\b|\bselect\b|\bvarious\b|\bfrom \$", it["title"], re.I))
    if price:
        price = price.rstrip(",.")
    # third-party marketplace sellers: we only post first-party prices
    blob = re.sub(r"<[^>]+>", " ", (it["title"] or "") + " " + (it.get("summary") or "") + " " + (it["html"] or ""))
    sold = re.search(r"\bsold by\s+([A-Z0-9][\w&.' -]{1,40})", blob, re.I)
    first_party = r"amazon|walmart|best ?buy|target|gamestop|newegg|costco|the manufacturer|" + re.escape((store or "zzz").lower())
    third_party = bool(re.search(r"third[- ]party|3rd[- ]party|marketplace seller", blob, re.I)) or bool(
        sold and not re.match(first_party, sold.group(1).strip().lower()))
    # promo code (posted only after it's found on the store's own page) and shipping note
    code = ""
    cm = re.search(r"(?:w/|with|use|using|apply)?\s*(?:promo |coupon |discount )?code[:\s]+([A-Z0-9][A-Z0-9-]{3,24})\b", it["title"], re.I) or \
        re.search(r"(?:promo|coupon|discount) code[:\s]+([A-Z0-9][A-Z0-9-]{3,24})\b", blob, re.I)
    if cm and not re.fullmatch(r"\d+", cm.group(1)):
        code = cm.group(1).upper()
    creator_code = bool(code) and (bool(re.search(CREATOR_CODE_HINTS, code, re.I)) or
                                   bool(re.search(r"exclusive (?:promo |coupon )?code|creator code|influencer code|code for (?:slickdeals|sd) members", blob, re.I)))
    tl = it["title"].lower()
    ship = "Prime" if re.search(r"w/\s*prime|prime members?|prime eligible|for prime", tl) else (
        "Free Shipping" if re.search(r"free (?:s&h|shipping)", tl) else "")
    item = re.sub(r"\s*(?:\+\s*)?(?:w/|with|use|using)?\s*(?:promo |coupon )?code[:\s]+[A-Z0-9-]+.*$", "", item, flags=re.I)
    item = re.sub(r"^(?:prime members?|[\w' ]{0,20} members?|in-store)\s*:\s*", "", item.strip(), flags=re.I)
    price = price.strip().rstrip(",.") if price else price  # "$8, on Amazon" -> 8
    item = re.sub(r"\s+(?:from|for|at|only|just|starting at|as low as|now)$", "", item.strip(" -–:"), flags=re.I)
    return {"item": item.strip(" -–:"), "price": price, "store": (store or "").strip(), "note": note_extra, "url": url,
            "search_link": search, "image": img.group(1) if img else "", "multi": multi, "third_party": third_party,
            "code": code, "creator_code": creator_code, "ship": ship}


def deal_from_reddit(it):
    title = it["title"]
    m = BRACKET.match(title)
    url = find_outbound(it["html"]) or it["link"]
    if not m:
        return {"item": title, "price": None, "store": "", "note": "", "url": url}
    paren = m.group("paren") or ""
    pm = re.search(r"\$\s?([\d,]+(?:\.\d{1,2})?)", paren)
    off = re.search(r"(\d{1,3})\s*%\s*off", paren, re.I)
    free = re.search(r"\bfree\b|100\s*%\s*off", paren + " " + title, re.I)
    note = ""
    if free:
        note = "free"
    elif off:
        note = f"{off.group(1)}% off"
    return {"item": m.group("item").strip(), "price": pm.group(1) if pm else None, "store": m.group("store").strip(),
            "note": note, "url": url}


def keyword_hit(text, words):
    """Whole-word match (plurals allowed), so "toy" doesn't match "Toyota" or "tv" "activity"."""
    t = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?:s|es)?(?![a-z0-9])", t) for w in words)


def items_from_cheapshark(feed):
    data = json.loads(fetch(feed["url"], ua=APP_UA))
    out = []
    for d in data:
        app = d.get("steamAppID")
        if not app:
            continue
        rating = int(d.get("steamRatingPercent") or 0)
        count = int(d.get("steamRatingCount") or 0)
        meta = int(d.get("metacriticScore") or 0)
        savings = float(d.get("savings") or 0)
        if savings < feed.get("min_savings", 50):
            continue
        popular = count >= feed.get("min_rating_count", 3000)
        if not ((meta >= feed.get("min_metacritic", 80) and popular) or (rating >= feed.get("min_steam_rating", 90) and popular)):
            continue
        changed = datetime.fromtimestamp(int(d.get("lastChange") or 0), tz=timezone.utc)
        out.append({
            "title": d.get("title", ""),
            "link": f"https://store.steampowered.com/app/{app}/",
            "html": "", "summary": "", "date": changed,
            "deal": {"item": d.get("title", ""), "price": d.get("salePrice"), "store": "Steam",
                     "note": f"{round(float(d.get('savings') or 0))}% off",
                     "url": f"https://store.steampowered.com/app/{app}/",
                     "image": f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{app}/header.jpg"},
            "uid": d.get("dealID"),
        })
    return out


def _page_title(url):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US"})
    with urllib.request.urlopen(req, timeout=20) as r:
        t = r.read(200000).decode("utf-8", "replace")
    return t, html.unescape((re.search(r"<title>(.*?)</title>", t, re.S) or [None, ""])[1]).strip()


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def keystore_url(store, title):
    """Find the game's own page at the key store and check it is the right game (None if we can't be sure)."""
    try:
        if store == "Fanatical":
            slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"[™®©':]", "", title.lower())).strip("-")
            url = f"https://www.fanatical.com/en/game/{slug}"
            _, page_title = _page_title(url)
            return url if _norm(page_title).startswith(_norm(title)) else None
        if store == "Gamesplanet":
            page, _ = _page_title("https://us.gamesplanet.com/search?query=" + urllib.parse.quote_plus(title))
            for href in re.findall(r'href="(/game/[^"#?]+)"', page):
                if _norm(href.split("/game/")[1]).startswith(_norm(title)):
                    return "https://us.gamesplanet.com" + href
    except Exception:
        return None
    return None


def items_from_keystores(feed):
    """Official PC key stores (Fanatical, Gamesplanet) via CheapShark: big discounts on well-reviewed games only."""
    out = []
    for store_id, store in feed["stores"].items():
        url = (f"https://www.cheapshark.com/api/1.0/deals?storeID={store_id}&sortBy=Deal%20Rating&pageSize=60"
               f"&upperPrice={feed.get('upper_price', 60)}")
        data = json.loads(fetch(url, ua=APP_UA))
        picked = 0
        for d in data:
            if picked >= feed.get("max_per_store", 2):
                break
            count = int(d.get("steamRatingCount") or 0)
            if float(d.get("savings") or 0) < feed.get("min_savings", 50) or count < feed.get("min_rating_count", 3000):
                continue
            if not (int(d.get("metacriticScore") or 0) >= feed.get("min_metacritic", 80)
                    or int(d.get("steamRatingPercent") or 0) >= feed.get("min_steam_rating", 90)):
                continue
            uid = f"{store}:{d.get('gameID')}@{d.get('salePrice')}"
            if uid in feed.get("_seen_uids", ()):
                continue
            link = keystore_url(store, d.get("title", ""))
            if not link:
                continue  # can't confirm the store's own page: skip rather than post a wrong link
            app = d.get("steamAppID")
            image = f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{app}/capsule_616x353.jpg" if app else ""
            out.append({"title": d.get("title", ""), "link": link, "html": "", "summary": "", "date": NOW, "uid": uid,
                        "deal": {"item": f"{d.get('title', '')} (Steam)", "price": d.get("salePrice"), "store": store,
                                 "note": f"{round(float(d.get('savings') or 0))}% off", "url": link, "image": image,
                                 "verify": {"cheapshark_deal": d.get("dealID")}}})
            picked += 1
            time.sleep(1)
    return out


PLATFORMS = [(r"playstation 5|\bps5\b", "PS5"), (r"playstation 4|\bps4\b", "PS4"), (r"nintendo switch 2|switch 2", "Switch 2"),
             (r"nintendo switch|\bswitch\b", "Switch"), (r"xbox series x\s*\|\s*s|xbox series x/s", "Xbox Series X|S"), (r"xbox series x|xbox series", "Xbox Series X"),
             (r"xbox one", "Xbox One")]


def clean_item(item, store):
    """Tidy a product name for a post: no "[Prime]"-style labels, no raw Amazon keyword titles, no ALL CAPS."""
    item = re.sub(r"^\s*(?:\[[^\]]{1,20}\]\s*)+", "", item).strip()
    item = re.sub(r"\s*[\u2122\u00ae\u00a9]", "", item).strip()  # no TM/(R)/(C) marks in posts
    if store == "Amazon" and (" | " in item or len(item) > 90 or
                              re.search(r" - (?:PlayStation|Nintendo|Xbox|PS[45])|\[Digital (?:Code|Download)\]", item, re.I)):
        item = shorten_title(item)
    letters = re.sub(r"[^A-Za-z]", "", item)
    if len(letters) > 3 and letters.isupper():
        item = " ".join(w.capitalize() if w.isalpha() and len(w) > 3 else w for w in item.split())
    return item


def shorten_title(t, limit=90):
    """Amazon titles are long and keyword-stuffed: keep the official name (first part), plus the platform for games."""
    t = re.sub(r"\s+", " ", html.unescape(t or "")).strip()
    if re.search(r"\b(?:4K|UHD|Blu-?ray|DVD|Steelbook)\b", t, re.I):
        # movies/TV: the season, edition and format are part of what's being sold, so keep the whole title
        return t if len(t) <= limit + 30 else t[:limit + 30].rsplit(" ", 1)[0].rstrip(",-")
    parts = re.split(r" [-\u2013\u2014|] |, | \((?=[^)]*(?:pack|count|edition|version|bundle)\b)", t, maxsplit=1)
    name = parts[0].strip(" -,")
    if len(name.split()) < 2 and len(parts) > 1 and not any(re.search(pat, t[len(name):], re.I) for pat, _ in PLATFORMS):
        # too short to be useful (and not just "Game - Platform"): keep a bit more
        name = t
    rest = t[len(name):]
    digital = bool(re.search(r"digital (?:code|download)|\[digital", rest, re.I))
    if not re.search(r"\((?:PS5|PS4|Switch 2|Switch|Xbox[^)]*)\)", name):
        for pat, label in PLATFORMS:
            if re.search(pat, rest, re.I) and not re.search(pat, name, re.I):
                name += f" ({label}, digital code)" if digital else f" ({label})"
                digital = False
                break
    if digital:
        name += " (digital code)"
    if len(name) > limit:
        name = name[:limit].rsplit(" ", 1)[0].rstrip(",-") 
    return name


def items_from_camel(feed):
    """camelcamelcamel RSS (popular deals / daily price drops), Amazon as the seller. Prices are re-checked on Amazon."""
    body = fetch(feed["url"], ua=APP_UA)
    root = ET.fromstring(body.encode("utf-8") if isinstance(body, str) else body)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        walmart = "camelmart.com" in link
        m = re.search(r"/product/(\d+)" if walmart else r"/product/([A-Z0-9]{10})", link)
        if not m:
            continue
        asin = m.group(1)  # Amazon ASIN, or Walmart's item ID on camelmart
        drop = re.search(r" - down [\d.]+% \(\$[\d,.]+\) to \$([\d,.]+) from \$([\d,.]+)$", title)
        if drop:
            price, name = drop.group(1), title[:drop.start()]
        else:
            pm = re.search(r"Current Price:\s*\$([\d,.]+)", desc)
            if not pm:
                continue
            price, name = pm.group(1), title
        image = ""
        if walmart or not name.strip() or "..." in name:
            try:  # full name (the drop feed cuts names off) and, for Walmart, the product photo
                site = "camelmart.com" if walmart else "camelcamelcamel.com"
                page = fetch(f"https://{site}/product/{asin}", ua=APP_UA)
                page = page.decode("utf-8", "replace") if isinstance(page, bytes) else page
                og = re.search(r'<meta property="og:title" content="([^"]+)"', page)
                img = re.search(r'<meta property="og:image" content="([^"]+)"', page)
                name = html.unescape(og.group(1)) if og else ("" if "..." in name else name)
                if walmart and img:
                    image = re.sub(r"\?.*$", "", html.unescape(img.group(1)))  # Walmart's own full-size photo
            except Exception:
                name = "" if "..." in name else name
            if not name:
                continue
        name = shorten_title(name)
        if len(re.sub(r"[^A-Za-z0-9]", "", name)) < 3:
            continue  # no usable product name: never post a nameless deal
        if walmart:
            name = re.sub(r",\s*\d{10,14}$", "", name)  # trailing UPC codes in Walmart titles
            url = f"https://www.walmart.com/ip/{asin}"
            deal = {"item": name, "price": price.replace(",", ""), "store": "Walmart", "note": "", "url": url,
                    "image": image, "verify": {"camelmart": asin}}
        else:
            url = f"https://www.amazon.com/dp/{asin}"
            deal = {"item": name, "price": price.replace(",", ""), "store": "Amazon", "note": "", "url": url,
                    "verify": {"asin": asin}}
        out.append({"title": name, "link": url, "html": "", "summary": "", "date": parse_date(it.findtext("pubDate")),
                    "uid": f"{asin}@{price}", "deal": deal})
    return out


def items_from_epic(feed):
    data = json.loads(fetch(feed["url"]))
    out = []
    for g in data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", []):
        promos = (g.get("promotions") or {}).get("promotionalOffers") or []
        offers = [o for p in promos for o in p.get("promotionalOffers", [])]
        free_now = [o for o in offers if (o.get("discountSetting") or {}).get("discountPercentage") == 0]
        if not free_now:
            continue
        slug = g.get("productSlug") or ((g.get("catalogNs") or {}).get("mappings") or [{}])[0].get("pageSlug") or ""
        if not slug:
            continue
        end = parse_date(free_now[0].get("endDate"))
        url = f"https://store.epicgames.com/p/{slug}"
        imgs = {i.get("type"): i.get("url") for i in g.get("keyImages") or []}
        image = imgs.get("OfferImageWide") or imgs.get("DieselStoreFrontWide") or imgs.get("Thumbnail") or ""
        until = f" until {end.strftime('%b %-d')}" if end else ""
        out.append({
            "title": g.get("title", ""), "link": url, "html": "", "summary": "",
            "date": parse_date(free_now[0].get("startDate")),
            "deal": {"item": g.get("title", ""), "price": None, "store": "Epic Games Store", "note": "free", "url": url,
                     "suffix": until, "image": image},
            "uid": g.get("id"),
        })
    return out


def items_from_gog(feed):
    data = json.loads(fetch(feed["url"], ua=APP_UA))
    out = []
    for p in data.get("products", []):
        price = p.get("price") or {}
        try:
            disc = abs(int(str(price.get("discount", "0")).strip("-%") or 0))
        except ValueError:
            disc = 0
        if disc < feed.get("min_savings", 50):
            continue
        if (p.get("reviewsRating") or 0) < feed.get("min_rating", 42) or (p.get("reviewsCount") or 0) < feed.get("min_reviews", 500):
            continue
        amount = (price.get("finalMoney") or {}).get("amount")
        url = p.get("storeLink") or f"https://www.gog.com/en/game/{p.get('slug')}"
        image = p.get("coverHorizontal") or ""
        if image.startswith("//"):
            image = "https:" + image
        out.append({
            "title": p.get("title", ""), "link": url, "html": "", "summary": "", "date": NOW,
            "deal": {"item": p.get("title", ""), "price": amount, "store": "GOG", "note": f"{disc}% off", "url": url, "image": image,
                     "verify": {"gog_id": p.get("id")}},
            "uid": f"{p.get('id')}@{amount}",
        })
    return out


PS_GRID_HASH = "4ce7d410a4db2c8b635a48c1dcec375906ff63b19dadd87e073f8fd0c0481d35"  # PS Store's own "category grid" query


def items_from_psn(feed, prices):
    """PlayStation Store (US): newly discounted games among the store's 200 best sellers.

    First run only records what's already on sale, so we post sales as they start."""
    key0 = "psn:first-run-done"
    first_time = key0 not in prices
    prices.setdefault(key0, NOW.isoformat(timespec="seconds"))
    out = []
    seen_titles = set()
    for offset in range(0, feed.get("top", 200), 50):
        var = json.dumps({"id": feed["category"], "pageArgs": {"size": 50, "offset": offset},
                          "sortBy": {"name": "sales30", "isAscending": False}, "filterBy": [], "facetOptions": []})
        ext = json.dumps({"persistedQuery": {"version": 1, "sha256Hash": PS_GRID_HASH}})
        url = ("https://web.np.playstation.com/api/graphql/v1/op?operationName=categoryGridRetrieve&variables="
               + urllib.parse.quote(var) + "&extensions=" + urllib.parse.quote(ext))
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                                   "x-psn-store-locale-override": "en-US", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            grid = json.loads(r.read())["data"]["categoryGridRetrieve"]
        for p in grid.get("products") or []:
            pr = p.get("price") or {}
            if not pr.get("discountText") or pr.get("isTiedToSubscription") or pr.get("isFree"):
                continue
            pct = int(re.sub(r"\D", "", pr["discountText"]) or 0)
            k = f"psn:{p['id']}"
            seen_before = prices.get(k)
            prices[k] = pr.get("discountedPrice")
            if first_time or seen_before == pr.get("discountedPrice") or pct < feed.get("min_savings", 30):
                continue
            title_id = p.get("npTitleId") or p["id"]
            if title_id in seen_titles:
                continue
            seen_titles.add(title_id)
            media = {m.get("role"): m.get("url") for m in p.get("media") or [] if m.get("type") == "IMAGE" or str(m.get("url", "")).endswith((".png", ".jpg"))}
            image = media.get("MASTER") or media.get("GAMEHUB_COVER_ART") or media.get("EDITION_KEY_ART") or ""
            plats = "/".join(x for x in ["PS4", "PS5"] if x in (p.get("platforms") or []))
            name = re.sub(r"\s+(PS4\s*&\s*PS5|PS5|PS4)$", "", p.get("name", "")).strip()
            link = f"https://store.playstation.com/en-us/product/{p['id']}"
            out.append({"title": name, "link": link, "html": "", "summary": "", "date": NOW, "uid": f"{k}@{pr.get('discountedPrice')}",
                        "deal": {"item": f"{name} ({plats})" if plats else name, "price": pr["discountedPrice"].lstrip("$"),
                                 "store": "US PSN", "note": f"{pct}% off", "url": link, "image": image}})
    return out[: feed.get("max_per_run", 4)]


def items_from_eshop(feed):
    """Nintendo eShop (US): popular games whose sale started in the last day, via nintendo.com's own search."""
    hits = []
    for page in range(feed.get("pages", 6)):
        body = json.dumps({"requests": [{"indexName": "store_game_en_us",
                                         "params": f"query=&hitsPerPage=100&page={page}&filters=topLevelFilters:\"Deals\""}]}).encode()
        req = urllib.request.Request("https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries", data=body, headers={
            "x-algolia-application-id": "U3B6GR4UA3", "x-algolia-api-key": "a29c6927638bfd8cee23993e51e721c9",
            "content-type": "application/json", "User-Agent": APP_UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            batch = json.loads(r.read())["results"][0]["hits"]
        hits += batch
        if len(batch) < 100:
            break
    out = []
    for h in hits:
        price = h.get("price") or {}
        ed = h.get("eshopDetails") or {}
        rank = h.get("popularityRank") or 10 ** 9
        if rank > feed.get("max_popularity_rank", 300) or (price.get("percentOff") or 0) < feed.get("min_savings", 30):
            continue
        if h.get("topLevelCategory") != "Games" or ed.get("productType") not in (None, "TITLE", "BUNDLE"):
            continue
        start = parse_date(ed.get("discountPriceStart"))
        if not start or NOW - start > timedelta(hours=feed.get("new_within_hours", 24)):
            continue
        plat = {"Nintendo Switch 2": "Switch 2", "Nintendo Switch": "Switch"}.get(h.get("platform"), "")
        title = re.sub(r"\s+[–-]\s+Nintendo Switch(?:™)?\s*2 Edition$", "", h.get("title", "")).strip()
        link = "https://www.nintendo.com" + (h.get("url") or "")
        img = h.get("productImageSquare") or (("https://assets.nintendo.com/image/upload/" + h["productImage"]) if h.get("productImage") else "")
        out.append({"title": title, "link": link, "html": "", "summary": "", "date": start, "uid": f"eshop:{h.get('nsuid')}@{price.get('finalPrice')}",
                    "deal": {"item": f"{title} ({plat})" if plat else title, "price": f"{price.get('finalPrice'):.2f}",
                             "store": "US eShop", "note": f"{round(price.get('percentOff') or 0)}% off", "url": link, "image": img,
                             "verify": {"eshop_nsuid": h.get("nsuid")}}})
    # one post per game: "Game (Switch/Switch 2)" when both versions are on sale
    merged = {}
    for it in out:
        base = it["title"].lower()
        if base in merged:
            m = merged[base]
            p1 = re.search(r"\(([^)]*)\)$", m["deal"]["item"]); p2 = re.search(r"\(([^)]*)\)$", it["deal"]["item"])
            if p1 and p2 and p2.group(1) not in p1.group(1):
                plats = "/".join(sorted({p1.group(1), p2.group(1)}, key=len))
                m["deal"]["item"] = f"{m['title']} ({plats})"
            if float(it["deal"]["price"]) < float(m["deal"]["price"]):
                m["deal"].update(price=it["deal"]["price"], note=it["deal"]["note"])
        else:
            merged[base] = it
    out = sorted(merged.values(), key=lambda it: it["date"], reverse=True)
    return out[: feed.get("max_per_run", 4)]


def recent(stamp, days):
    t = parse_date(stamp) if stamp else None
    return bool(t) and NOW - t < timedelta(days=days)


def shopify_live(domain, handle):
    """The product's live state straight from the shop: (available, price in dollars). None if it can't be read."""
    try:
        p = json.loads(fetch(f"https://{domain}/products/{handle}.js", ua=APP_UA))
        vs = p.get("variants") or []
        avail = [v for v in vs if v.get("available")]
        if not avail:
            return False, None
        return True, min(v.get("price", 0) for v in avail) / 100.0
    except Exception:
        return None


def items_from_shopify(feed, prices):
    """New price drops and new pre-orders/releases from a Shopify shop.

    The first time a shop is seen, its current prices are only recorded (no drafts),
    so we don't flood the feed with every item already on sale."""
    domain = feed["domain"]
    data = json.loads(fetch(f"https://{domain}/products.json?limit=250", ua=APP_UA))
    shop_key = f"shop:{domain}"
    first_time = shop_key not in prices
    prices.setdefault(shop_key, NOW.isoformat(timespec="seconds"))
    cur = feed.get("currency", "USD")
    out = []
    for p in data.get("products", []):
        if not p.get("variants"):
            continue
        v = p["variants"][0]
        try:
            price = float(v.get("price") or 0)
            compare = float(v.get("compare_at_price") or 0)
        except ValueError:
            continue
        key = f"{domain}:{v.get('id')}"
        old = prices.get(key)
        prices[key] = price
        in_stock = any(x.get("available") for x in p["variants"])
        was_in_stock = prices.get(key + ":stock")
        prices[key + ":stock"] = in_stock
        url = f"https://{domain.replace('www.', '')}/products/{p.get('handle')}"
        title = re.sub(r"\s+", " ", p.get("title", "")).strip()
        image = ((p.get("images") or [{}])[0] or {}).get("src", "")
        if first_time:
            continue
        sale_pct = round((1 - price / compare) * 100) if compare > price > 0 else 0
        is_new = old is None
        is_preorder = bool(re.search(r"pre-?order", title + " " + " ".join(p.get("tags") or []), re.I))
        dropped = old is not None and price < float(old) * (1 - feed.get("min_drop", 0.10))
        restocked = (was_in_stock is False and in_stock and feed.get("new_items")
                     and not recent(prices.get(key + ":restocked"), days=7))
        if not (dropped or is_new or restocked):
            continue
        # verify against the shop's live product data before anything becomes a draft
        live = shopify_live(domain, p.get("handle"))
        if not live or not live[0]:
            continue  # couldn't confirm, or not actually available
        if cur == "USD" and abs(live[1] - price) > 0.009:
            if dropped:
                continue  # price moved since the listing: don't claim a drop we can't confirm
            price = live[1]  # new item / restock: show the shop's live price
        if restocked and not is_new and not dropped:
            prices[key + ":restocked"] = NOW.isoformat(timespec="seconds")
            price_txt = f" (${price:.2f})" if cur == "USD" and price else ""
            out.append({"title": title, "link": url, "html": "", "summary": "", "date": NOW,
                        "uid": f"{key}@restock@{NOW.date().isoformat()}",
                        "deal": {"item": title, "price": None, "store": feed["store"], "note": "", "url": url,
                                 "custom": f"{title} is back in stock at {feed['store']}{price_txt}", "event": "restock",
                                 "price_txt": price_txt.strip(), "image": image}})
            continue
        if dropped and (sale_pct >= feed.get("min_sale", 20) or price < float(old) * 0.8):
            note = f"{sale_pct}% off" if sale_pct else ""
            out.append({"title": title, "link": url, "html": "", "summary": "", "date": NOW, "uid": f"{key}@{price}",
                        "deal": {"item": title, "price": f"{price:.2f}" if cur == "USD" else None, "store": feed["store"],
                                 "note": note or ("on sale" if cur != "USD" else ""), "url": url, "image": image}})
        elif is_new and feed.get("new_items") and (is_preorder or feed.get("all_new")):
            label = "up for pre-order" if is_preorder else "now available"
            clean = re.sub(r"\s*[\(\[]\s*pre-?order\s*[\)\]]", "", title, flags=re.I).strip()
            price_txt = f" (${price:.2f})" if cur == "USD" and price else ""
            out.append({"title": title, "link": url, "html": "", "summary": "", "date": NOW, "uid": f"{key}@new",
                        "deal": {"item": clean, "price": None, "store": feed["store"], "note": "", "url": url,
                                 "custom": f"{clean} {label} at {feed['store']}{price_txt}",
                                 "event": "preorder" if is_preorder else "new", "price_txt": price_txt.strip(), "image": image}})
    return out[: feed.get("max_per_run", 3)]


# ---------------------------------------------------------------------------
# Post writing
# ---------------------------------------------------------------------------

def fit(text, url, suffix, limit, url_len=None):
    """Trim text so text + url + suffix fits the platform limit."""
    link_len = url_len if url_len is not None else len(url)
    room = limit - link_len - len(suffix) - 2
    if len(text) > room:
        text = text[: max(room - 1, 10)].rstrip() + "…"
    parts = [text, url] + ([suffix] if suffix else [])
    return " ".join(p for p in parts if p)


PLATFORM_DASH = re.compile(r"\s+[-–]\s+((?:Nintendo )?Switch(?: 2)?|PS[45]|PlayStation [45]|Xbox(?: Series X| One)?|PC|Steam Deck)\b", re.I)


def platform_parens(text):
    """'Game - Nintendo Switch' -> 'Game (Nintendo Switch)'."""
    return PLATFORM_DASH.sub(lambda m: f" ({m.group(1)})", text, count=1)


# Several accurate ways to say the same deal, so the feed doesn't read like one template and the
# X / Threads / Bluesky versions aren't word-for-word copies. Every phrasing uses only the deal's own facts;
# wording that implies a change ("drops to") is only used when the data shows a discount.
def deal_sentences(d, seed):
    """{"x": ..., "threads": ..., "bluesky": ...} - three different phrasings of the same facts."""
    options = deal_variants(d)
    n = int(hashlib.sha1(seed.encode()).hexdigest(), 16)
    k = len(options)
    picks = [options[(n + i) % k] for i in range(3)] if k >= 3 else [options[(n + i) % k] for i in range(3)]
    return dict(zip(("x", "threads", "bluesky"), picks))


def deal_variants(d):
    base = deal_sentence(d)
    item = platform_parens(re.sub(r"\s+", " ", d.get("item") or "").strip())
    store = d.get("store") or ""
    if not item or not store:
        return [base]
    ev = d.get("event")
    pt = f" ({d['price_txt'].strip('() ')})" if d.get("price_txt") else ""
    if ev == "restock":
        return [base, f"Back in stock at {store}: {item}{pt}", f"{store} has {item} back in stock{pt}"]
    if ev == "preorder":
        return [base, f"Pre-orders are open at {store}: {item}{pt}", f"{store} is taking pre-orders for {item}{pt}"]
    if ev == "new":
        return [base, f"New at {store}: {item}{pt}", f"{store} has added {item}{pt}"]
    if d.get("custom"):
        return [base]
    if d.get("note") == "free":
        suf = d.get("suffix", "")
        return [base, f"{store} is giving away {item}{suf}", f"Free on {store}{suf}: {item}"]
    if d.get("price"):
        digital = re.search(r"steam|gog|eshop|nintendo|psn|playstation store|xbox|epic|humble|fanatical|green man|"
                            r"gamesplanet|gamebillet|itch|indiegala|ubisoft|ea app|battle\.net|amazon", store, re.I)
        prep = "on" if digital else "at"
        code = f" with code {d['code']}" if d.get("code") else ""
        extras = [x for x in (d.get("ship"), d.get("note")) if x]
        # no nested brackets, and no "Reg. $X" / "was $X" claims we can't confirm with the store
        extras = [re.sub(r"^\((.*)\)$", r"\1", x.strip()) for x in extras]
        extras = [x for x in extras if x and not re.match(r"(?:reg(?:ular)?\.?|list|was|orig(?:inal)?\.?|msrp)\b", x, re.I)]
        ex = " (" + ", ".join(extras) + ")" if extras else ""
        p = f"${d['price']}"
        out = [base,
               f"{store} has {item} for {p}{code}{ex}",
               f"{item} for {p} {prep} {store}{code}{ex}"]
        pct = re.search(r"(\d+)% off", d.get("note") or "")
        if pct:  # a confirmed discount: these phrasings are accurate
            rest = [x for x in extras if "% off" not in x]
            rx = " (" + ", ".join(rest) + ")" if rest else ""
            out += [f"{item} drops to {p} {prep} {store}{code}{rx}, {pct.group(1)}% off",
                    f"{item} is {pct.group(1)}% off {prep} {store}, now {p}{code}{rx}"]
        return out
    if d.get("note") == "on sale":
        return [base, f"{store} has {item} on sale", f"On sale at {store}: {item}"]
    return [base]


def deal_sentence(d):
    if d.get("custom"):
        return platform_parens(d["custom"])
    item = platform_parens(re.sub(r"\s+", " ", d["item"]).strip())
    store = d["store"]
    if d.get("note") == "free":
        base = f"{item} is free on {store}" if store else f"{item} is free"
        return base + d.get("suffix", "")
    if d.get("price"):
        s = f"{item} is ${d['price']}"
        if store:
            digital = re.search(r"steam|gog|eshop|nintendo|psn|playstation store|xbox|epic|humble|fanatical|green man|gamesplanet|gamebillet|itch|indiegala|ubisoft|ea app|battle\.net|amazon", store, re.I)
            s += f" {'on' if digital else 'at'} {store}"
        if d.get("code"):
            s += f" w/ code {d['code']}"
        extras = [x for x in (d.get("ship"), d.get("note")) if x]
        # no nested brackets, and no "Reg. $X" / "was $X" claims we can't confirm with the store
        extras = [re.sub(r"^\((.*)\)$", r"\1", x.strip()) for x in extras]
        extras = [x for x in extras if x and not re.match(r"(?:reg(?:ular)?\.?|list|was|orig(?:inal)?\.?|msrp)\b", x, re.I)]
        if extras:
            s += " (" + ", ".join(extras) + ")"
        return s
    if store and d.get("note") and d["note"] != "on sale":
        return f"{item} is {d['note']} at {store}"
    if store and d.get("note") == "on sale":
        return f"{item} is on sale at {store}"
    if store:
        return f"{item} at {store}" + (f" ({d['note']})" if d.get("note") else "")
    return item


LINKABLE = re.compile(r"(?i)\bhttps?://\S+|\bwww\.\S+|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|dk|uk|de|fr|jp|ca|us|gg|tv|me|ly|app|dev|store|shop|games?|xyz|info|biz|eu|nl|se|no|fi|it|es|au|nz|ru|cn|kr|br|mx|ai|fm|to|be|ch|at|pl|cz)\b")


def unlink(text):
    """Only the deal's own link may be a link: strip URLs and stop names like "SMiLE.dk" turning into links."""
    text = re.sub(r"(?i)\bhttps?://\S+|\bwww\.\S+", "", text)
    text = re.sub(r"[™®©]|\((?:tm|r)\)", "", text, flags=re.I)  # drop ™ ® ©
    # a one-dot leader (looks like a period) keeps the name readable without the apps linking it
    text = LINKABLE.sub(lambda m: m.group(0).replace(".", "\u2024"), text)
    return re.sub(r"\s{2,}", " ", text).strip()


def make_posts(text, url, disclose, cfg):
    texts = text if isinstance(text, dict) else {"x": text, "threads": text, "bluesky": text}
    texts = {k: unlink(v) for k, v in texts.items()}
    x_tag = cfg["x_disclosure"] if disclose else ""
    b_tag = cfg["bluesky_disclosure"] if disclose else ""
    t_tag = cfg["threads_disclosure"] if disclose else ""
    return {
        "x": fit(texts["x"], url, x_tag, 280, url_len=23),          # X counts every link as 23 characters
        "bluesky": fit(texts["bluesky"], url, b_tag, 300),
        "threads": fit(texts["threads"], url, t_tag, 500),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = load_json(CONFIG_PATH, {})
    amazon_tag = os.environ.get("AMAZON_TAG") or cfg.get("amazon_tag", "")
    max_age = timedelta(hours=cfg.get("max_item_age_hours", 12))

    seen = load_json(SEEN_PATH, {})
    drafts = load_json(DRAFTS_PATH, [])
    tips = load_json(TIPS_PATH, [])
    prices = load_json(PRICES_PATH, {})
    seen_keys = {v.get("key") for v in seen.values() if isinstance(v, dict)}
    status = {"run_at": NOW.isoformat(timespec="seconds"), "feeds": []}

    new_drafts, new_tips = [], []
    for n, feed in enumerate(cfg.get("feeds", [])):
        if n:
            time.sleep(2)  # be polite between requests
        info = {"name": feed["name"], "ok": False, "items": 0, "new": 0, "error": ""}
        try:
            if feed["parser"] == "cheapshark":
                items = items_from_cheapshark(feed)
            elif feed["parser"] == "epic_free":
                items = items_from_epic(feed)
            elif feed["parser"] == "gog":
                items = items_from_gog(feed)
            elif feed["parser"] == "shopify":
                items = items_from_shopify(feed, prices)
            elif feed["parser"] == "keystores":
                items = items_from_keystores(dict(feed, _seen_uids={v.get("uid") for v in seen.values() if isinstance(v, dict)}))
            elif feed["parser"] == "psn":
                items = items_from_psn(feed, prices)
            elif feed["parser"] == "eshop":
                items = items_from_eshop(feed)
            elif feed["parser"] == "camel":
                items = items_from_camel(feed)
            else:
                items = parse_feed(fetch(feed["url"]))
            info["ok"], info["items"] = True, len(items)
        except Exception as e:  # keep going if one feed fails
            body = ""
            if hasattr(e, "read"):
                try:
                    body = " | " + e.read()[:150].decode("utf-8", "ignore")
                except Exception:
                    pass
            info["error"] = f"{type(e).__name__}: {e}{body}"[:300]
            status["feeds"].append(info)
            print(f"[warn] {feed['name']}: {info['error']}", file=sys.stderr)
            continue

        for it in items:
            iid = item_id(feed["name"], it.get("uid") or it["link"] or it["title"])
            prev = seen.get(iid)
            # already handled: only a deal we posted before may come back, and only at a lower price
            if prev and (feed["kind"] != "deal" or prev.get("price") is None):
                continue
            if it["date"] and NOW - it["date"] > max_age and not feed.get("max_age_ignore"):
                seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                continue
            kind = feed["kind"]
            found = (it["date"] or NOW).isoformat(timespec="seconds")

            if kind == "tip":
                store_links = []
                for short in re.findall(r"\b(?:buff\.ly|bit\.ly)/[A-Za-z0-9]+", it["summary"])[:3]:
                    if not SHORTLINK_OK[0]:
                        break
                    final, _ = clean_link(follow_locations("https://" + short), "")
                    if final and "buff.ly" not in final and "bit.ly" not in final:
                        store_links.append(final)
                new_tips.append({"id": iid, "source": feed["name"], "text": it["summary"][:300], "link": it["link"],
                                 "store_links": store_links, "found_at": found})
                seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                info["new"] += 1
                continue

            if kind == "news":
                if not keyword_hit(it["title"], cfg.get("news_keywords", []) + cfg.get("themed_promo_keywords", [])):
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue
                # gaming news from official/news sources: the headline and the link, mixed in with the deals
                title = re.sub(r"\s+", " ", html.unescape(it["title"] or "")).strip()
                title = re.sub(r"\s+[-|\u2013\u2014]\s+(?:PlayStation\.Blog|Xbox Wire|Gematsu)$", "", title, flags=re.I)
                link, _ = clean_link(it["link"], "")
                if not title or not link:
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue
                seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                pic = ""
                try:  # the article's own share image (the publisher's key art / screenshot), like Wario64's news posts
                    page = fetch(link)
                    page = page.decode("utf-8", "replace") if isinstance(page, bytes) else page
                    og = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page) or \
                        re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', page)
                    pic = html.unescape(og.group(1)) if og else ""
                except Exception:
                    pic = ""
                news_extra = {"picture": pic, "alt": f"{title}, image from {feed['name']}"[:400],
                              "picture_type": "article image"} if pic.startswith("http") else {}
                new_drafts.append({**news_extra, "id": iid, "base": iid, "kind": "news", "source": feed["name"], "store": feed["name"],
                                   "found_at": found, "source_link": it["link"], "url": link, "link_type": "article",
                                   "image": "", "multi": False, "code": "", "posts": make_posts(title, link, False, cfg),
                                   "item": title, "price": "", "verify": {}, "status": "new"})
                info["new"] += 1
                if info["new"] >= feed.get("max_per_run", 2):
                    break
                continue
            else:
                hay = f"{it['title']} {it['summary'][:300]}"
                # theme check on the deal's title (summaries add noise like "snow blower" matches)
                # filtered sources need a real gaming/franchise word, not just a product type like "plush"
                generic = set(cfg.get("generic_keywords", []))
                if feed.get("filter") and not keyword_hit(it["title"], [k for k in cfg.get("include_keywords", []) if k not in generic]):
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue
                if keyword_hit(hay, cfg.get("exclude_keywords", [])):
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue
                if keyword_hit(hay, cfg.get("themed_promo_keywords", [])):
                    # restaurant / theater tie-ins (themed meals, popcorn buckets): news-style, posted by hand
                    new_tips.append({"id": iid, "source": feed["name"], "kind": "themed meal / promo",
                                     "text": it["title"], "link": it["link"], "found_at": found})
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    info["new"] += 1
                    continue
                if "deal" in it:
                    d = dict(it["deal"])
                elif feed["parser"] == "slickdeals":
                    d = deal_from_slickdeals(it)
                else:
                    d = deal_from_reddit(it)
                new_price = price_num(d.get("price"))
                if prev:
                    # never the same deal twice. A new post only when the price is at least 5% below what we posted,
                    # and only from the next day on: a same-day drop waits, and must still be live (seen again in the
                    # store's own data, and re-checked before drafting) the next day.
                    if new_price is None or new_price > prev["price"] * 0.95:
                        continue
                    first = parse_date(prev.get("at"))
                    if first and first.astimezone(LOCAL_TZ).date() >= NOW.astimezone(LOCAL_TZ).date():
                        continue
                url, affiliate = clean_link(d["url"], amazon_tag)
                if len(re.sub(r"[^A-Za-z]", "", d.get("item") or "")) < 3:
                    new_tips.append({"id": iid, "source": feed["name"], "kind": "couldn't read the item name",
                                     "text": it["title"], "link": it["link"], "found_at": found})
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue
                if d.get("creator_code"):
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue  # a deal site's or creator's own code: not ours to post
                if feed.get("amazon_only") and d.get("store") != "Amazon":
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue
                if d.get("third_party") and d.get("store") != "Amazon":
                    # sold by a marketplace seller: not a first-party price, so no draft (listed for reference)
                    new_tips.append({"id": iid, "source": feed["name"], "kind": "third-party seller",
                                     "text": it["title"], "link": it["link"], "found_at": found})
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue
                if not url:
                    # no usable store link (e.g. Slickdeals wouldn't reveal it): keep as a tip for manual posting
                    new_tips.append({"id": iid, "source": feed["name"], "text": it["title"], "link": it["link"], "found_at": found})
                    seen[iid] = {"at": NOW.isoformat(timespec="seconds"), "key": None}
                    continue
                if not d["store"] and url:
                    d["store"] = store_from_host(urllib.parse.urlparse(url).netloc)
                d["item"] = clean_item(d.get("item") or "", d.get("store") or "")
                text = deal_sentences(d, iid + str(d.get("price") or ""))
                disclose = affiliate or cfg.get("tag_all_deal_posts", True)
                key = norm_key(d["item"]) + f" {d.get('price') or ''}"
                asin = re.search(r"amazon\.com/(?:[^/]+/)?dp/([A-Z0-9]{10})", url or "")
                if asin:  # same Amazon product from Slickdeals and camelcamelcamel: draft it once
                    key = f"amazon:{asin.group(1)}:{round(price_num(d.get('price')) or 0)}"
                wm = re.search(r"walmart\.com/ip/(?:[^/]+/)?(\d+)", url or "")
                if wm:
                    key = f"walmart:{wm.group(1)}:{round(price_num(d.get('price')) or 0)}"
                store = d["store"]

            entry = {"at": NOW.isoformat(timespec="seconds"), "key": key}
            if kind == "deal" and price_num(d.get("price")) is not None:
                entry["price"] = price_num(d.get("price"))
            if key and key in seen_keys:
                seen[iid] = prev or entry  # same deal already drafted from another feed
                continue
            seen[iid] = entry
            seen_keys.add(key)
            new_drafts.append({
                # a price drop on a deal we already sent gets its own id, so it goes to Buffer again
                "id": item_id(iid, str(entry.get("price"))) if prev else iid,
                "base": iid,
                "kind": kind,
                "source": feed["name"],
                "store": store,
                "found_at": found,
                "source_link": it["link"],
                "url": url,
                "link_type": ("store search" if kind == "deal" and d.get("search_link") else "product"),
                "image": d.get("image", "") if kind == "deal" else "",
                "multi": bool(d.get("multi")) if kind == "deal" else False,
                "code": d.get("code", "") if kind == "deal" else "",
                "posts": make_posts(text, url, disclose, cfg),
                "item": (d.get("item") or "") if kind == "deal" else "",
                "price": (d.get("price") or "") if kind == "deal" else "",
                "verify": d.get("verify") or {},
                "status": "new",
            })
            info["new"] += 1
            if info["new"] >= feed.get("max_per_run", 99):
                break
        status["feeds"].append(info)

    limit = cfg.get("max_new_drafts_per_run", 25)
    new_drafts.sort(key=lambda d: d["found_at"], reverse=True)
    new_drafts = new_drafts[:limit]

    # a price drop replaces an older, not-yet-sent draft of the same deal
    dropped = {d["base"] for d in new_drafts if d["id"] != d["base"]}
    for d in drafts:
        if d.get("base", d["id"]) in dropped and d.get("status") == "new":
            d["status"] = "replaced by lower price"
    drafts = (new_drafts + drafts)[: cfg.get("keep_drafts", 300)]
    tips = (new_tips + tips)[:200]

    cutoff = NOW - timedelta(days=cfg.get("seen_days", 10))
    seen = {k: v for k, v in seen.items() if parse_date(v.get("at")) and parse_date(v.get("at")) > cutoff}

    status["new_drafts"] = len(new_drafts)
    status["new_tips"] = len(new_tips)
    save_json(SEEN_PATH, seen)
    save_json(PRICES_PATH, prices)
    save_json(DRAFTS_PATH, drafts)
    save_json(TIPS_PATH, tips)
    save_json(STATUS_PATH, status)
    write_report(drafts[:60], status)
    print(json.dumps(status, indent=2))


def write_report(drafts, status):
    lines = ["# Latest drafts", "", f"Last run: {status['run_at']} UTC", "", "| Feed | OK | Items | New |", "| --- | --- | --- | --- |"]
    for f in status["feeds"]:
        lines.append(f"| {f['name']} | {'yes' if f['ok'] else 'NO: ' + f['error'][:60]} | {f['items']} | {f['new']} |")
    tips = load_json(TIPS_PATH, [])[:25]
    lines += ["", "## Drafts (newest first)", ""]
    for d in drafts:
        flag = " · ⚠ search link: swap in the product page before posting" if d.get("link_type") == "store search" else ""
        lines.append(f"**{d['kind'].upper()}** · {d['source']} · found {d['found_at']}{flag}")
        lines.append("")
        lines.append(f"- X / Bluesky: {d['posts']['x']}")
        lines.append(f"- Threads: {d['posts']['threads']}")
        lines.append("")
    lines += ["## To check by hand (news, tips, deals without a usable link)", ""]
    for t in tips:
        extra = "".join(f" · store link: {u}" for u in t.get("store_links", []))
        lines.append(f"- {t.get('kind', 'tip')} · {t['source']} · {t['text'][:140]} — {t['link']}{extra}")
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
