import json
import time
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:8765"
ASSETS_DIR = Path(__file__).parent / "assets"
ASSETS_DIR.mkdir(exist_ok=True)


def run_tests():
    print("=== 1. Testing /api/status (Privacy Masking & Clips) ===")
    with urllib.request.urlopen(f"{BASE_URL}/api/status", timeout=10) as r:
        st = json.loads(r.read().decode())
        print("Status OK:", st["whop"]["title"], "| Clips found:", len(st["clips"]))
        assert "***" in st["whop"]["title"] or "Not Linked" in st["whop"]["title"], "Personal info must be masked!"

    print("\n=== 2. Testing /api/campaigns (Live ContentRewards Feed) ===")
    with urllib.request.urlopen(f"{BASE_URL}/api/campaigns", timeout=15) as r:
        cp = json.loads(r.read().decode())
        print(f"Campaigns OK: {cp['campaign_count']} live pools, {len(cp['youtube_sources'])} YouTube sources")
        assert cp["campaign_count"] > 0, "Expected live campaigns from ContentRewards"

    print("\n=== 3. Testing /api/ai-generate (AI Auto-Fill Hook, Voiceover & FTC Caption) ===")
    ai_payload = json.dumps({"url": "https://www.youtube.com/watch?v=nemUC34Q2hQ"}).encode("utf-8")
    req_ai = urllib.request.Request(
        f"{BASE_URL}/api/ai-generate",
        data=ai_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req_ai, timeout=35) as r:
        ai_out = json.loads(r.read().decode())
        print("AI Generate OK:", ai_out)
        assert ai_out.get("ok") is True

    print("\n=== 4. Testing HTTP 206 Range Video Streaming ===")
    existing_clip = st["clips"][0]
    req_range = urllib.request.Request(
        f"{BASE_URL}/clips/{existing_clip}",
        headers={"Range": "bytes=0-1023"},
    )
    with urllib.request.urlopen(req_range, timeout=10) as r:
        print(f"Video Stream Status: {r.status} | Content-Range: {r.headers.get('Content-Range')}")
        assert r.status == 206, "Expected HTTP 206 Partial Content for Chrome <video> player"

    print("\n=== 5. Capturing Clean Dashboard Screenshot via Playwright ===")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        page.goto(BASE_URL, wait_until="networkidle")
        time.sleep(2.5)
        screenshot_path = ASSETS_DIR / "dashboard_preview.png"
        page.screenshot(path=str(screenshot_path), full_page=False)
        browser.close()
        print(f"Saved UI Screenshot -> {screenshot_path} ({screenshot_path.stat().st_size} bytes)")

    print("\nALL E2E TESTS PASSED 100%!")


if __name__ == "__main__":
    run_tests()
