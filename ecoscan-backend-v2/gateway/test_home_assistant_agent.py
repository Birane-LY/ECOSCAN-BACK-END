import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from gateway.home_assistant_agent import GatewayConfig, HomeAssistantGateway


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.status = status
        self.body = (
            b""
            if payload is None
            else json.dumps(payload).encode("utf-8")
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class HomeAssistantGatewayTests(unittest.TestCase):
    equipment_id = "6a8354e0-6088-4979-9bc9-f2055d2d7401"
    command_id = "b29ecee3-0759-41bc-b65e-e3ec2594a7a0"
    entity_id = "switch.ecoscan_pompe"

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.outbox_path = Path(self.temporary_directory.name) / "data" / "callbacks.json"

    def build_gateway(self, opener, **overrides):
        monotonic = overrides.pop("monotonic", lambda: 0)
        sleep = overrides.pop("sleep", lambda _: None)
        config_values = {
            "django_base_url": "https://django.example.test",
            "django_internal_token": "django-secret",
            "home_assistant_url": "http://homeassistant.local:8123",
            "home_assistant_token": "ha-secret",
            "equipment_entities": {self.equipment_id: self.entity_id},
            "outbox_path": self.outbox_path,
            "poll_interval": 1,
            "state_timeout": 2,
            "request_timeout": 1,
        }
        config_values.update(overrides)
        return HomeAssistantGateway(
            GatewayConfig(**config_values),
            opener=opener,
            monotonic=monotonic,
            sleep=sleep,
        )

    def command_response(self, action="ON"):
        return FakeResponse(
            {
                "id": self.command_id,
                "equipement_id": self.equipment_id,
                "equipement_nom": "Pompe",
                "action": action,
                "statut": "SENT",
            }
        )

    def test_command_is_confirmed_only_after_home_assistant_reports_target_state(self):
        opener = FakeOpener(
            [
                self.command_response(),
                FakeResponse([]),
                FakeResponse({"entity_id": self.entity_id, "state": "on"}),
                FakeResponse({"statut": "CONFIRMED"}),
            ]
        )
        gateway = self.build_gateway(opener)

        self.assertTrue(gateway.run_once())

        home_assistant_request = opener.requests[1][0]
        self.assertEqual(
            home_assistant_request.full_url,
            "http://homeassistant.local:8123/api/services/homeassistant/turn_on",
        )
        self.assertEqual(
            home_assistant_request.get_header("Authorization"),
            "Bearer ha-secret",
        )
        self.assertEqual(
            json.loads(home_assistant_request.data),
            {"entity_id": self.entity_id},
        )
        state_request = opener.requests[2][0]
        self.assertEqual(
            state_request.full_url,
            f"http://homeassistant.local:8123/api/states/{self.entity_id}",
        )
        confirmation_request = opener.requests[3][0]
        self.assertTrue(confirmation_request.full_url.endswith("/confirmer/"))
        self.assertEqual(
            confirmation_request.get_header("X-internal-service-token"),
            "django-secret",
        )
        self.assertEqual(
            json.loads(self.outbox_path.read_text(encoding="utf-8")),
            [],
        )

    def test_missing_equipment_mapping_reports_failure_without_actuating(self):
        opener = FakeOpener(
            [
                self.command_response(),
                FakeResponse({"statut": "FAILED"}),
            ]
        )
        gateway = self.build_gateway(opener, equipment_entities={})

        self.assertTrue(gateway.run_once())

        self.assertEqual(len(opener.requests), 2)
        failure_request = opener.requests[1][0]
        self.assertTrue(failure_request.full_url.endswith("/echouer/"))
        self.assertIn(
            "Aucune entité Home Assistant",
            json.loads(failure_request.data)["detail"],
        )

    def test_unconfirmed_home_assistant_state_fails_the_command(self):
        opener = FakeOpener(
            [
                self.command_response(),
                FakeResponse([]),
                FakeResponse({"entity_id": self.entity_id, "state": "off"}),
                FakeResponse({"statut": "FAILED"}),
            ]
        )
        gateway = self.build_gateway(
            opener,
            monotonic=iter((0, 3)).__next__,
        )

        self.assertTrue(gateway.run_once())

        failure_request = opener.requests[3][0]
        self.assertTrue(failure_request.full_url.endswith("/echouer/"))
        self.assertIn(
            "n'a pas confirmé l'état on",
            json.loads(failure_request.data)["detail"],
        )

    def test_callback_is_persisted_and_retried_without_repeating_actuation(self):
        opener = FakeOpener(
            [
                self.command_response(),
                FakeResponse([]),
                FakeResponse({"entity_id": self.entity_id, "state": "on"}),
                URLError("backend temporarily unavailable"),
                FakeResponse({"statut": "CONFIRMED"}),
                FakeResponse(status=204),
            ]
        )
        gateway = self.build_gateway(opener)

        self.assertTrue(gateway.run_once())
        self.assertEqual(
            json.loads(self.outbox_path.read_text(encoding="utf-8"))[0][
                "commande_id"
            ],
            self.command_id,
        )

        self.assertFalse(gateway.run_once())

        self.assertEqual(
            sum(
                "/api/services/homeassistant/" in request.full_url
                for request, _ in opener.requests
            ),
            1,
        )
        self.assertEqual(
            json.loads(self.outbox_path.read_text(encoding="utf-8")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
