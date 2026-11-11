#!/usr/bin/env python3
"""
Telegram daily push for SpeakReady (cloud / GitHub Actions edition).

Sends the day's new prompts as a styled HTML digest document plus the
generated mp3 audio files to a Telegram chat via the Bot API.

Usage (CI):
    python3 scripts/telegram_push.py --ids p51,p52,p53

Standalone:
    python3 scripts/telegram_push.py --ids p51 p52 --dry-run

Config (first found wins):
    1. env vars  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   (Actions secrets)
    2. <repo>/state/telegram.json  ->  {"bot_token": "...", "chat_id": "..."}

Bot API notes:
    - sendDocument: HTML digest (also archived under telegram_digests/)
    - sendAudio:    one mp3 per new prompt (default voice variant)
    - A sent-marker file (state/telegram_sent_<date>) prevents duplicate
      pushes when a run is retried. The workflow only calls this step when
      the generation step produced new ids, so duplicates can't happen
      across days either.
"""

import argparse
import ast
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_JSON = os.path.join(ROOT, "data", "prompts.json")
AUDIO_DIR    = os.path.join(ROOT, "audio")
DRAFTS_DIR   = os.path.join(ROOT, "newsletter_drafts")
OUT_DIR      = os.path.join(ROOT, "telegram_digests")
STATE_DIR    = os.path.join(ROOT, "state")

CST = ZoneInfo("Asia/Shanghai")

CURL_TIMEOUT_DOC = 60
CURL_TIMEOUT_AUDIO = 180


def log_default(msg):
    ts = dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)


# ------------------------------ config ------------------------------------

