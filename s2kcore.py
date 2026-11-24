"""s2kcore - everything that talks to the Akai S2000 over SCSI, with no user interface.

Used by the server (s2kserver.py, on the Raspberry Pi with the PiSCSI board), by the client library
(s2kclient.py, audio loading and naming only) and by the tests.

The S2000's SCSI port tunnels MIDI SysEx (vendor commands 09 mode / 0C send / 0D status / 0E receive).
The sequence and encodings follow a hardware-proven implementation (see README.md for provenance).
"""
__version__ = '0.9.0.dev0'
API_VERSION = 1

import array
import collections
import contextlib
import io
import json
import math
import numbers
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

AKAI_CHARS = '0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ#+-.'
ADDR = 0                    # SysEx device address used by MESA/the proven code
PCM_BLOCK = 16384           # bytes per bulk write block (proven size)
MAX_STATUS_POLLS = 128
RATE = 44100                # default / highest rate
NATIVE_RATES = {0: 22050, 1: 44100}     # header bandwidth byte -> the S2000's playback engine rate
MIN_RATE, MAX_RATE = 4000, 65535        # any rate in between is sent as is; the header's 16-bit rate field caps it


def valid_rate(rate):
    return isinstance(rate, numbers.Integral) and not isinstance(rate, bool) and MIN_RATE <= rate <= MAX_RATE


def rate_fields(rate):
    """(bandwidth byte, pitch offset in semitones) for a sample rate. The S2000 plays at 22.05 or 44.1 kHz; any other
    rate is played at the nearer one and retuned by the header's pitch offset (STUNO). The same rule AKAISDS uses over
    MIDI, where the S2000 was seen to keep it."""
    bandwidth = min(NATIVE_RATES, key=lambda b: abs(math.log2(rate / NATIVE_RATES[b])))
    return bandwidth, 12 * math.log2(rate / NATIVE_RATES[bandwidth])


def encode_tune(semitones):
    """Pitch offset as stored at header bytes 20-21: signed 8.8 fixed point, little-endian (fraction byte first)."""
    return (round(semitones * 256) & 0xFFFF).to_bytes(2, 'little')


def decode_tune(two):
    raw = int.from_bytes(two, 'little')
    return (raw - 0x10000 if raw & 0x8000 else raw) / 256

# Sample header wire template captured from michaelsanois's S2000 Bridge release (see NOTICE), used with his
# permission. Type 3, 44.1 kHz, mono, no loop, play-to-end. Only name, root note, length, start/end and rate are patched.
SAMPLE_TEMPLATE = bytes.fromhex(
    "03013c1a1c190c0f0a0a0a0a0a0a0a8000000003000000000000000100000000"
    "0000ff0000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000044ac0000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000")
assert len(SAMPLE_TEMPLATE) == 192

ACK = bytes([0xF0, 0x47, ADDR, 0x16, 0x48, 0x00, 0xF7])


def words_used(frames):
    """Sample memory the S2000 allocates: always an even number of 16-bit words (162831 frames used 162832)."""
    return frames + frames % 2


class SendError(Exception):
    pass


class NoSuchSample(LookupError):
    """There is no sample in the requested slot."""


class ListMismatch(SendError):
    """Every sample was sent and verified, but the S2000's lists afterwards are not what was expected."""


class VerifyError(SendError):
    """A read-back comparison failed. The bus itself is healthy and quiet."""


# --------------------------------------------------------------------------- encoding
def nibbles(raw):
    out = bytearray()
    for b in raw:
        out += bytes((b & 15, b >> 4))          # low nibble first
    return bytes(out)


def unnibbles(wire):
    if len(wire) % 2 or any(b > 15 for b in wire):
        raise SendError('invalid nibble data in reply')
    return bytes(wire[i] | (wire[i + 1] << 4) for i in range(0, len(wire), 2))


def safe_stem(name):
    """File-name-safe version of an Akai name (runs of spaces become one underscore)."""
    return re.sub(r'\s+', '_', name.strip())


def akai_name(text):
    """Sanitise free text into a valid Akai name (max 12 chars of the Akai set)."""
    t = ''.join(c if c in AKAI_CHARS else ('-' if c in '_' else '') for c in text.upper())
    return (t.strip() or 'SAMPLE')[:12]


def encode_name(text):
    if not text or len(text) > 12 or any(c not in AKAI_CHARS for c in text):
        raise SendError(f'bad Akai name {text!r}')
    return bytes([AKAI_CHARS.index(c) for c in text]) + bytes([10] * (12 - len(text)))


def decode_name(raw12):
    return ''.join(AKAI_CHARS[b] if b < len(AKAI_CHARS) else '?' for b in raw12)


def sample_header(name, root, frames, rate=RATE):
    if not (1 <= frames <= 0xFFFFFFFF and 0 <= root < 128) or not valid_rate(rate):
        raise SendError('bad frames/root/rate')
    h = bytearray(SAMPLE_TEMPLATE)
    bandwidth, tune = rate_fields(rate)
    h[1] = bandwidth                      # 1 = 20 kHz (44.1 kHz engine), 0 = 10 kHz (22.05 kHz engine)
    h[20:22] = encode_tune(tune)
    h[2] = root
    h[19] = 2                             # playback type: No looping, as a sample sent over MIDI SDS gets
    h[3:15] = encode_name(name)
    h[26:30] = frames.to_bytes(4, 'little')
    h[30:34] = (0).to_bytes(4, 'little')
    h[34:38] = (frames - 1).to_bytes(4, 'little')
    h[138:140] = rate.to_bytes(2, 'little')
    return bytes(h)


def sysex_sample(header, ident):
    if not 0 <= ident < 16384:
        raise SendError('sample identifier out of range')
    return (bytes([0xF0, 0x47, ADDR, 0x0B, 0x48, ident & 127, ident >> 7])
            + nibbles(header) + b'\xF7')


def post_object():
    return bytes([0xF0, 0x47, ADDR, 0x34, 0x48, 1, 0, 0, 0, 0, 0, 0, 0xF7])


def transfer_cdb(op, n, raw=False):
    if op not in (12, 14) or not 0 <= n <= 0xFFFFFF:
        raise SendError('bad transfer cdb')
    return bytes([op, 0, (n >> 16) & 255, (n >> 8) & 255, n & 255, 128 if raw else 0])


def header_read_request(index, offset):
    return bytes([0xF0, 0x47, ADDR, 43, 0x48, index & 127, index >> 7, 0,
                  offset & 127, offset >> 7, 64, 0, 0xF7])


def pcm_read_request(index, words):
    """RSPACK request for a whole sample. `words` is the number of 16-bit sample WORDS (= frames),
    not bytes: the S2000 then advertises 2*words bytes. (Confirmed on hardware, 16 Nov 2026; the
    Bridge's newer engine also encodes the frame count here.)"""
    nbytes = words
    r = bytearray([0xF0, 0x47, ADDR, 12, 0x48, index & 127, index >> 7,
                   0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0xF7])
    for i in range(4):
        r[11 + i] = (nbytes >> (7 * i)) & 127
    return bytes(r)


LIST_PROGRAMS = bytes([0xF0, 0x47, ADDR, 2, 0x48, 0xF7])
LIST_SAMPLES = bytes([0xF0, 0x47, ADDR, 4, 0x48, 0xF7])
STAT = bytes([0xF0, 0x47, ADDR, 0, 0x48, 0xF7])


# --------------------------------------------------------------------------- reply parsing
def _common(f, op):
    if not (len(f) >= 6 and f[0] == 0xF0 and f[1] == 0x47 and f[2] == ADDR and f[3] == op
            and f[4] == 0x48 and f[-1] == 0xF7):
        raise SendError(f'unexpected reply (wanted opcode {op}): {f[:12].hex()}')
    if any(b >= 128 for b in f[1:-1]):
        raise SendError('high bit inside SysEx reply')


def parse_roster(f, sample):
    _common(f, 5 if sample else 3)
    n = f[5] | (f[6] << 7)
    if n > (4096 if sample else 254) or len(f) != 8 + 12 * n:
        raise SendError('bad roster length')
    names = [bytes(f[7 + 12 * i:19 + 12 * i]) for i in range(n)]
    if len(set(names)) != n or any(b > 40 for nm in names for b in nm):
        raise SendError('invalid or duplicate names in roster')
    return names


