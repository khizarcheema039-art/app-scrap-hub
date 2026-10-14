import os
import sqlite3
import json
import csv
import io
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

# ------------------------------------------------------------------------------
# Paths (portable)
# ------------------------------------------------------------------------------
# Defaults to the folder this file lives in, so it works on your PC
# (C:\Users\Khizar\Desktop\App Scrap), on Vercel, or anywhere else.
# Override with the APPS_BASE_DIR / APPS_DB environment variables if needed.
BASE_DIR = os.environ.get("APPS_BASE_DIR") or os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("APPS_DB") or os.path.join(BASE_DIR, "apps.db")

CSV_HEADER = [
    "ID", "App Title", "Platform", "Category", "Developer",
    "Confidence Score (%)", "Requires Phone Verification",
    "Detection Reasons", "Store App ID", "Icon URL", "Scraped At",
]


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_FILE)


def init_db():
    """Creates the SQLite table and indexes (apps.db next to this file by default)."""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scraped_apps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            platform TEXT NOT NULL,
            category TEXT NOT NULL,
            developer TEXT,
            icon_url TEXT,
            confidence_score INTEGER NOT NULL,
            requires_phone BOOLEAN NOT NULL,
            reasons TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Indexes for fast querying across 7,000+ apps
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_platform ON scraped_apps(platform)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_category ON scraped_apps(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_requires_phone ON scraped_apps(requires_phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_score ON scraped_apps(confidence_score)")

    conn.commit()
    conn.close()


def save_app(app_data: Dict[str, Any]) -> bool:
    """Inserts or updates an app in the database with strict null-safety checks."""
    app_id = str(app_data.get("app_id") or "").strip()
    if not app_id:
        return False

    # Prevent NOT NULL constraint failures if stores return None for title
    raw_title = app_data.get("title")
    title = str(raw_title).strip() if raw_title else app_id

    platform = str(app_data.get("platform") or "Unknown").strip()
    category = str(app_data.get("category") or "General").strip()
    developer = str(app_data.get("developer") or "Unknown Developer").strip()
    icon = str(app_data.get("icon") or "").strip()
    score = int(app_data.get("confidence_score") or 0)
    requires_phone = bool(app_data.get("requires_phone", False))
    reasons = json.dumps(app_data.get("reasons") or [])

    conn = _connect()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO scraped_apps (
                app_id, title, platform, category, developer, icon_url,
                confidence_score, requires_phone, reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(app_id) DO UPDATE SET
                title = excluded.title,
                category = excluded.category,
                developer = excluded.developer,
                icon_url = excluded.icon_url,
                confidence_score = excluded.confidence_score,
                requires_phone = excluded.requires_phone,
                reasons = excluded.reasons
        """, (app_id, title, platform, category, developer, icon, score, requires_phone, reasons))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB Error] Could not save {app_id}: {e}")
        return False
    finally:
        conn.close()


def get_stats() -> Dict[str, Any]:
    """Returns database summary metrics and breakdown by category and platform."""
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM scraped_apps")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM scraped_apps WHERE requires_phone = 1")
    requires_phone = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM scraped_apps WHERE platform = 'Apple App Store'")
    ios_total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM scraped_apps WHERE platform = 'Google Play'")
    android_total = cursor.fetchone()[0]

    cursor.execute("""
        SELECT platform, category, COUNT(*), SUM(CASE WHEN requires_phone = 1 THEN 1 ELSE 0 END)
        FROM scraped_apps
        GROUP BY platform, category
        ORDER BY platform, category
    """)
    category_breakdown = cursor.fetchall()
    conn.close()

    percent = round((requires_phone / total * 100), 1) if total > 0 else 0

    return {
        "total": total,
        "requires_phone": requires_phone,
        "requires_phone_percentage": f"{percent}%",
        "ios_total": ios_total,
        "android_total": android_total,
        "breakdown": [
            {
                "platform": r[0],
                "category": r[1],
                "count": r[2],
                "phone_required_count": r[3],
            } for r in category_breakdown
        ],
    }


# ------------------------------------------------------------------------------
# CSV export (shared logic)
# ------------------------------------------------------------------------------
def _fetch_rows(platform: Optional[str], category: Optional[str],
                requires_phone: Optional[bool]) -> List[Tuple]:
    query = ("SELECT id, title, platform, category, developer, confidence_score, "
             "requires_phone, reasons, app_id, icon_url, scraped_at "
             "FROM scraped_apps WHERE 1=1")
    params: List[Any] = []

    if platform:
        query += " AND platform = ?"
        params.append(platform)
    if category:
        query += " AND category = ?"
        params.append(category)
    if requires_phone is not None:
        query += " AND requires_phone = ?"
        params.append(1 if requires_phone else 0)

    query += " ORDER BY confidence_score DESC, id DESC"

    conn = _connect()
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _reasons_to_text(raw: Any) -> str:
    try:
        parsed = json.loads(raw)
        return " | ".join(parsed) if isinstance(parsed, list) else str(parsed)
    except Exception:
        return str(raw)


def _write_csv(fileobj, rows: List[Tuple]) -> None:
    writer = csv.writer(fileobj)
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow([
            row[0], row[1], row[2], row[3], row[4], row[5],
            "YES" if row[6] else "NO",
            _reasons_to_text(row[7]), row[8], row[9], row[10],
        ])


def export_to_csv_file(platform: Optional[str] = None, category: Optional[str] = None,
                       requires_phone: Optional[bool] = None) -> str:
    """
    Exports filtered apps to a .csv file saved in BASE_DIR.
    Returns the absolute path to the generated CSV file.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cat_tag = category.replace(" ", "_") if category else "all_categories"
    plat_tag = ("ios" if platform == "Apple App Store"
                else "android" if platform == "Google Play"
                else "all_platforms")

    csv_filepath = os.path.join(BASE_DIR, f"scraped_apps_{plat_tag}_{cat_tag}_{timestamp}.csv")
    rows = _fetch_rows(platform, category, requires_phone)

    with open(csv_filepath, mode="w", newline="", encoding="utf-8") as f:
        _write_csv(f, rows)

    return csv_filepath


def export_to_csv_string(platform: Optional[str] = None, category: Optional[str] = None,
                         requires_phone: Optional[bool] = None) -> str:
    """Exports filtered apps into an in-memory CSV string for browser downloads."""
    rows = _fetch_rows(platform, category, requires_phone)
    output = io.StringIO()
    _write_csv(output, rows)
    return output.getvalue()


if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("[Database] Initialized successfully.")
    print(f"[Database] SQLite file location: {DB_FILE}")
    print("=" * 60)