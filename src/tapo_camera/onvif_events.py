from __future__ import annotations

import logging
import time
from datetime import timedelta

from lxml import etree
from onvif import ONVIFError, ONVIFService
from requests import Session
from zeep.exceptions import Error as ZeepError
from zeep.transports import Transport

from .monitor import CameraError, Motion, Settings, utc_now

LOGGER = logging.getLogger("tapo_camera")
EVENT_NS = "http://www.onvif.org/ver10/events/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
WSNT_NS = "http://docs.oasis-open.org/wsn/b-2"
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
MOTION_TOPICS = (
    "RuleEngine/CellMotionDetector/Motion",
    "VideoSource/MotionAlarm",
    "RuleEngine/MotionRegionDetector/Motion",
)


def parse_motion(notification: etree._Element) -> Motion | None:
    topic_value = notification.findtext(f"{{{WSNT_NS}}}Topic") or ""
    # Namespace prefixes are chosen by the camera, not fixed to "tns1".
    topic = "/".join(part.split(":")[-1] for part in topic_value.strip().split("/"))
    if topic not in MOTION_TOPICS:
        return None
    message = notification.find(f"{{{WSNT_NS}}}Message/{{{SCHEMA_NS}}}Message")
    if message is None:
        LOGGER.warning("Evento movimento senza payload XML riconosciuto; ignorato.")
        return None
    if message.get("PropertyOperation") == "Deleted":
        return None
    data = message.find(f"{{{SCHEMA_NS}}}Data")
    if data is None:
        LOGGER.warning("Evento movimento senza Data; ignorato.")
        return None
    for item in data.findall(f"{{{SCHEMA_NS}}}SimpleItem"):
        if item.get("Name") in ("IsMotion", "State", "LogicalState"):
            value = (item.get("Value") or "").strip().lower()
            if value in ("true", "1"):
                return Motion(topic, message.get("UtcTime"), utc_now())
            if value not in ("false", "0"):
                LOGGER.warning("Stato movimento ONVIF non riconosciuto; ignorato.")
    return None


class OnvifEvents:
    def __init__(self, settings: Settings):
        from pathlib import Path

        import onvif

        self.session = Session()
        self.session.trust_env = False
        self.manager = None
        self.headers: list[etree._Element] = []
        transport = Transport(session=self.session, timeout=10, operation_timeout=15)
        wsdl_dir = Path(onvif.__file__).resolve().parent.parent / "wsdl"

        def service(address: str, wsdl: str, binding: str) -> ONVIFService:
            return ONVIFService(
                address, settings.username, settings.password,
                str(wsdl_dir / wsdl), binding_name=binding,
                transport=transport,
            )

        try:
            device = service(
                f"http://{settings.host}:{settings.onvif_port}/onvif/device_service",
                "devicemgmt.wsdl",
                "{http://www.onvif.org/ver10/device/wsdl}DeviceBinding",
            )
            capabilities = device.GetCapabilities({"Category": "Events"})
            if capabilities.Events is None or not capabilities.Events.XAddr:
                raise CameraError("La camera non espone eventi ONVIF.")
            events = service(
                capabilities.Events.XAddr, "events.wsdl",
                f"{{{EVENT_NS}}}EventBinding",
            )
            subscription = events.CreatePullPointSubscription(
                {"InitialTerminationTime": "PT60S"}
            )
            address = subscription.SubscriptionReference.Address._value_1
            parameters = subscription.SubscriptionReference.ReferenceParameters
            if parameters is not None:
                self.headers = parameters._value_1 or []
            self.pull_service = service(
                address, "events.wsdl", f"{{{EVENT_NS}}}PullPointSubscriptionBinding"
            )
            self.pullpoint = self.pull_service.ws_client
            self.manager = service(
                address, "events.wsdl", f"{{{EVENT_NS}}}SubscriptionManagerBinding"
            ).ws_client
            self._set_renewal(subscription)
        except (ONVIFError, ZeepError, OSError, CameraError) as error:
            self.close()
            if isinstance(error, CameraError):
                raise
            raise CameraError(
                "Connessione ONVIF fallita; verificare account videocamera, "
                "IP, porta e orologio della camera."
            ) from None

    def _set_renewal(self, response: object) -> None:
        current = getattr(response, "CurrentTime", None)
        termination = getattr(response, "TerminationTime", None)
        if current is None or termination is None:
            raise CameraError("ONVIF non ha restituito la durata della sottoscrizione.")
        lifetime = (termination - current).total_seconds()
        if lifetime <= 0:
            raise CameraError("Sottoscrizione ONVIF gia' scaduta.")
        self.renew_at = time.monotonic() + min(30, lifetime / 2)

    def pull(self) -> list[Motion]:
        try:
            if time.monotonic() >= self.renew_at:
                response = self.manager.Renew(
                    TerminationTime="PT60S", _soapheaders=self.headers
                )
                self._set_renewal(response)
            # Zeep discards mixed text in TopicExpressionType. Parse the raw
            # PullMessages response instead of silently losing every topic.
            with self.pull_service.zeep_client.settings(raw_response=True):
                response = self.pullpoint.PullMessages(
                    Timeout=timedelta(seconds=5), MessageLimit=100,
                    _soapheaders=self.headers,
                )
            response.raise_for_status()
            envelope = etree.fromstring(
                response.content,
                parser=etree.XMLParser(resolve_entities=False, no_network=True),
            )
            body = envelope.find(f"{{{SOAP_NS}}}Body/{{{EVENT_NS}}}PullMessagesResponse")
            if body is None:
                raise CameraError("Risposta ONVIF inattesa o SOAP Fault.")
            return [
                motion for notification in body.findall(f"{{{WSNT_NS}}}NotificationMessage")
                if (motion := parse_motion(notification)) is not None
            ]
        except (ONVIFError, ZeepError, OSError, etree.XMLSyntaxError):
            raise CameraError("Lettura/rinnovo ONVIF fallito.") from None

    def close(self) -> None:
        try:
            if self.manager is not None:
                self.manager.Unsubscribe(_soapheaders=self.headers)
        except (ONVIFError, ZeepError, OSError):
            LOGGER.warning("Unsubscribe ONVIF fallito; la sottoscrizione scadra' sulla camera.")
        finally:
            self.session.close()
