import email
import hashlib
import imaplib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv


STATE_PATH = Path(__file__).with_name("state.json")


@dataclass
class Config:
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    mailbox: str
    poll_seconds: int
    openai_api_key: Optional[str]
    openai_ocr_model: str
    llm_provider: str
    anthropic_api_key: Optional[str]
    anthropic_ocr_model: str
    openclaw_gateway_url: str
    openclaw_gateway_token: Optional[str]
    openclaw_session_id: str


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return int(v)


def load_config() -> Config:
    load_dotenv()

    imap_host = os.getenv("FASTMAIL_IMAP_HOST", "imap.fastmail.com")
    imap_port = _env_int("FASTMAIL_IMAP_PORT", 993)
    imap_user = os.getenv("FASTMAIL_IMAP_USER", "").strip()
    imap_password = os.getenv("FASTMAIL_IMAP_PASSWORD", "")
    mailbox = os.getenv("FASTMAIL_IMAP_MAILBOX", "INBOX")
    poll_seconds = _env_int("POLL_SECONDS", 20)

    openai_api_key = os.getenv("OPENAI_API_KEY")
    openai_ocr_model = os.getenv("OPENAI_OCR_MODEL", "gpt-4o-mini")

    llm_provider = os.getenv("LLM_PROVIDER", "openai").strip().lower() or "openai"
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    anthropic_ocr_model = os.getenv("ANTHROPIC_OCR_MODEL", "claude-sonnet-4-20250514")

    openclaw_gateway_url = os.getenv("OPENCLAW_GATEWAY_URL", "ws://127.0.0.1:18789")
    openclaw_gateway_token = os.getenv("OPENCLAW_GATEWAY_TOKEN")
    openclaw_session_id = os.getenv("OPENCLAW_SESSION_ID", "inbox")

    if not imap_user or not imap_password:
        raise ValueError("Missing FASTMAIL_IMAP_USER or FASTMAIL_IMAP_PASSWORD")

    if not openclaw_gateway_token:
        openclaw_gateway_token = read_openclaw_token_from_config()

    return Config(
        imap_host=imap_host,
        imap_port=imap_port,
        imap_user=imap_user,
        imap_password=imap_password,
        mailbox=mailbox,
        poll_seconds=poll_seconds,
        openai_api_key=openai_api_key,
        openai_ocr_model=openai_ocr_model,
        llm_provider=llm_provider,
        anthropic_api_key=anthropic_api_key,
        anthropic_ocr_model=anthropic_ocr_model,
        openclaw_gateway_url=openclaw_gateway_url,
        openclaw_gateway_token=openclaw_gateway_token,
        openclaw_session_id=openclaw_session_id,
    )


def read_openclaw_token_from_config() -> Optional[str]:
    p = Path.home() / ".openclaw" / "openclaw.json"
    try:
        data = json.loads(p.read_text())
        return data.get("gateway", {}).get("auth", {}).get("token")
    except Exception:
        return None


