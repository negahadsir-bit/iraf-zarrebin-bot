#!/usr/bin/env python3
"""IRAF TV «ذره‌بین» headline bot (runs from GitHub Actions every few minutes).
  python bot.py check   -> read Telegram updates, answer simple messages, queue render jobs in state.json
                           prints has_jobs=true|false to $GITHUB_OUTPUT
  python bot.py render  -> render every queued job and send the finished video back
Flow for the user: send a video, then send the headline as a text message.
Env: BOT_TOKEN (required), ALLOWED_CHAT_IDS (comma separated; empty = only tells you your chat id)."""
import urllib.error, json, os, sys, time, subprocess, tempfile, shutil, urllib.request, urllib.parse, uuid, re

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, 'state.json')
TOKEN = os.environ.get('BOT_TOKEN', '')
ALLOWED = {x.strip() for x in os.environ.get('ALLOWED_CHAT_IDS', '').split(',') if x.strip()}
# With TELEGRAM_API_URL set (a local Bot API server started by the workflow) files may be up to 2 GB instead of 20 MB.
BASE = os.environ.get('TELEGRAM_API_URL', '').rstrip('/')
DATA_DIR = os.environ.get('TELEGRAM_DATA', '')
API = f"{BASE or 'https://api.telegram.org'}/bot{TOKEN}"
FILE_API = f'https://api.telegram.org/file/bot{TOKEN}'
MAX_HEADLINE = 72                # characters; longer headlines would not fit in two lines
MAX_DL = (2000 if BASE else 20) * 1024 * 1024   # Telegram bot download limit (cloud 20 MB, local server 2 GB)
MAX_UP = (2000 if BASE else 50) * 1024 * 1024   # Telegram bot upload limit (cloud 50 MB, local server 2 GB)

