"""
DealSifta QA Bot — Grading Dashboard

Lightweight web dashboard to track bot accuracy over time:
- Overall accuracy + per-category (extraction, scoring, missing_inputs)
- Per-deal breakdown
- Correction timeline
- Bot readiness gauge (is it ready for autonomous mode?)

Run: python3 qa_dashboard.py
Open: http://localhost:8501
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List

# Load env
_ENV = Path(__file__).resolve().parent / ".env"
if _ENV.exists():
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _get_db():
    import pymssql
    return pymssql.connect(
        os.environ["AZURE_SQL_SERVER"],
        os.environ["AZURE_SQL_USER"],
        os.environ["AZURE_SQL_PASSWORD"],
        os.environ["AZURE_SQL_DATABASE"],
    )


def _fetch_dashboard_data() -> Dict[str, Any]:
    """Query all corrections and grades for the dashboard."""
    c = _get_db()
    cur = c.cursor()

    cur.execute(
        "SELECT id, uid, deal_name, field_name, old_value, new_value, "
        "deal_context, corrected_at FROM scoring_corrections ORDER BY id"
    )
    rows = cur.fetchall()

    # Fetch current (post-correction) scores from raw_deal_emails
    current_scores = {}  # {uid: {score, score_breakdown}}
    try:
        cur.execute(
            "SELECT uid, extracted_json FROM raw_deal_emails "
            "WHERE extracted_json IS NOT NULL"
        )
        for uid_row in cur.fetchall():
            try:
                ej = json.loads(uid_row[1])
                current_scores[uid_row[0]] = {
                    "score": ej.get("score"),
                    "breakdown": ej.get("score_breakdown"),
                }
            except Exception:
                pass
    except Exception:
        pass
    c.close()

    corrections = []
    grades = []
    final_grades = {}  # {deal_name: {corrected_fields, num_corrections, ...}}
    self_grades = {}   # legacy — keep for backward compat
    score_corrections = {}  # {deal_name: {old_score, new_score}} from explicit score corrections
    for row in rows:
        entry = {
            "id": row[0], "uid": row[1], "deal_name": row[2],
            "field_name": row[3], "old_value": row[4], "new_value": row[5],
            "context": row[6], "corrected_at": row[7].isoformat() if row[7] else None,
        }
        if row[3] == "_deal_final_grade":
            try:
                ctx = json.loads(row[6]) if row[6] else {}
                final_grades[row[2]] = {
                    "uid": row[1],
                    "corrected_fields": ctx.get("corrected_fields", []),
                    "num_corrections": ctx.get("num_corrections", 0),
                    "field_changes": ctx.get("field_changes", {}),
                    "predicted_fields": ctx.get("predicted_fields", []),
                    "predictions_correct": ctx.get("predictions_correct", []),
                    "predictions_wrong": ctx.get("predictions_wrong", []),
                    "corrections_missed": ctx.get("corrections_missed", []),
                    "first_pass_score": ctx.get("first_pass_score"),
                    "first_pass_breakdown": ctx.get("first_pass_breakdown"),
                    "final_score": ctx.get("final_score"),
                    "final_breakdown": ctx.get("final_breakdown"),
                    "bot_proposed_score": ctx.get("bot_proposed_score"),
                    "bot_proposed_breakdown": ctx.get("bot_proposed_breakdown"),
                }
            except Exception:
                pass
        elif row[3] == "_bot_self_grade":
            try:
                ctx = json.loads(row[6]) if row[6] else {}
                self_grades[row[2]] = {
                    "uid": row[1],
                    "total": ctx.get("total_checks", 0),
                    "passed": ctx.get("checks_passed", 0),
                    "pct": int(row[5].replace("%", "")) if "%" in str(row[5]) else 0,
                    "fields": ctx.get("fields", {}),
                    "self_assessment": ctx.get("self_assessment", ""),
                }
            except Exception:
                pass
        elif row[3] and row[3].startswith("_bot_grade"):
            grades.append(entry)
        elif row[3] == "score":
            # Explicit score correction — track old/new for reconstruction
            try:
                score_corrections[row[2]] = {
                    "old_score": float(row[4]) if row[4] else None,
                    "new_score": float(row[5]) if row[5] else None,
                }
            except (ValueError, TypeError):
                pass
            corrections.append(entry)
        elif row[3] and not row[3].startswith("_"):
            corrections.append(entry)

    # Per-category accuracy from user grades
    cat_stats = {}
    for g in grades:
        cat = g["field_name"].replace("_bot_grade_", "").replace("_bot_grade", "overall")
        cat_stats.setdefault(cat, {"correct": 0, "wrong": 0})
        cat_stats[cat][g["new_value"]] = cat_stats[cat].get(g["new_value"], 0) + 1

    # Per-deal stats — prefer _deal_final_grade (user corrections), fall back to legacy
    deal_stats = {}

    # Seed from final grades (new system: based on user corrections)
    for dn, fg in final_grades.items():
        corrected = fg["corrected_fields"]
        n = fg["num_corrections"]
        # Build field map: corrected fields = wrong, everything else = ok
        all_fields = ["deal_name", "city", "state", "units", "vintage",
                      "avg_rent_monthly", "guidance_price", "in_place_cap_rate",
                      "occupancy", "transaction_type", "asset_class", "product",
                      "renovation_year", "renovated", "going_in_cash_yield"]
        field_map = {f: (f not in corrected) for f in all_fields}
        # Also add corrected fields that might not be in the standard list
        for f in corrected:
            field_map[f] = False

        # Bot accuracy: same formula as pipeline but bot errors = false alarms
        n_false = len(fg.get("predictions_wrong", []))
        bot_errors = n_false  # fields bot would have gotten wrong
        bot_pct = round((len(all_fields) - bot_errors) / len(all_fields) * 100, 1) if bot_errors <= len(all_fields) else 0

        # Score tracking: first-pass from grade context, final from grade ctx OR DB
        fp_score = fg.get("first_pass_score")
        fp_breakdown = fg.get("first_pass_breakdown")
        final_score = fg.get("final_score")
        final_breakdown = fg.get("final_breakdown")
        # Fall back to DB current score if grade ctx doesn't have final
        if final_score is None:
            cs = current_scores.get(fg["uid"], {})
            final_score = cs.get("score")
            final_breakdown = cs.get("breakdown")

        # Fallback: reconstruct from explicit score corrections
        dn_sc = score_corrections.get(dn)
        if fp_score is None and dn_sc:
            fp_score = dn_sc.get("old_score")
        if final_score is None and dn_sc:
            final_score = dn_sc.get("new_score")
        # If still no final score, use first-pass (no corrections changed score)
        if final_score is None and fp_score is not None:
            final_score = fp_score

        deal_stats[dn] = {
            "uid": fg["uid"],
            "corrections": n,
            "corrected_fields": corrected,
            "field_changes": fg.get("field_changes", {}),
            "final_pct": round((len(all_fields) - n) / len(all_fields) * 100, 1) if n <= len(all_fields) else 0,
            "bot_pct": bot_pct,
            "categories": {},
            "fields": field_map,
            "predicted_fields": fg.get("predicted_fields", []),
            "predictions_correct": fg.get("predictions_correct", []),
            "predictions_wrong": fg.get("predictions_wrong", []),
            "corrections_missed": fg.get("corrections_missed", []),
            "first_pass_score": fp_score,
            "first_pass_breakdown": fp_breakdown,
            "final_score": final_score,
            "final_breakdown": final_breakdown,
            "bot_proposed_score": fg.get("bot_proposed_score"),
            "bot_proposed_breakdown": fg.get("bot_proposed_breakdown"),
        }

    # Seed from legacy self-grades for deals without final grades
    for dn, sg in self_grades.items():
        if dn not in deal_stats:
            deal_stats[dn] = {
                "uid": sg["uid"],
                "corrections": 0,
                "corrected_fields": [],
                "field_changes": {},
                "final_pct": sg["pct"],
                "categories": {},
                "fields": sg.get("fields", {}),
                "self_assessment": sg.get("self_assessment", ""),
            }

    # Count user_correction entries per deal
    for cr in corrections:
        dn = cr["deal_name"] or "Unknown"
        if dn not in deal_stats:
            deal_stats[dn] = {
                "uid": cr["uid"], "corrections": 0, "corrected_fields": [],
                "field_changes": {}, "final_pct": 0,
                "categories": {}, "fields": {}, "self_assessment": "",
            }
        cat = (cr["field_name"] or "").replace("user_correction_", "") or "other"
        deal_stats[dn]["categories"][cat] = deal_stats[dn]["categories"].get(cat, 0) + 1

    # Backfill scores for ALL deals (not just final_grades path)
    for dn, ds in deal_stats.items():
        uid = ds.get("uid")
        # If no first_pass_score/final_score yet, try to fill from DB + corrections
        if "first_pass_score" not in ds:
            ds["first_pass_score"] = None
            ds["first_pass_breakdown"] = None
        if "final_score" not in ds:
            ds["final_score"] = None
            ds["final_breakdown"] = None
        # Try current_scores from raw_deal_emails
        if ds["final_score"] is None and uid in current_scores:
            ds["final_score"] = current_scores[uid].get("score")
            ds["final_breakdown"] = current_scores[uid].get("breakdown")
        # Try score_corrections for first-pass reconstruction
        dn_sc = score_corrections.get(dn)
        if ds["first_pass_score"] is None and dn_sc:
            ds["first_pass_score"] = dn_sc.get("old_score")
        if ds["final_score"] is None and dn_sc:
            ds["final_score"] = dn_sc.get("new_score")
        # If no corrections changed score, first-pass = final
        if ds["first_pass_score"] is None and ds["final_score"] is not None:
            ds["first_pass_score"] = ds["final_score"]

    # Filter to CBRE scraper deals only (uid >= 50000)
    deal_stats = {dn: ds for dn, ds in deal_stats.items()
                  if ds.get("uid") is not None and ds["uid"] >= 50000}

    # Running averages
    graded_deals = [ds for ds in deal_stats.values() if ds.get("fields")]
    if graded_deals:
        avg_final = sum(ds["final_pct"] for ds in graded_deals) / len(graded_deals)
    else:
        avg_final = None

    # Overall from user grades
    total_correct = sum(v.get("correct", 0) for v in cat_stats.values())
    total_wrong = sum(v.get("wrong", 0) for v in cat_stats.values())
    total_user_grades = total_correct + total_wrong
    user_grade_pct = (total_correct / total_user_grades * 100) if total_user_grades > 0 else None

    # Bot prediction accuracy: avg bot_pct across graded deals (same formula as pipeline)
    bot_graded = [ds for ds in deal_stats.values() if ds.get("bot_pct") is not None and ds.get("fields")]
    bot_pred_wrong_total = sum(len(ds.get("predictions_wrong", [])) for ds in deal_stats.values())
    bot_pred_missed_total = sum(len(ds.get("corrections_missed", [])) for ds in deal_stats.values())
    avg_bot_pct = sum(ds["bot_pct"] for ds in bot_graded) / len(bot_graded) if bot_graded else None

    return {
        "corrections": corrections,
        "grades": grades,
        "cat_stats": cat_stats,
        "deal_stats": deal_stats,
        "total_corrections": len(corrections),
        "total_user_grades": total_user_grades,
        "user_grade_pct": user_grade_pct,
        "total_correct": total_correct,
        "total_wrong": total_wrong,
        "avg_final": avg_final,
        "deals_reviewed": len(graded_deals),
        "avg_bot_pct": avg_bot_pct,
        "bot_pred_wrong_total": bot_pred_wrong_total,
        "bot_pred_missed_total": bot_pred_missed_total,
        "bot_deals_graded": len(bot_graded),
        "generated_at": datetime.utcnow().isoformat(),
    }


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DealSifta QA Bot — Grading Dashboard</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e4e4e7; --muted: #71717a; --accent: #6366f1;
    --green: #22c55e; --red: #ef4444; --yellow: #eab308; --blue: #3b82f6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text); padding: 24px;
    max-width: 1200px; margin: 0 auto;
  }
  h1 { font-size: 24px; margin-bottom: 8px; }
  h2 { font-size: 18px; margin-bottom: 12px; color: var(--muted); }
  h3 { font-size: 15px; margin-bottom: 8px; color: var(--accent); text-transform: uppercase; letter-spacing: 1px; }
  .subtitle { color: var(--muted); font-size: 14px; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin-bottom: 32px; }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px;
  }
  .stat-value { font-size: 36px; font-weight: 700; margin: 8px 0; }
  .stat-label { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  .green { color: var(--green); }
  .red { color: var(--red); }
  .yellow { color: var(--yellow); }
  .blue { color: var(--blue); }
  .gauge-container { text-align: center; padding: 20px; }
  .gauge {
    width: 160px; height: 80px; position: relative; margin: 0 auto 12px;
    overflow: hidden;
  }
  .gauge-bg, .gauge-fill {
    width: 160px; height: 160px; border-radius: 50%;
    border: 16px solid transparent; position: absolute; top: 0; left: 0;
  }
  .gauge-bg { border-color: var(--border); clip-path: polygon(0 0, 100% 0, 100% 50%, 0 50%); }
  .gauge-fill {
    border-color: var(--accent);
    clip-path: polygon(0 0, 100% 0, 100% 50%, 0 50%);
    transform-origin: center center;
  }
  .gauge-label { font-size: 28px; font-weight: 700; }
  .gauge-sub { font-size: 12px; color: var(--muted); }
  .progress-bar {
    height: 8px; background: var(--border); border-radius: 4px;
    overflow: hidden; margin: 8px 0;
  }
  .progress-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 14px; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 12px; font-weight: 500;
  }
  .badge-extraction { background: #3b82f620; color: var(--blue); }
  .badge-scoring { background: #6366f120; color: var(--accent); }
  .badge-missing { background: #eab30820; color: var(--yellow); }
  .badge-correct { background: #22c55e20; color: var(--green); }
  .badge-wrong { background: #ef444420; color: var(--red); }
  .readiness-section { text-align: center; margin-bottom: 32px; }
  .readiness-bar {
    height: 24px; background: var(--border); border-radius: 12px;
    overflow: hidden; margin: 12px auto; max-width: 600px; position: relative;
  }
  .readiness-fill {
    height: 100%; border-radius: 12px; transition: width 0.5s;
    background: linear-gradient(90deg, var(--red), var(--yellow), var(--green));
  }
  .readiness-target {
    position: absolute; top: 0; height: 100%; width: 2px; background: white;
    opacity: 0.5;
  }
  .readiness-label { font-size: 13px; color: var(--muted); margin-top: 4px; }
  .empty-state { text-align: center; padding: 40px; color: var(--muted); }
  .timestamp { color: var(--muted); font-size: 12px; text-align: right; margin-top: 24px; }
</style>
</head>
<body>

<h1>🤖 DealSifta QA Bot</h1>
<p class="subtitle">Grading Dashboard — Training Phase</p>

<!-- Readiness Gauge -->
<div class="card readiness-section" id="readiness-section">
  <h3>Deployment Readiness</h3>
  <div class="readiness-bar">
    <div class="readiness-fill" id="readiness-fill"></div>
    <div class="readiness-target" style="left: 95%"></div>
  </div>
  <div class="readiness-label">
    <span id="readiness-pct">—</span> accuracy · Target: 95% for autonomous deployment
  </div>
</div>

<!-- Summary Cards -->
<div class="grid" id="summary-cards"></div>

<!-- Category Breakdown -->
<div class="card" style="margin-bottom: 32px;" id="category-section">
  <h3>Accuracy by Category</h3>
  <div id="category-bars"></div>
</div>

<!-- Per-Deal Breakdown -->
<div class="card" style="margin-bottom: 32px;">
  <h3>Per-Deal Performance</h3>
  <table>
    <thead><tr><th>Deal</th><th>UID</th><th>Score</th><th>Corrections</th><th>Pipeline %</th><th>Bot %</th><th>Fields</th><th>Changes Applied</th><th>Bot Predictions</th></tr></thead>
    <tbody id="deal-table"></tbody>
  </table>
</div>

<!-- Correction Timeline -->
<div class="card">
  <h3>Correction Log</h3>
  <table>
    <thead><tr><th>Time</th><th>Deal</th><th>Type</th><th>Correction</th><th>Grade</th></tr></thead>
    <tbody id="timeline-table"></tbody>
  </table>
</div>

<p class="timestamp" id="timestamp"></p>

<script>
const DATA = __DATA_PLACEHOLDER__;

function init() {
  renderReadiness();
  renderSummary();
  renderCategories();
  renderDeals();
  renderTimeline();
  document.getElementById('timestamp').textContent =
    'Generated: ' + new Date(DATA.generated_at + 'Z').toLocaleString();
}

function renderReadiness() {
  const pct = DATA.avg_final;
  const fill = document.getElementById('readiness-fill');
  const label = document.getElementById('readiness-pct');
  if (pct !== null) {
    fill.style.width = Math.min(pct, 100) + '%';
    label.textContent = pct.toFixed(1) + '%';
  } else {
    fill.style.width = '0%';
    label.textContent = 'No reviews yet — start training!';
  }
}

function renderSummary() {
  const container = document.getElementById('summary-cards');
  const finalPct = DATA.avg_final;
  const botPct = DATA.avg_bot_pct;
  const botWrong = DATA.bot_pred_wrong_total || 0;
  const botMissed = DATA.bot_pred_missed_total || 0;
  const cards = [
    {
      label: 'Pipeline Accuracy',
      value: finalPct !== null ? finalPct.toFixed(1) + '%' : '—',
      color: finalPct >= 90 ? 'green' : finalPct >= 70 ? 'yellow' : 'red',
      sub: finalPct !== null ? '% fields needing no correction · ' + DATA.deals_reviewed + ' deals' : 'no reviews yet'
    },
    {
      label: 'Bot Accuracy',
      value: botPct !== null ? botPct.toFixed(1) + '%' : '—',
      color: botPct === null ? 'blue' : botPct >= 90 ? 'green' : botPct >= 70 ? 'yellow' : 'red',
      sub: botPct !== null
        ? `${botWrong} false alarms · ${botMissed} missed · ${DATA.bot_deals_graded} deals`
        : 'no predictions yet'
    },
    {
      label: 'Corrections Made',
      value: DATA.total_corrections,
      color: DATA.total_corrections > 0 ? 'yellow' : 'green',
      sub: DATA.total_corrections + ' issues found'
    },
    {
      label: 'Deals Reviewed',
      value: DATA.deals_reviewed || Object.keys(DATA.deal_stats).length,
      color: 'blue',
      sub: 'unique deals'
    }
  ];
  container.innerHTML = cards.map(c => `
    <div class="card">
      <div class="stat-label">${c.label}</div>
      <div class="stat-value ${c.color}">${c.value}</div>
      <div class="stat-label">${c.sub}</div>
    </div>
  `).join('');
}

function renderCategories() {
  const container = document.getElementById('category-bars');
  const cats = ['extraction', 'scoring', 'missing_inputs'];
  const labels = { extraction: 'Extraction', scoring: 'Scoring', missing_inputs: 'Missing Inputs' };
  const colors = { extraction: '#3b82f6', scoring: '#6366f1', missing_inputs: '#eab308' };

  let html = '';
  for (const cat of cats) {
    const stats = DATA.cat_stats[cat];
    if (!stats) {
      html += `
        <div style="margin-bottom: 16px;">
          <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
            <span>${labels[cat]}</span>
            <span class="muted" style="color:var(--muted)">No grades</span>
          </div>
          <div class="progress-bar"><div class="progress-fill" style="width:0%;background:${colors[cat]}"></div></div>
        </div>`;
      continue;
    }
    const correct = stats.correct || 0;
    const wrong = stats.wrong || 0;
    const total = correct + wrong;
    const pct = total > 0 ? (correct / total * 100) : 0;
    html += `
      <div style="margin-bottom: 16px;">
        <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
          <span>${labels[cat]}</span>
          <span class="${pct >= 90 ? 'green' : pct >= 70 ? 'yellow' : 'red'}">${pct.toFixed(0)}% (${correct}/${total})</span>
        </div>
        <div class="progress-bar"><div class="progress-fill" style="width:${pct}%;background:${colors[cat]}"></div></div>
      </div>`;
  }
  container.innerHTML = html || '<div class="empty-state">No category grades yet</div>';
}

function renderDeals() {
  const tbody = document.getElementById('deal-table');
  const deals = Object.entries(DATA.deal_stats);
  if (!deals.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-state">No deals reviewed yet</td></tr>';
    return;
  }
  tbody.innerHTML = deals.map(([name, d]) => {
    // Corrections count
    const n = d.corrections || 0;
    const corrColor = n === 0 ? 'green' : n <= 2 ? 'yellow' : 'red';
    // Pipeline accuracy: % of fields that needed no correction
    const pct = d.final_pct || 0;
    const pctColor = pct >= 90 ? 'green' : pct >= 70 ? 'yellow' : 'red';
    // Field badges: green = correct (no correction needed), red = user corrected
    const fields = d.fields || {};
    const fieldBadges = Object.entries(fields).map(([f, ok]) =>
      `<span class="badge ${ok ? 'badge-correct' : 'badge-wrong'}" style="margin:1px;font-size:10px;">${f}: ${ok ? '✓' : '✗'}</span>`
    ).join(' ');
    // Show what was changed
    const changes = d.field_changes || {};
    const changeStr = Object.entries(changes).map(([f, v]) => `${f}→${v}`).join(', ');
    const changeCell = changeStr
      ? `<span style="font-size:11px;color:var(--yellow)">${changeStr}</span>`
      : '<span style="color:var(--muted)">No corrections</span>';
    // Bot prediction accuracy
    const pred = d.predicted_fields || [];
    const pCorr = d.predictions_correct || [];
    const pWrong = d.predictions_wrong || [];
    const missed = d.corrections_missed || [];
    let predCell = '';
    if (!pred.length && !missed.length && !n) {
      predCell = '<span style="color:var(--green);font-size:11px">✅ Clean</span>';
    } else if (!pred.length && !missed.length) {
      predCell = '<span style="color:var(--muted);font-size:11px">No predictions</span>';
    } else {
      const parts = [];
      if (pCorr.length) parts.push(`<span style="color:var(--green);font-size:11px">✅ ${pCorr.join(', ')}</span>`);
      if (pWrong.length) parts.push(`<span style="color:var(--red);font-size:11px">❌ ${pWrong.join(', ')}</span>`);
      if (missed.length) parts.push(`<span style="color:var(--yellow);font-size:11px">🔴 missed: ${missed.join(', ')}</span>`);
      if (!parts.length) parts.push('<span style="color:var(--muted);font-size:11px">No predictions</span>');
      predCell = parts.join('<br>');
    }
    // Bot accuracy per deal
    const bpct = d.bot_pct != null ? d.bot_pct : 100;
    const bpctColor = bpct >= 90 ? 'green' : bpct >= 70 ? 'yellow' : 'red';
    // Score: first-pass → bot proposed → final
    const fp = d.first_pass_score;
    const fs = d.final_score;
    const bp = d.bot_proposed_score;
    let scoreCell = '';
    // Prefer showing bot proposed score if it differs from first-pass
    const effectiveTarget = fs != null ? fs : bp;
    if (fp != null && effectiveTarget != null && fp !== effectiveTarget) {
      const delta = (effectiveTarget - fp).toFixed(1);
      const deltaColor = delta > 0 ? 'green' : 'red';
      const label = fs != null ? '' : ' <span style="font-size:9px;color:var(--muted)">(bot)</span>';
      scoreCell = `<span style="color:var(--muted)">${fp}</span> → <span class="${deltaColor}" style="font-weight:700">${effectiveTarget}</span>${label} <span class="${deltaColor}" style="font-size:10px">(${delta > 0 ? '+' : ''}${delta})</span>`;
    } else if (fp != null && effectiveTarget != null) {
      scoreCell = `<span style="font-weight:700">${fp}</span> → <span style="font-weight:700">${effectiveTarget}</span> <span style="color:var(--muted);font-size:10px">(no change)</span>`;
    } else if (fs != null) {
      scoreCell = `<span style="font-weight:700">${fs}</span>`;
    } else if (bp != null && fp != null) {
      const delta = (bp - fp).toFixed(1);
      const deltaColor = delta > 0 ? 'green' : 'red';
      scoreCell = `<span style="color:var(--muted)">${fp}</span> → <span class="${deltaColor}" style="font-weight:700">${bp}</span> <span style="font-size:9px;color:var(--muted)">(bot)</span>`;
    } else if (fp != null) {
      scoreCell = `<span style="font-weight:700">${fp}</span>`;
    } else {
      scoreCell = '<span style="color:var(--muted)">—</span>';
    }
    return `<tr>
      <td>${name}</td><td>${d.uid}</td>
      <td>${scoreCell}</td>
      <td><span class="${corrColor}">${n}</span></td>
      <td><span class="${pctColor}">${pct}%</span></td>
      <td><span class="${bpctColor}">${bpct}%</span></td>
      <td style="max-width:300px;line-height:1.8">${fieldBadges || '—'}</td>
      <td style="max-width:200px">${changeCell}</td>
      <td style="max-width:200px">${predCell}</td>
    </tr>`;
  }).join('');
}

function renderTimeline() {
  const tbody = document.getElementById('timeline-table');
  // Merge corrections and grades, sort by time
  const all = [
    ...DATA.corrections.map(c => ({...c, type: 'correction'})),
    ...DATA.grades.map(g => ({...g, type: 'grade'}))
  ].sort((a, b) => (a.id || 0) - (b.id || 0));

  if (!all.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No activity yet</td></tr>';
    return;
  }

  tbody.innerHTML = all.map(e => {
    const time = e.corrected_at ? new Date(e.corrected_at).toLocaleDateString() : '—';
    const deal = e.deal_name || '—';
    let typeBadge, detail, grade;
    if (e.type === 'correction') {
      const cat = (e.field_name || '').replace('user_correction_', '');
      const cls = cat === 'extraction' ? 'badge-extraction' : cat === 'scoring' ? 'badge-scoring' : 'badge-missing';
      typeBadge = `<span class="badge ${cls}">${cat}</span>`;
      detail = (e.new_value || '').substring(0, 80);
      grade = '';
    } else {
      typeBadge = '<span class="badge" style="background:#ffffff10;color:var(--muted)">grade</span>';
      detail = e.field_name.replace('_bot_grade_', '');
      const isCorrect = e.new_value === 'correct';
      grade = `<span class="badge ${isCorrect ? 'badge-correct' : 'badge-wrong'}">${isCorrect ? '👍' : '👎'}</span>`;
    }
    return `<tr><td>${time}</td><td>${deal}</td><td>${typeBadge}</td><td>${detail}</td><td>${grade}</td></tr>`;
  }).join('');
}

init();
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            data = _fetch_dashboard_data()
            html = _HTML.replace("__DATA_PLACEHOLDER__", json.dumps(data, default=str))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
        elif self.path == "/api/data":
            data = _fetch_dashboard_data()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logs


if __name__ == "__main__":
    port = 8501
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"[qa-dashboard] Running at http://localhost:{port}", flush=True)
    server.serve_forever()
