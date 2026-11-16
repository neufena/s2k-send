#!/usr/bin/env python3
"""s2ksend - send one WAV file to an Akai S2000 over SCSI, from a Raspberry Pi
with a PiSCSI board acting as the SCSI *initiator*.

The S2000's SCSI port can tunnel MIDI SysEx (this is how MESA / ReCycle talk to
it). This script speaks that tunnel:

    09 mode switch | 0C send | 0D status | 0E receive     (6-byte vendor CDBs)

and uses the standard Akai S1000/S3000 SysEx to create one sample in RAM.
The protocol sequence is taken from a hardware-proven implementation
(S2000MesaOfflineUpload.h / NativeSend.h); see README.md.

Every run first REHEARSES the whole transfer against a built-in simulated
S2000 and refuses to go on if the rehearsal fails. The bus work itself is done
by the small C++ helper s2kexec (build it with `make`).

    sudo ./s2ksend.py kick.wav --target 6 --initiator 7 --manage-piscsi
    ./s2ksend.py kick.wav --rehearse-only        # no hardware, any machine
    ./s2ksend.py --selftest
"""
import argparse
import array
import collections
import datetime
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import wave

AKAI_CHARS = '0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ#+-.'
ADDR = 0                    # SysEx device address used by MESA/the proven code
PCM_BLOCK = 16384           # bytes per bulk write block (proven size)
MAX_STATUS_POLLS = 128
RATE = 44100                # default / highest rate
RATES = (44100, 22050)      # the two rates the S2000 supports; 16-bit mono per sample

# Captured MESA wire template for a sample header: type 3, 44.1 kHz, mono, no loop,
# play-to-end. Only name, root note, length, start/end and rate are patched.
SAMPLE_TEMPLATE = bytes.fromhex(
    "03013c1a1c190c0f0a0a0a0a0a0a0a8000000003000000000000000100000000"
    "0000ff0000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000044ac0000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000")
assert len(SAMPLE_TEMPLATE) == 192

ACK = bytes([0xF0, 0x47, ADDR, 0x16, 0x48, 0x00, 0xF7])


class SendError(Exception):
    pass


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
    if not (1 <= frames <= 0xFFFFFFFF and 0 <= root < 128) or rate not in RATES:
        raise SendError('bad frames/root/rate')
    h = bytearray(SAMPLE_TEMPLATE)
    h[1] = 1 if rate == 44100 else 0      # bandwidth: 1 = 20 kHz, 0 = 10 kHz (Ohsaki's S3000 format notes)
    h[2] = root
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
    return v(9, 2), v(15, 4)         # free headers, free sample words


class Inventory:
    def __init__(self, programs, samples, free_headers, free_words):
        self.programs, self.samples = programs, samples
        self.free_headers, self.free_words = free_headers, free_words


