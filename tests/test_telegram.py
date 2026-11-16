import io
import json
import os
import shutil
import threading
import time
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from tapo_camera.cli import main
from tapo_camera.monitor import CameraError, Capture, Motion, Recorder, Settings, monitor
from tapo_camera.telegram import (
    HELP, MotionDelivery, TelegramAPI, TelegramBot, TelegramError, TelegramSettings,
    run_telegram,
)

TOKEN = "123456:" + "x" * 35
USERS = frozenset({123456789, 987654321})
UID = 123456789
JPEG = b"\xff\xd8test-image\xff\xd9"


def update(text="/stato", uid=UID, date=1000, update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": uid, "is_bot": False},
            "chat": {"id": uid, "type": "private"},
            "date": date, "text": text,
        },
    }


class TelegramConfigurationTests(unittest.TestCase):
    def test_requires_explicit_token_and_numeric_user_whitelist(self):
        for value in ("", " ", "+3912345", "@spouse", "-123", "0", "1,", "1,,2",
                      "1.5", "True", str(2**52)):
            with self.subTest(value=value), patch.dict(os.environ, {
                "TAPO_TELEGRAM_BOT_TOKEN": TOKEN, "TAPO_TELEGRAM_USER_IDS": value,
            }, clear=True), self.assertRaises(TelegramError):
                TelegramSettings.from_env()
        for value in ("", "not-a-token", "123:a", TOKEN + "\nunsafe"):
            with self.subTest(token=value), self.assertRaises(TelegramError):
                TelegramSettings(value, USERS)
        for ids in (frozenset(), frozenset({True}), frozenset({-1}), frozenset({"123"})):
            with self.assertRaises(TelegramError):
                TelegramSettings(TOKEN, ids)

    def test_parse_deduplicate_and_hide_token_in_repr(self):
        with patch.dict(os.environ, {
            "TAPO_TELEGRAM_BOT_TOKEN": TOKEN,
            "TAPO_TELEGRAM_USER_IDS": "123456789, 987654321,123456789",
        }, clear=True):
            settings = TelegramSettings.from_env()
        self.assertEqual(settings.user_ids, USERS)
        self.assertNotIn(TOKEN, repr(settings))


