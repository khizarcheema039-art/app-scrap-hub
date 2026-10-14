"""
App Store + Google Play scraper: finds apps that REQUIRE phone-number
verification / phone-number login, in the 7 target categories, and stores
them in C:\\Users\\Khizar\\Desktop\\App Scrap\\apps.db (via your database.py).

Install once:
    pip install requests google-play-scraper

Run (fresh start, backs up the old apps.db first):
    python scraper.py --fresh

Other examples:
    python scraper.py                              # resume, 100 per category
    python scraper.py --per-category 10            # quick test run
    python scraper.py --stores ios                 # App Store only
    python scraper.py --categories Social Finance  # only some categories
    python scraper.py --no-reviews                 # faster, less accurate
"""
import os
import sys
import re
import time
import sqlite3
import argparse
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple, Iterator

import requests

BASE_DIR = r"C:\Users\Khizar\Desktop\App Scrap"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database  # your unchanged database.py
from google_play_scraper import (
    app as play_app,
    permissions as play_permissions,
    search as play_search,
    reviews as play_reviews,
    Sort,
)

# ==============================================================================
# SETTINGS
# ==============================================================================
CATEGORIES = ["Social", "Finance", "Food & Drink", "Shopping",
              "Travel", "Lifestyle", "Productivity"]

PHONE_THRESHOLD = 50          # score (0-100) needed to count as "requires phone"
WORKERS = 5                   # parallel detail fetches (lower if you get blocked)
REVIEW_CHECK = True           # read recent user reviews when description is unclear

# Countries used for App Store top charts / lookups
IOS_COUNTRIES = ["us", "gb", "ca", "au", "in", "pk", "ae", "sg",
                 "sa", "id", "br", "mx", "tr", "ng", "za", "eg"]
IOS_SEARCH_COUNTRIES = 6      # keyword search uses the first N of IOS_COUNTRIES
PLAY_COUNTRIES = ["us", "gb", "in", "pk", "id", "br", "mx", "tr", "ng", "za", "eg", "ph"]
SEARCH_MODIFIERS = ["", "sign up with phone number", "login with mobile number",
                    "otp verification"]

CACHE_DB = os.path.join(BASE_DIR, "checked_cache.db")   # remembers apps already evaluated

# App Store genre ids -> the app's OWN primary genre is checked against these
IOS_GENRES = {
    "Social": "6005", "Finance": "6015", "Food & Drink": "6023",
    "Shopping": "6024", "Travel": "6003", "Lifestyle": "6012",
    "Productivity": "6007",
}

# Google Play's own genreId -> our category (the app's real store category)
PLAY_GENRE_MAP = {
    "SOCIAL": "Social", "COMMUNICATION": "Social",
    "FINANCE": "Finance",
    "FOOD_AND_DRINK": "Food & Drink",
    "SHOPPING": "Shopping",
    "TRAVEL_AND_LOCAL": "Travel",
    "LIFESTYLE": "Lifestyle", "DATING": "Lifestyle",
    "PRODUCTIVITY": "Productivity",
}

