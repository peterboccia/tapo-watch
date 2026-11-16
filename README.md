# TapoWatch

**Local Tapo motion alerts, snapshots and video clips delivered to Telegram.**

TapoWatch subscribes to your camera's local motion events, saves the resulting
photo or short video, and sends it to explicitly authorized Telegram users.
It also provides private commands for snapshots and camera privacy control.

An independent community project, not affiliated with or endorsed by TP-Link
or Telegram. Tapo and related names belong to their respective owners.

## Features

- Real ONVIF PullPoint motion events, with subscription renewal and reconnects.
- JPEG motion photos or lightweight **3-second silent H.264 MP4** clips.
- Local media plus JSON metadata, saved before notification delivery.
- Telegram notifications restricted to a numeric user-ID whitelist.
- A persistent button menu: **Foto / Help**, **Accendi / Spegni**.
- Help and current camera privacy status in one message.
- Manual photos that temporarily disable privacy and restore its previous state.
- Verified privacy changes through pytapo.
- Docker deployment suitable for a 64-bit Raspberry Pi, with persistent media,
  non-root execution, a read-only root filesystem, and automatic restarts.
- Local-only capture commands and a Python hook for custom consumers.

This repository contains **only the camera application**. There is no smart
plug control, countdown/probe utility, or operating-system shutdown code.
The current bot commands, menu labels, and most runtime messages are Italian;
they are documented in English below. Multilingual translation catalogs are
not implemented in this release.

## Requirements

- Python **3.11+** for local installation, or Docker with **Compose 2.30+**.
- A Tapo camera exposing local **ONVIF, RTSP, and compatible pytapo privacy
  controls**. Not every model/firmware does; many battery cameras do not.
- Camera and host on the same trusted LAN, and outbound HTTPS to Telegram.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).

The original deployment used a Tapo C210 hardware 1.0, firmware 1.5.2.
This is not a compatibility guarantee for all Tapo cameras.
The container uses Python 3.13 and builds for the host's architecture,
including typical Linux x86-64 and 64-bit Raspberry Pi installations.
Raspberry Pi OS must be **64-bit**, not ARMv7/32-bit.

