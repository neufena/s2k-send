# Contributing

Thanks for your interest. This is a small hobby project, so replies may be slow.

## Scope
s2k-send sends samples to, and receives them from, an Akai S2000 over SCSI. Deleting, renaming, loop points, tuning, programs
and keygroups are out of scope; do those over MIDI. Please open an issue to discuss a larger change before writing it.

## Setup
Needs Python 3.13+, `sox` and a C++ compiler. No hardware is needed: the tests use a simulated S2000.

    ./run_tests.sh      # all tests
    make                # builds the s2kexec bus helper

`s2kserver --mock` runs a simulated S2000 for developing clients.

## Pull requests
* Branch from `main` and open a PR; `main` is protected and needs CI to pass.
* Keep PRs small and focused, with a short list of the changes in the description.
* Add or update tests, and add an entry under `[Unreleased]` in `CHANGELOG.md`.
* Do not commit secrets, hostnames or recordings of your own network, or samples you do not have the right to share.
* Hardware changes: say what you tested on (Pi model, PiSCSI board, S2000 firmware). Untested hardware paths are marked as such
  in the README.

## Licensing
By contributing you agree your work is under the project's MIT licence. Material taken from other projects must be credited in
`NOTICE`, and must be compatible with the licence or have its author's permission.
