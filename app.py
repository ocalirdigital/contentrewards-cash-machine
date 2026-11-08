import os
import re
import json
import time
import asyncio
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from scraper import fetch_live_campaigns

BASE_DIR = Path(__file__).parent
CLIPS_DIR = BASE_DIR / "clips"
DOWNLOADS_DIR = BASE_DIR / "downloads"
ASSETS_DIR = BASE_DIR / "assets"
CLIPS_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR.mkdir(exist_ok=True)
ASSETS_DIR.mkdir(exist_ok=True)

ENV_PATH = BASE_DIR / ".env"


def load_env() -> dict:
    env_vars = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env_vars[k.strip()] = v.strip()
    return env_vars


def save_env(whop_key: str, gemini_key: str, openai_key: str = "", cr_handle: str = ""):
    lines = [
        f"WHOP_API_KEY={whop_key.strip()}",
        f"GEMINI_API_KEY={gemini_key.strip()}",
        f"OPENAI_API_KEY={openai_key.strip()}",
        f"CREATOR_HANDLE={cr_handle.strip()}",
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mask_secret(val: str) -> str:
    if not val or len(val) < 6:
        return ""
    return val[:4] + "••••••••" + val[-3:]


def get_whop_status():
    env_vars = load_env()
    api_key = env_vars.get("WHOP_API_KEY", "")
    gemini_key = env_vars.get("GEMINI_API_KEY", "")
    openai_key = env_vars.get("OPENAI_API_KEY", "")
    cr_handle = env_vars.get("CREATOR_HANDLE", "")

    has_gemini = bool(gemini_key and gemini_key != "your_gemini_api_key_here")
    has_openai = bool(openai_key and openai_key != "your_openai_api_key_here")

    base_info = {
        "gemini": has_gemini,
        "openai": has_openai,
        "cr_handle": cr_handle,
        "whop_masked": mask_secret(api_key) if api_key != "your_whop_api_key_here" else "",
        "gemini_masked": mask_secret(gemini_key) if has_gemini else "",
        "openai_masked": mask_secret(openai_key) if has_openai else "",
    }

    if not api_key or api_key == "your_whop_api_key_here":
        return {
            **base_info,
            "connected": False,
            "title": "Not Linked (Enter Key Below)",
            "id": "",
        }
    try:
        req = urllib.request.Request(
            "https://api.whop.com/api/v5/company",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
            raw_title = data.get("title", "Connected")
            raw_id = data.get("id", "")
            # Privacy-safe masked display so personal names/IDs are never leaked on screen
            masked_title = raw_title[:2] + "***" if len(raw_title) > 2 else "Verified"
            masked_id = "biz_***" + raw_id[-3:] if len(raw_id) > 6 else "Linked"
            return {
                **base_info,
                "connected": True,
                "id": masked_id,
                "title": f"Verified ({masked_title} • {masked_id})",
            }
    except Exception as e:
        return {**base_info, "connected": False, "title": "Invalid API Key", "error": str(e)}


def detect_best_start_second(video_url: str, duration_sec: int = 18) -> tuple[int, int, str, str]:
    """
    Fetches video metadata and detects the #1 Most Replayed (Heatmap) peak if available.
    Returns (start_sec, total_duration, video_title, description_snippet).
    """
    try:
        out = subprocess.check_output(
            [
                "yt-dlp",
                "--js-runtimes", "node",
                "--remote-components", "ejs:github",
                "-J", "--no-playlist",
                video_url,
            ],
            text=True,
            encoding="utf-8",
            timeout=25,
        )
        info = json.loads(out)
        total_dur = int(info.get("duration") or 90)
        vtitle = info.get("title") or "Viral Video"
        vdesc = (info.get("description") or "")[:600]
        hm = info.get("heatmap") or []
        if hm:
            best = max(hm, key=lambda x: x.get("value", 0))
            return max(0, int(best.get("start_time", 8))), total_dur, vtitle, vdesc
        if total_dur <= duration_sec + 5:
            return 0, total_dur, vtitle, vdesc
        return max(5, int(total_dur * 0.18)), total_dur, vtitle, vdesc
    except Exception:
        return 8, 90, "Official Campaign Trailer", ""


def generate_ai_metadata(video_url: str) -> dict:
    """
    Uses the user's Gemini API Key or OpenAI API Key (if configured in .env) or smart
    heatmap analysis to generate:
    - on_screen_text (Hook)
    - vo_text (Voiceover narration)
    - caption (FTC-compliant #Ad caption)
    - start_sec (Optimal viral start timestamp)
    """
    start_sec, total_dur, vtitle, vdesc = detect_best_start_second(video_url, 18)
    env_vars = load_env()
    gemini_key = env_vars.get("GEMINI_API_KEY", "")
    openai_key = env_vars.get("OPENAI_API_KEY", "")
    cr_handle = env_vars.get("CREATOR_HANDLE", "@callofduty") or "@callofduty"

    prompt = (
        f"You are an expert short-form video producer for Whop & ContentRewards bounty campaigns.\n"
        f"Source Video Title: {vtitle}\n"
        f"Source Description: {vdesc}\n"
        f"Target Brand Tag: {cr_handle}\n"
        f"Video Duration: {total_dur} seconds (Peak engagement around {start_sec}s).\n\n"
        f"Return ONLY valid JSON with these exact keys:\n"
        f"- 'on_screen_text': Genuine, non-clickbait on-screen headline (max 9 words)\n"
        f"- 'vo_text': Energetic 2-sentence English voiceover script (max 32 words)\n"
        f"- 'caption': FTC-compliant social caption mentioning {cr_handle} and ending with a newline + '#Ad #Viral #Gaming'\n"
        f"- 'start_sec': integer start second ({start_sec})\n"
    )

    # 1. Try Gemini 2.5 Flash if configured
    if gemini_key and gemini_key != "your_gemini_api_key_here":
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }).encode("utf-8")
        req = urllib.request.Request(api_url, data=payload, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode())
                raw_txt = resp["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(raw_txt)
                return {
                    "ok": True,
                    "source": "Gemini 2.5 Flash AI",
                    "title": vtitle,
                    "start_sec": int(parsed.get("start_sec", start_sec)),
                    "on_screen_text": parsed.get("on_screen_text", f"New highlights from {vtitle[:35]}"),
                    "vo_text": parsed.get("vo_text", f"Check out the best moments from {vtitle}! Which part was your favorite?"),
                    "caption": parsed.get("caption", f"{vtitle} looks insane! {cr_handle}\n#Ad #Gaming #Clips"),
                }
        except Exception:
            pass

    # 2. Try OpenAI GPT-4o-mini if configured
    if openai_key and openai_key != "your_openai_api_key_here":
        api_url = "https://api.openai.com/v1/chat/completions"
        payload = json.dumps({
            "model": "gpt-4o-mini",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {openai_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode())
                raw_txt = resp["choices"][0]["message"]["content"]
                parsed = json.loads(raw_txt)
                return {
                    "ok": True,
                    "source": "OpenAI GPT-4o-mini",
                    "title": vtitle,
                    "start_sec": int(parsed.get("start_sec", start_sec)),
                    "on_screen_text": parsed.get("on_screen_text", f"New highlights from {vtitle[:35]}"),
                    "vo_text": parsed.get("vo_text", f"Check out the best moments from {vtitle}! Which part was your favorite?"),
                    "caption": parsed.get("caption", f"{vtitle} looks insane! {cr_handle}\n#Ad #Gaming #Clips"),
                }
        except Exception:
            pass

    # 3. Built-in fallback analyzer when no external LLM key is configured
    clean_title = re.sub(r"[^\w\s-]", "", vtitle).strip() or "New Season Update"
    return {
        "ok": True,
        "source": "Built-in Smart Heatmap Analyzer (Add Gemini/OpenAI Key Above for LLM Copywriting)",
        "title": vtitle,
        "start_sec": start_sec,
        "on_screen_text": f"{clean_title[:42]} goes hard",
        "vo_text": f"{clean_title} just dropped and the action in this clip is wild. Which moment caught your attention first?",
        "caption": f"{clean_title} is finally here and looking great! Which part are you trying first? {cr_handle}\n#Ad #Clips #Trending",
    }


def ts_ass(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


async def generate_word_synced_tts(text: str, mp3_path: Path, voice: str = "en-US-GuyNeural"):
    import edge_tts
    words = []
    try:
        comm = edge_tts.Communicate(text, voice, rate="+8%", boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, voice, rate="+8%")
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                s = chunk["offset"] / 1e7
                d = chunk["duration"] / 1e7
                words.append((s, s + d, chunk["text"]))
    return words


def write_ass_file(ass_path: Path, on_screen_text: str, total_dur: float, words=None):
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Hook,Arial Black,54,&H0000FFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,6,3,8,50,50,150,1",
        "Style: Word,Arial Black,82,&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,4,2,60,60,280,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if on_screen_text.strip():
        clean_hook = on_screen_text.strip().replace("\n", " ")
        lines.append(f"Dialogue: 0,{ts_ass(0)},{ts_ass(total_dur)},Hook,,0,0,0,,{clean_hook}")

    if words:
        for i in range(0, len(words), 2):
            group = words[i:i + 2]
            g_end = words[i + 2][0] if i + 2 < len(words) else group[-1][1] + 0.3
            for j, (ws, we, _) in enumerate(group):
                end = group[j + 1][0] if j + 1 < len(group) else g_end
                parts = []
                for k, (_, _, wt) in enumerate(group):
                    t = wt.upper()
                    parts.append("{\\c&H00FFFF&}" + t + "{\\c&HFFFFFF&}" if k == j else t)
                lines.append(f"Dialogue: 1,{ts_ass(ws)},{ts_ass(end)},Word,,0,0,0,,{' '.join(parts)}")

    ass_path.write_text("\n".join(lines), encoding="utf-8")


def create_vertical_clip(
    video_url: str,
    start_sec: int,
    duration_sec: int,
    mode: str = "clean_official",
    on_screen_text: str = "",
    vo_text: str = "",
) -> str:
    stamp = int(time.time())
    out_file = CLIPS_DIR / f"clip_{mode}_{stamp}.mp4"
    ass_file = DOWNLOADS_DIR / f"subs_{stamp}.ass"

    local_candidate = Path(video_url.strip('"').strip("'"))
    if local_candidate.exists() and local_candidate.is_file():
        raw_file = local_candidate
        total_dur = 90
        if start_sec < 0:
            start_sec = 0
    else:
        raw_file = DOWNLOADS_DIR / f"raw_{stamp}.mp4"
        auto_start, total_dur, _, _ = detect_best_start_second(video_url, duration_sec)
        if start_sec <= 0:
            start_sec = auto_start

        if mode == "fast_montage":
            cmd_dl = [
                "yt-dlp",
                "--js-runtimes", "node",
                "--remote-components", "ejs:github",
                "-f", "bestvideo[height<=1080]+bestaudio/best",
                "--merge-output-format", "mp4",
                "-o", str(raw_file),
                video_url,
            ]
        else:
            end_sec = start_sec + duration_sec
            cmd_dl = [
                "yt-dlp",
                "--js-runtimes", "node",
                "--remote-components", "ejs:github",
                "-f", "bestvideo[height<=1080]+bestaudio/best",
                "--merge-output-format", "mp4",
                "--download-sections", f"*{start_sec}-{end_sec}",
                "-o", str(raw_file),
                video_url,
            ]
        proc = subprocess.run(cmd_dl, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if not raw_file.exists():
            cmd_fallback = [
                "yt-dlp",
                "--js-runtimes", "node",
                "--remote-components", "ejs:github",
                "-f", "best[ext=mp4]/best",
                "-o", str(raw_file),
                video_url,
            ]
            subprocess.run(cmd_fallback, capture_output=True, text=True, encoding="utf-8", errors="ignore")

        if not raw_file.exists():
            raise RuntimeError(f"Failed to download video: {proc.stderr[-300:]}")

    words = None
    mp3_file = DOWNLOADS_DIR / f"vo_{stamp}.mp3"
    if mode == "ai_voice_captions":
        narration = vo_text.strip() or on_screen_text.strip() or "Check out this insane moment right here, wait until the end!"
        words = asyncio.run(generate_word_synced_tts(narration, mp3_file))

    write_ass_file(ass_file, on_screen_text, duration_sec, words=words)
    ass_esc = str(ass_file).replace("\\", "/").replace(":", "\\:")

    if mode == "fast_montage":
        seg_len = max(3.5, round(duration_sec / 4.0, 1))
        step = max(int(seg_len) + 4, int(total_dur / 5))
        s1, s2, s3, s4 = 5, min(5 + step, max(10, total_dur - 6)), min(5 + step * 2, max(15, total_dur - 6)), min(5 + step * 3, max(20, total_dur - 6))
        segs = [(s1, s1 + seg_len), (s2, s2 + seg_len), (s3, s3 + seg_len), (s4, s4 + seg_len)]
        fc = ""
        for i, (s, e) in enumerate(segs):
            fc += f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}];"
            fc += f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}];"
        fc += "[v0][v1][v2][v3]concat=n=4:v=1:a=0,split=2[bgs][fgs];"
        fc += "[a0][a1][a2][a3]concat=n=4:v=0:a=1[out_a];"
        fc += "[bgs]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5,eq=brightness=-0.15[bg];"
        fc += "[fgs]scale=-2:1280,crop=1080:1280[fg];"
        fc += f"[bg][fg]overlay=0:320,subtitles='{ass_esc}'[out_v]"
        cmd_ff = [
            "ffmpeg", "-y", "-i", str(raw_file),
            "-filter_complex", fc,
            "-map", "[out_v]", "-map", "[out_a]",
            "-t", str(duration_sec), "-r", "30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", str(out_file),
        ]
    elif mode == "ai_voice_captions" and mp3_file.exists():
        fc = (
            "[0:v]split=2[bgs][fgs];"
            "[bgs]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5,eq=brightness=-0.15[bg];"
            "[fgs]scale=-2:1280,crop=1080:1280[fg];"
            f"[bg][fg]overlay=0:320,subtitles='{ass_esc}'[out_v];"
            "[0:a]volume=0.35[ga];[1:a]volume=1.5[vo];"
            "[ga][vo]amix=inputs=2:duration=first:dropout_transition=2[out_a]"
        )
        cmd_ff = [
            "ffmpeg", "-y", "-i", str(raw_file), "-i", str(mp3_file),
            "-filter_complex", fc,
            "-map", "[out_v]", "-map", "[out_a]",
            "-t", str(duration_sec), "-r", "30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", str(out_file),
        ]
    else:
        fc = (
            "[0:v]split=2[bgs][fgs];"
            "[bgs]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5,eq=brightness=-0.15[bg];"
            "[fgs]scale=-2:1280,crop=1080:1280[fg];"
            f"[bg][fg]overlay=0:320,subtitles='{ass_esc}'[out_v]"
        )
        cmd_ff = [
            "ffmpeg", "-y", "-i", str(raw_file),
            "-filter_complex", fc,
            "-map", "[out_v]", "-map", "0:a?",
            "-t", str(duration_sec), "-r", "30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", str(out_file),
        ]

    ff_proc = subprocess.run(cmd_ff, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if not out_file.exists():
        raise RuntimeError(f"FFmpeg error: {ff_proc.stderr[-350:]}")

    return out_file.name


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Whop & ContentRewards AI Clipper Studio — Ocalir Labs</title>
<style>
  :root {
    --bg: #070b14;
    --card: #0f172a;
    --card-alt: #0b1120;
    --border: #1e293b;
    --accent: #ff5722;
    --green: #10b981;
    --blue: #3b82f6;
    --purple: #8b5cf6;
    --text: #f8fafc;
    --muted: #94a3b8;
  }
  * { box-sizing: border-box; }
  body { font-family: 'Inter', 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 18px; }
  .container { max-width: 1360px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; background: var(--card); padding: 15px 22px; border-radius: 16px; border: 1px solid var(--border); margin-bottom: 16px; flex-wrap: wrap; gap: 14px; box-shadow: 0 10px 30px rgba(0,0,0,0.35); }
  .badge { background: rgba(16,185,129,0.15); color: var(--green); padding: 7px 14px; border-radius: 999px; font-weight: 700; font-size: 12px; border: 1px solid rgba(16,185,129,0.3); }
  .sponsor-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .sponsor-btn { padding: 8px 14px; border-radius: 8px; text-decoration: none; font-weight: 700; font-size: 12px; color: #fff; display: inline-flex; align-items: center; gap: 6px; transition: 0.15s; border: none; cursor: pointer; }
  .sponsor-btn:hover { transform: translateY(-1px); opacity: 0.95; }
  .config-bar { background: linear-gradient(180deg, #111c35 0%, #0f172a 100%); border: 1px solid #334155; border-radius: 16px; padding: 16px 20px; margin-bottom: 18px; box-shadow: 0 8px 25px rgba(0,0,0,0.25); }
  .config-grid { display: grid; grid-template-columns: repeat(4, 1fr) auto; gap: 12px; align-items: end; margin-top: 10px; }
  .grid { display: grid; grid-template-columns: 1.08fr 1.12fr; gap: 20px; align-items: start; }
  .card { background: var(--card); padding: 20px; border-radius: 16px; border: 1px solid var(--border); box-shadow: 0 10px 30px rgba(0,0,0,0.25); }
  h2 { margin-top: 0; font-size: 17px; color: #fff; display: flex; align-items: center; gap: 8px; }
  label { display: block; font-size: 11.5px; color: var(--muted); margin-bottom: 5px; margin-top: 10px; font-weight: 600; }
  input, select, textarea { width: 100%; padding: 9px 11px; border-radius: 8px; border: 1px solid #334155; background: var(--card-alt); color: #fff; font-family: inherit; font-size: 13px; transition: border-color 0.2s; }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--blue); }
  button { margin-top: 12px; width: 100%; padding: 11px 16px; background: var(--accent); color: #fff; border: none; border-radius: 8px; font-weight: 700; cursor: pointer; font-size: 13.5px; transition: 0.15s; }
  button:hover { opacity: 0.92; }
  button:disabled { opacity: 0.5; cursor: wait; }
  .camp-item { background: var(--card-alt); border: 1px solid var(--border); border-radius: 10px; padding: 11px 14px; margin-bottom: 9px; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .btn-sm { margin: 0; width: auto; padding: 8px 13px; font-size: 12px; background: var(--blue); white-space: nowrap; border-radius: 7px; font-weight: 600; }
  .clip-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 14px; margin-top: 12px; }
  .clip-card { background: var(--card-alt); border: 1px solid #10b98144; border-radius: 12px; padding: 12px; display: flex; flex-direction: column; justify-content: space-between; }
  .clip-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 10px; }
  video { width: 100%; height: 270px; object-fit: contain; border-radius: 8px; background: #000; margin-top: 8px; border: 1px solid #1e293b; }
  .status-banner { margin-top: 12px; padding: 10px 14px; border-radius: 8px; background: rgba(59,130,246,0.12); border: 1px solid rgba(59,130,246,0.3); font-size: 13px; color: #60a5fa; font-weight: 600; display: none; }
</style>
</head>
<body>
<div class="container">
  <!-- TOP HEADER -->
  <div class="header">
    <div>
      <h1 style="margin:0;font-size:21px;letter-spacing:-0.3px;">⚡ Whop & ContentRewards AI Clipper Studio</h1>
      <p style="margin:4px 0 0;color:var(--muted);font-size:12px;">
        Open-Source 1080p Vertical Clipping, AI Copywriting & Bounty Discovery Suite by <strong>Ocalir Labs</strong>
        • Whop API: <strong id="whop-title" style="color:#10b981;">Checking...</strong>
        • AI Copywriter: <strong id="gemini-status" style="color:#38bdf8;">Checking...</strong>
      </p>
    </div>
    <div class="sponsor-bar">
      <a class="sponsor-btn" style="background:#ff5f5f;" href="https://checkout.dodopayments.com/buy/pdt_0NqNaai72jdmYgFMZLV0j?quantity=1" target="_blank">☕ Buy Me a Coffee (Dodo)</a>
      <a class="sponsor-btn" style="background:#db61a2;" href="https://github.com/sponsors/ocalirdigital" target="_blank">💖 GitHub Sponsors</a>
      <div class="badge" id="whop-badge">● Engine Ready</div>
    </div>
  </div>

  <!-- ALWAYS-VISIBLE ACCOUNT & API KEYS SETUP PANEL -->
  <div class="config-bar" id="config-panel">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
      <div>
        <strong style="font-size:14px;color:#fff;">🔑 Account, Whop & AI API Configuration (Saved strictly to your local <code>.env</code> — Never uploaded to GitHub)</strong>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">Enter your own keys below to unlock LLM Hook/Caption Generation & Whop Store verification.</span>
      </div>
      <div style="display:flex;gap:8px;">
        <a href="https://contentrewards.com/c/discover" target="_blank" style="text-decoration:none;"><button type="button" class="btn-sm" style="background:#334155;">🌐 Login to ContentRewards ↗</button></a>
        <a href="https://aistudio.google.com/app/apikey" target="_blank" style="text-decoration:none;"><button type="button" class="btn-sm" style="background:#334155;">✨ Get Free Gemini Key ↗</button></a>
      </div>
    </div>
    <div class="config-grid">
      <div>
        <label style="margin-top:0;">Gemini API Key (AI Hooks & Captions)</label>
        <input type="password" id="gemini-key-input" placeholder="AIzaSy... (Google AI Studio)">
      </div>
      <div>
        <label style="margin-top:0;">OpenAI API Key (Optional Alternative)</label>
        <input type="password" id="openai-key-input" placeholder="sk-proj-... (GPT-4o-mini)">
      </div>
      <div>
        <label style="margin-top:0;">Whop Company API Key (Optional)</label>
        <input type="password" id="whop-key-input" placeholder="apik_... (Whop Developer)">
      </div>
      <div>
        <label style="margin-top:0;">Default Campaign / Brand Tag</label>
        <input type="text" id="cr-handle-input" placeholder="@callofduty" value="@callofduty">
      </div>
      <div style="display:flex;gap:8px;">
        <button type="button" class="btn-sm" style="background:var(--green);padding:10px 16px;" onclick="saveApiKeys()">💾 Save to Local .env</button>
        <button type="button" class="btn-sm" style="background:#ef4444;padding:10px 12px;" onclick="clearApiKeys()" title="Clear all saved keys from local .env">🧹 Clear</button>
      </div>
    </div>
  </div>

  <!-- MAIN 2-COLUMN STUDIO GRID -->
  <div class="grid">
    <!-- LEFT COLUMN: 1080p FFmpeg Clipping Studio -->
    <div class="card">
      <h2>🎬 1. Automated 1080p Vertical Clipping Studio (9:16)</h2>
      <form id="clip-form">
        <label>Source Video (YouTube / Stream URL or Local Official MP4 File Path)</label>
        <div style="display:flex;gap:8px;">
          <input type="text" id="url-input" name="url" value="https://www.youtube.com/watch?v=nemUC34Q2hQ" placeholder="https://www.youtube.com/watch?v=... or C:\\path\\to\\video.mp4" required>
          <button type="button" class="btn-sm" style="background:var(--purple);padding:0 14px;" id="ai-gen-btn" onclick="generateWithAI()">✨ AI Auto-Fill Hook & Caption</button>
        </div>

        <label>Editing & Campaign Compliance Mode</label>
        <select name="mode" id="mode-select" onchange="toggleVoBox()">
          <option value="clean_official">✅ Mode 1: Official Campaign Safe Mode (FTC Compliant • 1080p HD • Original Audio • No AI Voice)</option>
          <option value="fast_montage">🔥 Mode 2: 4-Scene Fast-Cut Action Montage (1080p Highlights • Original Audio)</option>
          <option value="ai_voice_captions">🎙️ Mode 3: AI Voiceover + Word-by-Word Highlighted Captions (Edge-TTS WordBoundary)</option>
        </select>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
          <div>
            <label>Start Second (0 = Auto Heatmap Peak)</label>
            <input type="number" id="start-input" name="start" value="0" min="0" required>
          </div>
          <div>
            <label>Clip Duration (Seconds)</label>
            <input type="number" name="duration" value="18" min="5" max="60" required>
          </div>
        </div>

        <label>On-Screen Hook Text (Optional — Burned at Top of Frame)</label>
        <input type="text" id="hook-input" name="on_screen_text" value="Season 1 of COD MW4 is feeding us with new content">

        <div id="vo-box" style="display:none;">
          <label>AI Voiceover Script (Used in Mode 3 Only)</label>
          <input type="text" id="vo-input" name="vo_text" placeholder="Call of Duty MW4 Season 1 just dropped and these new maps look insane!">
        </div>

        <label>FTC-Compliant Post Caption (#Ad on its own line + @brand tag)</label>
        <textarea id="caption-box" rows="2">The new MW4 Season 1 maps and updates look insane, which map are you dropping into first? @callofduty
#Ad #MW4 #CallOfDuty</textarea>

        <div style="display:grid;grid-template-columns:1.5fr 1fr;gap:10px;">
          <button type="submit" id="submit-btn">🚀 Render 1080p Vertical Clip</button>
          <button type="button" style="background:var(--purple);" onclick="copyCaption()">📋 Copy FTC Caption</button>
        </div>
      </form>

      <div id="status-msg" class="status-banner"></div>

      <h2 style="margin-top:22px;">🎥 Generated Vertical Clips (<span id="clip-count">0</span> Ready in <code>clips/</code>)</h2>
      <div id="clips-list" class="clip-grid">Loading...</div>
    </div>

    <!-- RIGHT COLUMN: Live ContentRewards Scraper -->
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <h2 style="margin:0;">🔥 2. Live ContentRewards Bounty Feed (<span id="camp-count">0</span> Active Pools)</h2>
        <button class="btn-sm" onclick="loadCampaigns()">🔄 Refresh Live Feed</button>
      </div>
      <p style="font-size:12px;color:var(--muted);margin-top:0;">
        Real-time campaigns and source videos fetched directly from <code>contentrewards.com/c/discover</code>:
      </p>

      <h3 style="font-size:13px;color:#38bdf8;margin-bottom:8px;">⚡ Ready Source Videos (1-Click Load into Studio):</h3>
      <div id="yt-sources" style="max-height:220px;overflow-y:auto;margin-bottom:16px;">Loading...</div>

      <h3 style="font-size:13px;color:#10b981;margin-bottom:8px;">💰 Active Bounty Campaigns ($8,000 – $50,000 Pools):</h3>
      <div id="camp-list" style="max-height:480px;overflow-y:auto;">Loading...</div>
    </div>
  </div>
</div>

<script>
async function saveApiKeys() {
  const gk = document.getElementById('gemini-key-input').value;
  const ok = document.getElementById('openai-key-input').value;
  const wk = document.getElementById('whop-key-input').value;
  const ch = document.getElementById('cr-handle-input').value;
  await fetch('/api/save-keys', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({gemini_key: gk, openai_key: ok, whop_key: wk, cr_handle: ch})
  });
  document.getElementById('gemini-key-input').value = '';
  document.getElementById('openai-key-input').value = '';
  document.getElementById('whop-key-input').value = '';
  showBanner('✅ API keys & brand tag saved strictly to your local .env file!', '#10b981');
  loadStatus();
}

async function clearApiKeys() {
  await fetch('/api/save-keys', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({clear_all: true})
  });
  document.getElementById('gemini-key-input').value = '';
  document.getElementById('openai-key-input').value = '';
  document.getElementById('whop-key-input').value = '';
  showBanner('🧹 Cleared all personal API keys from local .env!', '#f59e0b');
  loadStatus();
}

async function generateWithAI() {
  const url = document.getElementById('url-input').value;
  if (!url) return;
  const btn = document.getElementById('ai-gen-btn');
  btn.disabled = true;
  btn.innerText = '⏳ Analyzing...';
  showBanner('✨ Analyzing source video metadata, heatmap peak & generating FTC-compliant copy...', '#a855f7');
  try {
    const res = await fetch('/api/ai-generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url})
    });
    const out = await res.json();
    if (out.ok) {
      document.getElementById('start-input').value = out.start_sec;
      document.getElementById('hook-input').value = out.on_screen_text;
      document.getElementById('vo-input').value = out.vo_text;
      document.getElementById('caption-box').value = out.caption;
      showBanner('✨ AI Auto-Fill Complete (' + out.source + ')! Peak start: ' + out.start_sec + 's.', '#10b981');
    } else {
      showBanner('❌ AI Error: ' + out.error, '#ef4444');
    }
  } catch (e) {
    showBanner('❌ Error: ' + e, '#ef4444');
  }
  btn.disabled = false;
  btn.innerText = '✨ AI Auto-Fill Hook & Caption';
}

function showBanner(text, color = '#60a5fa') {
  const el = document.getElementById('status-msg');
  el.style.display = 'block';
  el.style.color = color;
  el.style.borderColor = color;
  el.innerText = text;
}

function toggleVoBox() {
  const m = document.getElementById('mode-select').value;
  document.getElementById('vo-box').style.display = (m === 'ai_voice_captions') ? 'block' : 'none';
}

function selectVideo(url) {
  const inp = document.getElementById('url-input');
  inp.value = url;
  inp.style.borderColor = '#10b981';
  showBanner('⚡ Source video loaded into Studio: ' + url + ' — Click "✨ AI Auto-Fill" or "🚀 Render"!', '#10b981');
  window.scrollTo({top: 0, behavior: 'smooth'});
}

async function copyCaption() {
  const txt = document.getElementById('caption-box').value;
  try {
    await navigator.clipboard.writeText(txt);
  } catch (e) {}
  showBanner('📋 FTC-compliant caption copied to clipboard! Ready to paste (Ctrl+V).', '#10b981');
}

async function openTikTokAndFolder(filename) {
  await copyCaption();
  window.open('https://www.tiktok.com/tiktokstudio/upload', '_blank');
  await fetch('/api/show-file', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file: filename})
  });
  showBanner('🚀 TikTok Studio opened in a new tab, caption copied to clipboard, and ' + filename + ' highlighted in Explorer!', '#10b981');
}

async function showInFolder(filename) {
  const res = await fetch('/api/show-file', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file: filename})
  });
  const out = await res.json();
  if (out.ok) {
    showBanner('📂 Opened ' + filename + ' in Windows Explorer.', '#10b981');
  }
}

async function deleteClip(filename) {
  await fetch('/api/delete-clip', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file: filename})
  });
  showBanner('🗑️ Deleted ' + filename, '#f59e0b');
  loadStatus();
}

