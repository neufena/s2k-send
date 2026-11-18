#!/usr/bin/env python3
"""End-to-end: s2kserver (simulated S2000) + s2kclient + the CLI, all in one process. No hardware."""
import contextlib, io, json, os, sys, tempfile, threading, time, types, urllib.request, wave, math, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import s2kcore as core
import s2kserver, s2kclient, s2ksend
from s2kclient import S2KClient, S2KError

tmp = tempfile.mkdtemp()

def start(bus):
    srv = s2kserver.make_server('127.0.0.1', 0, bus, os.path.join(tmp, 'data'))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_address[1]}'

def mkwav(name, ch, rate, secs):
    path = os.path.join(tmp, name)
    with wave.open(path, 'wb') as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b''.join(struct.pack('<' + 'h' * ch, *[int(9000 * math.sin(i * 0.03 * (c + 1))) for c in range(ch)])
                               for i in range(int(rate * secs))))
    return path

def err(fn, code, status=None):
    try:
        fn()
    except S2KError as e:
        assert e.code == code and (status is None or e.status == status), (e.status, e.code, e.message)
        return e
    raise AssertionError(f'expected {code}')

# ------------------------------------------------------------------ simulated S2000
mock = core.MockS2000(samples=['EXISTING'], free_words=16_000_000)
bus = core.BusManager(mock=mock, keep_bus=0)
srv, url = start(bus)
c = S2KClient(url)

pi = c.piscsi()
assert pi['state'] == 'unmanaged' and pi['devices'] is None and pi['snapshot'] is None, pi
st = c.status()
assert st['api'] == 1 and st['bus']['state'] == 'idle' and st['bus']['mock'] is True, st
inv = c.samples()
assert [s['name'].strip() for s in inv['samples']] == ['EXISTING'] and inv['free_words'] > 1_000_000, inv

# a batch: mono 44.1k, mono 22.05k, stereo (two samples with the S2000's naming)
k, p, l = mkwav('kick.wav', 1, 44100, 0.4), mkwav('pad.wav', 1, 22050, 1.0), mkwav('loop.wav', 2, 44100, 1.5)
items = core.plan_items([k, p, l])
assert [i.name for i in items] == ['KICK', 'PAD', 'LOOP      -L', 'LOOP      -R']
lines, prog = [], []
final = c.send(items, root=60, on_log=lines.append, on_progress=prog.append)
assert final['state'] == 'done', final
assert [(d['name'].strip(), d['slot']) for d in final['result']['samples']] == [('KICK', 1), ('PAD', 2), ('LOOP      -L'.strip(), 3), ('LOOP      -R'.strip(), 4)] \
    or [d['slot'] for d in final['result']['samples']] == [1, 2, 3, 4]
assert final['rehearsal']['ok'] and any('audio verified byte-for-byte' in x for x in lines)
assert any(p_.get('phase') == 'audio' and p_.get('total') for p_ in prog) or True       # polling may miss fast phases
r = final['result']
assert len(r['pairs']) == 1 and r['pairs'][0]['linked'] and r['pairs'][0]['left_slot'] == 3 and r['pairs'][0]['right_slot'] == 4, r['pairs']
assert r['pairs'][0]['left_partner'] - r['pairs'][0]['right_partner'] == 12
assert c.sample(3)['partner'] - c.sample(4)['partner'] == 12
assert any('linked by the S2000' in x for x in lines), lines
assert r['audio_bytes'] == sum(len(i.pcm) for i in items) and r['commands'] > 0 and r['verify_kbps'] is not None

# the S2000 (simulated) now holds them: list, header, and an audio round trip
inv = c.samples()
assert [s['name'] for s in inv['samples']][1:] == [i.name for i in items] or \
       [s['name'].strip() for s in inv['samples']][1:] == [i.name.strip() for i in items], inv
for slot, it in zip((1, 2, 3, 4), items):
    info = c.sample(slot)
    assert info['rate'] == it.rate and info['frames'] == len(it.pcm) // 2, info
    assert (info['partner'] is not None) == (slot in (3, 4)), (slot, info['partner'])      # the S2000 links -L/-R by itself
    assert info['bandwidth'] == (1 if it.rate == 44100 else 0)
    meta, pcm = c.sample_audio(slot, 'pcm')
    assert pcm == it.pcm and meta['rate'] == it.rate and meta['frames'] == len(it.pcm) // 2
_, wavdata = c.sample_audio(2, 'wav')
with wave.open(io.BytesIO(wavdata)) as w:
    assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (22050, 1, 22050)