def load_config():
    """Return (bot_token, chat_id), empty strings when not configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    cfg_path = os.path.join(STATE_DIR, "telegram.json")
    if (not token or not chat_id) and os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            token = token or str(cfg.get("bot_token", "")).strip()
            chat_id = chat_id or str(cfg.get("chat_id", "")).strip()
        except (OSError, ValueError):
            pass
    return token, chat_id


def sent_marker(today):
    return os.path.join(STATE_DIR, "telegram_sent_%s" % today)


# ------------------------------ data --------------------------------------

def load_prompts():
    with open(PROMPTS_JSON, encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for k in ("prompts", "data", "items"):
            if isinstance(doc.get(k), list):
                return doc[k]
        for v in doc.values():
            if isinstance(v, list):
                return v
    raise ValueError("prompts.json contains no prompt list")


def find_entries(prompts, ids):
    by_id = {str(p.get("id")): p for p in prompts}
    found, missing = [], []
    for pid in ids:
        if pid in by_id:
            found.append(by_id[pid])
        else:
            missing.append(pid)
    return found, missing


def read_draft(today):
    path = os.path.join(DRAFTS_DIR, "%s.md" % today)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def parse_ids(raw_list):
    ids = []
    for raw in raw_list or []:
        ids.extend(s for s in re.split(r"[,\s]+", str(raw).strip()) if s)
    return ids


# ------------------------------ rendering ---------------------------------

def humanize(key):
    return re.sub(r"_+", " ", str(key)).strip().capitalize()


def format_value(v):
    if isinstance(v, list):
        items = []
        for x in v:
            s = str(x).strip()
            # chunks_cn items are stringified dicts like
            # "{'en': '...', 'cn': '...'}": render as "en → cn".
            if s.startswith("{") and s.endswith("}"):
                try:
                    d = ast.literal_eval(s)
                except (ValueError, SyntaxError):
                    d = None
                if isinstance(d, dict) and "en" in d and "cn" in d:
                    items.append("• %s → %s"
                                 % (html.escape(str(d["en"])),
                                    html.escape(str(d["cn"]))))
                    continue
            items.append("• " + html.escape(s))
        return "<br>".join(items)
    if isinstance(v, dict):
        return "<br>".join("• %s: %s" % (html.escape(humanize(k)), html.escape(str(x)))
                           for k, x in v.items())
    return html.escape(str(v))


def entry_to_html(entry):
    title = entry.get("question") or entry.get("title") or entry.get("id", "")
    pid = entry.get("id", "")
    parts = ['<div class="card">',
             "<h2>%s <span class=\"pid\">%s</span></h2>"
             % (html.escape(str(title)), html.escape(str(pid)))]
    for k, v in entry.items():
        if k in ("id", "question", "title"):
            continue
        parts.append("<h3>%s</h3>" % html.escape(humanize(k)))
        parts.append("<p>%s</p>" % format_value(v))
    parts.append("</div>")
    return "\n".join(parts)


def md_to_html(text):
    """Tiny markdown subset -> HTML: headings, bullets, **bold**, paragraphs."""
    out, in_list = [], False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def inline(s):
        s = html.escape(s)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            close_list()
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            close_list()
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level + 1, inline(m.group(2)), level + 1))
            continue
        if re.match(r"^[-*]\s+", line):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % inline(re.sub(r"^[-*]\s+", "", line)))
            continue
        close_list()
        out.append("<p>%s</p>" % inline(line))
    close_list()
    return "\n".join(out)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SpeakReady Daily · {date}</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       max-width: 720px; margin: 0 auto; padding: 20px; color: #1c1c1e; line-height: 1.7; }}
.header {{ background: #0a84ff; color: #fff; border-radius: 12px; padding: 20px; }}
.header h1 {{ margin: 0 0 6px; font-size: 22px; }}
.card {{ border: 1px solid #e5e5ea; border-radius: 12px; padding: 16px; margin: 16px 0; }}
.card h2 {{ margin-top: 0; font-size: 18px; }}
.card h2 .pid {{ font-size: 12px; font-weight: normal; color: #8e8e93; }}
.card h3 {{ font-size: 13px; color: #636366; margin: 12px 0 4px;
            text-transform: uppercase; letter-spacing: .04em; }}
.card p {{ margin: 0 0 6px; }}
.newsletter {{ background: #f2f2f7; border-radius: 12px; padding: 16px; margin-top: 20px; }}
.newsletter h2 {{ margin-top: 0; }}
.footer {{ color: #8e8e93; font-size: 12px; margin-top: 24px; text-align: center; }}
</style>
</head>
<body>
<div class="header"><h1>📚 SpeakReady Daily</h1>
<div>{date} · {n} new prompts · tap 🔊 for audio</div></div>
{cards}
{newsletter}
<div class="footer">Generated by the SpeakReady cloud pipeline ☁️</div>
</body>
</html>
"""


def render_html(today, entries, draft_md):
    cards = "\n".join(entry_to_html(e) for e in entries)
    newsletter = ""
    if draft_md.strip():
        newsletter = ('<div class="newsletter"><h2>⚡ 60-Second Daily English Upgrade</h2>\n'
                      + md_to_html(draft_md) + "\n</div>")
    return HTML_TEMPLATE.format(date=html.escape(today), n=len(entries),
                                cards=cards, newsletter=newsletter)


# ------------------------------ sending -----------------------------------