class TelegramAPITests(unittest.TestCase):
    def setUp(self):
        self.api = TelegramAPI(TelegramSettings(TOKEN, USERS))
        self.api.session.close()
        self.api.session = Mock()
        self.response = self.api.session.post.return_value
        self.response.json.return_value = {"ok": True, "result": []}

    def test_transport_https_timeout_no_redirects_and_no_environment_proxy(self):
        fresh = TelegramAPI(TelegramSettings(TOKEN, USERS))
        self.assertFalse(fresh.session.trust_env)
        fresh.close()
        self.api.updates(17)
        args, kwargs = self.api.session.post.call_args
        self.assertEqual(args[0], f"https://api.telegram.org/bot{TOKEN}/getUpdates")
        self.assertEqual(kwargs["timeout"], (5, 25))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["data"], {
            "offset": 17, "timeout": 20, "limit": 100,
            "allowed_updates": '["message"]',
        })
        self.response.close.assert_called_once()

    def test_errors_are_sanitized_and_response_closed(self):
        for error in (
            requests.Timeout(f"secret {TOKEN}"),
            requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}"),
        ):
            self.api.session.post.side_effect = error
            with self.assertRaises(TelegramError) as raised:
                self.api.updates(0)
            self.assertNotIn(TOKEN, str(raised.exception))
        self.api.session.post.side_effect = None
        for body in ({"ok": False, "description": TOKEN}, {}, [], {"ok": True}):
            self.response.json.return_value = body
            with self.assertRaises(TelegramError) as raised:
                self.api.updates(0)
            self.assertNotIn(TOKEN, str(raised.exception))
        self.response.json.side_effect = ValueError(TOKEN)
        with self.assertRaises(TelegramError):
            self.api.updates(0)
        self.assertGreater(self.response.close.call_count, 0)

    def test_malformed_update_lists_fail_closed(self):
        for result in ({}, [None], [{}], [{"update_id": True}], [{"update_id": -1}]):
            self.response.json.return_value = {"ok": True, "result": result}
            with self.assertRaises(TelegramError):
                self.api.updates(0)

    def test_outbound_denies_unknown_users_before_opening_any_file(self):
        capture = Capture(Path("must-not-open.jpg"), Path("unused.json"), "caption")
        with patch.object(Path, "open") as opened:
            for uid in (0, -UID, 5, True, str(UID), "@user", "+391234"):
                self.api.text(uid, "help or error")
                self.api.photo(uid, capture)
                self.api.video(uid, capture)
        opened.assert_not_called()
        self.api.session.post.assert_not_called()
        for method in ("sendMessage", "sendPhoto", "sendVideo", "sendDocument", "getFile"):
            with self.assertRaises(TelegramError):
                self.api.call(method, {"chat_id": 555})
        self.api.session.post.assert_not_called()

    def test_photo_uses_multipart_jpeg_and_caption_without_parse_mode(self):
        capture = Capture(Path("known-recorder-file.jpg"), Path("unused.json"), "Movimento <camera>")
        stream = io.BytesIO(JPEG)
        with patch.object(Path, "open") as opened:
            opened.return_value.__enter__.return_value = stream
            self.api.photo(UID, capture)
        opened.assert_called_once_with("rb")
        kwargs = self.api.session.post.call_args.kwargs
        self.assertEqual(kwargs["data"]["chat_id"], UID)
        self.assertEqual(kwargs["data"]["caption"], capture.message)
        self.assertNotIn("parse_mode", kwargs["data"])
        self.assert_menu(kwargs["data"])
        self.assertEqual(kwargs["files"], {"photo": ("snapshot.jpg", stream, "image/jpeg")})

    def test_denial_is_only_non_whitelist_payload_allowed_without_menu_or_files(self):
        self.api.deny(555)
        data = self.api.session.post.call_args.kwargs["data"]
        self.assertEqual(data, {"chat_id": 555, "text": "Non sei nella whitelist."})
        self.assertIsNone(self.api.session.post.call_args.kwargs["files"])
        self.api.session.post.reset_mock()
        for data, files in (
            ({"chat_id": 555, "text": "Non sei nella whitelist.", "reply_markup": "{}"}, None),
            ({"chat_id": 555, "text": "Non sei nella whitelist."}, {"photo": b"private"}),
            ({"chat_id": -555, "text": "Non sei nella whitelist."}, None),
        ):
            with self.assertRaises(TelegramError):
                self.api.call("sendMessage", data, files)
        self.api.session.post.assert_not_called()

    def assert_menu(self, data):
        markup = json.loads(data["reply_markup"])
        self.assertEqual(
            [[button["text"] for button in row] for row in markup["keyboard"]],
            [["Foto", "Help"], ["Accendi", "Spegni"]],
        )
        self.assertTrue(markup["resize_keyboard"])
        self.assertTrue(markup["is_persistent"])
        self.assertFalse(markup["one_time_keyboard"])

    def test_video_is_sent_as_mp4_with_caption_menu_and_streaming(self):
        capture = Capture(Path("motion.mp4"), Path("motion.json"), "Movimento", "video")
        stream = io.BytesIO(b"video")
        with patch.object(Path, "open") as opened:
            opened.return_value.__enter__.return_value = stream
            self.api.video(UID, capture)
        args, kwargs = self.api.session.post.call_args
        self.assertTrue(args[0].endswith("/sendVideo"))
        self.assertEqual(kwargs["files"], {"video": ("motion.mp4", stream, "video/mp4")})
        self.assertEqual(kwargs["data"]["caption"], "Movimento")
        self.assertEqual(kwargs["data"]["supports_streaming"], "true")
        self.assert_menu(kwargs["data"])

    def test_text_sends_persistent_menu_to_authorized_user(self):
        self.api.text(UID, HELP)
        data = self.api.session.post.call_args.kwargs["data"]
        self.assertEqual(data["chat_id"], UID)
        self.assertEqual(data["text"], HELP)
        self.assert_menu(data)


class TelegramRoutingTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.recorder = Mock()
        self.capture = Capture(Path("unused.jpg"), Path("unused.json"), "Foto")
        self.recorder.record.return_value = self.capture
        self.privacy = Mock()
        self.privacy.status.return_value = False
        self.privacy.set.side_effect = lambda enabled: enabled
        self.factory = Mock(return_value=self.privacy)
        self.stop = threading.Event()
        self.clock = Mock(return_value=1000)
        self.monotonic = Mock(return_value=100)
        self.bot = TelegramBot(
            TelegramSettings(TOKEN, USERS), self.api, self.recorder, self.factory,
            self.stop, threading.Lock(), self.clock, self.monotonic,
        )
        self.bot.username = "HouseCameraBot"

    def reset_rate(self):
        self.monotonic.return_value += 4

    def assert_no_effects(self):
        self.api.assert_not_called()
        self.assertEqual(self.api.method_calls, [])
        self.recorder.record.assert_not_called()
        self.factory.assert_not_called()
        self.privacy.assert_not_called()
        self.assertEqual(self.privacy.method_calls, [])

    def test_all_unauthorized_surfaces_silent_for_every_command(self):
        for command in (
            "/stato", "/foto", "/accendi", "/spegni", "/help", "/unknown",
            "/start", "/menu", "Stato", "Foto", "Accendi", "Spegni", "Help",
        ):
            cases = []
            for mutate in (
                lambda m: m.pop("from"),
                lambda m: m.update({"from": None}),
                lambda m: m["from"].update({"id": str(UID)}),
                lambda m: m["from"].update({"id": True}),
                lambda m: m["from"].update({"is_bot": True}),
                lambda m: m["from"].pop("is_bot"),
                lambda m: m["chat"].update({"id": 555}),
                lambda m: m["chat"].update({"id": str(UID)}),
                lambda m: m["chat"].update({"type": "group"}),
                lambda m: m["chat"].update({"type": "supergroup"}),
                lambda m: m["chat"].update({"type": "channel"}),
                lambda m: m.update({"sender_chat": {"id": UID}}),
                lambda m: m.update({"via_bot": {"id": UID}}),
                lambda m: m.update({"forward_origin": {}}),
                lambda m: m.update({"edit_date": 1000}),
                lambda m: m.update({"contact": {"phone_number": "+391234"}}) or m.pop("from"),
            ):
                item = update(command)
                mutate(item["message"])
                cases.append(item)
            for key in ("edited_message", "channel_post", "edited_channel_post", "business_message"):
                item = update(command)
                item[key] = item.pop("message")
                cases.append(item)
            cases.extend([{}, {"callback_query": {"from": {"id": UID}, "data": command}}])
            for item in cases:
                with self.subTest(command=command, item=item):
                    self.bot.handle(item)
                    self.assert_no_effects()
        self.assertEqual(self.bot.last_command, {})

    def test_unknown_private_user_gets_only_rate_limited_denial_without_camera_access(self):
        self.bot.handle(update("/foto", uid=555))
        self.api.deny.assert_called_once_with(555)
        self.bot.handle(update("/help", uid=555))
        self.api.deny.assert_called_once()
        self.reset_rate()
        self.bot.handle(update("ciao", uid=555))
        self.assertEqual(self.api.deny.call_count, 2)
        self.assertEqual([call[0] for call in self.api.method_calls], ["deny", "deny"])
        self.recorder.record.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(self.privacy.method_calls, [])
        self.assertEqual(self.bot.last_command, {})

    def test_stale_forwarded_and_group_unknown_users_get_no_denial(self):
        stale = update("/foto", uid=555, date=999)
        forwarded = update("/foto", uid=555)
        forwarded["message"]["forward_origin"] = {}
        group = update("/foto", uid=555)
        group["message"]["chat"]["type"] = "group"
        for item in (stale, forwarded, group):
            self.bot.handle(item)
        self.assert_no_effects()

    def test_denial_failure_is_sanitized_without_camera_access(self):
        self.api.deny.side_effect = TelegramError(TOKEN)
        with self.assertLogs("tapo_camera", level="WARNING") as logs:
            self.bot.handle(update("/foto", uid=555))
        self.assertNotIn(TOKEN, str(logs.output))
        self.factory.assert_not_called()
        self.recorder.record.assert_not_called()

    def test_routes_all_commands_and_plain_text_and_own_mentions(self):
        self.bot.handle(update("/stato"))
        self.privacy.status.assert_called_once()
        self.assertIn("OFF", self.api.text.call_args.args[1])
        self.reset_rate()
        self.bot.handle(update("accendi"))
        self.privacy.set.assert_called_with(False)
        self.reset_rate()
        self.bot.handle(update("/spegni@housecamerabot"))
        self.privacy.set.assert_called_with(True)
        self.assertIn("ON", self.api.text.call_args.args[1])
        self.reset_rate()
        self.bot.handle(update(" FOTO "))
        self.recorder.record.assert_called_once_with()
        self.api.photo.assert_called_once_with(UID, self.capture)
        for command in ("/help", "help"):
            self.reset_rate()
            self.bot.handle(update(command))
            self.assertTrue(self.api.text.call_args.args[1].startswith(HELP + "\n\n"))
            self.assertIn("Privacy OFF", self.api.text.call_args.args[1])
        for command in ("/start", "/menu", "/unknown"):
            self.reset_rate()
            self.bot.handle(update(command))
            self.api.text.assert_called_with(UID, HELP)
        self.factory.assert_called_once()
        self.bot.close()
        self.privacy.close.assert_called_once()

    def test_menu_buttons_use_existing_camera_command_routes(self):
        self.bot.handle(update("Help"))
        self.privacy.status.assert_called_once()
        self.reset_rate()
        self.bot.handle(update("Foto"))
        self.recorder.record.assert_called_once_with()
        self.api.photo.assert_called_once_with(UID, self.capture)
        self.reset_rate()
        self.bot.handle(update("Accendi"))
        self.privacy.set.assert_called_with(False)
        self.reset_rate()
        self.bot.handle(update("Spegni"))
        self.privacy.set.assert_called_with(True)
        self.reset_rate()
        self.bot.handle(update("Help"))
        self.assertTrue(self.api.text.call_args.args[1].startswith(HELP + "\n\n"))
        self.assertIn("Privacy OFF", self.api.text.call_args.args[1])

    def test_help_and_status_alias_read_live_privacy_without_changing_it(self):
        for command, enabled in (("Help", True), ("/help", False), ("/stato", True)):
            with self.subTest(command=command):
                self.reset_rate()
                self.privacy.status.return_value = enabled

                def read_status():
                    self.assertTrue(self.bot.camera_lock.locked())
                    return enabled

                self.privacy.status.side_effect = read_status
                self.bot.handle(update(command))
                reply = self.api.text.call_args.args[1]
                self.assertTrue(reply.startswith(HELP + "\n\n"))
                self.assertIn("Privacy ON" if enabled else "Privacy OFF", reply)
        self.assertEqual(self.privacy.status.call_count, 3)
        self.privacy.set.assert_not_called()
        self.recorder.record.assert_not_called()

    def test_help_failed_read_keeps_help_and_reports_failure_without_fake_status(self):
        self.privacy.status.side_effect = CameraError(TOKEN)
        with self.assertLogs("tapo_camera", level="WARNING") as logs:
            self.bot.handle(update("Help"))
        reply = self.api.text.call_args.args[1]
        self.assertTrue(reply.startswith(HELP + "\n\n"))
        self.assertIn("Lettura dello stato privacy pytapo non riuscita", reply)
        self.assertNotIn("Camera raggiungibile", reply)
        self.assertNotIn(TOKEN, reply)
        self.assertNotIn(TOKEN, str(logs.output))
        self.privacy.set.assert_not_called()
        self.recorder.record.assert_not_called()

    def test_photo_with_privacy_off_never_changes_privacy(self):
        self.bot.handle(update("Foto"))
        self.privacy.status.assert_called_once_with()
        self.privacy.set.assert_not_called()
        self.recorder.record.assert_called_once_with()
        self.api.photo.assert_called_once_with(UID, self.capture)

    def test_photo_restores_privacy_before_sending_and_under_camera_lock(self):
        self.privacy.status.return_value = True
        order = []

        def set_privacy(enabled):
            self.assertTrue(self.bot.camera_lock.locked())
            order.append(enabled)
            return enabled

        def record():
            self.assertTrue(self.bot.camera_lock.locked())
            order.append("capture")
            return self.capture

        self.privacy.set.side_effect = set_privacy
        self.recorder.record.side_effect = record
        self.api.photo.side_effect = lambda *args: order.append("send")
        self.bot.handle(update("/foto"))
        self.assertEqual(order, [False, "capture", True, "send"])

    def test_photo_capture_failure_still_restores_privacy(self):
        self.privacy.status.return_value = True
        self.recorder.record.side_effect = CameraError(TOKEN)
        with self.assertLogs("tapo_camera", level="WARNING"):
            self.bot.handle(update("/foto"))
        self.assertEqual([call.args for call in self.privacy.set.call_args_list], [(False,), (True,)])
        self.api.photo.assert_not_called()
        reply = self.api.text.call_args.args[1]
        self.assertIn("Privacy ripristinata ON", reply)
        self.assertNotIn("incerto", reply)
        self.assertNotIn(TOKEN, reply)

    def test_photo_failed_disable_also_attempts_restore_without_capturing(self):
        self.privacy.status.return_value = True
        self.privacy.set.side_effect = [CameraError(TOKEN), True]
        with self.assertLogs("tapo_camera", level="WARNING"):
            self.bot.handle(update("/foto"))
        self.assertEqual([call.args for call in self.privacy.set.call_args_list], [(False,), (True,)])
        self.recorder.record.assert_not_called()
        self.api.photo.assert_not_called()
        self.assertIn("Privacy ripristinata ON", self.api.text.call_args.args[1])

    def test_photo_restore_failure_does_not_send_and_warns_uncertain_state(self):
        self.privacy.status.return_value = True
        self.privacy.set.side_effect = [False, CameraError(TOKEN)]
        with self.assertLogs("tapo_camera", level="WARNING") as logs:
            self.bot.handle(update("/foto"))
        self.recorder.record.assert_called_once_with()
        self.api.photo.assert_not_called()
        reply = self.api.text.call_args.args[1]
        self.assertIn("Ripristino", reply)
        self.assertIn("incerto", reply)
        self.assertNotIn(TOKEN, reply)
        self.assertNotIn(TOKEN, str(logs.output))

    def test_photo_read_failure_never_captures_or_changes_privacy(self):
        self.privacy.status.side_effect = CameraError(TOKEN)
        with self.assertLogs("tapo_camera", level="WARNING"):
            self.bot.handle(update("/foto"))
        self.privacy.set.assert_not_called()
        self.recorder.record.assert_not_called()
        self.api.photo.assert_not_called()
        self.assertNotIn("incerto", self.api.text.call_args.args[1])

    def test_photo_restores_privacy_when_interrupted_or_expired_after_disable(self):
        self.privacy.status.return_value = True
        self.recorder.record.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.bot.handle(update("/foto"))
        self.assertEqual([call.args for call in self.privacy.set.call_args_list], [(False,), (True,)])
        self.privacy.set.reset_mock()
        self.recorder.record.reset_mock()
        self.reset_rate()

        def slow_disable(enabled):
            if not enabled:
                self.clock.return_value = 1031
            return enabled

        self.privacy.set.side_effect = slow_disable
        self.bot.handle(update("/foto"))
        self.assertEqual([call.args for call in self.privacy.set.call_args_list], [(False,), (True,)])
        self.recorder.record.assert_not_called()
        self.api.photo.assert_not_called()

    def test_arguments_and_other_bot_mentions_never_touch_camera_or_files(self):
        for command in (
            "/foto ../../.env", "/foto C:\\secret.jpg", "/spegni now", "/stato@OtherBot",
            "/help@OtherBot", "/foto@HouseCameraBot arg", "/help\n/foto", "https://x",
            "/foto@" , "/foto extra", "/spegni;foto", "x" * 101,
        ):
            self.bot.handle(update(command))
        self.assert_no_effects()

    def test_stale_future_missing_dates_and_stop_never_execute(self):
        for command in ("/spegni", "/accendi", "/foto", "/help"):
            for date in (999, 900, 1001, None, "1000", True):
                self.bot.handle(update(command, date=date))
        self.clock.return_value = 1031
        self.bot.handle(update("/spegni", date=1000))
        self.stop.set()
        self.bot.handle(update("/spegni", date=1031))
        self.assert_no_effects()

    def test_rechecks_age_after_camera_lock_and_after_authentication(self):
        class SlowLock:
            def __enter__(inner):
                self.clock.return_value = 1031

            def __exit__(inner, *args):
                pass

        self.bot.camera_lock = SlowLock()
        self.bot.handle(update("/spegni"))
        self.assert_no_effects()
        self.bot.camera_lock = threading.Lock()
        self.clock.return_value = 1000
        self.reset_rate()
        self.factory.side_effect = lambda: (self.clock.configure_mock(return_value=1031), self.privacy)[1]
        self.bot.handle(update("/accendi"))
        self.privacy.set.assert_not_called()
        self.api.text.assert_not_called()

    def test_rate_limit_per_authorized_user_including_help(self):
        self.bot.handle(update("/help"))
        self.bot.handle(update("/spegni"))
        self.privacy.set.assert_not_called()
        self.bot.handle(update("/spegni", uid=987654321))
        self.privacy.set.assert_called_once_with(True)
        self.monotonic.return_value = 103
        self.bot.handle(update("/spegni"))
        self.assertEqual(self.privacy.set.call_count, 2)

    def test_camera_failures_send_only_fixed_message_to_authorized_requestor(self):
        self.privacy.set.side_effect = CameraError(f"unsafe {TOKEN}")
        with self.assertLogs("tapo_camera", level="WARNING") as logs:
            self.bot.handle(update("/spegni"))
        self.assertNotIn(TOKEN, str(logs.output))
        self.assertNotIn(TOKEN, self.api.text.call_args.args[1])
        self.assertEqual(self.api.text.call_args.args[0], UID)
        self.assertIn("incerto", self.api.text.call_args.args[1])
        self.api.text.side_effect = TelegramError("failure")
        self.reset_rate()
        with self.assertLogs("tapo_camera", level="WARNING"):
            self.bot.handle(update("/spegni"))

    def test_privacy_connection_failure_identifies_auth_stage_without_claiming_write(self):
        self.factory.side_effect = CameraError(f"unsafe {TOKEN}")
        for command in ("/help", "/stato", "/accendi"):
            self.reset_rate()
            with self.assertLogs("tapo_camera", level="WARNING") as logs:
                self.bot.handle(update(command))
            self.assertIn("connessione privacy", str(logs.output))
            reply = self.api.text.call_args.args[1]
            self.assertIn("camera/admin", reply)
            self.assertNotIn("incerto", reply)
            self.assertNotIn(TOKEN, reply)
            self.assertNotIn(TOKEN, str(logs.output))
        self.privacy.status.assert_not_called()
        self.privacy.set.assert_not_called()

    def test_read_failure_never_claims_privacy_might_have_changed(self):
        self.privacy.status.side_effect = CameraError(f"unsafe {TOKEN}")
        with self.assertLogs("tapo_camera", level="WARNING") as logs:
            self.bot.handle(update("/stato"))
        reply = self.api.text.call_args.args[1]
        self.assertIn("Lettura", reply)
        self.assertNotIn("incerto", reply)
        self.assertNotIn(TOKEN, reply)
        self.assertNotIn(TOKEN, str(logs.output))
        self.privacy.set.assert_not_called()

    def test_initialize_discards_backlog_validates_bot_and_refuses_webhook(self):
        self.api.call.side_effect = [
            {"is_bot": True, "username": "HouseCameraBot"}, {"url": ""},
        ]
        self.api.updates.return_value = [update("/spegni", update_id=500)]
        self.bot.initialize()
        self.assertEqual(self.bot.offset, 501)
        self.api.updates.assert_called_once_with(-1, timeout=0)
        self.privacy.set.assert_not_called()
        self.api.call.side_effect = [
            {"is_bot": True, "username": "HouseCameraBot"}, {"url": "https://existing"},
        ]
        with self.assertRaises(TelegramError):
            self.bot.initialize()
        self.api.call.side_effect = None
        self.api.call.return_value = {"is_bot": False, "username": "HouseCameraBot"}
        with self.assertRaises(TelegramError):
            self.bot.initialize()

    def test_poll_offsets_advance_on_ignored_updates_and_prevent_duplicates(self):
        batches = [
            [update("/spegni", update_id=10, uid=555), update("/help", update_id=11)],
            [update("/spegni", update_id=11)],
        ]

        def updates(offset):
            if batches:
                return batches.pop(0)
            self.stop.set()
            return []

        self.api.updates.side_effect = updates
        self.bot.poll()
        self.assertEqual([call.args[0] for call in self.api.updates.call_args_list], [0, 12, 12])
        self.api.deny.assert_called_once_with(555)
        self.api.text.assert_called_once_with(
            UID, HELP + "\n\nCamera raggiungibile. Privacy OFF: immagini abilitate.",
        )
        self.privacy.set.assert_not_called()

    def test_poll_backoff_is_interruptible_and_never_logs_raw_exception(self):
        self.api.updates.side_effect = TelegramError(TOKEN)
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        self.bot.stop = stop
        with self.assertLogs("tapo_camera", level="WARNING") as logs:
            self.bot.poll()
        stop.wait.assert_called_once_with(5)
        self.assertNotIn(TOKEN, str(logs.output))


class TelegramIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent / (".telegram-data-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.settings = Settings("192.0.2.1", "dummy", "dummy", output=self.folder)

    def tearDown(self):
        # Windows scanners can briefly hold a just-created JPEG directory.
        for attempt in range(10):
            try:
                shutil.rmtree(self.folder)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)

    def test_motion_delivery_whitelist_cooldown_durable_pairs_and_queue_bounds(self):
        stop = threading.Event()
        api = Mock()
        api.settings = TelegramSettings(TOKEN, USERS)
        delivery = MotionDelivery(api, stop)
        clock = Mock(return_value=10)
        snapshot = Mock(return_value=JPEG)
        recorder = Recorder(self.settings, snapshot, delivery, clock)
        motion = Motion("topic", None, "now")
        first = recorder.record(motion)
        self.assertIsNone(recorder.record(motion))
        self.assertEqual(delivery.pending.qsize(), 1)
        self.assertTrue(first.metadata.exists())
        self.assertEqual(first.image.read_bytes(), JPEG)
        api.photo.side_effect = lambda uid, saved: stop.set() if uid == max(USERS) else None
        delivery.run()
        self.assertEqual([call.args[0] for call in api.photo.call_args_list], sorted(USERS))
        api.close.assert_called_once()
        stop.clear()
        for _ in range(4):
            delivery(first)
        with self.assertLogs("tapo_camera", level="WARNING"):
            delivery(first)
        self.assertEqual(delivery.pending.qsize(), 4)
        stop.set()
        delivery(first)
        self.assertEqual(delivery.pending.qsize(), 4)

    def test_one_failed_recipient_does_not_prevent_other_recipient(self):
        stop = threading.Event()
        api = Mock()
        api.settings = TelegramSettings(TOKEN, USERS)
        delivery = MotionDelivery(api, stop)
        capture = Recorder(self.settings, lambda: JPEG).record()
        delivery(capture)

        def send(uid, capture):
            if uid == min(USERS):
                raise TelegramError("safe")
            stop.set()

        api.photo.side_effect = send
        with self.assertLogs("tapo_camera", level="WARNING"):
            delivery.run()
        self.assertEqual(api.photo.call_count, 2)
        self.assertTrue(capture.metadata.exists())

    def test_motion_video_delivery_only_uses_whitelisted_video_recipients(self):
        stop = threading.Event()
        api = Mock()
        api.settings = TelegramSettings(TOKEN, USERS)
        delivery = MotionDelivery(api, stop)
        recorder = Recorder(
            self.settings, lambda: JPEG, delivery,
            motion_video=lambda: b"\x00\x00\x00\x20ftypisom",
        )
        capture = recorder.record(Motion("topic", None, "received"))
        api.video.side_effect = lambda uid, saved: stop.set() if uid == max(USERS) else None
        delivery.run()
        self.assertEqual([call.args[0] for call in api.video.call_args_list], sorted(USERS))
        api.photo.assert_not_called()
        self.assertTrue(capture.metadata.is_file())
        self.assertEqual(capture.image.suffix, ".mp4")

    def test_real_runner_motion_and_command_threads_cleanup_no_manual_broadcast(self):
        command_api = Mock()
        delivery_api = Mock()
        command_api.settings = delivery_api.settings = TelegramSettings(TOKEN, USERS)
        command_api.call.side_effect = [
            {"is_bot": True, "username": "HouseCameraBot"}, {"url": ""},
        ]
        motion_sent = threading.Event()
        source = Mock()
        motion = Motion("topic", None, "now")
        pulls = [True]

        def pull():
            if pulls:
                pulls.pop()
                return [motion]
            motion_sent.wait(0.01)
            return []

        source.pull.side_effect = pull
        delivery_api.photo.side_effect = lambda uid, capture: motion_sent.set()
        polling = [True]

        def updates(offset, timeout=20):
            if offset == -1:
                return [update("/spegni", update_id=5)]
            if polling:
                self.assertEqual(offset, 6)
                polling.pop()
                self.assertTrue(motion_sent.wait(3))
                return [update("/foto", date=int(time.time()), update_id=6)]
            self.assertEqual(offset, 7)
            raise KeyboardInterrupt

        command_api.updates.side_effect = updates
        recorder = Recorder(self.settings, lambda: JPEG, Mock())
        privacy = Mock()
        privacy.status.return_value = False
        factory = Mock(return_value=privacy)
        with patch("tapo_camera.telegram.TelegramAPI", side_effect=[command_api, delivery_api]):
            with self.assertRaises(KeyboardInterrupt):
                run_telegram(TelegramSettings(TOKEN, USERS), recorder, lambda: source, factory)
        source.close.assert_called_once()
        command_api.close.assert_called_once()
        delivery_api.close.assert_called_once()
        factory.assert_called_once_with()
        privacy.status.assert_called_once_with()
        privacy.set.assert_not_called()
        privacy.close.assert_called_once_with()
        command_api.photo.assert_called_once()
        self.assertEqual(command_api.photo.call_args.args[0], UID)
        self.assertEqual(len(list(self.folder.rglob("*.jpg"))), 2)
        self.assertEqual(len(list(self.folder.rglob("*.json"))), 2)
        self.assertTrue(all(call.args[1] != command_api.photo.call_args.args[1]
                            for call in delivery_api.photo.call_args_list))
        self.assertFalse(any(t.name in ("tapo-motion", "tapo-telegram-send")
                             for t in threading.enumerate()))

    def test_runner_initialization_failure_closes_both_sessions_without_camera(self):
        first, second = Mock(), Mock()
        first.call.side_effect = TelegramError("safe")
        connect = Mock()
        factory = Mock()
        with patch("tapo_camera.telegram.TelegramAPI", side_effect=[first, second]):
            with self.assertRaises(TelegramError):
                run_telegram(TelegramSettings(TOKEN, USERS),
                             Recorder(self.settings, lambda: JPEG), connect, factory)
        first.close.assert_called_once()
        second.close.assert_called_once()
        connect.assert_not_called()
        factory.assert_not_called()

    def test_motion_failure_stops_runner_without_logging_sensitive_exception(self):
        first, second = Mock(), Mock()
        first.call.side_effect = [
            {"is_bot": True, "username": "HouseCameraBot"}, {"url": ""},
        ]
        first.updates.return_value = []
        connect = Mock(side_effect=RuntimeError(TOKEN))
        with patch("tapo_camera.telegram.TelegramAPI", side_effect=[first, second]), self.assertLogs(
            "tapo_camera", level="ERROR"
        ) as logs:
            with self.assertRaises(CameraError):
                run_telegram(TelegramSettings(TOKEN, USERS),
                             Recorder(self.settings, lambda: JPEG), connect, Mock())
        self.assertNotIn(TOKEN, str(logs.output))
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_motion_capture_and_privacy_commands_never_overlap(self):
        first, second = Mock(), Mock()
        first.settings = second.settings = TelegramSettings(TOKEN, USERS)
        first.call.side_effect = [
            {"is_bot": True, "username": "HouseCameraBot"}, {"url": ""},
        ]
        capture_started = threading.Event()
        release_capture = threading.Event()
        command_lock_wait = threading.Event()
        source = Mock()
        source.pull.return_value = [Motion("topic", None, "now")]
        privacy = Mock()
        privacy.set.side_effect = lambda value: value
        real_lock = threading.Lock()

        class ObservedLock:
            def __enter__(inner):
                if threading.current_thread().name != "tapo-motion":
                    command_lock_wait.set()
                real_lock.acquire()

            def __exit__(inner, *args):
                real_lock.release()

        def snapshot():
            capture_started.set()
            self.assertTrue(release_capture.wait(3))
            return JPEG

        calls = [True]

        def updates(offset, timeout=20):
            if offset == -1:
                return []
            if calls:
                calls.pop()
                self.assertTrue(capture_started.wait(3))
                return [update("/spegni", date=int(time.time()))]
            raise KeyboardInterrupt

        first.updates.side_effect = updates

        def release():
            if command_lock_wait.wait(3):
                # The command reached the shared lock while RTSP is still active.
                privacy.set.assert_not_called()
            release_capture.set()

        helper = threading.Thread(target=release)
        helper.start()
        try:
            with patch("tapo_camera.telegram.TelegramAPI", side_effect=[first, second]), patch(
                "tapo_camera.telegram.threading", SimpleNamespace(
                    Event=threading.Event, Lock=ObservedLock, Thread=threading.Thread,
                )
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_telegram(TelegramSettings(TOKEN, USERS),
                                 Recorder(self.settings, snapshot), lambda: source,
                                 lambda: privacy)
        finally:
            release_capture.set()
            helper.join(4)
        privacy.set.assert_called_once_with(True)
        privacy.close.assert_called_once()
        source.close.assert_called_once()

    def test_monitor_stop_unsubscribes_and_interrupts_reconnect_wait(self):
        stop = threading.Event()
        source = Mock()
        recorder = Mock()

        def pull():
            stop.set()
            return [Motion("topic", None, "now")]

        source.pull.side_effect = pull
        monitor(lambda: source, recorder, stop=stop)
        source.close.assert_called_once()
        recorder.record.assert_not_called()
        stop.clear()

        def failing():
            stop.set()
            raise CameraError("safe")

        with self.assertLogs("tapo_camera", level="ERROR"):
            monitor(failing, recorder, stop=stop)

    def test_cli_wires_telegram_with_explicit_admin_without_camera_contact(self):
        env = {
            "TAPO_CAMERA_USERNAME": "local", "TAPO_CAMERA_PASSWORD": "local-secret",
            "TAPO_CAMERA_AUTH": "admin", "TAPO_CAMERA_ADMIN_PASSWORD": "admin-secret",
            "TAPO_TELEGRAM_BOT_TOKEN": TOKEN,
            "TAPO_TELEGRAM_USER_IDS": str(UID),
        }
        with patch("tapo_camera.cli.load_dotenv"), patch.dict(os.environ, env, clear=True), patch(
            "tapo_camera.telegram.run_telegram"
        ) as run, patch("tapo_camera.cli.PrivacyController") as privacy, patch(
            "tapo_camera.cli.capture_frame"
        ) as capture, patch("tapo_camera.cli.monitor") as watch:
            result = main(["telegram", "--output", str(self.folder)])
            self.assertEqual(result, 0)
            args = run.call_args.args
            self.assertEqual(args[0].user_ids, frozenset({UID}))
            self.assertEqual(args[1].settings.username, "local")
            self.assertIsNone(args[1].motion_video)
            args[3]()
            self.assertEqual(privacy.call_args.args[0].username, "admin")
            self.assertEqual(privacy.call_args.args[0].password, "admin-secret")
            capture.assert_not_called()
            watch.assert_not_called()

    def test_cli_motion_media_environment_and_explicit_override(self):
        for value, override, video in (
            ("photo", None, False), ("video", None, True), ("", None, False),
            ("video", "photo", False), ("photo", "video", True),
            ("invalid", "photo", False),
        ):
            with self.subTest(value=value, override=override), patch(
                "tapo_camera.cli.load_dotenv"
            ), patch.dict(os.environ, {
                "TAPO_CAMERA_USERNAME": "local", "TAPO_CAMERA_PASSWORD": "dummy",
                "TAPO_TELEGRAM_BOT_TOKEN": TOKEN, "TAPO_TELEGRAM_USER_IDS": str(UID),
                "TAPO_CAMERA_MOTION_MEDIA": value,
            }, clear=True), patch("tapo_camera.telegram.run_telegram") as run:
                argv = ["telegram", "--output", str(self.folder)]
                if override is not None:
                    argv.extend(["--motion-media", override])
                self.assertEqual(main(argv), 0)
                self.assertEqual(run.call_args.args[1].motion_video is not None, video)

    def test_cli_motion_media_is_loaded_from_dotenv(self):
        (self.folder / ".env").write_text("TAPO_CAMERA_MOTION_MEDIA=video\n")
        with patch("tapo_camera.cli.PROJECT_ROOT", self.folder), patch.dict(os.environ, {
            "TAPO_CAMERA_USERNAME": "local", "TAPO_CAMERA_PASSWORD": "dummy",
            "TAPO_TELEGRAM_BOT_TOKEN": TOKEN, "TAPO_TELEGRAM_USER_IDS": str(UID),
        }, clear=True), patch("tapo_camera.telegram.run_telegram") as run:
            self.assertEqual(main(["telegram", "--output", str(self.folder)]), 0)
            self.assertIsNotNone(run.call_args.args[1].motion_video)

    def test_cli_invalid_motion_media_fails_before_network(self):
        with patch("tapo_camera.cli.load_dotenv"), patch.dict(os.environ, {
            "TAPO_CAMERA_USERNAME": "local", "TAPO_CAMERA_PASSWORD": "dummy",
            "TAPO_CAMERA_MOTION_MEDIA": "gif",
        }, clear=True), patch("tapo_camera.telegram.run_telegram") as run, patch(
            "tapo_camera.cli.PrivacyController"
        ) as privacy, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["telegram", "--output", str(self.folder)]), 2)
        self.assertIn("TAPO_CAMERA_MOTION_MEDIA", stderr.getvalue())
        run.assert_not_called()
        privacy.assert_not_called()

    def test_cli_invalid_telegram_config_fails_before_network(self):
        for extra in ({}, {"TAPO_TELEGRAM_BOT_TOKEN": TOKEN},
                      {"TAPO_TELEGRAM_BOT_TOKEN": TOKEN, "TAPO_TELEGRAM_USER_IDS": "@wife"}):
            with patch("tapo_camera.cli.load_dotenv"), patch.dict(os.environ, {
                "TAPO_CAMERA_USERNAME": "local", "TAPO_CAMERA_PASSWORD": "secret", **extra,
            }, clear=True), patch("tapo_camera.telegram.TelegramAPI") as api, patch(
                "tapo_camera.cli.PrivacyController"
            ) as privacy, redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["telegram", "--output", str(self.folder)]), 2)
            api.assert_not_called()
            privacy.assert_not_called()
            self.assertNotIn(TOKEN, stderr.getvalue())

    def test_watch_and_snapshot_never_construct_telegram_even_when_configured(self):
        for command in ("watch", "snapshot"):
            with patch("tapo_camera.cli.load_dotenv"), patch.dict(os.environ, {
                "TAPO_CAMERA_USERNAME": "local", "TAPO_CAMERA_PASSWORD": "secret",
                "TAPO_TELEGRAM_BOT_TOKEN": TOKEN, "TAPO_TELEGRAM_USER_IDS": str(UID),
            }, clear=True), patch("tapo_camera.telegram.TelegramAPI") as api, patch(
                "tapo_camera.cli.capture_frame", return_value=JPEG,
            ), patch("tapo_camera.cli.monitor"):
                self.assertEqual(main([command, "--output", str(self.folder)]), 0)
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
