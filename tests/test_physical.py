#!/usr/bin/env python3
"""Runs the real physical() flow (preflight, batch of samples at both rates, verify, reporting) through the real
HelperBus line protocol, against a fake helper backed by the simulator. No hardware."""
import os, sys, types, array, random, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import s2ksend as s

random.seed(5)
def rnd(frames): return array.array('h', [random.randrange(-30000, 30000) for _ in range(frames)]).tobytes()

items = [s.Item('KICK', rnd(30000), 44100, 'kick.wav', False),
         s.Item('LOOP-L', rnd(100000), 22050, 'loop.wav', False),
         s.Item('LOOP-R', rnd(100000), 22050, 'loop.wav', False),
         s.Item('SNARE', rnd(25000), 44100, 'snare.wav', True)]

LAST_RATES = {}

def run(items, fail_after=None, block=16384):
    os.environ.pop('FAIL_AFTER', None)
    if fail_after is not None:
        os.environ['FAIL_AFTER'] = str(fail_after)
    s.open_bus = lambda a, say: s.HelperBus([sys.executable, os.path.join(HERE, 'fake_s2kexec.py')], 7, 6)
    out = []
    cwd = os.getcwd(); work = tempfile.mkdtemp(); os.chdir(work)
    a = types.SimpleNamespace(log=os.devnull, yes=True, target=6, initiator=7, root=60, no_verify_pcm=False,
                              manage_piscsi=False, sent_dir='s2k-sent', block=block)
    try:
        rc = s.physical(a, items, out.append)
        files = sorted(os.listdir('s2k-sent'))
        import wave
        LAST_RATES.clear()
        for f in files:
            with wave.open(os.path.join('s2k-sent', f)) as w:
                LAST_RATES[f] = (w.getframerate(), w.getnchannels(), w.getnframes())
    finally:
        os.chdir(cwd)
    return rc, out, files

rc, out, files = run(items)
assert rc == 0 and files == ['KICK.wav', 'LOOP-L.wav', 'LOOP-R.wav', 'SNARE.wav'], (rc, files)
assert sum('audio verified byte-for-byte' in x for x in out) == 4, out
assert any('All done. 4 sample(s)' in x and 'audio writes' in x and 'verify reads' in x for x in out), out
assert any('Slots: KICK=1, LOOP-L=2, LOOP-R=3, SNARE=4' in x for x in out), out
# the saved exact-audio files carry the right rate, mono, and length
assert LAST_RATES == {'KICK.wav': (44100, 1, 30000), 'LOOP-L.wav': (22050, 1, 100000),
                      'LOOP-R.wav': (22050, 1, 100000), 'SNARE.wav': (44100, 1, 25000)}, LAST_RATES

rc, out, files = run(items, block=32768)
assert rc == 0, out

# bus fails part-way through the batch: finished samples are reported, the rest listed as not completed
rc, out, files = run(items, fail_after=150)
assert rc == 1 and any('Already in the S2000 RAM from this run:' in x for x in out), out
assert any('Not completed:' in x for x in out) and any('SCSI bus error' in x for x in out), out

# a name clash is caught by the preflight BEFORE anything is written
rc, out, files = run([s.Item('EXISTING', rnd(1000), 44100, 'e.wav', False)])
assert rc == 1 and any('already on the S2000' in x for x in out) and not any('may have been written' in x for x in out), out
print('test_physical OK')
