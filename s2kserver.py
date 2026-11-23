#!/usr/bin/env python3
"""s2kserver - HTTP server that sends samples to an Akai S2000 over SCSI.

Runs ON the Raspberry Pi with the PiSCSI board (as root). Clients (the CLI s2ksend.py, or a GUI) talk to it
over plain HTTP/JSON on the home network. There is NO authentication: use it on a private network only.

    sudo s2kserver                            # real hardware, port 8420 (./s2kserver.py from a checkout)
    s2kserver --mock                          # simulated S2000: develop clients without any hardware

The API is documented in API.md. In short:
    GET  /v1/status                          server / bus state
    GET  /v1/samples                         sample list + free memory
    GET  /v1/samples/<slot>[/audio]          one sample's header / its audio (WAV or raw PCM)
    GET  /v1/pairs                           stereo -L/-R pairs and their partner fields (read-only)
    POST /v1/jobs                            create a send job (JSON: samples, root, verify, block)
    PUT  /v1/jobs/<id>/samples/<n>           upload that sample's raw 16-bit mono little-endian PCM
    POST /v1/jobs/<id>/start                 run it (after the server's own rehearsal passed)
    GET  /v1/jobs/<id>?since=N               state, progress, new log lines, result / error
    GET  /v1/jobs/<id>/events                the same as a Server-Sent Events stream
    GET  /v1/piscsi                          PiSCSI service state, attached devices, saved setup
    POST /v1/recover                         after a failed send and a power-cycle
"""
import argparse
import collections
import json
import os
import queue
import re
import shutil
import signal
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import s2kcore as core
from s2kcore import Busy, Item, NeedsAttention, SendError, VerifyError

MAX_SAMPLES = 200
MAX_TOTAL_BYTES = 64 * 1024 * 1024          # the S2000 holds at most 32 MB of 16-bit audio, plus headroom
BLOCKS = (8192, 16384, 32768)
KEEP_JOBS = 50
TERMINAL = ('done', 'failed', 'cancelled')


class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


# --------------------------------------------------------------------------- jobs
class Job:
    def __init__(self, jid, specs, root, verify, block, directory, kind='send', slot=None):
        self.id, self.specs, self.root, self.verify, self.block, self.dir = jid, specs, root, verify, block, directory
        self.kind, self.slot = kind, slot          # 'send', or 'download' of the sample in `slot`
        self.audio_file = None                     # a finished download's WAV
        self.audio = [None] * len(specs)
        self.state = 'awaiting_audio'
        self.created = time.time()
        self.log, self.progress, self.rehearsal = [], {}, None
        self.result = self.error = None
        self.done = []
        self.cond = threading.Condition()

    def add_log(self, msg):
        with self.cond:
            self.log.append((len(self.log), round(time.time(), 3), msg))
            self.cond.notify_all()

    def on_event(self, ev):
        with self.cond:
            names = [s['name'] for s in self.specs]
            self.progress = dict(ev, sample_index=names.index(ev['name']) if ev.get('name') in names else None)
            self.cond.notify_all()

    def set_state(self, state, error=None):
        with self.cond:
            self.state = state
            if error is not None:
                self.error = error
            self.cond.notify_all()

    def items(self):
        return [Item(sp['name'], self.audio[i], sp['rate'], 'api', False, sp.get('root'))
                for i, sp in enumerate(self.specs)]

    def snapshot(self, since=0):
        with self.cond:
            return {'id': self.id, 'kind': self.kind, 'slot': self.slot, 'state': self.state, 'created': self.created,
                    'root': self.root,
                    'verify': self.verify, 'block': self.block,
                    'samples': [dict(sp, uploaded=self.audio[i] is not None) for i, sp in enumerate(self.specs)],
                    'rehearsal': self.rehearsal, 'progress': self.progress,
                    'log': [{'n': n, 't': t, 'msg': m} for n, t, m in self.log[since:]],
                    'next': len(self.log), 'result': self.result, 'error': self.error}


