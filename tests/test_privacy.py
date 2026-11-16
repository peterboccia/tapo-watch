import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import AsyncMock, Mock, patch

import requests

from tapo_camera.cli import main
from tapo_camera.monitor import CameraError
from tapo_camera.privacy import PrivacyController, PrivacySettings


class PrivacyTests(unittest.TestCase):
    def controller(self, client):
        with patch("pytapo.Tapo", return_value=client) as factory:
            result = PrivacyController(PrivacySettings("192.0.2.1", "u", "secret"))
        self.assertFalse(factory.call_args.kwargs["printDebugInformation"])
        self.assertFalse(factory.call_args.kwargs["printWarnInformation"])
        self.assertFalse(factory.call_args.kwargs["retryStok"])
        return result

    def test_status_only_reads_and_parses_strictly(self):
        client = Mock()
        controller = self.controller(client)
        for value, expected in (("on", True), ("off", False)):
            client.getPrivacyMode.return_value = {"enabled": value}
            self.assertIs(controller.status(), expected)
        client.setPrivacyMode.assert_not_called()
        for response in (None, {}, {"enabled": True}, {"enabled": "unknown"}, ["on"]):
            client.getPrivacyMode.return_value = response
            with self.assertRaises(CameraError):
                controller.status()

    def test_changes_are_read_back_in_order(self):
        for enabled in (False, True):
            client = Mock()
            client.getPrivacyMode.return_value = {"enabled": "on" if enabled else "off"}
            controller = self.controller(client)
            self.assertIs(controller.set(enabled), enabled)
            self.assertEqual(
                [call[0] for call in client.mock_calls],
                ["setPrivacyMode", "getPrivacyMode"],
            )
            client.setPrivacyMode.assert_called_once_with(enabled)

    def test_mismatch_and_unavailable_readback_never_report_success(self):
        for response in ({"enabled": "off"}, {}, None):
            client = Mock()
            client.getPrivacyMode.return_value = response
            with self.assertRaises(CameraError):
                self.controller(client).set(True)
            client.setPrivacyMode.assert_called_once_with(True)

    def test_readback_exception_is_safe_and_does_not_retry_write(self):
        client = Mock()
        client.getPrivacyMode.side_effect = RuntimeError("private-password")
        with self.assertRaises(CameraError) as raised:
            self.controller(client).set(True)
        self.assertIn("stato incerto", str(raised.exception))
        self.assertNotIn("private-password", str(raised.exception))
        client.setPrivacyMode.assert_called_once()

    def test_write_and_read_failures_are_safe(self):
        for operation in ("setPrivacyMode", "getPrivacyMode"):
            client = Mock()
            getattr(client, operation).side_effect = RuntimeError("private-password")
            controller = self.controller(client)
            with self.assertRaises(CameraError) as raised:
                controller.set(True) if operation == "setPrivacyMode" else controller.status()
            self.assertNotIn("private-password", str(raised.exception))
            if operation == "setPrivacyMode":
                client.getPrivacyMode.assert_not_called()

    def test_constructor_failure_is_safe(self):
        with patch("pytapo.Tapo", side_effect=RuntimeError("private-password")):
            with self.assertRaises(CameraError) as raised:
                PrivacyController(PrivacySettings("192.0.2.1", "u", "secret"))
        self.assertNotIn("private-password", str(raised.exception))
        self.assertNotIn("secret", repr(PrivacySettings("192.0.2.1", "u", "secret")))

    def test_connection_diagnostics_classify_without_leaking_raw_errors(self):
        cases = (
            (requests.exceptions.SSLError("private-password"), "[TLS]"),
            (requests.Timeout("private-password"), "[TIMEOUT]"),
            (TimeoutError("private-password"), "[TIMEOUT]"),
            (requests.ConnectionError("private-password"), "[RETE]"),
            (PermissionError("private-password"), "[PERMESSI]"),
            (Exception("Invalid authentication data"), "[AUTH]"),
            (Exception("Invalid login credentials"), "[AUTH]"),
            (Exception("Failure detecting hashing algorithm."), "[PROTOCOLLO]"),
            (Exception("Failed to detect device type."), "[PROTOCOLLO]"),
            (Exception("Error communicating with Tapo Camera. Status code: 401"), "[HTTP 401]"),
            (Exception("Invalid authentication data private-password"), "[NON_CLASSIFICATO]"),
            (Exception("Error communicating with Tapo Camera. Status code: 401 private-password"), "[NON_CLASSIFICATO]"),
            (RuntimeError("private-password"), "[NON_CLASSIFICATO]"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected), patch("pytapo.Tapo", side_effect=error):
                with self.assertRaises(CameraError) as raised:
                    PrivacyController(PrivacySettings("192.0.2.1", "u", "secret"))
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("private-password", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_transport_cleanup(self):
        client = Mock()
        client.transport.close = AsyncMock()
        self.controller(client).close()
        client.transport.close.assert_awaited_once()

    def test_suspension_is_actionable_without_raw_diagnostics(self):
        with patch("pytapo.Tapo", side_effect=Exception(
            "Temporary Suspension: private server diagnostics"
        )):
            with self.assertRaises(CameraError) as raised:
                PrivacyController(PrivacySettings("192.0.2.1", "u", "secret"))
        self.assertIn("temporaneamente sospesa", str(raised.exception))
        self.assertNotIn("private server", str(raised.exception))


class PrivacyCliTests(unittest.TestCase):
    def invoke(self, arguments, environment, controller):
        with patch("tapo_camera.cli.load_dotenv"), patch.dict(
            os.environ, environment, clear=True
        ), patch("tapo_camera.cli.PrivacyController", return_value=controller) as factory, patch(
            "tapo_camera.cli.capture_frame"
        ) as snapshot, patch("tapo_camera.cli.monitor") as watch, redirect_stdout(
            io.StringIO()
        ) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = main(arguments)
        snapshot.assert_not_called()
        watch.assert_not_called()
        return result, stdout.getvalue(), stderr.getvalue(), factory

    def test_commands_use_camera_credentials_and_verified_output(self):
        env = {
            "TAPO_CAMERA_USERNAME": "camera-user", "TAPO_CAMERA_PASSWORD": "camera-secret",
            "KASA_USERNAME": "cloud-user", "KASA_PASSWORD": "cloud-secret",
        }
        for command, enabled in (("privacy-status", True), ("privacy-on", True), ("privacy-off", False)):
            controller = Mock()
            controller.status.return_value = controller.set.return_value = enabled
            result, output, _, factory = self.invoke([command], env, controller)
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output), {
                "privacy": "on" if enabled else "off", "state_verified": True,
            })
            self.assertEqual(factory.call_args.args[0].username, "camera-user")
            self.assertEqual(factory.call_args.args[0].password, "camera-secret")
            if command == "privacy-status":
                controller.set.assert_not_called()
                controller.status.assert_called_once()
            else:
                controller.set.assert_called_once_with(enabled)
            controller.close.assert_called_once()

    def test_camera_empty_pair_rejects_legacy_plug_credentials(self):
        controller = Mock()
        result, _, _, factory = self.invoke(
            ["privacy-status"], {"KASA_USERNAME": "u", "KASA_PASSWORD": "p"}, controller,
        )
        self.assertEqual(result, 2)
        factory.assert_not_called()

    def test_partial_pair_missing_password_and_invalid_auth_fail_before_network(self):
        for env in (
            {"TAPO_CAMERA_USERNAME": "u", "KASA_USERNAME": "cloud", "KASA_PASSWORD": "secret"},
            {"TAPO_CAMERA_PASSWORD": "secret", "KASA_USERNAME": "cloud", "KASA_PASSWORD": "secret"},
            {}, {"TAPO_CAMERA_AUTH": "admin"}, {"TAPO_CAMERA_AUTH": "invalid"},
        ):
            result, _, error, factory = self.invoke(["privacy-status"], env, Mock())
            self.assertEqual(result, 2)
            factory.assert_not_called()
            self.assertNotIn("secret", error)

    def test_explicit_admin_and_environment_with_cli_override(self):
        for args, env, expected in (
            (["privacy-status", "--auth", "admin"], {"TAPO_CAMERA_ADMIN_PASSWORD": "cloud"}, "cloud"),
            (["privacy-status"], {"TAPO_CAMERA_AUTH": "admin", "TAPO_CAMERA_ADMIN_PASSWORD": "explicit", "KASA_PASSWORD": "cloud"}, "explicit"),
        ):
            result, _, _, factory = self.invoke(args, env, Mock())
            self.assertEqual(result, 0)
            self.assertEqual(factory.call_args.args[0].username, "admin")
            self.assertEqual(factory.call_args.args[0].password, expected)
        result, _, _, factory = self.invoke(
            ["privacy-status", "--auth", "camera", "--control-port", "444"],
            {"TAPO_CAMERA_AUTH": "admin", "TAPO_CAMERA_USERNAME": "local", "TAPO_CAMERA_PASSWORD": "p"},
            Mock(),
        )
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.args[0].username, "local")
        self.assertEqual(factory.call_args.args[0].control_port, 444)

    def test_failure_does_not_report_verified_or_fallback(self):
        controller = Mock()
        controller.set.side_effect = CameraError("Verifica fallita")
        result, output, _, factory = self.invoke(
            ["privacy-on"], {"TAPO_CAMERA_USERNAME": "u", "TAPO_CAMERA_PASSWORD": "p"}, controller,
        )
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        factory.assert_called_once()
        controller.close.assert_called_once()

    def test_watch_never_constructs_privacy_controller(self):
        with patch("tapo_camera.cli.load_dotenv"), patch.dict(
            os.environ, {"TAPO_CAMERA_USERNAME": "u", "TAPO_CAMERA_PASSWORD": "p"},
            clear=True,
        ), patch("tapo_camera.cli.PrivacyController") as privacy, patch(
            "tapo_camera.cli.monitor"
        ) as watch:
            self.assertEqual(main(["watch"]), 0)
        privacy.assert_not_called()
        watch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
