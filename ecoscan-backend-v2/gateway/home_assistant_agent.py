"""Passerelle HTTP Django vers les entités actionneurs de Home Assistant."""

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


LOGGER = logging.getLogger("ecoscan.gateway")
IDENTIFIANT_ENTITE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
DOMAINES_ACTIONNABLES = {"fan", "input_boolean", "light", "switch"}
ETATS_CIBLES = {"ON": "on", "OFF": "off"}


class GatewayError(Exception):
    """Signale une erreur de configuration ou de transport de la passerelle."""


@dataclass(frozen=True)
class GatewayConfig:
    django_base_url: str
    django_internal_token: str
    home_assistant_url: str
    home_assistant_token: str
    equipment_entities: dict[str, str]
    outbox_path: Path
    poll_interval: float = 2.0
    state_timeout: float = 15.0
    request_timeout: float = 10.0

    @classmethod
    def from_environment(cls):
        django_base_url = _required_environment("DJANGO_BASE_URL").rstrip("/")
        internal_token = _required_environment("DJANGO_INTERNAL_TOKEN")
        home_assistant_url = _required_environment(
            "HOME_ASSISTANT_URL"
        ).rstrip("/")
        home_assistant_token = _required_environment("HOME_ASSISTANT_TOKEN")
        mapping_path = Path(
            os.environ.get(
                "HOME_ASSISTANT_ENTITY_MAP_FILE",
                "equipment-entities.json",
            )
        )
        try:
            equipment_entities = json.loads(mapping_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise GatewayError(
                f"Impossible de lire le mapping Home Assistant : {mapping_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise GatewayError(
                f"Le mapping Home Assistant n'est pas un JSON valide : {mapping_path}"
            ) from exc

        if not isinstance(equipment_entities, dict):
            raise GatewayError("Le mapping doit être un objet JSON équipement-entité.")
        mapping_normalise = {}
        entites_utilisees = set()
        for equipment_id, entity_id in equipment_entities.items():
            if not isinstance(equipment_id, str) or not isinstance(entity_id, str):
                raise GatewayError("Les clés et valeurs du mapping doivent être du texte.")
            try:
                UUID(equipment_id)
            except ValueError as exc:
                raise GatewayError(
                    f"Identifiant d'équipement EcoScan invalide : {equipment_id}"
                ) from exc
            if not IDENTIFIANT_ENTITE_RE.fullmatch(entity_id):
                raise GatewayError(
                    f"Identifiant d'entité Home Assistant invalide : {entity_id}"
                )
            if entity_id.split(".", 1)[0] not in DOMAINES_ACTIONNABLES:
                raise GatewayError(
                    f"Le domaine Home Assistant n'est pas pilotable en ON/OFF : {entity_id}"
                )
            if entity_id in entites_utilisees:
                raise GatewayError(
                    f"L'entité Home Assistant est associée à plusieurs équipements : "
                    f"{entity_id}"
                )
            entites_utilisees.add(entity_id)
            mapping_normalise[str(UUID(equipment_id))] = entity_id
        equipment_entities = mapping_normalise

        for setting_name, base_url in (
            ("DJANGO_BASE_URL", django_base_url),
            ("HOME_ASSISTANT_URL", home_assistant_url),
        ):
            parsed_url = urllib.parse.urlparse(base_url)
            if (
                parsed_url.scheme not in ("http", "https")
                or not parsed_url.netloc
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise GatewayError(
                    f"{setting_name} doit être une URL HTTP ou HTTPS absolue."
                )

        return cls(
            django_base_url=django_base_url,
            django_internal_token=internal_token,
            home_assistant_url=home_assistant_url,
            home_assistant_token=home_assistant_token,
            equipment_entities=equipment_entities,
            outbox_path=Path(
                os.environ.get(
                    "GATEWAY_OUTBOX_PATH",
                    "data/pending-callbacks.json",
                )
            ),
            poll_interval=_positive_float("GATEWAY_POLL_INTERVAL", 2.0),
            state_timeout=_positive_float("HOME_ASSISTANT_STATE_TIMEOUT", 15.0),
            request_timeout=_positive_float("GATEWAY_REQUEST_TIMEOUT", 10.0),
        )


def _required_environment(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise GatewayError(f"La variable {name} doit être configurée.")
    return value


def _positive_float(name, default):
    raw_value = os.environ.get(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise GatewayError(f"{name} doit être un nombre positif.") from exc
    if not math.isfinite(value) or value <= 0:
        raise GatewayError(f"{name} doit être un nombre positif.")
    return value


class HomeAssistantGateway:
    """Récupère une commande Django, pilote HA, puis confirme son état observé."""

    def __init__(
        self,
        config,
        opener=None,
        monotonic=None,
        sleep=None,
    ):
        self.config = config
        self.opener = opener or urllib.request.urlopen
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep

    def _request(self, base_url, path, token_header, token, payload=None):
        data = None
        headers = {token_header: token}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with self.opener(request, timeout=self.config.request_timeout) as response:
                status_code = response.status
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            raise GatewayError(f"HTTP {exc.code} lors de l'appel {path}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GatewayError(f"Échec réseau lors de l'appel {path}.") from exc

        if status_code == 204 or not response_body:
            return status_code, None
        try:
            return status_code, json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError(f"Réponse JSON invalide lors de l'appel {path}.") from exc

    def _django_request(self, path, payload=None):
        return self._request(
            self.config.django_base_url,
            path,
            "X-Internal-Service-Token",
            self.config.django_internal_token,
            payload,
        )

    def _home_assistant_request(self, path, payload=None):
        return self._request(
            self.config.home_assistant_url,
            path,
            "Authorization",
            f"Bearer {self.config.home_assistant_token}",
            payload,
        )

    def _charger_commande(self):
        status_code, payload = self._django_request(
            "/api/internal/energy-assets/commandes/suivante/",
            {},
        )
        if status_code == 204:
            return None
        if status_code != 200 or not isinstance(payload, dict):
            raise GatewayError("L'API Django a renvoyé une commande invalide.")
        return payload

    def _attendre_etat(self, entity_id, action):
        etat_cible = ETATS_CIBLES[action]
        chemin = f"/api/states/{urllib.parse.quote(entity_id, safe='')}"
        echeance = self.monotonic() + self.config.state_timeout
        while True:
            status_code, payload = self._home_assistant_request(chemin)
            if (
                status_code == 200
                and isinstance(payload, dict)
                and payload.get("state") == etat_cible
            ):
                return
            if self.monotonic() >= echeance:
                raise GatewayError(
                    f"Home Assistant n'a pas confirmé l'état {etat_cible} "
                    f"de {entity_id} dans le délai imparti."
                )
            self.sleep(min(0.5, max(echeance - self.monotonic(), 0)))

    def _executer_commande(self, commande):
        commande_id = commande.get("id")
        equipment_id = commande.get("equipement_id")
        action = commande.get("action")
        if not isinstance(commande_id, str):
            raise GatewayError("La commande Django ne contient pas d'identifiant.")
        if not isinstance(equipment_id, str) or action not in ETATS_CIBLES:
            self._mettre_en_file_callback(
                commande_id,
                "echouer",
                "La commande ne contient pas un équipement ou une action ON/OFF valide.",
            )
            return

        entity_id = self.config.equipment_entities.get(equipment_id)
        if entity_id is None:
            self._mettre_en_file_callback(
                commande_id,
                "echouer",
                f"Aucune entité Home Assistant n'est configurée pour "
                f"l'équipement {equipment_id}.",
            )
            return

        service = "turn_on" if action == "ON" else "turn_off"
        try:
            status_code, _ = self._home_assistant_request(
                f"/api/services/homeassistant/{service}",
                {"entity_id": entity_id},
            )
            if status_code not in (200, 201):
                raise GatewayError(
                    f"Home Assistant a répondu HTTP {status_code} au pilotage."
                )
            self._attendre_etat(entity_id, action)
        except GatewayError as exc:
            self._mettre_en_file_callback(commande_id, "echouer", str(exc))
            return

        self._mettre_en_file_callback(commande_id, "confirmer", None)

    def _charger_outbox(self):
        try:
            contenu = self.config.outbox_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise GatewayError("Impossible de lire la file persistante de retours.") from exc
        try:
            callbacks = json.loads(contenu)
        except json.JSONDecodeError as exc:
            raise GatewayError("La file persistante de retours est corrompue.") from exc
        if not isinstance(callbacks, list):
            raise GatewayError("La file persistante de retours doit être une liste JSON.")
        return callbacks

    def _sauvegarder_outbox(self, callbacks):
        path = self.config.outbox_path
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(callbacks, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_path, path)
        except OSError as exc:
            raise GatewayError("Impossible d'enregistrer la file persistante de retours.") from exc

    def _mettre_en_file_callback(self, commande_id, transition, detail):
        callbacks = self._charger_outbox()
        if any(callback["commande_id"] == commande_id for callback in callbacks):
            return
        callbacks.append(
            {
                "commande_id": commande_id,
                "transition": transition,
                "detail": detail,
            }
        )
        self._sauvegarder_outbox(callbacks)
        self._envoyer_callbacks_en_attente()

    def _envoyer_callbacks_en_attente(self):
        callbacks = self._charger_outbox()
        while callbacks:
            callback = callbacks[0]
            path = (
                f"/api/internal/energy-assets/commandes/"
                f"{callback['commande_id']}/{callback['transition']}/"
            )
            payload = (
                {"detail": callback["detail"]}
                if callback["transition"] == "echouer"
                else {}
            )
            try:
                status_code, _ = self._django_request(path, payload)
            except GatewayError as exc:
                LOGGER.warning(
                    "Impossible de transmettre le résultat de la commande %s ; "
                    "il sera retenté : %s",
                    callback["commande_id"],
                    exc,
                )
                return False
            if status_code != 200:
                LOGGER.error(
                    "Django a refusé le résultat de la commande %s (HTTP %s).",
                    callback["commande_id"],
                    status_code,
                )
                return False
            callbacks.pop(0)
            self._sauvegarder_outbox(callbacks)
        return True

    def run_once(self):
        """Traite au plus une commande et conserve les callbacks non transmis."""
        if not self._envoyer_callbacks_en_attente():
            return True
        commande = self._charger_commande()
        if commande is None:
            return False
        self._executer_commande(commande)
        return True

    def run_forever(self):
        LOGGER.info("Passerelle Home Assistant démarrée.")
        while True:
            try:
                a_travaille = self.run_once()
            except GatewayError:
                LOGGER.exception("La passerelle ne peut pas traiter la file de commandes.")
                a_travaille = False
            self.sleep(
                0 if a_travaille else self.config.poll_interval
            )


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = GatewayConfig.from_environment()
    except GatewayError as exc:
        raise SystemExit(str(exc)) from exc
    HomeAssistantGateway(config).run_forever()


if __name__ == "__main__":
    main()
