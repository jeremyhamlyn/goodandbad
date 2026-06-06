#!/usr/bin/env python3
"""Minimal server to serve the Good & Bad scoping document PDF."""

import os
from flask import Flask, send_file, render_template_string

app = Flask(__name__)

PDF_PATH = os.path.join(os.path.dirname(__file__), 'GoodAndBad_Scoping_Document.pdf')

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Good & Bad — Scoping Document</title>
<style>
  body { background:#0d1117; color:#e6edf3; font-family:sans-serif;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; min-height:100vh; margin:0; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:12px;
          padding:48px 56px; text-align:center; max-width:520px; }
  h1 { color:#d29922; font-size:1.7rem; margin-bottom:.3rem; }
  p  { color:#8b949e; margin:.5rem 0 2rem; }
  a.btn { display:inline-block; background:#1f6feb; color:#fff;
          padding:12px 32px; border-radius:6px; text-decoration:none;
          font-weight:600; font-size:1rem; }
  a.btn:hover { background:#388bfd; }
  .meta { font-size:.8rem; color:#484f58; margin-top:1.5rem; }
</style>
</head>
<body>
<div class="card">
  <h1>Good &amp; Bad Threat Intel</h1>
  <p>Active Threat Database with Real-Time Firewall &amp; DNS Integration<br>
     <em>Scoping Document — 27 April 2026</em></p>
  <a class="btn" href="/document.pdf">&#8615; Download PDF</a>
  <div class="meta">9 pages &bull; diagrams included &bull; {{ size }} KB</div>
</div>
</body>
</html>"""

@app.route('/')
def index():
    size = round(os.path.getsize(PDF_PATH) / 1024)
    return render_template_string(PAGE, size=size)

@app.route('/document.pdf')
def document():
    return send_file(PDF_PATH, mimetype='application/pdf',
                     download_name='GoodAndBad_Scoping_Document.pdf')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8097, debug=False)