# Search seeds only DISCOVER apps; the category is verified from store metadata.
CATEGORY_SEEDS = {
    "Social": ["messenger", "chat app", "text messaging", "video call", "social network",
               "voice chat", "group chat", "live stream", "make friends", "private messenger",
               "second phone number", "calling app", "community app", "anonymous chat",
               "random video chat", "sms app", "social media", "stickers chat",
               "short video", "status saver", "walkie talkie", "chat rooms"],
    "Finance": ["mobile banking", "digital wallet", "crypto exchange", "money transfer",
                "send money abroad", "p2p payments", "stock trading", "cash advance",
                "buy now pay later", "credit card app", "loan app", "bill payment",
                "upi payments", "neobank", "remittance", "insurance app", "bitcoin wallet",
                "budget tracker", "mobile recharge", "mutual funds", "personal loan",
                "mobile money", "savings app"],
    "Food & Drink": ["food delivery", "restaurant order", "grocery delivery", "takeaway",
                     "pizza delivery", "coffee order rewards", "fast food app", "meal kit",
                     "table reservation", "online grocery", "quick commerce", "burger order",
                     "restaurant rewards", "recipe app", "food ordering", "liquor delivery",
                     "bakery order", "dinner delivery", "cloud kitchen", "supermarket",
                     "fresh vegetables delivery", "meat delivery"],
    "Shopping": ["online shopping", "buy and sell", "marketplace", "ecommerce",
                 "classifieds", "second hand", "deals and coupons", "fashion shopping",
                 "auction app", "cashback shopping", "grocery shopping", "wholesale",
                 "sell used items", "thrift", "sneakers", "electronics shopping",
                 "beauty shopping", "flash sale", "mobile phones shopping", "furniture shopping",
                 "pharmacy online", "jewellery shopping"],
    "Travel": ["flight booking", "cab booking", "taxi app", "ride sharing", "hotel booking",
               "car rental", "train tickets", "bus tickets", "esim travel", "airline app",
               "vacation rental", "scooter rental", "carpool", "tour booking", "parking app",
               "moto taxi", "airport", "cruise", "bike taxi", "intercity ride",
               "travel planner", "metro ticket"],
    "Lifestyle": ["dating app", "meet singles", "social discovery", "horoscope", "astrology",
                  "relationship app", "make new friends", "video dating", "gay dating",
                  "matchmaking", "live chat dating", "couples app", "pregnancy tracker",
                  "daily routine", "wallpaper", "home services", "pet care", "wedding",
                  "marriage bureau", "fitness community", "salon booking", "tarot"],
    "Productivity": ["authenticator", "password manager", "cloud storage", "notes app",
                     "task manager", "document scanner", "pdf editor", "calendar",
                     "email app", "ai chatbot", "vpn", "to do list", "office suite",
                     "e-signature", "file manager", "voice recorder", "translator", "scheduler",
                     "caller id", "contacts manager", "team collaboration", "business card scanner"],
}

# ==============================================================================
# DETECTION (phone-number login / verification)
# ==============================================================================
_I = re.IGNORECASE
_PHONE = r"(?:phone|mobile|cell(?:ular)?|telephone)"
_NUM = rf"{_PHONE}(?:\s*number|\s*no\.?)?"

STRONG_DESC = [re.compile(p, _I) for p in [
    rf"\b(?:sign\s*[- ]?up|sign\s*[- ]?in|log\s*[- ]?in|login|register|registration|create\s+(?:an?\s+)?account|get\s+started)\b[^.\n]{{0,40}}\b(?:with|using|via|by|through|your)\s+(?:your\s+|a\s+|the\s+)?{_NUM}",
    rf"\b{_NUM}\s*(?:login|log\s*[- ]?in|sign\s*[- ]?in|sign\s*[- ]?up|registration|verification|verify|authentication|otp)\b",
    rf"\b(?:verify|verified|verifying|confirm|validate)\b[^.\n]{{0,30}}\b(?:{_NUM}|sms|text message|otp)\b",
    r"\b(?:sms|text\s*message|text)\s*(?:verification|code|otp|confirmation|pin|passcode)\b",
    r"\b(?:otp|one[- ]time\s*(?:password|passcode|pin|code))\b",
    r"\b(?:verification|confirmation|security|login)\s*code\b[^.\n]{0,30}\b(?:sms|text|phone|mobile|number)\b",
    rf"\b(?:enter|provide|add|link|register|use|confirm)\s+(?:your\s+|a\s+|the\s+)?(?:valid\s+|active\s+|working\s+)?{_NUM}\b",
    rf"\b{_NUM}\s*(?:is\s+)?(?:required|mandatory|needed|necessary)\b",
    rf"\b(?:requires?|need|needs)\s+(?:a\s+|your\s+)?(?:valid\s+)?{_NUM}\b",
    rf"\baccount\s+(?:is\s+)?(?:linked|tied|connected|associated|registered)\s+(?:to|with)\s+(?:your\s+)?(?:{_NUM}|sim)\b",
    r"\b(?:two|2)[- ]?(?:factor|step)\s*(?:authentication|verification)\b[^.\n]{0,25}\b(?:sms|text|phone)\b",
    rf"\buses?\s+your\s+{_NUM}\b",
]]
WEAK_DESC = re.compile(rf"\b{_NUM}\b", _I)

