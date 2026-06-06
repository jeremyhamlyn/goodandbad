import logging
import os
import threading
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
from fetcher import (init_db, get_db, fetch_all, SOURCES,
                     get_xref_stats, query_crossref)
from ip_fetcher import (init_ip_db, fetch_all_ips, IP_SOURCES, IP_CATEGORIES,
                        get_ip_stats, get_ip_xref_stats, query_ips,
                        query_ip_crossref)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

CATEGORIES = {
    "malware":     {"label": "Malware & Hackers",  "icon": "skull-crossbones", "color": "danger",   "desc": "Active malware distribution, exploit kits, botnet C&C, cryptominers, and hacker infrastructure"},
    "phishing":    {"label": "Phishing",            "icon": "fish",            "color": "warning",  "desc": "Sites impersonating legitimate services to steal credentials and personal data"},
    "adult":       {"label": "Adult / Porn",        "icon": "eye-slash",       "color": "purple",   "desc": "Pornographic and adult content sites — blocked in business environments"},
    "gambling":    {"label": "Gambling",            "icon": "dice",            "color": "info",     "desc": "Online gambling, betting, and casino sites"},
    "advertising": {"label": "Advertising & Spam",  "icon": "megaphone",       "color": "secondary","desc": "Aggressive ad networks, tracking, telemetry, and spam domains"},
}

PAID_SOURCES = [
    {"name": "Recorded Future",      "tier": "Enterprise",  "price": "$50k–$500k+/yr",  "free": False,  "best_for": "Full threat lifecycle intelligence, threat actor tracking, brand protection", "url": "https://www.recordedfuture.com/"},
    {"name": "Proofpoint ET Pro",    "tier": "SMB/Enterprise","price": "~$750/sensor/yr","free": "ET Open (limited)", "best_for": "IDS/IPS rules, malware delivery, C2 detection — best value paid ruleset", "url": "https://www.proofpoint.com/us/threat-insight/et-pro-ruleset"},
    {"name": "IBM X-Force Exchange", "tier": "Commercial",  "price": "$2k/10k records/mo","free": "5k records/mo", "best_for": "Vulnerability intelligence, IOCs, incident response integration", "url": "https://exchange.xforce.ibmcloud.com/"},
    {"name": "VirusTotal Enterprise","tier": "Enterprise",  "price": "On request",       "free": "Community (40 req/min)", "best_for": "60+ AV engine analysis, private scanning, YARA hunting, threat hunting", "url": "https://www.virustotal.com/"},
    {"name": "Shodan",               "tier": "Freelancer",  "price": "From $69/mo",      "free": "100 queries/mo", "best_for": "Exposed devices, open ports, vulnerable software, network intelligence", "url": "https://www.shodan.io/"},
    {"name": "URLScan.io",           "tier": "Commercial",  "price": "From ~$200/mo",    "free": "Limited scans", "best_for": "URL screenshot/DOM analysis, redirect chains, certificate inspection", "url": "https://urlscan.io/"},
    {"name": "Cisco Talos",          "tier": "Enterprise",  "price": "On request",       "free": "Web lookup only", "best_for": "IP/domain reputation, Snort rules, real-time threat feeds integrated with Cisco products", "url": "https://talosintelligence.com/"},
    {"name": "Webroot BrightCloud",  "tier": "Enterprise",  "price": "On request",       "free": "Web lookup only", "best_for": "Real-time URL categorisation and reputation used by many firewall vendors", "url": "https://www.brightcloud.com/"},
]

PER_PAGE = 100


def paginate(query_fn, page, **kwargs):
    offset = (page - 1) * PER_PAGE
    rows = query_fn(limit=PER_PAGE, offset=offset, **kwargs)
    total = query_fn(count_only=True, **kwargs)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return rows, total, pages


