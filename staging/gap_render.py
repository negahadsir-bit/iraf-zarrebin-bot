#!/usr/bin/env python3
"""Renders a cut clip into the 1080x1920 template look. Series «گپ»: vertical interview clip -> 1080x1920 with word-group subtitles, guest line,
IRAFTV wordmark, soft bottom gradient and the animated «گپ» badge (top right, 100% opacity)."""
import json
import os
import re
import subprocess
import sys

from playwright.sync_api import sync_playwright

import words_timing as wt

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, FPS = 1080, 1920, 30
# layout in px at 1080x1920 (measured from the sample, 720x1280 x1.5)
SUB_Y, NAME_Y, MARK_Y = 1383, 1443, 1500
SUB_SIZE, NAME_SIZE, MARK_SIZE = 48, 32, 24.5
NAME_OPACITY = 0.30   # 10 points above the IRAFTV wordmark (0.20)
BADGE_X, BADGE_Y, BADGE_W = 708, 235, 219   # visible badge box (native 310x158 inside the 399x275 mov)
BADGE_NATIVE = (40, 57, 349, 214)

PAGE = """<!doctype html><meta charset=utf-8><style>
@font-face{font-family:Sub;src:url('file://%(h)s/font_sub.ttf')}
@font-face{font-family:Name;src:url('file://%(h)s/font_name.ttf')}
@font-face{font-family:Lato;font-weight:700;src:url('file://%(h)s/font_lato.ttf')}
html,body{margin:0;width:1080px;height:1920px;background:transparent;overflow:hidden}
.grad{position:absolute;left:0;top:0;width:1080px;height:1920px;opacity:.9;
 background:url('file://%(h)s/gradient_zb.png') 0 0/100%% 100%% no-repeat,url('file://%(h)s/gradient_zb.png') 0 0/100%% 100%% no-repeat}
.l{position:absolute;left:0;width:1080px;text-align:center;transform:translateY(-50%%);white-space:nowrap}
#sub{top:%(sy)spx;font:%(ss)spx Sub;color:#fff;direction:rtl;text-shadow:0 2px 14px rgba(0,0,0,.55)}
#name{top:%(ny)spx;font:%(ns)spx Name;color:#cfcfcf;opacity:%(no)s;direction:rtl}
#mark{top:%(my)spx;font:700 %(ms)spx Lato;letter-spacing:.74em;padding-left:.74em;color:#d9d9d9;opacity:.20;direction:ltr}
</style><body><div class=grad></div><div class=l id=sub></div><div class=l id=name></div><div class=l id=mark>IRAFTV</div>"""


def run(*a):
    subprocess.run(a, check=True)