def ocr_image_via_anthropic(image_path: Path, model: str, api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except Exception:
        return None

    try:
        import base64

        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        mime = "image/png"
        if image_path.suffix.lower() in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif image_path.suffix.lower() == ".webp":
            mime = "image/webp"

        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all readable text from this image. Return plain text."},
                        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    ],
                }
            ],
        )
        parts = getattr(msg, "content", None) or []
        out: List[str] = []
        for p in parts:
            if getattr(p, "type", None) == "text":
                t = getattr(p, "text", "") or ""
                if t:
                    out.append(t)
        txt = "\n".join(out).strip()
        return txt or None
    except Exception:
        return None


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"seen": {}}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"seen": {}}


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _get_body_text(msg: Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    continue
        if parts:
            return "\n".join(parts).strip()

        # fallback to html stripped-ish
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype == "text/html" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return _very_naive_html_to_text(html)
                except Exception:
                    continue
        return ""

    ctype = msg.get_content_type()
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")
    if ctype == "text/html":
        return _very_naive_html_to_text(text)
    return text.strip()


def _very_naive_html_to_text(html: str) -> str:
    # Avoid adding new dependencies. Good enough to preview.
    out = []
    in_tag = False
    for ch in html:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            out.append(" ")
            continue
        if not in_tag:
            out.append(ch)
    return " ".join("".join(out).split()).strip()


def extract_attachments(msg: Message, out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: List[Path] = []
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        is_attachment = "attachment" in disp
        is_inline_image = part.get_content_maintype() == "image"

        if not is_attachment and filename is None and not is_inline_image:
            continue

        filename = _decode_header(filename)
        if not filename:
            ext = "bin"
            ctype = part.get_content_type()
            if ctype == "image/png":
                ext = "png"
            elif ctype == "image/jpeg":
                ext = "jpg"
            elif ctype == "image/webp":
                ext = "webp"
            elif ctype == "application/pdf":
                ext = "pdf"

            cid = (part.get("Content-ID") or "").strip().strip("<>")
            if cid:
                filename = f"inline_{cid}.{ext}"
            else:
                safe_ctype = ctype.replace("/", "_")
                filename = f"attachment_{safe_ctype}.{ext}"

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ").strip()
        if not safe_name:
            safe_name = "attachment.bin"

        path = out_dir / safe_name
        # avoid collisions
        if path.exists():
            stem = path.stem
            suffix = path.suffix
            path = out_dir / f"{stem}_{int(time.time())}{suffix}"

        path.write_bytes(payload)
        files.append(path)

    return files


def sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def ocr_image_via_openai(image_path: Path, model: str) -> Optional[str]:
    try:
        from openai import OpenAI
    except Exception:
        return None

    try:
        client = OpenAI()
        b64 = image_path.read_bytes()
        # OpenAI python SDK expects base64 data URL via the "image_url" field
        import base64

        encoded = base64.b64encode(b64).decode("ascii")
        mime = "image/png"
        if image_path.suffix.lower() in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif image_path.suffix.lower() == ".webp":
            mime = "image/webp"

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all readable text from this image. Return plain text."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                    ],
                }
            ],
        )
        return (resp.choices[0].message.content or "").strip() if resp.choices else None
    except Exception:
        return None


def is_image(path: Path) -> bool:
    return path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def extract_pdf_text(path: Path) -> Optional[str]:
    try:
        from pypdf import PdfReader
    except Exception:
        return None

    try:
        reader = PdfReader(str(path))
        parts: List[str] = []
        for page in reader.pages:
            t = page.extract_text() or ""
            t = t.strip()
            if t:
                parts.append(t)
        out = "\n\n".join(parts).strip()
        return out if out else None
    except Exception:
        return None


def run_openclaw_agent(message: str, *, session_id: str, url: str, token: Optional[str]) -> Tuple[int, str]:
    cmd = [
        "openclaw",
        "agent",
        "--session-id",
        session_id,
        "--message",
        message,
        "--json",
    ]

    env = os.environ.copy()
    if token:
        env["OPENCLAW_GATEWAY_TOKEN"] = token

    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    return p.returncode, p.stdout


def format_email_summary(
    *,
    subject: str,
    from_: str,
    to: str,
    date: str,
    message_id: str,
    body_text: str,
    attachments: List[Path],
    ocr_texts: Dict[str, str],
    pdf_texts: Dict[str, str],
) -> str:
    lines = []
    lines.append("New email ingested (Fastmail IMAP)")
    lines.append(f"Subject: {subject}")
    lines.append(f"From: {from_}")
    lines.append(f"To: {to}")
    lines.append(f"Date: {date}")
    lines.append(f"Message-ID: {message_id}")

    if attachments:
        lines.append("Attachments:")
        for p in attachments:
            extra = ""
            if p.name in ocr_texts:
                extra = " (OCR extracted)"
            lines.append(f"- {p.name}{extra}")

    if body_text:
        lines.append("Body (text preview):")
        preview = body_text.strip()
        if len(preview) > 2000:
            preview = preview[:2000] + "..."
        lines.append(preview)

    if ocr_texts:
        lines.append("OCR (image attachments):")
        for name, text in ocr_texts.items():
            t = text.strip()
            if len(t) > 2000:
                t = t[:2000] + "..."
            lines.append(f"[{name}]")
            lines.append(t)

    if pdf_texts:
        lines.append("PDF text (attachments):")
        for name, text in pdf_texts.items():
            t = text.strip()
            if len(t) > 4000:
                t = t[:4000] + "..."
            lines.append(f"[{name}]")
            lines.append(t)

    return "\n".join(lines).strip() + "\n"