REVIEW_PATTERNS = [re.compile(p, _I) for p in [
    rf"\b(?:ask(?:s|ed|ing)?|requires?|required|forc(?:e|es|ed|ing)|need(?:s|ed)?|want(?:s|ed)?|demand(?:s|ed)?|must\s+(?:enter|give|provide))\b[^.\n]{{0,30}}\b{_NUM}\b",
    r"\b(?:verification|confirmation|sms|security)\s*code\b",
    r"\botp\b",
    rf"\b(?:verify|verification)\b[^.\n]{{0,25}}\b{_PHONE}\b",
    r"\b(?:can'?t|cannot|never|not|didn'?t|don'?t)\s+(?:get|got|receive|received|receiving)\b[^.\n]{0,25}\b(?:code|otp|sms)\b",
    rf"\b(?:sign\s*[- ]?up|register|log\s*[- ]?in|login)\b[^.\n]{{0,30}}\b{_PHONE}\b",
]]

# Apps that are universally phone-number-first (small, explicit list)
KNOWN_PHONE_FIRST = re.compile(
    r"\b(whatsapp|telegram|viber|kakaotalk|wechat|zalo|textnow|text free|textplus|"
    r"talkatone|truecaller|botim|zangi)\b|^signal\b[^a-z]*(?:private|messenger)", _I)


def analyze(title: str, description: str, extra: List[Tuple[int, str]],
            review_texts: Optional[List[str]] = None) -> Tuple[int, List[str]]:
    """Returns (score 0-100, reasons)."""
    score, reasons = 0, []
    desc = description or ""

    hits = []
    for rx in STRONG_DESC:
        m = rx.search(desc)
        if m:
            hits.append(re.sub(r"\s+", " ", m.group(0)).strip()[:70])
    if hits:
        score += min(50 + 10 * (len(hits) - 1), 80)
        reasons.append(f"Description has {len(hits)} phone login/verification phrase(s), e.g. '{hits[0]}'")
    elif WEAK_DESC.search(desc):
        score += 10
        reasons.append("Description mentions a phone/mobile number")

    if KNOWN_PHONE_FIRST.search(title or ""):
        score += 50
        reasons.append("Known phone-number-first app")

    for pts, why in extra:
        score += pts
        reasons.append(why)

    if review_texts:
        n = sum(1 for t in review_texts if any(rx.search(t) for rx in REVIEW_PATTERNS))
        if n >= 3:
            score += 50
            reasons.append(f"{n} recent reviews mention phone number / OTP / SMS code")
        elif n == 2:
            score += 30
            reasons.append("2 recent reviews mention phone number / OTP / SMS code")
        elif n == 1:
            score += 15
            reasons.append("1 recent review mentions phone number / OTP / SMS code")

    return min(score, 100), reasons


def play_permission_signals(perm_dict: Dict[str, List[str]]) -> List[Tuple[int, str]]:
    """google_play_scraper returns HUMAN-READABLE permission text, not RECEIVE_SMS."""
    flat = [p.lower() for lst in (perm_dict or {}).values() for p in lst]
    out = []
    if any("receive text message" in p or "read your text message" in p or "receive sms" in p
           or "read_sms" in p or "receive_sms" in p for p in flat):
        out.append((30, "Declares SMS read/receive permission (OTP auto-read)"))
    if any("read phone number" in p or "your phone number" in p or "read_phone_numbers" in p
           for p in flat):
        out.append((20, "Declares permission to read the phone number"))
    if any("phone status and identity" in p or "read_phone_state" in p for p in flat):
        out.append((5, "Declares phone state permission"))
    return out


# ==============================================================================
# HELPERS: rate limiting, HTTP, DB lookups, cache, progress
# ==============================================================================
class RateLimiter:
    def __init__(self, interval: float):
        self.interval, self.lock, self.next = interval, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            delay = max(0.0, self.next - now)
            self.next = max(now, self.next) + self.interval
        if delay:
            time.sleep(delay)


