import csv
import io
import logging
import re
import sqlite3
from datetime import datetime, timezone

import requests

from fetcher import DB_PATH, get_db

IP_SOURCES = [
    {
        "name": "FeodoTracker-IPs",
        "url": "https://feodotracker.abuse.ch/downloads/ipblocklist.csv",
        "category": "c2",
        "format": "csv_feodo_ip",
        "description": "abuse.ch Feodo Tracker — active botnet C2 IP addresses for Emotet, Dridex, TrickBot, QakBot, BazarLoader. Updated every 5 minutes. Only confirmed active C2s included.",
        "homepage": "https://feodotracker.abuse.ch/",
    },
    {
        "name": "CINS-Army",
        "url": "https://cinsscore.com/list/ci-badguys.txt",
        "category": "scanner",
        "format": "txt_ips",
        "description": "CINS Score Army list — IPs observed performing reconnaissance, scanning, and malicious behaviour over the past 14 days. Sourced from CINS threat intelligence honeypots.",
        "homepage": "https://cinsscore.com/",
    },
    {
        "name": "EmergingThreats-IPs",
        "url": "https://rules.emergingthreats.net/blockrules/compromised-ips.txt",
        "category": "attack",
        "format": "txt_ips",
        "description": "Emerging Threats — compromised and known-attack IP addresses, maintained by Proofpoint threat researchers alongside the ET ruleset.",
        "homepage": "https://rules.emergingthreats.net/",
    },
    {
        "name": "IPsum",
        "url": "https://raw.githubusercontent.com/stamparm/ipsum/master/ipsum.txt",
        "category": "attack",
        "format": "txt_ipsum",
        "description": "ipsum — meta-aggregated IP blacklist: each IP is scored by how many independent threat sources flag it. Only IPs flagged by 3+ sources included here.",
        "homepage": "https://github.com/stamparm/ipsum",
    },
    {
        "name": "SSLBL-IPs",
        "url": "https://sslbl.abuse.ch/blacklist/sslipblacklist.txt",
        "category": "c2",
        "format": "txt_ips",
        "description": "abuse.ch SSL Blacklist — IPs hosting botnet C2 servers with malicious SSL certificates. High-confidence C2 infrastructure indicators.",
        "homepage": "https://sslbl.abuse.ch/",
    },
    {
        "name": "DShield-Subnets",
        "url": "https://www.dshield.org/block.txt",
        "category": "attack",
        "format": "txt_dshield",
        "description": "SANS DShield — top attacking /24 subnets aggregated from firewall logs submitted by thousands of contributors worldwide. Updated daily.",
        "homepage": "https://www.dshield.org/",
    },
    {
        "name": "BlocklistDE",
        "url": "https://lists.blocklist.de/lists/all.txt",
        "category": "attack",
        "format": "txt_ips",
        "description": "Blocklist.de — IPs reported as attackers in the past 48 hours via fail2ban reports from hundreds of servers. Covers SSH, mail, FTP, web, and SIP attacks.",
        "homepage": "https://www.blocklist.de/",
    },
    {
        "name": "Greensnow",
        "url": "https://blocklist.greensnow.co/greensnow.txt",
        "category": "brute_force",
        "format": "txt_ips",
        "description": "Greensnow — SSH brute-force attack IPs detected by honeypots. Rolling blocklist of IPs actively attempting credential attacks.",
        "homepage": "https://greensnow.co/",
    },
    {
        "name": "BinaryDefense",
        "url": "https://www.binarydefense.com/banlist.txt",
        "category": "attack",
        "format": "txt_ips",
        "description": "Binary Defense Artillery Threat Intelligence — IPs collected from Artillery honeypots performing attacks, scans, and exploitation attempts.",
        "homepage": "https://www.binarydefense.com/",
    },
    {
        "name": "TorExitNodes",
        "url": "https://check.torproject.org/torbulkexitlist",
        "category": "tor",
        "format": "txt_ips",
        "description": "Tor Project — authoritative list of all active Tor exit node IP addresses. Traffic from these IPs originates from the anonymous Tor network.",
        "homepage": "https://www.torproject.org/",
    },
]

