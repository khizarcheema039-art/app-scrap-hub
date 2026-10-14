"""
server.py - backend for the Phone Verification Intelligence & Export Hub.

Project layout (same folder as apps.db):
    server.py
    apps.db
    requirements.txt
    public/index.html      <- the frontend (renamed from hub.html)

Install once:
    python -m pip install flask

Run locally:
    python server.py
Then open http://127.0.0.1:5000/

Deploys on Vercel as-is (Flask is auto-detected; static files come from public/).

Endpoints
    GET /api/health
    GET /api/categories
    GET /api/apps?category=Social&limit=50[&platform=Google Play][&verification=phone|all|none]
    GET /api/download?...same parameters...   -> CSV file

Parameters
    category     : one of the 7 categories (required)
    limit        : integer 1-100 (required)
    platform     : optional, "Apple App Store" or "Google Play" (omit for both)
    verification : optional, "phone" (default) = requires phone only,
                   "all" = every app, "none" = no phone required
"""
import os
import io
import csv
import json
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, request, Response, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))

# apps.db lives next to this file. Optional override via the APPS_DB env variable.
DB_FILE = os.environ.get("APPS_DB") or os.path.join(HERE, "apps.db")

CATEGORIES = ["Social", "Finance", "Food & Drink", "Shopping",
              "Travel", "Lifestyle", "Productivity"]
PLATFORMS = ["Apple App Store", "Google Play"]
VERIFICATION = {"phone": 1, "none": 0, "all": None}
MIN_LIMIT, MAX_LIMIT = 1, 100

PUBLIC_DIR = os.path.join(HERE, "public")

# Frontend files are served from public/ (Vercel's CDN does this in production).
app = Flask(__name__, static_folder=PUBLIC_DIR, static_url_path="")


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def get_conn() -> sqlite3.Connection:
    """Opens apps.db read-only (Vercel's filesystem is read-only)."""
    uri = Path(DB_FILE).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class BadRequest(Exception):
    pass


def parse_params():
    category = (request.args.get("category") or "").strip()
    if category not in CATEGORIES:
        raise BadRequest(f"Invalid category. Choose one of: {', '.join(CATEGORIES)}")

    raw_limit = (request.args.get("limit") or "").strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        raise BadRequest(f"'limit' must be a whole number between {MIN_LIMIT} and {MAX_LIMIT}.")
    if not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise BadRequest(f"'limit' must be between {MIN_LIMIT} and {MAX_LIMIT}.")

    platform = (request.args.get("platform") or "").strip() or None
    if platform and platform not in PLATFORMS:
        raise BadRequest(f"Invalid platform. Choose one of: {', '.join(PLATFORMS)} (or omit it).")

    verification = (request.args.get("verification") or "phone").strip().lower()
    if verification not in VERIFICATION:
        raise BadRequest("Invalid verification filter. Use 'phone', 'all' or 'none'.")

    return category, limit, platform, verification


def fetch_apps(category, limit, platform=None, verification="phone"):
    """Returns (apps, available): the top `limit` apps and how many match in total."""
    where = "category = ?"
    params = [category]
    if platform:
        where += " AND platform = ?"
        params.append(platform)
    flag = VERIFICATION[verification]
    if flag is not None:
        where += " AND requires_phone = ?"
        params.append(flag)

    conn = get_conn()
    try:
        available = conn.execute(f"SELECT COUNT(*) FROM scraped_apps WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT id, title, platform, category, developer, confidence_score, requires_phone, "
            f"reasons, app_id, icon_url, scraped_at FROM scraped_apps WHERE {where} "
            "ORDER BY confidence_score DESC, id DESC LIMIT ?",
            params + [limit]).fetchall()
    finally:
        conn.close()

    apps = []
    for r in rows:
        try:
            reasons = json.loads(r["reasons"] or "[]")
            if not isinstance(reasons, list):
                reasons = [str(reasons)]
        except Exception:
            reasons = [str(r["reasons"])]
        a = {
            "id": r["id"],
            "title": r["title"],
            "platform": r["platform"],
            "category": r["category"],
            "developer": r["developer"],
            "confidence_score": r["confidence_score"],
            "requires_phone": bool(r["requires_phone"]),
            "reasons": reasons,
            "app_id": r["app_id"],
            "icon_url": r["icon_url"],
            "scraped_at": r["scraped_at"],
        }
        a["store_url"] = store_link(a)
        apps.append(a)
    return apps, available