class JobManager:
    def __init__(self, bus, data_dir):
        self.bus, self.data_dir = bus, data_dir
        self.jobs = collections.OrderedDict()
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.current = None
        threading.Thread(target=self._worker, daemon=True).start()

    # -- creation / lookup
    def create(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('samples'), list) or not body['samples']:
            raise ApiError(400, 'bad_request', 'body must be JSON with a non-empty "samples" list')
        specs = body['samples']
        if len(specs) > MAX_SAMPLES:
            raise ApiError(400, 'too_many_samples', f'at most {MAX_SAMPLES} samples per job')
        root = body.get('root', 60)
        verify = bool(body.get('verify', True))
        block = body.get('block', core.PCM_BLOCK)
        if not isinstance(root, int) or not 0 <= root < 128:
            raise ApiError(400, 'bad_root', 'root must be an integer 0-127')
        if block not in BLOCKS:
            raise ApiError(400, 'bad_block', f'block must be one of {list(BLOCKS)}')
        clean, seen, total = [], set(), 0
        fixed = core.pad_pair_names([sp['name'] for sp in specs if isinstance(sp, dict) and isinstance(sp.get('name'), str)])
        for sp in specs:
            if isinstance(sp, dict) and sp.get('name') in fixed:
                sp['name'] = fixed[sp['name']]
        for i, sp in enumerate(specs):
            if not isinstance(sp, dict):
                raise ApiError(400, 'bad_sample', f'sample {i} is not an object')
            name, rate, frames = sp.get('name'), sp.get('rate'), sp.get('frames')
            sroot = sp.get('root')
            try:
                if not isinstance(name, str):
                    raise SendError('name must be a string')
                core.encode_name(name)
            except SendError:
                raise ApiError(400, 'invalid_name', f'sample {i}: {name!r} is not a valid Akai name '
                                                    f'(max 12 characters of {core.AKAI_CHARS!r})')
            if name in seen:
                raise ApiError(400, 'duplicate_name', f'name {name!r} appears twice in the job')
            seen.add(name)
            if not core.valid_rate(rate):
                raise ApiError(400, 'bad_rate', f'sample {i}: rate must be an integer from {core.MIN_RATE} to {core.MAX_RATE}')
            if not isinstance(frames, int) or frames < 1:
                raise ApiError(400, 'bad_frames', f'sample {i}: frames must be a positive integer')
            if sroot is not None and (not isinstance(sroot, int) or not 0 <= sroot < 128):
                raise ApiError(400, 'bad_root', f'sample {i}: root must be an integer 0-127')
            total += frames * 2
            clean.append({'name': name, 'rate': rate, 'frames': frames, **({'root': sroot} if sroot is not None else {})})
        if total > MAX_TOTAL_BYTES:
            raise ApiError(413, 'too_big', f'{total} bytes of audio is more than the {MAX_TOTAL_BYTES} byte limit')
        job = self._add(lambda jid, d: Job(jid, clean, root, verify, block, d))
        for old, new in fixed.items():
            job.add_log(f'stereo name {old!r} sent as {new!r}: the S2000 only links -L/-R pairs named in that padded form')
        return job

    def create_download(self, slot):
        """A read-only job: queued at once (no upload, rehearsal or confirmation needed)."""
        job = self._add(lambda jid, d: Job(jid, [], None, False, core.PCM_BLOCK, d, kind='download', slot=slot))
        job.set_state('queued')
        self.q.put(job.id)
        return job

    def _add(self, make):
        jid = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
        job = make(jid, os.path.join(self.data_dir, 'jobs', jid))
        with self.lock:
            self.jobs[jid] = job
            finished = [j for j in self.jobs.values() if j.state in TERMINAL]
            for old in finished[:max(0, len(self.jobs) - KEEP_JOBS)]:        # never drop a job still in progress
                del self.jobs[old.id]
                if old.kind == 'download':                     # a copy of audio still on the S2000: free the SD card
                    shutil.rmtree(old.dir, ignore_errors=True)
        return job

    def get(self, jid):
        job = self.jobs.get(jid)
        if job is None:
            raise ApiError(404, 'no_such_job', f'no job {jid}')
        return job

    # -- upload + rehearsal
    def put_audio(self, job, index, data):
        with job.cond:
            if job.state != 'awaiting_audio':
                raise ApiError(409, 'wrong_state', f'job is {job.state}; audio can only be uploaded while awaiting_audio')
            if not 0 <= index < len(job.specs):
                raise ApiError(404, 'no_such_sample', f'job has {len(job.specs)} samples')
            want = job.specs[index]['frames'] * 2
            if len(data) != want:
                raise ApiError(400, 'wrong_length', f'sample {index} needs exactly {want} bytes '
                                                    f'(frames x 2), got {len(data)}')
            job.audio[index] = bytes(data)
            complete = all(a is not None for a in job.audio)
        if complete:
            self._rehearse(job)

    def _rehearse(self, job):
        try:
            n = core.rehearse(job.items(), job.root, job.verify, lambda m: None, job.block)
            job.rehearsal = {'ok': True, 'commands': n}
            job.add_log(f'rehearsal OK ({n} commands, run on a simulated S2000 before touching the real one)')
            job.set_state('ready')
        except SendError as e:
            job.rehearsal = {'ok': False, 'message': str(e)}
            job.set_state('failed', {'code': 'rehearsal_failed', 'message': str(e), 'done': [], 'not_completed': []})

    # -- start / cancel
    def start(self, job):
        with job.cond:
            if job.state != 'ready':
                raise ApiError(409, 'wrong_state', f'job is {job.state}; it can only be started when ready')
            job.state = 'queued'
            job.cond.notify_all()
        self.q.put(job.id)

    def cancel(self, job):
        with job.cond:
            if job.state in ('running',):
                raise ApiError(409, 'running', 'a running transfer cannot be cancelled safely')
            if job.state in TERMINAL:
                raise ApiError(409, 'wrong_state', f'job is already {job.state}')
            job.state = 'cancelled'
            job.cond.notify_all()

    # -- worker
    def _worker(self):
        while True:
            jid = self.q.get()
            job = self.jobs.get(jid)
            if job is None or job.state != 'queued':
                continue
            self.current = job.id
            try:
                self._run(job)
            except Exception as e:                            # never let the worker die
                traceback.print_exc()
                self._fail(job, 'internal_error', f'{type(e).__name__}: {e}')
            finally:
                self.current = None

    def _fail(self, job, code, message):
        done = list(job.done)
        left = [s['name'] for s in job.specs if s['name'] not in {d['name'] for d in done}]
        job.add_log(f'FAILED ({code}): {message}')
        job.set_state('failed', {'code': code, 'message': message, 'done': done, 'not_completed': left})

    def _run(self, job):
        os.makedirs(job.dir, exist_ok=True)
        if job.kind == 'download':
            work, budget, failed_code = self._download, 300.0, 'read_failed'
        else:
            items = job.items()
            budget = max(600.0, 120 + sum(len(i.pcm) for i in items) / 50000)
            failed_code = 'send_failed'
            work = lambda job, sess: core.op_send(sess, items, job.root, job.verify, progress=job.add_log,
                                                  done=job.done, sent_dir=job.dir)
        job.set_state('running')
        with open(os.path.join(job.dir, 'transcript.log'), 'w', buffering=1) as logf:
            for _ in range(240):                              # wait (up to 2 min) if a read operation holds the bus
                try:
                    with self.bus.session(lambda m: logf.write(m + '\n'), budget_s=budget) as sess:
                        sess.block, sess.on_event, sess.dump_dir = job.block, job.on_event, job.dir
                        job.result = work(job, sess)
                    job.set_state('done')
                    return
                except Busy:
                    time.sleep(0.5)
                except NeedsAttention as e:
                    return self._fail(job, 'needs_attention', str(e))
                except core.NoSuchSample as e:
                    return self._fail(job, 'not_found', str(e))
                except VerifyError as e:
                    return self._fail(job, 'verify_failed', str(e))
                except core.ListMismatch as e:
                    return self._fail(job, 'list_mismatch', str(e))
                except SendError as e:
                    return self._fail(job, failed_code, str(e))
        self._fail(job, 'busy', 'the bus stayed busy for two minutes')

    def _download(self, job, sess):
        t0 = time.monotonic()
        job.add_log(f'reading sample slot {job.slot}')
        info, pcm = core.op_audio(sess, job.slot)
        elapsed = time.monotonic() - t0
        path = os.path.join(job.dir, (core.safe_stem(info['name']) or f'slot{job.slot}') + '.wav')
        core.save_wav(path, pcm, info['rate'])
        job.audio_file = path
        job.add_log(f'read {info["name"]!r}: {info["frames"]} frames at {info["rate"]} Hz, {len(pcm) / 1000:.0f} KB '
                    f'in {elapsed:.1f} s')
        return dict({k: v for k, v in info.items() if k != 'header_hex'}, bytes=len(pcm), elapsed_s=round(elapsed, 2),
                    read_kbps=round(len(pcm) / max(elapsed, 1e-9) / 1000, 1))


