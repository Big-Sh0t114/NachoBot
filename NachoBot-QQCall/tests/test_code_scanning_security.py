import ssl
import unittest
from unittest.mock import patch

from service import rpc


class WebSocketTlsSecurityTests(unittest.TestCase):
    def test_secure_websocket_uses_default_verification_and_minimum_tls_12(self):
        raw_socket = object()
        wrapped_socket = object()
        observed = {}

        def wrap(context, sock, *, server_hostname):
            observed["context"] = context
            observed["socket"] = sock
            observed["server_hostname"] = server_hostname
            return wrapped_socket

        with patch.object(ssl.SSLContext, "wrap_socket", autospec=True, side_effect=wrap):
            result = rpc._secure_websocket_socket(raw_socket, "localhost")

        context = observed["context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertIs(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(observed["socket"], raw_socket)
        self.assertEqual(observed["server_hostname"], "localhost")
        self.assertIs(result, wrapped_socket)


if __name__ == "__main__":
    unittest.main()