def store_link(a: dict) -> str:
    if a["platform"] == "Google Play":
        return f"https://play.google.com/store/apps/details?id={a['app_id']}"
    return f"https://apps.apple.com/app/id{a['app_id']}"


def safe_cell(v):
    """Neutralise spreadsheet formulas (=, +, -, @) in text cells."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


# ------------------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------------------
@app.errorhandler(BadRequest)
def handle_bad_request(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(sqlite3.Error)
def handle_db_error(e):
    return jsonify({"error": f"Database error: {e}"}), 500


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
@app.get("/")
def index():
    # Locally Flask serves the frontend; on Vercel the CDN serves public/index.html
    # before this route is ever reached.
    for name in ("index.html", "hub.html"):
        if os.path.exists(os.path.join(PUBLIC_DIR, name)):
            return send_from_directory(PUBLIC_DIR, name)
    return "Backend is running. Put your frontend in the 'public' folder as index.html. Try /api/categories", 200


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "db": DB_FILE, "db_exists": os.path.exists(DB_FILE)})


@app.get("/api/categories")
def categories():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT category, platform, COUNT(*) AS n FROM scraped_apps "
            "WHERE requires_phone = 1 GROUP BY category, platform").fetchall()
    finally:
        conn.close()

    counts = {c: {"total": 0, "by_platform": {p: 0 for p in PLATFORMS}} for c in CATEGORIES}
    for r in rows:
        if r["category"] in counts:
            counts[r["category"]]["total"] += r["n"]
            counts[r["category"]]["by_platform"][r["platform"]] = r["n"]

    return jsonify({
        "categories": [{"name": c, **counts[c]} for c in CATEGORIES],
        "platforms": PLATFORMS,
        "limit_min": MIN_LIMIT,
        "limit_max": MAX_LIMIT,
    })


@app.get("/api/apps")
def apps_json():
    category, limit, platform, verification = parse_params()
    apps, available = fetch_apps(category, limit, platform, verification)
    return jsonify({
        "category": category,
        "platform": platform or "All",
        "verification": verification,
        "requested": limit,
        "available": available,     # matching apps in the DB (can be < requested)
        "returned": len(apps),
        "apps": apps,
    })


@app.get("/api/download")
def download_csv():
    category, limit, platform, verification = parse_params()
    apps, _ = fetch_apps(category, limit, platform, verification)

    out = io.StringIO()
    w = csv.writer(out, quoting=csv.QUOTE_ALL)
    w.writerow(["ID", "App Title", "Platform", "Category", "Developer",
                "Confidence Score (%)", "Requires Phone Verification (YES/NO)",
                "Detection Reasons", "Store ID", "Scraped Date"])
    for a in apps:
        w.writerow([a["id"], safe_cell(a["title"]), a["platform"], a["category"],
                    safe_cell(a["developer"]), a["confidence_score"],
                    "YES" if a["requires_phone"] else "NO",
                    safe_cell(" | ".join(a["reasons"])), a["app_id"], a["scraped_at"]])

    cat_tag = category.replace(" & ", "_and_").replace(" ", "_")
    filename = f"phone_verification_apps_{cat_tag}_{len(apps)}.csv"
    return Response(
        out.getvalue().encode("utf-8-sig"),   # BOM so Excel reads non-English names correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    print(f"[server] Using database: {DB_FILE}")
    if not os.path.exists(DB_FILE):
        print("[server] WARNING: database file not found - run chart_crawler.py first.")
    app.run(host="127.0.0.1", port=5000, debug=True)