def parse_stat(f):
    if not (len(f) == 21 and f[0] == 0xF0 and f[1] == 0x47 and f[2] == ADDR and f[3] == 1
            and f[4] == 0x48 and f[20] == 0xF7 and all(b < 128 for b in f[2:20])):
        raise SendError('bad STAT reply')
    v = lambda at, n: sum(f[at + j] << (7 * j) for j in range(n))
    if v(9, 2) > v(7, 2) or v(15, 4) > v(11, 4):
        raise SendError('inconsistent capacity in STAT reply')
    return v(9, 2), v(15, 4), v(7, 2), v(11, 4)      # free headers, free words, total headers, total words


class Inventory:
    def __init__(self, programs, samples, free_headers, free_words, total_headers=None, total_words=None):
        self.programs, self.samples = programs, samples
        self.free_headers, self.free_words = free_headers, free_words
        self.total_headers, self.total_words = total_headers, total_words


# --------------------------------------------------------------------------- header comparison
# Field names follow Ohsaki's S3000 format notes; unnamed bytes are shown as 'other'.
FIELDS = [(0, 1, 'type', True), (1, 2, 'bandwidth', False), (2, 3, 'root note', False),
          (3, 15, 'name', True), (15, 19, 'flags/loop bytes', False), (19, 20, 'playback type', False),
          (20, 22, 'pitch offset', True), (22, 26, 'start address (internal)', False),
          (26, 30, 'length', True), (30, 34, 'start', False), (34, 38, 'end', False),
          (38, 136, 'loop data', False), (136, 138, 'stereo partner', False),
          (138, 140, 'sample rate', True), (140, 192, 'other', False)]


def header_diffs(sent, got):
    """[(first, last, field, sent_hex, got_hex, critical)] for each field that differs."""
    out = []
    for a, b, name, critical in FIELDS:
        if sent[a:b] != got[a:b]:
            out.append((a, b - 1, name, sent[a:b].hex(), got[a:b].hex(), critical))
    return out


def _flip_sign(v):
    u = (v & 0xFFFF) ^ 0x8000
    return u - 65536 if u >= 32768 else u


def pcm_diagnosis(want, got):
    """Explain how read-back audio differs from the expected audio (both 16-bit little-endian)."""
    if len(want) != len(got):
        return f'length differs: expected {len(want)} bytes, S2000 returned {len(got)}'
    a, b = array.array('h'), array.array('h')
    a.frombytes(want)
    b.frombytes(got)
    if sys.byteorder == 'big':
        a.byteswap()
        b.byteswap()
    diffs = [i for i in range(len(a)) if a[i] != b[i]]
    if not diffs:
        return 'identical'
    maxd = max(abs(a[i] - b[i]) for i in diffs)
    out = [f'{len(diffs)} of {len(a)} words differ (first at word {diffs[0]}, largest difference {maxd})']
    sw = array.array('h', b)
    sw.byteswap()
    if sw == a:
        out.append('the read-back matches if the two bytes of every word are swapped')
    if array.array('h', [_flip_sign(v) for v in b]) == a:
        out.append('the read-back matches if the sign bit of every word is flipped (unsigned storage)')
    for k in (-2, -1, 1, 2):
        lo, hi = max(0, k), len(a) + min(0, k)
        if a[lo:hi] == b[lo - k:hi - k]:
            out.append(f'the read-back matches if shifted by {k} word(s)')
    if maxd <= 2 and len(diffs) > len(a) // 4:
        out.append('the differences are tiny and widespread: typical of dither from a different sox conversion, '
                   'not of a transfer fault')
    return '; '.join(out)


def partner_map(pairs):
    """pairs: [(slot_l, field_l, slot_r, field_r)] read from real stereo pairs (field = bytes 136-137).
    If each field is the address of the partner's entry, field_l = B + s*slot_r and field_r = B + s*slot_l.
    Returns (stride, base, consistent_with_all, notes)."""
    notes, fits = [], []
    for sl, fl, sr, fr in pairs:
        if sl == sr or (fl - fr) % (sr - sl):
            notes.append(f'pair at slots {sl}/{sr}: fields 0x{fl:04x}/0x{fr:04x} do not fit a linear address rule')
            continue
        stride = (fl - fr) // (sr - sl)
        fits.append((stride, fl - stride * sr, sl, sr))
    if not fits:
        return None, None, False, notes
    stride, base = fits[0][:2]
    ok = all(f[:2] == (stride, base) for f in fits) and not notes
    for f in fits[1:]:
        if f[:2] != (stride, base):
            notes.append(f'pair at slots {f[2]}/{f[3]} gives stride {f[0]}, base 0x{f[1]:04x}: not the same rule')
    return stride, base, ok, notes


