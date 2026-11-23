"""Nightly report for the daily review (runs on GitHub; the review itself only reads the result).

- What went out today (per app) and any Buffer errors
- Every held draft (Deal ended / Out of stock / Couldn't verify / Price went up) re-checked live, with all fallbacks
- A list of post IDs that are clearly dead (safe to delete), written to data/daily_report.json and .md
Nothing is deleted or changed here.
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
DAY = NOW.astimezone(LOCAL).date()
ORG = vs.ORG
HOLD_TAGS = {"Deal ended", "Out of stock", "Couldn't verify", "Price went up"}
Q = """query($input: PostsInput!, $after: String) { posts(first: 100, after: $after, input: $input) {
  edges { node { id text status channelService dueAt sentAt error { message } tags { name } } }
  pageInfo { hasNextPage endCursor } } }"""


def posts(key, flt):
    out, after = [], None
    for _ in range(5):
        data = pb.gql(key, Q, {"input": {"organizationId": ORG, "filter": flt}, "after": after})["posts"]
        out += [e["node"] for e in data["edges"]]
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return out


def main():
    key = os.environ.get("BUFFER_API_KEY", "").strip()
    start = datetime.combine(DAY, datetime.min.time(), LOCAL).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    span = {"start": start.isoformat(), "end": end.isoformat()}
    report = {"date": DAY.isoformat(), "made_at": NOW.isoformat(timespec="seconds"), "sent": {}, "errors": [],
              "held": [], "delete_post_ids": [], "pipeline": []}

    for p in posts(key, {"status": ["sent"], "dueAt": span}):
        report["sent"][p["channelService"]] = report["sent"].get(p["channelService"], 0) + 1
    for p in posts(key, {"status": ["error"]}):
        report["errors"].append({"app": p["channelService"], "text": (p.get("text") or "")[:90],
                                 "error": (p.get("error") or {}).get("message", "")})

    sent = pb.load(pb.SENT_PATH, {})
    drafts = {d["id"]: d for d in pb.load(pb.DRAFTS_PATH, [])}
    post_to_deal = {pid: did for did, v in sent.items() for pid in (v.get("posts") or {}).values()}
    held = {}
    for p in posts(key, {"status": ["draft"]}):
        tags = {t["name"] for t in p.get("tags") or []}
        if tags & HOLD_TAGS and post_to_deal.get(p["id"]):
            h = held.setdefault(post_to_deal[p["id"]], {"tags": set(), "posts": []})
            h["tags"] |= tags & HOLD_TAGS
            h["posts"].append(p["id"])

    browser = vs.Browser()
    page_getter = browser.get

    try:
        for did, h in held.items():
            d = drafts.get(did)
            if not d:
                continue
            state, live, detail = vs.check_deal(d, page_getter)  # includes every fallback (camelcamelcamel, etc.)
            claimed = vs.num(d.get("price"))
            if state in ("ended", "out_of_stock"):
                verdict, why = "dead", "deal ended" if state == "ended" else "out of stock (restock watch still on)"
            elif state == "live" and claimed and live and live > claimed * 1.05:
                verdict, why = "dead", f"price now ${live:.2f}, more than 5% above the deal's ${claimed:.2f}"
            elif state == "unknown" and "Couldn't verify" in h["tags"]:
                verdict, why = "dead", f"failed to verify twice, all fallbacks tried ({detail})"
            elif state == "live":
                verdict, why = "alive", f"live at ${live:.2f}" + (f" (post says ${claimed:.2f})" if claimed and abs(live - claimed) > 0.009 else "")
            else:
                verdict, why = "check", detail or "couldn't confirm"
            report["held"].append({"item": d.get("item") or d["posts"]["x"][:60], "store": d.get("store", ""),
                                   "tags": sorted(h["tags"]), "verdict": verdict, "why": why, "posts": h["posts"],
                                   "check": pb.camel_link(d)})
            if verdict == "dead":
                report["delete_post_ids"] += h["posts"]
    finally:
        browser.close()

    st = pb.load(os.path.join(pb.DATA, "status.json"), {})
    report["pipeline"] = [f"{f['name']}: {f['error'][:100]}" for f in st.get("feeds", []) if f.get("error")]
    # the day's mix: Amazon vs other stores vs news (drafts made that day); flagged if one side took over
    mix = {"Amazon": 0, "other stores": 0, "news": 0}
    for did, v in sent.items():
        if "posts" not in v:
            continue
        try:
            if datetime.fromisoformat(v["at"]).astimezone(LOCAL).date() != DAY:
                continue
        except Exception:
            continue
        d = drafts.get(did) or {}
        side = "news" if d.get("kind") == "news" else ("Amazon" if d.get("store") == "Amazon" else "other stores")
        mix[side] += 1
    report["mix"] = mix
    total = sum(mix.values())
    if total >= 8 and (mix["Amazon"] > total * 0.65 or mix["Amazon"] < total * 0.35):
        report["pipeline"].append(f"mix out of balance: {mix['Amazon']} Amazon, {mix['other stores']} other stores, "
                                  f"{mix['news']} news (aim is about half and half)")
    try:  # how the Amazon prices were checked that day: live on Amazon, by a backup, or not confirmed
        report["amazon_checks"] = pb.load(os.path.join(pb.DATA, "amazon_checks.json"), {}).get(DAY.isoformat(), {})
    except Exception:
        report["amazon_checks"] = {}
    pb.save(os.path.join(pb.DATA, "daily_report.json"), report)

    lines = [f"# Daily report {DAY:%b %-d, %Y}", "",
             "Posted: " + (", ".join(f"{k} {v}" for k, v in sorted(report["sent"].items())) or "nothing"),
             f"Buffer errors: {len(report['errors'])}", ""]
    for e in report["errors"]:
        lines.append(f"- {e['app']}: {e['text']} ({e['error']})")
    lines += ["", "## Held drafts", ""]
    for h in report["held"]:
        lines.append(f"- [{h['verdict'].upper()}] {h['item']} ({h['store']}) - {', '.join(h['tags'])}: {h['why']}"
                     + (f" - {h['check']}" if h["check"] else ""))
    if report["pipeline"]:
        lines += ["", "## Source problems", ""] + [f"- {x}" for x in report["pipeline"]]
    with open(os.path.join(pb.DATA, "daily_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
