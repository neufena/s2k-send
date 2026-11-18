#!/usr/bin/env python3
"""Sample rates, auto rate choice, name planning and batch loading (needs sox for the conversion cases)."""
import os, sys, math, struct, wave, shutil, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import s2kcore as s

# header: 22.05 kHz => bandwidth 0, rate 22050; 44.1 kHz => bandwidth 1, rate 44100
h44, h22 = s.sample_header('A', 60, 100, 44100), s.sample_header('A', 60, 100, 22050)
assert (h44[1], h44[138:140]) == (1, (44100).to_bytes(2, 'little'))
assert (h22[1], h22[138:140]) == (0, (22050).to_bytes(2, 'little'))
assert h44[20:22] == h22[20:22] == b'\0\0'                       # the S2000's own rates need no retuning
for bad in (3999, 65536):
    try: s.sample_header('A', 60, 100, bad); raise SystemExit('rate not rejected')
    except s.SendError: pass
# other rates: played at the nearer engine rate and retuned by the pitch offset (AKAISDS's rule, kept by the S2000)
assert s.rate_fields(11025) == (0, -12.0) and s.rate_fields(88200 // 2) == (1, 0.0)
bw, tune = s.rate_fields(32000)                                   # nearer 44.1 kHz than 22.05 kHz (on a log scale)
assert bw == 1 and abs(tune - 12 * math.log2(32000 / 44100)) < 1e-9
h11, h32, h48 = (s.sample_header('A', 60, 100, r) for r in (11025, 32000, 48000))
assert (h11[1], h11[20:22]) == (0, bytes([0x00, 0xF4]))          # -12.00 semitones in 8.8 fixed point (AKAISDS: same bytes)
assert h32[1] == 1 and abs(s.decode_tune(h32[20:22]) - tune) < 1 / 256 and h32[138:140] == (32000).to_bytes(2, 'little')
assert h48[1] == 1 and 1.46 < s.decode_tune(h48[20:22]) < 1.48
assert s.decode_tune(s.encode_tune(-5.5)) == -5.5 and s.decode_tune(s.encode_tune(0.25)) == 0.25
# the header differences a 22.05 kHz readback would show are only the normal ones
assert s.header_diffs(h22, h22) == []

# auto rate
assert [s.choose_rate(r) for r in (8000, 11025, 22050, 32000, 44100, 48000, 96000)] == [22050]*3 + [44100]*4
assert s.choose_rate(48000, '22050') == 22050 and s.choose_rate(11025, '44100') == 44100
assert s.choose_rate(32000, 'native') == 32000 and s.choose_rate(48000, '32000') == 32000
assert s.choose_rate(None) == 44100                              # unreadable source rate: auto still works
for src, opt in ((96000, 'native'), (2000, 'native'), (44100, '70000'), (None, 'native'), (44100, '44.1k'), (44100, None)):
    try: s.choose_rate(src, opt); raise SystemExit(f'{src} {opt} not rejected')
    except s.SendError: pass

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
    nat = s.plan_items([mk('h48.wav', 1, 3, 48000, 0.5)], rate='native')   # 24-bit -> 16-bit, rate kept
    assert nat[0].rate == 48000 and nat[0].converted and len(nat[0].pcm) == 2 * 24000
n32 = s.plan_items([mk('n32.wav', 1, 2, 32000, 0.25)], rate='native')      # already 16-bit: used as is
assert (n32[0].rate, n32[0].converted, len(n32[0].pcm)) == (32000, False, 2 * 8000)
mm = s.plan_items([l], mono_mix=True)
assert len(mm) == 1 and mm[0].name == 'LOOP'
try: s.plan_items([k, l], name='X'); raise SystemExit('--name with a batch not rejected')
except s.SendError: pass

# whole batch rehearsal at both rates, both block sizes
for blk in (8192, 16384, 32768):
    s.rehearse(items, 60, True, lambda m: None, blk)
s.rehearse(n32, 60, True, lambda m: None)                          # a non-native rate rehearses too
# stereo names follow the S2000's own convention: -L / -R are the LAST two of the 12 characters
assert s.sample_names('Loop', 2) == ['LOOP      -L', 'LOOP      -R']
assert all(len(n) == 12 for n in s.sample_names('A very long stereo name', 2))
assert s.safe_stem('LOOP      -L') == 'LOOP_-L'
print('test_batch_rates OK')
