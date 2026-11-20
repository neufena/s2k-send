# s2kserver HTTP API (version 1)

`s2kserver` runs on the Raspberry Pi that has the PiSCSI board. Clients (the CLI, a GUI, scripts) talk to it over
plain HTTP + JSON on the home network. **There is no authentication or TLS: private network only.**
Default address `http://<pi>:8420`. All endpoints are under `/v1/`. CORS is open (`Access-Control-Allow-Origin: *`).

## Concepts
* **Audio** is raw **16-bit signed little-endian MONO PCM** at any integer `rate` from **4000 to 65535 Hz**. The S2000
  plays at 22050 or 44100 Hz; for any other rate the server sets the header's bandwidth to the nearer of the two and a
  pitch offset (`12 x log2(rate / engine rate)` semitones) so the sample plays at the right pitch. Only 44100 and 22050
  are proven on hardware so far.
  Clients decide naming, stereo splitting (two samples, the S2000 convention `NAME      -L` / `NAME      -R`:
  stem padded to 10 characters, then `-L`/`-R` as the last two of 12, sent L then R: the S2000 then links them as a stereo pair itself), resampling and bit-depth conversion.
  `s2kclient.py` plus `s2kcore.load_audio` / `s2kcore.plan_items` do this for Python clients.
* **Names**: up to 12 characters from `0-9`, space, `A-Z`, `#`, `+`, `-`, `.`. The server rejects anything else.
* **Slots** are 0-based positions in the S2000's sample list. New samples are appended.
* **One bus, one operation at a time.** While an operation runs PiSCSI is stopped (the Pi's emulated disks
  disappear) and restarted `--keep-bus` seconds after the last operation (default 20 s), with its attached devices put back as they were. Read endpoints answer
  `409 busy` if the bus is in use; send jobs wait their turn.
* **`needs_attention`**: if a send fails after something was written, the server deliberately leaves PiSCSI stopped and
  refuses work (`409 needs_attention`) until the S2000 and the Pi have been power-cycled and `POST /v1/recover` is called.
* **Errors** are `{"error": {"code": "...", "message": "..."}}` with an HTTP status (400 bad input, 404 not found,
  409 busy / wrong state / needs attention, 413 too big, 502 bus error, 500 internal).

## Server and bus
| Endpoint | Reply |
|---|---|
| `GET /v1/status` | `{server, version, api, bus: {state: idle\|busy\|needs_attention, piscsi, target, initiator, mock, keep_bus_s, piscsi_snapshot, last_restore}, jobs: {current, queued}}` (`last_restore`: `{restored, attached, detached, failed, note}` from the most recent PiSCSI restart) |
| `GET /v1/piscsi` | `{state, restore_mode, devices: [{id, lun, type, file, protected}] \| null, snapshot: {taken, pending, devices} \| null, snapshot_devices}`: the PiSCSI service state, its attached devices (read from `scsictl -l`; `null` when stopped or unmanaged) and the setup the server saved before stopping it |
| `POST /v1/probe` | INQUIRY of the S2000: `{status, bytes, vendor, product}` |
| `POST /v1/recover` | after a failed send **and a power-cycle**: restart PiSCSI, accept work again |

## Reading the sampler (read-only; each call takes the bus briefly)
| Endpoint | Reply |
|---|---|
| `GET /v1/samples` | `{samples: [{slot, name}], programs: [name], free_headers, total_headers, free_words, total_words}` (memory in 16-bit words; bytes = x2) |
| `GET /v1/samples/{slot}` | `{slot, name, frames, rate, root, bandwidth, tune, playback, partner, header_hex}` (`tune`: pitch offset in semitones); `partner` is `null` when unpaired (field `0xffff`), else the raw 16-bit value |
| `GET /v1/samples/{slot}/audio?format=wav\|pcm` | the sample's audio (default WAV), in one blocking request with no progress. Headers `X-S2K-Name`, `X-S2K-Rate`, `X-S2K-Frames`, `X-S2K-Root`. For progress, use a download job (below) |
| `GET /v1/pairs` | `{samples, pairs: [{left: {slot, name, partner}, right: {...}, linked}], rule: {stride, base, consistent, notes} \| null, layout: {pairs, spacing, spacing_consistent, holes: [{before_slot, entries}], extra_entries, first_address, last_address} \| null}`: finds `-L`/`-R` pairs, decodes their stereo-partner fields, and reports the entry spacing and gaps those addresses imply |

