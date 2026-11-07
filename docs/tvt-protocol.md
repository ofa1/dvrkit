# TVT NVMS-9000 web protocol notes (clean-room)

Observed on Hawk Vision HV-D5100-8HL (rebadged TVT), firmware 1.3.8.55236B221125, web resources `v=20210528.01`.
Derived from the device's own web UI scripts (read for interoperability) and live captures. No TVT code is reproduced here.

## 1. Session (HTTP XML API)

- `POST /<cmd>` with body:
  `<?xml version="1.0" encoding="utf-8" ?><request version="1.0" systemType="NVMS-9000" clientType="WEB">…</request>`
- Session cookie: `Cookie: sessionId=<id>`.
- `doLogin`: `<content><userName><![CDATA[u]]></userName><password><![CDATA[base64(pw)]]></password></content>`.
  The response has `<sessionId>{GUID}</sessionId>`. The web UI strips the braces before using it.
- `doLogout`: `<content><sessionId><![CDATA[{GUID}]]></sessionId></content>`.
- Lockout: 5 failed logins gives `536870951` (user locked).
- Error codes:
  - `536870943`: errorParam.
  - `536871011`: session invalid or expired. Also returned by the WebSocket `create_connection` when the session id still has its braces.

## 2. Recording search (HTTP XML API)

- `queryChlsExistRec` (empty request): which channels have recordings.
- `queryDatesExistRec`: `<condition><chlId>{00000001-0000-0000-0000-000000000000}</chlId><chlId>{00000002-0000-0000-0000-000000000000}</chlId>…</condition>`.
  Returns `content>item` entries, one `YYYY-MM-DD` per date with recordings on any requested channel. This is a union calendar, not per-channel availability.
- `queryChlRecLog`: returns recording segments for one channel over a start/end range; per-camera recording hours are calculated from these segments, not inferred from the union calendar. DVRKit batches adjacent uncached calendar dates into one range, then clips copies to each day's `00:00:00`–`23:59:59` bounds.
  - Times are in device-local time, format `YYYY-MM-DD HH:mm:ss`.
  - The `recType` list in `<condition>` is **required**; without it the device answers errorParam.
  - Request body:
  ```xml
  <types><recType><enum>MOTION</enum><enum>SCHEDULE</enum><enum>SENSOR</enum><enum>MANUAL</enum><enum>INTELLIGENT</enum><enum>POS</enum></recType></types>
  <requireField><chl/><recList><item><recType/><startTime/><endTime/><size/></item></recList></requireField>
  <condition>
    <startTime>2026-11-06 00:00:00</startTime><endTime>2026-11-06 23:59:59</endTime>
    <recType type='list'><itemType type='recType'/><item>MANUAL</item><item>SCHEDULE</item><item>MOTION</item><item>SENSOR</item></recType>
    <chl id='{00000002-0000-0000-0000-000000000000}'></chl>
  </condition>
  ```
  - Response: `content>recList>item` entries, each with `recType`, `startTime`, `endTime` and `size`.
  - Recording event types (the `recType` items): `MANUAL SCHEDULE MOTION SENSOR TRIPWIRE INVADE FACEDETECTION POS FACEMATCH AOIENTRY AOILEAVE VEHICLE`.

## 3. Media WebSocket

- URL: `ws(s)://<host>/requestWebsocketConnection?sessionID=<GUID without braces>`. Binary type: arraybuffer.
- Text frames are JSON: `{url, basic:{ver:"1.0", time, id, nonce}, data:{…}}`. Responses use the URL `<url>#response`.
- The server first sends `/device/create_connection#response` with `data.code`. A code of 0 means OK; only then does the client send its command.

### Commands (client to device)

