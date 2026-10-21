from unittest.mock import patch

from django.test import SimpleTestCase

from accounts.api import n8n_client


class N8NClientTests(SimpleTestCase):
    @patch.object(n8n_client, "N8N_WEBHOOK_TIMEOUT", 3)
    @patch.object(n8n_client, "N8N_WEBHOOK_TOKEN", "test-webhook-token")
    @patch.object(n8n_client, "N8N_WEBHOOK_BASE_URL", "http://localhost:5678/webhook")
    @patch("accounts.api.n8n_client.urllib.request.urlopen")
    def test_post_event_sends_configured_token_and_payload(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        self.assertTrue(n8n_client._post_event("/anomalie-detectee", {"id": "test"}))

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:5678/webhook/anomalie-detectee")
        self.assertEqual(request.get_header("X-n8n-webhook-token"), "test-webhook-token")
        self.assertEqual(request.data, b'{"id": "test"}')
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3)
        response.read.assert_called_once()

    @patch.object(n8n_client, "N8N_WEBHOOK_TOKEN", "replace-with-a-token")
    @patch.object(n8n_client, "N8N_WEBHOOK_BASE_URL", "http://localhost:5678/webhook")
    @patch("accounts.api.n8n_client.urllib.request.urlopen")
    def test_post_event_does_not_send_when_token_is_a_placeholder(self, urlopen):
        self.assertFalse(n8n_client._post_event("/anomalie-detectee", {}))
        urlopen.assert_not_called()
