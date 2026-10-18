"""Second check for clips whose position is doubtful: re-listen and move them to where their words are
heard. A clip that cannot be located is kept (flagged); the final editor re-listens to its audio anyway."""
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from align import locate
from models import Clip
from refine import _cut
from retry import QuotaError

PADS = (40.0, 150.0, 400.0)  # look near the claimed place first, then ever wider
END_ZONE = 90.0              # a clip ending this close to the end of the file is suspect
MIN_LEN, MAX_LEN = 18.0, 76.0
LEAD, TAIL = 0.2, 0.3


def is_risky(clip: Clip, total: float) -> bool:
    return clip.shaky or clip.end >= total - END_ZONE


def verify_one(clip: Clip, audio: Path, total: float, client, workdir: Path):
    """The clip with confirmed times (shaky cleared), or None when its words are not
    heard anywhere near where it was supposed to be (speech recognition on dialect varies
    from listen to listen, so that alone never proves the clip wrong)."""
    for pad in PADS:
        w0, w1 = max(0.0, clip.start - pad), min(total, clip.end + pad)
        dest = workdir / f"ver_{clip.rank}_{int(pad)}.mp3"
        window = client.transcribe_sentences(_cut(audio, w0, w1 - w0, dest))
        found = locate(clip.text, window, clip.start - w0, min_ratio=0.7) if window else None
        if found and MIN_LEN <= found[1] - found[0] <= MAX_LEN:
            start, end = max(0.0, w0 + found[0] - LEAD), min(total, w0 + found[1] + TAIL)
            print(f"verify clip {clip.rank}: confirmed {clip.start:.1f}-{clip.end:.1f} -> "
                  f"{start:.1f}-{end:.1f} (pad {pad:.0f}s)", file=sys.stderr, flush=True)
            return replace(clip, start=start, end=end, shaky=False)
    print(f"verify clip {clip.rank}: not confirmed, kept as is", file=sys.stderr, flush=True)
    return None


def verify_clips(clips: list[Clip], audio: Path, total: float, client, workdir: Path,
                 workers: int = 3) -> list[Clip]:
    risky = [c for c in clips if is_risky(c, total) and len(c.text.split()) >= 8]
    if not risky:
        return clips

    def work(c: Clip):
        try:
            return verify_one(c, audio, total, client, workdir)
        except QuotaError:
            return c  # cannot check right now: keep it, still flagged
        except Exception as e:  # noqa: BLE001
            print(f"verify clip {c.rank} skipped: {type(e).__name__}", file=sys.stderr, flush=True)
            return c

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = dict(zip((c.rank for c in risky), [f.result() for f in
                                                    [pool.submit(work, c) for c in risky]]))
    return [results.get(c.rank) or c for c in clips]
