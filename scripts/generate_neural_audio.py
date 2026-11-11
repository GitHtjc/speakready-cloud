#!/usr/bin/env python3
"""
SpeakReady neural audio generation (edge-tts).

Same behavior as the Mac's scripts/generate_neural_audio.py, plus:
  --ids p51,p52,p53   only generate audio for these prompt ids
                      (used by CI so a fresh checkout doesn't regenerate
                      the whole back-catalog)
  --dry-run           list what would be generated, call nothing

Without --ids it audits the entire prompts.json and backfills anything
missing, exactly like the original.
"""
import argparse
import asyncio
import json
import os
import re
import sys

# Ensure repository root and virtualenv site-packages are accessible
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
venv_site_packages = os.path.join(PROJECT_ROOT, ".venv", "lib", "python3.12", "site-packages")
if os.path.exists(venv_site_packages) and venv_site_packages not in sys.path:
    sys.path.insert(0, venv_site_packages)

import edge_tts

VOICES = {
    "jenny": "en-US-JennyNeural",
    "guy": "en-US-GuyNeural",
    "sonia": "en-GB-SoniaNeural"
}


async def generate_audio(text, voice, out_path, rate="+0%"):
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        return False
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(out_path)
        if not os.path.exists(out_path) or os.path.getsize(out_path) <= 1000:
            raise RuntimeError("edge-tts produced an empty file")
        return True
    except Exception as e:
        # Never leave a 0-byte mp3 behind: the browser would serve silence and
        # the old size check would keep treating it as missing.
        if os.path.exists(out_path) and os.path.getsize(out_path) <= 1000:
            try:
                os.remove(out_path)
            except OSError:
                pass
        print(f"⚠️ Error generating {out_path} with {voice}: {e}", file=sys.stderr)
        return False


def parse_ids(raw):
    if not raw:
        return None
    return set(s for s in re.split(r"[,\s]+", raw.strip()) if s)


async def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate neural audio for SpeakReady prompts")
    ap.add_argument("--ids", default=None,
                    help="comma-separated prompt ids to limit generation to "
                         "(default: audit everything missing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be generated without calling edge-tts")
    args = ap.parse_args(argv)
    wanted = parse_ids(args.ids)

    audio_dir = os.path.join(PROJECT_ROOT, "audio")
    prompts_path = os.path.join(PROJECT_ROOT, "data", "prompts.json")
    os.makedirs(audio_dir, exist_ok=True)

    with open(prompts_path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    prompts = doc if isinstance(doc, list) else None
    if prompts is None:
        for v in doc.values():
            if isinstance(v, list):
                prompts = v
                break
    if prompts is None:
        raise ValueError("prompts.json contains no prompt list")

    if wanted is not None:
        prompts = [p for p in prompts if str(p.get("id")) in wanted]
        if not prompts:
            print("no matching prompts for --ids filter; nothing to do")
            return

    print(f"🎙 Auditing & generating neural accessibility reader audio for {len(prompts)} scenarios...")
    tasks = []

    for p in prompts:
        pid = p["id"]
        text = p.get("listenText") or p.get("natural") or p.get("question")

        # Primary Jenny (US Female)
        tasks.append((text, VOICES["jenny"], os.path.join(audio_dir, f"{pid}.mp3")))
        tasks.append((text, VOICES["jenny"], os.path.join(audio_dir, f"{pid}_jenny.mp3")))

        # Guy (US Male)
        tasks.append((text, VOICES["guy"], os.path.join(audio_dir, f"{pid}_guy.mp3")))

    # Filter only missing files
    missing = []
    for text, voice, out_path in tasks:
        if not os.path.exists(out_path) or os.path.getsize(out_path) <= 1000:
            missing.append((text, voice, out_path))

    if args.dry_run:
        print(f"[dry-run] would generate {len(missing)} files:")
        for _, voice, out_path in missing:
            print(f"  {os.path.basename(out_path)} ({voice})")
        return

    if not missing:
        print(f"✅ All {len(tasks)} audio files are already intact and valid ({len(prompts)} scenarios x 3 voices).")
        return

    print(f"⚡ Generating {len(missing)} missing audio files...")
    # Run in batches of 10 to avoid socket limits
    batch_size = 10
    failed = 0
    for i in range(0, len(missing), batch_size):
        batch = [generate_audio(t, v, o) for t, v, o in missing[i:i + batch_size]]
        results = await asyncio.gather(*batch)
        failed += sum(1 for result in results if not result)
        print(f"  Processed {min(i + batch_size, len(missing))}/{len(missing)} missing files...")

    # Final integrity check: "generated" must mean every file exists and is
    # larger than the 1 KB guard. Otherwise exit non-zero so the caller can
    # retry instead of reporting success.
    invalid = []
    for p in prompts:
        pid = p["id"]
        for suffix in ("", "_jenny", "_guy"):
            path = os.path.join(audio_dir, f"{pid}{suffix}.mp3")
            if not os.path.exists(path) or os.path.getsize(path) <= 1000:
                invalid.append(os.path.basename(path))

    if failed or invalid:
        print(
            f"❌ {len(invalid)} audio files are missing or invalid after generation"
            f" ({failed} edge-tts calls failed): {', '.join(invalid[:12])}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("✅ All neural audio files generated and verified successfully!")


if __name__ == "__main__":
    asyncio.run(main())