# --------------------------------------------------------------------------- HTTP
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = f's2k-server/{core.__version__}'
    protocol_version = 'HTTP/1.0'              # one request per connection: simple, and fine for SSE

    # -- plumbing
    def log_message(self, fmt, *args):
        if self.command != 'GET' or '/events' in self.path or self.path.startswith('/v1/samples'):
            sys.stderr.write('%s %s\n' % (self.log_date_time_string(), fmt % args))

    def _send(self, status, body=b'', ctype='application/json', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode())

    def _error(self, status, code, message, **extra):
        self._json(status, {'error': dict({'code': code, 'message': message}, **extra)})

    def _body(self, limit=MAX_TOTAL_BYTES):
        n = int(self.headers.get('Content-Length') or 0)
        if n > limit:
            raise ApiError(413, 'too_big', 'request body too large')
        return self.rfile.read(n) if n else b''

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        for m, pat, fn in ROUTES:
            if m == method:
                match = pat.fullmatch(parsed.path)
                if match:
                    try:
                        return fn(self, *match.groups(), query=urllib.parse.parse_qs(parsed.query))
                    except ApiError as e:
                        return self._error(e.status, e.code, e.message, **e.extra)
                    except Busy as e:
                        return self._error(409, 'busy', str(e))
                    except NeedsAttention as e:
                        return self._error(409, 'needs_attention', str(e))
                    except LookupError as e:
                        return self._error(404, 'not_found', str(e))
                    except SendError as e:
                        return self._error(502, 'bus_error', str(e))
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    except Exception as e:
                        traceback.print_exc()
                        return self._error(500, 'internal_error', f'{type(e).__name__}: {e}')
        self._error(404, 'not_found', f'no such endpoint: {method} {parsed.path}')

    def do_GET(self): self._dispatch('GET')
    def do_PUT(self): self._dispatch('PUT')
    def do_POST(self): self._dispatch('POST')
    def do_DELETE(self): self._dispatch('DELETE')

    def do_OPTIONS(self):
        self._send(204, b'', headers={'Access-Control-Allow-Methods': 'GET, PUT, POST, DELETE, OPTIONS',
                                      'Access-Control-Allow-Headers': 'Content-Type'})

    # -- helpers
    @property
    def bus(self): return self.server.bus

    @property
    def jobs(self): return self.server.jobs

    def read_op(self, fn, label):
        """Run a read-only bus operation under the exclusive bus lock."""
        with self.bus.session(lambda m: None, budget_s=300.0) as sess:
            return fn(sess)


