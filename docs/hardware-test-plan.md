# Hardware test plan

Everything below is simulator-tested only. Run it on the real Pi + PiSCSI + S2000 in this order: read-only checks first,
then sends. Record each result in the table at the end, with the job ID for any send (`s2ksend --jobs`).

After any failed send, before anything else: `s2ksend --fetch-job ID` to keep the transcript and the audio sent.

## Before starting
* Any other initiator on the bus (the Mac) powered off or disconnected, and SCSI ID 0 (the Pi's) reserved in PiSCSI.
* Nothing on the S2000 that needs saving: samples are added to RAM, and a failure may need a power-cycle.
* Test files on the client: `kick.wav` (mono), `loop.wav` (stereo), and about 15 short mono WAVs for the large batch.
* Note the devices PiSCSI has attached (web UI) so they can be compared after each restart.

## 1. Install (README steps)
On the Pi, follow **Install > Server** exactly: `sudo make install`, the venv, `pip install .`, then start
`sudo /opt/s2k-send/bin/s2kserver`. On the client, `pipx install` (or `pip install .`).

Expect: no errors; `s2ksend --status` shows the server version and `bus: idle`. Note anything in the README that was
wrong or missing.

## 2. Read-only
| Command | Expect |
| --- | --- |
| `s2ksend --probe` | Vendor and product of the S2000 |
| `s2ksend --piscsi` | The same devices as the PiSCSI web UI, with correct IDs, types and image files |
| `s2ksend --list` | The samples on the S2000, free memory |
| `s2ksend --pairs` | Any stereo pairs, `linked` |

## 3. PiSCSI restore and the keep-bus wait
1. Run `s2ksend --list` (this stops PiSCSI), then immediately:
   `for i in $(seq 40); do s2ksend --list >/dev/null && echo "$i ok" || echo "$i FAILED"; sleep 1; done`
2. Expect every line `ok`: reads made while the keep-bus timer restarts PiSCSI (about 20 s in) wait instead of failing.
3. About 30 s after the last command: `s2ksend --status` shows the last restore with nothing failed, and `s2ksend --piscsi`
   and the web UI show the same devices as before.

## 4. Mono at 44.1 kHz, both block sizes
1. `s2ksend kick.wav --name KICKA --yes` (block 16384)
2. `s2ksend kick.wav --name KICKB --block 32768 --yes`
3. Expect both `audio verified byte-for-byte`. Record the `audio writes` and `verify reads` KB/s of each.
4. `s2ksend --fetch-job ID` for one of them; check `transcript.log` has `t=` and `dt=` on every command (typical `dt`
   is expected to be about 50 ms).

## 5. 22.05 kHz
1. `s2ksend kick.wav --name KICK22 --rate 22050 --yes`
2. `s2ksend --check SLOT kick.wav` (slot from step 1): bandwidth 0, rate 22050.
3. Play KICKA and KICK22 on the S2000: **same pitch**, KICK22 duller (half the bandwidth).

## 5b. Other sample rates (no resampling)
Make test tones at 32 kHz, 11.025 kHz and 48 kHz (`sox -n -r 32000 -b 16 t32.wav synth 2 sine 440`, and likewise), then
send each with `--rate native`: `s2ksend t32.wav --rate native --name T32 --yes`.
1. Expect `audio verified byte-for-byte` and no critical header notes (the pitch offset is checked on read-back).
2. `s2ksend --check SLOT t32.wav`: rate 32000, bandwidth 1, pitch offset -5.55 (11025: bandwidth 0, -12.00; 48000:
   bandwidth 1, +1.47).
3. Play each on the S2000 against a 44.1 kHz 440 Hz tone: **all the same pitch**.
4. `s2ksend --download SLOT t32back.wav` and check the WAV is 32 kHz.

## 6. Large batch, and the slot-order assumption
Batch mode assumes the S2000 adds each new sample at the end of its list, in the order sent.
1. Send about 15 short files in one run. Expect every sample verified and the final list check to pass.
2. Delete one sample in the **middle** of the S2000's list (on the S2000 itself), then send a batch of 3 more.
3. Expect the batch to pass, with the first new sample in the deleted sample's slot (the S2000 fills the free header
   entry and moves the later samples up one) and the reported slots matching `s2ksend --list`. If it fails, record
   which slots the new samples took and fetch the job.

