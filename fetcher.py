import csv
import io
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

DB_PATH = "/home/jsoh/goodandbad/goodandbad.db"

# ── Public Suffix List engine ──────────────────────────────────────────────
# Downloaded once at startup; used to find registered/apex domain from any
# fully-qualified hostname while keeping full original data intact.

_PSL: frozenset = frozenset()

def _load_psl():
    global _PSL
    try:
        r = requests.get(
            "https://publicsuffix.org/list/public_suffix_list.dat",
            timeout=15, headers={"User-Agent": "GoodAndBad/1.0"}
        )
        entries = set()
        for line in r.text.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            if line.startswith("*") or line.startswith("!"):
                continue  # skip wildcards/exceptions — fallback handles them
            entries.add(line.lower())
        _PSL = frozenset(entries)
        logging.info(f"PSL loaded: {len(_PSL):,} entries")
    except Exception as e:
        logging.warning(f"PSL load failed ({e}), using simple 2-part fallback")
        _PSL = frozenset()


def base_domain(domain: str) -> str:
    """Return the registered/apex domain, e.g. mail.evil.co.uk → evil.co.uk.
    Original domain is preserved in the database; this is used only for
    cross-source grouping."""
    d = (domain or "").lower().strip().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    parts = d.split(".")
    if len(parts) <= 2:
        return d
    if _PSL:
        for i in range(len(parts)):
            suffix = ".".join(parts[i:])
            if suffix in _PSL:
                # one label to the left of this suffix = registered domain
                if i == 0:
                    return d          # whole domain is a public suffix
                return ".".join(parts[i - 1:])
    # fallback: last 2 labels
    return ".".join(parts[-2:])