def call(method, data=None, files=None, timeout=60):
    if files:
        b = uuid.uuid4().hex
        body = b''
        for k, v in (data or {}).items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (fn, content, ct) in files.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + content + b'\r\n'
        body += f'--{b}--\r\n'.encode()
        req = urllib.request.Request(f'{API}/{method}', body, {'Content-Type': f'multipart/form-data; boundary={b}'})
    else:
        req = urllib.request.Request(f'{API}/{method}', urllib.parse.urlencode(data or {}).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def ask_layout(chat):
    kb = {'keyboard': [[{'text': 'عمودی'}, {'text': 'افقی'}]], 'resize_keyboard': True, 'one_time_keyboard': True}
    try: call('sendMessage', {'chat_id': chat, 'text': '🎬 ویدیو را گرفتم.\nقالب عمودی باشد یا افقی؟', 'reply_markup': json.dumps(kb)})
    except Exception as e: print('sendMessage failed', e)

BAR_LEN = 10
_FA = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')

def bar(pct):
    """«▰▰▰▱▱▱▱▱▱▱ ۳۰٪» progress line."""
    pct = max(0, min(100, int(pct)))
    full = round(BAR_LEN * pct / 100)
    return '▰' * full + '▱' * (BAR_LEN - full) + ' ' + f'{pct}٪'.translate(_FA)

class Progress:
    """One message that fills up while the video is made (edits are throttled; a failed edit never matters)."""
    def __init__(self, chat, title='ساخت ویدیو'):
        self.chat, self.title, self.mid, self.last, self.shown = chat, title, None, 0.0, ''
        self.set(0, 'در حال شروع', force=True)

    def set(self, pct, label, force=False):
        text = f'⏳ {self.title}\n{bar(pct)}\n{label}'
        now = time.monotonic()
        if text == self.shown or (not force and self.mid is not None and now - self.last < 2.5): return
        try:
            if self.mid is None:
                self.mid = call('sendMessage', {'chat_id': self.chat, 'text': text})['result']['message_id']
            else:
                call('editMessageText', {'chat_id': self.chat, 'message_id': self.mid, 'text': text})
            self.shown, self.last = text, now
        except Exception as e: print('progress failed', e)

    def done(self):
        if self.mid is None: return
        try: call('editMessageText', {'chat_id': self.chat, 'message_id': self.mid, 'text': f'✅ {self.title}\n{bar(100)}\nآماده شد'})
        except Exception as e: print('progress failed', e)

def say(chat, text):
    try: call('sendMessage', {'chat_id': chat, 'text': text})
    except Exception as e: print('sendMessage failed', e)

def load():
    try: return json.load(open(STATE, encoding='utf-8'))
    except Exception: return {'offset': 0, 'pending': {}, 'jobs': []}
def save(s): json.dump(s, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

def balance(text):
    """Headline in two balanced lines unless the user already broke lines (one line is at most ~36 chars at the standard size)."""
    text = re.sub(r'\s*\n\s*', ' ', text.strip()) if text.count('\n') > 1 else text.strip()
    if '\n' in text or len(text) <= 30: return text
    words = text.split(' ')
    best, bi = None, 1
    for i in range(1, len(words)):
        left, right = ' '.join(words[:i]), ' '.join(words[i:])
        widest = max(len(left), len(right))
        score = widest - (3 if left[-1] in '؛:،' and widest <= 36 else 0)
        if best is None or score < best: best, bi = score, i
    return ' '.join(words[:bi]) + '\n' + ' '.join(words[bi:])

def output(k, v):
    p = os.environ.get('GITHUB_OUTPUT')
    if p: open(p, 'a').write(f'{k}={v}\n')

TWEET_RE = re.compile(r'https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/\s]+/status/(\d+)\S*')

def review_keyboard(rid):
    return json.dumps({'inline_keyboard': [[{'text': '✅ تأیید', 'callback_data': f'ok:{rid}'}, {'text': '✏️ اصلاح تیتر', 'callback_data': f'ed:{rid}'}],
                                           [{'text': '🔎 زوم ۱۵٪', 'callback_data': f'zm:{rid}'}, {'text': '🧽 حذف لوگو', 'callback_data': f'dl:{rid}'}],
                                           [{'text': '❌ رد', 'callback_data': f'no:{rid}'}]]})

def on_button(s, cq):
    chat = str(cq['message']['chat']['id']); mid = cq['message']['message_id']
    try: call('answerCallbackQuery', {'callback_query_id': cq['id']})
    except Exception as e: print('answer failed', e)
    if chat not in ALLOWED: return
    act, _, rid = (cq.get('data') or '').partition(':')
    rv = s.setdefault('reviews', {}).get(rid)
    def mark(txt):
        try: call('editMessageReplyMarkup', {'chat_id': chat, 'message_id': mid, 'reply_markup': json.dumps({'inline_keyboard': [[{'text': txt, 'callback_data': 'x:0'}]]})})
        except Exception as e: print('edit markup failed', e)
    if act == 'ok':
        mark('✅ تأیید شد'); s['reviews'].pop(rid, None)
    elif act == 'no':
        mark('❌ رد شد'); s['reviews'].pop(rid, None)
    elif act in ('zm', 'dl'):
        if not rv: say(chat, 'این مورد دیگر در دسترس نیست.'); return
        fix = 'zoom' if act == 'zm' else 'delogo'
        if rv.get('fix') == fix: say(chat, 'این تغییر همین الان روی ویدیو اعمال شده است.'); return
        s['jobs'].append({'chat': chat, 'url': rv['url'], 'video_url': rv.get('video_url', ''), 'headline': rv['headline'], 'caption': rv.get('caption', ''), 'layout': rv.get('layout', ''), 'fix': fix, 'id': rid, 'review': True})
        say(chat, '🔎 ویدیو را با زوم دوباره می‌سازم.' if fix == 'zoom' else '🧽 لوگوها را پیدا و پاک می‌کنم و دوباره می‌سازم.')
    elif act == 'ed':
        if not rv: say(chat, 'این مورد دیگر در دسترس نیست.'); return
        s['pending'][chat] = {'step': 'edit', 'review': rid, 'at': time.time()}
        say(chat, f'✏️ تیتر جدید را بنویس (حداکثر {MAX_HEADLINE} حرف).')

def process(s, timeout=0):
    r = call('getUpdates', {'offset': s['offset'], 'timeout': timeout, 'allowed_updates': json.dumps(['message', 'callback_query'])}, timeout=timeout + 30)
    for u in r.get('result', []):
        s['offset'] = u['update_id'] + 1
        if u.get('callback_query'):
            on_button(s, u['callback_query']); continue
        m = u.get('message')
        if not m: continue
        chat = str(m['chat']['id'])
        if chat not in ALLOWED:
            say(chat, f'این ربات خصوصی است.\nشناسهٔ چت شما: {chat}')
            continue
        vid = m.get('video') or (m.get('document') if str(m.get('document', {}).get('mime_type', '')).startswith('video/') else None)
        text = (m.get('text') or '').strip()
        if vid:
            if vid.get('file_size', 0) > MAX_DL:
                say(chat, f'حجم ویدیو بیشتر از {MAX_DL // 1048576} مگابایت است و تلگرام اجازهٔ دریافتش را به ربات نمی‌دهد. نسخهٔ کم‌حجم‌تر بفرست.')
                continue
            s['pending'][chat] = {'file_id': vid['file_id'], 'at': time.time(), 'step': 'layout'}
            ask_layout(chat)
        elif TWEET_RE.search(text) and not text.startswith('/'):
            s['pending'].pop(chat, None)
            s['jobs'].append({'chat': chat, 'url': TWEET_RE.search(text).group(0), 'layout': '', 'headline': '', 'id': uuid.uuid4().hex[:8]})
            say(chat, '🔗 لینک را گرفتم؛ ویدیو و متن توییت را می‌گیرم و تیتر پیشنهادی را آماده می‌کنم.')
        elif text.startswith('/'):
            if text.startswith('/start') or text.startswith('/help'):
                say(chat, 'سلام 👋\nلینک یک توییت ویدیویی (x.com) بفرست تا خودم ویدیو را بگیرم، تیتر را ترجمه و کوتاه کنم و برای تأیید برایت بفرستم.\nیا اول ویدیو را بفرست، بعد تیتر را در یک پیام متنی بنویس. برای شکستن خط، تیتر را در چند خط بنویس.\nساخت چند دقیقه طول می‌کشد و پیشرفتش را همین‌جا می‌بینی.')
            elif text.startswith('/cancel'):
                s['pending'].pop(chat, None); say(chat, 'لغو شد. هر وقت خواستی ویدیوی تازه‌ای بفرست.')
        elif text and s['pending'].get(chat, {}).get('step') == 'edit':
            rv = s.get('reviews', {}).get(s['pending'][chat].get('review'))
            if not rv:
                s['pending'].pop(chat, None); say(chat, 'این مورد دیگر در دسترس نیست.')
            elif len(text) > MAX_HEADLINE:
                say(chat, f'تیتر بلند است ({len(text)} حرف). حداکثر {MAX_HEADLINE} حرف؛ کوتاه‌ترش کن و دوباره بفرست.')
            else:
                rid = s['pending'].pop(chat)['review']
                s['jobs'].append({'chat': chat, 'url': rv['url'], 'video_url': rv.get('video_url', ''), 'headline': text, 'caption': rv.get('caption', ''), 'layout': rv.get('layout', ''), 'fix': rv.get('fix', ''), 'id': rid, 'review': True})
                say(chat, '✅ تیتر جدید ثبت شد؛ ساخت دوباره شروع می‌شود.')
        elif text and s['pending'].get(chat, {}).get('step') == 'layout':
            if text in ('عمودی', 'افقی'):
                s['pending'][chat]['layout'] = 'land' if text == 'افقی' else 'portrait'
                s['pending'][chat]['step'] = 'headline'
                try: call('sendMessage', {'chat_id': chat, 'text': '✍️ حالا تیتر را بنویس.', 'reply_markup': json.dumps({'remove_keyboard': True})})
                except Exception as e: print('sendMessage failed', e)
            else:
                ask_layout(chat)
        elif text and len(text) > MAX_HEADLINE:
            say(chat, f'تیتر بلند است ({len(text)} حرف). حداکثر حدود {MAX_HEADLINE} حرف (هر خط ۳۶ حرف) تا فونت ریز نشود؛ کوتاه‌ترش کن و دوباره بفرست. ویدیو نگه داشته شد.')
        elif text:
            p = s['pending'].pop(chat, None)
            if not p:
                say(chat, 'اول ویدیو را بفرست، بعد تیتر را بنویس.')
            else:
                s['jobs'].append({'chat': chat, 'file_id': p['file_id'], 'headline': text, 'layout': p.get('layout', ''), 'id': uuid.uuid4().hex[:8]})
                say(chat, '✅ تیتر ثبت شد؛ ساخت ویدیو همین الان شروع می‌شود.')
    save(s)

def check():
    s = load()
    process(s)
    output('has_jobs', 'true' if s['jobs'] else 'false')
    print('offset', s['offset'], 'jobs', len(s['jobs']))

def download(file_id, dest):
    info = call('getFile', {'file_id': file_id})['result']
    if info.get('file_size', 0) > MAX_DL: raise RuntimeError('file too large')
    fp = info['file_path']
    if BASE and os.path.isabs(fp):  # local server hands out absolute paths inside its data directory
        local = os.path.join(DATA_DIR, os.path.relpath(fp, '/var/lib/telegram-bot-api')) if DATA_DIR else fp
        try:
            shutil.copyfile(local, dest)
        except OSError:  # owned by the server's user inside the container: copy it out through docker
            subprocess.check_call(['docker', 'cp', f'tgapi:{fp}', dest])
        return
    with urllib.request.urlopen(f"{FILE_API}/{fp}", timeout=600) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)

def headline_seconds(length):
    """How long the headline stays: ~20% of the video, never under 6.5 s, at most 15 s, and gone before the last 1.2 s."""
    return round(max(1.0, min(max(6.5, 0.2 * length), 15.0, length - 1.2)), 2)

def video_length(path):
    out = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path])
    return float(out.strip())