def fetch_unseen_uids(imap: imaplib.IMAP4_SSL) -> List[str]:
    typ, data = imap.uid("search", None, "UNSEEN")
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].decode("utf-8").split()


def fetch_email_by_uid(imap: imaplib.IMAP4_SSL, uid: str) -> bytes:
    typ, data = imap.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data:
        raise RuntimeError(f"Failed to fetch UID {uid}")
    for item in data:
        if isinstance(item, tuple) and item[1]:
            return item[1]
    raise RuntimeError(f"No RFC822 payload for UID {uid}")


def main() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 2

    if not cfg.openclaw_gateway_token:
        print(
            "Missing OPENCLAW_GATEWAY_TOKEN and could not read token from ~/.openclaw/openclaw.json",
            file=sys.stderr,
        )
        return 2

    state = load_state()
    seen: Dict[str, Any] = state.get("seen", {})

    print(f"IMAP: {cfg.imap_host}:{cfg.imap_port} mailbox={cfg.mailbox}")
    print(f"Gateway: {cfg.openclaw_gateway_url} session={cfg.openclaw_session_id}")
    print("Polling...")

    while True:
        try:
            with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as imap:
                imap.login(cfg.imap_user, cfg.imap_password)
                typ, _ = imap.select(cfg.mailbox)
                if typ != "OK":
                    raise RuntimeError(f"Failed to select mailbox {cfg.mailbox}")

                uids = fetch_unseen_uids(imap)
                if not uids:
                    time.sleep(cfg.poll_seconds)
                    continue

                for uid in uids:
                    if uid in seen:
                        continue

                    raw = fetch_email_by_uid(imap, uid)
                    msg = email.message_from_bytes(raw)

                    subject = _decode_header(msg.get("Subject"))
                    from_ = _decode_header(msg.get("From"))
                    to = _decode_header(msg.get("To"))
                    date = _decode_header(msg.get("Date"))
                    message_id = _decode_header(msg.get("Message-ID"))

                    body_text = _get_body_text(msg)

                    base_dir = Path(__file__).with_name("inbox") / uid
                    attachments = extract_attachments(msg, base_dir / "attachments")

                    ocr_texts: Dict[str, str] = {}
                    for p in attachments:
                        if not is_image(p):
                            continue
                        if cfg.llm_provider == "anthropic":
                            text = ocr_image_via_anthropic(p, cfg.anthropic_ocr_model, cfg.anthropic_api_key)
                        else:
                            text = ocr_image_via_openai(p, cfg.openai_ocr_model) if cfg.openai_api_key else None
                        if text:
                            ocr_texts[p.name] = text

                    pdf_texts: Dict[str, str] = {}
                    for p in attachments:
                        if is_pdf(p):
                            text = extract_pdf_text(p)
                            if text:
                                pdf_texts[p.name] = text

                    summary = format_email_summary(
                        subject=subject,
                        from_=from_,
                        to=to,
                        date=date,
                        message_id=message_id,
                        body_text=body_text,
                        attachments=attachments,
                        ocr_texts=ocr_texts,
                        pdf_texts=pdf_texts,
                    )

                    rc, out = run_openclaw_agent(
                        summary,
                        session_id=cfg.openclaw_session_id,
                        url=cfg.openclaw_gateway_url,
                        token=cfg.openclaw_gateway_token,
                    )
                    if rc != 0:
                        print("openclaw agent failed:")
                        print(out)
                    else:
                        print(f"Forwarded UID {uid} to OpenClaw session {cfg.openclaw_session_id}")

                    seen[uid] = {"ts": time.time(), "subject": subject, "message_id": message_id}
                    state["seen"] = seen
                    save_state(state)

                time.sleep(cfg.poll_seconds)

        except KeyboardInterrupt:
            print("Exiting")
            return 0
        except Exception as e:
            print(f"Loop error: {e}", file=sys.stderr)
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
