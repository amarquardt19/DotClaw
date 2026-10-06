import argparse
import csv
import datetime as dt
import json
import os
import subprocess
from typing import Any, Dict, List, Optional


def _run_himalaya_envelope_list(*, folder: str, page: int, page_size: int, config_path: Optional[str]) -> List[Dict[str, Any]]:
    cmd = [
        "himalaya",
        "envelope",
        "list",
        "--folder",
        folder,
        "--page",
        str(page),
        "--page-size",
        str(page_size),
        "--output",
        "json",
        "order by date desc",
    ]
    if config_path:
        cmd.extend(["--config", config_path])

    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"himalaya envelope list failed (exit {proc.returncode}):\n{proc.stderr.strip()}")

    out = proc.stdout.strip()
    if not out:
        return []

    data = json.loads(out)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected himalaya output type: {type(data)}")

    return data


def _safe_get(d: Dict[str, Any], key: str) -> Any:
    return d.get(key)


def _fmt_date(s: Optional[str]) -> str:
    if not s:
        return ""
    return s


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


def write_reports(*, rows: List[Dict[str, Any]], out_csv: str, out_md: str) -> None:
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    os.makedirs(os.path.dirname(out_md), exist_ok=True)

    fieldnames = [
        "id",
        "date",
        "from_addr",
        "from_name",
        "to_addr",
        "to_name",
        "subject",
        "flags",
        "has_attachment",
    ]

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Markdown tables become unwieldy for huge inboxes. We write all rows, but subjects are truncated.
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(f"# INBOX Index\n\n")
        f.write(f"Generated: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n\n")
        f.write(f"Rows: {len(rows)}\n\n")
        f.write("| Date | From | Subject | Attach? | ID |\n")
        f.write("|---|---|---|---:|---:|\n")
        for r in rows:
            date = r.get("date", "")
            from_addr = r.get("from_addr", "")
            subj = _truncate(r.get("subject", ""), 120)
            attach = "yes" if r.get("has_attachment") else ""
            mid = r.get("id", "")
            f.write(f"| {date} | {from_addr} | {subj} | {attach} | {mid} |\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--max-pages", type=int, default=0, help="0 = no limit")
    ap.add_argument(
        "--max-emails",
        type=int,
        default=0,
        help="0 = no limit. Stop after collecting this many envelopes (most recent first).",
    )
    ap.add_argument("--config", default=os.environ.get("HIMALAYA_CONFIG"), help="Optional himalaya config path")
    ap.add_argument("--out-dir", default=os.path.expanduser("~/.openclaw/workspace/reports"))
    args = ap.parse_args()

    out_csv = os.path.join(args.out_dir, "inbox_index.csv")
    out_md = os.path.join(args.out_dir, "inbox_index.md")

    page = 1
    all_envs: List[Dict[str, Any]] = []
    while True:
        if args.max_pages and page > args.max_pages:
            break
        if args.max_emails and len(all_envs) >= args.max_emails:
            break

        print(f"Fetching {args.folder} page {page} (pageSize={args.page_size})...", flush=True)
        envs = _run_himalaya_envelope_list(
            folder=args.folder,
            page=page,
            page_size=args.page_size,
            config_path=args.config,
        )
        if not envs:
            break
        all_envs.extend(envs)
        if args.max_emails and len(all_envs) >= args.max_emails:
            all_envs = all_envs[: args.max_emails]
            break

        # If Himalaya returns fewer than the requested page size, we've reached the end.
        if len(envs) < args.page_size:
            break
        page += 1

    rows: List[Dict[str, Any]] = []
    for e in all_envs:
        from_obj = _safe_get(e, "from") or {}
        to_obj = _safe_get(e, "to") or {}
        rows.append(
            {
                "id": str(_safe_get(e, "id") or ""),
                "date": _fmt_date(_safe_get(e, "date")),
                "from_addr": str(_safe_get(from_obj, "addr") or ""),
                "from_name": str(_safe_get(from_obj, "name") or ""),
                "to_addr": str(_safe_get(to_obj, "addr") or ""),
                "to_name": str(_safe_get(to_obj, "name") or ""),
                "subject": str(_safe_get(e, "subject") or ""),
                "flags": ",".join([str(x) for x in (_safe_get(e, "flags") or [])]),
                "has_attachment": bool(_safe_get(e, "has_attachment")),
            }
        )

    write_reports(rows=rows, out_csv=out_csv, out_md=out_md)

    print(f"Wrote: {out_csv}")
    print(f"Wrote: {out_md}")
    print(f"Rows: {len(rows)}")


if __name__ == "__main__":
    main()