| url | data |
|---|---|
| `/device/preview/open` | `task_id` (`{GUID}`), `channel_id` (`{GUID}`), `stream_index` (live: 2 = sub), `audio` (bool) |
| `/device/preview/close` | `task_id` |
| `/device/playback/open` | `task_id`, `channel_id`, `start_time` and `end_time` (**UTC epoch seconds**), `stream_index` (1 in the web UI = main), `type_mask` (array of event names, e.g. `["manual","schedule","motion","sensor"]`) |
| `/device/playback/refresh_play_index` | `task_id`, `play_frame_index` (**flow control**, see below) |
| `/device/playback/key_frame`, `/device/playback/all_frame` | `task_id`, `frame_time` (`yyyy-MM-dd HH:mm:ss:S`, UTC). Switches between sending key frames only (fast speeds) and all frames. |
| `/device/playback/audio/open`, `/audio/close`, `/device/preview/audio/open`, `/close` | `task_id` |
| `/device/playback/close` | `task_id` |

- **Seeking:** the web UI closes the task and opens a new one at the new `start_time`.
- **Lowercase event names for `type_mask`:** `manual schedule motion sensor gsensor shelter overspeed overbound osc avd tripwire perimeter vfd pos smart_motion face_verity cpc cdd ipd smart_aoi_entry smart_aoi_leave smart_pass_line smart_plate_verity`.
- **Flow control:**
  - Playback stops after about 15 frames unless the client acknowledges.
  - The web UI sends `refresh_play_index` every 8 decoded frames, with the frame index taken from each packet's header (`u32 @36`, see below).
- **End of recording:** the device sends error code `536870961` on `/device/playback/data`.
- **Other error codes:** `536870942` no record, `536870945` and `536870982` device busy, `536870931` and `536870935` offline, `536870953` no permission.

### Binary frame (device to client)

```
u32le w0              ; 0 -> header length 8, otherwise 16
u32le jsonLen
[header padding to 8 or 16 bytes]
JSON (jsonLen bytes): {"url":"/device/playback/data"|"/device/preview/data",
                       "data":{"task_id":…, "playback_stream_data"|"live_stream_data":"<offset>,<len>"}}
payload = bytes[header + jsonLen + offset : … + len]
```

### Payload: "SHFL" media packet (all integers little-endian)

| Offset | Size | Meaning |
|---|---|---|
| 0 | 4 | magic `SHFL` |
| 4 | 1 | `0x03` (version?) |
| 5 | 1 | `0x01` |
| 6 | 1 | stream kind: `2` live, `7` playback (video). Audio not yet captured. |
| 7 | 1 | key frame (1/0) |
| 24 | 4 | length of the data after the 44-byte base header |
| 28 | 8 | frame time, Windows FILETIME (100 ns since 1601), **UTC** |
| 36 | 4 | live: millisecond tick; playback: **frame index** (1, 2, 3…, used for `refresh_play_index`) |
| 48 | 4 | elementary stream length (`esLen`) |
| 52, 60 | 8 + 8 | further FILETIMEs (not yet decoded) |
| 68 | 12 | key frames only: codec block. Bytes 68-71 are zero, 72-75 hold ASCII `HEVC` (presumably `H264` for H.264), then u16 width at 76 and u16 height at 78 |

- **Elementary stream:** the last `esLen` bytes of the packet, i.e. `payload[len - esLen:]`. They start at 68 for non-key frames and at 80 for key frames.
- **Format:** plain Annex-B (start code `00 00 00 01`), not encrypted. Key frames carry VPS/SPS/PPS in-band.

## 4. Compatibility observations

- Live substream and recorded main stream were captured as HEVC on the tested recorder. A restricted account with search and playback permissions could search and play recordings.
- The recorder reported eight analog channels and `playbackMaxWin` 4. Four concurrent playback tasks opened successfully; a fifth returned error `536870982`. Other devices may have different limits.

## 4a. Permissions

- Remote search and playback need `@spr` on each channel in the user's permission group (set with `editAuthGroup`).

## 5. Open items

- Audio packet format (`audio:true` and `/playback/audio/open`).
- Whether the codec tag reads `H264` once the substreams are switched to H.264.
- Behaviour of `key_frame`/`all_frame` at speeds above 1x; tick and timestamp semantics in the other FILETIME fields.
- Whether the four-task limit applies independently to separate login sessions.
- Device time zone: `queryChlRecLog` uses device-local time while playback uses UTC epoch; the integration needs the device's zone (query not yet identified; the PoC takes it as an argument).