## Sending: jobs
A job is a batch of samples sent in one go. Create it, upload the audio (the server then rehearses the whole batch on a
simulated S2000), then start it.

1. `POST /v1/jobs` with JSON
   ```json
   {"samples": [{"name": "KICK", "rate": 44100, "frames": 17640, "root": 36},
                {"name": "PAD", "rate": 22050, "frames": 44100}],
    "root": 60, "verify": true, "block": 16384}
   ```
   `root` (0-127; per sample optional, default 60), `verify` (read every sample back and compare; default true),
   `block` (bytes per audio write: 8192, 16384 (default, proven on hardware) or 32768). Replies `201` with the job
   (`state: awaiting_audio`) and `upload: [urls]`.
2. `PUT /v1/jobs/{id}/samples/{n}` with the raw PCM as the body (`application/octet-stream`, exactly `frames x 2` bytes).
   When the last sample arrives the server rehearses the batch and the reply shows `state: ready` (or `failed` with
   `error.code = rehearsal_failed`).
3. `POST /v1/jobs/{id}/start` returns `202`, `state: queued`, then `running`.
4. Follow it: `GET /v1/jobs/{id}?since=N` (poll) or `GET /v1/jobs/{id}/events` (Server-Sent Events).
5. `DELETE /v1/jobs/{id}` cancels a job that is not yet running (a running transfer cannot be stopped safely).
   `GET /v1/jobs` lists recent jobs (the last 50 held in memory).
