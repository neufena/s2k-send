# s2k-send

Send samples to an Akai S2000 over SCSI from a Raspberry Pi with a PiSCSI board.

No disk image and no slow MIDI sample dump: the Pi's PiSCSI board acts as the SCSI **initiator** and talks to the S2000
through its SCSI-MIDI tunnel (vendor commands 09/0C/0D/0E carrying Akai SysEx), the same channel MESA and ReCycle use.

    +-------------------+   HTTP/JSON    +------------------------------+   SCSI   +---------+
    | s2ksend (CLI)     | -------------> | s2kserver  (on the Pi)       | -------> | S2000   |
    | or a GUI          |  home network  | -> s2kcore + s2kexec + GPIO  |          | ID 6    |
    +-------------------+                +------------------------------+          +---------+

* **Server** (`s2kserver`, on the Pi, as root): owns the bus, stops and restarts PiSCSI around each operation, runs send jobs,
  rehearses every job on a simulated S2000 first, verifies every sample by reading it back, and keeps transcripts.
* **Client** (`s2ksend` CLI, `s2kclient` library): on the same Pi or any computer. Loads and converts audio, names samples,
  uploads PCM and shows progress. A GUI can use the library or the HTTP API directly: see [API.md](API.md).
* **Home network only**: the server has no authentication.

Scope is sending samples to the S2000 and reading them back. Programs, keygroups, deleting and renaming are left to MIDI tools.

## Status
* **Verified on hardware** (Pi 3 Model B, PiSCSI, S2000): mono and stereo sends at 44.1 kHz with byte-for-byte read-back, batches
  from a client on another computer, `--status`, `--list`, `--check`, `--download`, `--pairs`, and the busy / not-found error paths.
* **Simulator-tested only**: 22.05 kHz sends, `--block 32768`, PiSCSI setup retention, large batches, the systemd service.
  [docs/hardware-test-plan.md](docs/hardware-test-plan.md) covers these.
* The simulated S2000 is built from our understanding of the protocol. Passing tests show the command sequence is self-consistent,
  not that a real S2000 accepts it; every hardware result was confirmed by reading back from the sampler.

See [CHANGELOG.md](CHANGELOG.md) for the version history.

## Hardware
* Current Raspberry Pi OS (Trixie, Python 3.13). Older releases are not supported.
* A PiSCSI board that supports initiator mode (FULLSPEC board, or direct wiring without transceivers). The S2000 Bridge project
  page says it was tested with a Raspberry Pi 3 and a RaSCSI v2.3B; other Pi models and boards are untested here.
* S2000 at SCSI ID 6. The Pi becomes the initiator, default ID 0, so **reserve ID 0 in PiSCSI** (no emulated device on it) and
  **power off or disconnect any other initiator using it** (or pass `--initiator N`).
* While an operation runs, `piscsi` is stopped: the Pi's emulated disks vanish, then come back `--keep-bus` seconds later **with the
  same setup as before** (see below).

## Install

### Server (on the Pi)
    sudo apt install g++ make python3-venv
    git clone https://github.com/neufena/s2k-send && cd s2k-send
    sudo make install                                     # builds the s2kexec bus helper into /usr/local/bin
    sudo python3 -m venv /opt/s2k-send
    sudo /opt/s2k-send/bin/pip install .
    sudo /opt/s2k-send/bin/s2kserver                      # listens on :8420
To start it at boot, follow the header of [s2k-server.service](s2k-server.service).

Options: `--target 6 --initiator 0 --data-dir DIR --helper PATH --no-manage-piscsi --keep-bus SECS --restore-piscsi exact|attach-only|off`.
`--mock` runs against a simulated S2000, with no hardware and no root, for developing clients.

### Client (any computer with Python 3.13 or later)
    pipx install git+https://github.com/neufena/s2k-send  # or: pip install . from a checkout
sox is needed only for audio that is not already 16-bit WAV at 44.1 or 22.05 kHz.