async function loadStatus() {
  const res = await fetch('/api/status');
  const data = await res.json();
  if (data.whop.connected) {
    document.getElementById('whop-title').innerText = data.whop.title;
    document.getElementById('whop-badge').innerText = '● Whop API v5 Linked';
  } else {
    document.getElementById('whop-title').innerText = data.whop.title;
    document.getElementById('whop-badge').innerText = '● Engine Ready';
  }
  if (data.whop.gemini) {
    document.getElementById('gemini-status').innerText = 'Gemini 2.5 Connected (' + data.whop.gemini_masked + ')';
  } else if (data.whop.openai) {
    document.getElementById('gemini-status').innerText = 'OpenAI Connected (' + data.whop.openai_masked + ')';
  } else {
    document.getElementById('gemini-status').innerText = 'Built-in Heatmap Analyzer (Add Gemini/OpenAI Key Below)';
  }
  if (data.whop.whop_masked) {
    document.getElementById('whop-key-input').placeholder = 'Saved: ' + data.whop.whop_masked;
  } else {
    document.getElementById('whop-key-input').placeholder = 'apik_... (Whop Developer)';
  }
  if (data.whop.gemini_masked) {
    document.getElementById('gemini-key-input').placeholder = 'Saved: ' + data.whop.gemini_masked;
  } else {
    document.getElementById('gemini-key-input').placeholder = 'AIzaSy... (Google AI Studio)';
  }
  if (data.whop.openai_masked) {
    document.getElementById('openai-key-input').placeholder = 'Saved: ' + data.whop.openai_masked;
  } else {
    document.getElementById('openai-key-input').placeholder = 'sk-proj-... (GPT-4o-mini)';
  }
  if (data.whop.cr_handle) {
    document.getElementById('cr-handle-input').value = data.whop.cr_handle;
  }

  const clips = data.clips || [];
  document.getElementById('clip-count').innerText = clips.length;
  const clipsDiv = document.getElementById('clips-list');
  if (clips.length === 0) {
    clipsDiv.innerHTML = '<p style="font-size:13px;color:#64748b;">No clips generated yet. Click Render above!</p>';
  } else {
    clipsDiv.innerHTML = clips.map(c => `
      <div class="clip-card">
        <div>
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
            <strong style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${c}">🎬 ${c}</strong>
            <a href="/clips/${c}" download style="color:#38bdf8;font-size:11px;text-decoration:none;font-weight:700;white-space:nowrap;">⬇️ MP4</a>
          </div>
          <video src="/clips/${c}" controls preload="metadata"></video>
        </div>
        <div class="clip-actions">
          <button class="btn-sm" style="background:#10b981;grid-column:span 2;" onclick="openTikTokAndFolder('${c}')">🚀 Post on TikTok Studio</button>
          <button class="btn-sm" style="background:#3b82f6;" onclick="showInFolder('${c}')">📂 Explorer</button>
          <button class="btn-sm" style="background:#ef4444;" onclick="deleteClip('${c}')">🗑️ Delete</button>
        </div>
      </div>
    `).join('');
  }
}