def run_ffmpeg(total, progress, *cmd):
    """Runs ffmpeg; calls progress(0..1) as the output time advances (ffmpeg -progress pipe:1)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        if progress and line.startswith("out_time_us=") and total:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / total))
            except ValueError:
                pass
    if p.wait():
        raise subprocess.CalledProcessError(p.returncode, cmd[0])


def probe_len(path):
    return float(json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]))["format"]["duration"])


def _merge_singles(groups, words, max_words, max_chars):
    """A caption is never a single word: it joins the shorter neighbour (when it fits)."""
    def width(g):
        return len(" ".join(words[j] for j in g))
    g = [list(x) for x in groups]
    i = 0
    while i < len(g):
        if len(g[i]) == 1 and len(g) > 1:
            opts = []
            if i > 0 and len(g[i - 1]) + 1 <= max_words and width(g[i - 1] + g[i]) <= max_chars:
                opts.append((len(g[i - 1]), i - 1))
            if i + 1 < len(g) and len(g[i + 1]) + 1 <= max_words and width(g[i] + g[i + 1]) <= max_chars:
                opts.append((len(g[i + 1]), i + 1))
            if opts:
                _, j = min(opts)
                if j < i:
                    g[j] = g[j] + g[i]
                else:
                    g[i] = g[i] + g[j]
                    j = i + 1
                del g[j if j > i else i]
                i = max(0, i - 1)
                continue
        i += 1
    return g


def group_words(words, times, max_words=4, max_chars=26):
    groups, cur = [], []
    for i, (w, t) in enumerate(zip(words, times)):
        cur.append(i)
        text = " ".join(words[j] for j in cur)
        end_punct = bool(re.search(r"[،,.؟?!؛:]$", w))
        gap = (times[i + 1][0] - t[1]) if i + 1 < len(words) else 9
        if len(cur) >= max_words or len(text) >= max_chars or end_punct or gap > 0.3:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    groups = _merge_singles(groups, words, max_words + 1, max_chars + 6)
    out = []
    for g in groups:
        text = re.sub(r"[،,.؛:]+", "", " ".join(words[j] for j in g)).strip()
        out.append((text, times[g[0]][0], times[g[-1]][1]))
    return out


def caption_schedule(groups, total):
    """[(text|None, start, end)] covering 0..total; a caption stays until the next one
    starts unless the pause is long."""
    sched, t = [], 0.0
    for i, (text, a, b) in enumerate(groups):
        if a > t + 0.01:
            sched.append((None, t, a))
        nxt = groups[i + 1][1] if i + 1 < len(groups) else total
        end = nxt  # stays through the pause until the next caption arrives
        sched.append((text, a, end))
        t = end
    if t < total:
        sched.append((None, t, total))
    return sched


def render_layers(work, name_line, sched):
    paths = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": W, "height": H})
        page_file = f"{work}/page.html"  # file:// fonts only load from a file page, not set_content
        with open(page_file, "w") as f:
            f.write(PAGE % dict(h=HERE, sy=SUB_Y, ss=SUB_SIZE, ny=NAME_Y, ns=NAME_SIZE, no=NAME_OPACITY,
                                my=MARK_Y, ms=MARK_SIZE))
        pg.goto(f"file://{page_file}")
        pg.evaluate("""async()=>{document.getElementById('sub').textContent='ا';
          await Promise.all(['54px Sub','32px Name','700 24px Lato'].map(f=>document.fonts.load(f,'اA')));
          await document.fonts.ready}""")
        pg.evaluate("t=>{document.getElementById('name').textContent=t}", name_line)
        pg.evaluate("()=>{document.getElementById('sub').textContent=''}")
        pg.screenshot(path=f"{work}/static.png", omit_background=True)
        pg.evaluate("()=>{for(const id of ['name','mark'])document.getElementById(id).style.display='none';"
                    "document.querySelector('.grad').style.display='none'}")
        pg.screenshot(path=f"{work}/blank.png", omit_background=True)
        for i, (text, a, b) in enumerate(sched):
            if text is None:
                paths[i] = f"{work}/blank.png"
                continue
            pg.evaluate("t=>{document.getElementById('sub').textContent=t}", text)
            paths[i] = f"{work}/cap_{i:03d}.png"
            pg.screenshot(path=paths[i], omit_background=True)
        browser.close()
    with open(f"{work}/caps.txt", "w") as f:
        for i, (text, a, b2) in enumerate(sched):
            f.write(f"file '{paths[i]}'\nduration {b2 - a:.3f}\n")
        f.write(f"file '{paths[len(sched) - 1]}'\n")


# Both templates share the whole design (subtitles, guest line, wordmark, gradient); only the top-right badge differs.
TEMPLATES = {
    "gap": dict(file="badge_gap.webm", size=(399, 275), vis=(40, 57, 349, 214),
                t_in=3.0, hold_at=5.0, out=(9.0, 11.84), rep=(0.4, 11.84)),
    "yaran": dict(file="badge_yaran.webm", size=(373, 230), vis=(32, 25, 342, 199),
                  t_in=2.94, hold_at=4.0, out=(5.87, 8.84), rep=(0.2, 8.84)),
}


def badge_filter(L, template="gap"):
    T = TEMPLATES[template]
    vis = T["vis"]
    s = BADGE_W / (vis[2] - vis[0])
    bw, bh = round(T["size"][0] * s), round(T["size"][1] * s)
    t_in = 0.7
    fmt = f"fps={FPS},scale={bw}:{bh}:flags=lanczos,format=rgba,setsar=1,setpts=PTS-STARTPTS"
    parts, labels = [], []

    def seg(name, trim, extra=""):
        parts.append(f"[{name}_src]{trim},{fmt}{extra}[{name}]")
        labels.append(f"[{name}]")
    parts.append(f"color=c=0x00000000:s={bw}x{bh}:r={FPS}:d={t_in},format=rgba,setsar=1[pre]")
    labels.append("[pre]")
    t_out = 0.59 * L
    hold = max(0.0, t_out - t_in - T["t_in"])
    parts.append("[1:v]split=4[in_src][hold_src][out_src][rep_src]")
    seg("in", f"trim=0:{T['t_in']}")
    seg("hold", f"trim={T['hold_at']}:{T['hold_at'] + 0.1}", f",tpad=stop_mode=clone:stop_duration={hold:.3f}")
    seg("out", f"trim={T['out'][0]}:{T['out'][1]}")
    parts.append(f"color=c=0x00000000:s={bw}x{bh}:r={FPS}:d=0.12,format=rgba,setsar=1[gap]")
    labels.append("[gap]")
    seg("rep", f"trim={T['rep'][0]}:{T['rep'][1]}")
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[badge]")
    ox = BADGE_X - round(vis[0] * s)
    oy = BADGE_Y - round(vis[1] * s)
    return ";".join(parts), ox, oy


def lufs_of(path):
    r = subprocess.run(["ffmpeg", "-v", "info", "-nostats", "-i", path, "-vn", "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r)
    return float(m[-1]) if m else -23.0


FADE = 2.0  # the last seconds fade out (picture to black, sound to silence)


FADE_IN = 0.4  # the first caption eases in instead of popping onto the screen


def cap_fade(sched):
    first = next((a for t, a, b in sched if t), None)
    return "" if first is None else f",fade=t=in:st={first:.3f}:d={FADE_IN}:alpha=1"


def afade_out(L):
    return f"afade=t=out:st={max(0.0, L - FADE):.3f}:d={min(FADE, L):.3f}"


MUSIC_BELOW = 5.0  # dB under the speech loudness: heard, never competing with the voice


def music_inputs(music_file, music_lufs, video, L):
    """(extra ffmpeg inputs, filter for [aout]) mixing the track low and constant under the speech."""
    gain = lufs_of(video) - MUSIC_BELOW - music_lufs
    fo = max(0.0, L - 2.0)
    f = (f"[4:a]aloop=loop=-1:size=2e9,atrim=0:{L:.3f},volume={gain:.1f}dB,"
         f"afade=t=in:d=0.5,afade=t=out:st={fo:.2f}:d=2[mus];"
         f"[0:a][mus]amix=inputs=2:duration=first:normalize=0,{afade_out(L)}[aout]")
    return ["-i", music_file], f


def main(video, text, guest, title, out, work="/tmp/gap_work", rows=None, music_file=None, music_lufs=None,
         template="gap", progress=None):
    os.makedirs(work, exist_ok=True)
    L = probe_len(video)
    x = wt.load_audio(video, f"{work}/a.wav")
    chunks = wt.speech_chunks(x)
    if rows:  # real word times (Gemini), fixed against the audio
        words = text.split()
        t0 = wt.rows_to_words(words, rows)
        t1, _ = wt.lock_to_pauses(t0, chunks)
        times = wt.from_gemini([{"start": a, "end": b} for a, b in t1], chunks)
    else:
        words = text.split()
        times = wt.align_acoustic(words, chunks, x)
    sched = caption_schedule(group_words(words, times), L)
    json.dump(sched, open(f"{work}/schedule.json", "w"), ensure_ascii=False, indent=1)
    render_layers(work, f"{guest} / {title}", sched)
    bf, ox, oy = badge_filter(L, template)
    fc = (bf + f";[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1,"
          f"fps={FPS},format=yuv420p[bg];[bg][2:v]overlay=0:0:eof_action=pass[a];"
          f"[3:v]fps={FPS},format=rgba{cap_fade(sched)}[caps];[a][caps]overlay=0:0:eof_action=pass[b];"
          f"[b][badge]overlay={ox}:{oy}:eof_action=pass,format=yuv420p,"
          f"fade=t=out:st={max(0.0, L - FADE):.3f}:d={min(FADE, L):.3f}[v]")
    extra, amap = [], ["-map", "0:a?", "-af", afade_out(L)]
    if music_file:
        extra, af = music_inputs(music_file, music_lufs if music_lufs is not None else lufs_of(music_file),
                                 video, L)
        fc += ";" + af
        amap = ["-map", "[aout]"]
    run_ffmpeg(L, progress, "ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", "-i", video, "-c:v", "libvpx-vp9", "-i", os.path.join(HERE, TEMPLATES[template]["file"]),
        "-loop", "1", "-i", f"{work}/static.png", "-f", "concat", "-safe", "0", "-i", f"{work}/caps.txt", *extra,
        "-filter_complex", fc, "-map", "[v]", *amap, "-t", f"{L:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out)
    print("ok", out, f"{L:.1f}s")
