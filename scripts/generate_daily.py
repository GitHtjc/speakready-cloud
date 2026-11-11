#!/usr/bin/env python3
"""
SpeakReady cloud daily pipeline -- content generation step (GitHub Actions).

Runs on a GitHub-hosted runner (US/EU), so the Gemini REST API is NOT
geo-blocked (unlike API calls made from the user's location, which return
HTTP 400 "User location is not supported for the API use").

One run:
  1. Reads data/prompts.json and learns the EXACT key schema from the last
     entry (no hardcoded field names).
  2. Generates 3 new graded speaking prompts via Gemini REST
     (GEMINI_API_KEY from the environment / Actions secret).
  3. Appends them to data/prompts.json (atomic write; git history is the
     backup -- no dated backup files needed).
  4. Updates the prompt-count badge in index.html (best effort).
  5. Appends the new prompts to llms.txt under a dated heading.
  6. Saves the newsletter draft to newsletter_drafts/YYYY-MM-DD.md.
  7. Writes state/last_run_date and emits the new ids to $GITHUB_OUTPUT
     (and to stdout as NEW_IDS=... for the log).

Idempotency: exits 0 immediately when state/last_run_date already equals
today (Asia/Shanghai). Audio generation and the Telegram push are separate
workflow steps that consume the new_ids output.
"""

import datetime as dt
import json
import os
import re
import sys
import traceback
import urllib.request
from zoneinfo import ZoneInfo

# ----------------------------- config -------------------------------------
ROOT         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_JSON = os.path.join(ROOT, "data", "prompts.json")
INDEX_HTML   = os.path.join(ROOT, "index.html")
LLMS_TXT     = os.path.join(ROOT, "llms.txt")
STATE_DIR    = os.path.join(ROOT, "state")
STATE_FILE   = os.path.join(STATE_DIR, "last_run_date")
DRAFTS_DIR   = os.path.join(ROOT, "newsletter_drafts")

CST = ZoneInfo("Asia/Shanghai")          # the user's day boundary
PROMPTS_PER_DAY = 3
GEMINI_MODEL    = "gemini-2.5-flash"
GEN_TIMEOUT_S   = 600
MAX_ATTEMPTS    = 3
# --------------------------------------------------------------------------


def log(msg):
    ts = dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)


def today_str():
    return dt.datetime.now(CST).date().isoformat()


def already_completed(today):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return f.read().strip() == today
    except OSError:
        return False


def mark_completed(today):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(today + "\n")


def emit_new_ids(new_ids):
    csv = ",".join(new_ids)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write("new_ids=%s\n" % csv)
    print("NEW_IDS=%s" % csv, flush=True)


# ------------------------- prompts.json handling --------------------------

def load_prompts():
    """Return (doc, key, prompt_list). doc is the whole JSON doc; key is the
    dict key holding the list, or None if the doc itself is a list."""
    with open(PROMPTS_JSON, encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, list):
        return doc, None, doc
    if isinstance(doc, dict):
        for k in ("prompts", "data", "items"):
            if isinstance(doc.get(k), list):
                return doc, k, doc[k]
        for k, v in doc.items():
            if isinstance(v, list):
                log("prompts.json: using list under key %r" % k)
                return doc, k, v
    raise ValueError("prompts.json is neither a list nor a dict containing a list")


def save_prompts(doc, key):
    tmp = PROMPTS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, PROMPTS_JSON)


def next_ids(prompts, count):
    max_n = 0
    for p in prompts:
        m = re.fullmatch(r"[Pp](\d+)", str(p.get("id", "")))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return ["p%d" % (max_n + i) for i in range(1, count + 1)]


# --------------------------- content generation ---------------------------

def build_generation_prompt(sample_entry, today):
    keys = [k for k in sample_entry.keys() if k != "id"]
    sample_json = json.dumps(sample_entry, ensure_ascii=False, indent=2)
    keys_bullets = "\n".join("  - %s" % k for k in keys)
    return (
        "You are the SpeakReady content engine. Today is %s.\n\n"
        "Ground your topics in currently-trending English-learning material "
        "(everyday small talk questions, remote job interview prompts, workplace "
        "communication scenarios, IELTS/TOEFL speaking topics -- the kind of sources "
        "like IELTS Liz, The Muse, Indeed, TalkDrill would feature this week). "
        "Pick 3 DISTINCT topics across different categories.\n\n"
        "Each prompt MUST use EXACTLY these keys (same names, same value types; "
        "omit \"id\" -- it is assigned afterwards):\n%s\n\n"
        "Here is a real example entry showing the expected structure and tone:\n"
        "%s\n\n"
        "Also draft this week's \"60-Second Daily English Upgrade\" newsletter issue "
        "as markdown (one upgrade: a natural phrase, one example, one micro-tip).\n\n"
        "OUTPUT STRICT JSON ONLY, no markdown fences, no commentary:\n"
        "{\"prompts\": [ <3 objects> ], \"newsletter_draft\": \"<markdown string>\"}"
        % (today, keys_bullets, sample_json)
    )


def extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return json.loads(text[start:end + 1])


def call_gemini_api(prompt):
    """Single Gemini REST call. Returns the parsed JSON dict. Raises on any
    failure (network, HTTP error, bad shape). Separated out so tests can
    monkeypatch it."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"
           % (GEMINI_MODEL, api_key))
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=GEN_TIMEOUT_S) as resp:
        payload = json.load(resp)
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("unexpected REST response shape: %s" % str(payload)[:300])
    return extract_json(text)


def generate_prompts(sample_entry, today):
    prompt = build_generation_prompt(sample_entry, today)
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log("generation attempt %d/%d via Gemini REST (%s) ..."
                % (attempt, MAX_ATTEMPTS, GEMINI_MODEL))
            data = call_gemini_api(prompt)
            break
        except Exception as e:
            last_err = e
            log("attempt %d failed: %s" % (attempt, e))
    else:
        raise RuntimeError("content generation failed after %d attempts: %s"
                           % (MAX_ATTEMPTS, last_err))
    prompts = data.get("prompts")
    draft = data.get("newsletter_draft", "")
    if not isinstance(prompts, list) or len(prompts) != PROMPTS_PER_DAY:
        raise ValueError("expected %d prompts, got: %r" % (PROMPTS_PER_DAY, prompts))
    expected_keys = set(k for k in sample_entry.keys() if k != "id")
    for i, p in enumerate(prompts):
        if not isinstance(p, dict) or set(p.keys()) != expected_keys:
            raise ValueError("prompt %d has wrong keys: %s (expected %s)"
                             % (i, sorted(p.keys()) if isinstance(p, dict) else type(p),
                                sorted(expected_keys)))
    if not isinstance(draft, str) or not draft.strip():
        log("warning: empty newsletter draft; continuing")
        draft = ""
    return prompts, draft


# ------------------------------ side effects ------------------------------

def update_badge(old_total, new_total):
    """Mirror the local pipeline's badge update: the two count spans plus the
    'N+ graded' in the meta description."""
    try:
        with open(INDEX_HTML, encoding="utf-8") as f:
            html = f.read()
    except OSError as e:
        log("index.html not readable (%s); skipping badge update" % e)
        return
    old, new = str(old_total), str(new_total)
    subs = [
        (r'id="metricPromptsCount">%s</span>' % re.escape(old),
         'id="metricPromptsCount">%s</span>' % new, "metricPromptsCount"),
        (r'id="tabCount">%s</span>' % re.escape(old),
         'id="tabCount">%s</span>' % new, "tabCount"),
        (r'%s\+ graded' % re.escape(old),
         '%s+ graded' % new, "N+ graded"),
    ]
    total_n = 0
    for pat, repl, label in subs:
        html, n = re.subn(pat, repl, html, count=1)
        total_n += n
        log("index.html: %s: %s -> %s" % (label, old, new) if n else
            "index.html: %s pattern not found" % label)
    if total_n == 0:
        log("warning: no badge patterns matched in index.html")
        return
    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log("index.html badge updated: %d -> %d" % (old_total, new_total))


def append_llms(today, new_entries):
    section = "\n\n## %s (auto)\n```json\n%s\n```\n" % (
        today, json.dumps(new_entries, ensure_ascii=False, indent=2))
    try:
        with open(LLMS_TXT, "a", encoding="utf-8") as f:
            f.write(section)
        log("llms.txt appended")
    except OSError as e:
        log("warning: could not append llms.txt (%s)" % e)


def save_draft(today, draft):
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    path = os.path.join(DRAFTS_DIR, "%s.md" % today)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(draft)
        log("newsletter draft saved: %s" % path)
    except OSError as e:
        log("warning: could not save newsletter draft (%s)" % e)


# --------------------------------- main -----------------------------------

def main(argv=None):
    force = argv is not None and "--force" in argv
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    today = today_str()

    if already_completed(today) and not force:
        log("today's run already completed (state/last_run_date=%s); exiting" % today)
        emit_new_ids([])
        return 0

    log("starting cloud generation run for %s" % today)
    doc, key, prompts = load_prompts()
    old_total = len(prompts)
    if not prompts:
        raise RuntimeError("prompts.json contains no entries to learn the schema from")
    sample = prompts[-1]

    new_prompts, draft = generate_prompts(sample, today)
    new_ids = next_ids(prompts, len(new_prompts))
    new_entries = []
    for pid, p in zip(new_ids, new_prompts):
        entry = {"id": pid}
        entry.update(p)
        new_entries.append(entry)

    prompts.extend(new_entries)
    save_prompts(doc, key)
    log("prompts.json: %d -> %d (%s)" % (old_total, len(prompts), ", ".join(new_ids)))

    update_badge(old_total, len(prompts))
    append_llms(today, new_entries)
    save_draft(today, draft)
    mark_completed(today)
    emit_new_ids(new_ids)
    log("generation step complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(1)