async function loadCampaigns() {
  document.getElementById('camp-list').innerHTML = '<p style="font-size:13px;color:#94a3b8;">Fetching live campaigns...</p>';
  const res = await fetch('/api/campaigns');
  const data = await res.json();
  document.getElementById('camp-count').innerText = data.campaign_count || 0;

  const ytDiv = document.getElementById('yt-sources');
  ytDiv.innerHTML = (data.youtube_sources || []).filter(u => u.includes('watch?v=')).map(u => `
    <div class="camp-item">
      <div style="font-size:12px;overflow:hidden;text-overflow:ellipsis;">🎬 <a href="${u}" target="_blank" style="color:#cbd5e1;">${u}</a></div>
      <button class="btn-sm" onclick="selectVideo('${u}')">⚡ Load in Studio</button>
    </div>
  `).join('');

  const cDiv = document.getElementById('camp-list');
  cDiv.innerHTML = (data.campaigns || []).map(c => `
    <div class="camp-item">
      <div>
        <div style="font-weight:700;font-size:13px;color:#fff;">${c.title}</div>
        <div style="font-size:11px;color:#10b981;margin-top:3px;">💵 Est. Rate: ~$${c.rate} / 1K Views</div>
      </div>
      <a href="${c.url}" target="_blank" style="text-decoration:none;"><button class="btn-sm" style="background:#ff5722;">Open Campaign ↗</button></a>
    </div>
  `).join('');
}

