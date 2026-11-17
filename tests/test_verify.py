#!/usr/bin/env python3
"""Rehearsal tests for the read-back handling (simulator only; no hardware)."""
import os, sys, types, io, contextlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import s2kcore as s

PCM = bytes(range(256)) * 69          # 17664 bytes


class Mut(s.MockS2000):
    """Simulated S2000 that reports some header bytes differently, as real firmware may."""
    def __init__(self, patch, **k):
        super().__init__(**k)
        self.patch = patch

    def _sysex(self, f):
        if f[3] == 43 and self.samples and not getattr(self, '_done', False):
            h = bytearray(self.samples[-1]['header'])
            self.patch(h)
            self.samples[-1]['header'] = bytes(h)
            self._done = True
        super()._sysex(f)


def run(patch):
    m = Mut(patch, samples=['EXISTING'])
    notes = []
    sess = s.Session(s.MockBus(m), lambda x: None)
    try:
        sess.send_sample('KICK', 60, PCM, True, progress=notes.append)
        return 'OK', notes
    except s.VerifyError as e:
        return 'VerifyError', notes
    except s.SendError as e:
        return 'SendError', notes


def internal(h): h[22:26] = b'\x01\x02\x03\x04'; h[34:38] = (8832).to_bytes(4, 'little')     # not critical
def renamed(h): h[3:6] = bytes([1, 2, 3])                                                      # critical
def newrate(h): h[138:140] = (22050).to_bytes(2, 'little')                                     # critical

r, n = run(lambda h: None);  assert r == 'OK' and sum('[normal' in x for x in n) == 2 and not any('[not critical]' in x for x in n), (r, n)
r, n = run(internal);        assert r == 'OK' and sum('[not critical]' in x for x in n) == 1, (r, n)
r, n = run(renamed);         assert r == 'VerifyError', (r, n)
r, n = run(newrate);         assert r == 'VerifyError', (r, n)

# the Bridge's own engine expects exactly this request for a 16174-frame sample (RAWBREAK164, slice 1)
assert s.pcm_read_request(0, 16174).hex() == 'f047000c480000000000002e7e00000100f7'
# real S2000 values seen on 16 Nov 2026: stereo partner reads back as ffff for a mono sample
d = s.header_diffs(s.sample_header('KICK', 60, 17664), bytes(s.sample_header('KICK', 60, 17664)[:136]) + b'\xff\xff' + bytes(s.sample_header('KICK', 60, 17664)[138:]))
assert [x[2] for x in d] == ['stereo partner'] and not d[0][5]
# audio diagnosis helper
import array, random, tempfile
random.seed(3)
w = array.array('h', [random.randrange(-20000, 20000) for _ in range(4000)])
want = w.tobytes()
sw = array.array('h', w); sw.byteswap()
assert 'swapped' in s.pcm_diagnosis(want, sw.tobytes())
flip = array.array('h', [s._flip_sign(v) for v in w])
assert flip != w and 'sign bit' in s.pcm_diagnosis(want, flip.tobytes())
assert 'shifted by 1' in s.pcm_diagnosis(want, (w[1:] + array.array('h', [0])).tobytes())
assert 'shifted by -1' in s.pcm_diagnosis(want, (array.array('h', [0]) + w[:-1]).tobytes())
dith = array.array('h', [v + random.choice((-1, 0, 1)) for v in w])
assert 'dither' in s.pcm_diagnosis(want, dith.tobytes())
assert s.pcm_diagnosis(want, want) == 'identical'
assert 'length differs' in s.pcm_diagnosis(want, want[:-2])

# a read-back that differs must raise VerifyError and save the raw data
class Bad(s.MockS2000):
    def _sysex(self, f):
        super()._sysex(f)
        if f[3] == 12 and self.pcm_out:
            self.pcm_out = bytes(b ^ 1 for b in self.pcm_out[:4]) + self.pcm_out[4:]
cwd = os.getcwd(); os.chdir(tempfile.mkdtemp())
try:
    sess = s.Session(s.MockBus(Bad(samples=['EXISTING'])), lambda x: None)
    try:
        sess.send_sample('KICK', 60, PCM, True)
        raise SystemExit('expected VerifyError')
    except s.VerifyError as e:
        assert 'DIFFERS' in str(e) and os.path.exists('KICK-readback.pcm') and os.path.exists('KICK-expected.pcm')
finally:
    os.chdir(cwd)
print('test_verify OK')
