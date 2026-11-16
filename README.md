# s2ksend - send a WAV straight to an Akai S2000 from a Raspberry Pi + PiSCSI

No disk image. The Pi's PiSCSI board acts as the SCSI **initiator**; the S2000 is the target.
It uses the S2000's SCSI-MIDI tunnel (vendor commands 09/0C/0D/0E carrying Akai SysEx), the same
channel MESA / ReCycle use. Sequence and encodings come from a hardware-proven implementation;
`tests/parity.py` checks 11 wire frames byte-for-byte against that code's C++.

## Status - read this first
* **Hardware-verified (v0.4, 16 Nov 2026):** a 0.4 s mono sample was sent to an S2000 and read back byte-for-byte identical.
  Earlier notes: not run on real hardware by me; verified: byte-exact encodings (parity test), the full
  transfer sequence against a strict simulated S2000 (`--selftest`), and that `s2kexec` builds.
* Only 16-bit mono 44.1 kHz (the proven profile). Other WAVs are converted with sox.
* Fewer safety gates than the original project (no replay gate, no hash pinning, no independent oracle).
  The rehearsal + read-back verification are the substitutes.

## Hardware
* PiSCSI board that supports initiator mode (FULLSPEC board, or direct wiring without transceivers).
* S2000 = ID 6. The Pi becomes the initiator, default ID 7, so **power off/disconnect the Mac**
  (or use `--initiator N` with a free ID).
* The S2000 Bridge project page says it was tested with a Raspberry Pi 3 and a RaSCSI v2.3B board. Other
  Pi models / boards are untested here (the GPIO code in gpio-source/ is what talks to the hardware).
* While a send runs, `piscsi` is stopped: your emulated disks vanish, then come back.

## Setup (on the Pi)
    sudo apt install g++ make sox
    make                       # builds ./s2kexec
    python3 s2ksend.py --selftest

## Use
    sudo ./s2ksend.py --probe --manage-piscsi                    # optional: INQUIRY ID 6
    ./s2ksend.py kick.wav --rehearse-only                        # no hardware needed
    sudo ./s2ksend.py kick.wav --name KICK --root 60 --manage-piscsi

Flow: rehearsal -> you type SEND -> stop PiSCSI -> inventory -> create sample in RAM -> stream
audio in 16 KB blocks -> finalise -> re-read inventory, header and (default) all the audio and compare
-> restart PiSCSI. A transcript `s2ksend-*.log` is written.

## Stereo (v0.5)
A stereo WAV is sent as TWO separate mono samples, `NAME-L` and `NAME-R` (name limited to 10 characters), the
way the S2000 itself stores stereo recordings. They are NOT linked as a pair (the stereo-partner header field is
left as for a mono sample); assign them to left/right zones on the S2000. `--mono-mix` sends one mono sample
instead. Both samples are checked for name clashes and total memory before anything is written. If the second
fails, the first is reported as already in RAM. Each half is saved as NAME-L-sent.wav / NAME-R-sent.wav.

    sudo ./s2ksend.py loop.wav --name LOOP --manage-piscsi
    sudo ./s2ksend.py --check 6 LOOP-L-sent.wav --manage-piscsi      # read-only re-check of a slot

## If something goes wrong
After an error that may have written, PiSCSI is deliberately left stopped. If the S2000 is silent, switch
SCSI-MIDI off on it, power-cycle the S2000 and the Pi. The sample only exists in S2000 RAM: save it to disk
on the S2000 if you want to keep it. Nothing is retried automatically.

## Files
s2ksend.py (protocol + rehearsal simulator + CLI; --check = read-only inspection) | s2kexec.cpp (bus helper, no Akai logic) |
tests/ (parity vs reference C++, verify/diagnosis, full physical flow through a fake bus helper) |
gpio-source/ (BSD-3 initiator code from the supplied release, licence kept) | tests/ (parity check)

## Provenance and licence (please read before sharing)
* The command sequence and encodings follow the S2000 Bridge release (michaelsanois/s2000-bridge) and the
  MESA II research at michaelsanois/mesa2-research, which independently documents the same sample-import
  sequence (bulk MODE, sample header + readiness reply, raw PCM blocks with flag 0x80, normal MODE, post-object).
* `SAMPLE_TEMPLATE` in s2ksend.py is the captured 192-byte header template from that release.
* No licence file was visible on either repository when this was written, so default copyright applies.
  Treat this package as PERSONAL USE ONLY until the author gives permission; do not redistribute it.
* `gpio-source/` is BSD-3-Clause (SCSI2Pi/PiSCSI-derived); its LICENSE and NOTICE must travel with it.

## Hardware test log
* 16 Nov 2026, Pi 3 Model B + PiSCSI board, S2000 at ID 6: a 0.4 s mono sample (kick.wav) transferred and played
  back correctly. v0.1's header read-back check was too strict and reported a false failure; v0.2 prints any
  differing header fields as notes and only fails on name, length or rate. Audio is still verified byte-for-byte.
* 16 Nov 2026 `--check 4`: the S2000 reports the header exactly as sent except two bytes it sets itself: start
  address (bytes 22-25, internal) and stereo partner (bytes 136-137 = ffff for a mono sample; the Bridge's own
  receive code accepts 0000 or ffff as "no partner"). The audio read-back failed in v0.2 because the RSPACK
  count is in sample WORDS (frames), not bytes; v0.3 asks for the right amount.
* 16 Nov 2026, second `--check`: size now correct, audio comparison reported DIFFERS. v0.4 disables sox dither
  (sox -D) so conversions are reproducible, saves the exact audio sent as NAME-sent.wav, and on any audio
  mismatch writes NAME-expected.pcm / NAME-readback.pcm and prints a diagnosis (byte swap, sign flip, shift,
  or tiny dither-like differences).
* 16 Nov 2026, v0.4: `--name KICK2` on Pi 3 + S2000 (ID 6): header checked, audio verified byte-for-byte, 63 commands.
  The earlier audio mismatch was sox dither in the comparison, not a transfer fault.
* 16 Nov 2026, v0.5: loop.wav (stereo, 3.69 s, 162831 frames) sent as LOOP-L / LOOP-R (slots 6 and 7), each
  verified byte-for-byte, 183 commands, 651 KB in all. Header notes identical to the mono case.
