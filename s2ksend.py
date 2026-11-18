#!/usr/bin/env python3
"""s2ksend - command-line client for s2kserver: send WAV files to an Akai S2000 via PiSCSI.

The server (s2kserver.py) runs on the Raspberry Pi with the PiSCSI board. This client can run on that Pi
or on any other computer on the network:

    s2ksend --server http://piscsi.local:8420 kick.wav snare.wav loop.wav
    export S2K_SERVER=http://piscsi.local:8420          # or set it once

Audio loading, naming and rate conversion (sox needed only for non-16-bit/odd-rate files) happen HERE; the
server receives ready-to-send 16-bit mono PCM. Other commands: --status --list --probe --pairs --check
--download --recover. Everything except sending is read-only.
"""
import argparse
import os
import sys

import s2kcore as core
from s2kclient import S2KClient, S2KError


def say(msg):
    print(msg, flush=True)


def build_parser():
    ap = argparse.ArgumentParser(description='Send WAV files to an Akai S2000 through s2kserver.')
    ap.add_argument('wavs', nargs='*', metavar='WAV', help='WAV files to send (or the output file for --download)')
    ap.add_argument('--server', default=os.environ.get('S2K_SERVER', 'http://localhost:8420'),
                    help='s2kserver URL (default $S2K_SERVER or http://localhost:8420)')
    ap.add_argument('--name', help='sample name on the S2000 (single file only; default: from the file name)')
    ap.add_argument('--root', type=int, default=60, help='root note, MIDI number (default 60)')
    ap.add_argument('--rate', choices=['auto', '44100', '22050'], default='auto',
                    help='sample rate on the S2000. auto: 22050 for sources at or below 22.05 kHz, else 44100')
    ap.add_argument('--mono-mix', action='store_true',
                    help='stereo files: mix to ONE mono sample (default: two samples NAME-L and NAME-R)')
    ap.add_argument('--no-verify-pcm', action='store_true', help='skip reading the audio back (faster)')
    ap.add_argument('--block', type=int, choices=[8192, 16384, 32768], default=core.PCM_BLOCK,
                    help='bytes per audio write (default 16384, the size proven on hardware; MESA uses up to 32768)')
    ap.add_argument('--yes', action='store_true', help='do not ask for the SEND confirmation')
    ap.add_argument('--rehearse-only', action='store_true',
                    help='plan and rehearse locally against the simulated S2000; the server is not contacted')
    ap.add_argument('--status', action='store_true', help='show the server and bus state')
    ap.add_argument('--list', action='store_true', help='READ-ONLY: list the samples on the S2000 and free memory')
    ap.add_argument('--probe', action='store_true', help='READ-ONLY: INQUIRY the S2000 on the bus')
    ap.add_argument('--piscsi', action='store_true', help='READ-ONLY: PiSCSI state, its attached devices, and the setup the server has saved')
    ap.add_argument('--pairs', action='store_true', help='READ-ONLY: find -L/-R pairs and decode their partner fields')
    ap.add_argument('--check', type=int, metavar='SLOT',
                    help='READ-ONLY: show the header of the sample in SLOT; with a WAV, also compare the audio')
    ap.add_argument('--download', type=int, metavar='SLOT', help='READ-ONLY: save the sample in SLOT as WAV (give the file name)')
    ap.add_argument('--recover', action='store_true',
                    help='after a failed send AND a power-cycle of the S2000 and the Pi: restart PiSCSI, accept work again')
    return ap


def describe(items):
    core.describe(items, say)
    if any(items[i].source == items[i + 1].source for i in range(len(items) - 1)):
        say('(stereo files become a -L/-R pair named like the S2000\'s own; the S2000 links the two halves itself when both are in)')


# ------------------------------------------------------------------ read-only commands
def show_status(c):
    st = c.status()
    b = st['bus']
    say(f'server {st["version"]} (API {st["api"]}) at {c.base}' + ('  [SIMULATED S2000]' if b['mock'] else ''))
    say(f'bus: {b["state"]}; PiSCSI: {b["piscsi"]}; S2000 SCSI ID {b["target"]}, server is ID {b["initiator"]}')
    snap = b.get('piscsi_snapshot')
    if snap and snap['pending']:
        say(f'PiSCSI setup saved ({snap["devices"]} device(s)); it will be put back when PiSCSI restarts')
    lr = b.get('last_restore')
    if lr:
        say(f'last PiSCSI restore: {lr["note"]}' + (f' ({len(lr["failed"])} command(s) failed)' if lr['failed'] else ''))
    say(f'jobs: current {st["jobs"]["current"] or "none"}, queued {st["jobs"]["queued"]}')
    return st


