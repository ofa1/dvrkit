# DVRKit

DVRKit is a Home Assistant custom integration for live viewing, searching and playing DVR recordings without transcoding or downloading recordings first. Its first backend uses the TVT NVMS-9000 web protocol. Recordings remain on the DVR; video is repackaged in memory for playback.

## Supported hardware

The backend has been exercised with a Hawk Vision HV-D5100-8HL (rebadged TVT), firmware 1.3.8.55236B221125. Other TVT NVMS-9000 devices may work but have not been verified. The tested recorder allowed four simultaneous recorded playback tasks; limits can differ by device. Live preview uses a separate task.

## Install DVRKit

### HACS custom repository

1. In HACS, add `https://github.com/ofa1/dvrkit` as a custom repository of type **Integration**.
2. Download DVRKit and restart Home Assistant.
3. Under **Settings → Devices & services**, add DVRKit. Enter the DVR host, HTTP port, account, device timezone and channel count. The account needs remote search and playback permissions for the relevant channels.

Alternatively, copy `custom_components/dvrkit` from this repository to Home Assistant's `config/custom_components/dvrkit`, restart Home Assistant and add the integration as above. The playback session limit is configurable in integration options and defaults to four; do not exceed your recorder's limit.

DVRKit exposes native Home Assistant camera entities for live viewing. Live preview uses the DVR's substream through an in-memory, loopback-only HLS bridge and Home Assistant's stream integration. It starts when a player requests media, carries video only, and does not transcode or save footage.

## Recorded playback with Advanced Camera Card

Install the separate [Advanced Camera Card fork](https://github.com/ofa1/advanced-camera-card), which includes the DVRKit engine:

1. Remove or disable any existing stock Advanced Camera Card installation/resource. Both builds register the same card element.
2. In HACS, add `https://github.com/ofa1/advanced-camera-card` as a custom repository of type **Dashboard**.
3. Enable beta/prerelease versions for that repository and download the latest `dvrkit` release. Restart or reload the frontend and clear its cache if necessary.
4. Confirm the card resource points to the HACS-installed `advanced-camera-card.js`, then use the configuration below.

For manual installation, download `advanced-camera-card.zip` from the fork's releases, extract **all** files into `config/www/advanced-camera-card/`, and add `/local/advanced-camera-card/advanced-camera-card.js` as a JavaScript module dashboard resource. The entrypoint imports hashed chunks, so copying only that file is insufficient.

The frontend is separate from this integration. For an existing bundled installation, the optional, ignored `custom_components/dvrkit/www/acc/` directory can instead contain the fork's complete production distribution; when its `advanced-camera-card.js` is present at integration startup, DVRKit serves and registers it. Use only one installation method.

Add a manual card to a dashboard, replacing the example entities with your DVRKit entities:

```yaml
type: custom:advanced-camera-card
cameras:
  - camera_entity: camera.dvr_channel_1
    engine: dvrkit
    live_provider: ha
  - camera_entity: camera.dvr_channel_2
    engine: dvrkit
    live_provider: ha
media_viewer:
  display:
    mode: grid
  auto_play: [visible]
  auto_pause: [hidden]
  controls:
    timeline:
      mode: below
      pan_mode: seek
```

Use groups of at most four recording cameras on the tested recorder. Shared timeline seeking opens a playback session per camera at the chosen time; each camera buffers independently, so playback is not frame-locked. Sessions cover at most one hour and begin at an available keyframe. Recording calendars represent dates on which any configured channel recorded; each camera's available hours come from its own segments. Events, snapshots, downloads and favorites are not supported by the DVRKit card engine.

Playback is not transcoded. HEVC/H.265 recordings require a browser or device that supports HEVC playback; support varies by client. Configure a compatible codec on the DVR where possible if your client cannot play its stream.

## Privacy and security

Playback URLs are short-lived bearer capabilities: anyone possessing one can access that stream until it expires. Do not share URLs or logs containing them. Protect Home Assistant and DVR credentials, restrict network access, and avoid caching private media or changing playlists in proxies. Remote access exposes sensitive camera footage and should be secured accordingly.

## Development and protocol debugging

The protocol client and muxer live in `custom_components/dvrkit/tvt.py` and `fmp4.py`. For protocol debugging, `poc.py` can save a recording to a local file:

```sh
uv sync
DVR_PASSWORD=... uv run poc.py --host DVR_HOST --user DVR_USER \
    --channel 2 --start "2026-01-01 12:00:00" --seconds 30 -o out.mp4
```

Treat recordings as private and delete debug output when finished. Run integration tests with:

```sh
uv run --python 3.14 --with homeassistant python -m unittest discover -s tests
```

See [the protocol notes](docs/tvt-protocol.md). No DVR vendor code or binaries are included.
