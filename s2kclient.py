"""s2kclient - Python client for s2kserver (see API.md). Standard library only, so a GUI can vendor this file.

    from s2kclient import S2KClient
    c = S2KClient('http://piscsi.local:8420')
    print(c.samples())                                   # slots, names, free memory
    job = c.send([{'name': 'KICK', 'rate': 44100, 'pcm': pcm_bytes}], root=60, on_log=print)

`pcm` is raw 16-bit signed little-endian MONO audio at `rate` (any integer from 4000 to 65535 Hz). Names are 12 characters from
0-9 space A-Z # + - . (the client chooses them; the server rejects invalid or duplicate names).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TERMINAL = ('done', 'failed', 'cancelled')


class S2KError(Exception):
    """An error reply from the server, or the server could not be reached."""

    def __init__(self, status, code, message, details=None):
        super().__init__(f'{code}: {message}')
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def _field(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


class S2KClient:
    def __init__(self, base_url='http://localhost:8420', timeout=120.0):
        self.base = base_url.rstrip('/')
        self.timeout = timeout

    # ---------------------------------------------------------------- plumbing
    def _request(self, method, path, body=None, ctype=None):
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if ctype:
            req.add_header('Content-Type', ctype)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                err = json.loads(data)['error']
                raise S2KError(e.code, err.get('code', 'error'), err.get('message', ''), err) from None
            except (ValueError, KeyError):
                raise S2KError(e.code, 'http_error', data[:200].decode('utf-8', 'replace')) from None
        except (urllib.error.URLError, OSError) as e:
            raise S2KError(0, 'unreachable', f'cannot reach {self.base}: {getattr(e, "reason", e)}') from None

    def _json(self, method, path, obj=None):
        body = None if obj is None else json.dumps(obj).encode()
        return json.loads(self._request(method, path, body, 'application/json' if body else None)[2])

    # ---------------------------------------------------------------- server / bus
    def status(self):
        return self._json('GET', '/v1/status')

    def probe(self):
        return self._json('POST', '/v1/probe')

    def piscsi(self):
        return self._json('GET', '/v1/piscsi')

    def recover(self):
        return self._json('POST', '/v1/recover')

    # ---------------------------------------------------------------- the sampler (read-only)
    def samples(self):
        return self._json('GET', '/v1/samples')

    def sample(self, slot):
        return self._json('GET', f'/v1/samples/{int(slot)}')

    def sample_audio(self, slot, fmt='pcm'):
        """Returns (info, bytes): info has name, rate, frames, root; bytes is raw PCM or a WAV file."""
        _, h, data = self._request('GET', f'/v1/samples/{int(slot)}/audio?format={fmt}')
        info = {'name': h.get('X-S2K-Name'), 'rate': int(h.get('X-S2K-Rate', 0)),
                'frames': int(h.get('X-S2K-Frames', 0)), 'root': int(h.get('X-S2K-Root', 0))}
        return info, data

    def start_download(self, slot):
        """Queue a download job for the sample in `slot`; follow it like a send job (job(), wait(), events)."""
        return self._json('POST', f'/v1/samples/{int(slot)}/download')

    def download_audio(self, job_id, fmt='pcm'):
        """A finished download's audio: raw PCM by default (as sample_audio()), or fmt='wav'."""
        return self._request('GET', f'/v1/jobs/{job_id}/audio?format={fmt}')[2]

    def download(self, slot, fmt='pcm', on_log=None, on_progress=None):
        """Download with progress: returns (info, bytes) like sample_audio(); on_progress receives
        {'phase': 'download', 'done', 'total'} (bytes) as the audio is read. Raises S2KError if the job fails."""
        job = self.start_download(slot)
        final = self.wait(job['id'], on_log, on_progress)
        if final['state'] != 'done':
            err = final['error'] or {}
            raise S2KError(0, err.get('code', final['state']), err.get('message', f'download {final["state"]}'), err)
        return final['result'], self.download_audio(job['id'], fmt)

    def pairs(self):
        return self._json('GET', '/v1/pairs')

    # ---------------------------------------------------------------- send jobs
    def create_job(self, items, root=60, verify=True, block=16384):
        specs = []
        for it in items:
            pcm = _field(it, 'pcm')
            spec = {'name': _field(it, 'name'), 'rate': _field(it, 'rate'), 'frames': len(pcm) // 2}
            if _field(it, 'root') is not None:
                spec['root'] = _field(it, 'root')
            specs.append(spec)
        return self._json('POST', '/v1/jobs', {'samples': specs, 'root': root, 'verify': verify, 'block': block})

    def upload(self, job_id, index, pcm):
        return json.loads(self._request('PUT', f'/v1/jobs/{job_id}/samples/{index}', bytes(pcm),
                                        'application/octet-stream')[2])

    def start(self, job_id):
        return self._json('POST', f'/v1/jobs/{job_id}/start')

    def cancel(self, job_id):
        return self._json('DELETE', f'/v1/jobs/{job_id}')

    def job(self, job_id, since=0):
        return self._json('GET', f'/v1/jobs/{job_id}?since={since}')

    def jobs(self):
        return self._json('GET', '/v1/jobs')['jobs']

    def job_files(self, job_id):
        """The evidence the server kept for a job: [{'name', 'bytes'}] (audio sent, transcript, mismatch dumps)."""
        return self._json('GET', f'/v1/jobs/{job_id}/files')['files']

    def job_file(self, job_id, name):
        return self._request('GET', f'/v1/jobs/{job_id}/files/{urllib.parse.quote(name, safe="")}')[2]

    def wait(self, job_id, on_log=None, on_progress=None, interval=0.3, since=0, until=TERMINAL):
        """Poll until the job reaches one of the states in `until`; stream new log lines to on_log(str)."""
        last_progress = None
        while True:
            snap = self.job(job_id, since)
            for line in snap['log']:
                if on_log:
                    on_log(line['msg'])
            since = snap['next']
            if on_progress and snap['progress'] != last_progress:
                last_progress = snap['progress']
                on_progress(last_progress)
            if snap['state'] in until:
                return snap
            time.sleep(interval)

    def prepare(self, items, root=60, verify=True, block=16384):
        """Create the job and upload all audio. The server rehearses the whole batch when the last upload
        arrives, so the returned snapshot is 'ready' (rehearsal passed) or 'failed' (see snap['error'])."""
        snap = self.create_job(items, root, verify, block)
        for i, it in enumerate(items):
            snap = self.upload(snap['id'], i, _field(it, 'pcm'))
        return snap

    def send(self, items, root=60, verify=True, block=16384, on_log=None, on_progress=None, confirm=None):
        """Convenience: prepare, optionally ask confirm(snapshot) -> bool, start, wait. Returns the final
        snapshot (state 'done' / 'failed' / 'cancelled')."""
        snap = self.prepare(items, root, verify, block)
        if snap['state'] != 'ready':
            return snap
        if confirm is not None and not confirm(snap):
            self.cancel(snap['id'])
            return self.job(snap['id'])
        self.start(snap['id'])
        return self.wait(snap['id'], on_log, on_progress)
