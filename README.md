# s2k-send: send samples to an Akai S2000 over SCSI from a Raspberry Pi + PiSCSI (client / server)

No disk image and no slow MIDI sample dump: the Pi's PiSCSI board acts as the SCSI **initiator** and talks to the S2000
through its SCSI-MIDI tunnel (vendor commands 09/0C/0D/0E carrying Akai SysEx), the same channel MESA / ReCycle use.

    +-------------------+   HTTP/JSON    +------------------------------+   SCSI   +---------+
    | s2ksend.py (CLI)  | -------------> | s2kserver.py  (on the Pi)    | -------> | S2000   |
    | or a GUI          |  home network  | -> s2kcore + s2kexec + GPIO  |          | ID 6    |
    +-------------------+                +------------------------------+          +---------+

* **Server** (`s2kserver.py`, on the Pi, as root): owns the bus, stops/restarts PiSCSI around each operation, runs send jobs,
  rehearses every job on a simulated S2000 first, verifies every sample by reading it back, keeps transcripts.
* **Client** (`s2ksend.py` CLI, `s2kclient.py` library): on the same Pi or any computer. Loads/converts audio, names samples,
  uploads PCM, shows progress. Intended to become the back end of a GUI such as AKAISDS: see **API.md**.
* **Home network only**: the server has no authentication.

## Status (read this first)
* **Hardware-verified earlier (single-file tool, 16 Nov 2026):** mono and stereo sends, 44.1 kHz, byte-for-byte audio read-back,
  header checks, read-only inspection of samples and stereo pairs. See the hardware log below.
* **The client/server split (v0.7) is simulator-tested only so far**: server, client, CLI, job queue, failure handling and the
  real-bus code path (through a fake bus helper) all pass `./run_tests.sh`; the server has not yet run on the Pi.
* **Not yet tried on hardware:** 22.05 kHz, `--block 32768`, batches of many files, the HTTP server itself.
* Fewer safety gates than the original project this knowledge came from (no replay gate, no pinned hashes). The rehearsal and the
  audio read-back are the substitutes. **Provenance and licence: see the end of this file.**

## Hardware
* PiSCSI board that supports initiator mode (FULLSPEC board, or direct wiring without transceivers). The S2000 Bridge project
  page says it was tested with a Raspberry Pi 3 and a RaSCSI v2.3B; other Pi models/boards are untested here.
* S2000 = ID 6. The Pi becomes the initiator, default ID 7, so **power off/disconnect the Mac** (or use `--initiator N`).
* While an operation runs, `piscsi` is stopped: the Pi's emulated disks vanish, then come back (`--keep-bus` seconds later).

## Install and run the server (on the Pi)
    sudo apt install g++ make sox python3
    make                                   # builds ./s2kexec, the bus helper
    sudo ./s2kserver.py                    # listens on :8420; add --keep-bus 0 to restart PiSCSI immediately
    ./s2kserver.py --mock                  # simulated S2000 for developing clients, no hardware, no root
To start it at boot see `s2k-server.service`. Options: `--target 6 --initiator 7 --data-dir DIR --no-manage-piscsi --keep-bus SECS`.

## Use the client (same Pi or another computer; needs only s2ksend.py, s2kcore.py, s2kclient.py)
    export S2K_SERVER=http://piscsi.local:8420            # or pass --server each time
    ./s2ksend.py --status                                 # server + bus state
    ./s2ksend.py --list                                   # samples on the S2000 and free memory (read-only)
    ./s2ksend.py kick.wav snare.wav hats.wav loop.wav     # send a batch (asks you to type SEND)
    ./s2ksend.py loop.wav --name LOOP --rate 22050 --block 32768 --no-verify-pcm --yes
    ./s2ksend.py --check 6 kick.wav                       # read-only: header + audio compared with a file
    ./s2ksend.py --download 6 kick_back.wav               # read-only: save a sample as WAV
    ./s2ksend.py --pairs                                  # read-only: stereo -L/-R pairs and their partner fields
    ./s2ksend.py --rehearse-only kick.wav loop.wav        # plan + rehearse locally, server not contacted