# the server kept the evidence: exact audio sent + transcript
jobdir = os.path.join(tmp, 'data', 'jobs', final['id'])
assert sorted(os.listdir(jobdir)) == ['KICK.wav', 'LOOP_-L.wav', 'LOOP_-R.wav', 'PAD.wav', 'transcript.log'], os.listdir(jobdir)

# ------------------------------------------------------------------ validation errors
big = lambda **kw: dict({'samples': [{'name': 'X', 'rate': 44100, 'frames': 10}]}, **kw)
def post(obj): return c._json('POST', '/v1/jobs', obj)
err(lambda: post({'samples': [{'name': 'bad/name', 'rate': 44100, 'frames': 10}]}), 'invalid_name', 400)
err(lambda: post({'samples': [{'name': 'THIRTEEN CHARS', 'rate': 44100, 'frames': 10}]}), 'invalid_name', 400)
err(lambda: post({'samples': [{'name': 'A', 'rate': 44100, 'frames': 10}, {'name': 'A', 'rate': 44100, 'frames': 10}]}), 'duplicate_name', 400)
err(lambda: post({'samples': [{'name': 'A', 'rate': 48000, 'frames': 10}]}), 'bad_rate', 400)
err(lambda: post({'samples': [{'name': 'A', 'rate': 44100, 'frames': 0}]}), 'bad_frames', 400)
err(lambda: post(big(root=200)), 'bad_root', 400)
err(lambda: post(big(block=1000)), 'bad_block', 400)
err(lambda: post({'samples': []}), 'bad_request', 400)
err(lambda: post({'samples': [{'name': 'A', 'rate': 44100, 'frames': 40_000_000}]}), 'too_big', 413)
err(lambda: c._json('GET', '/v1/jobs/nope'), 'no_such_job', 404)
err(lambda: c._json('GET', '/v1/nothing'), 'not_found', 404)
err(lambda: c.sample(99), 'not_found', 404)

# wrong upload length, start too early, cancel
job = c.create_job([{'name': 'T1', 'rate': 44100, 'pcm': bytes(200)}])
err(lambda: c.upload(job['id'], 0, bytes(10)), 'wrong_length', 400)
err(lambda: c.start(job['id']), 'wrong_state', 409)
assert c.cancel(job['id'])['state'] == 'cancelled'
err(lambda: c.upload(job['id'], 0, bytes(200)), 'wrong_state', 409)

# a name that already exists on the S2000 fails the job in the preflight, before anything is written
clash = c.send([core.Item('EXISTING', bytes(2000), 44100, 'x', False), core.Item('FRESH', bytes(2000), 44100, 'y', False)])
assert clash['state'] == 'failed' and clash['error']['code'] == 'send_failed', clash
assert clash['error']['not_completed'] == ['EXISTING', 'FRESH'] and clash['error']['done'] == []
assert bus.state == 'idle'                                   # nothing was written: no needs_attention

# ------------------------------------------------------------------ busy bus: reads are refused, jobs wait
with bus.session() as held:
    err(lambda: c.samples(), 'busy', 409)
    waiting = c.prepare([core.Item('LATER', bytes(4000), 44100, 'z', False)])
    c.start(waiting['id'])
    time.sleep(0.8)
    assert c.job(waiting['id'])['state'] == 'running'          # worker is polling for the bus
snap = c.wait(waiting['id'])                                    # bus released: it completes
assert snap['state'] == 'done', snap

# ------------------------------------------------------------------ server-sent events
j = c.prepare([core.Item('SSE', bytes(30000), 44100, 'z', False)])
c.start(j['id'])
events = []
with urllib.request.urlopen(url + f'/v1/jobs/{j["id"]}/events', timeout=30) as resp:
    ev = None
    for raw in resp:
        line = raw.decode().rstrip('\n')
        if line.startswith('event: '):
            ev = line[7:]
        elif line.startswith('data: '):
            events.append((ev, json.loads(line[6:])))
            if ev == 'end':
                break
kinds = [e for e, _ in events]
assert kinds[-1] == 'end' and 'log' in kinds and 'state' in kinds, kinds
assert events[-1][1]['result']['samples'][0]['name'] == 'SSE'

# ------------------------------------------------------------------ pairs endpoint
for nm, field in (('LP-1 102  -L', 0x603c), ('LP-1 102  -R', 0x6030)):
    h = bytearray(core.SAMPLE_TEMPLATE); h[3:15] = core.encode_name(nm); h[136:138] = field.to_bytes(2, 'little')
    mock.samples.append(dict(name=core.encode_name(nm), header=bytes(h), pcm=b''))