ITUNES_SEARCH_RL = RateLimiter(3.2)   # iTunes Search allows ~20 calls/min
ITUNES_API_RL = RateLimiter(0.3)
PLAY_SEARCH_RL = RateLimiter(1.0)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})


def get_json(url: str, params: Optional[dict], limiter: RateLimiter, retries: int = 3):
    for attempt in range(retries):
        limiter.wait()
        try:
            r = SESSION.get(url, params=params, timeout=20)
            if r.status_code in (403, 429, 503):
                time.sleep(6 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def db_saved_ids() -> set:
    conn = sqlite3.connect(database.DB_FILE)
    try:
        return {r[0] for r in conn.execute("SELECT app_id FROM scraped_apps WHERE requires_phone = 1")}
    finally:
        conn.close()


def db_count(platform: str, category: str) -> int:
    conn = sqlite3.connect(database.DB_FILE)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM scraped_apps WHERE platform=? AND category=? AND requires_phone=1",
            (platform, category)).fetchone()[0]
    finally:
        conn.close()


# --- "already evaluated" cache, so resuming a run does not re-check old apps ---
def cache_init():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS checked (
                        platform TEXT NOT NULL, category TEXT NOT NULL, app_id TEXT NOT NULL,
                        reviews INTEGER NOT NULL,
                        PRIMARY KEY (platform, category, app_id))""")
    conn.commit()
    conn.close()


def cache_ids(platform: str, category: str) -> set:
    conn = sqlite3.connect(CACHE_DB)
    try:
        # if reviews are enabled, only skip apps that were checked WITH reviews
        sql = "SELECT app_id FROM checked WHERE platform=? AND category=?"
        if REVIEW_CHECK:
            sql += " AND reviews=1"
        return {r[0] for r in conn.execute(sql, (platform, category))}
    finally:
        conn.close()


def cache_mark(platform: str, category: str, app_ids: List[str]):
    if not app_ids:
        return
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO checked (platform, category, app_id, reviews) VALUES (?,?,?,?)",
            [(platform, category, a, 1 if REVIEW_CHECK else 0) for a in app_ids])
        conn.commit()
    finally:
        conn.close()


class Progress:
    def __init__(self, platform: str, category: str, target: int):
        self.platform, self.category, self.target = platform, category, target
        self.saved = db_count(platform, category)
        self.checked = 0

    def done(self) -> bool:
        return self.saved >= self.target

    def line(self):
        print(f"   [{self.platform} | {self.category}] qualifying saved: {self.saved}/{self.target}"
              f"  (apps checked this run: {self.checked})", flush=True)


def run_batch(candidates: list, evaluator, state: Progress, key):
    """Evaluate candidates in parallel, save qualifying apps from the main thread.

    evaluator returns a record dict (qualifies), None (definitely not), or raises
    (temporary error -> not cached, will be retried on the next run).
    """
    if not candidates or state.done():
        return
    checked_ok: List[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(evaluator, c): c for c in candidates}
        for f in as_completed(futs):
            state.checked += 1
            try:
                rec = f.result()
            except Exception:
                continue                       # transient failure: do not cache
            checked_ok.append(key(futs[f]))
            if rec and not state.done():
                if database.save_app(rec):
                    state.saved += 1
                    if state.saved % 10 == 0:
                        state.line()
            if state.done():
                for g in futs:
                    g.cancel()
                break
    cache_mark(state.platform, state.category, checked_ok)


# ==============================================================================
# APPLE APP STORE
# ==============================================================================
def ios_reviews(track_id: str, cc: str) -> List[str]:
    url = f"https://itunes.apple.com/{cc}/rss/customerreviews/page=1/id={track_id}/sortby=mostrecent/json"
    data = get_json(url, None, ITUNES_API_RL, retries=2)
    if not data:
        return []
    entries = data.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    texts = []
    for e in entries:
        body = (e.get("content") or {}).get("label")
        if body:
            title = (e.get("title") or {}).get("label", "")
            texts.append(f"{title}. {body}")
    return texts


def ios_candidate_batches(cat: str, seen: set) -> Iterator[List[Tuple[dict, str]]]:
    genre = IOS_GENRES[cat]

    # 1) Top charts of this genre in several countries (already category-specific)
    for cc in IOS_COUNTRIES:
        ids: List[str] = []
        for feed in ("topfreeapplications/limit=200", "topgrossingapplications/limit=200",
                     "toppaidapplications/limit=100"):
            url = f"https://itunes.apple.com/{cc}/rss/{feed}/genre={genre}/json"
            data = get_json(url, None, ITUNES_API_RL, retries=2) or {}
            entries = data.get("feed", {}).get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            for e in entries:
                tid = (e.get("id") or {}).get("attributes", {}).get("im:id")
                if tid and tid not in seen:
                    ids.append(tid)
                    seen.add(tid)
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            data = get_json("https://itunes.apple.com/lookup",
                            {"id": ",".join(chunk), "country": cc, "entity": "software"},
                            ITUNES_API_RL) or {}
            items = [(it, cc) for it in data.get("results", []) if it.get("trackId")]
            if items:
                yield items

    # 2) Keyword search (phone-oriented modifiers); category verified by primaryGenreId
    for cc in IOS_COUNTRIES[:IOS_SEARCH_COUNTRIES]:
        for seed in CATEGORY_SEEDS[cat]:
            for mod in SEARCH_MODIFIERS:
                term = f"{seed} {mod}".strip()
                data = get_json("https://itunes.apple.com/search",
                                {"term": term, "country": cc, "media": "software",
                                 "entity": "software", "limit": 200}, ITUNES_SEARCH_RL) or {}
                items = []
                for it in data.get("results", []):
                    tid = str(it.get("trackId") or "")
                    if tid and tid not in seen:
                        seen.add(tid)
                        items.append((it, cc))
                if items:
                    yield items


def scrape_ios_category(cat: str, target: int):
    platform = "Apple App Store"
    state = Progress(platform, cat, target)
    print(f"\n[App Store] '{cat}' - target {target} phone-verification apps "
          f"(already in DB: {state.saved})")
    if state.done():
        return
    genre = IOS_GENRES[cat]
    seen = set(db_saved_ids()) | cache_ids(platform, cat)

    def evaluator(args):
        item, cc = args
        if str(item.get("primaryGenreId")) != genre:          # real category check
            return None
        tid = str(item.get("trackId"))
        title = item.get("trackName") or tid
        score, reasons = analyze(title, item.get("description", ""), [])
        if score < PHONE_THRESHOLD and REVIEW_CHECK:
            score, reasons = analyze(title, item.get("description", ""), [],
                                     ios_reviews(tid, cc))
        if score < PHONE_THRESHOLD:
            return None
        return {"title": title, "app_id": tid, "platform": platform, "category": cat,
                "developer": item.get("artistName"), "icon": item.get("artworkUrl100"),
                "confidence_score": score, "requires_phone": True, "reasons": reasons}

    key = lambda c: str(c[0].get("trackId"))
    for batch in ios_candidate_batches(cat, seen):
        run_batch(batch, evaluator, state, key)
        if state.done():
            break
    state.line()
    if not state.done():
        print(f"  ! Pool exhausted: only {state.saved}/{target} qualifying apps found.")


# ==============================================================================
# GOOGLE PLAY
# ==============================================================================
def play_candidate_batches(cat: str, seen: set) -> Iterator[List[Tuple[str, str]]]:
    for cc in PLAY_COUNTRIES:
        for seed in CATEGORY_SEEDS[cat]:
            for mod in SEARCH_MODIFIERS:
                term = f"{seed} {mod}".strip()
                PLAY_SEARCH_RL.wait()
                try:
                    hits = play_search(term, lang="en", country=cc, n_hits=100)
                except Exception as e:
                    print(f"   [warn] search '{term}' ({cc}) failed: {e}")
                    time.sleep(3)
                    continue
                batch = []
                for h in hits:
                    aid = h.get("appId")
                    if not aid or aid in seen:
                        continue
                    gid = h.get("genreId")                      # skip early if search exposes it
                    if gid and PLAY_GENRE_MAP.get(gid) != cat:
                        continue
                    seen.add(aid)
                    batch.append((aid, cc))
                if batch:
                    yield batch


def scrape_android_category(cat: str, target: int):
    platform = "Google Play"
    state = Progress(platform, cat, target)
    print(f"\n[Google Play] '{cat}' - target {target} phone-verification apps "
          f"(already in DB: {state.saved})")
    if state.done():
        return
    seen = set(db_saved_ids()) | cache_ids(platform, cat)

    def evaluator(args):
        app_id, cc = args
        d = play_app(app_id, lang="en", country=cc)             # raises on temp error -> retried later
        if PLAY_GENRE_MAP.get(d.get("genreId")) != cat:         # real category check
            return None
        title = d.get("title") or app_id
        desc = d.get("description") or ""
        try:
            extra = play_permission_signals(play_permissions(app_id, lang="en", country=cc))
        except Exception:
            extra = []
        score, reasons = analyze(title, desc, extra)
        if score < PHONE_THRESHOLD and REVIEW_CHECK:
            try:
                revs, _ = play_reviews(app_id, lang="en", country=cc,
                                       sort=Sort.NEWEST, count=100)
                texts = [r.get("content") or "" for r in revs]
            except Exception:
                texts = []
            score, reasons = analyze(title, desc, extra, texts)
        time.sleep(0.2)
        if score < PHONE_THRESHOLD:
            return None
        return {"title": title, "app_id": app_id, "platform": platform, "category": cat,
                "developer": d.get("developer"), "icon": d.get("icon"),
                "confidence_score": score, "requires_phone": True, "reasons": reasons}

    key = lambda c: c[0]
    for batch in play_candidate_batches(cat, seen):
        run_batch(batch, evaluator, state, key)
        if state.done():
            break
    state.line()
    if not state.done():
        print(f"  ! Pool exhausted: only {state.saved}/{target} qualifying apps found.")


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    global WORKERS, REVIEW_CHECK
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-category", type=int, default=100,
                    help="phone-verification apps wanted per category per store")
    ap.add_argument("--stores", choices=["both", "ios", "android"], default="both")
    ap.add_argument("--categories", nargs="*", default=CATEGORIES)
    ap.add_argument("--fresh", action="store_true", help="back up and start a new apps.db")
    ap.add_argument("--no-reviews", action="store_true")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args()

    WORKERS, REVIEW_CHECK = args.workers, not args.no_reviews

    if args.fresh:
        if os.path.exists(database.DB_FILE):
            backup = database.DB_FILE.replace(
                ".db", f"_backup_{datetime.now():%Y%m%d_%H%M%S}.db")
            os.replace(database.DB_FILE, backup)
            print(f"Old database backed up to: {backup}")
        if os.path.exists(CACHE_DB):
            os.remove(CACHE_DB)
    database.init_db()
    cache_init()

    cats = [c for c in args.categories if c in CATEGORIES]
    print("=" * 66)
    print(f"Target: {args.per_category} phone-verification apps x {len(cats)} categories "
          f"x {'2 stores' if args.stores == 'both' else '1 store'}")
    print(f"Review check: {'ON' if REVIEW_CHECK else 'OFF'} | workers: {WORKERS}")
    print("=" * 66)

    try:
        for cat in cats:
            if args.stores in ("both", "ios"):
                scrape_ios_category(cat, args.per_category)
            if args.stores in ("both", "android"):
                scrape_android_category(cat, args.per_category)
    except KeyboardInterrupt:
        print("\nStopped by user - progress is saved; run again to resume.")

    csv_file = database.export_to_csv_file(requires_phone=True)
    stats = database.get_stats()
    print("\n" + "=" * 66)
    print(f"Apps in database      : {stats['total']}")
    print(f"Requiring phone number: {stats['requires_phone']}")
    for row in stats["breakdown"]:
        print(f"  {row['platform']:<16} {row['category']:<13} {row['phone_required_count']}")
    print(f"CSV export            : {csv_file}")
    print("=" * 66)


if __name__ == "__main__":
    main()