def show_piscsi(c):
    r = c.piscsi()
    say(f'PiSCSI service: {r["state"]}; restore mode: {r["restore_mode"]}')
    fmt = lambda d: f'  ID {d["id"]}:{d["lun"]} {d["type"]} {d["file"] or "(no media)"}' + ('  [write protected]' if d['protected'] else '')
    if r['devices'] is not None:
        say(f'attached now ({len(r["devices"])}):')
        for d in r['devices']:
            say(fmt(d))
    if r['snapshot']:
        say(f'saved setup ({r["snapshot"]["devices"]} device(s), {"waiting to be restored" if r["snapshot"]["pending"] else "already restored"}):')
        for d in r['snapshot_devices']:
            say(fmt(d))
    elif r['devices'] is None:
        say('(nothing to show: PiSCSI is not managed here, or is stopped with no saved setup)')


def show_list(c):
    inv = c.samples()
    used = (inv['total_headers'] - inv['free_headers']) if inv.get('total_headers') else None
    say(f'{len(inv["samples"])} samples, {len(inv["programs"])} programs; free: {inv["free_words"]} words '
        f'({inv["free_words"] * 2 / 1e6:.1f} MB), {inv["free_headers"]} of {inv.get("total_headers", "?")} sample headers'
        + (f' ({used} in use)' if used is not None else ''))
    for s in inv['samples']:
        say(f'  {s["slot"]:3d}  {s["name"]!r}')
    if inv['programs']:
        say('programs: ' + ', '.join(repr(p.strip()) for p in inv['programs']))


def show_check(c, slot, wav):
    info = c.sample(slot)
    hdr = bytes.fromhex(info['header_hex'])
    say(f'slot {slot}: {info["name"]!r}')
    for lo, hi, field, _crit in [(f[0], f[1], f[2], f[3]) for f in core.FIELDS]:
        say(f'  bytes {lo:3d}-{hi - 1:3d} {field:26s} {hdr[lo:hi][:24].hex()}{"..." if hi - lo > 24 else ""}')
    say(f'  -> {info["frames"]} frames, rate {info["rate"]} Hz, root {info["root"]}')
    say('  -> stereo partner: none' if info['partner'] is None else
        f'  -> stereo partner field 0x{info["partner"]:04x} (linked; see --pairs for what it points to)')
    if wav:
        chans, converted, rate = core.load_audio(wav)
        if len(chans) != 1:
            raise core.SendError('that file is stereo: compare each half separately using the NAME-L / NAME-R files '
                                 'the server saved, or pass a mono file')
        if converted:
            say('  note: the file needed conversion; compare against the exact audio the server saved with the job instead')
        if rate != info['rate']:
            say(f'  note: the file is at {rate} Hz but the sample is at {info["rate"]} Hz')
        if len(chans[0]) != info['frames'] * 2:
            say(f'  audio NOT compared: file has {len(chans[0]) // 2} frames, the sample has {info["frames"]}')
        else:
            say('  reading audio back...')
            _, got = c.sample_audio(slot, 'pcm')
            diag = core.pcm_diagnosis(chans[0], got)
            say('  audio MATCHES the file byte-for-byte' if diag == 'identical'
                else f'  audio DIFFERS from the file: {diag}')


def show_pairs(c):
    r = c.pairs()
    say(f'{r["samples"]} samples on the S2000')
    if not r['pairs']:
        say('no -L/-R pairs found (names ending in -L and -R with the same start)')
        return
    f = lambda v: 'none (ffff)' if v is None else f'0x{v:04x}'
    for p in r['pairs']:
        say(f'  {p["left"]["name"]!r} slot {p["left"]["slot"]}: partner field {f(p["left"]["partner"])}   |   '
            f'{p["right"]["name"]!r} slot {p["right"]["slot"]}: partner field {f(p["right"]["partner"])}'
            + ('' if p['linked'] else '   (not linked)'))
    lay = r.get('layout')
    if lay:
        say(f'{lay["pairs"]} linked pair(s). If each field is the address of the partner\'s 12-byte entry:')
        say('  spacing inside every pair: ' + (f'{lay["spacing"]} bytes (consistent)' if lay['spacing_consistent']
                                              else 'NOT consistent'))
        if lay['holes']:
            say(f'  but entries are not simply 12 x slot: {lay["extra_entries"]} extra entries sit between pairs, at '
                + ', '.join(f'slot {h["before_slot"]} (+{h["entries"]})' for h in lay['holes']))
        else:
            say('  and no extra entries between pairs: address = base + 12 x slot')
    rule = r['rule']
    if rule and not (lay and lay['holes']):
        say(f'  rule that fits {"all" if rule["consistent"] else "the first"} linked pair(s): '
            f'field = 0x{rule["base"]:04x} + {rule["stride"]} x slot of the partner')