def is_landscape(path):
    out = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height:stream_side_data=rotation', '-of', 'json', path]))['streams'][0]
    w, h = out['width'], out['height']
    if abs(int((out.get('side_data_list') or [{}])[0].get('rotation', 0))) % 180 == 90: w, h = h, w
    return w > h * 1.05

DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')

STYLE = """تو سردبیر یک شبکهٔ خبری فارسی‌زبان هستی. متن خبر (هر زبانی) را بگیر و دو چیز بنویس: «تیتر» و «کپشن» فارسی.

قواعد تیتر:
- جملهٔ اسمی و خبری، به شکل «موضوع اصلی؛ جزئیات یا پیامد» (با نقطه‌ویرگول فارسی ؛) یا یک جملهٔ کوتاه، بدون نقطهٔ پایانی.
- حدود ۵۵ تا ۶۸ نویسه، هرگز بیشتر از ۷۰ نویسه.
- ساختار مورد پسند: «اتهام نظامیان پاکستان به سرقت طلا از خانه‌ها در خیبر»، «پانزدهمین روز اعتراضات در پاکستان؛ افزایش انتقادها از عاصم منیر»، «آتش‌سوزی گسترده در کالیفرنیا؛ تخلیهٔ هزاران نفر و خسارت به خانه‌ها».
- بدون هشتگ، ایموجی، نقل‌قول، علامت تعجب و عبارت‌هایی مثل «فوری». نام‌ها و جاها را به شکل رایج فارسی بنویس.

قواعد کپشن:
- فارسی روان، رسمی و بی‌طرف؛ ترجمهٔ وفادار و خلاصه‌شدهٔ خود خبر، بدون اضافه‌کردن اطلاعات یا نظر.
- اگر خبر چند واقعهٔ جدا دارد، سه خط کوتاه و هر خط با «🔷 » شروع شود. وگرنه یک پاراگراف دو تا چهار جمله‌ای.
- بدون هشتگ، منبع و تبلیغ؛ حداکثر ۶۰۰ نویسه.

خروجی فقط یک JSON با کلیدهای headline و caption."""