# --------------------------------------------------------------------------- session
class Session:
    """The protocol, independent of what is on the other end of the bus."""

    def __init__(self, bus, log, budget_s=600.0):
        self.bus, self.log = bus, log
        self.started = time.monotonic()
        self.deadline = self.started + budget_s
        self.commands = 0
        self.mutated = False         # a write command has been dispatched
        self.block = PCM_BLOCK       # bytes per raw audio write
        self.dump_dir = '.'          # where mismatch dumps go
        self.on_event = lambda e: None   # structured progress: {'phase', 'name', 'done', 'total'}
        self.last_header = None      # header read back for the most recently sent sample
        self.inventory_after = None  # last inventory read; reused as the next sample's "before"
        self.stats = dict(w_bytes=0, w_s=0.0, r_bytes=0, r_s=0.0)

    def cmd(self, cdb, direction, data=b'', length=0):
        if time.monotonic() > self.deadline:
            raise SendError('time budget exhausted')
        # "mutated" = something that changes the S2000 has been sent: raw audio, a sample header, or the
        # finalise message. Read-only requests (inventory, header/audio reads) do not count.
        if cdb[0] == 12 and direction == 'out' and (cdb[5] == 0x80 or (len(data) > 3 and data[3] in (0x0B, 0x34))):
            self.mutated = True
        want = len(data) if direction == 'out' else length
        self.commands += 1
        t0 = time.monotonic()
        status, transferred, got = self.bus.execute(cdb, direction, data, length)
        dt_ms = (time.monotonic() - t0) * 1000
        shown = got or data
        raw_pcm = cdb[0] in (12, 14) and cdb[5] == 0x80        # keep logs small: audio only as a prefix
        if raw_pcm and len(shown) > 20:
            shown_hex = shown[:20].hex() + f'...({len(shown)} bytes)'
        else:
            shown_hex = shown.hex()
        self.log(f'{self.commands:5d} t={t0 - self.started:8.3f}s dt={dt_ms:7.1f}ms cdb={cdb.hex()} {direction:4s} want={want} '
                 f'status={status} xfer={transferred} data={shown_hex}')
        if status != 0:
            raise SendError(f'SCSI status 0x{status:02x} for cdb {cdb.hex()}')
        if direction != 'none' and transferred != want:
            raise SendError(f'short/long transfer ({transferred} of {want}) for cdb {cdb.hex()}')
        if direction == 'in' and len(got) != length:
            raise SendError('reply buffer length mismatch')
        return got

    def mode(self, scsi, bulk=False):
        self.cmd(bytes([9, 0, 1 if scsi else 0, 1 if bulk else 0, 0, 0]), 'none')

    def control(self, payload, expect, bulk=False, keep=False):
        """Send one SysEx and collect the reply.
        expect: 'data' (one data frame) | 'ack' (Akai ACK) | 'sample' (SDS ACK/WAIT or Akai ACK)."""
        self.mode(True, bulk)
        self.cmd(transfer_cdb(12, len(payload)), 'out', payload)
        pending, frames, polls, total = bytearray(), [], 0, 0
        result = None
        while result is None:
            polls += 1
            if polls > MAX_STATUS_POLLS:
                raise SendError('no complete reply after 128 status polls')
            st = self.cmd(bytes([13, 0, 0, 0, 0, 0]), 'in', length=3)
            n = (st[0] << 16) | (st[1] << 8) | st[2]
            if n == 0:
                time.sleep(0.01)
                continue
            if n > 32768 or total + n > 65536:
                raise SendError(f'implausible reply size advertised ({n})')
            total += n
            data = self.cmd(transfer_cdb(14, n), 'in', length=n)
            for b in data:
                if not pending:
                    if b != 0xF0:
                        raise SendError('reply byte outside SysEx')
                elif b == 0xF0 or (b >= 128 and b != 0xF7):
                    raise SendError('nested/high-bit SysEx in reply')
                pending.append(b)
                if b == 0xF7:
                    if len(pending) < 6:
                        raise SendError('short SysEx in reply')
                    frames.append(bytes(pending))
                    pending.clear()
            if expect == 'data':
                if len(frames) > 1:
                    raise SendError('extra data frame')
                if frames:
                    if pending:
                        raise SendError('trailing partial reply')
                    result = frames[0]
            else:
                for f in frames:
                    verdict = self._readiness(f, expect)
                    if verdict == 'ready':
                        if pending:
                            raise SendError('trailing partial reply')
                        result = f
                frames.clear()
        if not keep:
            self.mode(False)
        return result

    @staticmethod
    def _readiness(f, expect):
        if expect == 'sample' and len(f) == 6 and f[1] == 0x7E:
            if f[2] != ADDR or f[4] >= 128:
                raise SendError('bad universal reply identity')
            if f[3] == 0x7C:
                return 'wait'
            if f[3] != 0x7F:
                raise SendError(f'S2000 refused the sample header (universal reply {f[3]:02x})')
            return 'ready'
        if f == ACK:
            return 'ready'
        raise SendError(f'unexpected reply, no data will be sent: {f.hex()}')

    # ---- queries
    def inventory(self):
        programs = parse_roster(self.control(LIST_PROGRAMS, 'data'), False)
        samples = parse_roster(self.control(LIST_SAMPLES, 'data'), True)
        fh, fw, th, tw = parse_stat(self.control(STAT, 'data'))
        return Inventory(programs, samples, fh, fw, th, tw)

    def read_header(self, index):
        raw = b''
        while len(raw) < 192:
            f = self.control(header_read_request(index, len(raw)), 'data')
            _common(f, 44)
            off = f[8] | (f[9] << 7)
            if not (len(f) == 141 and (f[5] | (f[6] << 7)) == index and f[7] == 0
                    and off == len(raw) and f[10] == 64 and f[11] == 0):
                raise SendError('header reply does not match the request')
            raw += unnibbles(f[12:-1])
        if raw[0] != 3:
            raise SendError('header read back is not a sample header')
        return raw

    def read_pcm(self, index, nbytes, phase='verify_audio', name=None):
        if nbytes > 0xFFFFFF:
            raise SendError('sample is over 16 MB: too big for the audio read-back, use --no-verify-pcm')
        self.mode(True, True)
        self.cmd(transfer_cdb(12, 18), 'out', pcm_read_request(index, nbytes // 2))
        st = self.cmd(bytes([13, 0, 0, 0, 0, 128]), 'in', length=3)
        advertised = (st[0] << 16) | (st[1] << 8) | st[2]
        if advertised != nbytes:
            self.mode(False)             # the ordinary exit command; the bus itself is healthy
            raise VerifyError(f'S2000 advertises {advertised} bytes for the audio read, expected {nbytes} '
                              f'(asked for {nbytes // 2} words)')
        got = bytearray()
        t0 = time.monotonic()
        while len(got) < nbytes:
            n = min(65534, nbytes - len(got))
            c = bytearray(transfer_cdb(14, n))
            c[5] = 128
            got += self.cmd(bytes(c), 'in', length=n)
            self.on_event({'phase': phase, 'name': name, 'done': len(got), 'total': nbytes})
        self.stats['r_s'] += time.monotonic() - t0
        self.stats['r_bytes'] += nbytes
        self.mode(False)
        return bytes(got)

    def verify_audio(self, index, pcm, label):
        got = self.read_pcm(index, len(pcm), 'verify_audio', label)
        if got == pcm:
            return
        for suffix, data in (('expected', pcm), ('readback', got)):
            with open(os.path.join(self.dump_dir, f'{label}-{suffix}.pcm'), 'wb') as f:
                f.write(data)
        raise VerifyError(f'audio read back from the S2000 DIFFERS from the file: {pcm_diagnosis(pcm, got)} '
                          f'(raw data saved as {os.path.join(self.dump_dir, label)}-expected.pcm / -readback.pcm)')

    def preflight(self, names, frame_counts):
        """One inventory before anything is written: names free, headers and memory enough for ALL samples."""
        inv = self.inventory()
        used = [encode_name(n) for n in names]
        clash = [n for n, e in zip(names, used) if e in inv.samples]
        if clash:
            raise SendError(f'already on the S2000: {", ".join(clash)}; choose another --name')
        if len(inv.samples) + len(names) > 4096 or inv.free_headers < len(names):
            raise SendError('not enough free sample headers on the S2000')
        need = sum(words_used(f) for f in frame_counts)
        if inv.free_words < need:
            raise SendError(f'needs {need} words but the S2000 has only {inv.free_words} free')
        self.inventory_after = inv        # the first sample reuses this instead of reading the lists again
        return inv

    # ---- the transfer
    def send_sample(self, name, root, pcm, verify_pcm=True, progress=lambda m: None, rate=RATE, refresh_inventory=True):
        frames = len(pcm) // 2
        before = self.inventory_after if self.inventory_after is not None else self.inventory()
        enc = encode_name(name)
        if enc in before.samples:
            raise SendError(f'a sample named {name!r} already exists on the S2000; choose another --name')
        if len(before.samples) >= 4096 or before.free_headers < 1:
            raise SendError('no free sample header on the S2000')
        if before.free_words < words_used(frames):
            raise SendError(f'sample needs {words_used(frames)} words but the S2000 has only {before.free_words} free')
        progress(f'S2000 has {len(before.samples)} samples, {before.free_words} free words')
        header = sample_header(name, root, frames, rate)
        progress('sending sample header')
        self.on_event({'phase': 'header', 'name': name})
        self.control(sysex_sample(header, len(before.samples)), 'sample', bulk=True, keep=True)
        blk_n = self.block
        for at in range(0, len(pcm), blk_n):
            blk = pcm[at:at + blk_n]
            t0 = time.monotonic()
            self.cmd(transfer_cdb(12, len(blk), raw=True), 'out', blk)
            self.stats['w_s'] += time.monotonic() - t0
            self.stats['w_bytes'] += len(blk)
            self.on_event({'phase': 'audio', 'name': name, 'done': min(at + blk_n, len(pcm)), 'total': len(pcm)})
            if (at // blk_n) % 16 == 0 or at + blk_n >= len(pcm):
                progress(f'  PCM {min(at + blk_n, len(pcm))}/{len(pcm)} bytes')
        self.mode(False)
        progress('finalising')
        self.on_event({'phase': 'finalise', 'name': name})
        self.control(post_object(), 'ack')
        if refresh_inventory:
            after = self.inventory()
            self.inventory_after = after
            if after.programs != before.programs or enc not in after.samples \
                    or sorted(after.samples) != sorted(before.samples + [enc]):
                raise SendError('inventory after the transfer is not what was expected')
            index = after.samples.index(enc)
        else:
            # Batch mode: read only the sample list to find the new sample. The S2000 puts it in the first free
            # header entry, which after a deletion on the S2000 is not the end of the list (seen 20 Nov 2026).
            # The caller checks the programs once, at the end of the batch.
            samples = parse_roster(self.control(LIST_SAMPLES, 'data'), True)
            if enc not in samples or [s for s in samples if s != enc] != before.samples:
                raise ListMismatch('the sample list after the transfer is not the old list plus the new sample')
            index = samples.index(enc)
            self.inventory_after = Inventory(before.programs, samples, before.free_headers - 1,
                                             before.free_words - words_used(frames),
                                             before.total_headers, before.total_words)
        self.on_event({'phase': 'verify_header', 'name': name})
        got = self.read_header(index)
        self.last_header = got
        diffs = header_diffs(header, got)
        for a, b, field, sv, gv, critical in diffs:
            progress(f'  header note: bytes {a}-{b} ({field}) sent {sv[:32]} / S2000 reports {gv[:32]}'
                     + ('' if critical else '  [normal: the S2000 fills this in itself]'
                        if field in ('start address (internal)', 'stereo partner') else '  [not critical]'))
        if any(d[5] for d in diffs):
            raise VerifyError('header read back from the S2000 differs in a field that matters '
                              '(type, name, length, rate or pitch offset) - see the notes above and the transcript')
        progress(f'header checked (sample slot {index})' + (' with notes above' if diffs else ''))
        if verify_pcm:
            progress('reading audio back to verify')
            self.verify_audio(index, pcm, safe_stem(name))
            progress('audio verified byte-for-byte')
        return index


# --------------------------------------------------------------------------- rehearsal S2000
class MockS2000:
    """A strict in-memory model of the behaviour the proven code expects. Used for the
    mandatory rehearsal and --selftest. It checks the sequence the sender uses; it is NOT
    proof of how real hardware behaves."""

    def __init__(self, samples=(), free_words=1_000_000):
        # The internal header table: None is a free entry (left by a sample deleted on the S2000). The sample list
        # is the table in order without the free entries, and a new sample takes the first free entry, as the real
        # S2000 was seen to do (20 Nov 2026), so it is not always added at the end.
        self.samples = [None if n is None else dict(name=encode_name(n), header=bytes(SAMPLE_TEMPLATE), pcm=b'')
                        for n in samples]
        self.programs = []
        self.free_words = free_words
        self.scsi = False
        self.bulk = False
        self.out = bytearray()
        self.followup = None
        self.pending = None
        self.pcm_in = None
        self.pcm_out = None

    def live(self):
        return [s for s in self.samples if s is not None]

    @staticmethod
    def _v7(n, count):
        return bytes((n >> (7 * i)) & 127 for i in range(count))

    def execute(self, cdb, direction, data, length):
        op = cdb[0]
        n = (cdb[2] << 16) | (cdb[3] << 8) | cdb[4]
        if op == 9:
            self.scsi, self.bulk = bool(cdb[2]), bool(cdb[3])
            return 0, 0, b''
        if op == 12:
            if len(data) != n:
                raise SendError('mock: length/data mismatch')
            if cdb[5] == 0x80:
                if not (self.pending and self.bulk and self.pcm_in is not None):
                    raise SendError('mock: raw PCM outside bulk mode / before a header')
                self.pcm_in += data
                return 0, n, b''
            if not self.scsi:
                raise SendError('mock: SysEx sent while not in SCSI-MIDI mode')
            self._sysex(bytes(data))
            return 0, n, b''
        if op == 13:
            if cdb[5] == 0x80:
                size = len(self.pcm_out or b'')
            else:
                if not self.out and self.followup:
                    self.out += self.followup
                    self.followup = None
                size = len(self.out)
            return 0, 3, bytes([size >> 16, (size >> 8) & 255, size & 255])
        if op == 14:
            if cdb[5] == 0x80:
                chunk, self.pcm_out = self.pcm_out[:n], self.pcm_out[n:]
            else:
                if n > len(self.out):
                    raise SendError('mock: receive longer than advertised')
                chunk, self.out = bytes(self.out[:n]), self.out[n:]
            return 0, len(chunk), bytes(chunk)
        raise SendError(f'mock: unknown opcode {op:#x}')

    @staticmethod
    def _entry_address(slot):
        return (0x6018 + 12 * slot).to_bytes(2, 'little')        # a table of 12-byte entries (no holes in this model)

    def _autolink(self, entry):
        """What the real S2000 was seen to do: a '-R' sample arriving after a '-L' sample with the same name start
        gets paired with it: each header's partner field becomes the address of the other's entry."""
        new = decode_name(self.samples[entry]['name'])
        if not new.endswith('-R'):
            return
        for i, smp in enumerate(self.samples):
            if smp is not None and i != entry and decode_name(smp['name'])[:-1] == new[:-1] \
                    and decode_name(smp['name']).endswith('L'):
                hl, hr = bytearray(smp['header']), bytearray(self.samples[entry]['header'])
                hl[136:138], hr[136:138] = self._entry_address(entry), self._entry_address(i)
                smp['header'], self.samples[entry]['header'] = bytes(hl), bytes(hr)
                return

    def _roster(self, op, names):
        n = len(names)
        return bytes([0xF0, 0x47, ADDR, op, 0x48, n & 127, n >> 7]) + b''.join(names) + b'\xF7'

    def _sysex(self, f):
        if f[:2] != b'\xF0\x47' or f[-1] != 0xF7 or f[4] != 0x48:
            raise SendError('mock: not an Akai SysEx')
        op = f[3]
        if op == 2:
            self.out += self._roster(3, self.programs)
        elif op == 4:
            self.out += self._roster(5, [s['name'] for s in self.live()])
        elif op == 0:
            used = sum(words_used(len(s['pcm']) // 2) for s in self.live())
            self.out += (bytes([0xF0, 0x47, ADDR, 1, 0x48, 0, 0]) + self._v7(256, 2) + self._v7(256 - len(self.live()), 2)
                         + self._v7(max(2_000_000, self.free_words), 4) + self._v7(self.free_words - used, 4) + b'\0\xF7')
        elif op == 0x0B:
            if not self.bulk:
                raise SendError('mock: sample header outside bulk mode')
            ident = f[5] | (f[6] << 7)
            if ident != len(self.live()):
                raise SendError('mock: sample identifier is not the next free slot')
            header = unnibbles(f[7:-1])
            if len(header) != 192:
                raise SendError('mock: header length')
            rate = int.from_bytes(header[138:140], 'little')
            bandwidth, tune = rate_fields(rate) if valid_rate(rate) else (None, None)
            if bandwidth is None or header[1] != bandwidth or header[20:22] != encode_tune(tune):
                raise SendError('mock: rate / bandwidth / pitch offset fields inconsistent')
            h = bytearray(header)                          # what the real S2000 was seen to do (16 Nov 2026):
            if h[136:138] == b'\x00\x00':                  # it stores 'no stereo partner' as ffff
                h[136:138] = b'\xff\xff'
            h[22:26] = (0x10000 + 0x4000 * len(self.live())).to_bytes(4, 'little')     # internal start address
            header = bytes(h)
            self.pending, self.pcm_in = header, bytearray()
            self.out += bytes([0xF0, 0x7E, ADDR, 0x7C, 0, 0xF7])         # WAIT first, then ACK
            self.followup = bytes([0xF0, 0x7E, ADDR, 0x7F, 0, 0xF7])
        elif op == 0x34:
            if not self.pending:
                raise SendError('mock: post without a sample')
            if len(self.pcm_in) != int.from_bytes(self.pending[26:30], 'little') * 2:
                raise SendError('mock: PCM length differs from header')
            new = dict(name=bytes(self.pending[3:15]), header=self.pending, pcm=bytes(self.pcm_in))
            if None in self.samples:
                entry = self.samples.index(None)
                self.samples[entry] = new
            else:
                entry = len(self.samples)
                self.samples.append(new)
            self._autolink(entry)
            self.pending = self.pcm_in = None
            self.out += ACK
        elif op == 43:
            idx, off = f[5] | (f[6] << 7), f[8] | (f[9] << 7)
            part = self.live()[idx]['header'][off:off + 64]
            self.out += bytes([0xF0, 0x47, ADDR, 44, 0x48, f[5], f[6], 0, f[8], f[9], 64, 0]) + nibbles(part) + b'\xF7'
        elif op == 12:
            idx = f[5] | (f[6] << 7)
            size = sum(f[11 + i] << (7 * i) for i in range(4))
            if size * 2 != len(self.live()[idx]['pcm']):
                raise SendError('mock: PCM read size (count is in words)')
            self.pcm_out = self.live()[idx]['pcm']
        else:
            raise SendError(f'mock: unsupported SysEx opcode {op:#x}')


class MockBus:
    banner = 'simulated S2000'

    def __init__(self, s2000):
        self.s = s2000

    def close(self):
        pass

    def execute(self, cdb, direction, data, length):
        return self.s.execute(cdb, direction, data, length)


# --------------------------------------------------------------------------- real bus (helper)
class HelperBus:
    """Runs commands through the s2kexec helper on the PiSCSI board."""

    def __init__(self, helper, initiator, target, timeout=1):
        prefix = list(helper) if isinstance(helper, (list, tuple)) else [helper]
        self.proc = subprocess.Popen(prefix + ['--initiator', str(initiator), '--target', str(target),
                                               '--timeout', str(timeout)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self.buf = b''
        first = self._line(20)
        if not first.startswith('READY'):
            self.close()
            raise SendError(f'bus helper failed to start: {first}')
        self.banner = first
        self.dead = False

    def _line(self, timeout):
        end = time.monotonic() + timeout
        while b'\n' not in self.buf:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.proc.stdout], [], [], left)[0]:
                raise SendError('bus helper stopped answering')
            chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
            if not chunk:
                raise SendError('bus helper exited unexpectedly')
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b'\n')
        return line.decode('ascii', 'replace').strip()

    def execute(self, cdb, direction, data, length):
        if self.dead:
            raise SendError('bus already failed; not touching it again')
        d = {'none': 'N', 'in': 'I', 'out': 'O'}[direction]
        n = len(data) if direction == 'out' else (length if direction == 'in' else 0)
        self.proc.stdin.write(f'X {cdb.hex()} {d} {n} {data.hex() if data else "-"}\n'.encode())
        reply = self._line(30)
        parts = reply.split(' ')
        if parts[0] == 'E':
            self.dead = True
            raise SendError('SCSI bus error: ' + reply[2:])
        if parts[0] != 'R' or len(parts) != 4:
            self.dead = True
            raise SendError('bad helper reply: ' + reply[:80])
        return int(parts[1]), int(parts[2]), (b'' if parts[3] == '-' else bytes.fromhex(parts[3]))

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.stdin.write(b'Q\n')
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# --------------------------------------------------------------------------- audio
def _le_bytes(arr):
    if sys.byteorder == 'big':
        arr = array.array('h', arr)
        arr.byteswap()
    return arr.tobytes()


def source_rate(path):
    """Sample rate of the source file (wave module first, then soxi), None if it cannot be read."""
    try:
        with wave.open(path, 'rb') as w:
            return w.getframerate()
    except (wave.Error, EOFError):
        pass
    if shutil.which('soxi'):
        r = subprocess.run(['soxi', '-r', path], capture_output=True, text=True)
        try:
            return int(float(r.stdout.strip()))
        except ValueError:
            pass
    return None


def choose_rate(src, option='auto'):
    """auto: resample to one of the S2000's own rates (sources at or below 22.05 kHz to 22.05 kHz, half the memory
    and nothing lost; anything above to 44.1 kHz). native: keep the source rate (no resampling; the header retunes
    it). A number: resample to that rate."""
    if option == 'auto':
        return RATE if src is None or src > 22050 else 22050
    if option == 'native':
        if src is None:
            raise SendError('cannot read the source sample rate, so it cannot be kept: give --rate HZ instead')
        target = src
    else:
        try:
            target = int(option)
        except (TypeError, ValueError):
            raise SendError(f'unknown rate option {option!r}: use auto, native or a number of Hz') from None
    if not valid_rate(target):
        raise SendError(f'{target} Hz is outside the {MIN_RATE}-{MAX_RATE} Hz the S2000 header can hold')
    return target


def load_audio(path, mono_mix=False, rate='auto'):
    """Return ([PCM bytes per channel], converted?, rate). Each channel is raw 16-bit little-endian.
    A 1- or 2-channel 16-bit WAV already at the chosen rate is used as is; anything else is converted with
    sox, with dither disabled (-D) so that converting the same file twice gives identical audio.
    mono_mix averages L and R."""
    target = choose_rate(source_rate(path), rate) if os.path.isfile(path) else RATE

    def read(p):
        with wave.open(p, 'rb') as w:
            ch, sw, fr, ct = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype()
            if ch > 2:
                if mono_mix:
                    raise wave.Error('format')           # let sox mix it down
                raise SendError(f'{ch}-channel audio: use --mono-mix, or reduce it to stereo first')
            if (sw, fr, ct) != (2, target, 'NONE'):
                raise wave.Error('format')
            raw = w.readframes(w.getnframes())
        a = array.array('h')
        a.frombytes(raw)
        if sys.byteorder == 'big':
            a.byteswap()
        return [a[c::ch] for c in range(ch)]

    if not os.path.isfile(path):
        raise SendError(f'file not found: {path}')
    converted = False
    try:
        chans = read(path)
    except (wave.Error, EOFError):
        converted = True
        if not shutil.which('sox'):
            raise SendError(f'{path} is not a 16-bit WAV at {target} Hz and sox is not installed to convert it '
                            '(sudo apt install sox)')
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, 'c.wav')
            cmd = ['sox', '-D', path, '-b', '16', '-r', str(target)] + (['-c', '1'] if mono_mix else []) + [out]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise SendError('sox could not convert the file: ' + r.stderr.strip()[-200:])
            chans = read(out)
    if mono_mix and len(chans) == 2:
        chans = [array.array('h', [(l + r) >> 1 for l, r in zip(*chans)])]
    if not chans or len(chans[0]) < 1:
        raise SendError('no audio in file')
    return [_le_bytes(c) for c in chans], converted, target


Item = collections.namedtuple('Item', 'name pcm rate source converted root', defaults=(None,))   # root None = batch default


def sample_names(base, channels, used=()):
    """Akai names for one file: a 12-char name for mono. For stereo the S2000's own convention: the name
    (max 10 chars) padded to 10, then '-L' / '-R' as the LAST two of the 12 characters.
    Names already in `used` are avoided by replacing the end of the name with a number."""
    stem = akai_name(base)
    maxlen = 12 if channels == 1 else 10
    for n in range(1, 1000):
        suffix = '' if n == 1 else str(n)
        cand = (stem[:maxlen - len(suffix)].strip() or 'SAMPLE') + suffix
        names = [cand] if channels == 1 else [cand.ljust(10) + '-L', cand.ljust(10) + '-R']
        if not any(x in used for x in names):
            return names
    raise SendError(f'cannot find a free name for {base!r}')


def pad_pair_names(names):
    """The S2000 only links a -L/-R pair whose names are the stem padded to 10 characters then '-L' / '-R' (12 in all).
    Returns {old: new} for the names that are one half of a pair in `names` but not yet in that form."""
    fixed = {}
    for n in names:
        if n.endswith('-L') and len(n) < 12 and n[:-2] + '-R' in names:
            stem = n[:-2].rstrip()
            if stem and len(stem) <= 10:
                fixed[n], fixed[n[:-2] + '-R'] = stem.ljust(10) + '-L', stem.ljust(10) + '-R'
    return fixed


def plan_items(paths, mono_mix=False, rate='auto', name=None):
    """Load every file (failing early, before the bus is touched) and give every sample a unique Akai name."""
    if name and len(paths) > 1:
        raise SendError('--name only makes sense with a single file; names come from the file names in a batch')
    loaded = [(p,) + load_audio(p, mono_mix, rate) for p in paths]
    used, items = set(), []
    for p, chans, converted, r in loaded:
        for nm, ch in zip(sample_names(name or os.path.splitext(os.path.basename(p))[0], len(chans), used), chans):
            items.append(Item(nm, ch, r, p, converted))
            used.add(nm)
    return items


# --------------------------------------------------------------------------- piscsi management
def _run_cmd(cmd, timeout=30):
    """Run a command; returns (returncode, stdout+stderr). (127, '') if the program does not exist."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except FileNotFoundError:
        return 127, ''
    except subprocess.TimeoutExpired:
        return 124, 'timed out'


TYPE_CODES = ('SCHD', 'SCRM', 'SCMO', 'SCCD', 'SCBR', 'SCDP', 'SCHS', 'SCLP')


def parse_scsictl_list(text):
    """Parse `scsictl -l`: rows like '| 0 | 0 | SCHD | /home/pi/images/disk.hda'. Only the first four columns are used
    (ID, LUN, TYPE, image file / status); a missing image ('NO MEDIA') gives file None. Returns a list of dicts."""
    devices = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith('|'):
            continue
        parts = [p.strip() for p in s.strip('|').split('|')]
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rest = parts[3] if len(parts) > 3 else ''
        flags = ' '.join(re.findall(r'[\(\[]([^\)\]]*)[\)\]]', rest))
        file = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', rest).strip()
        if file.upper() in ('', '-', 'NO MEDIA', 'NO IMAGE', 'EMPTY'):
            file = None
        devices.append({'id': int(parts[0]), 'lun': int(parts[1]), 'type': parts[2].upper(), 'file': file,
                        'protected': 'PROTECT' in s.upper() or 'PROTECT' in flags.upper(), 'raw': s})
    return devices


class PiscsiManager:
    """Stops and restarts the piscsi service while keeping the current device setup.

    Before stopping, the attached devices are read with `scsictl -l` and saved (also to a file, so a server
    restart while PiSCSI is stopped does not lose them). After starting again, PiSCSI's own default config may
    re-attach some devices (the web UI loads it); the manager then attaches what is missing and, in mode 'exact',
    detaches what was not there before, so the setup is the same as before the stop.
    mode: 'exact' | 'attach-only' (never detach) | 'off' (just restart the services, as before).
    Only the columns `scsictl -l` shows are restored (ID, LUN, type, image file, write-protect); vendor/product
    names and non-default block sizes are not shown by it and cannot be restored.
    """

    def __init__(self, run=_run_cmd, log=lambda m: None, mode='exact', state_file=None, settle_s=12.0,
                 sleep=time.sleep):
        self.run, self.log, self.mode, self.state_file = run, log, mode, state_file
        self.settle_s, self.sleep = settle_s, sleep
        self.snap = None
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file) as f:
                    snap = json.load(f)
                if snap.get('pending'):
                    self.snap = snap
                    self.log(f'a PiSCSI snapshot from {time.ctime(snap["taken"])} is waiting to be restored')
            except (OSError, ValueError):
                pass

    # -- service state
    def state(self):
        return self.run(['systemctl', 'is-active', 'piscsi'])[1].strip() or 'unknown'

    def active(self):
        return self.state() == 'active'

    def list_devices(self):
        rc, out = self.run(['scsictl', '-l'])
        if rc != 0:
            return None
        return parse_scsictl_list(out)

    # -- snapshot
    def _save(self):
        if self.state_file and self.snap is not None:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.state_file)), exist_ok=True)
                with open(self.state_file, 'w') as f:
                    json.dump(self.snap, f, indent=1)
            except OSError:
                pass

    def snapshot(self):
        """Remember the current setup (only while PiSCSI is running). Keeps an earlier pending snapshot."""
        if self.mode == 'off' or (self.snap and self.snap.get('pending')):
            return
        devices = self.list_devices()
        if devices is None:
            self.log('could not read the PiSCSI device list (scsictl -l): the setup will not be restored by this server')
            self.snap = None
            return
        self.snap = {'taken': time.time(), 'pending': True, 'devices': devices}
        self._save()
        self.log(f'saved the PiSCSI setup: {len(devices)} device(s)'
                 + ''.join(f'\n    ID {d["id"]}:{d["lun"]} {d["type"]} {d["file"] or "(no media)"}' for d in devices))

    def info(self):
        if not self.snap:
            return None
        return {'taken': self.snap['taken'], 'pending': self.snap.get('pending', False),
                'devices': len(self.snap['devices'])}

    # -- stop / restore
    def stop(self):
        rc, out = self.run(['systemctl', 'stop', 'piscsi'])
        if rc != 0:
            raise SendError(f'could not stop piscsi: {out.strip()[:120] or "systemctl failed"}')
        for _ in range(10):
            if not self.active():
                return
            self.sleep(1)
        raise SendError('piscsi did not stop')

    def _wait_stable(self):
        """Poll the device list until it has stopped changing (the web UI loads its default config a moment after
        starting). Returns the last list, or None if scsictl never answered."""
        last, same, waited = None, 0, 0.0
        while waited <= self.settle_s:
            cur = self.list_devices()
            key = None if cur is None else [(d['id'], d['lun'], d['type'], d['file']) for d in cur]
            same = same + 1 if (cur is not None and key == last) else 0
            if same >= 2:
                return cur
            last = key
            self.sleep(1)
            waited += 1
        return self.list_devices()

    @staticmethod
    def _addr(d):
        return str(d['id']) if d['lun'] == 0 else f'{d["id"]}:{d["lun"]}'

    def restore(self):
        """Start piscsi (and the web UI) and put the saved setup back. Returns a result dict; never raises."""
        res = {'restored': False, 'attached': [], 'detached': [], 'failed': [], 'note': ''}
        self.run(['systemctl', 'start', 'piscsi'])
        self.sleep(3)
        if self.run(['systemctl', 'is-enabled', 'piscsi-web'])[1].strip() in ('enabled', 'static'):
            self.run(['systemctl', 'restart', 'piscsi-web'])
        self.log(f'PiSCSI is now: {self.state()}')
        if self.mode == 'off' or not self.snap or not self.snap.get('pending'):
            res['note'] = 'no saved setup to restore'
            return res
        have = self._wait_stable()
        if have is None:
            res['note'] = 'scsictl does not answer: the setup could not be checked'
            self.log('WARNING: ' + res['note'])
            return res
        want = {(d['id'], d['lun']): d for d in self.snap['devices']}
        cur = {(d['id'], d['lun']): d for d in have}
        key = lambda d: (d['type'], d['file'])
        for k, d in sorted(want.items()):
            if k in cur and key(cur[k]) == key(d):
                continue
            if k in cur:                                           # something else is attached there
                self._do(['scsictl', '-i', self._addr(d), '-c', 'detach'], res, 'detached', d)
            cmd = ['scsictl', '-i', self._addr(d), '-c', 'attach', '-t', d['type'].lower()] + (['-f', d['file']] if d['file'] else [])
            self._do(cmd, res, 'attached', d)
        for k, d in sorted(cur.items()):
            if k not in want and self.mode == 'exact':
                self._do(['scsictl', '-i', self._addr(d), '-c', 'detach'], res, 'detached', d)
        for k, d in sorted(want.items()):                          # write protection last, once attached
            if d['protected'] and not any(f['id'] == d['id'] and f['lun'] == d['lun'] and f['why'].startswith('attach') for f in res['failed']):
                if not (k in cur and key(cur[k]) == key(d) and cur[k]['protected']):
                    self._do(['scsictl', '-i', self._addr(d), '-c', 'protect'], res, None, d)
        final = self.list_devices() or []
        fin = {(d['id'], d['lun']): key(d) for d in final}
        missing = [k for k, d in want.items() if fin.get(k) != key(d)]
        extra = [k for k in fin if k not in want] if self.mode == 'exact' else []
        res['restored'] = not missing and not extra and not res['failed']
        res['note'] = ('the PiSCSI setup is back as it was' if res['restored'] else
                       f'WARNING: setup not fully restored (missing {missing}, unexpected {extra}, failed {len(res["failed"])})')
        self.log(res['note'])
        self.snap['pending'] = False
        self.snap['restored'] = time.time()
        self._save()
        return res

    def _do(self, cmd, res, bucket, d):
        verb = cmd[4]                                              # scsictl -i ADDR -c VERB ...
        rc, out = self.run(cmd)
        what = f'ID {d["id"]}:{d["lun"]} {d["type"]} {d["file"] or "(no media)"}'
        if rc == 0:
            if bucket:
                res[bucket].append(what)
            self.log(f'  {verb} {what}')
        else:
            res['failed'].append({'id': d['id'], 'lun': d['lun'], 'why': f'{verb}: {out.strip()[:120]}'})
            self.log(f'  FAILED {" ".join(cmd)}: {out.strip()[:120]}')


# --------------------------------------------------------------------------- operations (used by the server)
def sample_info(slot, name_raw, hdr):
    partner = int.from_bytes(hdr[136:138], 'little')
    return {'slot': slot, 'name': decode_name(name_raw),
            'frames': int.from_bytes(hdr[26:30], 'little'), 'rate': int.from_bytes(hdr[138:140], 'little'),
            'root': hdr[2], 'bandwidth': hdr[1], 'tune': decode_tune(hdr[20:22]), 'playback': hdr[19],
            'partner': None if partner == 0xFFFF else partner, 'header_hex': hdr.hex()}


def op_inventory(sess):
    inv = sess.inventory()
    return {'samples': [{'slot': i, 'name': decode_name(n)} for i, n in enumerate(inv.samples)],
            'programs': [decode_name(n) for n in inv.programs],
            'free_headers': inv.free_headers, 'free_words': inv.free_words,
            'total_headers': inv.total_headers, 'total_words': inv.total_words}


def op_sample(sess, slot):
    inv = sess.inventory()
    if not 0 <= slot < len(inv.samples):
        raise NoSuchSample(f'no sample in slot {slot}')
    return sample_info(slot, inv.samples[slot], sess.read_header(slot))


def op_audio(sess, slot):
    """Read a sample's header and audio. Returns (info, raw 16-bit little-endian PCM)."""
    info = op_sample(sess, slot)
    if info['frames'] < 1 or not valid_rate(info['rate']):
        raise SendError(f'slot {slot} does not hold a plain 16-bit sample with a rate the header can describe')
    pcm = sess.read_pcm(slot, info['frames'] * 2, 'download', info['name'])
    return info, pcm


def pair_layout(linked):
    """linked: [(slot_l, field_l, slot_r, field_r)]. If each field is the internal address of the partner's
    12-byte entry, the left entry is at field_r and the right entry at field_l. Reports the spacing inside each
    pair and the extra entries ('holes') between consecutive pairs that a plain 12 x slot rule would not predict."""
    rows = sorted((sl, fr, sr, fl) for sl, fl, sr, fr in linked)          # (slot_l, addr_l, slot_r, addr_r)
    spacing = {ar - al for _, al, _, ar in rows}
    holes, prev = [], None
    for sl, al, sr, ar in rows:
        if prev is not None and (sl - prev[2]) == 1:                       # consecutive pairs only
            extra = (al - prev[3]) // 12 - 1
            if extra:
                holes.append({'before_slot': sl, 'entries': extra})
        prev = (sl, al, sr, ar)
    return {'pairs': len(rows), 'spacing': spacing.pop() if len(spacing) == 1 else None,
            'spacing_consistent': len(spacing) <= 1, 'holes': holes,
            'extra_entries': sum(h['entries'] for h in holes),
            'first_address': rows[0][1] if rows else None, 'last_address': rows[-1][3] if rows else None}


def op_pairs(sess):
    """Find -L/-R pairs by name and decode their stereo-partner fields. Read-only."""
    inv = sess.inventory()
    names = [decode_name(n) for n in inv.samples]
    sides = {}
    for i, n in enumerate(names):
        if n[-2:] in ('-L', '-R'):
            sides.setdefault(n[:-1], {})[n[-1]] = i
    found, linked = [], []
    for d in sides.values():
        if 'L' in d and 'R' in d:
            l, r = d['L'], d['R']
            fl = int.from_bytes(sess.read_header(l)[136:138], 'little')
            fr = int.from_bytes(sess.read_header(r)[136:138], 'little')
            is_linked = 0xFFFF not in (fl, fr)
            found.append({'left': {'slot': l, 'name': names[l], 'partner': None if fl == 0xFFFF else fl},
                          'right': {'slot': r, 'name': names[r], 'partner': None if fr == 0xFFFF else fr},
                          'linked': is_linked})
            if is_linked:
                linked.append((l, fl, r, fr))
    rule = layout = None
    if linked:
        stride, base, ok, notes = partner_map(linked)
        if stride is not None:
            rule = {'stride': stride, 'base': base, 'consistent': ok, 'notes': notes}
        layout = pair_layout(linked)
    return {'samples': len(names), 'pairs': found, 'rule': rule, 'layout': layout}


def op_probe(bus):
    st, n, data = bus.execute(bytes([0x12, 0, 0, 0, 36, 0]), 'in', b'', 36)
    return {'status': st, 'bytes': n, 'vendor': data[8:16].decode('ascii', 'replace').strip(),
            'product': data[16:32].decode('ascii', 'replace').strip()}


def save_wav(path, pcm, rate):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def wav_bytes(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _update_slots(done, samples):
    for d in done:
        d['slot'] = samples.index(encode_name(d['name']))


def op_send(sess, items, root, verify=True, progress=lambda m: None, done=None, sent_dir=None):
    """Send a batch. `done` (a list the caller owns) receives {'name', 'slot'} as each sample completes,
    so a failure part-way still tells the caller what is already in the S2000's RAM.

    Programs and free space are read once before and once after the whole batch. After each sample only the
    sample list is read, to find its slot: the S2000 fills a free header entry first, so a new sample is not always
    at the end, and it moves the samples after it up one. Slots in `done` are kept current. When a '-L' sample is followed by the matching '-R' the
    S2000 links the two as a stereo pair by itself; its partner fields are read back to confirm."""
    done = [] if done is None else done
    total = sum(len(i.pcm) for i in items)
    if sent_dir:
        os.makedirs(sent_dir, exist_ok=True)
        for it in items:                         # exactly the audio that will be sent
            save_wav(os.path.join(sent_dir, safe_stem(it.name) + '.wav'), it.pcm, it.rate)
    t0 = time.monotonic()
    first = sess.preflight([i.name for i in items], [len(i.pcm) // 2 for i in items])
    pairs = []
    for n, it in enumerate(items, 1):
        progress(f'--- [{n}/{len(items)}] {it.name!r} ({it.rate} Hz)')
        idx = sess.send_sample(it.name, root if it.root is None else it.root, it.pcm, verify,
                               progress=progress, rate=it.rate, refresh_inventory=False)
        done.append({'name': it.name, 'slot': idx})
        _update_slots(done, sess.inventory_after.samples)        # a sample filling a free entry moves later ones up
        progress(f'DONE: {it.name!r} is in sample slot {idx}.')
        prev = items[n - 2] if n >= 2 else None
        if prev is not None and it.name.endswith('-R') and prev.name.endswith('-L') and it.name[:-1] == prev.name[:-1]:
            fr = int.from_bytes(sess.last_header[136:138], 'little')
            fl = int.from_bytes(sess.read_header(done[-2]['slot'])[136:138], 'little')
            # Both were just sent with no partner, so any value now set came from the S2000 linking this pair.
            # Adjacency (a gap of 12) is not required: the table can have holes, as op_pairs reports.
            linked = 0xFFFF not in (fl, fr) and fl != fr
            pairs.append({'left': prev.name, 'right': it.name, 'left_slot': done[-2]['slot'], 'right_slot': idx,
                          'linked': linked, 'left_partner': fl, 'right_partner': fr})
            progress(f'stereo pair {it.name[:-2].strip()!r}: '
                     + ('linked by the S2000 (partner fields 0x%04x / 0x%04x)' % (fl, fr) if linked else
                        'NOT linked (partner fields 0x%04x / 0x%04x)' % (fl, fr)))
    after = sess.inventory()                    # one check of the whole batch
    want = {encode_name(i.name) for i in items}
    if after.programs != first.programs or [s for s in after.samples if s not in want] != first.samples \
            or not want <= set(after.samples):
        raise ListMismatch('the sample list after the batch is not what was expected (programs changed, or the '
                          'new samples are not exactly the ones sent)')
    _update_slots(done, after.samples)
    for p in pairs:
        p['left_slot'], p['right_slot'] = (after.samples.index(encode_name(p[k])) for k in ('left', 'right'))
    st = sess.stats
    elapsed = time.monotonic() - t0
    return {'samples': done, 'pairs': pairs, 'commands': sess.commands, 'audio_bytes': total,
            'elapsed_s': round(elapsed, 2), 'overall_kbps': round(total / max(elapsed, 1e-9) / 1000, 1),
            'write_kbps': round(st['w_bytes'] / st['w_s'] / 1000, 1) if st['w_s'] else None,
            'verify_kbps': round(st['r_bytes'] / st['r_s'] / 1000, 1) if st['r_s'] else None,
            'block': sess.block}


# --------------------------------------------------------------------------- bus ownership (used by the server)
class Busy(Exception):
    """Another operation owns the bus."""


class NeedsAttention(Exception):
    """A failed send left the bus/S2000 state uncertain: power-cycle, then recover."""


def default_helper():
    """The s2kexec bus helper: next to this file in a source checkout, otherwise on PATH (`make install`)."""
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), 's2kexec')
    if os.path.exists(local):
        return local
    return shutil.which('s2kexec') or '/usr/local/bin/s2kexec'


class BusManager:
    """Owns the SCSI bus: one operation at a time, PiSCSI stopped while an operation runs.

    A failure after something was written leaves PiSCSI stopped and the manager in 'needs_attention'
    until recover() is called (after power-cycling the S2000 and the Pi, as the README explains).
    keep_bus: seconds to leave PiSCSI stopped after an operation, so a GUI making several requests in a row
    does not stop/restart PiSCSI every time. mock: a MockS2000 to use instead of hardware (development).
    """

    def __init__(self, target=6, initiator=0, helper=None, manage_piscsi=True, keep_bus=20.0, mock=None,
                 require_root=True, timeout=1, log=lambda m: None, restore_mode='exact', state_file=None, piscsi=None):
        self.target, self.initiator, self.mock = target, initiator, mock
        self.pm = piscsi or PiscsiManager(log=log, mode=restore_mode, state_file=state_file)
        self.last_restore = None
        self.helper = helper or default_helper()
        self.manage_piscsi, self.keep_bus = manage_piscsi, keep_bus
        self.require_root, self.timeout, self.log = require_root, timeout, log
        self.lock = threading.Lock()
        self.state = 'idle'                  # idle | busy | needs_attention
        self._timer = None
        self._tlock = threading.Lock()
        self._restart_at = None              # time.monotonic() when the keep-bus timer will restart PiSCSI
        self._restoring = False              # the keep-bus timer is restarting PiSCSI (a few seconds)

    # -- status
    def piscsi_state(self):
        if self.mock is not None or not self.manage_piscsi:
            return 'unmanaged'
        return self.pm.state()

    def restore_state(self):
        """'restoring' (PiSCSI is being restarted now), 'scheduled' (kept stopped, restart due in restart_in_s) or None."""
        if self._restoring:
            return {'state': 'restoring', 'restart_in_s': 0}
        at = self._restart_at
        if at is not None and self.state == 'idle':
            return {'state': 'scheduled', 'restart_in_s': max(0, round(at - time.monotonic()))}
        return None

    def status(self):
        return {'state': self.state, 'piscsi': self.piscsi_state(), 'piscsi_restore': self.restore_state(), 'target': self.target,
                'initiator': self.initiator, 'mock': self.mock is not None, 'keep_bus_s': self.keep_bus,
                'piscsi_snapshot': self.pm.info(), 'last_restore': self.last_restore}

    def _restore(self):
        self.last_restore = self.pm.restore()
        return self.last_restore

    # -- PiSCSI restart timer
    def _cancel_timer(self):
        with self._tlock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._restart_at = None

    def _timed_restart(self):
        if not self.lock.acquire(blocking=False):
            return                           # a real operation owns the bus; it schedules its own restart
        self._restart_at = None
        self._restoring = True               # set only while holding the lock, so readers never wait on a real job
        try:
            if self.state == 'idle':
                self._restore()
        finally:
            self._restoring = False
            self.lock.release()

    def _schedule_restart(self):
        if self.mock is not None or not self.manage_piscsi:
            return
        if self.keep_bus > 0:
            with self._tlock:
                self._restart_at = time.monotonic() + self.keep_bus
                self._timer = threading.Timer(self.keep_bus, self._timed_restart)
                self._timer.daemon = True
                self._timer.start()
        else:
            self._restore()

    # -- opening the bus
    def _open(self):
        if self.mock is not None:
            return MockBus(self.mock)
        if self.require_root and os.geteuid() != 0:
            raise SendError('the server must run as root (sudo) to use the GPIO bus')
        if not os.path.exists(self.helper if not isinstance(self.helper, (list, tuple)) else self.helper[-1]):
            raise SendError(f'bus helper {self.helper} not found: run `make` (and `sudo make install` for an installed server)')
        if self.manage_piscsi:
            if self.pm.active():
                self.pm.snapshot()                        # remember the attached devices first
                self.log('stopping piscsi (emulated disks disappear until it is restarted)')
                self.pm.stop()
        elif self.pm.active():
            raise SendError('the piscsi service is running and owns the bus: stop it, or run the server with '
                            'PiSCSI management enabled')
        return HelperBus(self.helper, self.initiator, self.target, self.timeout)

    @contextlib.contextmanager
    def session(self, logline=lambda m: None, budget_s=600.0):
        """Exclusive access to the S2000. Raises Busy / NeedsAttention / SendError (before touching anything)."""
        if self.state == 'needs_attention':
            raise NeedsAttention('a previous send left the bus in an uncertain state: power-cycle the S2000 '
                                 'and the Pi, then POST /v1/recover')
        if not self.lock.acquire(timeout=0.2):
            # A short PiSCSI restart by the keep-bus timer is not a real operation: wait for it. A running
            # operation (a send job can take minutes) is reported as busy at once. (The short first wait covers
            # the moment between the timer taking the lock and setting _restoring.)
            if not (self._restoring and self.lock.acquire(timeout=60)):
                raise Busy('another operation is using the bus')
        self.state = 'busy'
        bus = sess = None
        outcome = 'ok'
        try:
            self._cancel_timer()
            bus = self._open()
            sess = Session(bus, logline, budget_s=budget_s)
            yield sess
        except VerifyError:
            outcome = 'quiet'                 # the bus is idle and healthy; only a comparison failed
            raise
        except BaseException:
            outcome = 'uncertain' if (sess is not None and sess.mutated) else 'untouched'
            raise
        finally:
            try:
                if bus is not None:
                    bus.close()
                if outcome == 'uncertain':
                    self.state = 'needs_attention'
                    self.log('PiSCSI deliberately left stopped (bus state uncertain)')
                else:
                    self.state = 'idle'
                    self._schedule_restart()
            finally:
                self.lock.release()

    def recover(self):
        """After power-cycling: restart PiSCSI and accept operations again."""
        if not self.lock.acquire(blocking=False):
            raise Busy('another operation is using the bus')
        try:
            if self.mock is None and self.manage_piscsi:
                self._restore()
            self.state = 'idle'
        finally:
            self.lock.release()

    def close(self):
        """Server shutdown: if PiSCSI is waiting on the keep-bus timer, restart it now."""
        pending = False
        with self._tlock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
                self._restart_at = None
                pending = True
        if pending and self.state == 'idle':
            self._restore()


def rehearse(items, root, verify, say, block=PCM_BLOCK):
    words = sum(len(i.pcm) // 2 for i in items)
    taken = {i.name for i in items}
    seed = next(n for n in ['EXISTING'] + [f'EXISTING{k}' for k in range(2, 1000)] if n not in taken)
    m = MockS2000(samples=[seed], free_words=max(1_000_000, words + 1000))
    sess = Session(MockBus(m), lambda x: None, budget_s=600)
    sess.block = block
    sess.dump_dir = tempfile.gettempdir()
    sess.preflight([i.name for i in items], [len(i.pcm) // 2 for i in items])
    for it in items:
        idx = sess.send_sample(it.name, root if it.root is None else it.root, it.pcm, verify, rate=it.rate)
        if m.live()[idx]['pcm'] != it.pcm:
            raise SendError('rehearsal: audio mismatch')
    say(f'rehearsal OK ({sess.commands} commands, run on a simulated S2000 before touching the real one)')
    return sess.commands


def describe(items, say):
    for it in items:
        frames = len(it.pcm) // 2
        say(f'  {it.name:12s} {frames:>9d} frames {frames / it.rate:7.2f} s  {it.rate} Hz  {len(it.pcm) / 1000:8.0f} KB'
            f'   <- {os.path.basename(it.source)}' + ('  (converted by sox)' if it.converted else ''))


