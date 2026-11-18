#!/usr/bin/env python3
"""Batch-mode inventory (read once before and once after the batch), stereo link report, and the busy-wait."""
import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import s2kcore as core

pcm = lambda n: bytes((i * 7) & 255 for i in range(n * 2))
items = [core.Item('KICK', pcm(5000), 44100, 'k', False), core.Item('SNARE', pcm(7000), 22050, 's', False),
         core.Item('LOOP      -L', pcm(9000), 44100, 'l', False), core.Item('LOOP      -R', pcm(9000), 44100, 'l', False)]

def run(fn, mock=None):
    mock = mock or core.MockS2000(samples=['A', 'B'], free_words=1_000_000)
    sess = core.Session(core.MockBus(mock), lambda m: None)
    return mock, sess, fn(sess)

# --- fewer commands than reading the whole list after every sample
mock, sess, res = run(lambda s: core.op_send(s, items, 60))
batch_cmds = res['commands']
mock2 = core.MockS2000(samples=['A', 'B'], free_words=1_000_000)
sess2 = core.Session(core.MockBus(mock2), lambda m: None)
sess2.preflight([i.name for i in items], [len(i.pcm) // 2 for i in items])
for it in items:
    sess2.send_sample(it.name, 60, it.pcm, True, rate=it.rate)             # old behaviour: inventory after each
assert batch_cmds <= sess2.commands - 15 * (len(items) - 1) + 15, (batch_cmds, sess2.commands)   # saves 15 per sample after the first, pays 15 for the pair check
assert [s['slot'] for s in res['samples']] == [2, 3, 4, 5]
assert [(core.decode_name(s['name']).strip(), s['pcm'] == i.pcm) for s, i in zip(mock.samples[2:], items)] == [(i.name.strip(), True) for i in items]
# the free-memory bookkeeping used between samples matches the (simulated) S2000
assert mock.samples[5]['header'][26:30] == (9000).to_bytes(4, 'little')

# --- the S2000 links the -L/-R pair by itself; the report says so
assert len(res['pairs']) == 1 and res['pairs'][0]['linked'] and res['pairs'][0]['left_partner'] - res['pairs'][0]['right_partner'] == 12
# a '-L' without its '-R' next to it is not reported as a pair, and an unlinked pair IS reported as not linked
mock3, _, res3 = run(lambda s: core.op_send(s, items[:3], 60))
assert res3['pairs'] == []
class NoLink(core.MockS2000):
    def _autolink(self, slot): pass
_, _, res4 = run(lambda s: core.op_send(s, items[2:], 60), NoLink(samples=['A'], free_words=1_000_000))
assert len(res4['pairs']) == 1 and res4['pairs'][0]['linked'] is False, res4['pairs']

# --- the final inventory catches a surprise (a program appearing mid-batch)
class Surprise(core.MockS2000):
    def _sysex(self, f):
        super()._sysex(f)
        if f[3] == 0x34 and not self.programs:
            self.programs.append(core.encode_name('SURPRISE'))
try:
    run(lambda s: core.op_send(s, items[:2], 60), Surprise(samples=['A'], free_words=1_000_000))
    raise SystemExit('expected SendError')
except core.SendError as e:
    assert not isinstance(e, core.VerifyError) and 'sample list after the batch' in str(e)
# ...and, since samples were written, the bus needs attention rather than going back to idle
sbus = core.BusManager(mock=Surprise(samples=['A'], free_words=1_000_000), keep_bus=0)
try:
    with sbus.session() as sess:
        core.op_send(sess, items[:2], 60)
except core.SendError:
    pass
assert sbus.state == 'needs_attention', sbus.state

# --- a pair whose entries are not adjacent in the table is still reported as linked (the same rule as --pairs)
class Gap(core.MockS2000):
    @staticmethod
    def _entry_address(slot):
        return (0x6018 + 24 * slot).to_bytes(2, 'little')
_, _, res5 = run(lambda s: core.op_send(s, items[2:], 60), Gap(samples=['A'], free_words=1_000_000))
assert res5['pairs'][0]['linked'], res5['pairs']

# --- memory is allocated in even numbers of words: a batch that does not fit is refused before anything is written
tight = core.MockS2000(samples=['A'], free_words=10_001)
tight.samples[0]['pcm'] = b''
odd = [core.Item('ODD', pcm(5001), 44100, 'o', False), core.Item('EVEN', pcm(5000), 44100, 'e', False)]
tsess = core.Session(core.MockBus(tight), lambda m: None)
try:
    core.op_send(tsess, odd, 60)
    raise SystemExit('expected SendError')
except core.SendError as e:
    assert 'needs 10002 words' in str(e) and not tsess.mutated, e
fits = core.MockS2000(samples=['A'], free_words=10_002)
_, _, res6 = run(lambda s: core.op_send(s, odd, 60), fits)
assert len(res6['samples']) == 2

# --- busy: a running operation is refused at once; the keep-bus PiSCSI restart is waited for
bus = core.BusManager(mock=core.MockS2000(samples=['A'], free_words=1_000_000), keep_bus=0)
with bus.session():
    t0 = time.monotonic()
    try:
        with bus.session():
            raise SystemExit('expected Busy')
    except core.Busy:
        assert time.monotonic() - t0 < 1.0
def hold_as_restart():
    bus._restoring = True
    bus.lock.acquire()
    time.sleep(0.6)
    bus.lock.release()
    bus._restoring = False
th = threading.Thread(target=hold_as_restart); th.start(); time.sleep(0.15)
t0 = time.monotonic()
with bus.session() as sess:                       # arrives while the "timer" is restarting PiSCSI: waits, then works
    assert core.op_inventory(sess)['samples']
assert 0.3 < time.monotonic() - t0 < 5, time.monotonic() - t0
th.join()
# the keep-bus timer firing while a real operation holds the bus never flags a restart, so readers are not made to wait
class Watch:
    def __init__(self, lock): self.lock, self.flags = lock, []
    def acquire(self, *a, **k):
        self.flags.append(bus._restoring)
        return self.lock.acquire(*a, **k)
    def release(self): self.lock.release()
bus.lock = Watch(bus.lock)
with bus.session():
    bus.lock.flags.clear()
    bus._timed_restart()
    assert bus.lock.flags == [False] and bus._restoring is False, bus.lock.flags
    t0 = time.monotonic()
    try:
        with bus.session():
            raise SystemExit('expected Busy')
    except core.Busy:
        assert time.monotonic() - t0 < 1.0
print('test_batch_mode OK')