def news_from(text):
    """Caption of a news post in any language -> (Persian headline <= MAX_HEADLINE chars, Persian caption), via DeepSeek in normal (non-reasoning) mode.
    Returns ('', '') when there is no key or the call fails, so callers can fall back."""
    if not DEEPSEEK_KEY or not text.strip(): return '', ''
    msgs = [{'role': 'system', 'content': STYLE}, {'role': 'user', 'content': text.strip()[:3000]}]
    for attempt in range(3):
        try:
            body = json.dumps({'model': 'deepseek-chat', 'temperature': 0.3, 'max_tokens': 700, 'response_format': {'type': 'json_object'}, 'messages': msgs}).encode()
            req = urllib.request.Request('https://api.deepseek.com/chat/completions', body, {'Content-Type': 'application/json', 'Authorization': f'Bearer {DEEPSEEK_KEY}'})
            with urllib.request.urlopen(req, timeout=60) as r: raw = json.load(r)['choices'][0]['message']['content']
            d = json.loads(raw)
            h = re.sub(r'\s+', ' ', str(d.get('headline', ''))).strip().strip('"«»').rstrip('.')
            c = str(d.get('caption', '')).strip()
            if h and len(h) <= MAX_HEADLINE: return h, c[:900]
            msgs += [{'role': 'assistant', 'content': raw}, {'role': 'user', 'content': f'تیتر {len(h)} نویسه است. کوتاه‌ترش کن تا حداکثر ۶۸ نویسه شود و دوباره همان JSON را بده.'}]
        except Exception as e:
            print('deepseek failed', e); time.sleep(2)
    return '', ''

