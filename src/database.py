"""
SQLite persistence layer for freight quotes.

Design: stores the full quote dict as a JSON blob in the `data` column,
with indexed metadata columns for fast filtering without JSON parsing.
This keeps all existing quote dict structure untouched.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

DEFAULT_DB_PATH = "freight_quotes.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id             TEXT UNIQUE NOT NULL,
    customer_id          TEXT NOT NULL,
    status               TEXT NOT NULL,
    origin               TEXT,
    destination          TEXT,
    weight_lbs           REAL,
    cargo_type           TEXT,
    pickup_date          TEXT,
    sell_rate            REAL,
    carrier_cost         REAL,
    markup_pct           REAL,
    gross_profit         REAL,
    winning_carrier_id   TEXT,
    winning_carrier_name TEXT,
    source               TEXT NOT NULL DEFAULT 'ui',
    data                 TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_status      ON quotes(status);
CREATE INDEX IF NOT EXISTS idx_quotes_customer_id ON quotes(customer_id);
CREATE INDEX IF NOT EXISTS idx_quotes_created_at  ON quotes(created_at);

CREATE TABLE IF NOT EXISTS customers (
    customer_id          TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    markup_pct           REAL NOT NULL,
    preferred_carriers   TEXT,        -- JSON array of carrier IDs, NULL = any carrier
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject      TEXT,
    sender       TEXT,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    parse_result TEXT,
    quote_id     TEXT,
    error_msg    TEXT,
    source       TEXT NOT NULL DEFAULT 'email',
    received_at  TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_messages(status);
"""

# Default customers seeded on first init (mirrors the hardcoded dict in customers.py)
_DEFAULT_CUSTOMERS = [
    ("CUST-A", "Acme Corp",    5,  '["amazon-freight"]'),
    ("CUST-B", "Beta Imports", 12, '["ups-freight"]'),
    ("CUST-C", "Gamma LLC",    30, None),
    ("CUST-D", "Delta Co",     10, None),
]


_DEFAULT_SETTINGS = [
    ("bidding_window_ms", "3000"),   # Round 1 carrier response window
    ("rebid_window_ms",   "1500"),   # Round 2 re-bid window
]


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create tables and indexes if they don't exist, then seed defaults."""
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        # Migrate: add source column to quotes if this is an older DB
        cols = [r[1] for r in conn.execute("PRAGMA table_info(quotes)").fetchall()]
        if "source" not in cols:
            conn.execute("ALTER TABLE quotes ADD COLUMN source TEXT NOT NULL DEFAULT 'ui'")
        now = datetime.now(timezone.utc).isoformat()
        # Seed default customers (INSERT OR IGNORE so existing data is never overwritten)
        conn.executemany(
            "INSERT OR IGNORE INTO customers (customer_id, name, markup_pct, preferred_carriers, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(cid, name, markup, carriers, now) for cid, name, markup, carriers in _DEFAULT_CUSTOMERS],
        )
        # Seed default settings
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            [(k, v, now) for k, v in _DEFAULT_SETTINGS],
        )


