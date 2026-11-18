# s2kserver HTTP API (version 1)

`s2kserver.py` runs on the Raspberry Pi that has the PiSCSI board. Clients (the CLI, a GUI, scripts) talk to it over
plain HTTP + JSON on the home network. **There is no authentication or TLS: private network only.**
Default address `http://<pi>:8420`. All endpoints are under `/v1/`. CORS is open (`Access-Control-Allow-Origin: *`).

## Concepts
* **Audio** is raw **16-bit signed little-endian MONO PCM** at **44100 or 22050 Hz** (the S2000's two rates).
  Clients decide naming, stereo splitting (two samples, the S2000 convention `NAME      -L` / `NAME      -R`:
  stem padded to 10 characters, then `-L`/`-R` as the last two of 12), resampling and bit-depth conversion.
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
| `GET /v1/samples/{slot}` | `{slot, name, frames, rate, root, bandwidth, playback, partner, header_hex}`; `partner` is `null` when unpaired (field `0xffff`), else the raw 16-bit value |
| `GET /v1/samples/{slot}/audio?format=wav\|pcm` | the sample's audio (default WAV). Headers `X-S2K-Name`, `X-S2K-Rate`, `X-S2K-Frames`, `X-S2K-Root` |
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
   `GET /v1/jobs` lists recent jobs.

**Job object**
```json
{"id": "...", "state": "awaiting_audio|ready|queued|running|done|failed|cancelled",
 "samples": [{"name": "KICK", "rate": 44100, "frames": 17640, "uploaded": true}],
 "rehearsal": {"ok": true, "commands": 207},
 "progress": {"phase": "audio", "name": "KICK", "sample_index": 0, "done": 16384, "total": 35280},
 "log": [{"n": 0, "t": 1794901107.2, "msg": "..."}], "next": 12,
 "result": {"samples": [{"name": "KICK", "slot": 5}], "commands": 183, "audio_bytes": 651324, "elapsed_s": 9.8,
            "overall_kbps": 66.5, "write_kbps": 310.2, "verify_kbps": 205.0, "block": 16384},
 "error": {"code": "send_failed", "message": "...", "done": [{"name": "KICK", "slot": 5}], "not_completed": ["PAD"]}}
```
* `progress.phase` is one of `header`, `audio` (writing), `finalise`, `verify_header`, `verify_audio`; `done`/`total` are bytes.
* Pass `since=<next from the previous reply>` to receive only new log lines.
* `error.code` is one of `send_failed`, `verify_failed`, `needs_attention`, `rehearsal_failed`, `busy`, `internal_error`.
  `verify_failed` means the transfer completed and the bus is healthy, but a read-back comparison failed.
* After `failed`, `error.done` lists samples already in the S2000's RAM (with slots) and `not_completed` those that are
  not (the one being sent when it stopped may be partly there).

**SSE** (`/events`): events `log` (`{n, t, msg}`), `progress`, `state` (`{state}`), then `end` (`{result, error}`),
plus `: keep-alive` comments.

## Evidence kept on the Pi (`--data-dir`, default `./s2k-data/jobs/<id>/`)
`NAME.wav` is exactly the audio that was sent; `transcript.log` has every SCSI command; `NAME-expected.pcm` and
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
* one job per queue run, with a sample entry per queued file (the GUI supplies the PCM and rate it already prepared, and
  an optional per-sample `root`);
* `progress` and the log drive the progress UI;
* `GET /v1/samples/{slot}/audio` serves receiving.

A Python GUI can import `s2kclient.py` (standard library only) or speak HTTP directly. Deleting or renaming samples is not
implemented. Check AKAISDS's own licence before sharing code with it (its README says LGPLv3, GitHub's sidebar says GPL-3.0);
talking to the server over HTTP keeps the projects independent.