# -- status / probe / recover
@route('GET', r'/v1/status')
def status(h, query):
    h._json(200, {'server': 's2kserver', 'version': core.__version__, 'api': core.API_VERSION,
                  'bus': h.bus.status(),
                  'jobs': {'current': h.jobs.current, 'queued': h.jobs.q.qsize()}})


@route('POST', r'/v1/probe')
def probe(h, query):
    with h.bus.session(lambda m: None, budget_s=60.0) as sess:
        h._json(200, core.op_probe(sess.bus))


@route('GET', r'/v1/piscsi')
def piscsi_info(h, query):
    """What the server would save before stopping PiSCSI (read-only; does not use the bus)."""
    pm = h.bus.pm
    live = pm.list_devices() if (h.bus.mock is None and h.bus.manage_piscsi) else None
    h._json(200, {'state': h.bus.piscsi_state(), 'restore_mode': pm.mode, 'devices': live,
                  'snapshot': pm.info(), 'snapshot_devices': pm.snap['devices'] if pm.snap else None})


@route('POST', r'/v1/recover')
def recover(h, query):
    h.bus.recover()
    h._json(200, {'bus': h.bus.status()})


# -- read-only sample operations
@route('GET', r'/v1/samples')
def samples(h, query):
    h._json(200, h.read_op(core.op_inventory, 'inventory'))


@route('GET', r'/v1/samples/(\d+)')
def sample(h, slot, query):
    h._json(200, h.read_op(lambda s: core.op_sample(s, int(slot)), 'sample'))


@route('GET', r'/v1/samples/(\d+)/audio')
def sample_audio(h, slot, query):
    info, pcm = h.read_op(lambda s: core.op_audio(s, int(slot)), 'audio')
    _send_audio(h, info, pcm, query)


def _send_audio(h, info, pcm, query):
    fmt = (query.get('format') or ['wav'])[0]
    headers = {'X-S2K-Name': info['name'], 'X-S2K-Rate': info['rate'], 'X-S2K-Frames': info['frames'],
               'X-S2K-Root': info['root']}
    if fmt == 'pcm':
        h._send(200, pcm, 'application/octet-stream', headers)
    elif fmt == 'wav':
        h._send(200, core.wav_bytes(pcm, info['rate']), 'audio/wav', headers)
    else:
        raise ApiError(400, 'bad_format', 'format must be wav or pcm')


