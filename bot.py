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

def process(s, timeout=0):
    r = call('getUpdates', {'offset': s['offset'], 'timeout': timeout, 'allowed_updates': json.dumps(['message'])}, timeout=timeout + 30)
    for u in r.get('result', []):
        s['offset'] = u['update_id'] + 1
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
        elif text.startswith('/'):
            if text.startswith('/start') or text.startswith('/help'):
                say(chat, 'سلام 👋\nاول ویدیو را بفرست، بعد تیتر را در یک پیام متنی بنویس. برای شکستن خط، تیتر را در چند خط بنویس.\nساخت چند دقیقه طول می‌کشد و پیشرفتش را همین‌جا می‌بینی.')
            elif text.startswith('/cancel'):
                s['pending'].pop(chat, None); say(chat, 'لغو شد. هر وقت خواستی ویدیوی تازه‌ای بفرست.')
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

class TooLong(Exception): pass

def render_job(job):
    work = tempfile.mkdtemp(prefix='job_')
    try:
        src, out = os.path.join(work, 'in.mp4'), os.path.join(work, 'out.mp4')
        prog = Progress(job['chat'])
        prog.set(5, 'دریافت ویدیو', force=True)
        download(job['file_id'], src)
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
        call('sendVideo', {'chat_id': job['chat'], 'supports_streaming': 'true', 'width': 1080, 'height': 1920},
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
        except TooLong:
            say(job['chat'], 'تیتر برای دو خط با اندازهٔ استاندارد خیلی بلند است و فونت باید ریز می‌شد. هر خط حدود ۳۶ حرف جا دارد (حدود ۷۰ حرف در کل). کوتاه‌ترش کن و دوباره بفرست؛ ویدیو نگه داشته شده است.')
            s['pending'][job['chat']] = {'file_id': job['file_id'], 'at': time.time(), 'step': 'headline', 'layout': job.get('layout', '')}
        except Exception as e:
            print('job failed', job['id'], e)
            job['tries'] = job.get('tries', 0) + 1
            if job['tries'] < 2: left.append(job)
            else: say(job['chat'], 'ساخت این ویدیو انجام نشد. لطفاً دوباره بفرستش یا ویدیوی دیگری امتحان کن.')
    s['jobs'] = left
    save(s)

_installed = False
def ensure_renderer():
    global _installed
    if _installed: return
    for c in ('npm install --no-audit --no-fund', 'npx playwright install --with-deps chromium',
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