Every send flow: load and plan locally -> upload -> server rehearsal -> you confirm -> transfer with live progress -> verify.

### Batches, rates and speed
* Names come from the file names (12 characters; duplicates get a number, e.g. KICK, KICK2). `--name` is for a single file.
* `--rate auto` (default): sources at or below 22.05 kHz go as 22.05 kHz, anything above as 44.1 kHz; `--rate 44100|22050` forces one
  (sox resamples, dither off). 22.05 kHz sets header bandwidth byte 0 and rate 22050 (Ohsaki's S3000 format notes).
* Speed: each run reports audio-write, verify-read and overall KB/s. Try `--block 32768` (MESA's size; default 16384 is the size proven
  here) and `--no-verify-pcm` (the read-back takes about as long again as the send). The sample-list read after each sample is reused
  as the next one's starting point.
* Stereo: a stereo WAV becomes two separate mono samples named the way the S2000 names its own pairs (name padded to 10 characters,
  then `-L` / `-R` as the last two of 12). They are **not linked** as a pair. `--mono-mix` sends one mono sample instead.
  If the second half fails the first is reported as already in RAM.

## If something goes wrong
After an error that may have written, the server leaves PiSCSI stopped and refuses work (`needs_attention`). Switch SCSI-MIDI off on the
S2000 if it is silent, power-cycle the S2000 and the Pi, then `./s2ksend.py --recover`. A failed read-back comparison is different: the
transfer completed, the bus is fine and PiSCSI is restarted normally; the output says how to inspect the sample read-only.
Samples live only in S2000 RAM: save them to disk on the S2000 if you want to keep them. Nothing is retried automatically.
The exact audio sent and a transcript of every SCSI command are kept per job under the server's `--data-dir`.

## Tests (no hardware)
    ./run_tests.sh
`tests/parity.py` compares 11 wire frames byte-for-byte with the reference C++ output; `test_server.py` runs the server, client library
and CLI against a simulated S2000 (batches, both rates, stereo, errors, busy bus, SSE, failure and recovery through a fake bus helper).

## Files
s2kcore.py (protocol, simulator, bus manager, audio helpers) | s2kserver.py | s2kclient.py | s2ksend.py (CLI) | API.md |
s2kexec.cpp + Makefile (bus helper, no Akai logic) | gpio-source/ (BSD-3 initiator code, licence kept) | s2k-server.service | tests/

## Stereo pair research (S2000 with pairs loaded from disk)
* A mono sample reads back partner field 0xffff (= none). In every real pair, each field is the internal address of the *partner's*
  12-byte entry: L's field = address(R), R's field = address(L), and **address(R) = address(L) + 12 in all 29 pairs seen** (59 samples,
  17 Nov 2026), so the S2000 keeps L and R in adjacent entries (12 = bytes per name in the sample roster).
* The address is **not** `base + 12 x slot`: in that data set 36 extra 12-byte entries sit between groups of pairs (at slots 4 (+3),
  8 (+4), 14 (+5), 22 (+5), 30 (+4), 36 (+4), 42 (+3), 46 (+4), 52 (+4)), and the first pair's base differed between two sessions
  (0x6030, then 0x6024). The entries probably also hold programs or deleted samples; the address of the next free entry therefore cannot
  be derived from the slot number. `--pairs` reports this; `--list` shows programs and header usage for checking the idea.
* Linking new pairs by writing this field is **not implemented**: it is an internal address that depends on the S2000's load history,
  the S2000 rewrote a 0000 we sent into ffff on a mono sample (so it may not accept a supplied value), and a wrong address could upset it.

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
* v0.6 (batches, 22.05 kHz, --block, speed report) is simulator-tested only so far (tests/: parity, verify, physical flow
  through a fake helper at both rates and both block sizes, batch/rate/name planning).
* v0.7: split into server / client / CLI with an HTTP API; simulator-tested only (see Status).