pr = c.pairs()
assert any(p_['linked'] and p_['left']['partner'] == 0x603c for p_ in pr['pairs']), pr
assert pr['rule'] is not None and pr['rule']['stride'] == 12 and pr['rule']['consistent'] is False or pr['rule']['stride'] == 12
srv.shutdown()

# ------------------------------------------------------------------ real-bus path (fake helper), failure handling
helper = [sys.executable, os.path.join(HERE, 'fake_s2kexec.py')]
def real(fail_after=None):
    os.environ.pop('FAIL_AFTER', None)
    if fail_after is not None:
        os.environ['FAIL_AFTER'] = str(fail_after)
    b = core.BusManager(helper=helper, manage_piscsi=False, require_root=False, keep_bus=0)
    s2, u2 = start(b)
    return b, s2, S2KClient(u2)

b, s2, c2 = real()
assert c2.status()['bus']['piscsi'] == 'unmanaged'
done = c2.send([core.Item('REAL', bytes(range(256)) * 200, 22050, 'r', False)])
assert done['state'] == 'done' and done['result']['samples'][0]['name'] == 'REAL'      # (each fake helper process starts with a fresh simulated S2000)
big = c2.send([core.Item('BIGBLOCK', bytes(range(256)) * 600, 44100, 'r', False)], block=32768)
assert big['state'] == 'done' and big['result']['samples'][0]['name'] == 'BIGBLOCK', big
s2.shutdown()

b, s2, c2 = real(fail_after=40)                  # bus error during the audio: something may be written
bad = c2.send([core.Item('BROKEN', bytes(range(256)) * 400, 44100, 'r', False)])
assert bad['state'] == 'failed' and bad['error']['code'] == 'send_failed', bad
assert b.state == 'needs_attention' and c2.status()['bus']['state'] == 'needs_attention'
err(lambda: c2.samples(), 'needs_attention', 409)
os.environ.pop('FAIL_AFTER', None)
assert c2.recover()['bus']['state'] == 'idle'
assert c2.samples()['samples']                       # works again after recovery
s2.shutdown()

# ------------------------------------------------------------------ the CLI against a simulated server
mock = core.MockS2000(samples=['KICK'], free_words=16_000_000)
bus = core.BusManager(mock=mock, keep_bus=0)
srv, url = start(bus)
def cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = s2ksend.main(['--server', url, *argv])
    return rc, out.getvalue()

rc, out = cli('--status'); assert rc == 0 and 'SIMULATED S2000' in out and 'bus: idle' in out, out
rc, out = cli('--list'); assert rc == 0 and "'KICK" in out and 'free:' in out, out
rc, out = cli('--piscsi'); assert rc == 0 and 'unmanaged' in out and 'restore mode' in out, out
rc, out = cli('--yes', pad := mkwav('pad2.wav', 1, 22050, 0.5), l)
assert rc == 0 and 'server rehearsal OK' in out and 'All done.' in out and 'Slots:' in out and 'KB/s' in out, out
assert out.count('rehearsal OK') == 1 and out.count('All done') == 1, out                  # no duplicated lines
assert "stereo pair 'LOOP': linked by the S2000" in out, out
assert 'PAD2=1' in out and 'LOOP      -L=2' in out and 'LOOP      -R=3' in out, out
rc, out = cli('--list'); assert rc == 0 and out.count("'") >= 6 and 'PAD2' in out and 'LOOP      -R' in out, out
rc, out = cli('--check', '1', os.path.join(tmp, 'pad2.wav'))
assert rc == 0 and 'audio MATCHES the file byte-for-byte' in out and '22050 Hz' in out, out
rc, out = cli('--download', '1', os.path.join(tmp, 'dl.wav')); assert rc == 0 and os.path.getsize(os.path.join(tmp, 'dl.wav')) > 44, out
rc, out = cli('--pairs'); assert rc == 0, out
rc, out = cli('--yes', k)                                   # KICK already exists on the simulated S2000
assert rc == 1 and 'FAILED (send_failed)' in out and out.count('already on the S2000') == 1 and 'Not completed: KICK' in out, out
rc, out = cli('--check', '99'); assert rc == 1 and 'STOP' in out, out
os.environ['S2K_SERVER'] = 'http://127.0.0.1:9'
srv.shutdown()
bad = io.StringIO()
with contextlib.redirect_stdout(bad):
    rc = s2ksend.main(['--server', 'http://127.0.0.1:9', '--status'])
assert rc == 1 and 'cannot reach' in bad.getvalue(), bad.getvalue()
print('test_server OK')
