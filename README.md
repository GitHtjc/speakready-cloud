# SpeakReady ☁️ Cloud Pipeline

The SpeakReady daily content pipeline, running entirely on **GitHub Actions**.
No Mac required — the Mac can sleep through the 07:52 run and sync up later.

## What it does (every day, 07:52 Beijing time)

1. **Generates** 3 new graded English speaking prompts via the Gemini API
   (`scripts/generate_daily.py`). The runner is in the US/EU, so the API
   key works there — no "User location is not supported" geo-block.
2. **Synthesizes** mp3 audio for the 3 prompts (Jenny/Guy voices, edge-tts).
3. **Pushes** a styled HTML digest + the mp3s to your Telegram chat.
4. **Commits** everything back to this repo: `data/prompts.json`,
   `audio/`, `llms.txt`, `index.html` (badge), `newsletter_drafts/`,
   `telegram_digests/`, `state/last_run_date`.

The repo is the **source of truth**. The Mac is now just a reader.

## Setup (about 10 minutes)

### 0. Stop the old local scheduler (do this FIRST)

On the Mac, in Terminal — otherwise the local job and the cloud job will
both generate prompts and their numbering will collide:

```bash
launchctl bootout gui/$(id -u)/com.speakready.daily
```

(Keep `~/.speakready/` and `~/Documents/MyApps/speakready/` as they are;
the cloud takes over from prompt #51 onward.)

### 1. Create the repo

On github.com: **New repository** → name it e.g. `speakready-cloud` →
**Private** → **Create repository**. Do not add a README/license (keep it empty).

### 2. Upload these files

Unzip `speakready-cloud.zip` and upload everything into the new repo,
preserving the folder structure (`.github/`, `scripts/`, `data/`, …).
Easiest via git:

```bash
cd speakready-cloud
git init -b main
git add -A
git commit -m "initial: SpeakReady cloud pipeline (50 prompts seeded)"
git remote add origin https://github.com/<you>/speakready-cloud.git
git push -u origin main
```

(The GitHub web UI "uploading an existing file" also works — drag the
unzipped *contents*, not the zip itself.)

### 3. Add the 3 secrets

Repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**, add these three (names must match exactly):

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | your Gemini API key |
| `TELEGRAM_BOT_TOKEN` | your `@speakready_bot` token |
| `TELEGRAM_CHAT_ID` | your Telegram chat id |

### 4. Allow the workflow to commit

Repo → **Settings** → **Actions** → **General** → *Workflow permissions* →
select **Read and write permissions** → **Save**.
(New repos default to read-only, which would block the daily commit.)

### 5. Test it now

Repo → **Actions** tab → **SpeakReady Daily** → **Run workflow** →
**Run workflow**. Watch the run:

- `Generate 3 new prompts` → log ends with `NEW_IDS=p51,p52,p53`
- `Generate audio` → 9 mp3s
- `Push digest + audio to Telegram` → your phone buzzes 📚🎧
- `Commit and push results` → repo gains a `daily: p51,p52,p53` commit

If a step goes red, open it — the logs say exactly which step failed.

### 6. Sync the Mac (whenever it's awake)

One-time:

```bash
git clone https://github.com/<you>/speakready-cloud.git ~/Documents/speakready-cloud
```

Then whenever you want the local site
(http://localhost:3333/serve/speakready/) to catch up:

```bash
bash ~/Documents/speakready-cloud/scripts/sync_to_mac.sh
```

This pulls the repo and copies `prompts.json`, `llms.txt`, `index.html`,
new mp3s (never overwrites existing ones) and newsletter drafts into
`~/Documents/MyApps/speakready/`. Your local portal, audio player and
everything else keep working unchanged.

## Notes

- **Schedule**: cron `52 23 * * *` UTC = 07:52 Asia/Shanghai. GitHub may
  start scheduled runs a few minutes late — harmless here.
- **Idempotency**: `state/last_run_date` records the completed day; re-runs
  (manual or retried) exit cleanly without duplicates. `concurrency` also
  serializes runs.
- **Cost**: GitHub Actions free tier (2,000 min/month) — this job uses ~5.
- **Logs**: every run's full log lives under the Actions tab.
- **Manual backfill**: Actions → Run workflow any time; it generates for the
  current day only if not already done (`--force` is not exposed in CI —
  re-running the same day is a no-op by design).
- **Local files to keep**: `~/Documents/MyApps/speakready/` (the site),
  `~/.speakready/telegram.json` (token backup). The old `com.speakready.daily`
  launchd job stays disabled; re-enable with `launchctl bootstrap` if you
  ever want to go back to local.