# --------------------------------------------------------------------------- header comparison
# Field names follow Ohsaki's S3000 format notes; unnamed bytes are shown as 'other'.
FIELDS = [(0, 1, 'type', True), (1, 2, 'bandwidth', False), (2, 3, 'root note', False),
          (3, 15, 'name', True), (15, 19, 'flags/loop bytes', False), (19, 20, 'playback type', False),
          (20, 22, 'pitch offset', False), (22, 26, 'start address (internal)', False),
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
        self.deadline = time.monotonic() + budget_s
        self.commands = 0
        self.mutated = False         # a write command has been dispatched
        self.block = PCM_BLOCK       # bytes per raw audio write
        self.dump_dir = '.'          # where mismatch dumps go
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
        status, transferred, got = self.bus.execute(cdb, direction, data, length)
        shown = got or data
        raw_pcm = cdb[0] in (12, 14) and cdb[5] == 0x80        # keep logs small: audio only as a prefix
        if raw_pcm and len(shown) > 20:
            shown_hex = shown[:20].hex() + f'...({len(shown)} bytes)'
        else:
            shown_hex = shown.hex()
        self.log(f'{self.commands:5d} cdb={cdb.hex()} {direction:4s} want={want} '
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
        fh, fw = parse_stat(self.control(STAT, 'data'))
        return Inventory(programs, samples, fh, fw)

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

    def read_pcm(self, index, nbytes):
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
        self.stats['r_s'] += time.monotonic() - t0
        self.stats['r_bytes'] += nbytes
        self.mode(False)
        return bytes(got)

    def verify_audio(self, index, pcm, label):
        got = self.read_pcm(index, len(pcm))
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
        if inv.free_words < sum(frame_counts):
            raise SendError(f'needs {sum(frame_counts)} words but the S2000 has only {inv.free_words} free')
        self.inventory_after = inv        # the first sample reuses this instead of reading the lists again
        return inv

    # ---- the transfer
    def send_sample(self, name, root, pcm, verify_pcm=True, progress=lambda m: None, rate=RATE):
        frames = len(pcm) // 2
        before = self.inventory_after if self.inventory_after is not None else self.inventory()
        enc = encode_name(name)
        if enc in before.samples:
            raise SendError(f'a sample named {name!r} already exists on the S2000; choose another --name')
        if len(before.samples) >= 4096 or before.free_headers < 1:
            raise SendError('no free sample header on the S2000')
        if before.free_words < frames:
            raise SendError(f'sample needs {frames} words but the S2000 has only {before.free_words} free')
        progress(f'S2000 has {len(before.samples)} samples, {before.free_words} free words')
        header = sample_header(name, root, frames, rate)
        progress('sending sample header')
        self.control(sysex_sample(header, len(before.samples)), 'sample', bulk=True, keep=True)
        blk_n = self.block
        for at in range(0, len(pcm), blk_n):
            blk = pcm[at:at + blk_n]
            t0 = time.monotonic()
            self.cmd(transfer_cdb(12, len(blk), raw=True), 'out', blk)
            self.stats['w_s'] += time.monotonic() - t0
            self.stats['w_bytes'] += len(blk)
            if (at // blk_n) % 16 == 0 or at + blk_n >= len(pcm):
                progress(f'  PCM {min(at + blk_n, len(pcm))}/{len(pcm)} bytes')
        self.mode(False)
        progress('finalising')
        self.control(post_object(), 'ack')
        after = self.inventory()
        self.inventory_after = after
        if after.programs != before.programs or enc not in after.samples \
                or sorted(after.samples) != sorted(before.samples + [enc]):
            raise SendError('inventory after the transfer is not what was expected')
        index = after.samples.index(enc)
        got = self.read_header(index)
        diffs = header_diffs(header, got)
        for a, b, field, sv, gv, critical in diffs:
            progress(f'  header note: bytes {a}-{b} ({field}) sent {sv[:32]} / S2000 reports {gv[:32]}'
                     + ('' if critical else '  [normal: the S2000 fills this in itself]'
                        if field in ('start address (internal)', 'stereo partner') else '  [not critical]'))
        if any(d[5] for d in diffs):
            raise VerifyError('header read back from the S2000 differs in a field that matters '
                              '(name, length or rate) - see the notes above and the transcript')
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
        self.samples = [dict(name=encode_name(n), header=bytes(SAMPLE_TEMPLATE), pcm=b'') for n in samples]
        self.programs = []
        self.free_words = free_words
        self.scsi = False
        self.bulk = False
        self.out = bytearray()
        self.followup = None
        self.pending = None
        self.pcm_in = None
        self.pcm_out = None

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
            self.out += self._roster(5, [s['name'] for s in self.samples])
        elif op == 0:
            used = sum(len(s['pcm']) // 2 for s in self.samples)
            self.out += (bytes([0xF0, 0x47, ADDR, 1, 0x48, 0, 0]) + self._v7(256, 2) + self._v7(256 - len(self.samples), 2)
                         + self._v7(max(2_000_000, self.free_words), 4) + self._v7(self.free_words - used, 4) + b'\0\xF7')
        elif op == 0x0B:
            if not self.bulk:
                raise SendError('mock: sample header outside bulk mode')
            ident = f[5] | (f[6] << 7)
            if ident != len(self.samples):
                raise SendError('mock: sample identifier is not the next free slot')
            header = unnibbles(f[7:-1])
            if len(header) != 192:
                raise SendError('mock: header length')
            rate = int.from_bytes(header[138:140], 'little')
            if rate not in RATES or header[1] != (1 if rate == 44100 else 0):
                raise SendError('mock: rate / bandwidth fields inconsistent')
            self.pending, self.pcm_in = header, bytearray()
            self.out += bytes([0xF0, 0x7E, ADDR, 0x7C, 0, 0xF7])         # WAIT first, then ACK
            self.followup = bytes([0xF0, 0x7E, ADDR, 0x7F, 0, 0xF7])
        elif op == 0x34:
            if not self.pending:
                raise SendError('mock: post without a sample')
            if len(self.pcm_in) != int.from_bytes(self.pending[26:30], 'little') * 2:
                raise SendError('mock: PCM length differs from header')
            self.samples.append(dict(name=bytes(self.pending[3:15]), header=self.pending, pcm=bytes(self.pcm_in)))
            self.pending = self.pcm_in = None
            self.out += ACK
        elif op == 43:
            idx, off = f[5] | (f[6] << 7), f[8] | (f[9] << 7)
            part = self.samples[idx]['header'][off:off + 64]
            self.out += bytes([0xF0, 0x47, ADDR, 44, 0x48, f[5], f[6], 0, f[8], f[9], 64, 0]) + nibbles(part) + b'\xF7'
        elif op == 12:
            idx = f[5] | (f[6] << 7)
            size = sum(f[11 + i] << (7 * i) for i in range(4))
            if size * 2 != len(self.samples[idx]['pcm']):
                raise SendError('mock: PCM read size (count is in words)')
            self.pcm_out = self.samples[idx]['pcm']
        else:
            raise SendError(f'mock: unsupported SysEx opcode {op:#x}')


class MockBus:
    def __init__(self, s2000):
        self.s = s2000

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
    """Sample rate of the source file (wave module first, then soxi), default RATE."""
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
    return RATE


def choose_rate(src, option='auto'):
    """The S2000 supports 44.1 and 22.05 kHz. auto: sources at or below 22.05 kHz are sent at 22.05 kHz
    (half the memory, nothing lost); anything above is sent at 44.1 kHz."""
    if option in ('44100', '22050', 44100, 22050):
        return int(option)
    return 22050 if src <= 22050 else 44100


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


Item = collections.namedtuple('Item', 'name pcm rate source converted')


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
def systemctl(*a):
    return subprocess.run(['systemctl', *a], capture_output=True, text=True).stdout.strip()


def piscsi_active():
    return systemctl('is-active', 'piscsi') == 'active'


def restart_piscsi(say):
    subprocess.run(['systemctl', 'start', 'piscsi'])
    time.sleep(3)
    if systemctl('is-enabled', 'piscsi-web') in ('enabled', 'static'):
        subprocess.run(['systemctl', 'restart', 'piscsi-web'])
    say(f'PiSCSI is now: {systemctl("is-active", "piscsi")}')


# --------------------------------------------------------------------------- main
def rehearse(items, root, verify, say, block=PCM_BLOCK):
    words = sum(len(i.pcm) // 2 for i in items)
    m = MockS2000(samples=['EXISTING'], free_words=max(1_000_000, words + 1000))
    sess = Session(MockBus(m), lambda x: None, budget_s=600)
    sess.block = block
    sess.dump_dir = tempfile.gettempdir()
    sess.preflight([i.name for i in items], [len(i.pcm) // 2 for i in items])
    for it in items:
        idx = sess.send_sample(it.name, root, it.pcm, verify, rate=it.rate)
        if m.samples[idx]['pcm'] != it.pcm or idx != len(m.samples) - 1:
            raise SendError('rehearsal: audio mismatch')
    say(f'rehearsal OK ({sess.commands} commands against the simulated S2000)')
    return sess.commands


def describe(items, say):
    for it in items:
        frames = len(it.pcm) // 2
        say(f'  {it.name:12s} {frames:>9d} frames {frames / it.rate:7.2f} s  {it.rate} Hz  {len(it.pcm) / 1000:8.0f} KB'
            f'   <- {os.path.basename(it.source)}' + ('  (converted by sox)' if it.converted else ''))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Send WAV files to an Akai S2000 via PiSCSI (SCSI initiator).')
    ap.add_argument('wavs', nargs='*', metavar='WAV', help='one or more WAV files')
    ap.add_argument('--name', help='sample name on the S2000 (single file only; default: from the file name)')
    ap.add_argument('--root', type=int, default=60, help='root note, MIDI number (default 60)')
    ap.add_argument('--rate', choices=['auto', '44100', '22050'], default='auto',
                    help='sample rate on the S2000. auto: 22050 for sources at or below 22.05 kHz, else 44100')
    ap.add_argument('--target', type=int, default=6, help='S2000 SCSI ID (default 6)')
    ap.add_argument('--initiator', type=int, default=7, help="the Pi's SCSI ID (default 7; the Mac must be off/disconnected)")
    ap.add_argument('--helper', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 's2kexec'))
    ap.add_argument('--manage-piscsi', action='store_true', help='stop the piscsi service before, restart it after')
    ap.add_argument('--no-verify-pcm', action='store_true', help='skip reading the audio back (faster)')
    ap.add_argument('--block', type=int, choices=[8192, 16384, 32768], default=PCM_BLOCK,
                    help='bytes per audio write (default 16384, the size proven on hardware; MESA uses up to 32768)')
    ap.add_argument('--mono-mix', action='store_true',
                    help='stereo files: mix to ONE mono sample (default: two samples NAME-L and NAME-R)')
    ap.add_argument('--sent-dir', default='s2k-sent', help='where the exact audio sent is saved (default s2k-sent)')
    ap.add_argument('--rehearse-only', action='store_true', help='only run the simulated rehearsal')
    ap.add_argument('--probe', action='store_true', help='just INQUIRY the target and exit')
    ap.add_argument('--check', type=int, metavar='SLOT',
                    help='READ-ONLY: show the header of the sample in SLOT (0 = first); with a WAV, also compare the audio')
    ap.add_argument('--pairs', action='store_true',
                    help='READ-ONLY: find -L/-R stereo pairs on the S2000 and decode their partner fields')
    ap.add_argument('--yes', action='store_true', help='do not ask for the SEND confirmation')
    ap.add_argument('--log', help='transcript file (default s2ksend-TIMESTAMP.log)')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args(argv)
    say = lambda m: print(m, flush=True)

    if a.selftest:
        import random
        random.seed(1)
        rnd = lambda frames: bytes(random.randrange(256) for _ in range(frames * 2))
        for frames in (1, 8191, 8192, 8193, 20000):
            for rate in RATES:
                one = [Item('SELFTEST', rnd(frames), rate, 'x.wav', False)]
                rehearse(one, 60, True, lambda m: None)
                two = [Item(n, rnd(frames), rate, 'y.wav', False) for n in sample_names('SELFTEST', 2)]
                rehearse(one[:0] + two + [Item('OTHER', rnd(frames), rate, 'z.wav', False)], 60, True, lambda m: None,
                         block=32768)
        say('selftest OK')
        return 0
    if not a.probe and not a.pairs and a.check is None and not a.wavs:
        ap.error('give at least one WAV file')
    if a.check is not None and len(a.wavs) > 1:
        ap.error('--check takes at most one WAV file')
    if not 0 <= a.root < 128 or a.target == a.initiator or not 0 <= a.target < 8 or not 0 <= a.initiator < 8:
        ap.error('bad --root/--target/--initiator')

    try:
        if a.probe:
            return probe(a, say)
        if a.pairs:
            return pairs_report(a, say)
        if a.check is not None:
            return check(a, say)
        items = plan_items(a.wavs, a.mono_mix, a.rate, a.name)
        say(f'{len(a.wavs)} file(s) -> {len(items)} sample(s), root {a.root}:')
        describe(items, say)
        if any(items[i].source == items[i + 1].source for i in range(len(items) - 1)):
            say('(stereo files become two separate mono samples, NOT linked as a pair)')
        rehearse(items, a.root, not a.no_verify_pcm, say, a.block)
        if a.rehearse_only:
            return 0
        return physical(a, items, say)
    except SendError as e:
        say(f'STOP: {e}')
        return 1


def open_bus(a, say):
    if os.geteuid() != 0:
        raise SendError('run as root (sudo) to access the GPIO bus')
    if not os.path.exists(a.helper):
        raise SendError(f'{a.helper} not found: run `make` first')
    if piscsi_active():
        if not a.manage_piscsi:
            raise SendError('the piscsi service is running and owns the bus. Stop it '
                            '(sudo systemctl stop piscsi) or pass --manage-piscsi')
        say('stopping piscsi (your emulated disks disappear until it is restarted)')
        subprocess.run(['systemctl', 'stop', 'piscsi'], check=True)
        for _ in range(10):
            if not piscsi_active():
                break
            time.sleep(1)
        if piscsi_active():
            raise SendError('piscsi did not stop')
    return HelperBus(a.helper, a.initiator, a.target)


def probe(a, say):
    bus = open_bus(a, say)
    try:
        say(bus.banner)
        st, n, data = bus.execute(bytes([0x12, 0, 0, 0, 36, 0]), 'in', b'', 36)
        say(f'INQUIRY status {st}, {n} bytes: vendor {data[8:16].decode("ascii", "replace")!r} '
            f'product {data[16:32].decode("ascii", "replace")!r}')
        return 0
    finally:
        bus.close()
        if a.manage_piscsi:
            restart_piscsi(say)


def pairs_report(a, say):
    """Read-only: find -L/-R pairs on the S2000, read their stereo-partner fields, and see whether the values
    follow a simple address rule. Nothing is written."""
    logname = a.log or datetime.datetime.now().strftime('s2ksend-pairs-%Y%m%d-%H%M%S.log')
    logf = open(logname, 'w', buffering=1)
    bus = open_bus(a, say)
    sess = Session(bus, lambda m: logf.write(m + '\n'), budget_s=600)
    try:
        say(bus.banner)
        inv = sess.inventory()
        names = [decode_name(n) for n in inv.samples]
        say(f'{len(names)} samples on the S2000')
        sides = {}
        for i, n in enumerate(names):
            if n[-2:] in ('-L', '-R'):
                sides.setdefault(n[:-1], {})[n[-1]] = i
        found = [(d['L'], d['R']) for d in sides.values() if 'L' in d and 'R' in d]
        if not found:
            say('no -L/-R pairs found (names ending in -L and -R with the same start)')
            return 0
        linked = []
        for l, r in found:
            fl = int.from_bytes(sess.read_header(l)[136:138], 'little')
            fr = int.from_bytes(sess.read_header(r)[136:138], 'little')
            say(f'  {names[l]!r} slot {l}: partner field 0x{fl:04x}   |   {names[r]!r} slot {r}: partner field 0x{fr:04x}')
            if 0xFFFF in (fl, fr):
                say('    not linked (0xffff = no partner)')
            else:
                linked.append((l, fl, r, fr))
        if linked:
            stride, base, ok, notes = partner_map(linked)
            for n in notes:
                say('  note: ' + n)
            if stride is not None:
                say(f'  rule that fits {"all" if ok else "the first"} linked pair(s): field = 0x{base:04x} + {stride} x slot of the partner')
        say(f'Transcript: {logname}')
        return 0
    finally:
        bus.close()
        if a.manage_piscsi:
            restart_piscsi(say)


def check(a, say):
    """Read-only: uses only the inventory, header-read and audio-read requests that a normal send uses
    for verification. Nothing is written to the S2000."""
    logname = a.log or datetime.datetime.now().strftime('s2ksend-check-%Y%m%d-%H%M%S.log')
    logf = open(logname, 'w', buffering=1)
    want, converted, want_rate = None, False, None
    if a.wavs:                                        # fail early, before PiSCSI is stopped
        chans, converted, want_rate = load_audio(a.wavs[0])
        if len(chans) != 1:
            raise SendError('this file is stereo: compare each half separately using the NAME-L / NAME-R files '
                            'saved in s2k-sent, or pass a mono file')
        want = chans[0]
    bus = open_bus(a, say)
    sess = Session(bus, lambda m: logf.write(m + '\n'), budget_s=300)
    try:
        say(bus.banner)
        inv = sess.inventory()
        say(f'{len(inv.samples)} samples on the S2000, {inv.free_words} free words')
        if not 0 <= a.check < len(inv.samples):
            raise SendError(f'no sample in slot {a.check}')
        say(f'slot {a.check}: {decode_name(inv.samples[a.check])!r}')
        hdr = sess.read_header(a.check)
        for lo, hi, field, _crit in [(f[0], f[1], f[2], f[3]) for f in FIELDS]:
            say(f'  bytes {lo:3d}-{hi - 1:3d} {field:26s} {hdr[lo:hi][:24].hex()}{"..." if hi - lo > 24 else ""}')
        frames = int.from_bytes(hdr[26:30], 'little')
        hdr_rate = int.from_bytes(hdr[138:140], 'little')
        say(f'  -> {frames} frames, rate {hdr_rate} Hz, root {hdr[2]}')
        partner = int.from_bytes(hdr[136:138], 'little')
        say('  -> stereo partner: none' if partner == 0xFFFF else
            f'  -> stereo partner field 0x{partner:04x} (linked; see --pairs for what it points to)')
        if want is not None:
            if want_rate != hdr_rate:
                say(f'  note: the file is at {want_rate} Hz but the sample is at {hdr_rate} Hz')
            if converted:
                say('  note: the file needed conversion; compare against the exact audio saved in s2k-sent/ by the '
                    'send instead (conversion can differ from what was sent, e.g. in older versions with dither)')
            if len(want) != frames * 2:
                say(f'  audio NOT compared: file has {len(want) // 2} frames, the sample has {frames}')
            else:
                say('  reading audio back...')
                try:
                    sess.verify_audio(a.check, want, f'slot{a.check}')
                    say('  audio MATCHES the file byte-for-byte')
                except VerifyError as e:
                    say('  ' + str(e))
        say(f'Transcript: {logname}')
        return 0
    finally:
        bus.close()
        if a.manage_piscsi:
            restart_piscsi(say)


def speed_report(sess, t_start, items):
    st = sess.stats
    total = sum(len(i.pcm) for i in items)
    elapsed = time.monotonic() - t_start
    parts = [f'{len(items)} sample(s), {total / 1000:.0f} KB of audio in {elapsed:.1f} s overall '
             f'({total / max(elapsed, 1e-9) / 1000:.0f} KB/s including every check), {sess.commands} commands']
    if st['w_s'] > 0:
        parts.append(f'audio writes {st["w_bytes"] / st["w_s"] / 1000:.0f} KB/s (block {sess.block})')
    if st['r_s'] > 0:
        parts.append(f'verify reads {st["r_bytes"] / st["r_s"] / 1000:.0f} KB/s')
    return '; '.join(parts)


def physical(a, items, say):
    logname = a.log or datetime.datetime.now().strftime('s2ksend-%Y%m%d-%H%M%S.log')
    logf = open(logname, 'w', buffering=1)
    os.makedirs(a.sent_dir, exist_ok=True)
    for it in items:                                     # exactly the audio that will be sent, for later --check
        with wave.open(os.path.join(a.sent_dir, safe_stem(it.name) + '.wav'), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(it.rate)
            w.writeframes(it.pcm)
    say(f'(exact audio saved in {a.sent_dir}/ as NAME.wav; use it with --check later)')
    total = sum(len(i.pcm) for i in items)
    say(f'About to SEND {len(items)} sample(s), {total} bytes in all, to the S2000 at SCSI ID {a.target} '
        f'(the Pi will be ID {a.initiator}).')
    say('Make sure the Mac is off or disconnected, and that nothing on the S2000 needs saving: it will')
    say('gain sample(s) in RAM. Do not touch the S2000 until this finishes.')
    if not a.yes:
        try:
            typed = input('Type SEND to continue: ').strip()
        except EOFError:
            typed = ''
        if typed != 'SEND':
            say('Cancelled; nothing sent.')
            return 1
    bus = open_bus(a, say)
    sess = Session(bus, lambda m: logf.write(m + '\n'), budget_s=max(600.0, 120 + total / 50000))
    sess.block = a.block
    sess.dump_dir = a.sent_dir
    ok = False
    quiet = False                    # True when a read-back check failed but the bus is idle and healthy
    done = []

    def partial(failed=None):
        if done:
            say('Already in the S2000 RAM from this run: ' + ', '.join(f'{n!r} (slot {i})' for n, i in done))
        left = [i.name for i in items if i.name not in {n for n, _ in done}]
        if left:
            say('Not completed: ' + ', '.join(left) + (f'  (the one being sent when it stopped, {failed!r}, '
                                                    'may be partly there)' if failed else ''))

    t_start = time.monotonic()
    current = None
    try:
        say(bus.banner)
        sess.preflight([i.name for i in items], [len(i.pcm) // 2 for i in items])
        for n, it in enumerate(items, 1):
            current = it.name
            say(f'--- [{n}/{len(items)}] {it.name!r} ({it.rate} Hz)')
            idx = sess.send_sample(it.name, a.root, it.pcm, not a.no_verify_pcm, progress=say, rate=it.rate)
            done.append((it.name, idx))
            say(f'DONE: {it.name!r} is in sample slot {idx}.')
        say('All done. ' + speed_report(sess, t_start, items))
        say('Slots: ' + ', '.join(f'{n}={i}' for n, i in done) + f'.  Transcript: {logname}')
        ok = True
        return 0
    except VerifyError as e:
        quiet = True
        say(f'VERIFY FAILED: {e}')
        say('The transfer itself completed and the S2000 is back in normal mode.')
        partial(current)
        say('Inspect read-only with:  sudo ./s2ksend.py --check <slot> ' + a.sent_dir + '/NAME.wav --manage-piscsi')
        say(f'Transcript: {logname}')
        return 1
    except SendError as e:
        say(f'STOP: {e}')
        partial(current)
        if sess.mutated:
            say('Something may have been written and the S2000 may still be in SCSI-MIDI mode.')
            say('Switch SCSI-MIDI off on it if it is silent, then power-cycle the S2000 and the Pi.')
        say(f'Transcript: {logname}')
        return 1
    finally:
        bus.close()
        if a.manage_piscsi:
            if ok or quiet or not sess.mutated:
                restart_piscsi(say)
            else:
                say('PiSCSI deliberately left stopped (bus state uncertain).')


if __name__ == '__main__':
    sys.exit(main())
