#!/usr/bin/env python3
"""
Deal Sifta — Run History (native desktop app)
Power Automate-style pipeline run viewer backed by watcher.log + Azure SQL.
"""
import os, re, json, threading
from flask import Flask, render_template_string, jsonify
import pymssql
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)

LOG_PATH = os.path.join(os.path.dirname(__file__), "watcher.log")

_FILE_ID_RE = re.compile(r'\[\S{20,}\]:\n?')


# ---------------------------------------------------------------------------
# Log parser — splits watcher.log into per-email runs with step details
# ---------------------------------------------------------------------------

def parse_runs():
    """Parse watcher.log into structured runs, one per uid processed."""
    if not os.path.exists(LOG_PATH):
        return []

    with open(LOG_PATH, "r") as f:
        lines = f.readlines()

    runs = []
    current_run = None

    for raw in lines:
        line = raw.rstrip()

        # New run starts with uid= line
        uid_match = re.match(r'\s*(?:\[[\d\-: ]+\]\s*)?uid=(\d+)\s+(.*)', line)
        if uid_match:
            if current_run:
                _finalise_run(current_run)
                runs.append(current_run)
            current_run = {
                "uid": int(uid_match.group(1)),
                "subject": uid_match.group(2).strip(),
                "steps": [],
                "deal_name": None,
                "score": None,
                "score_line": None,
                "msa": None,
                "status": "Succeeded",
                "ocr_count": 0,
                "ocr_chars": 0,
                "gated": False,
                "gate_reason": None,
                "db_ok": False,
                "calendar": None,
                "ocr_output": False,
                "web_rent": None,
                "timestamp": None,
                "step_count": 0,
                "rent_upside": False,
            }
            current_run["steps"].append({"icon": "📨", "label": "Email received", "detail": uid_match.group(2).strip(), "status": "ok"})
            continue

        if not current_run:
            continue

        # Pre-OCR gated
        if "PRE-OCR GATED" in line:
            reason = re.search(r'GATED \((.+?)\)', line)
            current_run["gated"] = True
            current_run["gate_reason"] = reason.group(1) if reason else "unknown"
            current_run["status"] = "Gated"
            current_run["steps"].append({"icon": "🚫", "label": "Pre-OCR gate", "detail": current_run["gate_reason"], "status": "gated"})

        # OCR lines
        elif line.strip().startswith("OCR:") and "done:" not in line.lower() and "no text" not in line.lower() and "resized" not in line.lower():
            fname = re.search(r'OCR:\s*(.+?)\s*\(', line)
            current_run["ocr_count"] += 1
            if fname:
                current_run["steps"].append({"icon": "🔍", "label": "OCR scan", "detail": fname.group(1)[:50], "status": "ok"})

        elif "OCR done:" in line:
            chars = re.search(r'(\d+) chars', line)
            if chars:
                current_run["ocr_chars"] += int(chars.group(1))

        elif "no text in image" in line:
            pass

        # Extract result
        elif "-> Extract:" in line:
            m = re.search(r'Extract:\s*(.+?)\s*\(deal=(\w+)\)', line)
            if m:
                current_run["deal_name"] = m.group(1)
                is_deal = m.group(2) == "True"
                current_run["steps"].append({
                    "icon": "🤖",
                    "label": "Claude extraction",
                    "detail": f"{m.group(1)} — {'deal' if is_deal else 'not a deal'}",
                    "status": "ok" if is_deal else "warn"
                })

        # MSA
        elif "-> MSA:" in line:
            msa = line.split("MSA:")[-1].strip()
            current_run["msa"] = msa
            current_run["steps"].append({"icon": "📍", "label": "MSA lookup", "detail": msa, "status": "ok"})

        # Web rent
        elif "-> Web rent:" in line:
            detail = line.split("Web rent:")[-1].strip()
            current_run["web_rent"] = detail
            current_run["steps"].append({"icon": "💰", "label": "Web rent lookup", "detail": detail, "status": "ok" if "not found" not in detail else "warn"})

        # Score
        elif "-> Score:" in line:
            m = re.search(r'Score:\s*([\d.]+)\s*\((.+)\)', line)
            if m:
                current_run["score"] = float(m.group(1))
                current_run["score_line"] = m.group(2)
                current_run["steps"].append({
                    "icon": "📊",
                    "label": "Scoring",
                    "detail": f"Score: {m.group(1)} — {m.group(2)}",
                    "status": "ok"
                })

        # DB writes
        elif "-> raw_deal_emails: OK" in line:
            current_run["db_ok"] = True
            current_run["steps"].append({"icon": "💾", "label": "Azure SQL write", "detail": "raw_deal_emails: OK", "status": "ok"})
        elif "-> deal_intake: OK" in line:
            current_run["steps"].append({"icon": "💾", "label": "Deal intake write", "detail": "deal_intake: OK", "status": "ok"})

        # Calendar
        elif "-> Calendar:" in line:
            detail = line.split("Calendar:")[-1].strip()
            current_run["calendar"] = detail
            current_run["steps"].append({"icon": "📅", "label": "Calendar event", "detail": detail, "status": "ok"})

        # OCR output
        elif "-> OCRoutput: appended" in line:
            current_run["ocr_output"] = True
            current_run["steps"].append({"icon": "📤", "label": "OCR output saved", "detail": "Appended to INBOX/OCRoutput", "status": "ok"})
        elif "-> OCRoutput: already exists" in line:
            current_run["ocr_output"] = True
            current_run["steps"].append({"icon": "📤", "label": "OCR output", "detail": "Already exists, skipped", "status": "warn"})

        # Failures
        elif "failed" in line.lower() and "->" in line:
            current_run["status"] = "Failed"
            current_run["steps"].append({"icon": "❌", "label": "Error", "detail": line.strip(), "status": "error"})

    if current_run:
        _finalise_run(current_run)
        runs.append(current_run)

    return list(reversed(runs))  # newest first


