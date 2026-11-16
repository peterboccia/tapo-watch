from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote

LOGGER = logging.getLogger("tapo_camera")
PROJECT_ROOT = Path.cwd()


class CameraError(Exception):
    """A safe-to-display camera error (never containing credentials)."""


@dataclass(frozen=True)
class Settings:
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    output: Path = PROJECT_ROOT / "captures"
    onvif_port: int = 2020
    rtsp_port: int = 554
    stream: str = "stream1"
    cooldown: int = 10
    capture_timeout: int = 20


@dataclass(frozen=True)
class Motion:
    topic: str
    camera_time: str | None
    received_at: str


@dataclass(frozen=True)
class Capture:
    image: Path
    metadata: Path
    message: str
    media_type: str = "photo"


class EventSource(Protocol):
    def pull(self) -> list[Motion]: ...
    def close(self) -> None: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rtsp_url(settings: Settings) -> str:
    username = quote(settings.username, safe="")
    password = quote(settings.password, safe="")
    return (
        f"rtsp://{username}:{password}@{settings.host}:"
        f"{settings.rtsp_port}/{settings.stream}"
    )


def capture_frame(settings: Settings, executable: str) -> bytes:
    url = rtsp_url(settings)
    try:
        result = subprocess.run(
            [
                executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-rtsp_transport", "tcp", "-i", url,
                "-map", "0:v:0", "-frames:v", "1", "-an",
                "-c:v", "mjpeg", "-q:v", "2", "-f", "image2pipe", "pipe:1",
            ],
            capture_output=True,
            timeout=settings.capture_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CameraError("RTSP snapshot timed out; check camera account/stream.") from None
    except OSError:
        raise CameraError("Cannot execute FFmpeg; reinstall TapoWatch or configure IMAGEIO_FFMPEG_EXE.") from None
    # FFmpeg diagnostics can contain the authenticated URL; never print them.
    if result.returncode:
        raise CameraError("RTSP snapshot failed; check camera account, IP and RTSP access.")
    if not result.stdout.startswith(b"\xff\xd8") or not result.stdout.endswith(b"\xff\xd9"):
        raise CameraError("RTSP did not return a complete JPEG.")
    return result.stdout


def capture_video(settings: Settings, executable: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="tapo-clip-") as directory:
        path = Path(directory) / "clip.mp4"
        try:
            result = subprocess.run(
                [
                    executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-rtsp_transport", "tcp", "-i", rtsp_url(settings),
                    "-map", "0:v:0", "-t", "3", "-an",
                    "-vf", "fps=10,scale=640:-2", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "28",
                    "-maxrate", "1M", "-bufsize", "2M",
                    "-threads", "2", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", "-f", "mp4", str(path),
                ],
                capture_output=True, timeout=settings.capture_timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise CameraError("Video RTSP scaduto; verificare stream e timeout.") from None
        except OSError:
            raise CameraError("Impossibile eseguire FFmpeg per il video.") from None
        if result.returncode or not path.is_file():
            raise CameraError("Video RTSP fallito; verificare accesso camera e FFmpeg.")
        if not 0 < path.stat().st_size <= 2 * 1024 * 1024:
            raise CameraError("Video vuoto o superiore al limite locale di 2 MiB.")
        data = path.read_bytes()
        if len(data) < 12 or data[4:8] != b"ftyp":
            raise CameraError("FFmpeg non ha restituito un video MP4 valido.")
        return data


def save_capture(
    settings: Settings, jpeg: bytes, motion: Motion | None, media_type: str = "photo",
) -> Capture:
    if media_type not in ("photo", "video"):
        raise CameraError("Tipo di acquisizione non supportato.")
    now = datetime.now(timezone.utc)
    timestamp = now.astimezone().strftime("%d/%m/%Y %H:%M:%S %z")
    message = (
        f"Movimento rilevato {timestamp}" if motion
        else f"Screenshot manuale {timestamp}"
    )
    folder = settings.output.resolve() / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{now.strftime('%H%M%S_%f')}_{uuid.uuid4().hex}"
    image = folder / f"{name}{'.mp4' if media_type == 'video' else '.jpg'}"
    metadata = folder / f"{name}.json"
    report = {
        "schema_version": 1,
        "camera_host": settings.host,
        "reason": "motion" if motion else "manual",
        "topic": motion.topic if motion else None,
        "camera_time": motion.camera_time if motion else None,
        "received_at": motion.received_at if motion else None,
        "captured_at": now.isoformat(),
        "image": image.name if media_type == "photo" else None,
        "video": image.name if media_type == "video" else None,
        "media_type": media_type,
        "message": message,
    }
    image_temp = image.with_suffix(image.suffix + ".tmp")
    metadata_temp = metadata.with_suffix(".json.tmp")
    try:
        with image_temp.open("xb") as file:
            file.write(jpeg)
            file.flush()
            os.fsync(file.fileno())
        image_temp.replace(image)
        with metadata_temp.open("x", encoding="utf-8") as file:
            json.dump(report, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        # A JSON file is the ready marker for future messaging consumers.
        metadata_temp.replace(metadata)
    finally:
        image_temp.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)
    return Capture(image, metadata, message, media_type)


def on_capture(capture: Capture) -> None:
    """Messaging extension point. The JPEG and ready-marker JSON already exist."""
    LOGGER.info(
        "%s | Media pronto: %s (metadati: %s)",
        capture.message, capture.image, capture.metadata,
    )


class Recorder:
    def __init__(
        self,
        settings: Settings,
        snapshot: Callable[[], bytes],
        hook: Callable[[Capture], None] = on_capture,
        clock: Callable[[], float] = time.monotonic,
        motion_video: Callable[[], bytes] | None = None,
    ):
        self.settings = settings
        self.snapshot = snapshot
        self.hook = hook
        self.clock = clock
        self.motion_video = motion_video
        self.last_capture: float | None = None

    def record(self, motion: Motion | None = None) -> Capture | None:
        if (
            motion is not None
            and self.last_capture is not None
            and self.clock() - self.last_capture < self.settings.cooldown
        ):
            return None
        if motion is not None and self.motion_video is not None:
            saved = save_capture(self.settings, self.motion_video(), motion, "video")
        else:
            saved = save_capture(self.settings, self.snapshot(), motion)
        self.last_capture = self.clock()
        try:
            self.hook(saved)
        except Exception as error:
            # User-supplied hooks must not discard an already persisted capture.
            LOGGER.error(
                "Hook fallito (%s); foto e JSON conservati in %s",
                type(error).__name__, saved.metadata,
            )
        return saved


def monitor(
    connect: Callable[[], EventSource],
    recorder: Recorder,
    sleep: Callable[[float], None] = time.sleep,
    stop: threading.Event | None = None,
) -> None:
    while stop is None or not stop.is_set():
        source = None
        try:
            source = connect()
            LOGGER.info("Monitor ONVIF connesso; in attesa di movimento.")
            while stop is None or not stop.is_set():
                for motion in source.pull():
                    if stop is not None and stop.is_set():
                        break
                    try:
                        recorder.record(motion)
                    except (CameraError, OSError) as error:
                        LOGGER.error(
                            "Media NON salvato (%s). Controllare RTSP/account e disco; "
                            "nuovo tentativo al prossimo evento attivo.",
                            type(error).__name__,
                        )
        except CameraError as error:
            LOGGER.error("%s Riconnessione tra 5 secondi; eventi nel frattempo non garantiti.", error)
        finally:
            if source is not None:
                source.close()
        if stop is None:
            sleep(5)
        else:
            stop.wait(5)
