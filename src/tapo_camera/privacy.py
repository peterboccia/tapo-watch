from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

import requests

from .monitor import CameraError


@dataclass(frozen=True)
class PrivacySettings:
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    control_port: int = 443


class PrivacyController:
    """Proprietary Tapo lens-mask control; never changes state on connection."""

    def __init__(self, settings: PrivacySettings):
        from pytapo import Tapo

        try:
            self.client = Tapo(
                settings.host, settings.username, settings.password,
                controlPort=settings.control_port,
                printDebugInformation=False, printWarnInformation=False,
                redactConfidentialInformation=True,
                retryStok=False,
            )
        except Exception as error:
            if str(error).startswith("Temporary Suspension:"):
                raise CameraError(
                    "Autenticazione temporaneamente sospesa dalla camera; attendere "
                    "lo sblocco e verificare le credenziali prima di riprovare."
                ) from None
            if isinstance(error, requests.exceptions.SSLError):
                raise CameraError(
                    "[TLS] Connessione HTTPS pytapo fallita; verificare firmware "
                    "e compatibilita' TLS. Nessuna modifica privacy eseguita."
                ) from None
            if isinstance(error, (requests.Timeout, TimeoutError)):
                raise CameraError(
                    "[TIMEOUT] La camera non risponde al controllo pytapo; "
                    "verificare IP, porta HTTPS e collegamento LAN."
                ) from None
            if isinstance(error, requests.ConnectionError):
                raise CameraError(
                    "[RETE] Connessione HTTPS pytapo non disponibile; verificare "
                    "IP, porta e accesso LAN dal container."
                ) from None
            if isinstance(error, PermissionError):
                raise CameraError(
                    "[PERMESSI] Accesso locale negato durante l'avvio di pytapo; "
                    "verificare i permessi del container, non le password."
                ) from None
            # Compare only known library diagnostics; never echo raw payloads.
            if str(error) in ("Invalid authentication data", "Invalid login credentials"):
                raise CameraError(
                    "[AUTH] La camera ha rifiutato l'autenticazione pytapo. "
                    "Verificare la password e la modalita' camera/admin. In "
                    "modalita' admin usare la password cloud del proprietario."
                ) from None
            if str(error) in (
                "Failure detecting hashing algorithm.",
                "Failed to detect device type.",
            ):
                raise CameraError(
                    "[PROTOCOLLO] Risposta o protocollo camera non riconosciuto "
                    "da pytapo; verificare versione firmware e libreria."
                ) from None
            http_status = re.fullmatch(
                r"Error communicating with Tapo Camera\. Status code: ([1-5][0-9]{2})",
                str(error),
            )
            if http_status is not None:
                raise CameraError(
                    f"[HTTP {http_status[1]}] Il controllo camera ha rifiutato "
                    "la richiesta HTTPS; verificare autenticazione e firmware."
                ) from None
            raise CameraError(
                "[NON_CLASSIFICATO] Connessione pytapo fallita; verificare account, "
                "autenticazione camera/admin, porta HTTPS e compatibilita' firmware. "
                "La causa non e' stata riconosciuta; non ripetere tentativi alla cieca."
            ) from None

    def status(self) -> bool:
        try:
            response = self.client.getPrivacyMode()
        except Exception:
            raise CameraError("Lettura privacy pytapo fallita.") from None
        if not isinstance(response, dict) or response.get("enabled") not in ("on", "off"):
            raise CameraError("Stato privacy pytapo assente o non riconosciuto.")
        return response["enabled"] == "on"

    def set(self, enabled: bool) -> bool:
        if type(enabled) is not bool:
            raise ValueError("Privacy requires a boolean.")
        try:
            self.client.setPrivacyMode(enabled)
        except Exception:
            raise CameraError(
                "Comando privacy pytapo fallito; stato incerto, controllare privacy-status/app."
            ) from None
        try:
            actual = self.status()
        except CameraError:
            raise CameraError(
                "Comando privacy inviato ma verifica fallita; stato incerto, "
                "controllare privacy-status/app."
            ) from None
        if actual != enabled:
            raise CameraError(
                "Verifica privacy fallita: la camera non riporta lo stato richiesto; "
                "controllare privacy-status/app."
            )
        return actual

    def close(self) -> None:
        try:
            asyncio.run(self.client.transport.close())
        except Exception:
            logging.getLogger("tapo_camera").warning("Chiusura sessione pytapo fallita.")