def curl_post(url, fields, files, timeout, log):
    """POST multipart/form-data via curl. fields=[(k,v)], files=[(k,path)]."""
    cmd = ["curl", "-sS", "-m", str(timeout), "-X", "POST", url]
    for k, v in fields:
        cmd += ["-F", "%s=%s" % (k, v)]
    for k, path in files:
        cmd += ["-F", "%s=@%s" % (k, path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Telegram request timed out")
    if r.returncode != 0:
        raise RuntimeError("curl failed: %s" % r.stderr.strip()[-500:])
    try:
        resp = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError("non-JSON response from Telegram: %s" % r.stdout[:200])
    if not resp.get("ok"):
        raise RuntimeError("Telegram API error: %s" % r.stdout[:500])
    return resp


def send_document(token, chat_id, path, caption, timeout, log):
    url = "https://api.telegram.org/bot%s/sendDocument" % token
    resp = curl_post(url, [("chat_id", chat_id), ("caption", caption)],
                     [("document", path)], timeout, log)
    log("Telegram: document sent (message_id=%s)"
        % resp["result"]["message_id"])
    return resp


def send_audio(token, chat_id, path, title, timeout, log):
    url = "https://api.telegram.org/bot%s/sendAudio" % token
    resp = curl_post(url, [("chat_id", chat_id), ("title", title),
                           ("performer", "SpeakReady")],
                     [("audio", path)], timeout, log)
    log("Telegram: audio sent: %s (message_id=%s)"
        % (os.path.basename(path), resp["result"]["message_id"]))
    return resp


# ------------------------------ main entry --------------------------------

def push_daily(today, ids, token, chat_id, log=log_default, dry_run=False,
               force=False):
    """Build the digest + send it with today's audio. Returns a summary dict."""
    os.makedirs(STATE_DIR, exist_ok=True)
    marker = sent_marker(today)
    if os.path.exists(marker) and not force and not dry_run:
        log("Telegram push for %s already sent; skipping" % today)
        return {"sent": False, "reason": "already_sent"}

    prompts = load_prompts()
    entries, missing = find_entries(prompts, ids)
    for pid in missing:
        log("warning: prompt %s not found in prompts.json; skipping" % pid)
    if not entries:
        raise RuntimeError("none of today's prompt ids found in prompts.json")

    draft_md = read_draft(today)
    os.makedirs(OUT_DIR, exist_ok=True)
    html_path = os.path.join(OUT_DIR, "speakready-%s.html" % today)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(today, entries, draft_md))
    log("digest written: %s" % html_path)

    audios = []
    for pid in ids:
        mp3 = os.path.join(AUDIO_DIR, "%s.mp3" % pid)
        if os.path.isfile(mp3):
            audios.append(mp3)
        else:
            log("warning: audio missing for %s (%s); skipping" % (pid, mp3))

    caption = "📚 SpeakReady Daily · %s\n%d new prompts + audio 🎧" % (today, len(entries))

    if dry_run:
        log("[dry-run] would send document: %s" % html_path)
        for a in audios:
            log("[dry-run] would send audio: %s" % a)
        return {"sent": False, "dry_run": True, "html": html_path, "audios": audios}

    send_document(token, chat_id, html_path, caption, CURL_TIMEOUT_DOC, log)
    for mp3 in audios:
        pid = os.path.splitext(os.path.basename(mp3))[0]
        send_audio(token, chat_id, mp3, "%s · SpeakReady" % pid,
                   CURL_TIMEOUT_AUDIO, log)

    with open(marker, "w", encoding="utf-8") as f:
        f.write(today + "\n")
    summary = {"sent": True, "html": html_path, "audios": audios,
               "messages": 1 + len(audios)}
    log("Telegram push complete: %d messages" % summary["messages"])
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Push SpeakReady daily digest to Telegram")
    ap.add_argument("--date", default=dt.datetime.now(CST).date().isoformat(),
                    help="which day's prompts to push (default: today, Asia/Shanghai)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the digest, print what would be sent, send nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-send even if today's push was already sent")
    ap.add_argument("--ids", nargs="*", default=None,
                    help="prompt ids (space- or comma-separated)")
    args = ap.parse_args(argv)

    token, chat_id = load_config()
    if not args.dry_run and (not token or not chat_id):
        print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and "
              "TELEGRAM_CHAT_ID env vars (Actions secrets), or create "
              'state/telegram.json with {"bot_token": "...", "chat_id": "..."}.',
              file=sys.stderr)
        return 2

    ids = parse_ids(args.ids)
    if not ids:
        print("no --ids given; nothing to push", file=sys.stderr)
        return 2

    push_daily(args.date, ids, token, chat_id, dry_run=args.dry_run,
               force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