def _finalise_run(run):
    """Post-process a run: consolidate OCR steps, count steps."""
    if run["ocr_count"] > 0:
        ocr_summary = {
            "icon": "📸", "label": "OCR processed",
            "detail": f"{run['ocr_count']} images → {run['ocr_chars']} chars extracted",
            "status": "ok" if run["ocr_chars"] > 50 else "warn"
        }
        run["steps"] = [s for s in run["steps"] if s["label"] != "OCR scan"]
        run["steps"].insert(1, ocr_summary)
    run["step_count"] = len(run["steps"])


# ---------------------------------------------------------------------------
# DB enrichment — timestamps, extracted fields, raw text
# ---------------------------------------------------------------------------

def enrich_from_db(runs):
    try:
        c = pymssql.connect(
            os.getenv("AZURE_SQL_SERVER"), os.getenv("AZURE_SQL_USER"),
            os.getenv("AZURE_SQL_PASSWORD"), os.getenv("AZURE_SQL_DATABASE"),
        )
        cur = c.cursor()
        cur.execute("SELECT uid, extracted_json, scraped_text, ocr_text, inserted_at FROM raw_deal_emails")
        db_map = {}
        for uid, ej, scraped, ocr, ins_at in cur.fetchall():
            rec = {"scraped": scraped or "", "ocr": ocr or "", "inserted_at": ins_at}
            if ej:
                try:
                    rec["fields"] = json.loads(ej)
                except Exception:
                    rec["fields"] = {}
            else:
                rec["fields"] = {}
            db_map[uid] = rec
        c.close()

        for run in runs:
            rec = db_map.get(run["uid"], {"fields": {}, "scraped": "", "ocr": "", "inserted_at": None})
            j = rec["fields"]
            run["asset_class"] = j.get("asset_class")
            run["tt"] = j.get("transaction_type")
            run["notes_reason"] = j.get("notes_score_reason")
            run["notes"] = j.get("notes") or ""
            run["scraped_text"] = rec["scraped"][:4000]
            raw_ocr = rec["ocr"][:4000]
            run["ocr_text"] = raw_ocr
            run["ocr_clean"] = _FILE_ID_RE.sub('', raw_ocr).strip() if raw_ocr else ""
            run["extracted"] = {k: v for k, v in j.items() if v is not None and k not in ("score_breakdown", "notes_score_reason")}
            if rec["inserted_at"]:
                run["timestamp"] = rec["inserted_at"].strftime("%b %d, %Y  %I:%M %p")
            db_score = j.get("score")
            if db_score is not None:
                run["score"] = db_score
            # Detect rent upside from Claude signals or MTM classification
            pos_sigs = [s.lower() for s in (j.get("positive_signals") or []) if isinstance(s, str)]
            run["rent_upside"] = (
                any(s in pos_sigs for s in ["below-market rents", "rent upside", "mark-to-market"])
                or run.get("tt") == "Mark-to-Market"
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Check if the watcher process is currently running
# ---------------------------------------------------------------------------

def watcher_alive():
    import subprocess
    try:
        out = subprocess.check_output(["pgrep", "-f", "email_watcher.py"], text=True)
        return bool(out.strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deal Sifta — Run History</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d1117;color:#e0e0e0;padding:0}

  /* ── toolbar ── */
  .toolbar{display:flex;align-items:center;padding:14px 28px;background:#161b22;border-bottom:1px solid #21262d;gap:16px;-webkit-app-region:drag}
  .toolbar h1{font-size:1.15rem;color:#fff;font-weight:700}
  .toolbar .spacer{flex:1}
  .watcher-status{display:flex;align-items:center;gap:6px;font-size:.72rem;color:#8b949e;-webkit-app-region:no-drag}
  .watcher-dot{width:8px;height:8px;border-radius:50%}
  .watcher-dot.on{background:#3fb950;box-shadow:0 0 6px #3fb95088}.watcher-dot.off{background:#f85149}
  .refresh-btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:5px 14px;border-radius:6px;font-size:.75rem;cursor:pointer;-webkit-app-region:no-drag}
  .refresh-btn:hover{background:#30363d}

  .content{padding:20px 28px}
  .subtitle{color:#555;font-size:.75rem;margin-bottom:16px}

  /* ── stats ── */
  .stats{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
  .stat{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:10px 16px;min-width:90px}
  .stat .l{font-size:.6rem;color:#555;text-transform:uppercase;letter-spacing:.5px}
  .stat .v{font-size:1.4rem;font-weight:700;color:#fff;margin-top:2px}
  .stat .v.g{color:#3fb950}.stat .v.y{color:#d29922}.stat .v.r{color:#f85149}

  /* ── filter bar ── */
  .filter-bar{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
  .filter-btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:5px 12px;border-radius:6px;font-size:.72rem;cursor:pointer}
  .filter-btn:hover{background:#30363d}
  .filter-btn.active{background:#388bfd22;border-color:#388bfd;color:#58a6ff}

  /* ── run cards ── */
  .run{background:#161b22;border:1px solid #21262d;border-radius:10px;margin-bottom:8px;overflow:hidden;transition:border-color .2s}
  .run:hover{border-color:#30363d}
  .run-header{display:flex;align-items:center;padding:12px 16px;cursor:pointer;gap:12px;user-select:none}
  .run-header:hover{background:#1c2128}

  .run-status-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:5px;font-size:.68rem;font-weight:600;letter-spacing:.3px;flex-shrink:0}
  .run-status-badge.succeeded{background:#23863622;color:#3fb950;border:1px solid #23863644}
  .run-status-badge.failed{background:#f8514922;color:#f85149;border:1px solid #f8514944}
  .run-status-badge.gated{background:#48505822;color:#8b949e;border:1px solid #48505844}
  .run-status-badge .dot{width:6px;height:6px;border-radius:50%}
  .run-status-badge.succeeded .dot{background:#3fb950}
  .run-status-badge.failed .dot{background:#f85149}
  .run-status-badge.gated .dot{background:#8b949e}

  .run-uid{font-family:monospace;color:#555;font-size:.7rem;min-width:50px}
  .run-name{font-weight:600;font-size:.85rem;color:#e6edf3;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .run-meta{display:flex;gap:10px;align-items:center;font-size:.72rem;flex-shrink:0}
  .run-time{color:#555;font-size:.68rem;min-width:130px;text-align:right}
  .run-steps-count{color:#484f58;font-size:.65rem}
  .run-score{font-weight:700;font-size:.95rem}
  .run-score.g{color:#3fb950}.run-score.y{color:#d29922}.run-score.r{color:#f85149}

  .badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.62rem;font-weight:600;letter-spacing:.3px}
  .badge.va{background:#7c3aed22;color:#a78bfa;border:1px solid #7c3aed44}
  .badge.dev{background:#f9731622;color:#fb923c;border:1px solid #f9731644}
  .badge.core{background:#06b6d422;color:#22d3ee;border:1px solid #06b6d444}
  .badge.lease{background:#eab30822;color:#fbbf24;border:1px solid #eab30844}
  .badge.sh{background:#ec489922;color:#f472b6;border:1px solid #ec489944}
  .badge.mtm{background:#10b98122;color:#34d399;border:1px solid #10b98144}
  .badge.rent-up{background:#f59e0b22;color:#fbbf24;border:1px solid #f59e0b44}
  .chevron{color:#484f58;transition:transform .2s;font-size:.75rem}
  .chevron.open{transform:rotate(90deg)}
  .notes-sig{color:#a78bfa;font-size:.68rem}

  /* ── detail / steps ── */
  .run-detail{display:none;border-top:1px solid #21262d;padding:0}
  .run-detail.open{display:block}
  .step{display:flex;align-items:flex-start;padding:9px 16px 9px 44px;gap:10px;font-size:.78rem;position:relative}
  .step::before{content:'';position:absolute;left:60px;top:0;bottom:0;width:1px;background:#21262d}
  .step:last-child::before{display:none}
  .step-icon{font-size:.95rem;flex-shrink:0;z-index:1}
  .step-body{flex:1}
  .step-label{font-weight:600;color:#c9d1d9}
  .step-detail{color:#8b949e;font-size:.72rem;margin-top:1px;word-break:break-all}
  .step-pip{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-top:5px}
  .step-pip.ok{background:#3fb950}.step-pip.warn{background:#d29922}.step-pip.error{background:#f85149}.step-pip.gated{background:#8b949e}

  .step-expand{color:#484f58;cursor:pointer;font-size:.68rem;margin-left:auto;padding:2px 8px;border-radius:4px;user-select:none}
  .step-expand:hover{color:#8b949e;background:#21262d}
  .step-raw{display:none;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px 14px;margin:6px 0 4px 0;font-family:monospace;font-size:.7rem;color:#8b949e;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto;line-height:1.5}
  .step-raw.open{display:block}
  .step-raw .field-key{color:#58a6ff}.step-raw .field-val{color:#c9d1d9}

  .raw-tabs{display:flex;gap:0;margin:8px 16px 0 44px;border-bottom:1px solid #21262d}
  .raw-tab{padding:7px 14px;font-size:.7rem;color:#555;cursor:pointer;border-bottom:2px solid transparent}
  .raw-tab:hover{color:#c9d1d9}
  .raw-tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
  .raw-panel{display:none;margin:0 16px 10px 44px;background:#0d1117;border:1px solid #21262d;border-top:0;border-radius:0 0 6px 6px;padding:10px 14px;font-family:monospace;font-size:.7rem;color:#8b949e;white-space:pre-wrap;word-break:break-word;max-height:400px;overflow-y:auto;line-height:1.5}
  .raw-panel.open{display:block}
</style>
</head>
<body>

<!-- ── Toolbar ── -->
<div class="toolbar">
  <h1>Deal Sifta</h1>
  <span style="color:#484f58;font-size:.8rem;font-weight:400">&mdash; Run History</span>
  <div class="spacer"></div>
  <div class="watcher-status">
    <span class="watcher-dot {{ 'on' if watcher_on else 'off' }}"></span>
    Watcher {{ 'running' if watcher_on else 'stopped' }}
  </div>
  <button class="refresh-btn" onclick="location.reload()">&#8635; Refresh</button>
</div>

<div class="content">
<p class="subtitle">{{ runs|length }} runs &middot; parsed from watcher.log &middot; last loaded {{ now }}</p>

<div class="stats">
  <div class="stat"><div class="l">Total Runs</div><div class="v">{{ runs|length }}</div></div>
  <div class="stat"><div class="l">Scored</div><div class="v g">{{ scored }}</div></div>
  <div class="stat"><div class="l">&ge; 7</div><div class="v g">{{ high }}</div></div>
  <div class="stat"><div class="l">5 &ndash; 7</div><div class="v y">{{ mid }}</div></div>
  <div class="stat"><div class="l">&lt; 5</div><div class="v r">{{ low }}</div></div>
  <div class="stat"><div class="l">Gated</div><div class="v" style="color:#8b949e">{{ gated }}</div></div>
</div>

<div class="filter-bar">
  <button class="filter-btn active" onclick="filterRuns('all')">All</button>
  <button class="filter-btn" onclick="filterRuns('succeeded')">Succeeded</button>
  <button class="filter-btn" onclick="filterRuns('high')">Score &ge; 7</button>
  <button class="filter-btn" onclick="filterRuns('mid')">Score 5-7</button>
  <button class="filter-btn" onclick="filterRuns('low')">Score &lt; 5</button>
  <button class="filter-btn" onclick="filterRuns('gated')">Gated</button>
  <button class="filter-btn" onclick="filterRuns('failed')">Failed</button>
</div>

{% for r in runs %}
<div class="run" data-score="{{ r.score or 0 }}" data-gated="{{ r.gated }}" data-status="{{ r.status }}">
  <div class="run-header" onclick="toggleRun(this)">
    <span class="chevron">&#x25B6;</span>
    {% if r.status == 'Succeeded' %}
      <span class="run-status-badge succeeded"><span class="dot"></span>Succeeded</span>
    {% elif r.status == 'Failed' %}
      <span class="run-status-badge failed"><span class="dot"></span>Failed</span>
    {% else %}
      <span class="run-status-badge gated"><span class="dot"></span>Gated</span>
    {% endif %}
    <span class="run-uid">{{ r.uid }}</span>
    <span class="run-name">{{ r.deal_name or r.subject[:60] }}</span>
    <div class="run-meta">
      {% if r.notes_reason %}<span class="notes-sig">{{ r.notes_reason }}</span>{% endif %}
      {% if r.tt == 'Mark-to-Market' %}<span class="badge mtm">Mark-to-Market</span>
      {% elif r.tt == 'Value-add' %}<span class="badge va">Value-add</span>
      {% elif r.tt == 'Development' %}<span class="badge dev">Development</span>
      {% elif r.tt == 'Core' %}<span class="badge core">Core</span>
      {% elif r.tt == 'Lease-up' %}<span class="badge lease">Lease-up</span>{% endif %}
      {% if r.asset_class == 'Senior Housing' %}<span class="badge sh">Senior</span>{% endif %}
      {% if r.rent_upside %}<span class="badge rent-up">Rent Upside</span>{% endif %}
      {% if not r.gated and r.score is not none %}
        <span class="run-score {% if r.score >= 7 %}g{% elif r.score >= 5 %}y{% else %}r{% endif %}">{{ r.score }}</span>
      {% endif %}
      <span class="run-steps-count">{{ r.step_count }} steps</span>
    </div>
    <span class="run-time">{{ r.timestamp or '' }}</span>
  </div>
  <div class="run-detail" id="detail-{{ r.uid }}">
    {% for s in r.steps %}
    <div class="step">
      <span class="step-icon">{{ s.icon }}</span>
      <span class="step-pip {{ s.status }}"></span>
      <div class="step-body">
        <div class="step-label" style="display:flex;align-items:center">
          {{ s.label }}
          {% if s.label == 'Email received' and r.scraped_text %}
            <span class="step-expand" onclick="toggleRaw(this)">&blacktriangleright; view email body</span>
          {% elif s.label == 'OCR processed' and r.ocr_clean %}
            <span class="step-expand" onclick="toggleRaw(this)">&blacktriangleright; view plain text</span>
          {% elif s.label == 'Claude extraction' and r.extracted %}
            <span class="step-expand" onclick="toggleRaw(this)">&blacktriangleright; view fields</span>
          {% elif s.label == 'Scoring' %}
            <span class="step-expand" onclick="toggleRaw(this)">&blacktriangleright; view notes</span>
          {% endif %}
        </div>
        <div class="step-detail">{{ s.detail }}</div>
        {% if s.label == 'Email received' and r.scraped_text %}
        <div class="step-raw">{{ r.scraped_text }}</div>
        {% elif s.label == 'OCR processed' and r.ocr_clean %}
        <div class="step-raw">{{ r.ocr_clean }}</div>
        {% elif s.label == 'Claude extraction' and r.extracted %}
        <div class="step-raw">{% for k, v in r.extracted.items() %}<span class="field-key">{{ k }}</span>: <span class="field-val">{{ v }}</span>
{% endfor %}</div>
        {% elif s.label == 'Scoring' and r.notes %}
        <div class="step-raw"><span class="field-key">notes</span>: <span class="field-val">{{ r.notes }}</span></div>
        {% endif %}
      </div>
    </div>
    {% endfor %}

    {% if r.scraped_text or r.ocr_clean %}
    <div class="raw-tabs" id="tabs-{{ r.uid }}">
      {% if r.scraped_text %}<div class="raw-tab" onclick="showPanel('{{ r.uid }}','scraped')">Email Body</div>{% endif %}
      {% if r.ocr_clean %}<div class="raw-tab" onclick="showPanel('{{ r.uid }}','ocr')">OCR Plain Text</div>{% endif %}
      {% if r.ocr_text %}<div class="raw-tab" onclick="showPanel('{{ r.uid }}','raw')">OCR Raw</div>{% endif %}
    </div>
    {% if r.scraped_text %}<div class="raw-panel" id="panel-{{ r.uid }}-scraped">{{ r.scraped_text }}</div>{% endif %}
    {% if r.ocr_clean %}<div class="raw-panel" id="panel-{{ r.uid }}-ocr">{{ r.ocr_clean }}</div>{% endif %}
    {% if r.ocr_text %}<div class="raw-panel" id="panel-{{ r.uid }}-raw">{{ r.ocr_text }}</div>{% endif %}
    {% endif %}
  </div>
</div>
{% endfor %}
</div><!-- .content -->

<script>
function toggleRun(hdr){
  const detail=hdr.nextElementSibling;
  const chev=hdr.querySelector('.chevron');
  detail.classList.toggle('open');
  chev.classList.toggle('open');
}
function toggleRaw(el){
  const raw=el.closest('.step-body').querySelector('.step-raw');
  if(raw){raw.classList.toggle('open');el.innerHTML=raw.classList.contains('open')?'&#9662; hide':'&#9656; view';}
}
function showPanel(uid,type){
  const tabs=document.getElementById('tabs-'+uid);
  tabs.querySelectorAll('.raw-tab').forEach(t=>t.classList.remove('active'));
  event.target.classList.add('active');
  ['scraped','ocr','raw'].forEach(t=>{
    const p=document.getElementById('panel-'+uid+'-'+t);
    if(p)p.classList.toggle('open',t===type);
  });
}
function filterRuns(type){
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('.run').forEach(r=>{
    const sc=parseFloat(r.dataset.score)||0;
    const gated=r.dataset.gated==='True';
    const st=r.dataset.status;
    let show=true;
    if(type==='succeeded')show=st==='Succeeded';
    else if(type==='high')show=st==='Succeeded'&&sc>=7;
    else if(type==='mid')show=st==='Succeeded'&&sc>=5&&sc<7;
    else if(type==='low')show=st==='Succeeded'&&sc<5;
    else if(type==='gated')show=st==='Gated';
    else if(type==='failed')show=st==='Failed';
    r.style.display=show?'':'none';
  });
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Flask route
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    import time
    runs = parse_runs()
    enrich_from_db(runs)
    scored = [r for r in runs if r["status"] == "Succeeded" and r["score"] is not None]
    return render_template_string(TEMPLATE,
        runs=runs,
        scored=len(scored),
        high=sum(1 for r in scored if r["score"] >= 7),
        mid=sum(1 for r in scored if 5 <= r["score"] < 7),
        low=sum(1 for r in scored if r["score"] < 5),
        gated=sum(1 for r in runs if r["status"] == "Gated"),
        watcher_on=watcher_alive(),
        now=time.strftime("%I:%M %p"),
    )


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    # --web flag falls back to plain browser mode (no pywebview)
    if "--web" in sys.argv:
        app.run(host="127.0.0.1", port=5050, debug=False)
    else:
        import webview

        def start_flask():
            app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)

        t = threading.Thread(target=start_flask, daemon=True)
        t.start()

        webview.create_window(
            "Deal Sifta — Run History",
            "http://127.0.0.1:5050",
            width=1280,
            height=860,
            min_size=(900, 600),
        )
        webview.start()
