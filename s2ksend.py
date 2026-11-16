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
import datetime
import os
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
RATE = 44100                # only 44.1 kHz mono 16-bit is supported (the proven profile)

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
    if not (1 <= frames <= 0xFFFFFFFF and 0 <= root < 128):
        raise SendError('bad frames/root')
    h = bytearray(SAMPLE_TEMPLATE)
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


# --------------------------------------------------------------------------- session
class Session:
    """The protocol, independent of what is on the other end of the bus."""

    def __init__(self, bus, log, budget_s=600.0):
        self.bus, self.log = bus, log
        self.deadline = time.monotonic() + budget_s
        self.commands = 0
        self.mutated = False         # a write command has been dispatched

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
        while len(got) < nbytes:
            n = min(65534, nbytes - len(got))
            c = bytearray(transfer_cdb(14, n))
            c[5] = 128
            got += self.cmd(bytes(c), 'in', length=n)
        self.mode(False)
        return bytes(got)

    def verify_audio(self, index, pcm, label):
        got = self.read_pcm(index, len(pcm))
        if got == pcm:
            return
        for suffix, data in (('expected', pcm), ('readback', got)):
            with open(f'{label}-{suffix}.pcm', 'wb') as f:
                f.write(data)
        raise VerifyError(f'audio read back from the S2000 DIFFERS from the file: {pcm_diagnosis(pcm, got)} '
                          f'(raw data saved as {label}-expected.pcm / {label}-readback.pcm)')

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
        return inv

    # ---- the transfer
    def send_sample(self, name, root, pcm, verify_pcm=True, progress=lambda m: None):
        frames = len(pcm) // 2
        before = self.inventory()
        enc = encode_name(name)
        if enc in before.samples:
            raise SendError(f'a sample named {name!r} already exists on the S2000; choose another --name')
        if len(before.samples) >= 4096 or before.free_headers < 1:
            raise SendError('no free sample header on the S2000')
        if before.free_words < frames:
            raise SendError(f'sample needs {frames} words but the S2000 has only {before.free_words} free')
        progress(f'S2000 has {len(before.samples)} samples, {before.free_words} free words')
        header = sample_header(name, root, frames)
        progress('sending sample header')
        self.control(sysex_sample(header, len(before.samples)), 'sample', bulk=True, keep=True)
        for at in range(0, len(pcm), PCM_BLOCK):
            blk = pcm[at:at + PCM_BLOCK]
            self.cmd(transfer_cdb(12, len(blk), raw=True), 'out', blk)
            if (at // PCM_BLOCK) % 16 == 0 or at + PCM_BLOCK >= len(pcm):
                progress(f'  PCM {min(at + PCM_BLOCK, len(pcm))}/{len(pcm)} bytes')
        self.mode(False)
        progress('finalising')
        self.control(post_object(), 'ack')
        after = self.inventory()
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
            self.verify_audio(index, pcm, name.strip().replace(' ', '_'))
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
                         + self._v7(2_000_000, 4) + self._v7(self.free_words - used, 4) + b'\0\xF7')
        elif op == 0x0B:
            if not self.bulk:
                raise SendError('mock: sample header outside bulk mode')
            ident = f[5] | (f[6] << 7)
            if ident != len(self.samples):
                raise SendError('mock: sample identifier is not the next free slot')
            header = unnibbles(f[7:-1])
            if len(header) != 192:
                raise SendError('mock: header length')
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


