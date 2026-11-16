import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lxml import etree
from requests import Response
from zeep.transports import Transport

from tapo_camera.cli import main, parser
from tapo_camera.monitor import (
    CameraError, Motion, Recorder, Settings, capture_frame, capture_video, monitor, save_capture,
)
from tapo_camera.onvif_events import OnvifEvents, parse_motion

JPEG = b"\xff\xd8test-image\xff\xd9"
SCHEMA = "http://www.onvif.org/ver10/schema"
EVENTS = "http://www.onvif.org/ver10/events/wsdl"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"
WSNT = "http://docs.oasis-open.org/wsn/b-2"
WSA = "http://www.w3.org/2005/08/addressing"


def notification(topic="tns1:RuleEngine/CellMotionDetector/Motion", value="true",
                 operation="Changed", name="IsMotion"):
    message = etree.fromstring(
        f'<tt:Message xmlns:tt="{SCHEMA}" UtcTime="2026-11-10T12:00:00Z" '
        f'PropertyOperation="{operation}"><tt:Source>'
        '<tt:SimpleItem Name="VideoSource" Value="1"/></tt:Source>'
        f'<tt:Data><tt:SimpleItem Name="{name}" Value="{value}"/></tt:Data>'
        '</tt:Message>'
    )
    result = etree.Element(f"{{{WSNT}}}NotificationMessage")
    etree.SubElement(result, f"{{{WSNT}}}Topic").text = topic
    etree.SubElement(result, f"{{{WSNT}}}Message").append(message)
    return result


class EventParsingTests(unittest.TestCase):
    def test_motion_topics_and_true_encodings(self):
        for topic in (
            "tns1:RuleEngine/CellMotionDetector/Motion",
            "other:VideoSource/MotionAlarm",
            "x:RuleEngine/x:MotionRegionDetector/x:Motion",
        ):
            for name in ("IsMotion", "State", "LogicalState"):
                for value in ("true", "1", " TRUE "):
                    with self.subTest(topic=topic, name=name, value=value):
                        motion = parse_motion(notification(topic, value, name=name))
                        self.assertIsNotNone(motion)
                        self.assertEqual(motion.camera_time, "2026-11-10T12:00:00Z")
                        self.assertIsNotNone(datetime.fromisoformat(motion.received_at).tzinfo)

    def test_ignore_unrelated_false_deleted_and_unknown_properties(self):
        for item in (
            notification(value="false"),
            notification(value="0"),
            notification(operation="Deleted"),
            notification(topic="tns1:RuleEngine/TamperDetector/Tamper"),
            notification(name="VideoSource"),
            etree.Element(f"{{{WSNT}}}NotificationMessage"),
        ):
            self.assertIsNone(parse_motion(item))

    def test_initialized_active_state_is_captured(self):
        self.assertIsNotNone(parse_motion(notification(operation="Initialized")))

    def test_source_items_cannot_trigger_motion(self):
        item = notification(value="false")
        source = item.find(f".//{{{SCHEMA}}}Source")[0]
        source.set("Name", "IsMotion")
        source.set("Value", "true")
        self.assertIsNone(parse_motion(item))

    def test_malformed_motion_is_reported(self):
        item = notification()
        message = item.find(f"{{{WSNT}}}Message")
        message.remove(message[0])
        with self.assertLogs("tapo_camera", level="WARNING"):
            self.assertIsNone(parse_motion(item))
        with self.assertLogs("tapo_camera", level="WARNING"):
            self.assertIsNone(parse_motion(notification(value="unknown")))


