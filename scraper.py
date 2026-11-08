import urllib.request
import re
import json


def fetch_live_campaigns():
    req = urllib.request.Request(
        "https://contentrewards.com/c/discover",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
    clean = html.replace('\\"', '"')

    camp_ids = list(dict.fromkeys(re.findall(r"/c/campaigns/([a-zA-Z0-9-]+)", clean)))
    yt_links = list(dict.fromkeys(re.findall(r"https://(?:www\.)?(?:youtube\.com|youtu\.be)/[a-zA-Z0-9_@/?=&-]+", clean)))
    drive_links = list(dict.fromkeys(re.findall(r"https://drive\.google\.com/[a-zA-Z0-9_/?=&-]+", clean)))

    campaigns = []
    seen_ids = set()
    for m in re.finditer(r'"id":"([0-9a-f-]{36})"', clean):
        cid = m.group(1)
        if cid in seen_ids:
            continue
        window = clean[m.start():m.start() + 1800]
        title_m = re.search(r'"title":"([^"]+)"', window)
        rate_m = re.search(r'"(?:cpm|rate|rewardRate|payoutRate)":\s*([0-9.]+)', window)
        yt_m = re.search(r'(https://(?:www\.)?(?:youtube\.com|youtu\.be)/[a-zA-Z0-9_@/?=&-]+)', window)
        if title_m:
            seen_ids.add(cid)
            campaigns.append({
                "id": cid,
                "title": title_m.group(1),
                "rate": rate_m.group(1) if rate_m else "1.50+",
                "youtube": yt_m.group(1) if yt_m else "",
                "url": f"https://contentrewards.com/c/campaigns/{cid}",
            })

    return {
        "campaign_count": len(camp_ids),
        "campaigns": campaigns[:30],
        "youtube_sources": yt_links[:25],
        "drive_sources": drive_links[:25],
    }


def fetch_campaign_details(cid: str):
    url = f"https://contentrewards.com/c/campaigns/{cid}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
    clean = html.replace('\\"', '"')
    yt_links = list(dict.fromkeys(re.findall(r"https://(?:www\.)?(?:youtube\.com|youtu\.be)/[a-zA-Z0-9_@/?=&-]+", clean)))
    drive_links = list(dict.fromkeys(re.findall(r"https://drive\.google\.com/[a-zA-Z0-9_/?=&-]+", clean)))
    dropbox_links = list(dict.fromkeys(re.findall(r"https://(?:www\.)?dropbox\.com/[a-zA-Z0-9_/?=&.-]+", clean)))
    return {
        "id": cid,
        "url": url,
        "youtube": yt_links,
        "drive": drive_links,
        "dropbox": dropbox_links,
    }


if __name__ == "__main__":
    d = fetch_campaign_details("78b4e379-2aa0-4d20-9fd0-ed02a816e3b1")
    print(json.dumps(d, indent=2))
