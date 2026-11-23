# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `GET /v1/status` reports `bus.piscsi_restore` (`scheduled` with seconds to go, or `restoring`) and `s2ksend --status` says
  PiSCSI is kept stopped on purpose and when it restarts, rather than looking like a fault (found in AKAISDS's hardware test).
- Python packaging (`pyproject.toml`): `pip install` gives the `s2ksend` and `s2kserver` commands.
- `make install` puts the `s2kexec` bus helper on `PATH`; the server finds it there when installed as a package.
- MIT licence for the project's own code.
- GitHub Actions CI: tests on Python 3.13 and 3.14, `s2kexec` build, package build.
- This changelog.
- `GET /v1/jobs/{id}/files[/{name}]` and `s2ksend --jobs` / `--fetch-job ID`: fetch a job's transcript, the audio sent and
  any mismatch dumps from a client; a failed send prints the command.
- Each transcript line records when the command ran (`t=`) and how long it took (`dt=`).
- Any sample rate from 4000 to 65535 Hz, sent without resampling (`--rate native`, or any `rate` in the API): the header's
  bandwidth and pitch offset make it play at the right pitch, as AKAISDS does over MIDI. Downloads accept any such rate.
  `GET /v1/samples/{slot}` reports the pitch offset as `tune`.
- Download jobs with progress: `POST /v1/samples/{slot}/download`, followed like a send job, then
  `GET /v1/jobs/{id}/audio`. `s2ksend --download` uses them and shows progress; `s2kclient` has `download()`.
- Error code `list_mismatch` for a wrong sample list after a batch (reported as `verify_failed` in 0.8.0).

### Changed
- Sent samples have playback type 2 (No looping), as one sent over MIDI SDS does; it was 3 (One-shot) (found in AKAISDS's
  hardware test, 20/11/2026).
- The rehearsal line says the rehearsal runs on a simulated S2000 before the real one is touched (it read as mock mode in a
  real run), and the overall KB/s note says it includes set-up and is low for small files (AKAISDS's hardware test).
- Requires Python 3.13 or later (current Raspberry Pi OS); older releases are not supported.
- `s2kserver --data-dir` defaults to `/var/lib/s2k-send`, as the systemd unit uses, so jobs and the PiSCSI snapshot are
  shared between a server started by hand and the service (`./s2k-data` with `--mock`).
- After each sample in a batch the sample list is read (about 5 commands) to find its slot; programs and free space are
  still read only before and after the batch.
- `--list` says "headers" rather than "sample headers": the S2000's count includes more than samples.
- `run_tests.sh` exits non-zero when a test fails.
- The systemd unit runs the installed `s2kserver` command.

### Fixed
- A batch sent after a sample was deleted on the S2000 failed `verify_failed`: the S2000 puts a new sample in the first
  free header entry, not at the end of the list, and moves the samples after it up one. Batch mode now finds each new
  sample by name, and reported slots are the final ones (found on hardware, 20/11/2026).
- `s2ksend` no longer prints a `BrokenPipeError` traceback when its output is piped into `head`.
- Sample memory is counted in even numbers of words, as the S2000 allocates it, so a batch that does not fit is refused
  before anything is written instead of failing part-way.
- A sample list that is wrong after a batch has written now leaves the bus in `needs_attention` instead of idle.
- The job log no longer skips lines logged just after a job starts, and the CLI always shows why a job failed.
- Stereo pairs are reported as linked by the same rule as `--pairs` (both partner fields set), not only when their
  table entries are adjacent.
- A read no longer waits up to 60 s when the keep-bus restart timer fires during a send.

### Removed
- `s2kcore.RATES`: use `s2kcore.valid_rate()`.
- `tests/test_physical.py`, which targeted the pre-0.7 single-file tool. Its 32 KB block case moved to `tests/test_server.py`.

## [0.8.0] - 2026-11-18

### Added
- PiSCSI setup retention: the attached devices are saved before PiSCSI is stopped and restored after it restarts
  (`--restore-piscsi exact|attach-only|off`), with the snapshot kept in `<data-dir>/piscsi-snapshot.json`.
- `--piscsi` to show the PiSCSI state, attached devices and the saved setup.
- Stereo link report in job results (`linked` / `NOT linked`).

### Changed
- The sample list is read once before and once after a batch instead of after every sample (about 15 fewer commands per sample).
- A read that arrives while the keep-bus timer is restarting PiSCSI now waits instead of returning `409 busy`.

### Fixed
- Duplicate "rehearsal OK", "All done" and failure lines in the CLI output.
- A harmless early failure could leave PiSCSI stopped; only audio, header and finalise messages now count as writes.

## [0.7.0] - 2026-11-17

### Added
- Split into a server (`s2kserver.py`, on the Pi), a client library (`s2kclient.py`) and a CLI (`s2ksend.py`) over an HTTP/JSON API
  with polling and SSE (see `API.md`).
- Keep-bus handling: PiSCSI stays stopped for `--keep-bus` seconds after an operation.
- `--mock` server mode backed by the simulated S2000.

Hardware: a 3-sample batch (687 KB, 201 commands, 11.06 s) sent from another computer and verified; the S2000 linked
`LOOP      -L` / `LOOP      -R` as a stereo pair by itself.

## [0.6.0] - 2026-11-16

### Added
- Batches of several files, 22.05 kHz sends, `--block`, a speed report.
- Stereo halves named the way the S2000 names its own pairs (`-L` / `-R` as the last two of 12 characters).
- `--pairs` to show stereo pairs and their partner fields.

## [0.5.0] - 2026-11-16

### Added
- Stereo WAVs sent as two mono samples. Hardware: `loop.wav` (3.69 s) sent and verified byte-for-byte.

## [0.4.0] - 2026-11-16

### Fixed
- sox dither disabled (`sox -D`) so conversions are reproducible; the earlier audio mismatch came from dither, not the transfer.

### Added
- The exact audio sent is saved, and an audio mismatch writes expected/read-back dumps with a diagnosis.
  First fully verified mono send on hardware.

## [0.3.0] - 2026-11-16

### Fixed
- Audio read-back requests count 16-bit words (frames), not bytes.

## [0.2.0] - 2026-11-16

### Changed
- Header read-back differences are shown as notes; only name, length or rate fail the check.

### Added
- Read-only `--check`.

## [0.1.0] - 2026-11-16

### Added
- First single-file sender. Its header read-back check was too strict and reported a false failure.

[Unreleased]: https://github.com/neufena/s2k-send/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/neufena/s2k-send/releases/tag/v0.8.0
