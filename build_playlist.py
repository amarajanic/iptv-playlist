#!/usr/bin/env python3
"""
Build a custom, provider-style IPTV playlist from iptv-org's free playlists.

Each entry in GROUPS becomes one folder in your IPTV app. Edit the list below
to add, remove, rename or reorder folders. No extra Python packages needed.

Usage:
    python build_playlist.py                     # writes playlist.m3u
    python build_playlist.py --check             # also removes dead streams
    python build_playlist.py --output my.m3u
"""

import argparse
import concurrent.futures
import re
import sys
import urllib.request

BASE = "https://iptv-org.github.io/iptv"

# ---------------------------------------------------------------------------
# YOUR FOLDERS
# name:           folder name shown in the TV app
# sources:        iptv-org playlists to pull channels from (in this order)
# only_countries: optional - keep only channels from these country codes
#                 (useful to shrink big worldwide categories like News)
# ---------------------------------------------------------------------------
GROUPS = [
    {
        "name": "EX-YU",
        "sources": [
            "countries/ba.m3u",  # Bosnia and Herzegovina (listed first)
            "countries/hr.m3u",  # Croatia
            "countries/rs.m3u",  # Serbia
            "countries/me.m3u",  # Montenegro
            "countries/si.m3u",  # Slovenia
            "countries/mk.m3u",  # North Macedonia
            "countries/xk.m3u",  # Kosovo
        ],
    },
    {"name": "Albania", "sources": ["countries/al.m3u"]},
    {"name": "International", "sources": ["countries/int.m3u"]},
    {
        "name": "News",
        "sources": ["categories/news.m3u"],
        # Worldwide news has 1000+ channels; keep a manageable selection.
        # Delete this line to get all of them.
        "only_countries": ["ba", "hr", "rs", "me", "si", "mk", "xk",
                           "us", "uk", "gb", "de", "fr", "tr", "qa", "int"],
    },
    {"name": "Movies", "sources": ["categories/movies.m3u"]},
    {"name": "Series", "sources": ["categories/series.m3u"]},
    {"name": "Sports", "sources": ["categories/sports.m3u"]},
    {"name": "Documentary", "sources": ["categories/documentary.m3u"]},
    {"name": "Kids", "sources": ["categories/kids.m3u"]},
    {"name": "Music", "sources": ["categories/music.m3u"]},
]

# Skip channels whose name contains any of these words (case-insensitive).
# Example: ["[Geo-blocked]", "[Not 24/7]"]
EXCLUDE_KEYWORDS = []

USER_AGENT = "Mozilla/5.0 (custom-iptv-playlist-builder)"


def fetch(path, retries=2):
    url = f"{BASE}/{path}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                print(f"  ! could not download {path}: {exc}", file=sys.stderr)
                return None
    return None


def parse_m3u(text):
    """Return entries: {'extinf': str, 'opts': [str], 'url': str}."""
    entries, extinf, opts = [], None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            extinf, opts = line, []
        elif line.startswith("#"):
            if extinf:
                opts.append(line)  # e.g. #EXTVLCOPT referrer / user-agent
        else:
            if extinf:
                entries.append({"extinf": extinf, "opts": opts, "url": line})
            extinf, opts = None, []
    return entries


def channel_name(extinf):
    idx = extinf.rfind('",')
    return (extinf[idx + 2:] if idx != -1 else extinf.split(",", 1)[-1]).strip()


def channel_country(extinf):
    # tvg-id looks like "N1BosniaHerzegovina.ba@SD" -> "ba"
    m = re.search(r'tvg-id="[^"]*?\.([a-z]{2,3})(?:@[^"]*)?"', extinf, re.I)
    return m.group(1).lower() if m else None


def set_group(extinf, group):
    if re.search(r'group-title="[^"]*"', extinf):
        return re.sub(r'group-title="[^"]*"', f'group-title="{group}"', extinf, count=1)
    m = re.match(r"(#EXTINF:\s*-?\d+(?:\.\d+)?)(.*)", extinf)
    if m:
        return f'{m.group(1)} group-title="{group}"{m.group(2)}'
    return extinf


def is_alive(entry, timeout):
    headers = {"User-Agent": USER_AGENT}
    for opt in entry["opts"]:
        low = opt.lower()
        if low.startswith("#extvlcopt:http-user-agent="):
            headers["User-Agent"] = opt.split("=", 1)[1]
        elif low.startswith("#extvlcopt:http-referrer="):
            headers["Referer"] = opt.split("=", 1)[1]
    try:
        req = urllib.request.Request(entry["url"], headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(512)
            return resp.status < 400
    except Exception:  # noqa: BLE001
        return False


def build(check, timeout, workers):
    cache, output = {}, []
    for group in GROUPS:
        seen, items = set(), []
        allowed = set(group.get("only_countries", []))
        for src in group["sources"]:
            if src not in cache:
                print(f"Downloading {src} ...")
                text = fetch(src)
                cache[src] = parse_m3u(text) if text else []
            entries = sorted(cache[src], key=lambda e: channel_name(e["extinf"]).lower())
            for e in entries:
                name = channel_name(e["extinf"])
                if e["url"] in seen:
                    continue
                if any(k.lower() in name.lower() for k in EXCLUDE_KEYWORDS):
                    continue
                if allowed and channel_country(e["extinf"]) not in allowed:
                    continue
                seen.add(e["url"])
                items.append(e)
        print(f"  {group['name']}: {len(items)} channels")
        output.append((group["name"], items))

    if check:
        urls = {e["url"]: e for _, items in output for e in items}
        print(f"Checking {len(urls)} streams (this can take a few minutes) ...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = dict(zip(urls, pool.map(lambda e: is_alive(e, timeout), urls.values())))
        output = [(g, [e for e in items if results[e["url"]]]) for g, items in output]
        alive = sum(results.values())
        print(f"  {alive} working, {len(urls) - alive} removed")

    return output


def write(output, path):
    total = sum(len(items) for _, items in output)
    if total == 0:
        print("No channels found - not writing an empty playlist.", file=sys.stderr)
        sys.exit(1)
    lines = ["#EXTM3U"]
    for group, items in output:
        for e in items:
            lines.append(set_group(e["extinf"], group))
            lines.extend(e["opts"])
            lines.append(e["url"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {total} channels to {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", default="playlist.m3u")
    p.add_argument("--check", action="store_true", help="remove streams that don't respond")
    p.add_argument("--timeout", type=int, default=8)
    p.add_argument("--workers", type=int, default=32)
    args = p.parse_args()
    write(build(args.check, args.timeout, args.workers), args.output)


if __name__ == "__main__":
    main()