IP_CATEGORIES = {
    "c2":          {"label": "C2 / Botnet",       "icon": "broadcast",      "color": "danger",   "desc": "Active command & control servers for botnets and malware families"},
    "attack":      {"label": "Attack / Exploit",   "icon": "bug",            "color": "warning",  "desc": "IPs actively exploiting, attacking, or compromising systems"},
    "brute_force": {"label": "Brute Force",        "icon": "key",            "color": "orange",   "desc": "IPs performing credential brute-force attacks (SSH, RDP, FTP, mail)"},
    "spam":        {"label": "Spam Sources",       "icon": "envelope-x",     "color": "secondary","desc": "IPs responsible for sending spam, phishing emails, and malicious mail"},
    "scanner":     {"label": "Scanners / Recon",   "icon": "eye",            "color": "info",     "desc": "IPs performing network reconnaissance, port scanning, and probing"},
    "tor":         {"label": "Tor Exit Nodes",     "icon": "shield-lock",    "color": "purple",   "desc": "Active Tor anonymisation network exit nodes — traffic origin unattributable"},
}


def ip_subnet(ip: str) -> str:
    """/24 subnet of an IP — e.g. 1.2.3.4 → 1.2.3.0/24. Used for cross-ref
    grouping; original IP is always preserved in the database."""
    parts = (ip or "").split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    return ip


def init_ip_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ips (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ip           TEXT,
            cidr         TEXT,
            category     TEXT,
            source       TEXT,
            date_added   TEXT,
            date_fetched TEXT,
            country      TEXT,
            asn          TEXT,
            port         TEXT,
            malware      TEXT,
            status       TEXT DEFAULT 'unknown',
            description  TEXT,
            tags         TEXT,
            UNIQUE(ip, source)
        );
        CREATE INDEX IF NOT EXISTS idx_ip_ip       ON ips(ip);
        CREATE INDEX IF NOT EXISTS idx_ip_category ON ips(category);
        CREATE INDEX IF NOT EXISTS idx_ip_source   ON ips(source);

        CREATE TABLE IF NOT EXISTS ip_sources (
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
    """)
    # Add ip_subnet column if missing
    try:
        conn.execute("ALTER TABLE ips ADD COLUMN ip_subnet TEXT")
    except Exception:
        pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ip_subnet ON ips(ip_subnet)")
    except Exception:
        pass
    # Backfill ip_subnet for existing rows
    conn.execute("""
        UPDATE ips SET ip_subnet =
            SUBSTR(ip, 1, INSTR(ip||'.', '.') + INSTR(SUBSTR(ip, INSTR(ip,'.')+1)||'.', '.') +
                INSTR(SUBSTR(ip, INSTR(ip,'.')+INSTR(SUBSTR(ip,INSTR(ip,'.')+1)||'.',INSTR(SUBSTR(ip,INSTR(ip,'.')+1)||'.','.')))||'.', '.') - 1) || '.0/24'
        WHERE ip_subnet IS NULL OR ip_subnet = ''
    """)
    # Simpler fallback via Python for any that failed the SQL expression
    rows = conn.execute("SELECT id, ip FROM ips WHERE ip_subnet IS NULL OR ip_subnet = ''").fetchall()
    if rows:
        conn.executemany("UPDATE ips SET ip_subnet = ? WHERE id = ?",
                         [(ip_subnet(r["ip"]), r["id"]) for r in rows])
    conn.commit()
    conn.close()


def upsert_ip_source(conn, src, count, status):
    conn.execute("""
        INSERT INTO ip_sources (name, url, homepage, category, description, last_fetched, record_count, fetch_status)
        VALUES (:name, :url, :homepage, :category, :description, :now, :count, :status)
        ON CONFLICT(name) DO UPDATE SET
            last_fetched = :now,
            record_count = :count,
            fetch_status = :status
    """, {
        "name": src["name"], "url": src["url"], "homepage": src["homepage"],
        "category": src["category"], "description": src["description"],
        "now": datetime.now(timezone.utc).isoformat(),
        "count": count, "status": status,
    })


def insert_ip(conn, ip, source, category, cidr=None, country=None, asn=None,
              port=None, malware=None, status="unknown", tags=None,
              description=None, date_added=None):
    now = datetime.now(timezone.utc).isoformat()
    subnet = ip_subnet(ip)
    conn.execute("""
        INSERT OR IGNORE INTO ips
            (ip, ip_subnet, cidr, category, source, date_added, date_fetched,
             country, asn, port, malware, status, tags, description)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ip, subnet, cidr, category, source,
          date_added or now, now,
          country, asn, port, malware, status, tags, description))


