"""Pick the picture for each new deal draft, following how Wario64 illustrates his posts.

Rules
- Amazon (any product): NEVER a screenshot of Amazon's page (Associates terms). We open the
  product page only to read the main product photo, the current price and availability:
    * games, movies, music: the box/case art, cropped tight
    * everything else (toys, LEGO, accessories, tech): the product photo on its white background
  The price in the post is corrected to Amazon's current price, and unavailable items are skipped.
- Steam / GOG / Epic single games: a cropped screenshot of the store's product area (art + price),
  without the site's header.
- Other single products (Shopify shops, Best Buy, GameStop, ...): the store's full-size product
  photo; if there isn't one, a cropped screenshot of the product area.
- Multi-item deals (sales, "& more", search pages) outside Amazon: a cropped screenshot of the
  product grid, without the site's header.
- If a store blocks us, or the only photo is a low-quality thumbnail, the draft gets no picture
  and is flagged "needs picture" so it can be added by hand.

Screenshots go to shots/; the workflow publishes them on the "images" branch (last 7 days only).
"""
import io
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
IMG_DIR = os.path.join(ROOT, "shots")
DRAFTS_PATH = os.path.join(DATA, "drafts.json")
SENT_PATH = os.path.join(DATA, "buffer_sent.json")
REPO_RAW = "https://raw.githubusercontent.com/" + os.environ.get("GITHUB_REPOSITORY", "jwest1985/price-notes") + "/images/"

MAX_PER_RUN = 7
MAX_AGE = timedelta(hours=6)
MIN_PHOTO = 600  # px; smaller store photos (e.g. Slickdeals thumbnails) look poor
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BLOCKED = re.compile(r"robot check|captcha|access denied|just a moment|are you a human|pardon our interruption|"
                     r"request blocked|verify you are|unusual traffic|403 forbidden|robot or human|press & hold|"
                     r"quick verification|confirm you.re a human|security check|page not found", re.I)
AGE_GATE = re.compile(r"mature content|ages? 1[78]\+|aged? 1[78]|age verification|verify your age|enter your birth|"
                      r"birth ?date|are you (?:over )?1[78]|not be appropriate for all ages|adults? only", re.I)
AMAZON_BOXED = re.compile(r"video games|movies\s*&\s*tv|blu-ray|dvd|4k ultra hd|\bmovies\b|tv shows|cds? & vinyl|music",
                          re.I)
# product area of single-game pages at a 1200px-wide window: (top, height)
FIXED_CLIPS = {"gog.com": (108, 740), "store.epicgames.com": (160, 700), "store.steampowered.com": (225, 620)}
NOW = datetime.now(timezone.utc)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def host_of(url):
    return re.sub(r"^www\.", "", urllib.parse.urlparse(url).netloc.lower()) if url else ""


# --------------------------------------------------------------------------- images

def trim(img, margin_ratio=0.0, thresh=245):
    """Crop away near-white borders; optionally add back a thin white margin."""
    from PIL import Image, ImageChops
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > 255 - thresh else 0)
    box = diff.getbbox()
    if box:
        rgb = rgb.crop(box)
    if margin_ratio:
        m = int(max(rgb.size) * margin_ratio)
        out = Image.new("RGB", (rgb.width + 2 * m, rgb.height + 2 * m), (255, 255, 255))
        out.paste(rgb, (m, m))
        rgb = out
    # keep it a sensible size for social posts (Wario64's are ~1,200-1,600px)
    if max(rgb.size) > 1600:
        rgb.thumbnail((1600, 1600))
    return rgb