def load_audio(path, mono_mix=False):
    """Return ([PCM bytes per channel], converted?). Each channel is raw 16-bit little-endian, 44.1 kHz.
    A 1- or 2-channel 16-bit 44.1 kHz WAV is used as is; anything else is converted with sox, with dither
    disabled (-D) so that converting the same file twice gives identical audio. mono_mix averages L and R."""
    def read(p):
        with wave.open(p, 'rb') as w:
            ch, sw, fr, ct = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype()
            if ch > 2:
                if mono_mix:
                    raise wave.Error('format')           # let sox mix it down
                raise SendError(f'{ch}-channel audio: use --mono-mix, or reduce it to stereo first')
            if (sw, fr, ct) != (2, RATE, 'NONE'):
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
            raise SendError('file is not a 16-bit 44.1 kHz WAV and sox is not installed to convert it '
                            '(sudo apt install sox)')
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, 'c.wav')
            cmd = ['sox', '-D', path, '-b', '16', '-r', str(RATE)] + (['-c', '1'] if mono_mix else []) + [out]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise SendError('sox could not convert the file: ' + r.stderr.strip()[-200:])
            chans = read(out)
    if mono_mix and len(chans) == 2:
        chans = [array.array('h', [(l + r) >> 1 for l, r in zip(*chans)])]
    if not chans or len(chans[0]) < 1:
        raise SendError('no audio in file')
    return [_le_bytes(c) for c in chans], converted


def load_pcm(path):
    """Single-channel convenience wrapper (used by --check)."""
    chans, converted = load_audio(path)
    if len(chans) != 1:
        raise SendError('this file is stereo: compare each half separately using the NAME-L-sent.wav / '
                        'NAME-R-sent.wav files, or pass a mono file')
    return chans[0], converted


