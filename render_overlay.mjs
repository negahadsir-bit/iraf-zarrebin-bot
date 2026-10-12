#!/usr/bin/env node
// Renders the headline overlay with transparency.
//   node render_overlay.mjs --h "تیتر|خط دوم" [--label "ذره‌بین"] [--out work] [--fps 30] [--out-time 5.2]
// Writes <out>/overlay.mov (ProRes 4444, animated part 0..T) and <out>/static.png (badge + watermark after T).
import { createRequire } from 'module';
import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf('--' + k); return i >= 0 ? args[i + 1] : d; };
const here = path.dirname(fileURLToPath(import.meta.url));
function loadPlaywright() {
  const roots = [here + '/', process.cwd() + '/', '/opt/npm-tools/node_modules/', '/usr/lib/node_modules/', '/usr/local/lib/node_modules/'];
  for (const r of roots) for (const n of ['playwright', 'playwright-core']) { try { return createRequire(r)(n); } catch {} }
  console.error('playwright not found'); process.exit(1);
}
if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync('/opt/pw-browsers')) process.env.PLAYWRIGHT_BROWSERS_PATH = '/opt/pw-browsers';
const { chromium } = loadPlaywright();

const outDir = opt('out', 'work'); fs.mkdirSync(outDir, { recursive: true });
const fps = Number(opt('fps', 30));
const outT = Number(opt('out-time', 5.2));
const total = outT + 0.95;                 // animated part lasts until the headline is fully gone
const q = new URLSearchParams({ alpha: '1', layout: opt('layout', 'portrait'), h: opt('h', ''), label: opt('label', 'ذره‌بین'), out: String(outT) });
const url = 'file://' + path.join(here, 'template', 'index.html') + '?' + q.toString();

const browser = await chromium.launch({ args: ['--allow-file-access-from-files'] });
const page = await (await browser.newContext({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 })).newPage();
await page.goto(url);
await page.waitForFunction(() => typeof window.seek === 'function');
await page.evaluate(() => window.ready);
await page.waitForTimeout(150);
const fit = await page.evaluate(() => window.__fit);
if (args.includes('--strict') && fit && !fit.ok) {
  console.error('HEADLINE_TOO_LONG ' + JSON.stringify(fit));
  await browser.close(); process.exit(3);
}
const stage = await page.$('#stage');
const shot = t => page.evaluate(t => window.seek(t), t).then(() => stage.screenshot({ type: 'png', omitBackground: true }));

const mov = path.join(outDir, 'overlay.mov');
const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-i', '-',
  '-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p10le', mov], { stdio: ['pipe', 'inherit', 'inherit'] });
const n = Math.round(total * fps);
for (let i = 0; i < n; i++) {
  const buf = await shot(i / fps);
  if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
}
ff.stdin.end();
if (await new Promise(r => ff.on('close', r))) { console.error('ffmpeg failed'); process.exit(1); }
fs.writeFileSync(path.join(outDir, 'static.png'), await shot(total + 0.5));
// cover frame: fully composed headline + shade + watermark, used as the very first video frame
fs.writeFileSync(path.join(outDir, 'cover.png'), await shot(Math.min(1.8, outT - 0.5)));
if (opt('stills')) {
  for (const t of opt('stills').split(',').map(Number)) fs.writeFileSync(path.join(outDir, `t${t.toFixed(2)}.png`), await shot(t));
}
console.log(JSON.stringify({ overlay: mov, static: path.join(outDir, 'static.png'), animSeconds: total }));
await browser.close();
