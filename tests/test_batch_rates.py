#!/usr/bin/env python3
"""Sample rates, auto rate choice, name planning and batch loading (needs sox for the conversion cases)."""
import os, sys, math, struct, wave, shutil, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import s2kcore as s

# header: 22.05 kHz => bandwidth 0, rate 22050; 44.1 kHz => bandwidth 1, rate 44100
h44, h22 = s.sample_header('A', 60, 100, 44100), s.sample_header('A', 60, 100, 22050)
assert (h44[1], h44[138:140]) == (1, (44100).to_bytes(2, 'little'))
assert (h22[1], h22[138:140]) == (0, (22050).to_bytes(2, 'little'))
try: s.sample_header('A', 60, 100, 48000); raise SystemExit('rate not rejected')
except s.SendError: pass
# the header differences a 22.05 kHz readback would show are only the normal ones
assert s.header_diffs(h22, h22) == []

# auto rate
assert [s.choose_rate(r) for r in (8000, 11025, 22050, 32000, 44100, 48000, 96000)] == [22050]*3 + [44100]*4
assert s.choose_rate(48000, '22050') == 22050 and s.choose_rate(11025, '44100') == 44100

# names for a batch
used = set()
for stem, ch in [('kick', 1), ('kick', 1), ('Loop', 2), ('loop', 2), ('a_very_long_sample_name_1', 1), ('a_very_long_sample_name_2', 1)]:
    names = s.sample_names(stem, ch, used)
    assert not (set(names) & used) and all(len(n) <= 12 for n in names), names
    used |= set(names)
assert len(used) == 2 + 4 + 2, used          # all unique: KICK KICK2, LOOP-L/R LOOP2-L/R, two long names
assert 'KICK2' in used and 'LOOP2     -L' in used

tmp = tempfile.mkdtemp()
def mk(name, ch, sw, fr, secs):
    path = os.path.join(tmp, name)
    w = wave.open(path, 'wb'); w.setnchannels(ch); w.setsampwidth(sw); w.setframerate(fr)
    out = bytearray()
    for i in range(int(fr * secs)):
        for c in range(ch):
            v = math.sin(i * 2 * math.pi * (200 + 100 * c) / fr)
            out += struct.pack('<h', int(12000 * v)) if sw == 2 else int(2 ** 22 * v).to_bytes(3, 'little', signed=True)
    w.writeframes(bytes(out)); w.close()
    return path

k = mk('kick.wav', 1, 2, 44100, 0.5)
l = mk('loop.wav', 2, 2, 22050, 1.0)
items = s.plan_items([k, l])
assert [(i.name, i.rate, i.converted) for i in items] == [('KICK', 44100, False), ('LOOP      -L', 22050, False), ('LOOP      -R', 22050, False)]
assert len(items[0].pcm) == 44100 and len(items[1].pcm) == len(items[2].pcm) == 44100
assert items[1].pcm != items[2].pcm            # left and right really are different channels
# forcing a rate resamples (needs sox)
if shutil.which('sox'):
    up = s.plan_items([l], rate='44100')
    assert up[0].rate == 44100 and up[0].converted and len(up[0].pcm) in range(2 * 44100 - 4, 2 * 44100 + 5)
    low = s.plan_items([mk('v16.wav', 1, 2, 16000, 1.0)])          # 16 kHz source -> 22.05 kHz
    assert low[0].rate == 22050 and low[0].converted
    hi = s.plan_items([mk('h48.wav', 1, 3, 48000, 0.5)])           # 24-bit 48 kHz -> 44.1 kHz
    assert hi[0].rate == 44100 and hi[0].converted
    again = s.plan_items([mk('h48.wav', 1, 3, 48000, 0.5)])
    assert again[0].pcm == hi[0].pcm                                # deterministic: no dither
mm = s.plan_items([l], mono_mix=True)
assert len(mm) == 1 and mm[0].name == 'LOOP'
try: s.plan_items([k, l], name='X'); raise SystemExit('--name with a batch not rejected')
except s.SendError: pass

# whole batch rehearsal at both rates, both block sizes
for blk in (8192, 16384, 32768):
    s.rehearse(items, 60, True, lambda m: None, blk)
# stereo names follow the S2000's own convention: -L / -R are the LAST two of the 12 characters
assert s.sample_names('Loop', 2) == ['LOOP      -L', 'LOOP      -R']
assert all(len(n) == 12 for n in s.sample_names('A very long stereo name', 2))
assert s.safe_stem('LOOP      -L') == 'LOOP_-L'
print('test_batch_rates OK')