@route('POST', r'/v1/samples/(\d+)/download')
def sample_download(h, slot, query):
    job = h.jobs.create_download(int(slot))
    h._json(202, job.snapshot())


@route('GET', r'/v1/jobs/([\w-]+)/audio')
def job_audio(h, jid, query):
    job = h.jobs.get(jid)
    if job.kind != 'download':
        raise ApiError(400, 'not_a_download', f'job {jid} is a {job.kind} job')
    if job.state != 'done' or not job.audio_file:
        raise ApiError(409, 'wrong_state', f'the download is {job.state}; the audio is available once it is done')
    with wave.open(job.audio_file, 'rb') as w:
        pcm = w.readframes(w.getnframes())
    _send_audio(h, job.result, pcm, query)


@route('GET', r'/v1/pairs')
def pairs(h, query):
    h._json(200, h.read_op(core.op_pairs, 'pairs'))


# -- jobs
@route('GET', r'/v1/jobs')
def jobs_list(h, query):
    h._json(200, {'jobs': [{'id': j.id, 'kind': j.kind, 'state': j.state, 'created': j.created,
                            'samples': len(j.specs) if j.kind == 'send' else 1}
                           for j in reversed(list(h.jobs.jobs.values()))]})


@route('POST', r'/v1/jobs')
def job_create(h, query):
    try:
        body = json.loads(h._body(1 << 20) or b'null')
    except ValueError:
        raise ApiError(400, 'bad_json', 'request body is not valid JSON')
    job = h.jobs.create(body)
    h._json(201, dict(job.snapshot(), upload=[f'/v1/jobs/{job.id}/samples/{i}' for i in range(len(job.specs))]))


@route('GET', r'/v1/jobs/([\w-]+)')
def job_get(h, jid, query):
    since = int((query.get('since') or ['0'])[0])
    h._json(200, h.jobs.get(jid).snapshot(since))


def _job_dir(h, jid):
    """A job's evidence folder. Also works for jobs no longer held in memory (older ones, or before a restart)."""
    path = os.path.join(h.jobs.data_dir, 'jobs', jid)          # jid is [\w-]+ (route), so it cannot leave jobs/
    if not os.path.isdir(path):
        raise ApiError(404, 'no_such_job', f'no files for job {jid}')
    return path


@route('GET', r'/v1/jobs/([\w-]+)/files')
def job_files(h, jid, query):
    path = _job_dir(h, jid)
    files = [{'name': n, 'bytes': os.path.getsize(os.path.join(path, n))}
             for n in sorted(os.listdir(path)) if os.path.isfile(os.path.join(path, n))]
    h._json(200, {'id': jid, 'files': files})


@route('GET', r'/v1/jobs/([\w-]+)/files/([^/]+)')
def job_file(h, jid, name, query):
    path = _job_dir(h, jid)
    name = urllib.parse.unquote(name)
    full = os.path.join(path, name)
    if '/' in name or name in ('.', '..') or not os.path.isfile(full):
        raise ApiError(404, 'no_such_file', f'job {jid} has no file {name!r}')
    ctype = {'.wav': 'audio/wav', '.log': 'text/plain; charset=utf-8'}.get(os.path.splitext(name)[1],
                                                                          'application/octet-stream')
    with open(full, 'rb') as f:                     # streamed: audio files can be several MB on a Pi
        h.send_response(200)
        h.send_header('Content-Type', ctype)
        h.send_header('Content-Length', str(os.fstat(f.fileno()).st_size))
        h.send_header('Access-Control-Allow-Origin', '*')
        h.end_headers()
        shutil.copyfileobj(f, h.wfile)


@route('PUT', r'/v1/jobs/([\w-]+)/samples/(\d+)')
def job_put(h, jid, index, query):
    job = h.jobs.get(jid)
    h.jobs.put_audio(job, int(index), h._body())
    h._json(200, job.snapshot(len(job.log)))


@route('POST', r'/v1/jobs/([\w-]+)/start')
def job_start(h, jid, query):
    job = h.jobs.get(jid)
    since = len(job.log)                 # taken before queueing, so lines the worker logs at once are not skipped
    h.jobs.start(job)
    h._json(202, job.snapshot(since))