def is_valid_ip(s):
    parts = s.strip().split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


# ── parsers ────────────────────────────────────────────────────────────────

def parse_csv_feodo_ip(text):
    rows = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or row[0].startswith("#"):
            continue
        if len(row) < 2:
            continue
        # first_seen_utc, dst_ip, dst_port, c2_status, last_online, malware
        first_seen = row[0].strip('"')
        ip         = row[1].strip('"')
        port       = row[2].strip('"') if len(row) > 2 else None
        status     = row[3].strip('"') if len(row) > 3 else "unknown"
        malware    = row[5].strip('"') if len(row) > 5 else None
        if not is_valid_ip(ip):
            continue
        rows.append({
            "ip": ip, "port": port, "malware": malware,
            "status": status.lower(), "date_added": first_seen,
        })
    return rows


def parse_txt_ips(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ip = line.split()[0]
        if is_valid_ip(ip):
            rows.append({"ip": ip})
    return rows


def parse_txt_dshield(text):
    """DShield block.txt: network  mask  attacks  netname  country  contact"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        network = parts[0]
        mask    = parts[1]
        country = parts[4] if len(parts) > 4 else None
        if not is_valid_ip(network):
            continue
        # Store as CIDR /24
        cidr = f"{network}/24"
        rows.append({"ip": network, "cidr": cidr, "country": country})
    return rows


def parse_txt_ipsum(text):
    """ipsum format: IP<tab>source_count — only include IPs flagged by 3+ sources."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ip    = parts[0].strip()
        try:
            count = int(parts[1].strip())
        except ValueError:
            continue
        if count >= 3 and is_valid_ip(ip):
            rows.append({"ip": ip, "tags": f"flagged_by_{count}_sources"})
    return rows


IP_PARSERS = {
    "csv_feodo_ip": parse_csv_feodo_ip,
    "txt_ips":      parse_txt_ips,
    "txt_dshield":  parse_txt_dshield,
    "txt_ipsum":    parse_txt_ipsum,
}


# ── cross-reference ────────────────────────────────────────────────────────

def _ip_xref_counts(conn, group_col):
    row = conn.execute(f"""
        SELECT
            COUNT(*)                                       AS total,
            SUM(CASE WHEN cnt = 1  THEN 1 ELSE 0 END)     AS solo,
            SUM(CASE WHEN cnt >= 2 THEN 1 ELSE 0 END)     AS two_plus,
            SUM(CASE WHEN cnt >= 3 THEN 1 ELSE 0 END)     AS three_plus,
            SUM(CASE WHEN cnt >= 4 THEN 1 ELSE 0 END)     AS four_plus
        FROM (
            SELECT {group_col}, COUNT(DISTINCT source) AS cnt
            FROM ips WHERE {group_col} IS NOT NULL AND {group_col} != ''
            GROUP BY {group_col}
        )
    """).fetchone()
    if not row or not row["total"]:
        return {"total": 0, "solo": 0, "two_plus": 0, "three_plus": 0, "four_plus": 0,
                "pct_solo": 0, "pct_two_plus": 0, "pct_three_plus": 0}
    t = row["total"]
    return {
        "total":          t,
        "solo":           row["solo"],
        "two_plus":       row["two_plus"],
        "three_plus":     row["three_plus"],
        "four_plus":      row["four_plus"],
        "pct_solo":       round(100 * row["solo"]      / t, 1),
        "pct_two_plus":   round(100 * row["two_plus"]  / t, 1),
        "pct_three_plus": round(100 * row["three_plus"]/ t, 1),
    }


def get_ip_xref_stats():
    """Returns cross-ref at both exact-IP and /24-subnet resolution."""
    conn = get_db()
    by_ip     = _ip_xref_counts(conn, "ip")
    by_subnet = _ip_xref_counts(conn, "ip_subnet")
    conn.close()
    return {
        # primary — exact IP matches across sources
        "total_ips":          by_ip["total"],
        "solo":               by_ip["solo"],
        "two_plus":           by_ip["two_plus"],
        "three_plus":         by_ip["three_plus"],
        "four_plus":          by_ip["four_plus"],
        "pct_solo":           by_ip["pct_solo"],
        "pct_two_plus":       by_ip["pct_two_plus"],
        "pct_three_plus":     by_ip["pct_three_plus"],
        # secondary — /24 subnet grouping
        "subnet_total":       by_subnet["total"],
        "subnet_solo":        by_subnet["solo"],
        "subnet_pct_solo":    by_subnet["pct_solo"],
        "subnet_two_plus":    by_subnet["two_plus"],
        "subnet_pct_two_plus":by_subnet["pct_two_plus"],
    }


def query_ip_crossref(min_sources=2, search=None, by_subnet=False,
                      limit=100, offset=0, count_only=False):
    """Cross-ref by exact IP or by /24 subnet. All original data preserved."""
    conn = get_db()
    group_col = "ip_subnet" if by_subnet else "ip"
    having = f"cnt >= {int(min_sources)}"
    where, params = "", []
    if search:
        where  = "WHERE (ip LIKE ? OR ip_subnet LIKE ?)"
        params = [f"%{search}%", f"%{search}%"]
    if count_only:
        row = conn.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT {group_col}, COUNT(DISTINCT source) AS cnt
                FROM ips {where} GROUP BY {group_col} HAVING {having}
            )
        """, params).fetchone()
        conn.close()
        return row[0]
    rows = conn.execute(f"""
        SELECT
            {group_col}                         AS group_key,
            COUNT(DISTINCT ip)                  AS ip_count,
            COUNT(DISTINCT source)              AS source_count,
            GROUP_CONCAT(DISTINCT source)       AS sources,
            GROUP_CONCAT(DISTINCT category)     AS categories,
            GROUP_CONCAT(DISTINCT country)      AS countries,
            GROUP_CONCAT(DISTINCT malware)      AS malwares,
            GROUP_CONCAT(DISTINCT port)         AS ports,
            MIN(date_added)                     AS first_seen,
            MAX(date_added)                     AS last_seen
        FROM ips {where}
        GROUP BY {group_col} HAVING {having}
        ORDER BY source_count DESC, ip_count DESC, {group_col}
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_ips(category=None, search=None, source=None,
              limit=100, offset=0, count_only=False):
    conn = get_db()
    conditions, params = [], []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if search:
        conditions.append("(ip LIKE ? OR malware LIKE ? OR country LIKE ? OR tags LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s, s])
    if source:
        conditions.append("source = ?")
        params.append(source)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    if count_only:
        row = conn.execute(f"SELECT COUNT(*) FROM ips {where}", params).fetchone()
        conn.close()
        return row[0]
    rows = conn.execute(
        f"SELECT * FROM ips {where} ORDER BY date_added DESC, id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ip_stats():
    conn = get_db()
    stats = {}
    for cat in IP_CATEGORIES:
        row = conn.execute("SELECT COUNT(*) FROM ips WHERE category=?", (cat,)).fetchone()
        stats[cat] = row[0]
    total = conn.execute("SELECT COUNT(*) FROM ips").fetchone()[0]
    sources = conn.execute("SELECT * FROM ip_sources ORDER BY category").fetchall()
    conn.close()
    return stats, total, [dict(r) for r in sources]


# ── main fetch ─────────────────────────────────────────────────────────────

def fetch_ip_source(src):
    logging.info(f"[IP] Fetching {src['name']} ...")
    try:
        r = requests.get(src["url"], timeout=60,
                         headers={"User-Agent": "GoodAndBad/1.0"})
        r.raise_for_status()
        rows = IP_PARSERS[src["format"]](r.text)
        conn = get_db()
        for row in rows:
            insert_ip(
                conn,
                ip=row.get("ip", ""),
                source=src["name"],
                category=src["category"],
                cidr=row.get("cidr"),
                country=row.get("country"),
                port=row.get("port"),
                malware=row.get("malware"),
                status=row.get("status", "unknown"),
                date_added=row.get("date_added"),
            )
        upsert_ip_source(conn, src, len(rows), "ok")
        conn.commit()
        conn.close()
        logging.info(f"[IP]   {src['name']}: {len(rows)} records")
        return len(rows), None
    except Exception as e:
        try:
            conn = get_db()
            upsert_ip_source(conn, src, 0, f"error: {str(e)[:120]}")
            conn.commit()
            conn.close()
        except Exception:
            pass
        logging.error(f"[IP]   {src['name']} failed: {e}")
        return 0, str(e)


def fetch_all_ips():
    results = {}
    for src in IP_SOURCES:
        count, err = fetch_ip_source(src)
        results[src["name"]] = {"count": count, "error": err}
    return results
