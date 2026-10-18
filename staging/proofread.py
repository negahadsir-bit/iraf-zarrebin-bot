import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path

from persian import normalize_fa
from refine import _cut
from retry import QuotaError

PAD = 0.3
MIN_SIMILAR = 0.5   # below this the model rewrote the speech instead of fixing spelling
LENGTH_RANGE = (0.75, 1.3)


def accept(draft: str, new: str) -> bool:
    """A correction must stay the same speech: close in wording and in length."""
    a, b = draft.split(), new.split()
    if not a or not b:
        return False
    if not LENGTH_RANGE[0] <= len(b) / len(a) <= LENGTH_RANGE[1]:
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= MIN_SIMILAR


def proofread_one(clip, draft: str, audio: Path, total: float, client, workdir: Path):
    """Listen to exactly this clip once more and fix mis-heard or misspelled words."""
    dest = workdir / f"proof_{clip.rank}.mp3"
    w0 = max(0.0, clip.start - PAD)
    w1 = min(total, clip.end + PAD)
    heard = client.correct_text(_cut(audio, w0, w1 - w0, dest), draft)
    heard = normalize_fa(heard or "")
    if heard and accept(draft, heard):
        return heard, True
    return draft, False


def proofread_clips(clips, sentences, audio: Path, total: float, client, workdir: Path,
                    workers: int = 3):
    """Every clip's shown text: typography always cleaned; words re-checked against the audio."""
    from formatting import _clip_words

    def work(c):
        draft = normalize_fa(c.text or " ".join(_clip_words(c, sentences)))
        if not draft:
            return c
        fixed, ok = draft, False
        if client is not None and hasattr(client, "correct_text"):
            try:
                fixed, ok = proofread_one(c, draft, audio, total, client, workdir)
            except QuotaError:
                pass
            except Exception as e:  # noqa: BLE001 - a text pass never loses a clip
                print(f"proofread clip {c.rank} skipped: {type(e).__name__}",
                      file=sys.stderr, flush=True)
        print(f"proofread clip {c.rank}: {'audio-checked' if ok else 'typography only'}, "
              f"{sum(1 for x, y in zip(draft.split(), fixed.split()) if x != y)} words differ",
              file=sys.stderr, flush=True)
        return replace(c, text=fixed)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return [f.result() for f in [pool.submit(work, c) for c in clips]]