class CaptureTests(unittest.TestCase):
    def test_motion_video_is_persisted_with_hook_and_manual_photo_stays_jpeg(self):
        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "u", "p", output=Path(directory))
            snapshot = Mock(return_value=JPEG)
            video = Mock(return_value=b"\x00\x00\x00\x20ftypisom")
            hook = Mock()
            recorder = Recorder(settings, snapshot, hook, motion_video=video)
            saved = recorder.record(Motion("motion", None, "received"))
            self.assertEqual(saved.media_type, "video")
            self.assertEqual(saved.image.suffix, ".mp4")
            report = json.loads(saved.metadata.read_text())
            self.assertIsNone(report["image"])
            self.assertEqual(report["video"], saved.image.name)
            hook.assert_called_once_with(saved)
            snapshot.assert_not_called()
            self.assertIsNone(recorder.record(Motion("motion", None, "received")))
            video.assert_called_once_with()
            manual = recorder.record()
            self.assertEqual(manual.media_type, "photo")
            self.assertEqual(manual.image.suffix, ".jpg")
            snapshot.assert_called_once_with()

    def test_video_failures_never_return_success_or_leak_credentials(self):
        settings = Settings("192.0.2.1", "u", "private-password")
        for failure in (
            subprocess.TimeoutExpired("private-password", 20),
            OSError("private-password"),
            SimpleNamespace(returncode=1),
            SimpleNamespace(returncode=0),
        ):
            with self.subTest(failure=type(failure).__name__), patch(
                "tapo_camera.monitor.subprocess.run"
            ) as run:
                if isinstance(failure, Exception):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                with self.assertRaises(CameraError) as raised:
                    capture_video(settings, "ffmpeg")
                self.assertNotIn("private-password", str(raised.exception))

    def test_real_video_encoding_duration_size_and_decode(self):
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        real_run = subprocess.run

        def synthetic_input(command, **kwargs):
            command = list(command)
            index = command.index("-rtsp_transport")
            del command[index:index + 2]
            index = command.index("-i")
            command[index:index + 2] = [
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
            ]
            return real_run(command, **kwargs)

        with patch("tapo_camera.monitor.subprocess.run", side_effect=synthetic_input):
            data = capture_video(Settings("192.0.2.1", "u", "p"), executable)
        self.assertLessEqual(len(data), 2 * 1024 * 1024)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(data)
            decoded = real_run(
                [
                    executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-i", str(path), "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
                ],
                capture_output=True, timeout=20, check=True,
            )
            # 3 seconds at 10 fps, scaled to 640x480, RGB24.
            self.assertEqual(len(decoded.stdout), 30 * 640 * 480 * 3)

    def test_bundled_ffmpeg_generates_saves_and_decodes_real_jpeg(self):
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        generated = subprocess.run(
            [
                executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=1",
                "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "2",
                "-f", "image2pipe", "pipe:1",
            ],
            capture_output=True, timeout=20, check=True,
        )
        self.assertTrue(generated.stdout.startswith(b"\xff\xd8"))
        self.assertTrue(generated.stdout.endswith(b"\xff\xd9"))
        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "u", "p", output=Path(directory))
            saved = save_capture(settings, generated.stdout, None)
            decoded = subprocess.run(
                [
                    executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-i", str(saved.image), "-frames:v", "1",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
                ],
                capture_output=True, timeout=20, check=True,
            )
            self.assertEqual(len(decoded.stdout), 320 * 240 * 3)

    def test_persistent_pair_and_hook_ready_order(self):
        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "user", "secret", output=Path(directory))
            seen = []

            def hook(saved):
                self.assertEqual(saved.image.read_bytes(), JPEG)
                seen.append(json.loads(saved.metadata.read_text(encoding="utf-8")))

            recorder = Recorder(settings, lambda: JPEG, hook, lambda: 10)
            saved = recorder.record(Motion("motion-topic", "camera-time", "received-time"))
            self.assertEqual(seen[0]["reason"], "motion")
            self.assertEqual(seen[0]["image"], saved.image.name)
            self.assertEqual(seen[0]["camera_time"], "camera-time")
            self.assertEqual(seen[0]["received_at"], "received-time")
            self.assertEqual(seen[0]["topic"], "motion-topic")
            self.assertEqual(seen[0]["message"], saved.message)
            self.assertRegex(
                saved.message,
                r"^Movimento rilevato \d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}$",
            )
            self.assertNotIn("secret", saved.metadata.read_text())
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])
            second = save_capture(settings, JPEG, None)
            self.assertNotEqual(second.image, saved.image)
            self.assertEqual(json.loads(second.metadata.read_text())["reason"], "manual")
            self.assertTrue(second.message.startswith("Screenshot manuale "))

    def test_motion_hook_logs_message_and_image_together(self):
        from tapo_camera.monitor import on_capture

        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "u", "p", output=Path(directory))
            recorder = Recorder(settings, lambda: JPEG, on_capture)
            with self.assertLogs("tapo_camera", level="INFO") as logs:
                saved = recorder.record(Motion("topic", None, "received"))
            self.assertIn(saved.message, logs.output[0])
            self.assertIn(str(saved.image), logs.output[0])

    def test_cooldown_exact_boundary_and_manual_override(self):
        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "u", "p", output=Path(directory))
            clock = Mock(return_value=0)
            snapshot = Mock(return_value=JPEG)
            recorder = Recorder(settings, snapshot, Mock(), clock)
            event = Motion("topic", None, "received")
            self.assertIsNotNone(recorder.record(event))
            clock.return_value = 9.999
            self.assertIsNone(recorder.record(event))
            clock.return_value = 10
            self.assertIsNotNone(recorder.record(event))
            self.assertIsNotNone(recorder.record())
            self.assertEqual(snapshot.call_count, 3)

    def test_capture_failure_does_not_consume_cooldown(self):
        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "u", "p", output=Path(directory))
            snapshot = Mock(side_effect=[CameraError("capture failed"), JPEG])
            recorder = Recorder(settings, snapshot, Mock(), lambda: 0)
            event = Motion("topic", None, "received")
            with self.assertRaises(CameraError):
                recorder.record(event)
            self.assertIsNotNone(recorder.record(event))

    def test_hook_failure_retains_files_and_logs_without_secret(self):
        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "u", "p", output=Path(directory))
            recorder = Recorder(
                settings, lambda: JPEG,
                Mock(side_effect=RuntimeError("private hook credentials")),
            )
            with self.assertLogs("tapo_camera", level="ERROR") as logs:
                saved = recorder.record()
            self.assertTrue(saved.image.is_file())
            self.assertTrue(saved.metadata.is_file())
            self.assertNotIn("private hook credentials", "".join(logs.output))

    def test_failed_json_commit_never_calls_hook(self):
        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "u", "p", output=Path(directory))
            hook = Mock()
            recorder = Recorder(settings, lambda: JPEG, hook)
            with patch("tapo_camera.monitor.json.dump", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    recorder.record()
            hook.assert_not_called()
            self.assertEqual(list(Path(directory).rglob("*.json")), [])
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])
            self.assertEqual(len(list(Path(directory).rglob("*.jpg"))), 1)

    def test_ffmpeg_output_and_credential_escaping(self):
        settings = Settings("192.0.2.1", "u@ser", "p:/@?#")
        with patch("tapo_camera.monitor.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=JPEG)
            self.assertEqual(capture_frame(settings, "ffmpeg.exe"), JPEG)
        command = run.call_args.args[0]
        self.assertIn("rtsp://u%40ser:p%3A%2F%40%3F%23@192.0.2.1:554/stream1", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertNotIn("p:/@?#", repr(settings))

    def test_ffmpeg_failures_are_safe(self):
        settings = Settings("192.0.2.1", "u", "private-password")
        cases = (
            subprocess.TimeoutExpired("private-password", 20),
            OSError("private-password"),
            SimpleNamespace(returncode=1, stdout=b"", stderr=b"private-password"),
            SimpleNamespace(returncode=0, stdout=b"not a jpeg"),
        )
        for result in cases:
            with self.subTest(result=type(result).__name__):
                with patch("tapo_camera.monitor.subprocess.run") as run:
                    if isinstance(result, Exception):
                        run.side_effect = result
                    else:
                        run.return_value = result
                    with self.assertRaises(CameraError) as raised:
                        capture_frame(settings, "ffmpeg")
                    self.assertNotIn("private-password", str(raised.exception))


class MonitorTests(unittest.TestCase):
    def test_real_motion_payload_reaches_durable_capture_hook(self):
        with TemporaryDirectory() as directory:
            settings = Settings("192.0.2.1", "u", "p", output=Path(directory))
            seen = []

            def hook(saved):
                self.assertEqual(saved.image.read_bytes(), JPEG)
                self.assertEqual(json.loads(saved.metadata.read_text())["reason"], "motion")
                seen.append(saved)

            source = Mock()
            source.pull.side_effect = [[parse_motion(notification())], KeyboardInterrupt]
            recorder = Recorder(settings, lambda: JPEG, hook)
            with self.assertRaises(KeyboardInterrupt):
                monitor(lambda: source, recorder)
            self.assertEqual(len(seen), 1)
            source.close.assert_called_once()

    def test_reconnect_after_connection_and_pull_failure_with_cleanup(self):
        source = Mock()
        source.pull.side_effect = CameraError("connection lost")
        connect = Mock(side_effect=[CameraError("offline"), source])
        sleep = Mock(side_effect=[None, KeyboardInterrupt])
        with self.assertLogs("tapo_camera", level="ERROR"), self.assertRaises(KeyboardInterrupt):
            monitor(connect, Mock(), sleep)
        self.assertEqual(connect.call_count, 2)
        source.close.assert_called_once()
        self.assertEqual(sleep.call_count, 2)

    def test_capture_failure_keeps_subscription_and_later_events_work(self):
        source = Mock()
        first = Motion("topic", None, "first")
        second = Motion("topic", None, "second")
        source.pull.side_effect = [[first, second], KeyboardInterrupt]
        recorder = Mock()
        recorder.record.side_effect = [OSError("private path"), None]
        with self.assertLogs("tapo_camera", level="ERROR") as logs:
            with self.assertRaises(KeyboardInterrupt):
                monitor(lambda: source, recorder)
        self.assertEqual(recorder.record.call_count, 2)
        source.close.assert_called_once()
        self.assertNotIn("private path", "".join(logs.output))


class SoapTransport(Transport):
    """Exercise installed WSDL bindings and real Zeep event deserialization offline."""

    def __init__(self):
        super().__init__()
        self.operations = []
        self.fail_pull = False

    def post_xml(self, address, envelope, headers):
        body = envelope.find("{http://www.w3.org/2003/05/soap-envelope}Body")
        operation = etree.QName(body[0]).localname
        self.operations.append((operation, address))
        if operation == "GetCapabilities":
            content = (
                f'<td:GetCapabilitiesResponse xmlns:td="{DEVICE}" xmlns:tt="{SCHEMA}">'
                '<td:Capabilities><tt:Events>'
                '<tt:XAddr>http://192.0.2.1:2020/events</tt:XAddr>'
                '<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>'
                '<tt:WSPullPointSupport>true</tt:WSPullPointSupport>'
                '<tt:WSPausableSubscriptionManagerInterfaceSupport>false'
                '</tt:WSPausableSubscriptionManagerInterfaceSupport>'
                '</tt:Events></td:Capabilities></td:GetCapabilitiesResponse>'
            )
        elif operation == "CreatePullPointSubscription":
            content = (
                f'<tev:CreatePullPointSubscriptionResponse xmlns:tev="{EVENTS}" '
                f'xmlns:wsa="{WSA}" xmlns:wsnt="{WSNT}">'
                '<tev:SubscriptionReference>'
                '<wsa:Address>http://192.0.2.1:2020/subscription/42</wsa:Address>'
                '</tev:SubscriptionReference>'
                '<wsnt:CurrentTime>2026-11-10T12:00:00Z</wsnt:CurrentTime>'
                '<wsnt:TerminationTime>2026-11-10T12:01:00Z</wsnt:TerminationTime>'
                '</tev:CreatePullPointSubscriptionResponse>'
            )
        elif operation == "PullMessages":
            if self.fail_pull:
                raise OSError("private transport diagnostics")
            content = (
                f'<tev:PullMessagesResponse xmlns:tev="{EVENTS}" xmlns:wsnt="{WSNT}">'
                '<tev:CurrentTime>2026-11-10T12:00:01Z</tev:CurrentTime>'
                '<tev:TerminationTime>2026-11-10T12:01:00Z</tev:TerminationTime>'
                '<wsnt:NotificationMessage><wsnt:Topic '
                'Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">'
                'tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>'
                '<wsnt:Message>'
                f'<tt:Message xmlns:tt="{SCHEMA}" UtcTime="2026-11-10T12:00:01Z" '
                'PropertyOperation="Changed"><tt:Data>'
                '<tt:SimpleItem Name="IsMotion" Value="true"/>'
                '</tt:Data></tt:Message></wsnt:Message>'
                '</wsnt:NotificationMessage></tev:PullMessagesResponse>'
            )
        elif operation == "Renew":
            content = (
                f'<wsnt:RenewResponse xmlns:wsnt="{WSNT}">'
                '<wsnt:TerminationTime>2026-11-10T12:02:00Z</wsnt:TerminationTime>'
                '<wsnt:CurrentTime>2026-11-10T12:01:00Z</wsnt:CurrentTime>'
                '</wsnt:RenewResponse>'
            )
        elif operation == "Unsubscribe":
            content = f'<wsnt:UnsubscribeResponse xmlns:wsnt="{WSNT}"/>'
        else:
            raise AssertionError(f"Unexpected operation: {operation}")
        response = Response()
        response.status_code = 200
        response._content = (
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f"<s:Body>{content}</s:Body></s:Envelope>"
        ).encode()
        response.headers["Content-Type"] = "application/soap+xml"
        return response


class OnvifBindingTests(unittest.TestCase):
    def test_real_wsdl_subscription_pull_renew_and_unsubscribe(self):
        transport = SoapTransport()
        with patch("tapo_camera.onvif_events.Transport", return_value=transport):
            source = OnvifEvents(Settings("192.0.2.1", "dummy-user", "dummy-password"))
        events = source.pull()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].topic, "RuleEngine/CellMotionDetector/Motion")
        source.renew_at = 0
        self.assertEqual(len(source.pull()), 1)
        with patch.object(source.session, "close") as close:
            source.close()
        close.assert_called_once()
        self.assertEqual(
            [operation for operation, _ in transport.operations],
            ["GetCapabilities", "CreatePullPointSubscription", "PullMessages",
             "Renew", "PullMessages", "Unsubscribe"],
        )
        for operation, address in transport.operations[2:]:
            self.assertEqual(address, "http://192.0.2.1:2020/subscription/42")

    def test_transport_failure_is_safe(self):
        transport = SoapTransport()
        with patch("tapo_camera.onvif_events.Transport", return_value=transport):
            source = OnvifEvents(Settings("192.0.2.1", "dummy-user", "dummy-password"))
        transport.fail_pull = True
        try:
            with self.assertRaises(CameraError) as raised:
                source.pull()
            self.assertNotIn("private transport diagnostics", str(raised.exception))
        finally:
            source.close()

    def test_fault_and_malformed_xml_trigger_explicit_reconnect_error(self):
        for content in (
            b"<invalid",
            (
                f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                '<s:Body><s:Fault/></s:Body></s:Envelope>'
            ).encode(),
        ):
            transport = SoapTransport()
            with patch("tapo_camera.onvif_events.Transport", return_value=transport):
                source = OnvifEvents(Settings("192.0.2.1", "dummy-user", "dummy-password"))
            response = Response()
            response.status_code = 200
            response._content = content
            try:
                with patch.object(transport, "post_xml", return_value=response):
                    with self.assertRaises(CameraError):
                        source.pull()
            finally:
                source.close()

    def test_constructor_failure_closes_http_session(self):
        with patch("tapo_camera.onvif_events.ONVIFService", side_effect=OSError("private")), patch(
            "tapo_camera.onvif_events.Session"
        ) as session:
            with self.assertRaises(CameraError) as raised:
                OnvifEvents(Settings("192.0.2.1", "dummy-user", "dummy-password"))
        session.return_value.close.assert_called_once()
        self.assertNotIn("private", str(raised.exception))

    def test_renewal_uses_camera_relative_lifetime(self):
        source = OnvifEvents.__new__(OnvifEvents)
        now = datetime(2001, 1, 1, tzinfo=timezone.utc)
        with patch("tapo_camera.onvif_events.time.monotonic", return_value=100):
            source._set_renewal(SimpleNamespace(CurrentTime=now, TerminationTime=now + timedelta(seconds=20)))
        self.assertEqual(source.renew_at, 110)
        with self.assertRaises(CameraError):
            source._set_renewal(SimpleNamespace(CurrentTime=now, TerminationTime=now))