@route('DELETE', r'/v1/jobs/([\w-]+)')
def job_cancel(h, jid, query):
    job = h.jobs.get(jid)
    h.jobs.cancel(job)
    h._json(200, job.snapshot(len(job.log)))


@route('GET', r'/v1/jobs/([\w-]+)/events')
def job_events(h, jid, query):
    job = h.jobs.get(jid)
    h.send_response(200)
    h.send_header('Content-Type', 'text/event-stream')
    h.send_header('Cache-Control', 'no-cache')
    h.send_header('Access-Control-Allow-Origin', '*')
    h.end_headers()

    def emit(event, data):
        h.wfile.write(f'event: {event}\ndata: {json.dumps(data)}\n\n'.encode())
        h.wfile.flush()

    sent_log, last_state, last_progress = 0, None, None
    while True:
        with job.cond:
            if (len(job.log) == sent_log and job.state == last_state and job.progress == last_progress
                    and job.state not in TERMINAL):
                job.cond.wait(15)
            logs = job.log[sent_log:]
            state, progress = job.state, dict(job.progress)
            snap_final = job.snapshot(len(job.log)) if state in TERMINAL else None
        try:
            for n, t, m in logs:
                emit('log', {'n': n, 't': t, 'msg': m})
            sent_log += len(logs)
            if progress != last_progress:
                emit('progress', progress)
                last_progress = progress
            if state != last_state:
                emit('state', {'state': state})
                last_state = state
            if not logs and progress == last_progress and state == last_state and state not in TERMINAL:
                h.wfile.write(b': keep-alive\n\n')
                h.wfile.flush()
            if state in TERMINAL and sent_log >= len(job.log):
                emit('end', {'result': snap_final['result'], 'error': snap_final['error']})
                return
        except (BrokenPipeError, ConnectionResetError):
            return


# --------------------------------------------------------------------------- main
def make_server(host, port, bus, data_dir):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.bus = bus
    srv.jobs = JobManager(bus, data_dir)
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description='HTTP server that sends samples to an Akai S2000 via PiSCSI.')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8420)
    ap.add_argument('--target', type=int, default=6, help='S2000 SCSI ID (default 6)')
    ap.add_argument('--initiator', type=int, default=0, help="the Pi's SCSI ID (default 0; reserve it in PiSCSI; the Mac must be off)")
    ap.add_argument('--helper', default=None,
                    help='path to the s2kexec bus helper (default: next to s2kcore.py, then PATH)')
    ap.add_argument('--no-manage-piscsi', action='store_true',
                    help='do not stop/restart the piscsi service (then it must already be stopped)')
    ap.add_argument('--keep-bus', type=float, default=20.0,
                    help='seconds to keep PiSCSI stopped after an operation (default 20; 0 = restart at once)')
    ap.add_argument('--restore-piscsi', choices=['exact', 'attach-only', 'off'], default='exact',
                    help="after restarting PiSCSI put its device setup back as it was: 'exact' (attach what is missing, "
                         "detach what was not there before), 'attach-only', or 'off' (just restart, as before)")
    ap.add_argument('--data-dir', help='where job transcripts, the exact audio sent and the PiSCSI setup snapshot are '
                                       'kept (default /var/lib/s2k-send, as the systemd unit; ./s2k-data with --mock)')
    ap.add_argument('--mock', action='store_true', help='simulated S2000, no hardware (for developing clients)')
    ap.add_argument('--mock-samples', default='', help='comma-separated names already in the simulated S2000')
    a = ap.parse_args(argv)
    a.data_dir = a.data_dir or ('s2k-data' if a.mock else '/var/lib/s2k-send')
    log = lambda m: sys.stderr.write(f'[bus] {m}\n')
    mock = None
    if a.mock:
        names = [n for n in a.mock_samples.split(',') if n]
        mock = core.MockS2000(samples=names, free_words=16_000_000)
    bus = core.BusManager(a.target, a.initiator, a.helper, not a.no_manage_piscsi, a.keep_bus, mock,
                          require_root=True, log=log, restore_mode=a.restore_piscsi,
                          state_file=os.path.join(a.data_dir, 'piscsi-snapshot.json'))
    srv = make_server(a.host, a.port, bus, a.data_dir)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    print(f's2kserver {core.__version__} listening on {a.host}:{a.port}'
          f' ({"SIMULATED S2000" if a.mock else f"S2000 at SCSI ID {a.target}, Pi is ID {a.initiator}"})', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        bus.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
