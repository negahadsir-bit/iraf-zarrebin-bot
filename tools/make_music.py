#!/usr/bin/env python3
"""Synthesises assets/news_bed.mp3: a rhythmic, tense news-style bed (A minor, 120 BPM, 16 bars = 32 s, loops cleanly).
No samples and no licensing issues: kick, snare, hats, pulsing bass and a plucked arpeggio, all generated here."""
import numpy as np, subprocess, sys
SR, BPM, BARS = 44100, 120, 16
beat = 60 / BPM; N = int(SR * beat * 4 * BARS)
t = np.arange(N) / SR
rng = np.random.default_rng(7)
def env(n, a=0.005, d=0.2):
    x = np.arange(n) / SR
    return np.minimum(x / a, 1) * np.exp(-x / d)
def place(buf, snd, at):
    i = int(at * SR); j = min(len(buf), i + len(snd))
    if i < len(buf): buf[i:j] += snd[:j - i]
def lp(x, k):  # one-pole low-pass
    y = np.zeros_like(x); a = k
    for i in range(1, len(x)): y[i] = y[i-1] + a * (x[i] - y[i-1])
    return y
def saw(f, n, detune=0.004):
    x = np.arange(n) / SR
    return sum(2 * ((x * f * (1 + d)) % 1) - 1 for d in (-detune, 0, detune)) / 3
kick = np.zeros(N); hat = np.zeros(N); bass = np.zeros(N); arp = np.zeros(N); snare = np.zeros(N)
# kick: pitch-dropping sine
kn = int(0.28 * SR); kx = np.arange(kn) / SR
kd = np.sin(2 * np.pi * (45 * kx + 90 * (1 - np.exp(-kx * 25)) / 25 * 1.0)) * np.exp(-kx / 0.11)
hn = int(0.05 * SR); hd = (rng.standard_normal(hn) - np.r_[0, rng.standard_normal(hn)[:-1]]) * env(hn, 0.001, 0.012)
sn = int(0.22 * SR); sd = (rng.standard_normal(sn)) * env(sn, 0.001, 0.07) * 0.8 + np.sin(2 * np.pi * 190 * np.arange(sn) / SR) * env(sn, 0.001, 0.05) * 0.5
prog = [57, 53, 48, 55]  # A, F, C, G (midi roots)
f = lambda m: 440 * 2 ** ((m - 69) / 12)
for bar in range(BARS):
    root = prog[bar % 4]; quality = [0, 4, 7] if root != 57 else [0, 3, 7]
    t0 = bar * 4 * beat
    for b in range(4):
        place(kick, kd, t0 + b * beat)
        if b in (1, 3) and bar >= 2: place(snare, sd, t0 + b * beat)
    for e in range(8):
        place(hat, hd * (1.0 if e % 2 else 0.55), t0 + e * beat / 2)
        n = int(beat / 2 * SR * 0.9)
        place(bass, saw(f(root - 12), n, 0.002) * env(n, 0.004, 0.16) * 0.9, t0 + e * beat / 2)
    for s16 in range(16):
        if bar < 4 and s16 % 2: continue
        m = root + 12 + quality[[0, 1, 2, 1, 0, 2, 1, 2][s16 % 8] if s16 % 3 else 2] + (12 if s16 % 8 == 6 else 0)
        n = int(0.22 * SR)
        place(arp, saw(f(m), n, 0.003) * env(n, 0.003, 0.09) * 0.5, t0 + s16 * beat / 4)
bass = lp(bass, 0.12); arp = lp(arp, 0.35)
mix = kick * 0.95 + hat * 0.16 + snare * 0.35 + bass * 0.55 + arp * 0.32
mix /= np.abs(mix).max()
mix = np.tanh(mix * 1.6) * 0.8
# gentle stereo: delay on the arpeggio
d = int(0.18 * beat * SR)
L = mix + np.r_[np.zeros(d), arp[:-d]] * 0.12
R = mix + np.r_[np.zeros(2 * d), arp[:-2 * d]] * 0.12
st = np.stack([L, R], 1); st = (st / np.abs(st).max() * 0.85 * 32767).astype('<i2')
out = sys.argv[1] if len(sys.argv) > 1 else 'assets/news_bed.mp3'
p = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 's16le', '-ar', str(SR), '-ac', '2', '-i', '-', '-b:a', '160k', out], stdin=subprocess.PIPE)
p.communicate(st.tobytes())
