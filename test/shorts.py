"""Find a viral game/movie/TV idea, write a script, find stock footage + music, and render a YouTube Short.

Usage:  python shorts.py            # pick from trending
        python shorts.py "topic"    # force a topic
Needs:  ANTHROPIC_API_KEY, PIXABAY_API_KEY (free: pixabay.com/api/docs) or PEXELS_API_KEY, mp3s in music/ (optional)
Output: out/<slug>/short.mp4, upload.txt, and the raw pieces (clips/, voice.mp3, subs.srt, music) for CapCut
"""
import asyncio, os, random, re, shutil, subprocess, sys
import xml.etree.ElementTree as ET
from pathlib import Path

import anthropic, edge_tts, imageio_ffmpeg, requests
from pydantic import BaseModel

SUBREDDITS = ["movies", "television", "gaming", "Games", "boxoffice", "marvelstudios"]
NEWS_QUERIES = ["video game", "movie trailer", "box office", "tv series", "netflix series"]
VOICE = "en-US-AndrewNeural"
MUSIC_VOLUME = 0.12
BACKGROUNDS = Path("backgrounds")  # fallback clips when there's no Pexels key (gameplay you own, etc.)
MUSIC = Path("music")              # royalty-free mp3s, e.g. from the YouTube Audio Library
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# keys from .env next to this file; real environment variables win
env = Path(__file__).with_name(".env")
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.strip().startswith("#") and v.strip():
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


class Short(BaseModel):
    topic: str
    why_viral: str
    title: str          # <= 70 chars, with #shorts
    narration: str      # spoken script, 90-130 words, strong hook in first sentence
    scenes: list[str]   # 5-7 stock-footage search terms, in narration order
    description: str
    tags: list[str]


def fetch_trends(limit=15):
    # RSS feeds: no API keys. Reddit's JSON API 403s and its RSS often 429s, so Google News is the backbone.
    feeds = {f"r/{s} hot": f"https://www.reddit.com/r/{s}/hot/.rss?limit={limit}" for s in SUBREDDITS}
    feeds |= {f"news: {q}": f"https://news.google.com/rss/search?q={q.replace(' ', '+')}+when:2d&hl=en-US&gl=US&ceid=US:en"
              for q in NEWS_QUERIES}
    headlines = []
    for label, url in feeds.items():
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 shorts-idea-finder"}, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"skip {label}: {e}")
            continue
        for el in list(ET.fromstring(r.content).iter())[:400]:
            if el.tag in ("item", "{http://www.w3.org/2005/Atom}entry"):
                title = el.findtext("title") or el.findtext("{http://www.w3.org/2005/Atom}title")
                headlines.append(f"[{label}] {title}")
                if sum(h.startswith(f"[{label}]") for h in headlines) >= limit:
                    break
    return headlines


def write_script(headlines, topic=None):
    ask = (f"Make the Short about: {topic}" if topic else
           "Pick the ONE topic with the most viral potential right now (high engagement, curiosity, debate).")
    resp = anthropic.Anthropic().messages.parse(
        model="claude-opus-5-5",
        max_tokens=16000,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content":
            "You write viral YouTube Shorts about games, movies and TV series.\n"
            f"Trending headlines right now:\n" + "\n".join(headlines) + f"\n\n{ask}\n"
            "Write a 30-45 second voiceover: first sentence is a scroll-stopping hook, short punchy sentences, "
            "one surprising fact or hot take, end with a question that makes people comment. "
            "Only state facts the headlines support or that are well known; no made-up news. "
            "Plain spoken text only, no emojis, no stage directions.\n"
            "For scenes: generic stock-footage search terms that match the mood of each part "
            "(e.g. 'gamer neon room', 'movie theater audience', 'dark city night'). "
            "No brand, actor or title names: stock sites don't have those."}],
        output_format=Short,
    )
    if resp.stop_reason == "refusal":
        sys.exit(f"Claude declined: {resp.stop_details}")
    return resp.parsed_output


def search_pexels(term, key):
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                     params={"query": term, "orientation": "portrait", "per_page": 10}, timeout=20)
    r.raise_for_status()
    out = []
    for v in r.json()["videos"]:
        # smallest file that is still at least 1080 tall: sharp enough, quick to download
        files = sorted((f for f in v["video_files"] if (f["height"] or 0) >= 1080), key=lambda f: f["height"]) \
            or sorted(v["video_files"], key=lambda f: -(f["height"] or 0))
        out.append((v["id"], files[0]["link"]))
    return out


def search_pixabay(term, key):
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": key, "q": term, "per_page": 10, "safesearch": "true"}, timeout=20)
    r.raise_for_status()
    return [(v["id"], v["videos"]["large"]["url"] or v["videos"]["medium"]["url"]) for v in r.json()["hits"]]