def save_quote(quote: dict, db_path: str = DEFAULT_DB_PATH, source: str = "ui") -> None:
    """
    Insert or replace a quote record.
    Extracts indexed metadata from the quote dict; stores full dict as JSON blob.
    source: 'ui' (default) or 'email' for email-ingested quotes.
    """
    init_db(db_path)

    req = quote.get("request", {})
    markup = quote.get("markup_result") or {}
    winning = quote.get("winning_bid") or {}
    fq = quote.get("final_quote") or {}
    pricing = fq.get("pricing") or markup
    # Honour source stored in request dict (email path stamps it there)
    effective_source = req.get("_source", source)

    now = datetime.now(timezone.utc).isoformat()

    row = {
        "quote_id":             quote["quote_id"],
        "customer_id":          req.get("customer_id", ""),
        "status":               quote.get("status", "INTAKE"),
        "origin":               req.get("origin"),
        "destination":          req.get("destination"),
        "weight_lbs":           req.get("weight_lbs"),
        "cargo_type":           req.get("cargo_type"),
        "pickup_date":          req.get("pickup_date"),
        "sell_rate":            pricing.get("sell_rate"),
        "carrier_cost":         pricing.get("cost") or pricing.get("carrier_cost"),
        "markup_pct":           pricing.get("markup_pct"),
        "gross_profit":         pricing.get("gross_profit"),
        "winning_carrier_id":   winning.get("carrier_id"),
        "winning_carrier_name": winning.get("carrier_name"),
        "source":               effective_source,
        "data":                 json.dumps(quote),
        "created_at":           quote.get("created_at", now),
        "updated_at":           now,
    }

    sql = """
        INSERT INTO quotes (
            quote_id, customer_id, status, origin, destination,
            weight_lbs, cargo_type, pickup_date, sell_rate, carrier_cost,
            markup_pct, gross_profit, winning_carrier_id, winning_carrier_name,
            source, data, created_at, updated_at
        ) VALUES (
            :quote_id, :customer_id, :status, :origin, :destination,
            :weight_lbs, :cargo_type, :pickup_date, :sell_rate, :carrier_cost,
            :markup_pct, :gross_profit, :winning_carrier_id, :winning_carrier_name,
            :source, :data, :created_at, :updated_at
        )
        ON CONFLICT(quote_id) DO UPDATE SET
            status               = excluded.status,
            sell_rate            = excluded.sell_rate,
            carrier_cost         = excluded.carrier_cost,
            markup_pct           = excluded.markup_pct,
            gross_profit         = excluded.gross_profit,
            winning_carrier_id   = excluded.winning_carrier_id,
            winning_carrier_name = excluded.winning_carrier_name,
            source               = excluded.source,
            data                 = excluded.data,
            updated_at           = excluded.updated_at
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(sql, row)


def load_quote(quote_id: str, db_path: str = DEFAULT_DB_PATH) -> Optional[dict]:
    """Load a single quote by ID. Returns None if not found."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT data FROM quotes WHERE quote_id = ?", (quote_id,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def list_quotes(
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> List[dict]:
    """
    List quotes as lightweight dicts (metadata only, no full data blob).
    Optionally filter by status and/or customer_id.
    """
    init_db(db_path)
    sql = """
        SELECT quote_id, customer_id, status, origin, destination,
               weight_lbs, cargo_type, pickup_date, sell_rate, carrier_cost,
               markup_pct, gross_profit, winning_carrier_id, winning_carrier_name,
               source, created_at, updated_at
        FROM quotes
        WHERE 1=1
    """
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if customer_id:
        sql += " AND customer_id = ?"
        params.append(customer_id)
    sql += " ORDER BY created_at DESC"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()

    return [dict(r) for r in rows]


def list_quotes_full(
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> List[dict]:
    """Same as list_quotes but returns full quote dicts (includes data blob)."""
    init_db(db_path)
    sql = "SELECT data FROM quotes WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if customer_id:
        sql += " AND customer_id = ?"
        params.append(customer_id)
    sql += " ORDER BY created_at DESC"

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()

    return [json.loads(r[0]) for r in rows]


def get_all_quotes(db_path: str = DEFAULT_DB_PATH) -> List[dict]:
    """Return all quotes as full dicts. Used by the analytics engine."""
    return list_quotes_full(db_path=db_path)


# ── Customer CRUD ─────────────────────────────────────────────────────────────

def save_customer(customer: dict, db_path: str = DEFAULT_DB_PATH) -> None:
    """
    Insert or replace a customer record.
    customer dict keys: customer_id, name, markup_pct, preferred_carriers (list or None)
    """
    init_db(db_path)
    carriers = customer.get("preferred_carriers")
    if isinstance(carriers, list):
        carriers = json.dumps(carriers) if carriers else None

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO customers (customer_id, name, markup_pct, preferred_carriers, created_at)
            VALUES (:customer_id, :name, :markup_pct, :preferred_carriers, :created_at)
            ON CONFLICT(customer_id) DO UPDATE SET
                name               = excluded.name,
                markup_pct         = excluded.markup_pct,
                preferred_carriers = excluded.preferred_carriers
            """,
            {
                "customer_id":        customer["customer_id"],
                "name":               customer["name"],
                "markup_pct":         customer["markup_pct"],
                "preferred_carriers": carriers,
                "created_at":         now,
            },
        )


def get_customer_from_db(customer_id: str, db_path: str = DEFAULT_DB_PATH) -> Optional[dict]:
    """Return a customer dict or None if not found."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone()
    if row is None:
        return None
    c = dict(row)
    c["preferred_carriers"] = json.loads(c["preferred_carriers"]) if c["preferred_carriers"] else None
    return c


def list_customers(db_path: str = DEFAULT_DB_PATH) -> List[dict]:
    """Return all customers ordered by customer_id."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM customers ORDER BY customer_id"
        ).fetchall()
    result = []
    for row in rows:
        c = dict(row)
        c["preferred_carriers"] = json.loads(c["preferred_carriers"]) if c["preferred_carriers"] else None
        result.append(c)
    return result


def update_quote_status(
    quote_id: str,
    new_status: str,
    extra_data: Optional[dict] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> Optional[dict]:
    """
    Transition a quote to a new status and persist the change.
    Uses state_machine.transition() to enforce valid transitions.
    Returns the updated quote dict, or None if quote not found.
    """
    from src.state_machine import transition

    quote = load_quote(quote_id, db_path)
    if quote is None:
        return None

    transition(quote, new_status, extra_data or {})
    save_quote(quote, db_path)
    return quote


# ── Settings (key-value store) ────────────────────────────────────────────────

def get_setting(key: str, default: Optional[str] = None, db_path: str = DEFAULT_DB_PATH) -> Optional[str]:
    """Return a setting value by key, or default if not set."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """Upsert a setting value."""
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )


def get_all_settings(db_path: str = DEFAULT_DB_PATH) -> dict:
    """Return all settings as a plain dict."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {k: v for k, v in rows}


# ── Inbox (email queue) ───────────────────────────────────────────────────────

def queue_inbox_message(
    body: str,
    subject: str = "",
    sender: str = "",
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Insert a raw email into the inbox queue. Returns the new message id."""
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO inbox_messages (subject, sender, body, status, received_at) "
            "VALUES (?, ?, ?, 'PENDING', ?)",
            (subject, sender, body, now),
        )
        return cur.lastrowid


def list_inbox_messages(
    status: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> List[dict]:
    """Return inbox messages ordered by received_at DESC."""
    init_db(db_path)
    sql = "SELECT id, subject, sender, body, status, parse_result, quote_id, error_msg, received_at, processed_at FROM inbox_messages WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY received_at DESC"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def mark_inbox_processed(
    message_id: int,
    quote_id: Optional[str] = None,
    parse_result: Optional[dict] = None,
    error: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Mark a message as PROCESSED or FAILED and record the outcome."""
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    new_status = "FAILED" if error else "PROCESSED"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE inbox_messages
            SET status=?, quote_id=?, parse_result=?, error_msg=?, processed_at=?
            WHERE id=?
            """,
            (
                new_status,
                quote_id,
                json.dumps(parse_result) if parse_result else None,
                error,
                now,
                message_id,
            ),
        )
