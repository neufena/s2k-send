# Hardware test plan

Everything below is simulator-tested only. Run it on the real Pi + PiSCSI + S2000 in this order: read-only checks first,
then sends. Record each result in the table at the end, with the job ID for any send (`s2ksend --jobs`).

After any failed send, before anything else: `s2ksend --fetch-job ID` to keep the transcript and the audio sent.

## Before starting
* Any other initiator on SCSI ID 7 (the Mac) powered off or disconnected.
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

## 6. Large batch, and the slot-order assumption
Batch mode assumes the S2000 adds each new sample at the end of its list, in the order sent.
1. Send about 15 short files in one run. Expect every sample verified and the final list check to pass.
2. Delete one sample in the **middle** of the S2000's list (on the S2000 itself), then send a batch of 3 more.
3. Expect the batch to pass. If it fails with `list_mismatch` or a header check on the wrong slot, the S2000 filled the
   gap rather than appending: record which slots the new samples took (`s2ksend --list`) and fetch the job.

## 7. Stereo linking
1. `s2ksend loop.wav --name LOOP2 --yes`: expect `linked by the S2000` (the known-good case, as a baseline).
2. **R before L**: make mono halves (`sox loop.wav left.wav remix 1`, `sox loop.wav right.wav remix 2`), then
   `s2ksend right.wav --name 'LOOP3     -R' --yes` followed by `s2ksend left.wav --name 'LOOP3     -L' --yes`.
3. **Something in between**: `s2ksend left.wav --name 'LOOP4     -L' --yes`, `s2ksend kick.wav --name KICKX --yes`,
   `s2ksend right.wav --name 'LOOP4     -R' --yes`.
4. For each, `s2ksend --pairs`: record whether LOOP3 and LOOP4 are linked, and their partner fields.

## 8. Download
`s2ksend --download SLOT out.wav` for KICKA and one LOOP2 half, then `s2ksend --check SLOT out.wav` on each: audio matches.

## 9. Service
Install the systemd unit (header of `s2k-server.service`), reboot the Pi, then `s2ksend --status` from the client.

## 10. AKAISDS
Run AKAISDS's own plan (`tests/s2k_test_plan.md` in that repo) against the same server.

## Results (20/11/2026)
| Step | Result | Job ID / numbers | Notes |
| --- | --- | --- | --- |
| 1 Install | | | |
| 2 Read-only | | | |
| 3 PiSCSI restore / keep-bus | | | |
| 4 Block 16384 / 32768 | | write KB/s: / ; verify KB/s: / | |
| 5 22.05 kHz | | | |
| 6 Large batch / gap | | | |
| 7 Stereo R-before-L / in between | | | |
| 8 Download | | | |
| 9 Service | | | |
| 10 AKAISDS | | | |
