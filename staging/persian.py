import re

_FA = "؀-ۿ"
_DIACRITICS = re.compile("[\u064c-\u065f\u0670\u0640]")  # harakat (not tanwin fatha), dagger alef, tatweel
_PREFIX = re.compile(rf"(?<![{_FA}])(ن?می) (?=[{_FA}])")
_SUFFIX = re.compile(rf"(?<=[{_FA}]{{2}}) (ها|های|هایی|ترین|تری)(?![{_FA}])")
_SPACE_BEFORE = re.compile(r"\s+([،؛؟!.:»)…])")
_SPACE_AFTER = re.compile(r"([،؛؟!.:])(?=[^\s\d\"»)،؛؟!.:])")
_LETTERS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "ە": "ه", "ى": "ی"})
ZWNJ = "‌"


def normalize_fa(text: str) -> str:
    """Typographic clean-up only: letters, half-spaces and punctuation. Never changes words."""
    t = text.translate(_LETTERS)
    t = _DIACRITICS.sub("", t)
    t = _PREFIX.sub("\\1" + ZWNJ, t)
    t = _SUFFIX.sub(ZWNJ + "\\1", t)
    if re.search(f"[{_FA}]", t):
        t = re.sub(r"\s*\?", "؟", t)
        t = re.sub(r"(?<!\d)\s*,(?!\d)", "،", t)
        t = re.sub(r"\s*;", "؛", t)
    t = re.sub(r"\.{3,}", "…", t)
    t = _SPACE_BEFORE.sub(r"\1", t)
    t = _SPACE_AFTER.sub(r"\1 ", t)
    t = re.sub(ZWNJ + "{2,}", ZWNJ, t)
    t = re.sub(r"\s*" + ZWNJ + r"\s*", ZWNJ, t)
    return re.sub(r"\s+", " ", t).strip()