def probe_size(path):
    o = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', path]))['streams'][0]
    return o['width'], o['height']

def find_logos(path, frames=36):
    """Boxes (x, y, w, h in full-size pixels) of static, edge-rich overlays (channel logos, handles, URLs) in the corners.
    A pixel counts as overlay when it hardly changes across the whole video but has strong local contrast.
    Returns [] when the picture itself is static (a locked-off shot), so real content is never erased."""
    import numpy as np
    W, H = probe_size(path)
    w, h = 320, max(2, int(320 * H / W) // 2 * 2)
    dur = max(1.0, video_length(path))
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf', f'fps={frames / dur:.4f},scale={w}:{h},format=gray', '-f', 'rawvideo', '-'],
                         stdout=subprocess.PIPE, check=True).stdout
    n = len(raw) // (w * h)
    if n < 6: return []
    a = np.frombuffer(raw[:n * w * h], np.uint8).reshape(n, h, w).astype(np.float32)
    std, mean = a.std(0), a.mean(0)
    gy, gx = np.gradient(mean)
    edge = np.hypot(gx, gy)
    static = (std < 7) & (edge > 10)
    if (std < 7).mean() > 0.85: return []
    out = []
    ch, cw = int(h * 0.25), int(w * 0.42)
    for ys, xs in ((slice(0, ch), slice(0, cw)), (slice(0, ch), slice(w - cw, w)), (slice(h - ch, h), slice(0, cw)), (slice(h - ch, h), slice(w - cw, w))):
        m = static[ys, xs]
        if m.sum() < 25: continue
        yy, xx = np.nonzero(m)
        y0, y1 = np.percentile(yy, [1, 99]); x0, x1 = np.percentile(xx, [1, 99])
        y0 += ys.start; y1 += ys.start; x0 += xs.start; x1 += xs.start
        pad = 4
        x0, y0, x1, y1 = max(1, x0 - pad), max(1, y0 - pad), min(w - 2, x1 + pad), min(h - 2, y1 + pad)
        bw, bh = (x1 - x0) / w, (y1 - y0) / h
        if bw < 0.04 or bh < 0.02 or bw > 0.4 or bh > 0.22: continue
        out.append((int(x0 * W / w), int(y0 * H / h), int((x1 - x0) * W / w), int((y1 - y0) * H / h)))
    return out

def clean_source(src, mode, work):
    """Pre-process the source: 'zoom' = centre crop 15 % (pushes corner logos out of frame); 'delogo' = erase detected corner overlays."""
    W, H = probe_size(src)
    out = os.path.join(work, 'fixed.mp4')
    if mode == 'zoom':
        vf = f'crop=trunc(iw/1.15/2)*2:trunc(ih/1.15/2)*2,scale={W}:{H},setsar=1'
    else:
        boxes = find_logos(src)
        print('logo boxes', boxes, flush=True)
        if not boxes: return src
        vf = ','.join(f'delogo=x={min(x, W - 3)}:y={min(y, H - 3)}:w={max(2, min(w, W - x - 2))}:h={max(2, min(h, H - y - 2))}' for x, y, w, h in boxes)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-vf', vf, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-c:a', 'copy', out], check=True)
    return out