### From a checkout, without installing
    make && sudo ./s2kserver.py                           # the server finds ./s2kexec next to s2kcore.py
    ./s2ksend.py --status

## Use
    export S2K_SERVER=http://piscsi.local:8420            # or pass --server each time
    s2ksend --status                                      # server and bus state
    s2ksend --list                                        # samples on the S2000 and free memory (read-only)
    s2ksend --piscsi                                      # PiSCSI state, attached devices, the saved setup (read-only)
    s2ksend kick.wav snare.wav hats.wav loop.wav          # send a batch (asks you to type SEND)
    s2ksend loop.wav --name LOOP --rate 22050 --block 32768 --no-verify-pcm --yes
    s2ksend --check 6 kick.wav                            # read-only: header and audio compared with a file
    s2ksend --download 6 kick_back.wav                    # read-only: save a sample as WAV, with progress
    s2ksend --pairs                                       # read-only: stereo -L/-R pairs and their partner fields
    s2ksend --rehearse-only kick.wav loop.wav             # plan and rehearse locally, server not contacted
    s2ksend --jobs                                        # recent send jobs
    s2ksend --fetch-job ID [FOLDER]                       # save a job's transcript and audio sent (after a failure, say)
Where `.local` names don't resolve (e.g. in a container), use the Pi's IP address instead.
Every send: load and plan locally -> upload -> server rehearsal -> you confirm -> transfer with live progress -> verify.

### Names, rates and stereo
* Names come from the file names (12 characters from `0-9`, space, `A-Z`, `#`, `+`, `-`, `.`; duplicates get a number, e.g. KICK,
  KICK2). `--name` is for a single file.
* `--rate auto` (default): resample to one of the S2000's own rates (sources at or below 22.05 kHz to 22.05 kHz, anything above
  to 44.1 kHz). `--rate native` keeps the source rate, from 4 kHz to 65.535 kHz, with no resampling: the header retunes it to play
  at the right pitch (simulator-tested only). `--rate HZ` resamples to that rate. sox resamples with dither off, so conversions are
  repeatable.
* A stereo WAV becomes two mono samples named the way the S2000 names its own pairs (name padded to 10 characters, then `-L` / `-R`
  as the last two of 12), sent L then R. **The S2000 links them as a stereo pair by itself** when the R arrives; the job checks both
  partner fields afterwards and reports `linked` or `NOT linked`. `--mono-mix` sends one mono sample instead.