def fetch_clips(terms, folder):
    """Download one stock clip per search term from Pexels or Pixabay (royalty-free, safe to upload)."""
    if key := os.environ.get("PEXELS_API_KEY"):
        search = lambda t: search_pexels(t, key)
    elif key := os.environ.get("PIXABAY_API_KEY"):
        search = lambda t: search_pixabay(t, key)
    else:
        print("no PEXELS_API_KEY or PIXABAY_API_KEY: using backgrounds/ or a gradient")
        own = sorted(BACKGROUNDS.glob("*.mp4"))
        return [shutil.copy(c, folder) for c in random.sample(own, min(len(terms), len(own)))]
    clips, used = [], set()
    for i, term in enumerate(terms):
        found = [(vid, url) for vid, url in search(term) if vid not in used]
        if not found:
            print(f"no clip for '{term}'")
            continue
        vid, url = random.choice(found[:5])
        used.add(vid)
        path = folder / f"{i:02}.mp4"
        path.write_bytes(requests.get(url, timeout=120).content)
        clips.append(path)
        print(f"clip {i}: '{term}'")
    return clips


async def _tts(text, mp3):
    words = []
    with open(mp3, "wb") as f:
        async for chunk in edge_tts.Communicate(text, VOICE, rate="+10%", boundary="WordBoundary").stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7  # 100ns ticks -> seconds
                words.append((start, start + chunk["duration"] / 1e7, chunk["text"]))
    return words


def caption_groups(words, per_line=3):
    for i in range(0, len(words), per_line):
        group = words[i:i + per_line]
        end = words[i + per_line][0] if i + per_line < len(words) else group[-1][1] + 0.5
        yield group[0][0], end, " ".join(w for _, _, w in group).upper()


def make_subs(words, folder):
    """subs.ass is burned into short.mp4; subs.srt is for importing into CapCut."""
    def ass_t(t): return f"{int(t // 3600)}:{int(t % 3600 // 60):02}:{t % 60:05.2f}"
    def srt_t(t): return f"{int(t // 3600):02}:{int(t % 3600 // 60):02}:{int(t % 60):02},{int(t % 1 * 1000):03}"
    ass = ["[Script Info]", "PlayResX: 1080", "PlayResY: 1920", "",
           "[V4+ Styles]",
           "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Bold",
           "Style: Big, Arial, 84, &H0000FFFF, &H00000000, 1, 7, 3, 5, 80, 80, 0, -1", "",
           "[Events]", "Format: Layer, Start, End, Style, Text"]
    srt = []
    for n, (start, end, text) in enumerate(caption_groups(words), 1):
        ass.append(f"Dialogue: 0,{ass_t(start)},{ass_t(end)},Big,{text.replace('{', '(').replace('}', ')')}")
        srt.append(f"{n}\n{srt_t(start)} --> {srt_t(end)}\n{text}\n")
    (folder / "subs.ass").write_text("\n".join(ass), encoding="utf-8")
    (folder / "subs.srt").write_text("\n".join(srt), encoding="utf-8")


def render(folder, clips, duration, music=None):
    """Cut the clips evenly across the voiceover, crop to 9:16, burn captions, mix voice + quiet music."""
    inputs, chains = [], []
    if clips:
        per = duration / len(clips)
        for i, c in enumerate(clips):
            inputs += ["-stream_loop", "-1", "-i", str(Path(c).resolve())]  # loop clips shorter than their slot
            chains.append(f"[{i}:v]trim=duration={per:.3f},setpts=PTS-STARTPTS,fps=30,"
                          f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v{i}]")
        chains.append("".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[bg]")
    else:  # nothing to show: animated gradient
        inputs += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "gradients=s=1080x1920:speed=0.01:n=3:r=30"]
        chains.append("[0:v]null[bg]")
    chains.append("[bg]subtitles=subs.ass[v]")
    voice = len(clips) if clips else 1  # ffmpeg input index of voice.mp3
    inputs += ["-i", "voice.mp3"]
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
        chains.append(f"[{voice + 1}:a]volume={MUSIC_VOLUME}[m];[{voice}:a][m]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        chains.append(f"[{voice}:a]anull[a]")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(chains),
                    "-map", "[v]", "-map", "[a]", "-t", f"{duration:.3f}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "short.mp4"],
                   cwd=folder, check=True)  # cwd so the subtitles path needs no Windows escaping


def main():
    topic = " ".join(sys.argv[1:]) or None
    headlines = fetch_trends()
    print(f"{len(headlines)} trending headlines")
    s = write_script(headlines, topic)
    print(f"Topic: {s.topic}\nWhy: {s.why_viral}\nScenes: {s.scenes}\n")

    folder = Path("out") / re.sub(r"[^a-z0-9]+", "-", s.topic.lower()).strip("-")[:50]
    (folder / "clips").mkdir(parents=True, exist_ok=True)
    words = asyncio.run(_tts(s.narration, folder / "voice.mp3"))
    make_subs(words, folder)
    clips = fetch_clips(s.scenes, folder / "clips")
    tracks = list(MUSIC.glob("*.mp3"))
    music = shutil.copy(random.choice(tracks), folder) if tracks else None
    render(folder, clips, words[-1][1] + 0.8, music)
    (folder / "upload.txt").write_text(
        f"TITLE:\n{s.title}\n\nDESCRIPTION:\n{s.description}\n\nTAGS:\n{', '.join(s.tags)}\n\nSCRIPT:\n{s.narration}\n",
        encoding="utf-8")
    print(f"Done: {folder / 'short.mp4'}")


if __name__ == "__main__":
    main()