def query_sites(category=None, search=None, source=None,
                limit=PER_PAGE, offset=0, count_only=False):
    conn = get_db()
    conditions = []
    params = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if search:
        conditions.append("(domain LIKE ? OR url LIKE ? OR tags LIKE ? OR threat_type LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s, s])
    if source:
        conditions.append("source = ?")
        params.append(source)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    if count_only:
        row = conn.execute(f"SELECT COUNT(*) FROM sites {where}", params).fetchone()
        conn.close()
        return row[0]
    rows = conn.execute(
        f"SELECT * FROM sites {where} ORDER BY date_added DESC, id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_db()
    stats = {}
    for cat in CATEGORIES:
        row = conn.execute("SELECT COUNT(*) FROM sites WHERE category=?", (cat,)).fetchone()
        stats[cat] = row[0]
    total = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    conn.close()
    return stats, total


def get_sources_info():
    conn = get_db()
    rows = conn.execute("SELECT * FROM sources ORDER BY category").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    stats, total = get_stats()
    sources = get_sources_info()
    xref = get_xref_stats()
    conn = get_db()
    recent = conn.execute(
        "SELECT * FROM sites ORDER BY date_fetched DESC, id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return render_template("index.html",
                           categories=CATEGORIES, stats=stats, total=total,
                           sources=sources, recent=[dict(r) for r in recent],
                           xref=xref)


@app.route("/category/<cat>")
def category(cat):
    if cat not in CATEGORIES:
        return redirect(url_for("index"))
    page = max(1, request.args.get("page", 1, type=int))
    search = request.args.get("q", "").strip()
    source_filter = request.args.get("source", "").strip()
    rows, total, pages = paginate(query_sites, page,
                                  category=cat, search=search or None,
                                  source=source_filter or None)
    conn = get_db()
    cat_sources = conn.execute(
        "SELECT DISTINCT source FROM sites WHERE category=? ORDER BY source", (cat,)
    ).fetchall()
    conn.close()
    return render_template("category.html",
                           cat=cat, meta=CATEGORIES[cat],
                           sites=rows, total=total, page=page, pages=pages,
                           search=search, source_filter=source_filter,
                           cat_sources=[r[0] for r in cat_sources],
                           categories=CATEGORIES)


@app.route("/crossref")
def crossref():
    min_src = request.args.get("min", 2, type=int)
    min_src = max(2, min(min_src, 6))
    page    = max(1, request.args.get("page", 1, type=int))
    search  = request.args.get("q", "").strip()
    rows, total, pages = paginate(
        lambda **kw: query_crossref(min_sources=min_src, search=search or None, **kw),
        page
    )
    xref = get_xref_stats()
    return render_template("crossref.html",
                           sites=rows, total=total, page=page, pages=pages,
                           min_src=min_src, search=search, xref=xref,
                           categories=CATEGORIES)


@app.route("/sources")
def sources():
    src_rows = get_sources_info()
    _, total = get_stats()
    return render_template("sources.html",
                           sources=src_rows, categories=CATEGORIES,
                           source_defs=SOURCES, total=total,
                           paid_sources=PAID_SOURCES)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    rows, total, pages = paginate(query_sites, page, search=q or None) if q else ([], 0, 1)
    return render_template("search.html",
                           q=q, sites=rows, total=total,
                           page=page, pages=pages, categories=CATEGORIES)


@app.route("/ips")
def ips_dashboard():
    ip_stats, ip_total, ip_sources = get_ip_stats()
    xref = get_ip_xref_stats()
    return render_template("ips_dashboard.html",
                           ip_categories=IP_CATEGORIES, ip_stats=ip_stats,
                           ip_total=ip_total, ip_sources=ip_sources,
                           xref=xref, categories=CATEGORIES)


@app.route("/ips/category/<cat>")
def ips_category(cat):
    if cat not in IP_CATEGORIES:
        return redirect(url_for("ips_dashboard"))
    page          = max(1, request.args.get("page", 1, type=int))
    search        = request.args.get("q", "").strip()
    source_filter = request.args.get("source", "").strip()
    rows, total, pages = paginate(query_ips, page,
                                  category=cat, search=search or None,
                                  source=source_filter or None)
    from fetcher import get_db as _db
    conn = _db()
    cat_sources = conn.execute(
        "SELECT DISTINCT source FROM ips WHERE category=? ORDER BY source", (cat,)
    ).fetchall()
    conn.close()
    return render_template("ips_category.html",
                           cat=cat, meta=IP_CATEGORIES[cat],
                           sites=rows, total=total, page=page, pages=pages,
                           search=search, source_filter=source_filter,
                           cat_sources=[r[0] for r in cat_sources],
                           categories=CATEGORIES, ip_categories=IP_CATEGORIES)


@app.route("/ips/crossref")
def ips_crossref():
    min_src   = request.args.get("min", 2, type=int)
    min_src   = max(2, min(min_src, 6))
    page      = max(1, request.args.get("page", 1, type=int))
    search    = request.args.get("q", "").strip()
    by_subnet = request.args.get("subnet", "0") == "1"
    rows, total, pages = paginate(
        lambda **kw: query_ip_crossref(min_sources=min_src, search=search or None,
                                       by_subnet=by_subnet, **kw),
        page
    )
    xref = get_ip_xref_stats()
    return render_template("ips_crossref.html",
                           sites=rows, total=total, page=page, pages=pages,
                           min_src=min_src, search=search, xref=xref,
                           by_subnet=by_subnet,
                           categories=CATEGORIES, ip_categories=IP_CATEGORIES)


@app.route("/scoping-document")
def scoping_document():
    return render_template("scoping_document.html", categories=CATEGORIES)


@app.route("/scoping-document/download")
def scoping_document_download():
    pdf_path = os.path.join(os.path.dirname(__file__), "GoodAndBad_Scoping_Document.pdf")
    return send_file(pdf_path, mimetype="application/pdf",
                     download_name="GoodAndBad_Scoping_Document.pdf", as_attachment=True)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    def run():
        fetch_all()
        fetch_all_ips()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return jsonify({"status": "started"})


@app.route("/api/stats")
def api_stats():
    stats, total = get_stats()
    xref = get_xref_stats()
    ip_stats, ip_total, _ = get_ip_stats()
    ip_xref = get_ip_xref_stats()
    return jsonify({"categories": stats, "total": total, "xref": xref,
                    "ip_categories": ip_stats, "ip_total": ip_total, "ip_xref": ip_xref})


if __name__ == "__main__":
    init_db()
    init_ip_db()
    logging.info("Initial data fetch starting...")
    fetch_all()
    fetch_all_ips()
    app.run(host="0.0.0.0", port=8099, debug=False)