class CameraConfigurationTests(unittest.TestCase):
    def test_installed_cli_uses_working_directory_env_outside_source_tree(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / ".env").write_text(
                "TAPO_CAMERA_AUTH=invalid\n", encoding="utf-8",
            )
            environment = {
                name: value for name, value in os.environ.items()
                if not name.startswith(("TAPO_", "KASA_")) and name != "PYTHONPATH"
            }
            result = subprocess.run(
                [sys.executable, "-m", "tapo_camera", "privacy-status"],
                cwd=directory, env=environment,
                capture_output=True, text=True, timeout=20, check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("TAPO_CAMERA_AUTH", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_default_host_and_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            arguments = parser().parse_args(["watch"])
        self.assertEqual(arguments.host, "192.168.1.201")
        for options in (
            ["watch", "--host", "bad"], ["watch", "--cooldown", "0"],
            ["watch", "--capture-timeout", "-1"], ["watch", "--onvif-port", "65536"],
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(options)

    def test_camera_credentials_are_required_even_with_legacy_plug_variables(self):
        for camera_pair in ({}, {"TAPO_CAMERA_USERNAME": "", "TAPO_CAMERA_PASSWORD": ""}):
            with self.subTest(camera_pair=camera_pair), TemporaryDirectory() as directory:
                with patch("tapo_camera.cli.load_dotenv"), patch.dict(
                    os.environ,
                    {"KASA_USERNAME": "shared-user", "KASA_PASSWORD": "shared-password", **camera_pair},
                    clear=True,
                ), patch("tapo_camera.cli.capture_frame") as capture, redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["snapshot", "--output", directory]), 2)
                capture.assert_not_called()

    def test_partial_camera_credentials_never_mix_with_kasa(self):
        for camera_pair in (
            {"TAPO_CAMERA_USERNAME": "camera-user"},
            {"TAPO_CAMERA_PASSWORD": "camera-password"},
        ):
            with self.subTest(camera_pair=camera_pair), patch("tapo_camera.cli.load_dotenv"), patch.dict(
                os.environ,
                {"KASA_USERNAME": "shared-user", "KASA_PASSWORD": "shared-password", **camera_pair},
                clear=True,
            ), patch("tapo_camera.cli.capture_frame") as capture, redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["snapshot"]), 2)
            capture.assert_not_called()
            self.assertNotIn("shared-password", stderr.getvalue())

    def test_missing_credentials_fail_before_capture(self):
        with patch("tapo_camera.cli.load_dotenv"), patch.dict(
            os.environ, {}, clear=True
        ), patch("tapo_camera.cli.capture_frame") as capture, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["snapshot"]), 2)
        capture.assert_not_called()

    def test_snapshot_cli_wires_settings_and_persists(self):
        with TemporaryDirectory() as directory:
            with patch("tapo_camera.cli.load_dotenv"), patch.dict(
                os.environ, {
                    "TAPO_CAMERA_USERNAME": "local-user", "TAPO_CAMERA_PASSWORD": "dummy",
                    "KASA_USERNAME": "other-user", "KASA_PASSWORD": "other-password",
                },
                clear=True,
            ), patch("tapo_camera.cli.capture_frame", return_value=JPEG) as capture:
                self.assertEqual(main(["snapshot", "--output", directory]), 0)
            self.assertEqual(capture.call_args.args[0].host, "192.168.1.201")
            self.assertEqual(capture.call_args.args[0].username, "local-user")
            self.assertEqual(capture.call_args.args[0].password, "dummy")
            self.assertEqual(len(list(Path(directory).rglob("*.jpg"))), 1)
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
