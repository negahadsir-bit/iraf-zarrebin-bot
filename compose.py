#!/usr/bin/env python3
"""compose.py input.mp4 workdir output.mp4
Needs workdir/overlay.mov (headline, animated part) and workdir/static.png (watermark) from render_overlay.mjs.
Badge (assets/badge.mov, 9.2 s) is time-stretched to the video length:
  intro 0-3.0 s of the clip -> frozen hold -> outro placed so it starts 5.3 s before the end -> short gap -> intro replays (loop).
Videos shorter than 9 s get intro + hold only."""
import subprocess, sys, json, os
IN, W, OUT = sys.argv[1:4]
HERE = os.path.dirname(os.path.abspath(__file__))
BADGE = os.path.join(HERE, 'assets', 'badge.mov')
ANIM_T = float(sys.argv[4]) if len(sys.argv) > 4 else 6.15     # headline animation length
FORCE = sys.argv[5] if len(sys.argv) > 5 else ''                # 'land' | 'portrait' (asked from the user); empty = auto-detect
BX, BY, BW, BH = 656, 328, 309, 154                              # badge placement on 1080x1920 (matches the safe-zone layout sample)

L = float(json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', IN]))['format']['duration'])
fmt = f"fps=30,scale={BW}:{BH}:flags=lanczos,format=rgba,setsar=1,setpts=PTS-STARTPTS"
parts, labels = [], []
def seg(name, trim, extra=''):
    parts.append(f"[{name}_src]{trim},{fmt}{extra}[{name}]"); labels.append(f"[{name}]")

if L >= 9:
    t_out = L - 5.3                      # outro (visible part) starts here
    hold = max(0.0, (t_out - 0.2) - 3.0)
    parts.append("[1:v]split=4[in_src][hold_src][out_src][rep_src]")
    seg('in', 'trim=0:3.0')
    seg('hold', 'trim=5.0:5.1', f",tpad=stop_mode=clone:stop_duration={hold:.3f}")
    seg('out', 'trim=6.5:9.2')
    parts.append(f"color=c=0x00000000:s={BW}x{BH}:r=30:d=0.12,format=rgba,setsar=1[gap]"); labels.append('[gap]')
    seg('rep', 'trim=0.4:9.2')
else:
    hold = max(0.0, L - 3.0)
    parts.append("[1:v]split=2[in_src][hold_src]")
    seg('in', 'trim=0:3.0')
    seg('hold', 'trim=5.0:5.1', f",tpad=stop_mode=clone:stop_duration={hold:.3f}")
parts.append(''.join(labels) + f"concat=n={len(labels)}:v=1:a=0[badge]")
fc = ';'.join(parts)
pr = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height:stream_side_data=rotation', '-of', 'json', IN]))['streams'][0]
VW, VH = pr['width'], pr['height']
rot = abs(int((pr.get('side_data_list') or [{}])[0].get('rotation', 0))) % 180
if rot == 90: VW, VH = VH, VW
LAND = (FORCE == 'land') if FORCE else (VW > VH * 1.05)
if LAND:
    # horizontal source: dim greyscale copy fills the story frame, the real picture sits in the middle at full colour
    bgchain = ("[0:v]split=2[b0][f0];"
               "[b0]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=6,vflip,eq=saturation=0:brightness=-0.10,lutyuv=y=val*0.6,setsar=1,fps=30,format=yuv420p[bgd];"
               "[f0]scale=1080:-2:flags=lanczos,setsar=1,fps=30,format=yuv420p[fg];"
               "[bgd][fg]overlay=0:(H-h)/2:shortest=0[bg]")
else:
    bgchain = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,format=yuv420p[bg]"
fc += (";" + bgchain +
       ";[bg][2:v]overlay=0:0:eof_action=pass[a]"
       f";[a][3:v]overlay=0:0:enable='gte(t,{ANIM_T})':shortest=0[a2]"
       ";[a2]null[b]"
       f";[b][badge]overlay={BX}:{BY}:eof_action=pass,format=yuv420p[v]")
cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-i', IN, '-i', BADGE, '-i', os.path.join(W, 'overlay.mov'), '-loop', '1', '-i', os.path.join(W, 'static.png'),
       '-filter_complex', fc, '-map', '[v]', '-map', '0:a?', '-t', f'{L:.3f}',
       '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
       '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
       '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', OUT]
subprocess.check_call(cmd)
print('ok', OUT, f'{L:.1f}s')
