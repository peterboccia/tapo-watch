from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .monitor import (
    PROJECT_ROOT, CameraError, Recorder, Settings, capture_frame, capture_video, monitor,
)
from .privacy import PrivacyController, PrivacySettings


def ipv4(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ipaddress.AddressValueError:
        raise argparse.ArgumentTypeError("Indirizzo IPv4 non valido.") from None


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Serve un intero positivo.") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("Serve un intero positivo.")
    return number


def port(value: str) -> int:
    number = positive_integer(value)
    if number > 65535:
        raise argparse.ArgumentTypeError("Porta fuori intervallo (1-65535).")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="TapoWatch: local Tapo motion alerts, snapshots and video clips delivered to Telegram."
    )
    result.add_argument("command", choices=("watch", "snapshot", "privacy-on", "privacy-off", "privacy-status", "telegram"))
    result.add_argument("--host", type=ipv4, default=os.getenv("TAPO_CAMERA_HOST") or "192.168.1.201")
    result.add_argument("--onvif-port", type=port, default=2020)
    result.add_argument("--rtsp-port", type=port, default=554)
    result.add_argument("--stream", choices=("stream1", "stream2"), default="stream1")
    result.add_argument("--output", type=Path, default=PROJECT_ROOT / "captures")
    result.add_argument("--cooldown", type=positive_integer, default=10, help="Secondi minimi tra foto di movimento (default: 10).")
    result.add_argument("--capture-timeout", type=positive_integer, default=20)
    result.add_argument(
        "--motion-media", choices=("video", "photo"), default=None,
        help="Alert Telegram: video MP4 di 3 secondi oppure photo JPEG "
        "(default: TAPO_CAMERA_MOTION_MEDIA oppure photo). Foto e watch restano JPEG.",
    )
    result.add_argument("--control-port", type=port, default=443, help="Porta HTTPS pytapo.")
    result.add_argument(
        "--auth", choices=("camera", "admin"), default=None,
        help="Autenticazione privacy (default: TAPO_CAMERA_AUTH oppure camera).",
    )
    return result


def privacy_settings(arguments: argparse.Namespace) -> PrivacySettings:
    auth = arguments.auth or os.getenv("TAPO_CAMERA_AUTH") or "camera"
    if auth not in ("camera", "admin"):
        raise ValueError("TAPO_CAMERA_AUTH deve essere camera oppure admin.")
    if auth == "admin":
        username = "admin"
        password = os.getenv("TAPO_CAMERA_ADMIN_PASSWORD", "")
    else:
        username = os.getenv("TAPO_CAMERA_USERNAME", "")
        password = os.getenv("TAPO_CAMERA_PASSWORD", "")
    if not username.strip() or not password:
        raise ValueError(
            "Configurare la coppia completa Account videocamera, "
            "oppure --auth admin con TAPO_CAMERA_ADMIN_PASSWORD."
        )
    return PrivacySettings(arguments.host, username, password, arguments.control_port)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    arguments = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Third-party SOAP/HTTP diagnostics may contain credentials or full payloads.
    for name in ("onvif", "zeep", "urllib3", "requests", "pytapo"):
        logger = logging.getLogger(name)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
    if arguments.command.startswith("privacy-"):
        try:
            control_settings = privacy_settings(arguments)
        except ValueError as error:
            print(f"Errore: {error}", file=sys.stderr)
            return 2
        controller = None
        try:
            controller = PrivacyController(control_settings)
            enabled = (
                controller.status() if arguments.command == "privacy-status"
                else controller.set(arguments.command == "privacy-on")
            )
            print(json.dumps({
                "privacy": "on" if enabled else "off",
                "state_verified": True,
            }))
            return 0
        except ImportError:
            print("Errore: installare le dipendenze con python -m pip install .", file=sys.stderr)
            return 2
        except CameraError as error:
            print(f"Errore camera: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Interrotto; verificare lo stato privacy nell'app.", file=sys.stderr)
            return 130
        finally:
            if controller is not None:
                controller.close()
    username = os.getenv("TAPO_CAMERA_USERNAME", "")
    password = os.getenv("TAPO_CAMERA_PASSWORD", "")
    if not username.strip() or not password:
        print(
            "Errore: impostare una coppia completa TAPO_CAMERA_USERNAME/"
            "TAPO_CAMERA_PASSWORD nel .env "
            "o nell'ambiente. Le credenziali devono essere valide per l'Account videocamera.",
            file=sys.stderr,
        )
        return 2
    settings = Settings(
        host=arguments.host, username=username, password=password,
        output=arguments.output, onvif_port=arguments.onvif_port,
        rtsp_port=arguments.rtsp_port, stream=arguments.stream,
        cooldown=arguments.cooldown, capture_timeout=arguments.capture_timeout,
    )
    try:
        import imageio_ffmpeg

        from .onvif_events import OnvifEvents

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        recorder = Recorder(settings, lambda: capture_frame(settings, executable))
        if arguments.command == "snapshot":
            recorder.record()
        elif arguments.command == "telegram":
            from .telegram import TelegramSettings, run_telegram

            try:
                motion_media = arguments.motion_media or os.getenv("TAPO_CAMERA_MOTION_MEDIA") or "photo"
                if motion_media not in ("video", "photo"):
                    raise ValueError("TAPO_CAMERA_MOTION_MEDIA deve essere video oppure photo.")
                telegram_settings = TelegramSettings.from_env()
                control_settings = privacy_settings(arguments)
            except (ValueError, CameraError) as error:
                print(f"Errore: {error}", file=sys.stderr)
                return 2
            settings.output.mkdir(parents=True, exist_ok=True)
            if motion_media == "video":
                recorder.motion_video = lambda: capture_video(settings, executable)
            run_telegram(
                telegram_settings, recorder, lambda: OnvifEvents(settings),
                lambda: PrivacyController(control_settings),
            )
        else:
            settings.output.mkdir(parents=True, exist_ok=True)
            logging.getLogger("tapo_camera").info(
                "Camera %s; foto in %s; cooldown %ss. Ctrl+C per fermare.",
                settings.host, settings.output.resolve(), settings.cooldown,
            )
            monitor(lambda: OnvifEvents(settings), recorder)
    except ImportError:
        print("Errore: installare le dipendenze con python -m pip install .", file=sys.stderr)
        return 2
    except (CameraError, OSError, RuntimeError) as error:
        # OSError/RuntimeError from tools/filesystems may include unsafe strings.
        detail = str(error) if isinstance(error, CameraError) else type(error).__name__
        print(f"Errore camera: {detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Monitor fermato. Le foto gia' salvate restano su disco.", file=sys.stderr)
        return 130
    return 0