### Speed
Each run reports audio-write, verify-read and overall KB/s. Time is dominated by command count (about 50 ms per SCSI command), not
data rate; a 3-sample, 687 KB batch took 11 s. `--block 32768` (MESA's size; the default 16384 is the size proven on hardware) and
`--no-verify-pcm` (the read-back takes about as long again as the send) both help.

## Your PiSCSI setup is kept
Before the server stops `piscsi` it reads the attached devices with `scsictl -l` and saves them (in memory and in
`<data-dir>/piscsi-snapshot.json`). After starting PiSCSI again it waits for the web UI's own default config to settle, then puts the
setup back: it re-attaches what is missing (`scsictl -i ID[:LUN] -c attach -t TYPE -f FILE`, write protection included) and, in the default
`--restore-piscsi exact` mode, detaches anything that was not there before. `attach-only` never detaches; `off` just restarts the
services. The snapshot survives a server restart or a failed send, and is applied by `--recover` or the next restart.

Limits: only what `scsictl -l` shows is restored (ID, LUN, type, image file, write protect), not vendor/product names or non-default
block sizes. The output format of `scsictl -l` varies between PiSCSI versions, so check once with `s2ksend --piscsi` that your devices
are listed correctly before relying on it.

## If something goes wrong
After an error that may have written, the server leaves PiSCSI stopped and refuses work (`needs_attention`). Switch SCSI-MIDI off on the
S2000 if it is silent, power-cycle the S2000 and the Pi, then run `s2ksend --recover`. Nothing is retried automatically.

A failed read-back comparison is different: the transfer completed, the bus is fine and PiSCSI restarts normally; the output says how
to inspect the sample read-only. The exact audio sent, a timed transcript of every SCSI command and, on a mismatch, the expected
and read-back audio are kept per job under the server's `--data-dir`; `s2ksend --fetch-job ID` copies them to the client.

Samples live only in the S2000's RAM: save them to disk on the sampler to keep them.

| Symptom | Meaning and action |
| --- | --- |
| `409 busy` | A job or read is using the bus. Retry shortly. |
| `needs_attention` | See above: power-cycle both, then `s2ksend --recover`. |
| `already on the S2000: NAME` | Name clash. Use `--name` or delete the sample on the S2000. |
| `verify_failed` | Transfer completed, bus healthy. Inspect with `s2ksend --check SLOT file`. |
| `list_mismatch` | Samples were sent and verified but the S2000's list changed unexpectedly. Power-cycle, `--recover`, then `--list`. |
| `bus helper ... not found` | Run `sudo make install` (or `make` in a checkout). |
| Emulated disks missing after a job | Wait `--keep-bus` seconds, then check `--status` and `--piscsi`. |
| No answer from the S2000 | Another initiator or a PiSCSI device on the Pi's ID (0 by default), wrong `--target`, SCSI-MIDI left on after a failure, or the board is not in initiator mode. |

## Technical notes
* The S2000 stores a sent stereo-partner field of `0000` as `ffff` (none) and fills in the sample's internal start address itself, so
  those two header fields always differ on read-back and are reported as notes, not failures.
* Audio read-back requests count 16-bit words (frames), not bytes.
* Stereo partner fields hold the internal address of the other half's 12-byte table entry, with `address(R) = address(L) + 12`. The
  table base moves after loading and deleting, so the address cannot be worked out from the slot number; the S2000 sets it.
  The `-L` / `-R` suffix must be the last two of the 12 characters: `LOOP-L` style names are not linked.

## Development
    ./run_tests.sh                                        # simulator only, no hardware; needs sox
`tests/parity.py` compares 11 wire frames byte-for-byte with output from the reference C++ (`tests/parity.reference.out`);
`test_server.py` runs the server, client library and CLI against a simulated S2000, including the real bus-helper protocol through
`tests/fake_s2kexec.py`. CI runs the tests on Python 3.13 and 3.14, builds `s2kexec` and builds the package.

New SCSI behaviour belongs in `Session` or an `op_*` function in `s2kcore.py`, mirrored in `MockS2000` so rehearsals and tests stay
honest. New endpoints go in `s2kserver.py`, then `s2kclient.py`, then the CLI, then API.md.

| File | Role |
| --- | --- |
| `s2kcore.py` | Protocol, simulated S2000, bus and PiSCSI management, audio loading and naming |
| `s2kserver.py` | HTTP server and job queue |
| `s2kclient.py` | Client library, standard library only |
| `s2ksend.py` | CLI |
| `s2kexec.cpp`, `Makefile` | Bus helper: runs one raw SCSI command per request line, no Akai logic |
| `gpio-source/` | BSD-3-Clause initiator code (SCSI2Pi/PiSCSI-derived) |

## Provenance and licence
* s2k-send's own code is under the [MIT licence](LICENSE).
* `gpio-source/` and `s2kexec.cpp` are BSD-3-Clause (SCSI2Pi/PiSCSI-derived); keep `gpio-source/LICENSE` and `NOTICE` with them.
* The command sequence and encodings follow the S2000 Bridge release (michaelsanois/s2000-bridge) and the MESA II research
  (michaelsanois/mesa2-research), which independently documents the same sample-import sequence.
* `SAMPLE_TEMPLATE` in `s2kcore.py` is the 192-byte sample header template captured in that release. Those repositories carry no
  licence, so **until the author gives permission, treat this package as personal use only and do not redistribute it.**