def pad_to_shape(img, lo=0.8, hi=1.91):
    """Put a too-tall or too-wide product photo on white so feeds show all of it (4:5 up to 1.91:1)."""
    from PIL import Image
    w, h = img.size
    if w / h < lo:
        nw, nh = int(h * lo), h
    elif w / h > hi:
        nw, nh = w, int(w / hi)
    else:
        return img
    out = Image.new("RGB", (nw, nh), (255, 255, 255))
    out.paste(img, ((nw - w) // 2, (nh - h) // 2))
    return out


def view_quality(data):
    """(edge_white, aspect): how plain-white the photo's outer edge is, and the product's shape after trimming."""
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if max(img.size) < MIN_PHOTO:
        return 0.0, 0
    small = img.copy()
    small.thumbnail((200, 200))
    w, h = small.size
    b = max(2, int(min(w, h) * 0.06))
    px = small.load()
    edge = [px[x, y] for x in range(w) for y in range(h) if x < b or y < b or x >= w - b or y >= h - b]
    white = sum(1 for r, g, bl in edge if r > 235 and g > 235 and bl > 235) / len(edge)
    t = trim(img)
    return white, t.width / t.height


GOOD_SHAPE = (0.75, 2.2)


def amazon_gallery(page):
    """Amazon's own gallery photos for the product (main first), as hi-res URLs."""
    try:
        html = page.content()
    except Exception:
        return []
    # either 'initial': [...] or 'initial': A.$.parseJSON('[...]')
    m = (re.search(r"'colorImages'\s*:\s*\{\s*'initial'\s*:\s*[\w.$]*parseJSON\('(\[.*?\])'\)", html, re.S)
         or re.search(r"'colorImages'\s*:\s*\{\s*'initial'\s*:\s*(\[.*?\])\s*\}", html, re.S))
    out = []
    if m:
        try:
            for e in json.loads(m.group(1).replace("\\'", "'")):
                u = e.get("hiRes") or e.get("large")
                if u and u not in out:
                    out.append(u)
        except Exception:
            pass
    return out


# Amazon product images are not used until Amazon approves it in writing or PA API access (10 sales in 30 days).
# The Amazon page is still checked for price and stock.
AMAZON_PHOTOS = False
MAX_BYTES = 950_000  # under Bluesky's strictest stated limit (1 MB); far under X and Threads


def save_fit(img, path):
    """Save as sharp a JPEG as fits under MAX_BYTES: lower quality a little first, then size if needed."""
    img = img.convert("RGB")
    while True:
        for q in (90, 85, 80, 75):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q, optimize=True, progressive=True, subsampling=0 if q >= 85 else 2)
            if buf.tell() <= MAX_BYTES:
                with open(path, "wb") as f:
                    f.write(buf.getvalue())
                return
        img = img.resize((int(img.width * 0.85), int(img.height * 0.85)))


def save_photo(data, path, margin_ratio, pad=False):
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    if max(img.size) < MIN_PHOTO:
        return False
    out = trim(img, margin_ratio)
    # products: padded to 4:5 .. 1.91:1; box art: tight, only the thinnest white strip needed so X shows it whole (3:4 .. 2:1)
    out = pad_to_shape(out) if pad else pad_to_shape(out, 0.75, 2.0)
    save_fit(out, path)
    return True


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


# --------------------------------------------------------------------------- pages

def open_page(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    try:
        page.wait_for_load_state("networkidle", timeout=9000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    pass_age_gate(page)
    title = page.title() or ""
    try:
        body = page.inner_text("body", timeout=5000)[:3000]
    except Exception:
        body = ""
    if not title.strip() or BLOCKED.search(title) or BLOCKED.search(body[:600]) or len(body.strip()) < 200:
        return False
    close_popups(page)
    return True


def pass_age_gate(page):
    """Click through "are you 17/18+" pages (GOG, Steam, publisher sites) to reach the real product page."""
    for _ in range(2):
        try:
            text = page.inner_text("body", timeout=4000)[:2500]
        except Exception:
            return
        if not AGE_GATE.search(text):
            return
        for sel in ["#ageYear", "select[name='ageYear']", "select[name*='year' i]", "select[id*='year' i]"]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.select_option("1988")
            except Exception:
                pass
        clicked = False
        for label in ["Continue", "View Page", "Enter", "I am 18", "I'm 18", "Yes", "Confirm", "Submit", "Proceed"]:
            try:
                btn = page.get_by_role("button", name=label, exact=False).first
                if not btn.count() or not btn.is_visible():
                    btn = page.get_by_role("link", name=label, exact=True).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return
        try:
            page.wait_for_load_state("networkidle", timeout=9000)
        except Exception:
            pass
        page.wait_for_timeout(1500)


def close_popups(page):
    # cookie banners: always the "reject"/"necessary only" option
    for sel in ["#onetrust-reject-all-handler", "#CybotCookiebotDialogBodyButtonDecline", "button:has-text('Reject all')",
                "button:has-text('Decline')", "button:has-text('Necessary only')", "button[aria-label='Close']",
                "button[aria-label='close']", "[data-testid='modal-close']", ".needsclick.klaviyo-close-form",
                "button:has-text('No thanks')", "button:has-text('Opt-Out')", "button:has-text('Opt out')"]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=1500)
                page.wait_for_timeout(300)
        except Exception:
            pass


def first_product_top(page):
    """Top (page y) of the first big image below the site header: where the products start."""
    return page.evaluate("""() => {
      let best = null;
      for (const im of document.images) {
        const r = im.getBoundingClientRect();
        if (r.width >= 140 && r.height >= 140 && r.top + window.scrollY > 90) {
          const y = r.top + window.scrollY;
          if (best === null || y < best) best = y;
        }
      }
      return best;
    }""")


def grid_box(page, top):
    """Left/right edges of the product tiles (leaves out filter sidebars)."""
    return page.evaluate("""(top) => {
      let x0 = null, x1 = null;
      for (const im of document.images) {
        const r = im.getBoundingClientRect(), y = r.top + window.scrollY;
        if (r.width >= 120 && r.height >= 120 && y >= top - 5 && y < top + 1100) {
          x0 = x0 === null ? r.left : Math.min(x0, r.left);
          x1 = x1 === null ? r.right : Math.max(x1, r.right);
        }
      }
      return x0 === null ? null : [x0, x1];
    }""", top)


def clip_shot(page, path, top, height, x0=0, x1=1200):
    clean_for_shot(page)  # every screenshot: nothing covering the product
    top = max(0, int(top))
    full_h = page.evaluate("document.documentElement.scrollHeight")
    height = int(min(height, max(200, full_h - top)))
    x0, x1 = max(0, int(x0)), min(1200, int(x1))
    png = page.screenshot(type="png", full_page=True, clip={"x": x0, "y": top, "width": x1 - x0, "height": height})
    from PIL import Image
    save_fit(Image.open(io.BytesIO(png)), path)


# --------------------------------------------------------------------------- per-case pictures

def money(text):
    m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None



def count_amazon_check(how):
    """how: live / backup / unconfirmed. Daily counts, so we can see how often Amazon blocks us."""
    import json as _json
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "amazon_checks.json")
    try:
        data = _json.load(open(path))
    except Exception:
        data = {}
    day = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Toronto")).date().isoformat()
    data.setdefault(day, {}).setdefault(how, 0)
    data[day][how] += 1
    for k in sorted(data)[:-14]:
        data.pop(k)
    _json.dump(data, open(path, "w"), indent=1, sort_keys=True)


def collect_price(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fix_price(d, new_txt, why):
    for k, t in d["posts"].items():
        d["posts"][k] = re.sub(r"\$" + re.escape(str(d["price"])) + r"(?![\d.])", "$" + new_txt, t, count=1)
    d["price_note"] = f"price corrected from ${d['price']} to ${new_txt} ({why})"
    d["price"] = new_txt


def amazon_picture(page, d, path):
    """Amazon pages are never opened (Amazon doesn't allow automated access). The price is confirmed by any one of
    camelcamelcamel (scanned within a day), an active Slickdeals deal, or Wario64/CheapAssGamer (last 24 hours);
    you confirm it again yourself when you make the link."""
    if True:  # backups only
        try:
            import verify_scheduled as vs
            state, live, detail = vs.amazon_backup(d)
        except Exception as e:
            state, live, detail = "unknown", None, str(e)[:80]
        old = collect_price(d.get("price"))
        if state == "live" and live is not None and old is not None:
            if live > old * 1.15 + 0.5:
                d["skip"] = f"deal looks expired: camelcamelcamel shows ${live:.2f} (deal said ${d['price']})"
            elif abs(live - old) > 0.005:
                fix_price(d, f"{live:.2f}", "camelcamelcamel's current Amazon price")
        elif state == "out_of_stock":
            d["skip"] = "out of stock on Amazon (per camelcamelcamel)"
            d["watch_restock"] = True
        elif state != "live":
            # nothing confirmed it (camelcamelcamel, Slickdeals, Wario64, CheapAssGamer): skip the deal entirely
            d["skip"] = f"Amazon price unconfirmed - {detail}"
        count_amazon_check("backup" if state in ("live", "out_of_stock") else "unconfirmed")
        print(f"   amazon backup: {state} {detail}")
        return "no picture (Amazon deal: no Amazon images, by rule)"

def clean_for_shot(page):
    """No hover pop-ups, chat bubbles or sticky bars in the picture."""
    try:
        page.mouse.move(0, 0)
        page.evaluate("""() => {
          for (const el of document.querySelectorAll('body *')) {
            const p = getComputedStyle(el).position;
            if (p === 'fixed' || p === 'sticky') el.style.setProperty('visibility', 'hidden', 'important');
          }
        }""")
        page.wait_for_timeout(500)
    except Exception:
        pass


def tile_rows(tiles):
    """Group product cards into rows by their top edge."""
    rows = []
    for t in sorted(tiles, key=lambda t: (t[1], t[0])):
        if rows and abs(rows[-1][0][1] - t[1]) < 40:
            rows[-1].append(t)
        else:
            rows.append([t])
    return [sorted(r, key=lambda t: t[0]) for r in rows]


GRID_PAGES = 4        # up to 4 pictures per post (X and Bluesky show at most 4)
PAGE_SHAPE = 0.75     # each picture about 3:4, the tallest X shows without cropping


def grid_pages(page, d, path):
    """Big sales, flyers and bundles: the store's own deal cards, split into up to 4 tight 'pages' (like Wario64's
    multi-picture sale posts). Returns the saved paths, or [] if the page has no clear card grid."""
    if not open_page(page, d["url"]):
        return []
    for _ in range(10):  # scroll down step by step so lazy-loaded cards appear
        page.mouse.wheel(0, 1100)
        page.wait_for_timeout(700)
    page.mouse.wheel(0, -20000)
    page.wait_for_timeout(800)
    try:
        rows = [r for r in tile_rows([t for t in page.evaluate(TILES_JS) if t[0] >= 0 and t[2] <= 1200]) if len(r) >= 2]
    except Exception:
        return []
    if not rows:
        return []
    # the main grid: cards of the usual size that line up with the grid's columns (drops stray hover/pop-up boxes)
    from collections import Counter
    count = Counter(len(r) for r in rows if len(r) >= 2).most_common(1)[0][0]
    ref = next(r for r in rows if len(r) == count)
    cols = [t[0] for t in ref]
    w = sorted(t[2] - t[0] for t in ref)[len(ref) // 2]
    cleaned = []
    for r in rows:
        keep = [t for t in r if abs((t[2] - t[0]) - w) < 0.15 * w and any(abs(t[0] - c) < 20 for c in cols)]
        if len(keep) == count:
            cleaned.append(keep)
    rows = cleaned
    if not rows:
        return []
    start = 0
    grid = [rows[start]]
    for r in rows[start + 1:]:
        prev = grid[-1]
        card_h = max(t[3] - t[1] for t in prev)
        gap = min(t[1] for t in r) - max(t[3] for t in prev)
        if len(r) != len(grid[0]) or gap > card_h * 0.5:
            break
        grid.append(r)
    if len(grid) < 2:
        return []
    x0 = min(t[0] for r in grid for t in r) - 10
    x1 = max(t[2] for r in grid for t in r) + 10
    pitch = (max(t[3] for t in grid[-1]) - min(t[1] for t in grid[0])) / len(grid)
    per_page = max(1, int((x1 - x0) / PAGE_SHAPE / pitch))
    paths = []
    for n in range(GRID_PAGES):
        chunk = grid[n * per_page:(n + 1) * per_page]
        if not chunk:
            break
        top = min(t[1] for t in chunk[0]) - 10
        bottom = max(t[3] for t in chunk[-1]) + 10
        p_path = path if n == 0 else path.replace(".jpg", f"-{n + 1}.jpg")
        clip_shot(page, p_path, top, bottom - top, x0, x1)
        paths.append((p_path, sum(len(r) for r in chunk)))
    return paths


def grid_picture(page, d, path):
    """Multi-item deals: the store's own deal cards, 2 full rows, cropped tight (like Wario64's Newegg posts)."""
    if not open_page(page, d["url"]):
        return None
    page.mouse.wheel(0, 900)  # wake up lazy-loaded tiles
    page.wait_for_timeout(1500)
    page.mouse.wheel(0, -900)
    page.wait_for_timeout(800)
    try:
        # fully visible cards only (carousels hide some off to the side)
        rows = [r for r in tile_rows([t for t in page.evaluate(TILES_JS) if t[0] >= 0 and t[2] <= 1200])
                if len(r) >= 2]
    except Exception:
        rows = []
    if rows:
        cols = min(len(rows[0]), 4)
        picked = [rows[0][:cols]]
        card_h = max(t[3] - t[1] for t in rows[0])
        gap = (rows[1][0][1] - max(t[3] for t in rows[0])) if len(rows) > 1 else 9999
        if len(rows) > 1 and len(rows[1]) >= cols and gap < card_h * 0.25:  # the next row of the same grid
            picked.append(rows[1][:cols])
        cards = [t for r in picked for t in r]
        # X shows pictures up to 3:4 tall without cropping; tall cards -> one row
        if len(picked) == 2:
            w = max(t[2] for t in cards) - min(t[0] for t in cards)
            h = max(t[3] for t in cards) - min(t[1] for t in cards)
            if w / h < 0.75:
                cards = picked[0]
        x0 = min(t[0] for t in cards) - 10
        x1 = max(t[2] for t in cards) + 10
        top = min(t[1] for t in cards) - 10
        bottom = max(t[3] for t in cards) + 10
        clip_shot(page, path, top, bottom - top, x0, x1)
        return f"deal cards ({len(cards)}, cropped)"
    # fallback: the old fixed slice under the first product
    top = first_product_top(page)
    if top is None:
        return None
    box = grid_box(page, top)
    x0, x1 = (box[0] - 60, box[1] + 60) if box else (0, 1200)
    if x1 - x0 < 500:
        x0, x1 = 0, 1200
    clip_shot(page, path, top - 30, 1050, x0, x1)
    return "product grid (cropped)"


def product_area_picture(page, d, path):
    if not open_page(page, d["url"]):
        return None
    top = first_product_top(page)
    if top is None:
        return None
    clip_shot(page, path, top - 40, 860)
    return "store page (cropped)"


def amazon_is_boxed(crumbs):
    """Box art only for the games/movies themselves, not gaming accessories filed under Video Games."""
    if re.search(r"movies\s*&\s*tv|blu-ray|\bdvd\b|4k ultra hd|tv shows|cds? & vinyl", crumbs, re.I):
        return True
    if not re.search(r"video games", crumbs, re.I):
        return False
    if re.search(r"accessor|controller|headset|mice|mouse|keyboard|console|hardware|monitor|chair|"
                 r"cable|charg|storage|memory|gift card", crumbs, re.I):
        return False
    segs = [x.strip() for x in re.split(r"[›>\n]+", crumbs) if x.strip()]
    return bool(segs) and re.search(r"\bgames\b", segs[-1], re.I) is not None


def boxed_item(d):
    return bool(re.search(r"\((?:[^)]*\b(?:PS5|PS4|Switch|Xbox|XSX|Series X|4K|Blu-ray|DVD|UHD|Steelbook)\b)", d["posts"]["x"], re.I))


def first_result_photo(page, d, path):
    """Single item behind a store search link (e.g. Best Buy): the first result's full-size photo."""
    if not open_page(page, d["url"]):
        return None
    top = first_product_top(page)
    if top is None:
        return None
    # pick the result that best matches the item (Best Buy lists digital versions and other games too)
    item = (d.get("item") or re.split(r" (?:is|up for|now) ", d["posts"]["x"])[0]).lower()
    want = set(re.findall(r"[a-z0-9]+", item)) - {"the", "and", "of", "a", "for", "with", "edition"}
    tiles = page.evaluate("""() => {
      const out = [];
      for (const im of document.images) {
        const r = im.getBoundingClientRect();
        if (r.width < 120 || r.height < 120) continue;
        let el = im, text = im.alt || '';
        let tile = im;
        for (let i = 0; i < 8 && el; i++) {
          el = el.parentElement;
          if (!el) break;
          // stop before the container that holds other products' tiles
          const links = new Set([...el.querySelectorAll('img')]
            .filter(x => x.getBoundingClientRect().width >= 120)
            .map(x => { const l = x.closest('a[href]'); return l ? l.href.split(/[?#]/)[0] : (x.currentSrc || x.src); }));
          if (links.size > 1) break;
          tile = el;
          if (el.innerText && el.innerText.length > text.length && el.innerText.length < 800) text = el.innerText;
        }
        el = tile;
        const a = im.closest('a[href]') || (tile && tile.querySelector('a[href]'));
        out.push({src: im.currentSrc || im.src, text: text.slice(0, 400), y: r.top + window.scrollY,
                  href: a ? a.href : ''});
      }
      return out.slice(0, 24);
    }""")
    best, best_score = None, 0.0
    for t in tiles:
        words = set(re.findall(r"[a-z0-9]+", t["text"].lower()))
        if not want or not words:
            continue
        score = len(want & words) / len(want)
        if "digital" in words and "digital" not in want:
            score -= 0.3
        if score > best_score:
            best, best_score = t, score
    if not best or best_score < 0.6:
        return None
    sold = re.search(r"sold by\s*:?\s*([^\n]+)", best["text"], re.I)
    store = re.search(r" (?:at|on) ([A-Z][\w&.' ]+?)(?: https?://| \(|$)", d["posts"]["x"])
    if sold and store and store.group(1).lower().split()[0] not in sold.group(1).lower():
        d["skip"] = f"sold by a marketplace seller ({sold.group(1).strip()[:40]})"
    # link straight to the matched product page instead of the search results
    href = best.get("href") or ""
    if not href or host_of(href) != host_of(d["url"]) or re.search(r"search|/s\?|searchpage", href, re.I):
        return None
    href = re.split(r"[?#]", href)[0] if "bestbuy.com" not in href else href.split("#")[0]
    for k, t in d["posts"].items():
        d["posts"][k] = t.replace(d["url"], href)
    d["url"] = href
    d["link_type"] = "product"
    src = best["src"]
    for candidate in [re.split(r"[;?]", src)[0], src]:
        try:
            r = page.request.get(candidate, timeout=15000)
            if r.ok and save_photo(r.body(), path, 0.0 if boxed_item(d) else 0.04, pad=not boxed_item(d)):
                return "box art (cropped)" if boxed_item(d) else "product photo (from store search)"
        except Exception:
            continue
    return None


def photo_from_feed(d, path):
    if not d.get("image"):
        return None
    try:
        if save_photo(fetch_bytes(d["image"]), path, 0.0 if boxed_item(d) else 0.04, pad=not boxed_item(d)):
            return "box art (cropped)" if boxed_item(d) else "product photo"
    except Exception as e:
        print("photo failed:", d["image"][:80], str(e)[:80])
    return None


def code_on_store_page(page, d):
    """Promo codes are only posted when the store itself shows the code on the product page."""
    try:
        if not open_page(page, d["url"]):
            return False
        text = page.inner_text("body", timeout=5000)
    except Exception:
        return False
    return d["code"].lower() in text.lower()


def official_art(d, path, host):
    """The store's own artwork for the game, saved as-is (no trimming, no added margin)."""
    from PIL import Image
    urls = []
    m = re.search(r"/app/(\d+)", d.get("url") or "")
    if host == "store.steampowered.com" and m:
        app = m.group(1)
        urls += [f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{app}/capsule_616x353.jpg",
                 f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{app}/header.jpg"]
    if d.get("image"):
        urls.insert(0 if host != "store.steampowered.com" else len(urls), d["image"])
    for u in urls:
        try:
            img = Image.open(io.BytesIO(fetch_bytes(u))).convert("RGB")
        except Exception as e:
            print("artwork failed:", u[:80], str(e)[:80])
            continue
        if max(img.size) < 450:
            continue
        if max(img.size) > 1600:
            img.thumbnail((1600, 1600))
        save_fit(pad_to_shape(img, 0.75, 2.0), path)
        return "official artwork"
    return None


def pick_picture(page, d, path):
    host = host_of(d.get("url"))
    if not host:
        return "needs picture (no link)"
    if d.get("code") and not code_on_store_page(page, d):
        # held as a tagged draft for you: never posted until you confirm the code on the store's own site
        d.setdefault("flags", []).append("Check promo code")
    if "amazon." in host:
        return amazon_picture(page, d, path)
    if d.get("multi"):
        pages = grid_pages(page, d, path)
        if len(pages) > 1:
            d["_pages"] = pages
            return f"deal cards ({sum(c for _, c in pages)}, {len(pages)} pages)"
        return grid_picture(page, d, path) or "needs picture (store blocked the screenshot)"
    if d.get("link_type") == "store search":
        if first_result_photo(page, d, path):
            return "box art (cropped)" if boxed_item(d) else "product photo"
        # no verified product page: never post a search link that may show nothing
        d["skip"] = "couldn't find the product page at the store (search link only)"
        return "needs picture (no product page)"
    if host in FIXED_CLIPS:
        # GOG, Steam, Epic: the game's official artwork, nothing on top of it (no store-page screenshot)
        return official_art(d, path, host) or "needs picture (no official artwork found)"
    return (photo_from_feed(d, path) or product_area_picture(page, d, path)
            or "needs picture (store blocked the screenshot)")


def alt_text(d, kind):
    """Alt text built only from facts we know for certain: the deal's item name, the store, and where the picture came from."""
    first = d["posts"]["x"].split("http")[0]
    item = (d.get("item") or re.split(r" (?:is|are) (?:\$|free\b)| up for | now ", first)[0]).strip()
    item = re.sub(r"\s*[\u2122\u00ae\u00a9]", "", item).strip()  # no TM/(R)/(C) marks
    store = d.get("store") or host_of(d.get("url")) or "the store"
    # short and factual, product name first (what people and search engines look for)
    if kind.startswith("box art"):
        return f"{item} box art"
    if kind.startswith("product photo"):
        return f"{item} product photo, {store}"
    if kind.startswith("official artwork"):
        return f"{item} official artwork, {store}"
    if kind.startswith("store page"):
        return f"{item} on the {store} store page"
    m = re.match(r"deal cards \((\d+)", kind)
    if m:
        return f"{m.group(1)} {store} deals with names and prices"
    if kind.startswith("product grid"):
        return f"{store} deals page"
    return item


# --------------------------------------------------------------------------- main

PICTURE_STATS_PATH = os.path.join(DATA, "picture_stats.json")
DISABLED_PATH = os.path.join(DATA, "disabled_sources.json")
MIN_TRIES, MIN_SUCCESS = 6, 0.6  # a source+store that can't get pictures this often gets switched off


def record_picture(d, ok):
    """Keep score of pictures per source and store; switch off any that can't get pictures consistently."""
    key = f"{d.get('source', '?')} | {d.get('store', '?')}"
    stats = load(PICTURE_STATS_PATH, {})
    hist = (stats.get(key, []) + [1 if ok else 0])[-20:]
    stats[key] = hist
    with open(PICTURE_STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1, ensure_ascii=False)
    if len(hist) >= MIN_TRIES and sum(hist) / len(hist) < MIN_SUCCESS:
        disabled = load(DISABLED_PATH, {})
        if key not in disabled:
            disabled[key] = {"at": NOW.isoformat(timespec="seconds"), "pictures": f"{sum(hist)} of {len(hist)}"}
            with open(DISABLED_PATH, "w", encoding="utf-8") as f:
                json.dump(disabled, f, indent=1, ensure_ascii=False)
            print(f"SOURCE SWITCHED OFF: {key} (pictures for only {sum(hist)} of {len(hist)} deals)")
            topic = os.environ.get("NTFY_TOPIC", "").strip()
            if topic:
                try:
                    req = urllib.request.Request(f"https://ntfy.sh/{topic}", method="POST", data=(
                        f"{d.get('store')} deals from {d.get('source')} are switched off: pictures worked for only "
                        f"{sum(hist)} of the last {len(hist)} deals.").encode(), headers={"Title": "Source removed", "Tags": "warning"})
                    urllib.request.urlopen(req, timeout=15).read()
                except Exception:
                    pass


def main():
    drafts = load(DRAFTS_PATH, [])
    sent = load(SENT_PATH, {})
    os.makedirs(IMG_DIR, exist_ok=True)

    todo = []
    for d in drafts:
        if d.get("kind") != "deal" or d["id"] in sent or d.get("picture") is not None:
            continue
        try:
            fresh = NOW - datetime.fromisoformat(d["found_at"]) <= MAX_AGE
        except Exception:
            fresh = True
        if fresh:
            todo.append(d)
    todo.sort(key=lambda d: 0 if d.get("store") == "Amazon" else 1)  # Amazon first
    todo = todo[:MAX_PER_RUN]
    if not todo:
        print("pictures: nothing new")
        return

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1200, "height": 1200}, device_scale_factor=1, locale="en-US",
                                  timezone_id="America/New_York", user_agent=BROWSER_UA)
        ctx.add_cookies([{"name": n, "value": v, "domain": "store.steampowered.com", "path": "/"} for n, v in
                         [("birthtime", "568022401"), ("lastagecheckage", "1-0-1988"), ("wants_mature_content", "1")]])
        page = ctx.new_page()
        for d in todo:
            name = f"{NOW:%Y%m%d}-{d['id']}.jpg"
            path = os.path.join(IMG_DIR, name)
            if os.path.exists(path):
                os.remove(path)
            try:
                kind = pick_picture(page, d, path)
            except Exception as e:
                print("picture failed:", (d.get("item") or "")[:60], str(e)[:120])
                kind = "needs picture (error)"
            if kind.startswith(("needs picture", "no picture")) or not os.path.exists(path):
                d["picture"] = ""
                d["picture_type"] = kind if kind.startswith(("needs picture", "no picture")) else "needs picture"
            else:
                d["picture"] = REPO_RAW + name
                d["picture_type"] = kind
                d["alt"] = alt_text(d, kind)
                pages = d.pop("_pages", None)
                if pages:
                    store = d.get("store") or host_of(d.get("url"))
                    d["pictures"] = [REPO_RAW + os.path.basename(pp) for pp, _ in pages]
                    d["alts"] = [f"{store} sale, page {i + 1} of {len(pages)}: {c} deals with names and prices"
                                 for i, (_, c) in enumerate(pages)]
            if d.get("store") != "Amazon":  # Amazon posts never get pictures (by rule); everything else must have one
                ok = bool(d.get("picture"))
                if not ok and not d.get("skip"):
                    d["skip"] = "no picture could be made"
                record_picture(d, ok)
            print(f"- {d['picture_type']}: {(d.get('item') or '')[:60]}" + (f"  [{d['skip']}]" if d.get("skip") else ""))
        browser.close()

    with open(DRAFTS_PATH, "w", encoding="utf-8") as f:
        json.dump(drafts, f, indent=1, ensure_ascii=False)
    tips_path = os.path.join(DATA, "tips.json")
    tips = load(tips_path, [])
    known = {t["id"] for t in tips}
    for d in todo:
        if d.get("skip") and d["id"] not in known:
            tips.insert(0, {"id": d["id"], "source": d.get("source", ""), "kind": "skipped: " + d["skip"],
                            "text": d["posts"]["x"][:200], "link": d.get("source_link", ""),
                            "found_at": d.get("found_at", "")})
    with open(tips_path, "w", encoding="utf-8") as f:
        json.dump(tips[:300], f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