# ------------------------------------------------------------------ sending
def send(c, a):
    items = core.plan_items(a.wavs, a.mono_mix, a.rate, a.name)
    say(f'{len(a.wavs)} file(s) -> {len(items)} sample(s), root {a.root}:')
    describe(items)
    st = c.status()
    b = st['bus']
    say(f'server {c.base}: bus {b["state"]}, S2000 at SCSI ID {b["target"]}' + ('  [SIMULATED S2000]' if b['mock'] else ''))
    if b['state'] == 'needs_attention':
        raise S2KError(409, 'needs_attention', 'a previous send left the bus in an uncertain state: power-cycle the '
                       'S2000 and the Pi, then run --recover')
    total = sum(len(i.pcm) for i in items)
    say(f'uploading {total / 1000:.0f} KB to the server...')
    snap = c.prepare(items, a.root, not a.no_verify_pcm, a.block)
    if snap['state'] != 'ready':
        err = snap['error'] or {}
        say(f'STOP: the server rehearsal failed: {err.get("message", snap["state"])}')
        return 1
    say(f'server rehearsal OK ({snap["rehearsal"]["commands"]} commands against the simulated S2000)')
    if not a.yes:
        say(f'About to SEND {len(items)} sample(s), {total} bytes in all, to the S2000.')
        say('Make sure the Mac is off or disconnected, and that nothing on the S2000 needs saving: it will')
        say('gain sample(s) in RAM. Do not touch the S2000 until this finishes.')
        try:
            typed = input('Type SEND to continue: ').strip()
        except EOFError:
            typed = ''
        if typed != 'SEND':
            c.cancel(snap['id'])
            say('Cancelled; nothing sent.')
            return 1
    started = c.start(snap['id'])
    shown = []
    final = c.wait(snap['id'], on_log=lambda m: (shown.append(m), say(m)), since=started['next'])
    if final['state'] == 'done':
        r = final['result']
        say(f'All done. {len(r["samples"])} sample(s), {r["audio_bytes"] / 1000:.0f} KB of audio in {r["elapsed_s"]} s '
            f'overall ({r["overall_kbps"]} KB/s including every check), {r["commands"]} commands'
            + (f'; audio writes {r["write_kbps"]} KB/s (block {r["block"]})' if r['write_kbps'] else '')
            + (f'; verify reads {r["verify_kbps"]} KB/s' if r['verify_kbps'] else ''))
        say('Slots: ' + ', '.join(f'{s["name"].strip()}={s["slot"]}' for s in r['samples']))
        return 0
    err = final['error'] or {}
    if not err:
        say(f'job {final["state"]}')
    elif not any(m.startswith('FAILED (') for m in shown):
        say(f'FAILED ({err.get("code")}): {err.get("message")}')
    if err.get('done'):
        say('Already in the S2000 RAM from this run: ' + ', '.join(f'{d["name"]!r} (slot {d["slot"]})' for d in err['done']))
    if err.get('not_completed'):
        say('Not completed: ' + ', '.join(err['not_completed']))
    if err.get('code') == 'needs_attention' or (err.get('code') == 'send_failed' and c.status()['bus']['state'] == 'needs_attention'):
        say('Something may have been written and the S2000 may still be in SCSI-MIDI mode. Switch SCSI-MIDI off on it if')
        say('it is silent, power-cycle the S2000 and the Pi, then run: s2ksend --recover')
    return 1


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        if a.rehearse_only:
            if not a.wavs:
                ap.error('give at least one WAV file')
            items = core.plan_items(a.wavs, a.mono_mix, a.rate, a.name)
            say(f'{len(a.wavs)} file(s) -> {len(items)} sample(s), root {a.root}:')
            describe(items)
            core.rehearse(items, a.root, not a.no_verify_pcm, say, a.block)
            return 0
        c = S2KClient(a.server)
        if a.status:
            show_status(c)
        elif a.list:
            show_list(c)
        elif a.piscsi:
            show_piscsi(c)
        elif a.probe:
            r = c.probe()
            say(f'INQUIRY status {r["status"]}, {r["bytes"]} bytes: vendor {r["vendor"]!r} product {r["product"]!r}')
        elif a.pairs:
            show_pairs(c)
        elif a.check is not None:
            if len(a.wavs) > 1:
                ap.error('--check takes at most one WAV file')
            show_check(c, a.check, a.wavs[0] if a.wavs else None)
        elif a.download is not None:
            if len(a.wavs) != 1:
                ap.error('--download SLOT needs exactly one output file name')
            info, wav = c.sample_audio(a.download, 'wav')
            with open(a.wavs[0], 'wb') as f:
                f.write(wav)
            say(f'saved slot {a.download} {info["name"]!r} ({info["frames"]} frames, {info["rate"]} Hz) to {a.wavs[0]}')
        elif a.recover:
            r = c.recover()
            say(f'bus: {r["bus"]["state"]}; PiSCSI: {r["bus"]["piscsi"]}')
        else:
            if not a.wavs:
                ap.error('give at least one WAV file (or use --list, --status, ...)')
            return send(c, a)
        return 0
    except (S2KError, core.SendError) as e:
        say(f'STOP: {e.message if isinstance(e, S2KError) else e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
