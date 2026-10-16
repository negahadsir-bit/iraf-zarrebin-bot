"""Rough word timing from the audio itself: known text + pauses in the speech.
Placeholder until real word timestamps (Gemini on the cut clip) are available."""
import re
import subprocess
import numpy as np

SR = 16000


def load_audio(video: str, wav: str) -> np.ndarray:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", str(SR), wav],
                   check=True)
    import scipy.io.wavfile as w
    _, x = w.read(wav)
    return x.astype(float) / 32768


def speech_chunks(x: np.ndarray, min_pause: float = 0.22, hop_s: float = 0.02):
    """[(start, end)] of speech between pauses, with a threshold relative to the noise floor."""
    hop = int(hop_s * SR)
    n = len(x) // hop
    e = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1))
    db = 20 * np.log10(e + 1e-9)
    thr = np.percentile(db, 10) + 3.5
    quiet = db < thr
    pauses, s = [], None
    for i, q in enumerate(quiet):
        if q and s is None:
            s = i
        if not q and s is not None:
            if (i - s) * hop_s >= min_pause:
                pauses.append((s * hop_s, i * hop_s))
            s = None
    if s is not None and (n - s) * hop_s >= min_pause:
        pauses.append((s * hop_s, n * hop_s))
    dur = n * hop_s
    chunks, t = [], 0.0
    for a, b in pauses:
        if a - t > 0.15:
            chunks.append((t, a))
        t = b
    if dur - t > 0.15:
        chunks.append((t, dur))
    return chunks


def refine_chunks(x: np.ndarray, chunks, frac: float = 0.3, hold: float = 0.06, hop_s: float = 0.01):
    """Speech chunks with their start moved to the first moment the band energy stays above `frac` of the
    chunk's own level for `hold` seconds. The pause detector also keeps breaths and room noise next to
    the speech, so a chunk can start half a second before the first word is heard, and captions locked
    to that start would come up ahead of the voice."""
    _, env = syllable_peaks(x, hop_s)
    k = max(1, int(round(hold / hop_s)))
    out = []
    for a, b in chunks:
        i0, i1 = int(a / hop_s), min(len(env), int(b / hop_s) + 1)
        seg = env[i0:i1]
        if len(seg) < 3 * k:
            out.append((a, b))
            continue
        above = (seg > frac * np.percentile(seg, 90)).astype(int)
        run = np.convolve(above, np.ones(k, dtype=int), mode="valid") == k  # k frames in a row
        idx = np.flatnonzero(run)
        if len(idx) == 0:
            out.append((a, b))
            continue
        s = (i0 + idx[0]) * hop_s
        out.append((max(a, s), b) if b - s >= 0.1 else (a, b))  # only the start: a caption stays until the next one
    return out


def _weight(word: str) -> float:
    return 1.5 + len(re.sub(r"[^\w]", "", word))