Default camera ports are **2020/TCP** (ONVIF), **554/TCP** (RTSP), and
**443/TCP** (pytapo HTTPS). Do not expose them to the Internet.
See [TP-Link's local streaming guide](https://www.tp-link.com/us/support/faq/2680/).

## Quick start with Docker

Run these commands from the cloned `tapo-watch` directory:

```bash
cp .env.example .env
# Edit .env locally with your camera credentials, token and user IDs.
docker compose up -d --build
docker compose logs --tail=50 -f tapo-watch
```

In PowerShell, use `Copy-Item .env.example .env` instead of `cp`.
Do not overwrite an existing `.env` when upgrading.

The Compose service runs in Telegram mode, saves captures in a named volume
at `/data/captures`, uses `Europe/Rome` for caption times, and publishes **no
ports**. No privileged mode or host networking is required. Change `TZ` in
Compose if needed.

Each authorized user must first open a private chat with the bot and press
**Start**. Send `/menu` to display or refresh the keyboard, then `/help`.
Wait at least three seconds between commands.

The application **does not change camera privacy at startup**. For continuous
motion alerts, enable images with `/accendi` when you intend to monitor.

### Updating and changing configuration

```bash
# After pulling/copying new source:
docker compose up -d --build

# After changing only .env:
docker compose up -d --force-recreate

# Stop without deleting the media volume:
docker compose down
```

A simple container restart does not reload changed Compose environment values.
Do not use `docker compose down -v` unless you intend to delete the captures.
`unless-stopped` restarts after crashes/host reboots, but not after a manual
stop. Automatic restarts do not guarantee delivery or detect every stuck worker.

## Configuration

Only `.env.example` belongs in Git. **Never publish your real `.env`, bot
token, camera passwords, or captures.**

| Variable | Purpose |
| --- | --- |
| `TAPO_CAMERA_HOST` | Camera IPv4 address; default `192.168.1.201` |
| `TAPO_CAMERA_USERNAME` | Local Camera Account username for ONVIF/RTSP |
| `TAPO_CAMERA_PASSWORD` | Local Camera Account password for ONVIF/RTSP |
| `TAPO_CAMERA_AUTH` | Privacy authentication: `camera` (default) or `admin` |
| `TAPO_CAMERA_ADMIN_PASSWORD` | Required cloud owner password when privacy authentication is `admin` |
| `TAPO_CAMERA_MOTION_MEDIA` | Telegram motion alerts: `photo` (default) or `video` |
| `TAPO_TELEGRAM_BOT_TOKEN` | Secret token issued by BotFather |
| `TAPO_TELEGRAM_USER_IDS` | Comma-separated positive numeric Telegram **user IDs** |

For example, the non-secret portion might contain:

```dotenv
TAPO_CAMERA_HOST=192.168.1.201
TAPO_CAMERA_AUTH=admin
TAPO_CAMERA_MOTION_MEDIA=video
TAPO_TELEGRAM_USER_IDS=123456789,987654321
```

The IDs above are placeholders. IDs are **not phone numbers, usernames, or
negative group IDs**. Verify recipients' identities and IDs through a trusted
channel; do not send your token to public helper bots.
Every whitelisted user can receive media, request photos, and change privacy.
An empty/invalid whitelist prevents Telegram mode from starting.

When running locally, `.env` and the default `captures` directory are resolved
from the working directory at application startup, not the installed package
or any parent project. Process environment values take precedence over `.env`;
explicit CLI overrides take precedence where provided.
Compose injects `.env` using `format: raw`: surrounding quotes become literal
characters, so do not add them around passwords unnecessarily.

### Camera Account versus cloud account

Configure a dedicated **Camera Account** in the Tapo app's advanced camera
settings. ONVIF/RTSP requires this local account; cloud login credentials do
not automatically grant streaming access.

Privacy control uses **pytapo 3.4.30**:

- `camera`: use `TAPO_CAMERA_USERNAME` and `TAPO_CAMERA_PASSWORD`.
- `admin`: use literal username `admin` and `TAPO_CAMERA_ADMIN_PASSWORD`,
  containing the owner's cloud password. `--auth admin` overrides the environment.

Where required by firmware, enable **Third-Party Compatibility** in the Tapo
app's profile/third-party services settings. This is separate from linking
Tapo to Kasa and changes third-party access protections.
Changes may take time to become effective. If authentication is temporarily
suspended, wait for the camera to unlock and check credentials before retrying.
Do not start login loops, disable account security, or share raw debug output.

If migrating from the original combined project, explicitly configure the
camera variables above. **`KASA_USERNAME`, `KASA_PASSWORD`, `TAPO_NAME`, and
`TAPO_HOST` are not used by TapoWatch.** No `python-kasa` dependency is needed.
Copy only your required configuration values, not the old entire project.
Existing captures/volumes are not automatically migrated or deleted.

## Telegram commands

The keyboard contains:

| | |
| --- | --- |
| **Foto** | **Help** |
| **Accendi** | **Spegni** |

| Command | Behavior |
| --- | --- |
| `/foto` | Take a fresh JPEG and send it only to the requester. If privacy is ON, temporarily disable it, take the photo, and restore ON before sending. If OFF, capture without changing it. |
| `/help` | Show instructions plus current privacy state in one message |
| `/stato` | Backward-compatible alias for `/help`; no separate button |
| `/accendi` | Set and verify privacy **OFF**, enabling images; does not switch power |
| `/spegni` | Set and verify privacy **ON**, blocking images; does not cut power |
| `/start`, `/menu` | Show instructions/keyboard without contacting the camera |

Commands also work as case-insensitive plain text with no arguments.
Mentions such as `/foto@YourBotUsername` must match this bot.
Paths, arguments, or mentions of other bots cannot trigger camera operations.
Simple unknown commands show static help to authorized users.

**Foto privacy restoration:** the initial privacy read must succeed.
The same camera lock covers reading, temporarily disabling privacy, capture,
and restoring ON. Restoration is attempted even when disabling privacy or
taking/saving the photo fails, or the operation is interrupted. Failed
restoration prevents sending the photo and produces an explicit warning:
check Help/the Tapo app. Network failures, forced termination, and power loss
can prevent restoration; it cannot be guaranteed.

Help still displays instructions if its privacy query fails, along with an
explicit error instead of an invented status.

### Access control

Commands and media require a private message whose `from.id` and `chat.id`
both equal a whitelisted ID. Bot/anonymous senders, groups/channels, forwarded
messages, edited messages, and stale messages are ignored.

A non-whitelisted user writing directly in a valid private chat receives only:

```text
Non sei nella whitelist.
```

This means "You are not on the whitelist." The response contains no keyboard,
status, or media and never accesses the camera. It is the only outbound
whitelist exception and allows only that exact fixed text with no files or
extra fields. Denials are globally limited to one every three seconds, without
retaining unknown user IDs. `/start` never enrolls a user.

## Motion photos and videos

Set `TAPO_CAMERA_MOTION_MEDIA=photo` for JPEG motion alerts (the default when
missing/empty), or `video` for:

- **3-second silent H.264 MP4**;
- width **640 pixels**, proportional even height, **10 fps**;
- CRF 28, `veryfast` preset, 1 Mbit/s maximum video rate setting;
- two FFmpeg threads and fast-start MP4 layout.

MP4 is generally smaller than GIF for camera footage. GIFs are not generated.
Actual size and CPU usage depend on the scene; files above **2 MiB** are
explicitly rejected. Errors do not silently fall back to JPEG.

`--motion-media photo|video` overrides `.env`. Invalid environment values fail
explicitly in Telegram mode. Manual **Foto**, local `snapshot`, and local
`watch` always remain JPEG.

Capture begins **after** the event and RTSP connection. There is no pre-event
buffer or retrieval of the Tapo app's event thumbnail. Video adds recording/
encoding latency and more frames, but does not remove source-image blur.
A fast-moving subject may already be out of view.

## Local installation and commands

From the repository root, create a virtual environment and install:

```bash
python -m venv .venv
```

Activate it:

```bash
# Linux/macOS:
source .venv/bin/activate
```

```powershell
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
```

Then:

```bash
python -m pip install .
tapo-watch --help
tapo-watch privacy-status
tapo-watch snapshot
tapo-watch watch
tapo-watch telegram
```

Use `python -m tapo_camera` instead of `tapo-watch` if preferred.
FFmpeg is supplied by `imageio-ffmpeg`; optionally set `IMAGEIO_FFMPEG_EXE`
to a trusted installed FFmpeg executable. H.264 encoding requires `libx264`.
Local commands do not install a background service.

| Command | Purpose |
| --- | --- |
| `snapshot` | Save one local JPEG immediately; no Telegram delivery or automatic privacy change |
| `watch` | Save JPEGs on ONVIF motion; no Telegram delivery or automatic privacy change |
| `privacy-status` | Read privacy and print verified JSON |
| `privacy-on`, `privacy-off` | Explicitly change privacy and verify the resulting state |
| `telegram` | Run motion monitoring, private command polling, and notification delivery |

| Option | Default |
| --- | --- |
| `--host` | `TAPO_CAMERA_HOST` or `192.168.1.201` |
| `--onvif-port` / `--rtsp-port` / `--control-port` | `2020` / `554` / `443` |
| `--stream` | `stream1` (`stream2` is also supported) |
| `--output` | Working-directory `captures` |
| `--cooldown` | `10` seconds |
| `--capture-timeout` | `20` seconds |
| `--auth` | `TAPO_CAMERA_AUTH` or `camera` |
| `--motion-media` | `TAPO_CAMERA_MOTION_MEDIA` or `photo` |

Privacy JSON reports `privacy: "on"|"off"` and `state_verified: true` only after
successful readback; writes must match the requested state. A failed
standalone privacy write may already have changed the camera and does not
automatically roll back. Verify the app/status before retrying.

Exit codes: **0** success, **1** camera/filesystem error, **2** configuration
or missing dependencies, **130** interrupted.

## Persistence, delivery, and limitations

- Captures are saved under `captures/YYYY-MM-DD/`, or the configured output,
  as a unique UTC basename plus `.jpg`/`.mp4` and `.json`.
- JSON is published only after the media has been saved. It includes
  `schema_version`, `camera_host`, `reason`, event/save timestamps, `topic`,
  `media_type`, `image`, `video`, and `message`. Photo metadata uses
  `image` with `video: null`; video metadata uses `video` with `image: null`.
  An interrupted save can leave an orphan media file.
- `tapo_camera.monitor.on_capture(capture)` runs after persistence. Its
  `capture.image` attribute is the media path, including for videos (historical
  naming); `capture.metadata`, `capture.message`, and `capture.media_type`
  identify the remaining data. Hook errors do not delete captures.
- Captions use local save time, including seconds and UTC offset; metadata
  also retains event times. The default local hook logs rather than sends.
- The default cooldown is ten seconds after each successful capture. Motion
  during cooldown is discarded. Manual photos bypass the cooldown but reset
  it, potentially postponing the next motion capture.
- Motion broadcasts only to whitelisted recipients through a queue of
  **four captures**. Queue overflow leaves new media on disk without delivery.
  Delivery is best effort, with no automatic media/message retries or replay
  of old JSON. A failure for one recipient does not block attempts to others.
- ONVIF subscriptions renew automatically and reconnect after five seconds
  on errors. Lost events are not recovered. RTSP/disk failures wait for the
  next active motion event.
- Startup drops old Telegram updates. Accepted commands must be dated at/
  after startup and no more than thirty seconds old, with checks repeated
  after waiting for the camera. Authorized commands are limited to one every
  three seconds per user. Camera operations are serialized.
- HTTPS polling requires no webhook. An existing webhook prevents startup
  and is not deleted automatically. HTTP proxies from the environment and
  redirects are disabled; raw token URLs/network exceptions are not logged.
- `Ctrl+C` requests shutdown, discards pending delivery, and releases ONVIF
  subscriptions when possible. In-flight work may finish. Worker waiting is
  bounded by `--capture-timeout + 65` seconds.
- Captures and `.env` are not encrypted locally. Restrict filesystem access
  and manage retention manually: there is **no automatic deletion**.
  RTSP credentials appear in FFmpeg process arguments and may be visible to
  users with process-inspection rights. Do not share process dumps/debug logs.
- Sent media passes through Telegram's cloud, not a local-only connection or
  secret chat. Obtain any consent required for camera use and notifications.

## Development and verification

The package uses a standard `src` layout:

```text
tapo-watch/
  src/tapo_camera/       # Application only
  tests/                # Offline camera/Telegram tests
  .github/workflows/    # Linux/Windows test workflow
  .env.example
  .dockerignore
  .gitignore
  compose.yaml
  Dockerfile
  pyproject.toml
  README.md
  LICENSE
```

Install editable for development and run from the repository root:

```bash
python -m pip install -e .
python -m pip check
python -m unittest discover -s tests -v
```

Tests simulate camera/Telegram interactions; local encoding tests invoke
FFmpeg. They do not need real credentials, contact your camera, send Telegram
messages, or change privacy. GitHub Actions installs the standalone package
and runs CLI smoke checks and the test suite on Linux/Windows.

Before relying on a deployment, verify real hardware:

1. With privacy OFF, run `privacy-status` and `snapshot`.
2. Start the bot, send Help, then Foto, at least three seconds apart.
3. With privacy ON, request Foto and check it returns to ON afterward.
4. With privacy OFF and motion detection enabled, trigger motion and check the
   received media and matching local JSON. For video, check playback, duration,
   lack of audio, size, and latency on your hardware.
5. Test a non-whitelisted private account: only the fixed denial should arrive.
6. Monitor disk space and logs. Camera/privacy operations can fail when the
   device, LAN, or authentication service is unavailable.

## License

TapoWatch is distributed under the **[MIT License](LICENSE)**. You may use,
modify, and redistribute it, including commercially, subject to the license
notice requirements. It is provided without warranty.

Third-party packages, FFmpeg, and codecs retain their own licenses. The MIT
license covers this application's code, not a relicensing of its dependencies;
review those licenses when redistributing images or binaries.