def sample_names(base, channels):
    """Akai names for the sample(s): one 12-char name for mono, NAME-L / NAME-R (name max 10 chars) for stereo."""
    if channels == 1:
        return [akai_name(base)]
    stem = akai_name(base)[:10].strip() or 'SAMPLE'
    return [stem + '-L', stem + '-R']


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
def rehearse(pcms, names, root, verify, say):
    words = sum(len(p) // 2 for p in pcms)
    s = MockS2000(samples=['EXISTING'], free_words=max(1_000_000, words + 1000))
    sess = Session(MockBus(s), lambda m: None, budget_s=600)
    sess.preflight(names, [len(p) // 2 for p in pcms])
    for name, pcm in zip(names, pcms):
        idx = sess.send_sample(name, root, pcm, verify, progress=lambda m: None)
        if s.samples[idx]['pcm'] != pcm or idx != len(s.samples) - 1:
            raise SendError('rehearsal: audio mismatch')
    say(f'rehearsal OK ({sess.commands} commands against the simulated S2000)')
    return sess.commands


def main(argv=None):
    ap = argparse.ArgumentParser(description='Send a WAV to an Akai S2000 via PiSCSI (SCSI initiator).')
    ap.add_argument('wav', nargs='?')
    ap.add_argument('--name', help='sample name on the S2000 (default: from the file name)')
    ap.add_argument('--root', type=int, default=60, help='root note, MIDI number (default 60)')
    ap.add_argument('--target', type=int, default=6, help='S2000 SCSI ID (default 6)')
    ap.add_argument('--initiator', type=int, default=7, help="the Pi's SCSI ID (default 7; the Mac must be off/disconnected)")
    ap.add_argument('--helper', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 's2kexec'))
    ap.add_argument('--manage-piscsi', action='store_true', help='stop the piscsi service before, restart it after')
    ap.add_argument('--no-verify-pcm', action='store_true', help='skip reading the audio back')
    ap.add_argument('--mono-mix', action='store_true',
                    help='stereo file: mix to ONE mono sample (default: two samples NAME-L and NAME-R)')
    ap.add_argument('--rehearse-only', action='store_true', help='only run the simulated rehearsal')
    ap.add_argument('--probe', action='store_true', help='just INQUIRY the target and exit')
    ap.add_argument('--check', type=int, metavar='SLOT',
                    help='READ-ONLY: show the header of the sample in SLOT (0 = first); with a WAV, also compare the audio')
    ap.add_argument('--yes', action='store_true', help='do not ask for the SEND confirmation')
    ap.add_argument('--log', help='transcript file (default s2ksend-TIMESTAMP.log)')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args(argv)
    say = lambda m: print(m, flush=True)

    if a.selftest:
        import random
        random.seed(1)
        for frames in (1, 8191, 8192, 8193, 20000):
            pcm = bytes(random.randrange(256) for _ in range(frames * 2))
            rehearse([pcm], ['SELFTEST'], 60, True, lambda m: None)
            pcm2 = bytes(random.randrange(256) for _ in range(frames * 2))
            rehearse([pcm, pcm2], sample_names('SELFTEST', 2), 60, True, lambda m: None)
        say('selftest OK')
        return 0
    if not a.probe and a.check is None and not a.wav:
        ap.error('give a WAV file')
    if not 0 <= a.root < 128 or a.target == a.initiator or not 0 <= a.target < 8 or not 0 <= a.initiator < 8:
        ap.error('bad --root/--target/--initiator')

    try:
        if a.probe:
            return probe(a, say)
        if a.check is not None:
            return check(a, say)
        pcms, converted = load_audio(a.wav, a.mono_mix)
        names = sample_names(a.name or os.path.splitext(os.path.basename(a.wav))[0], len(pcms))
        frames = len(pcms[0]) // 2
        say(f'{a.wav}: {frames} frames ({frames / RATE:.2f} s)'
            + (', stereo -> two separate mono samples ' + ' and '.join(repr(n) for n in names) + ' (not linked as a pair)'
               if len(pcms) == 2 else f', mono, Akai name {names[0]!r}')
            + f', root {a.root}' + (' (converted by sox, no dither)' if converted else ''))
        rehearse(pcms, names, a.root, not a.no_verify_pcm, say)
        if a.rehearse_only:
            return 0
        return physical(a, names, pcms, say)
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


def check(a, say):
    """Read-only: uses only the inventory, header-read and audio-read requests that a normal send uses
    for verification. Nothing is written to the S2000."""
    logname = a.log or datetime.datetime.now().strftime('s2ksend-check-%Y%m%d-%H%M%S.log')
    logf = open(logname, 'w', buffering=1)
    want, converted = (load_pcm(a.wav) if a.wav else (None, False))     # fail early, before PiSCSI is stopped
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
        say(f'  -> {frames} frames, rate {int.from_bytes(hdr[138:140], "little")} Hz, root {hdr[2]}')
        if a.wav:
            if converted:
                say('  note: the file needed conversion; compare against the "-sent.wav" saved by the send instead '
                    'if it was sent with an earlier version (older versions used random dither)')
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


def physical(a, names, pcms, say):
    logname = a.log or datetime.datetime.now().strftime('s2ksend-%Y%m%d-%H%M%S.log')
    logf = open(logname, 'w', buffering=1)
    for name, pcm in zip(names, pcms):
        sent_wav = f'{name.strip().replace(" ", "_")}-sent.wav'
        with wave.open(sent_wav, 'wb') as w:             # exactly the audio that will be sent, for later --check
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        say(f'(exact audio saved as {sent_wav}; use it with --check later)')
    total = sum(len(p) for p in pcms)
    say(f'About to SEND {len(pcms)} sample(s), {total} bytes in all, to the S2000 at SCSI ID {a.target} '
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
    ok = False
    quiet = False                    # True when a read-back check failed but the bus is idle and healthy
    done = []
    def partial():
        if done:
            say('Already in the S2000 RAM from this run: ' + ', '.join(f'{n!r} (slot {i})' for n, i in done))
    try:
        say(bus.banner)
        sess.preflight(names, [len(p) // 2 for p in pcms])
        for name, pcm in zip(names, pcms):
            say(f'--- {name!r}')
            idx = sess.send_sample(name, a.root, pcm, not a.no_verify_pcm, progress=say)
            done.append((name, idx))
            say(f'DONE: {name!r} is in sample slot {idx}.')
        say(f'All done: {len(done)} sample(s), {sess.commands} commands. Transcript: {logname}')
        ok = True
        return 0
    except VerifyError as e:
        quiet = True
        say(f'VERIFY FAILED: {e}')
        say('The transfer itself completed and the S2000 is back in normal mode.')
        partial()
        say(f'Inspect read-only with:  sudo ./s2ksend.py --check <slot> NAME-sent.wav --manage-piscsi')
        say(f'Transcript: {logname}')
        return 1
    except SendError as e:
        say(f'STOP: {e}')
        partial()
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