document.getElementById('clip-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.innerText = '⏳ Rendering 1080p Vertical Clip...';
  showBanner('⏳ Downloading 1080p source & running FFmpeg 9:16 vertical pipeline (15-25s)...', '#38bdf8');
  const formData = new FormData(e.target);
  const body = Object.fromEntries(formData.entries());
  try {
    const res = await fetch('/api/clip', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const out = await res.json();
    if (out.ok) {
      showBanner('✅ 1080p Vertical Clip Ready: ' + out.file + ' — Preview below!', '#10b981');
      loadStatus();
    } else {
      showBanner('❌ Error: ' + out.error, '#ef4444');
    }
  } catch (err) {
    showBanner('❌ Connection Error: ' + err, '#ef4444');
  }
  btn.disabled = false;
  btn.innerText = '🚀 Render 1080p Vertical Clip';
});

loadStatus();
loadCampaigns();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/api/status":
            clips = [f.name for f in sorted(CLIPS_DIR.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True)]
            payload = {"whop": get_whop_status(), "clips": clips}
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        elif self.path == "/api/campaigns":
            try:
                data = fetch_live_campaigns()
            except Exception as e:
                data = {"campaign_count": 0, "campaigns": [], "youtube_sources": [], "error": str(e)}
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path.startswith("/clips/"):
            fname = urllib.parse.unquote(self.path.split("/clips/", 1)[1].split("?")[0])
            fpath = (CLIPS_DIR / fname).resolve()
            if not fpath.exists() or fpath.parent != CLIPS_DIR.resolve():
                self.send_response(404)
                self.end_headers()
                return
            file_size = fpath.stat().st_size
            range_header = self.headers.get("Range")
            if range_header:
                m = re.search(r"bytes=(\d+)-(\d*)", range_header)
                if m:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else file_size - 1
                    end = min(end, file_size - 1)
                    length = end - start + 1
                    with open(fpath, "rb") as f:
                        f.seek(start)
                        chunk = f.read(length)
                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Content-Length", str(length))
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
            data = fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

        if self.path == "/api/save-keys":
            if body.get("clear_all"):
                save_env("", "", "", "@callofduty")
            else:
                env_now = load_env()
                wk = body.get("whop_key") if body.get("whop_key") else env_now.get("WHOP_API_KEY", "")
                gk = body.get("gemini_key") if body.get("gemini_key") else env_now.get("GEMINI_API_KEY", "")
                ok = body.get("openai_key") if body.get("openai_key") else env_now.get("OPENAI_API_KEY", "")
                ch = body.get("cr_handle") if body.get("cr_handle") else env_now.get("CREATOR_HANDLE", "@callofduty")
                save_env(wk, gk, ok, ch)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

        elif self.path == "/api/ai-generate":
            try:
                res = generate_ai_metadata(body.get("url", ""))
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/show-file":
            vfile = (CLIPS_DIR / body.get("file", "")).resolve()
            if vfile.exists() and vfile.parent == CLIPS_DIR.resolve():
                subprocess.Popen(["explorer.exe", f"/select,{vfile}"])
                res = {"ok": True}
            else:
                res = {"ok": False, "error": "File not found"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/delete-clip":
            vfile = (CLIPS_DIR / body.get("file", "")).resolve()
            if vfile.exists() and vfile.parent == CLIPS_DIR.resolve():
                vfile.unlink()
                res = {"ok": True}
            else:
                res = {"ok": False, "error": "File not found"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/clip":
            try:
                fname = create_vertical_clip(
                    video_url=body.get("url", ""),
                    start_sec=int(body.get("start", 0)),
                    duration_sec=int(body.get("duration", 18)),
                    mode=body.get("mode", "clean_official"),
                    on_screen_text=body.get("on_screen_text", ""),
                    vo_text=body.get("vo_text", ""),
                )
                res = {"ok": True, "file": fname}
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Whop & ContentRewards AI Clipper Studio running at: http://127.0.0.1:8765")
    server.serve_forever()
