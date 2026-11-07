<p align="center">
  <img src="custom_components/dvrkit/brand/icon.png" alt="DVRKit" width="112">
</p>

<h1 align="center">DVRKit</h1>

<p align="center">
  Your DVR's live cameras and recording timeline, inside Home Assistant.
</p>

<p align="center">
  <a href="https://github.com/ofa1/dvrkit/releases"><img src="https://img.shields.io/github/v/release/ofa1/dvrkit" alt="Latest release"></a>
  <a href="https://hacs.xyz/docs/faq/custom_repositories/"><img src="https://img.shields.io/badge/HACS-Custom%20integration-41BDF5" alt="HACS custom integration"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/ofa1/dvrkit" alt="MIT license"></a>
</p>

DVRKit connects a compatible DVR to Home Assistant. Watch live cameras through native camera entities, then browse recordings in [Advanced Camera Card](https://github.com/ofa1/advanced-camera-card) with a multi-camera grid and shared timeline seeking.

Recordings stay on your DVR. DVRKit streams them on demand, repackaging video in memory without transcoding or downloading complete recordings first.

> **Early release:** Currently tested with one TVT-based recorder. Recording playback requires the **DVRKit fork of Advanced Camera Card**, not the stock card. See [compatibility](#compatibility) before installing.

## Features

- **Native camera entities** for live viewing in Home Assistant.
- **Multi-camera recording playback** in an Advanced Camera Card grid.
- **Shared timeline seeking** to move multiple cameras to the same point in time.
- **Recording calendars and availability** to find footage stored on the DVR.
- **On-demand streaming**, with no cloud storage or separate recording library.
- **Local DVR connection**, using the TVT NVMS-9000 web protocol.

## What you need

- Home Assistant with [HACS](https://hacs.xyz/) installed, or access to its configuration directory for manual installation.
- A compatible DVR reachable from Home Assistant over the network.
- A DVR account with remote search and playback permissions for the channels you want to use.
- For recording playback: the [DVRKit Advanced Camera Card fork](https://github.com/ofa1/advanced-camera-card) and a browser that supports your recordings' video codec.

DVRKit is a **custom integration**, not a Home Assistant app/add-on. It runs inside Home Assistant; you do not install a separate service.
DVRKit is configured through the Home Assistant UI and declares a dependency on Home Assistant's HTTP integration.

## Installation

### 1. Install the integration

[![Open your Home Assistant instance and add DVRKit to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ofa1&repository=dvrkit&category=integration)

1. Open the button above, or add `https://github.com/ofa1/dvrkit` in **HACS → Custom repositories** with category **Integration**.
2. Download DVRKit in HACS.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and search for **DVRKit**.
5. Enter your DVR's host, HTTP port, account credentials, device timezone and channel count.

Your channels appear as camera entities. You can use them for live viewing in standard Home Assistant camera cards without installing the ACC fork.

For recorders with RTSP enabled, open the integration's **Configure** options and select **RTSP** as the live transport. Set the recorder's RTSP port (usually `554`). This lets Home Assistant consume the substream directly instead of waiting for DVRKit's WebSocket-to-HLS bridge. The WebSocket bridge remains the default for recorders without a verified RTSP service.

<details>
<summary>Manual installation</summary>

1. Download `dvrkit.zip` from the [latest release](https://github.com/ofa1/dvrkit/releases/latest).
2. Extract it and copy its `custom_components/dvrkit` directory into your Home Assistant configuration directory at `config/custom_components/dvrkit`.
3. Restart Home Assistant, then add **DVRKit** under **Settings → Devices & services**.

</details>

### 2. Install the recording playback card

For the recording timeline and multi-camera playback, install the [DVRKit Advanced Camera Card fork](https://github.com/ofa1/advanced-camera-card).

> **Use one card build only.** The fork and stock Advanced Camera Card register the same custom element. Remove or disable your existing stock installation and its dashboard resource before installing the fork.

1. Add `https://github.com/ofa1/advanced-camera-card` in **HACS → Custom repositories** with category **Dashboard**.
2. Enable beta/prerelease versions for that repository and download the latest `dvrkit` release.
3. Reload the Home Assistant frontend. Clear its cache if it still loads the old card.
4. Confirm the dashboard resource points to the HACS-installed `advanced-camera-card.js`.

<details>
<summary>Manual card installation</summary>

Download `advanced-camera-card.zip` from the [fork's releases](https://github.com/ofa1/advanced-camera-card/releases). Extract **all files** into `config/www/advanced-camera-card/`, then add `/local/advanced-camera-card/advanced-camera-card.js` as a JavaScript module dashboard resource.

Copying only `advanced-camera-card.js` is insufficient: it imports the other JavaScript chunks in the archive.

</details>

### 3. Add your dashboard card

Add a manual card to a dashboard and paste the configuration below. Replace the example camera entities with the ones created by DVRKit.

```yaml
type: custom:advanced-camera-card
view:
  default: live
cameras:
  - camera_entity: camera.dvr_channel_1
    engine: dvrkit
    live_provider: ha
  - camera_entity: camera.dvr_channel_2
    engine: dvrkit
    live_provider: ha
live:
  lazy_load: false
  show_image_during_load: false
  display:
    mode: grid
  controls:
    thumbnails:
      mode: none
    timeline:
      mode: none
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

The card opens in a live camera grid without loading the recording timeline. Use ACC's existing recording or timeline controls to browse history; the recording viewer retains its grid and shared timeline. Shared seeking opens a recording session for each camera at the selected time. Use groups of **at most four recording cameras** on the tested recorder.

### Optional: low-latency public live viewing

For live video through the same public HTTPS connection as Home Assistant, use the existing [WebRTC Camera integration](https://github.com/AlexxIT/WebRTC) and ACC's `webrtc-card` provider. Its **MSE** mode carries video through an authenticated HA WebSocket proxy; it does not require a direct browser connection to the DVR, a public WebRTC media port, or Tailscale on the viewing device.

1. Enable **RTSP** in DVRKit's integration options.
2. Install **WebRTC Camera** from HACS, or follow its manual installation instructions.
3. If it will manage a dedicated go2rtc process for MSE only, create `config/go2rtc.yaml` before adding the integration:

   ```yaml
   api:
     listen: "127.0.0.1:1984"
   rtsp:
     listen: ""
   webrtc:
     listen: ""
   ```

   This keeps the go2rtc API on loopback and disables unnecessary inbound media listeners. Do not overwrite an existing go2rtc configuration; connect WebRTC Camera to that server instead and retain its existing security settings.
4. Restart HA, then add **WebRTC Camera** under **Devices & services**. Its existing card resource is registered automatically.
5. Replace each camera's live-provider settings:

   ```yaml
   cameras:
     - camera_entity: camera.dvr_channel_1
       engine: dvrkit
       live_provider: webrtc-card
       webrtc_card:
         entity: camera.dvr_channel_1
         mode: mse
         media: video
   ```

Keep the live grid and historical viewer settings from the example above. This changes only live transport; recordings still use DVRKit's HLS playback and shared timeline. MSE still requires browser support for the DVR's codec. This setup was exercised with HEVC in Chromium. Android browsers also need HEVC support in MSE, not just native video playback.

## Compatibility

| Item | Current support |
| --- | --- |
| DVR protocol | TVT NVMS-9000 web protocol |
| Tested recorder | Hawk Vision HV-D5100-8HL, rebadged TVT |
| Tested firmware | `1.3.8.55236B221125` |
| Other TVT-based recorders | May work; not yet verified |
| Concurrent recording playback | Four sessions on the tested recorder; device limits may differ |
| Live video | DVR substream, video only |
| Recording video | Original DVR codec, no transcoding |
| Recording card | DVRKit fork of Advanced Camera Card |

### Playback behavior and limits

- **Codec support depends on your browser.** HEVC/H.265 recordings need a client that can play HEVC. DVRKit does not convert them to H.264. Configure a compatible codec on the DVR where possible.
- **Shared seeking is not frame synchronization.** Cameras seek to the same requested time but buffer independently. Playback starts at an available keyframe.
- **Sessions cover at most one hour.** They stream footage on demand rather than downloading the full recording.
- **Calendar dates are recorder-wide.** A date indicates that at least one configured channel recorded. Each camera's available hours come from its own recording segments.
- **Playback capacity is device-specific.** The session limit defaults to four and is configurable in the integration options. Do not exceed your recorder's limit. Live uses a separate RTSP connection or TVT preview task.
- **Live and history use separate paths.** RTSP live video goes directly to Home Assistant's native streaming providers. Historical footage continues to use DVRKit's on-demand HLS endpoint. Actual live latency depends on recorder startup, keyframe cadence, the selected HA transport, browser codecs and network connectivity; RTSP alone does not guarantee public WebRTC connectivity.
- **Events, snapshots, downloads and favorites** are not supported by the DVRKit card engine.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Cannot connect to the DVR | Check that Home Assistant can reach the DVR host and HTTP port, and that the account has remote search and playback permissions. |
| Live view works, but the recording timeline does not | Confirm you installed the DVRKit ACC fork and set `engine: dvrkit` for each camera. The stock card does not include this engine. |
| The frontend still shows the old card | Remove duplicate dashboard resources, reload the frontend and clear its cache. Load only one ACC build. |
| Manual card installation fails to load | Extract every JavaScript file from the release ZIP, not just the entrypoint. |
| Recordings are listed but video will not play | Check browser support for the DVR's video codec, especially HEVC/H.265. |
| Another camera cannot start recording playback | Check the DVR's concurrent playback limit and the integration's session-limit option. Try fewer recording cameras at once. |
| Recording times look wrong | Check the configured device timezone against the DVR's timezone. |

If the problem persists, [open an issue](https://github.com/ofa1/dvrkit/issues). Include your recorder model, firmware, Home Assistant version, browser, installation method and steps to reproduce. Redact credentials and playback URLs from logs.

## Privacy and security

DVRKit connects directly to your DVR. With RTSP selected, live video flows from the DVR to Home Assistant's native streaming providers. With the WebSocket transport selected, live preview passes through an in-memory, loopback-only HLS bridge and Home Assistant's stream integration. Both start on demand and do not save footage.

Recording playback URLs are **short-lived bearer capabilities**: anyone with a URL can access that stream until it expires. Do not share these URLs or logs containing them.

Protect Home Assistant and DVR credentials, restrict network access, and secure any remote access to Home Assistant. Keep private media and changing playlists out of proxy caches.

## Development

The protocol client and muxer live in `custom_components/dvrkit/tvt.py` and `custom_components/dvrkit/fmp4.py`. See the [protocol notes](docs/tvt-protocol.md) for implementation details. No DVR vendor code or binaries are included.

Run the integration tests:

```sh
uv run --python 3.14 --with homeassistant --with numpy --with av --with PyTurboJPEG python -m unittest discover -s tests
```

Pull requests and pushes to `main` run these tests, Hassfest, HACS validation, and a packaging/version check. The checks can also be started manually from GitHub Actions. They do not access a DVR or deploy to Home Assistant.

The integration manifest is the release version source; `pyproject.toml` and `uv.lock` must match it. After committing changes, validate and build the same archive from tracked files at `HEAD`:

```sh
uv run --python 3.14 python scripts/package_release.py
# For a release, also verify the tag against the manifest:
uv run --python 3.14 python scripts/package_release.py --tag v0.1.1
```

The output, `dist/dvrkit.zip`, contains `custom_components/dvrkit/` at the archive root. Ignored local files, including the optional ACC bundle, are never included. Publish ACC separately using its own release archive.

Pushing a `v*` tag on a commit reachable from GitHub's `main` branch runs all checks before creating a GitHub release with this archive. The tag must equal `v` followed by the manifest version. The local `public-main` branch maps to GitHub's `main`; creating a tag or release is a separate maintainer action.

<details>
<summary>Protocol debugging and recording capture</summary>

Normal playback does not download recordings first. The separate `poc.py` development tool can explicitly save a recording to a local file for protocol debugging:

```sh
uv sync
DVR_PASSWORD=... uv run poc.py --host DVR_HOST --user DVR_USER \
    --channel 2 --start "2026-01-01 12:00:00" --seconds 30 -o out.mp4
```

Treat recordings as private and delete debug output when finished.

</details>

<details>
<summary>Existing bundled card installations</summary>

The frontend is separate from this integration. Existing bundled deployments may use the optional, ignored `custom_components/dvrkit/www/acc/` directory for the fork's complete production distribution. If `advanced-camera-card.js` is present there at integration startup, DVRKit serves and registers it.

Use only one card installation method. New installations should follow the HACS or manual card instructions above.

</details>

## Credits and license

Built for [Home Assistant](https://www.home-assistant.io/) and [Advanced Camera Card](https://github.com/dermotduffy/advanced-camera-card). DVRKit is an independent community project, not an official DVR vendor integration.

[MIT license](LICENSE).