6. `GET /v1/jobs/{id}/files` lists the evidence the server kept: `{id, files: [{name, bytes}]}`, and
   `GET /v1/jobs/{id}/files/{name}` returns one file (`name` URL-encoded as a single path segment). Files are
   `NAME.wav` (the exact audio sent), `transcript.log` (every SCSI command, with `t=` seconds since the session started
   and `dt=` the command's duration) and, after a failed comparison, `NAME-expected.pcm` / `NAME-readback.pcm`.
   This works for older jobs too, as long as their folder is still in `--data-dir`. Unknown jobs or files give `404`
   (`no_such_job` / `no_such_file`).

**Job object**
```json
{"id": "...", "state": "awaiting_audio|ready|queued|running|done|failed|cancelled",
 "samples": [{"name": "KICK", "rate": 44100, "frames": 17640, "uploaded": true}],
 "rehearsal": {"ok": true, "commands": 207},
 "progress": {"phase": "audio", "name": "KICK", "sample_index": 0, "done": 16384, "total": 35280},
 "log": [{"n": 0, "t": 1794901107.2, "msg": "..."}], "next": 12,
 "result": {"samples": [{"name": "KICK", "slot": 5}], "pairs": [{"left": "LOOP      -L", "right": "LOOP      -R", "left_slot": 6, "right_slot": 7, "linked": true, "left_partner": 24672, "right_partner": 24660}], "commands": 183, "audio_bytes": 651324, "elapsed_s": 9.8,
            "overall_kbps": 66.5, "write_kbps": 310.2, "verify_kbps": 205.0, "block": 16384},
 "error": {"code": "send_failed", "message": "...", "done": [{"name": "KICK", "slot": 5}], "not_completed": ["PAD"]}}
```
* `result.pairs` lists each `-L`/`-R` pair in the batch and whether the S2000 linked it (it does this itself when the `-R` arrives after the `-L`; both partner fields are read back to check).
* `progress.phase` is one of `header`, `audio` (writing), `finalise`, `verify_header`, `verify_audio`; `done`/`total` are bytes.
* Pass `since=<next from the previous reply>` to receive only new log lines.
* `error.code` is one of `send_failed`, `verify_failed`, `list_mismatch`, `needs_attention`, `rehearsal_failed`, `busy`, `internal_error`.
  `verify_failed` means the transfer completed and the bus is healthy, but a read-back comparison failed.
  `list_mismatch` means every sample was sent and verified, but the S2000's lists afterwards did not match what was
  expected; its state is uncertain, so the bus is left in `needs_attention`. Do not retry: power-cycle, recover, and
  check the list.
* After `failed`, `error.done` lists samples already in the S2000's RAM (with slots) and `not_completed` those that are
  not (the one being sent when it stopped may be partly there).

## Receiving with progress: download jobs
1. `POST /v1/samples/{slot}/download` returns `202` with a job (`kind: download`, `slot`, `state: queued`). It is
   read-only, so there is no upload, rehearsal or start step.
2. Follow it exactly like a send job (`GET /v1/jobs/{id}?since=N` or `/events`). `progress` is
   `{phase: "download", done, total}` in bytes while the audio is read.
3. When `state` is `done`, `result` is the sample's details (`slot, name, frames, rate, root, bandwidth, tune, playback,
   partner`) plus `bytes, elapsed_s, read_kbps`, and `GET /v1/jobs/{id}/audio?format=wav|pcm` returns the audio (same
   headers as above; `409 wrong_state` before it is done, `400 not_a_download` for a send job).
4. On failure `error.code` is `not_found` (no sample in that slot), `read_failed`, `verify_failed` (the S2000
   advertised a different size), `needs_attention` or `busy`.

Every job (send or download) has `kind` and `slot` (`null` for sends); `GET /v1/jobs` lists both kinds.
The server keeps the last 50 finished jobs in memory (unfinished ones are never dropped). When a download job is dropped,
its folder on the Pi is deleted too, so fetch its audio soon after it finishes; send evidence is kept.

**SSE** (`/events`): events `log` (`{n, t, msg}`), `progress`, `state` (`{state}`), then `end` (`{result, error}`),
plus `: keep-alive` comments.

## Evidence kept on the Pi (`--data-dir`, default `/var/lib/s2k-send/jobs/<id>/`)
`NAME.wav` is exactly the audio that was sent (for a download job, the audio that was read); `transcript.log` has every SCSI command; `NAME-expected.pcm` and
`NAME-readback.pcm` appear if a read-back comparison failed.

## Example (curl)
```
curl http://piscsi.local:8420/v1/samples
curl -X POST http://piscsi.local:8420/v1/jobs -d '{"samples":[{"name":"KICK","rate":44100,"frames":17640}]}'
curl -X PUT  http://piscsi.local:8420/v1/jobs/$ID/samples/0 --data-binary @kick.pcm
curl -X POST http://piscsi.local:8420/v1/jobs/$ID/start
curl "http://piscsi.local:8420/v1/jobs/$ID?since=0"
```

## Mapping to a GUI such as AKAISDS
AKAISDS (Python/Qt) already has a batch queue, a sample browser with a free-memory bar and per-sample rate settings. This API
maps onto that directly:
* the sample browser and memory bar use `GET /v1/samples`;
* one job per queue run, with a sample entry per queued file (the GUI supplies the PCM at its own rate, no resampling
  needed, and an optional per-sample `root`);
* `progress` and the log drive the progress UI;
* receiving uses a download job (`POST /v1/samples/{slot}/download`) for a progress bar, or
  `GET /v1/samples/{slot}/audio` when none is needed.

A Python GUI can import `s2kclient.py` (standard library only) or speak HTTP directly. Deleting or renaming samples is not
implemented. Check AKAISDS's own licence before sharing code with it (its README says LGPLv3, GitHub's sidebar says GPL-3.0);
talking to the server over HTTP keeps the projects independent.
