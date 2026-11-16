from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

from .monitor import CameraError, Capture, Recorder, monitor, on_capture
from .privacy import PrivacyController

LOGGER = logging.getLogger("tapo_camera")
HELP = (
    "Scegli un pulsante nel menu oppure usa: "
    "/foto (scatto con ripristino privacy), "
    "/accendi (privacy OFF), /spegni (privacy ON), "
    "/help (aiuto e stato privacy) o /menu. "
    "Accendi/spegni non controllano l'alimentazione."
)
MENU = {
    "keyboard": [
        [{"text": "Foto"}, {"text": "Help"}],
        [{"text": "Accendi"}, {"text": "Spegni"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "one_time_keyboard": False,
    "input_field_placeholder": "Scegli un comando",
}
MAX_COMMAND_AGE = 30
COMMAND_INTERVAL = 3
ACCESS_DENIED = "Non sei nella whitelist."


class TelegramError(CameraError):
    """Only fixed, non-sensitive diagnostics may cross the API boundary."""


@dataclass(frozen=True)
class TelegramSettings:
    token: str = field(repr=False)
    user_ids: frozenset[int]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", self.token):
            raise TelegramError("Configurare TAPO_TELEGRAM_BOT_TOKEN valido.")
        if (
            not isinstance(self.user_ids, frozenset)
            or not self.user_ids
            or any(type(uid) is not int or not 0 < uid < 2**52 for uid in self.user_ids)
        ):
            raise TelegramError("Configurare una whitelist di ID utente Telegram positivi.")

    @classmethod
    def from_env(cls) -> TelegramSettings:
        value = os.getenv("TAPO_TELEGRAM_USER_IDS", "")
        parts = value.split(",")
        if any(not re.fullmatch(r"[1-9][0-9]*", part.strip()) for part in parts):
            raise TelegramError("TAPO_TELEGRAM_USER_IDS richiede ID numerici separati da virgole.")
        return cls(
            os.getenv("TAPO_TELEGRAM_BOT_TOKEN", "").strip(),
            frozenset(int(part.strip()) for part in parts),
        )


class TelegramAPI:
    def __init__(self, settings: TelegramSettings):
        self.settings = settings
        self.session = requests.Session()
        # Do not leak credentials through an environment-provided HTTP proxy.
        self.session.trust_env = False

    def call(self, method: str, data: dict | None = None, files: dict | None = None):
        if method not in ("getMe", "getWebhookInfo", "getUpdates", "sendMessage", "sendPhoto", "sendVideo"):
            raise TelegramError("Metodo Telegram non consentito.")
        if method in ("sendMessage", "sendPhoto", "sendVideo"):
            uid = (data or {}).get("chat_id")
            denial = (
                method == "sendMessage" and files is None
                and type(uid) is int and 0 < uid < 2**52
                and data == {"chat_id": uid, "text": ACCESS_DENIED}
            )
            if not denial and (type(uid) is not int or uid not in self.settings.user_ids):
                raise TelegramError("Destinatario Telegram non consentito.")
        try:
            response = self.session.post(
                f"https://api.telegram.org/bot{self.settings.token}/{method}",
                data=data or {}, files=files, timeout=(5, 25),
                allow_redirects=False,
            )
            try:
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("ok") is not True:
                    raise TelegramError("Richiesta Telegram non riuscita.")
                return body["result"]
            finally:
                response.close()
        except (requests.RequestException, ValueError, KeyError):
            # Requests exceptions and Telegram descriptions can include the token URL.
            raise TelegramError("Richiesta Telegram non riuscita; verificare rete e configurazione.") from None

    def updates(self, offset: int, timeout: int = 20) -> list[dict]:
        result = self.call("getUpdates", {
            "offset": offset, "timeout": timeout, "limit": 100,
            "allowed_updates": '["message"]',
        })
        if not isinstance(result, list) or any(
            not isinstance(item, dict) or type(item.get("update_id")) is not int
            or item["update_id"] < 0 for item in result
        ):
            raise TelegramError("Risposta aggiornamenti Telegram non valida.")
        return result

    def text(self, user_id: int, text: str) -> None:
        if type(user_id) is not int or user_id not in self.settings.user_ids:
            return
        self.call("sendMessage", {
            "chat_id": user_id, "text": text,
            "reply_markup": json.dumps(MENU),
        })

    def deny(self, user_id: int) -> None:
        self.call("sendMessage", {"chat_id": user_id, "text": ACCESS_DENIED})

    def photo(self, user_id: int, capture: Capture) -> None:
        if type(user_id) is not int or user_id not in self.settings.user_ids:
            return
        # This path comes exclusively from Recorder, never from chat arguments.
        with capture.image.open("rb") as image:
            self.call(
                "sendPhoto", {
                    "chat_id": user_id, "caption": capture.message,
                    "reply_markup": json.dumps(MENU),
                },
                {"photo": ("snapshot.jpg", image, "image/jpeg")},
            )

    def close(self) -> None:
        self.session.close()

    def video(self, user_id: int, capture: Capture) -> None:
        if type(user_id) is not int or user_id not in self.settings.user_ids:
            return
        with capture.image.open("rb") as video:
            self.call(
                "sendVideo", {
                    "chat_id": user_id, "caption": capture.message,
                    "supports_streaming": "true",
                    "reply_markup": json.dumps(MENU),
                },
                {"video": ("motion.mp4", video, "video/mp4")},
            )


class TelegramBot:
    def __init__(
        self,
        settings: TelegramSettings,
        api: TelegramAPI,
        recorder: Recorder,
        privacy_factory: Callable[[], PrivacyController],
        stop: threading.Event,
        camera_lock: threading.Lock,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.api = api
        self.recorder = recorder
        self.privacy_factory = privacy_factory
        self.privacy: PrivacyController | None = None
        self.stop = stop
        self.camera_lock = camera_lock
        self.clock = clock
        self.monotonic = monotonic
        self.started_at = int(clock())
        self.last_command: dict[int, float] = {}
        self.last_denial = float("-inf")
        self.username = ""
        self.offset = 0

    def initialize(self) -> None:
        me = self.api.call("getMe")
        if (
            not isinstance(me, dict) or me.get("is_bot") is not True
            or not isinstance(me.get("username"), str)
            or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", me["username"])
        ):
            raise TelegramError("Identita' bot Telegram non valida.")
        webhook = self.api.call("getWebhookInfo")
        if not isinstance(webhook, dict) or webhook.get("url") != "":
            raise TelegramError("Webhook attivo: rimuoverlo prima di usare il polling.")
        self.username = me["username"]
        # Negative offset forgets earlier updates. Discard even the last one;
        # the first positive-offset poll confirms it without ever executing it.
        backlog = self.api.updates(-1, timeout=0)
        self.offset = max((item["update_id"] + 1 for item in backlog), default=0)

    def private_message(self, update: dict) -> dict | None:
        if any(key in update for key in (
            "edited_message", "channel_post", "edited_channel_post", "business_message",
        )):
            return None
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        sender, chat = message.get("from"), message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return None
        uid = sender.get("id")
        if (
            type(uid) is not int or not 0 < uid < 2**52
            or sender.get("is_bot") is not False or chat.get("type") != "private"
            or type(chat.get("id")) is not int or chat["id"] != uid
            or any(key in message for key in (
                "sender_chat", "via_bot", "forward_origin", "forward_date", "edit_date",
            ))
        ):
            return None
        return message

    def authorized_message(self, update: dict) -> dict | None:
        message = self.private_message(update)
        if message is None or message["from"]["id"] not in self.settings.user_ids:
            return None
        return message

    def fresh(self, message: dict) -> bool:
        date = message.get("date")
        now = self.clock()
        return (
            type(date) is int and date >= self.started_at
            and 0 <= now - date <= MAX_COMMAND_AGE
        )

    def handle(self, update: dict) -> None:
        message = self.private_message(update)
        if message is None or not self.fresh(message) or self.stop.is_set():
            return
        text = message.get("text")
        if not isinstance(text, str) or len(text) > 100:
            return
        if message["from"]["id"] not in self.settings.user_ids:
            now = self.monotonic()
            if now - self.last_denial >= COMMAND_INTERVAL:
                self.last_denial = now
                try:
                    self.api.deny(message["from"]["id"])
                except TelegramError:
                    LOGGER.warning("Avviso accesso negato Telegram non consegnato.")
            return
        match = re.fullmatch(
            r"/?([A-Za-z]+)(?:@([A-Za-z0-9_]+))?", text.strip(),
        )
        if match is None:
            return
        command, mention = match.groups()
        if mention is not None and mention.lower() != self.username.lower():
            return
        uid = message["from"]["id"]
        now = self.monotonic()
        if now - self.last_command.get(uid, float("-inf")) < COMMAND_INTERVAL:
            return
        self.last_command[uid] = now
        stage = "aiuto"
        privacy_write_started = False
        privacy_restored = False
        try:
            if command.lower() not in ("help", "stato", "foto", "accendi", "spegni"):
                self.api.text(uid, HELP)
                return
            with self.camera_lock:
                # Motion acquisition may have held the lock for a full RTSP timeout.
                if not self.fresh(message) or self.stop.is_set():
                    return
                if self.privacy is None:
                    stage = "connessione privacy"
                    self.privacy = self.privacy_factory()
                # Never execute an expired request after slow authentication.
                if not self.fresh(message) or self.stop.is_set():
                    return
                if command.lower() == "foto":
                    stage = "lettura privacy"
                    enabled = self.privacy.status()
                    if not self.fresh(message) or self.stop.is_set():
                        return
                    if enabled:
                        privacy_write_started = True
                        try:
                            stage = "disattivazione privacy"
                            self.privacy.set(False)
                            if not self.fresh(message) or self.stop.is_set():
                                return
                            stage = "acquisizione foto"
                            capture = self.recorder.record()
                        finally:
                            # A failed OFF command may still have changed the camera.
                            previous_stage = stage
                            stage = "ripristino privacy"
                            self.privacy.set(True)
                            privacy_write_started = False
                            privacy_restored = True
                            stage = previous_stage
                    else:
                        stage = "acquisizione foto"
                        capture = self.recorder.record()
                    reply = None
                else:
                    if command.lower() in ("help", "stato"):
                        stage = "lettura privacy"
                        enabled = self.privacy.status()
                    else:
                        stage = "modifica privacy"
                        privacy_write_started = True
                        enabled = self.privacy.set(command.lower() == "spegni")
                    reply = (
                        "Camera raggiungibile. Privacy ON: immagini bloccate."
                        if enabled else "Camera raggiungibile. Privacy OFF: immagini abilitate."
                    )
                    if command.lower() in ("help", "stato"):
                        reply = HELP + "\n\n" + reply
                    capture = None
            stage = "invio risposta"
            if capture is not None:
                if capture.media_type == "video":
                    self.api.video(uid, capture)
                else:
                    self.api.photo(uid, capture)
            elif reply is not None:
                self.api.text(uid, reply)
        except (CameraError, OSError, RuntimeError):
            LOGGER.warning("Comando Telegram non completato (%s).", stage)
            errors = {
                "aiuto": "Invio del menu Telegram non riuscito.",
                "connessione privacy": (
                    "Connessione al controllo privacy pytapo non riuscita. "
                    "Verificare account camera/admin, porta HTTPS e firmware. "
                    "Provare privacy-status dal Raspberry; se l'autenticazione "
                    "e' sospesa, attendere prima di riprovare."
                ),
                "lettura privacy": "Lettura dello stato privacy pytapo non riuscita.",
                "modifica privacy": "Modifica o verifica della privacy non riuscita.",
                "disattivazione privacy": "Disattivazione temporanea della privacy non riuscita; nessuna foto acquisita.",
                "ripristino privacy": "Ripristino della privacy ON non riuscito; foto non inviata.",
                "acquisizione foto": "Acquisizione foto non riuscita; verificare RTSP, privacy e spazio disco.",
                "invio risposta": "Operazione camera completata, ma invio della risposta Telegram non riuscito.",
            }
            reply = errors[stage]
            if privacy_restored:
                reply += " Privacy ripristinata ON."
            if privacy_write_started:
                # Only a command that reached set() may have changed privacy.
                reply += (
                    " Stato eventualmente incerto: verificare /help o "
                    "l'app Tapo prima di riprovare."
                )
            if command.lower() in ("help", "stato"):
                reply = HELP + "\n\n" + reply
            try:
                self.api.text(uid, reply)
            except TelegramError:
                LOGGER.warning("Risposta Telegram non consegnata.")

    def poll(self) -> None:
        while not self.stop.is_set():
            try:
                updates = self.api.updates(self.offset)
                for update in updates:
                    if self.stop.is_set():
                        break
                    if update["update_id"] < self.offset:
                        continue
                    self.offset = update["update_id"] + 1
                    self.handle(update)
            except TelegramError:
                LOGGER.warning("Polling Telegram non riuscito; nuovo tentativo tra 5 secondi.")
                self.stop.wait(5)

    def close(self) -> None:
        if self.privacy is not None:
            self.privacy.close()


class MotionDelivery:
    """Bounded, best-effort live delivery: never replay old JSON files."""

    def __init__(self, api: TelegramAPI, stop: threading.Event):
        self.api = api
        self.stop = stop
        self.pending: queue.Queue[Capture] = queue.Queue(maxsize=4)

    def __call__(self, capture: Capture) -> None:
        on_capture(capture)
        if self.stop.is_set():
            return
        try:
            self.pending.put_nowait(capture)
        except queue.Full:
            LOGGER.warning("Coda Telegram piena; foto conservata solo sul disco.")

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    capture = self.pending.get(timeout=0.25)
                except queue.Empty:
                    continue
                for uid in sorted(self.api.settings.user_ids):
                    if self.stop.is_set():
                        break
                    try:
                        if capture.media_type == "video":
                            self.api.video(uid, capture)
                        else:
                            self.api.photo(uid, capture)
                    except (TelegramError, OSError):
                        LOGGER.warning("Notifica Telegram non consegnata; foto conservata sul disco.")
        finally:
            self.api.close()


def run_telegram(
    settings: TelegramSettings,
    recorder: Recorder,
    connect: Callable,
    privacy_factory: Callable[[], PrivacyController],
) -> None:
    stop = threading.Event()
    camera_lock = threading.Lock()
    api = TelegramAPI(settings)
    delivery = MotionDelivery(TelegramAPI(settings), stop)
    bot = TelegramBot(settings, api, recorder, privacy_factory, stop, camera_lock)
    original_hook = recorder.hook
    threads: list[threading.Thread] = []
    monitor_failed = threading.Event()

    class MotionRecorder:
        def record(self, motion):
            with camera_lock:
                if not stop.is_set():
                    # Manual photos retain the original local hook and are sent
                    # only to the requesting user, not broadcast to the household.
                    recorder.hook = delivery
                    try:
                        return recorder.record(motion)
                    finally:
                        recorder.hook = original_hook

    def watch() -> None:
        try:
            monitor(connect, MotionRecorder(), stop=stop)
        except Exception:
            LOGGER.error("Monitor movimento interrotto; arresto del bot.")
            monitor_failed.set()
            stop.set()

    try:
        bot.initialize()
        threads = [
            threading.Thread(target=watch, name="tapo-motion", daemon=True),
            threading.Thread(target=delivery.run, name="tapo-telegram-send", daemon=True),
        ]
        for thread in threads:
            thread.start()
        LOGGER.info("Bot Telegram attivo: solo chat private in whitelist; backlog scartato.")
        bot.poll()
        if monitor_failed.is_set():
            raise CameraError("Monitor movimento interrotto; riavviare dopo aver verificato la camera.")
    finally:
        stop.set()
        # ONVIF connect/pull/unsubscribe and FFmpeg already have finite timeouts.
        # Do not wait indefinitely for a failed third-party camera stack.
        deadline = time.monotonic() + recorder.settings.capture_timeout + 65
        for thread in threads:
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    LOGGER.warning("Worker non terminato entro il limite di arresto.")
        bot.close()
        api.close()
        if not threads or threads[-1].ident is None:
            delivery.api.close()