SOURCES = [
    # ── Round 1 ──────────────────────────────────────────────────────────────
    {
        "name": "URLhaus",
        "url": "https://urlhaus.abuse.ch/downloads/csv_recent/",
        "category": "malware",
        "description": "abuse.ch URLhaus — active malware distribution URLs updated every 5 minutes. One of the most current active-threat feeds available.",
        "format": "csv_urlhaus",
        "homepage": "https://urlhaus.abuse.ch/",
    },
    {
        "name": "OpenPhish",
        "url": "https://raw.githubusercontent.com/openphish/public_feed/refs/heads/main/feed.txt",
        "category": "phishing",
        "description": "OpenPhish community feed — AI-verified phishing URLs, updated every 12 hours. No authentication required.",
        "format": "txt_urls",
        "homepage": "https://openphish.com/",
    },
    {
        "name": "StevenBlack-Adult",
        "url": "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn/hosts",
        "category": "adult",
        "description": "StevenBlack hosts — adult/pornographic domains aggregated from multiple community blocklists. 160k+ entries.",
        "format": "hosts",
        "homepage": "https://github.com/StevenBlack/hosts",
    },
    {
        "name": "StevenBlack-Gambling",
        "url": "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/gambling/hosts",
        "category": "gambling",
        "description": "StevenBlack hosts — online gambling and betting domains from multiple aggregated community blocklists.",
        "format": "hosts",
        "homepage": "https://github.com/StevenBlack/hosts",
    },
    {
        "name": "Disconnect-Ads",
        "url": "https://s3.amazonaws.com/lists.disconnect.me/simple_ad.txt",
        "category": "advertising",
        "description": "Disconnect.me simple ad list — advertising and tracking domains used by the Disconnect browser extension.",
        "format": "txt_domains",
        "homepage": "https://disconnect.me/",
    },
    # ── Round 2 ──────────────────────────────────────────────────────────────
    {
        "name": "ThreatFox",
        "url": "https://threatfox.abuse.ch/export/csv/recent/",
        "category": "malware",
        "description": "abuse.ch ThreatFox — indicators of compromise (IOCs): malware C2 domains/URLs, botnet infrastructure, updated continuously by the security community.",
        "format": "csv_threatfox",
        "homepage": "https://threatfox.abuse.ch/",
    },
    {
        "name": "FeodoTracker",
        "url": "https://feodotracker.abuse.ch/downloads/domainblocklist.txt",
        "category": "malware",
        "description": "abuse.ch Feodo Tracker — botnet C2 (command & control) domains for banking trojans: Emotet, Dridex, TrickBot, QakBot, BazarLoader.",
        "format": "txt_domains",
        "homepage": "https://feodotracker.abuse.ch/",
    },
    {
        "name": "Hagezi-Multi",
        "url": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/hosts/multi.txt",
        "category": "advertising",
        "description": "Hagezi DNS Multi blocklist — high-quality curated list of ads, tracking, telemetry, and malicious domains. Widely regarded as one of the best maintained DNS blocklists.",
        "format": "hosts",
        "homepage": "https://github.com/hagezi/dns-blocklists",
    },
    {
        "name": "EasyPrivacy",
        "url": "https://v.firebog.net/hosts/Easyprivacy.txt",
        "category": "advertising",
        "description": "EasyPrivacy (via Firebog) — tracking, analytics, and data-collection domains. Companion to EasyList used in uBlock Origin and AdBlock Plus.",
        "format": "txt_domains",
        "homepage": "https://easylist.to/",
    },
    {
        "name": "NoCoin",
        "url": "https://raw.githubusercontent.com/hoshsadiq/adblock-nocoin-list/master/hosts.txt",
        "category": "malware",
        "description": "NoCoin filter list — cryptomining and browser-based coin-mining domains. Blocks sites that hijack visitor CPU to mine cryptocurrency.",
        "format": "hosts",
        "homepage": "https://github.com/hoshsadiq/adblock-nocoin-list",
    },
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def clean_domain(d):
    d = (d or "").lower().strip()
    if d.startswith("www."):
        d = d[4:]
    return d


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sites (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            domain       TEXT,
            url          TEXT,
            category     TEXT,
            subcategory  TEXT,
            source       TEXT,
            date_added   TEXT,
            date_fetched TEXT,
            status       TEXT DEFAULT 'unknown',
            description  TEXT,
            tags         TEXT,
            threat_type  TEXT,
            reporter     TEXT,
            UNIQUE(url, source)
        );
        CREATE TABLE IF NOT EXISTS sources (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT UNIQUE,
            url          TEXT,
            homepage     TEXT,
            category     TEXT,
            description  TEXT,
            last_fetched TEXT,
            record_count INTEGER DEFAULT 0,
            fetch_status TEXT DEFAULT 'never'
        );
        CREATE INDEX IF NOT EXISTS idx_category ON sites(category);
        CREATE INDEX IF NOT EXISTS idx_domain   ON sites(domain);
        CREATE INDEX IF NOT EXISTS idx_source   ON sites(source);
    """)
    # Add columns if missing (safe on repeated runs)
    for col, sql in [
        ("domain_clean", "ALTER TABLE sites ADD COLUMN domain_clean TEXT"),
        ("domain_base",  "ALTER TABLE sites ADD COLUMN domain_base  TEXT"),
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass
    for idx, sql in [
        ("idx_domain_clean", "CREATE INDEX IF NOT EXISTS idx_domain_clean ON sites(domain_clean)"),
        ("idx_domain_base",  "CREATE INDEX IF NOT EXISTS idx_domain_base  ON sites(domain_base)"),
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass

    # Fast SQL backfill for domain_clean (no Python needed)
    conn.execute("""
        UPDATE sites SET domain_clean = LOWER(
            CASE WHEN domain LIKE 'www.%' THEN SUBSTR(domain,5) ELSE domain END
        ) WHERE domain_clean IS NULL OR domain_clean = ''
    """)
    conn.commit()

    # Python backfill for domain_base (needs PSL — do in batches)
    _migrate_domain_base(conn)
    conn.close()


def _migrate_domain_base(conn):
    """Compute domain_base for rows that don't have it yet, in batches."""
    batch = 5000
    offset = 0
    total_updated = 0
    while True:
        rows = conn.execute(
            "SELECT id, domain FROM sites WHERE domain_base IS NULL OR domain_base = '' LIMIT ?",
            (batch,)
        ).fetchall()
        if not rows:
            break
        updates = [(base_domain(r["domain"]), r["id"]) for r in rows]
        conn.executemany("UPDATE sites SET domain_base = ? WHERE id = ?", updates)
        conn.commit()
        total_updated += len(rows)
        offset += batch
        if len(rows) < batch:
            break
    if total_updated:
        logging.info(f"Migrated domain_base for {total_updated:,} rows")


def upsert_source(conn, src, count, status):
    conn.execute("""
        INSERT INTO sources (name, url, homepage, category, description, last_fetched, record_count, fetch_status)
        VALUES (:name, :url, :homepage, :category, :description, :now, :count, :status)
        ON CONFLICT(name) DO UPDATE SET
            last_fetched = :now,
            record_count = :count,
            fetch_status = :status
    """, {
        "name": src["name"], "url": src["url"], "homepage": src["homepage"],
        "category": src["category"], "description": src["description"],
        "now": datetime.now(timezone.utc).isoformat(), "count": count, "status": status,
    })


def insert_site(conn, domain, url, category, source, date_added=None,
                status="unknown", tags=None, threat_type=None,
                subcategory=None, reporter=None, description=None):
    now = datetime.now(timezone.utc).isoformat()
    d_clean = clean_domain(domain)
    d_base  = base_domain(domain)
    conn.execute("""
        INSERT OR IGNORE INTO sites
            (domain, domain_clean, domain_base, url, category, subcategory, source,
             date_added, date_fetched, status, tags, threat_type, reporter, description)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (domain, d_clean, d_base, url, category, subcategory, source,
          date_added or now, now, status,
          tags, threat_type, reporter, description))


def extract_domain(url):
    try:
        parsed = urlparse(url if url.startswith("http") else "http://" + url)
        return parsed.hostname or url
    except Exception:
        return url


# ── parsers ────────────────────────────────────────────────────────────────

def parse_csv_urlhaus(text):
    rows = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or row[0].startswith("#"):
            continue
        if len(row) < 8:
            continue
        # id, dateadded, url, url_status, last_online, threat, tags, urlhaus_link, reporter
        _, date_added, url, url_status, _, threat, tags, _, reporter = (row + [""] * 9)[:9]
        domain = extract_domain(url)
        rows.append({
            "domain": domain, "url": url,
            "date_added": date_added, "status": url_status or "unknown",
            "tags": tags, "threat_type": threat, "reporter": reporter.strip(),
        })
    return rows


def parse_csv_threatfox(text):
    rows = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or row[0].startswith("#"):
            continue
        if len(row) < 9:
            continue
        # first_seen, ioc_id, ioc_value, ioc_type, threat_type, malware_id, malware_alias, malware_name, last_seen, confidence, anon, tags, line_count, reporter, reference
        first_seen = row[0].strip('"')
        ioc_value  = row[2].strip('"')
        ioc_type   = row[3].strip('"')
        threat     = row[4].strip('"')
        malware    = row[7].strip('"') if len(row) > 7 else ""
        tags       = row[11].strip('"') if len(row) > 11 else ""
        reporter   = row[13].strip('"') if len(row) > 13 else ""
        if ioc_type not in ("domain", "url"):
            continue
        if ioc_type == "url":
            domain = extract_domain(ioc_value)
            url = ioc_value
        else:
            domain = ioc_value
            url = "http://" + ioc_value
        threat_label = f"{threat} / {malware}" if malware else threat
        rows.append({
            "domain": domain, "url": url, "date_added": first_seen,
            "threat_type": threat_label, "tags": tags, "reporter": reporter,
        })
    return rows


def parse_txt_urls(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("http"):
            domain = extract_domain(line)
            rows.append({"domain": domain, "url": line})
    return rows


def parse_hosts(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1"):
            domain = parts[1]
            if domain in ("0.0.0.0", "localhost", "local", "broadcasthost"):
                continue
            rows.append({"domain": domain, "url": "http://" + domain})
    return rows


def parse_txt_domains(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}$", line):
            rows.append({"domain": line, "url": "http://" + line})
    return rows


PARSERS = {
    "csv_urlhaus":  parse_csv_urlhaus,
    "csv_threatfox": parse_csv_threatfox,
    "txt_urls":     parse_txt_urls,
    "hosts":        parse_hosts,
    "txt_domains":  parse_txt_domains,
}


# ── cross-reference stats ──────────────────────────────────────────────────

def _xref_row_to_dict(row, total):
    if not row or not total:
        return {"total": 0, "solo": 0, "two_plus": 0, "three_plus": 0, "four_plus": 0,
                "pct_solo": 0, "pct_two_plus": 0, "pct_three_plus": 0}
    return {
        "total":          total,
        "solo":           row["solo"],
        "two_plus":       row["two_plus"],
        "three_plus":     row["three_plus"],
        "four_plus":      row["four_plus"],
        "pct_solo":       round(100 * row["solo"]      / total, 1),
        "pct_two_plus":   round(100 * row["two_plus"]  / total, 1),
        "pct_three_plus": round(100 * row["three_plus"]/ total, 1),
    }


def _xref_query(conn, group_col):
    row = conn.execute(f"""
        SELECT
            COUNT(*)                                       AS total,
            SUM(CASE WHEN cnt = 1  THEN 1 ELSE 0 END)     AS solo,
            SUM(CASE WHEN cnt >= 2 THEN 1 ELSE 0 END)      AS two_plus,
            SUM(CASE WHEN cnt >= 3 THEN 1 ELSE 0 END)      AS three_plus,
            SUM(CASE WHEN cnt >= 4 THEN 1 ELSE 0 END)      AS four_plus
        FROM (
            SELECT {group_col}, COUNT(DISTINCT source) AS cnt
            FROM sites
            WHERE {group_col} IS NOT NULL AND {group_col} != ''
            GROUP BY {group_col}
        )
    """).fetchone()
    total = row["total"] if row else 0
    return _xref_row_to_dict(row, total)


def get_xref_stats():
    """Returns cross-ref stats at both domain_clean and domain_base resolution."""
    conn = get_db()
    by_clean = _xref_query(conn, "domain_clean")
    by_base  = _xref_query(conn, "domain_base")
    conn.close()
    # Expose domain_base stats as the primary numbers, clean as secondary
    return {
        # primary — grouped by registered/apex domain (PSL-normalised)
        "total_domains":   by_base["total"],
        "solo":            by_base["solo"],
        "two_plus":        by_base["two_plus"],
        "three_plus":      by_base["three_plus"],
        "four_plus":       by_base["four_plus"],
        "pct_solo":        by_base["pct_solo"],
        "pct_two_plus":    by_base["pct_two_plus"],
        "pct_three_plus":  by_base["pct_three_plus"],
        # secondary — original www-stripped grouping for comparison
        "clean_total":     by_clean["total"],
        "clean_solo":      by_clean["solo"],
        "clean_pct_solo":  by_clean["pct_solo"],
        "clean_two_plus":  by_clean["two_plus"],
        "clean_pct_two_plus": by_clean["pct_two_plus"],
    }


def query_crossref(min_sources=2, search=None, limit=100, offset=0, count_only=False):
    """Cross-ref grouped by domain_base (registered domain).
    Original full domain, all sources, all categories preserved in results."""
    conn = get_db()
    having = f"cnt >= {int(min_sources)}"
    where, params = "", []
    if search:
        where  = "WHERE (domain_base LIKE ? OR domain_clean LIKE ?)"
        params = [f"%{search}%", f"%{search}%"]
    if count_only:
        row = conn.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT domain_base, COUNT(DISTINCT source) AS cnt
                FROM sites {where}
                GROUP BY domain_base HAVING {having}
            )
        """, params).fetchone()
        conn.close()
        return row[0]
    rows = conn.execute(f"""
        SELECT
            domain_base,
            COUNT(DISTINCT source)              AS source_count,
            COUNT(DISTINCT domain_clean)        AS subdomain_count,
            GROUP_CONCAT(DISTINCT source)       AS sources,
            GROUP_CONCAT(DISTINCT category)     AS categories,
            GROUP_CONCAT(DISTINCT threat_type)  AS threat_types,
            GROUP_CONCAT(DISTINCT status)       AS statuses,
            MIN(date_added)                     AS first_seen,
            MAX(date_added)                     AS last_seen
        FROM sites {where}
        GROUP BY domain_base HAVING {having}
        ORDER BY source_count DESC, subdomain_count DESC, domain_base
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── main fetch ─────────────────────────────────────────────────────────────

def fetch_source(src):
    logging.info(f"Fetching {src['name']} ...")
    try:
        r = requests.get(src["url"], timeout=60,
                         headers={"User-Agent": "GoodAndBad/1.0"})
        r.raise_for_status()
        rows = PARSERS[src["format"]](r.text)
        conn = get_db()
        for row in rows:
            insert_site(
                conn,
                domain=row.get("domain", ""),
                url=row.get("url", ""),
                category=src["category"],
                source=src["name"],
                date_added=row.get("date_added"),
                status=row.get("status", "unknown"),
                tags=row.get("tags"),
                threat_type=row.get("threat_type"),
                reporter=row.get("reporter"),
            )
        upsert_source(conn, src, len(rows), "ok")
        conn.commit()
        conn.close()
        logging.info(f"  {src['name']}: {len(rows)} records")
        return len(rows), None
    except Exception as e:
        try:
            conn = get_db()
            upsert_source(conn, src, 0, f"error: {str(e)[:120]}")
            conn.commit()
            conn.close()
        except Exception:
            pass
        logging.error(f"  {src['name']} failed: {e}")
        return 0, str(e)


def fetch_all():
    results = {}
    for src in SOURCES:
        count, err = fetch_source(src)
        results[src["name"]] = {"count": count, "error": err}
    return results


# Load PSL at import time (non-blocking fail is fine — fallback is in base_domain)
try:
    _load_psl()
except Exception as _e:
    logging.warning(f"PSL pre-load skipped: {_e}")
