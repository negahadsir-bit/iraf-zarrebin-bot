#!/usr/bin/env python3
"""Synthesises assets/news_bed.mp3: a calm, mellow background bed (A minor, 76 BPM, 12 bars, loops cleanly).
Soft pad chords, a warm bass note per bar and a sparse bell-like pluck melody; no drums. All generated here, no samples."""
import numpy as np, subprocess, sys
SR, BPM, BARS = 44100, 76, 12
beat = 60 / BPM; bar = 4 * beat; N = int(SR * bar * BARS)
f = lambda m: 440 * 2 ** ((m - 69) / 12)
def add(buf, snd, at):
    """Mix a sound in at `at` seconds, wrapping around the end so the loop has no seam."""
    idx = (int(at * SR) + np.arange(len(snd))) % len(buf)
    np.add.at(buf, idx, snd)
def lp(x, a):
    y = np.zeros_like(x)
    for i in range(1, len(x)): y[i] = y[i-1] + a * (x[i] - y[i-1])
    return y
pad = np.zeros(N); bass = np.zeros(N); bell = np.zeros(N)
prog = [(57, [0, 3, 7, 10]), (53, [0, 4, 7, 11]), (48, [0, 4, 7, 11]), (55, [0, 4, 7, 10])]  # Am7 Fmaj7 Cmaj7 G7
melody = [  # (bar, beat, midi) sparse pentatonic phrases
    (0, 0, 76), (0, 2.5, 72), (1, 1, 74), (1, 3, 72), (2, 0, 71), (2, 2, 76), (3, 1.5, 74),
    (4, 0, 76), (4, 2, 79), (5, 1, 77), (5, 3, 76), (6, 0.5, 72), (6, 2.5, 74), (7, 1, 71),
    (8, 0, 72), (8, 2, 76), (9, 1, 74), (9, 3, 72), (10, 0, 69), (10, 2, 71), (11, 1, 72),
]
for b in range(BARS):
    root, ch = prog[b % 4]; t0 = b * bar
    n = int((bar + 1.2) * SR); x = np.arange(n) / SR
    envp = np.minimum(x / 1.2, 1) * np.minimum((bar + 1.2 - x) / 1.2, 1)
    for k in ch:
        for d in (-0.003, 0.003):
            add(pad, np.sin(2 * np.pi * f(root + 12 + k) * (1 + d) * x) * envp * 0.06, t0 - 0.6)
    nb = int(bar * SR); xb = np.arange(nb) / SR
    add(bass, np.sin(2 * np.pi * f(root - 12) * xb) * np.minimum(xb / 0.05, 1) * np.exp(-xb / 2.5) * 0.45, t0)
    add(bass, np.sin(2 * np.pi * f(root - 12) * xb) * np.minimum(xb / 0.05, 1) * np.exp(-xb / 1.5) * 0.25, t0 + 2 * beat)
for b, bt, m in melody:
    n = int(2.2 * SR); x = np.arange(n) / SR
    snd = (np.sin(2 * np.pi * f(m) * x) + 0.25 * np.sin(2 * np.pi * f(m) * 2 * x) * np.exp(-x / 0.3)) * np.minimum(x / 0.004, 1) * np.exp(-x / 0.7) * 0.22
    add(bell, snd, b * bar + bt * beat)
pad = lp(pad, 0.25)
mix = pad + bass + bell
# soft echo on the bell, stereo spread
d1, d2 = int(0.375 * SR), int(0.5 * SR)
L = mix + np.roll(bell, d1) * 0.25; R = mix + np.roll(bell, d2) * 0.25
st = np.stack([L, R], 1); st = (st / np.abs(st).max() * 0.8 * 32767).astype('<i2')
out = sys.argv[1] if len(sys.argv) > 1 else 'assets/news_bed.mp3'
p = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 's16le', '-ar', str(SR), '-ac', '2', '-i', '-', '-b:a', '160k', out], stdin=subprocess.PIPE)
p.communicate(st.tobytes())