## 7. Stereo linking
1. `s2ksend loop.wav --name LOOP2 --yes`: expect `linked by the S2000` (the known-good case, as a baseline).
2. **R before L**: make mono halves (`sox loop.wav left.wav remix 1`, `sox loop.wav right.wav remix 2`), then
   `s2ksend right.wav --name 'LOOP3     -R' --yes` followed by `s2ksend left.wav --name 'LOOP3     -L' --yes`.
3. **Something in between**: `s2ksend left.wav --name 'LOOP4     -L' --yes`, `s2ksend kick.wav --name KICKX --yes`,
   `s2ksend right.wav --name 'LOOP4     -R' --yes`.
4. For each, `s2ksend --pairs`: record whether LOOP3 and LOOP4 are linked, and their partner fields.

## 8. Download
`s2ksend --download SLOT out.wav` for KICKA and one LOOP2 half: progress lines up to 100%, then `s2ksend --check SLOT out.wav`
on each: audio matches. Record the read KB/s that `--download` prints for the larger one.

## 9. Service
Install the systemd unit (header of `s2k-server.service`), reboot the Pi, then `s2ksend --status` from the client.

## 10. AKAISDS
Run AKAISDS's own plan (`tests/s2k_test_plan.md` in that repo) against the same server.

## Results (20/11/2026)
Server and client at `7e773c9` (0.9.0.dev0). Client ran from the devcontainer, over the Pi's IP.

| Step | Result | Job ID / numbers | Notes |
| --- | --- | --- | --- |
| 1 Install | Pass | | Server from README on the Pi. Client in a devcontainer venv (`pip install .`), not pipx: host Python is 3.12. mDNS (`piscsi.local`) doesn't resolve in the container, so `S2K_SERVER` used the IP. |
| 2 Read-only | Pass | | First `--probe` failed (`no response from target`) because the S2000 had not booted properly; fine after a restart. 4 default samples, 1 program, 33.4 MB free. `--list` reports 2 more headers in use than samples listed (6 vs 4 at the start). |
| 3 PiSCSI restore / keep-bus | Pass | | 40/40 `--list` ok over about 40 s. Restore put back all 3 devices, including one added in the web UI before the run. |
| 4 Block 16384 / 32768 | Pass | KICKA `20261120-133457-212aee`, KICKB `20261120-133500-957c1f`; write KB/s: 187.6 / 240.6; verify KB/s: 335.7 / 338.0 | Both verified byte-for-byte and play. Transcript has `t=` and `dt=` on all 63 commands; dt median 40.6 ms, max 180.7 ms. |
| 5 22.05 kHz | Pass | KICK22 in slot 6; write 154.0 KB/s, verify 235.8 KB/s | `--check`: bandwidth 0, 22050 Hz. Same pitch as KICKA, duller. |
| 5b Other rates (32k / 11.025k / 48k) | Pass | pitch offsets: -5.55 / -12.00 / +1.47 | Verified byte-for-byte; bandwidth 1 / 0 / 1. Same pitch as 44.1 kHz by ear. `--download` of T32 gave a 32 kHz WAV (75.4 KB/s). |
| 6 Large batch / gap | 15-file batch pass; **gap case fails** | batch `20261120-135036-bedd20`; failed `20261120-135451-9a01db` | 15 samples in slots 10-24, in order, 22.5 s. After deleting B05 (slot 14) on the S2000, the first of 3 new samples took slot 14 and moved the later ones up one. The batch expected it at the end, read the wrong header and failed `verify_failed`. N01 correct at slot 14; N02/N03 not sent; bus idle. The S2000 fills the gap, so batch slots are not "append in order". |
| 7 Stereo R-before-L / in between | Pass | | LOOP2, LOOP3 (R before L) and LOOP4 (KICKX between) all linked. Partner field = 0x6018 + 12 x partner slot for all three. |
| 8 Download | Pass | KICKA 22.4 KB/s; LOOP2 -L 144.4 KB/s (326 KB) | Progress to 100%; `--check` audio matches. |
| 9 Service | Pass | | Unit enabled, active, NRestarts=0, listening 1 s after start. PiSCSI restore after the reboot works. A device added in the web UI is not there after the reboot (PiSCSI's own startup config). `--jobs` empty: the service uses a new `--data-dir`. |
| 10 AKAISDS | | | |

Also seen: `--list` and `--jobs` print a `BrokenPipeError` traceback when piped into `head`.
