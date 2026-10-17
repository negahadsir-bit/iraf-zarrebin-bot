CATS = ("احساسی عرفانی", "اجتماعی", "سرگرمی و فان", "سیاسی", "گپ")
_ABOUT = (
    "دلتنگی، درد، غرور، معنویت، عرفان و لحظه‌های تکان‌دهنده",
    "مسائل و دغدغه‌های مردم و جامعه، خانواده، فرهنگ و زندگی روزمره",
    "طنز، خاطره‌ی بامزه، لحظه‌ی شاد و سرگرم‌کننده",
    "موضع‌گیری و تحلیل سیاسی، تاریخ و مسائل کشور و منطقه",
    "برنامه‌ی «گپ»: دغدغه‌ها و مشکلات مهاجران موفق افغانستانی در ایران؛ خاطره‌های بامزه یا غمگین",
)


def focus_note(selected) -> str:
    """Prompt text telling the selector which kind of clips the user wants (empty = no focus)."""
    picked = sorted({i for i in selected if 0 <= i < len(CATS)})
    if not picked:
        return ""
    lines = "\n".join(f"- «{CATS[i]}»: {_ABOUT[i]}" for i in picked)
    return ("کاربر برای این مصاحبه فقط این دسته‌ها را خواسته است:\n" + lines +
            "\nفقط برش‌هایی بده که از نظر موضوع و حال‌وهوا دقیقاً در همین دسته‌ها می‌گنجند. "
            "برشی که بیرون از این دسته‌هاست، حتی اگر به‌تنهایی خوب باشد، نده. "
            "اگر کمتر از تعداد خواسته‌شده برش مناسب پیدا شد همان‌ها را بده و برای پر کردن تعداد، "
            "برش بی‌ربط اضافه نکن؛ اگر هیچ برش مناسبی نبود فهرست خالی برگردان. "
            "بقیه‌ی قوانین (شروع، پایان، مدت) سر جایش می‌ماند.")


_HEAD = "## دسته:"


def rules_for(text: str, selected) -> str:
    """General rules plus the sections of the chosen categories (all sections if none chosen)."""
    parts = text.split("\n" + _HEAD)
    general, sections = parts[0], parts[1:]
    picked = {CATS[i] for i in selected if 0 <= i < len(CATS)}
    keep = []
    for sec in sections:
        title = sec.split("\n", 1)[0].strip()
        if not picked or title in picked:
            keep.append(_HEAD + sec)
    return "\n".join([general.rstrip()] + [k.rstrip() for k in keep]) + "\n"