def align(words: list[str], chunks: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Spread words over the speech chunks. Word counts per chunk follow the chunk lengths,
    and every chunk boundary is snapped to the nearest word boundary."""
    w = np.array([_weight(x) for x in words])
    cum = np.concatenate([[0], np.cumsum(w)]) / w.sum()
    lens = np.array([b - a for a, b in chunks])
    edges = np.concatenate([[0], np.cumsum(lens)]) / lens.sum()
    bounds = [0]
    for j in range(1, len(chunks)):
        k = int(np.argmin(np.abs(cum - edges[j])))
        punct = [m for m in range(max(1, k - 2), min(len(words), k + 2) + 1)
                 if re.search(r"[،,.؟?!؛:]$", words[m - 1])]
        if punct:  # a pause is far likelier where the text has a comma or full stop
            k = min(punct, key=lambda m: abs(m - k))
        bounds.append(min(max(k, bounds[-1]), len(words)))
    bounds.append(len(words))
    out = [None] * len(words)
    for j, (a, b) in enumerate(chunks):
        lo, hi = bounds[j], bounds[j + 1]
        if hi <= lo:
            continue
        ww = w[lo:hi]
        c = np.concatenate([[0], np.cumsum(ww)]) / ww.sum()
        for m in range(hi - lo):
            out[lo + m] = (a + (b - a) * c[m], a + (b - a) * c[m + 1])
    return out


def from_gemini(rows: list[dict], chunks: list[tuple[float, float]], min_len: float = 0.06):
    """Word times from Gemini, corrected with the audio: no word may start or end inside a
    detected pause, and times stay in order."""
    pauses = [(chunks[i][1], chunks[i + 1][0]) for i in range(len(chunks) - 1)]
    out, prev_end = [], 0.0
    for r in rows:
        a, b = float(r["start"]), float(r["end"])
        for p0, p1 in pauses:
            if p0 < a < p1:       # starts inside a pause -> starts when speech resumes
                a = p1
            if p0 < b < p1:       # ends inside a pause -> ends when the pause begins
                b = p0
        a = max(a, prev_end)
        b = max(b, a + min_len)
        out.append((a, b))
        prev_end = a  # words may touch but never go backwards
    return out


def syllable_peaks(x: np.ndarray, hop_s: float = 0.01):
    """(times of syllable nuclei, envelope) from the 250-2800 Hz band energy."""
    from scipy.signal import butter, sosfiltfilt, find_peaks
    sos = butter(4, [250, 2800], btype="band", fs=SR, output="sos")
    y = np.abs(sosfiltfilt(sos, x))
    hop = int(hop_s * SR)
    n = len(y) // hop
    env = y[:n * hop].reshape(n, hop).mean(1)
    k = int(0.05 / hop_s)  # ~50 ms smoothing
    env = np.convolve(env, np.ones(k) / k, mode="same")
    env = env ** 0.6
    pk, _ = find_peaks(env, distance=int(0.09 / hop_s), prominence=0.06 * env.max())
    return pk * hop_s, env


def align_acoustic(words: list[str], chunks, x: np.ndarray):
    """Like align(), but inside each speech chunk the word boundaries sit on the energy
    valleys between syllable peaks, at the peak count that matches the words' share of
    the chunk. Speaking-rate changes inside a chunk therefore do not shift the words."""
    base = align(words, chunks)               # chunk membership + fallback times
    peaks, env = syllable_peaks(x)
    hop_s = 0.01
    w = np.array([_weight(t) for t in words])
    # which words belong to which chunk (from the first alignment)
    out = list(base)
    for a, b in chunks:
        idx = [i for i, t in enumerate(base) if t is not None and a - 1e-6 <= t[0] and t[1] <= b + 1e-6]
        if len(idx) < 2:
            continue
        pk = [p for p in peaks if a - 0.02 <= p <= b + 0.02]
        if len(pk) < 0.8 * len(idx):          # too few peaks to trust: keep proportional timing
            continue
        ww = w[idx]
        cum = np.cumsum(ww) / ww.sum()
        bounds = [a]
        for f in cum[:-1]:
            m = int(round(f * len(pk)))
            m = min(max(m, 1), len(pk) - 1)
            lo, hi = int(pk[m - 1] / hop_s), int(pk[m] / hop_s)
            v = lo + int(np.argmin(env[lo:hi + 1]))
            bounds.append(max(v * hop_s, bounds[-1] + 0.05))
        bounds.append(b)
        dur = np.diff(bounds)
        prop = ww / ww.sum() * (b - a)
        for _ in range(8):                    # keep every word's length plausible for its size
            dur = np.clip(dur, 0.55 * prop, 1.8 * prop)
            dur = dur * (b - a) / dur.sum()
        edges = a + np.concatenate([[0], np.cumsum(dur)])
        for j, i in enumerate(idx):
            out[i] = (float(edges[j]), float(edges[j + 1]))
    return out


# --- Gemini word times, locked to the pauses that are really in the audio -------------

_ZW = str.maketrans({"\u200c": "", "\u064a": "\u06cc", "\u0643": "\u06a9"})


def _norm(w: str) -> str:
    return re.sub(r"[^\w]", "", w.translate(_ZW))


def rows_to_words(words: list[str], rows: list[dict]):
    """Per original word, (start, end) from Gemini's rows. Gemini may merge, split or
    misspell a word, so rows are matched to the real words by text; unmatched words share
    the time span of the rows they replace."""
    from difflib import SequenceMatcher
    a = [_norm(w) for w in words]
    b = [_norm(r["text"]) for r in rows]
    out = [None] * len(words)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out[i1 + k] = (float(rows[j1 + k]["start"]), float(rows[j1 + k]["end"]))
        elif i2 > i1 and j2 > j1:
            s, e = float(rows[j1]["start"]), float(rows[j2 - 1]["end"])
            w = np.array([_weight(t) for t in words[i1:i2]])
            c = np.concatenate([[0], np.cumsum(w)]) / w.sum()
            for k in range(i2 - i1):
                out[i1 + k] = (s + (e - s) * c[k], s + (e - s) * c[k + 1])
    for i, t in enumerate(out):  # words Gemini dropped: squeeze between neighbours
        if t is None:
            j = i
            while j < len(out) and out[j] is None:
                j += 1
            lo = out[i - 1][1] if i else 0.0
            hi = out[j][0] if j < len(out) else lo + 0.3 * (j - i)
            n = j - i
            for k in range(n):
                out[i + k] = (lo + (hi - lo) * k / n, lo + (hi - lo) * (k + 1) / n)
    return out


def lock_to_pauses(times, chunks, min_gap: float = 0.3, reach: float = 1.6):
    """Gemini's silences between words are matched, in order, with the real pauses of the
    audio; time between matched pauses is stretched linearly. Fixes both the offset and the
    drift of Gemini's clock. Returns (new times, anchors used)."""
    pauses = [(chunks[i][1], chunks[i + 1][0]) for i in range(len(chunks) - 1)]
    gaps = [(times[i - 1][1], times[i][0]) for i in range(1, len(times)) if times[i][0] - times[i - 1][1] >= min_gap]
    anchors, gi = [], 0
    for p0, p1 in pauses:
        best, best_d = None, reach
        for j in range(gi, len(gaps)):
            g0, g1 = gaps[j]
            if g0 > p1 + reach:
                break
            d = abs((g0 + g1) / 2 - (p0 + p1) / 2)
            if d < best_d:
                best, best_d = j, d
        if best is not None:
            g0, g1 = gaps[best]
            anchors += [(g0, p0), (max(g1, g0 + 1e-3), max(p1, p0 + 1e-3))]
            gi = best + 1
    last_end = max(t[1] for t in times)
    anchors.append((last_end, chunks[-1][1]))      # the last word ends where speech ends
    xs, ys = [], []
    for g, p in sorted(anchors):
        if (not xs) or (g > xs[-1] and p > ys[-1]):  # keep both axes increasing
            xs.append(g)
            ys.append(p)

    def f(t):
        if not xs:
            return t
        if t <= xs[0]:
            return t + (ys[0] - xs[0])
        if t >= xs[-1]:
            return t + (ys[-1] - xs[-1])
        return float(np.interp(t, xs, ys))
    return [(f(a), f(b)) for a, b in times], list(zip(xs, ys))