class NoVideo(Exception): pass

def fetch_tweet(url):
    """Public tweet -> (text, best mp4 url) through X's embed endpoint (no login needed)."""
    tid = TWEET_RE.search(url).group(1)
    req = urllib.request.Request(f'https://cdn.syndication.twimg.com/tweet-result?id={tid}&token=a&lang=en', headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r: d = json.load(r)
    best = None
    for m in d.get('mediaDetails', []):
        for v in (m.get('video_info') or {}).get('variants', []):
            if v.get('content_type') == 'video/mp4' and (best is None or v.get('bitrate', 0) > best.get('bitrate', 0)): best = v
    if not best: raise NoVideo()
    return d.get('text', ''), best['url']

def clean_caption(t):
    t = re.sub(r'https?://\S+', '', t); t = t.replace('#', '').replace('_', ' ')
    return re.sub(r'\s+', ' ', t).strip()

def fallback_headline(t):
    t = clean_caption(t)
    if len(t) <= MAX_HEADLINE - 6: return t
    return t[:MAX_HEADLINE - 6].rsplit(' ', 1)[0]

REVIEWS = {}

class TooLong(Exception): pass

def render_job(job):
    work = tempfile.mkdtemp(prefix='job_')
    try:
        src, out = os.path.join(work, 'in.mp4'), os.path.join(work, 'out.mp4')
        prog = Progress(job['chat'])
        prog.set(5, 'دریافت ویدیو', force=True)
        caption_note = ''
        if job.get('url'):
            if not job.get('video_url'):
                text, job['video_url'] = fetch_tweet(job['url'])
                if not job.get('headline'):
                    job['headline'], job['caption'] = news_from(clean_caption(text))
                    if not job['headline']:
                        job['headline'] = fallback_headline(text)
                        caption_note = '\n⚠️ ترجمهٔ خودکار انجام نشد؛ متن اصلی کوتاه شده است. با «اصلاح تیتر» درستش کن.'
            req = urllib.request.Request(job['video_url'], headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=300) as r, open(src, 'wb') as f: shutil.copyfileobj(r, f)
        else:
            download(job['file_id'], src)
        if job.get('fix'):
            prog.set(10, 'اصلاح ویدیو (زوم یا حذف لوگو)', force=True)
            src = clean_source(src, job['fix'], work)
        prog.set(15, 'آماده‌سازی تیتر')
        h = balance(job['headline']).replace('\n', '|')
        layout = job.get('layout') or ('land' if is_landscape(src) else 'portrait')
        t_out = headline_seconds(video_length(src))
        prog.set(25, 'طراحی تیتر و نشان')
        p = subprocess.run(['node', os.path.join(HERE, 'render_overlay.mjs'), '--h', h, '--out', work, '--out-time', str(t_out), '--layout', layout, '--strict'], cwd=HERE)
        if p.returncode == 3: raise TooLong()
        if p.returncode: raise RuntimeError('overlay render failed')
        prog.set(40, 'ساخت و ترکیب ویدیو', force=True)
        cp = subprocess.Popen([sys.executable, os.path.join(HERE, 'compose.py'), src, work, out, str(round(t_out + 0.95, 2)), layout], stdout=subprocess.PIPE, text=True)
        for line in cp.stdout:  # compose.py prints PROGRESS <0..1> while ffmpeg runs
            if line.startswith('PROGRESS '):
                try: prog.set(40 + 52 * float(line.split()[1]), 'ساخت و ترکیب ویدیو')
                except ValueError: pass
        if cp.wait(): raise subprocess.CalledProcessError(cp.returncode, 'compose.py')
        prog.set(94, 'ارسال ویدیو', force=True)
        if os.path.getsize(out) > MAX_UP:
            say(job['chat'], f'ویدیوی نهایی از {MAX_UP // 1048576} مگابایت بیشتر شد و تلگرام اجازهٔ ارسالش را نمی‌دهد. ویدیوی کوتاه‌تری بفرست.')
            return
        extra = {}
        if job.get('url'):
            extra = {'caption': ('' + f"{job['headline']}{caption_note}" + (f"\n\n{job['caption']}" if job.get('caption') else '') + f"\n\n{job['url']}")[:1020], 'reply_markup': review_keyboard(job['id'])}
            REVIEWS[job['id']] = {'chat': job['chat'], 'url': job['url'], 'video_url': job.get('video_url', ''), 'headline': job['headline'], 'caption': job.get('caption', ''), 'layout': layout, 'fix': job.get('fix', '')}
        call('sendVideo', {'chat_id': job['chat'], 'supports_streaming': 'true', 'width': 1080, 'height': 1920, **extra},
             {'video': ('iraftv-zarrebin.mp4', open(out, 'rb').read(), 'video/mp4')}, timeout=300)
        prog.done()
    finally:
        shutil.rmtree(work, ignore_errors=True)

def render():
    s = load()
    left = []
    for job in s['jobs']:
        try:
            render_job(job)
        except NoVideo:
            say(job['chat'], 'این توییت ویدیو ندارد (یا خصوصی است).')
        except TooLong:
            say(job['chat'], 'تیتر برای دو خط با اندازهٔ استاندارد خیلی بلند است و فونت باید ریز می‌شد. هر خط حدود ۳۶ حرف جا دارد (حدود ۷۰ حرف در کل). کوتاه‌ترش کن و دوباره بفرست؛ ویدیو نگه داشته شده است.')
            if job.get('url'):
                REVIEWS[job['id']] = {'chat': job['chat'], 'url': job['url'], 'video_url': job.get('video_url', ''), 'headline': job.get('headline', ''), 'caption': job.get('caption', ''), 'layout': job.get('layout', ''), 'fix': job.get('fix', '')}
                s['pending'][job['chat']] = {'step': 'edit', 'review': job['id'], 'at': time.time()}
            else:
                s['pending'][job['chat']] = {'file_id': job['file_id'], 'at': time.time(), 'step': 'headline', 'layout': job.get('layout', '')}
        except Exception as e:
            print('job failed', job['id'], e)
            job['tries'] = job.get('tries', 0) + 1
            if job['tries'] < 2: left.append(job)
            else: say(job['chat'], 'ساخت این ویدیو انجام نشد. لطفاً دوباره بفرستش یا ویدیوی دیگری امتحان کن.')
    s['jobs'] = left
    s.setdefault('reviews', {}).update(REVIEWS); REVIEWS.clear()
    for k in list(s['reviews'])[:-60]: s['reviews'].pop(k)
    save(s)

_installed = False
def ensure_renderer():
    global _installed
    if _installed: return
    for c in ('python3 -c "import numpy" || pip install --break-system-packages numpy', 'npm install --no-audit --no-fund', 'npx playwright install --with-deps chromium',
              'which ffmpeg || sudo apt-get install -y ffmpeg'):
        subprocess.check_call(c, shell=True)
    _installed = True

def serve(seconds=280):
    """Long-poll Telegram for ~seconds, answering immediately; render jobs as soon as they are queued."""
    end = time.time() + seconds
    while time.time() < end:
        s = load()
        try:
            process(s, timeout=min(25, max(1, int(end - time.time()))))
        except Exception as e:
            wait = 3
            body = ''
            if isinstance(e, urllib.error.HTTPError):
                try: body = e.read().decode('utf-8', 'replace')[:300]
                except Exception: pass
                try: wait = max(3, min(120, int(json.loads(body).get('parameters', {}).get('retry_after', 3)) + 1))
                except Exception: wait = 30 if e.code == 429 else 3
            print('poll failed', e, body, 'waiting', wait, flush=True)
            time.sleep(wait)
            continue
        if load()['jobs']:
            try:
                ensure_renderer()
            except Exception as e:
                print('renderer install failed', e); time.sleep(10); continue
            render()

if __name__ == '__main__':
    if not TOKEN: sys.exit('BOT_TOKEN missing')
    {'check': check, 'render': render, 'serve': serve}[sys.argv[1]]()
