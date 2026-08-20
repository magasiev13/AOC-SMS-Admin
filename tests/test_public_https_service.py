import socket
import unittest
from unittest.mock import patch

from app.services.public_https_service import (
    PublicHttpsFetchError,
    fetch_public_https_text,
    resolve_public_https_target,
)


def _address_record(address: str) -> tuple[object, ...]:
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))


class TestPublicHttpsService(unittest.TestCase):
    def test_private_and_link_local_addresses_are_rejected(self) -> None:
        for address in ("127.0.0.1", "10.0.0.8", "169.254.169.254"):
            with self.subTest(address=address):
                with patch(
                    "app.services.public_https_service.socket.getaddrinfo",
                    return_value=[_address_record(address)],
                ):
                    with self.assertRaisesRegex(PublicHttpsFetchError, "non-public"):
                        resolve_public_https_target("https://policies.example.org/privacy")

    def test_mixed_public_and_private_dns_answers_are_rejected(self) -> None:
        records = [
            _address_record("93.184.216.34"),
            _address_record("127.0.0.1"),
        ]
        with patch(
            "app.services.public_https_service.socket.getaddrinfo",
            return_value=records,
        ):
            with self.assertRaisesRegex(PublicHttpsFetchError, "127.0.0.1"):
                resolve_public_https_target("https://policies.example.org/privacy")

    def test_each_redirect_hop_is_revalidated(self) -> None:
        def getaddrinfo(hostname, port, type):
            del port, type
            if hostname == "public.example.org":
                return [_address_record("93.184.216.34")]
            return [_address_record("127.0.0.1")]

        with patch(
            "app.services.public_https_service.socket.getaddrinfo",
            side_effect=getaddrinfo,
        ), patch(
            "app.services.public_https_service._read_response",
            return_value=(302, {"location": "https://internal.example.org/secret"}, b""),
        ):
            with self.assertRaisesRegex(PublicHttpsFetchError, "non-public"):
                fetch_public_https_text(
                    "https://public.example.org/start",
                    2,
                    4096,
                    3,
                    "TwineviaTest/1.0",
                )

    def test_nonstandard_ports_and_user_information_are_rejected_before_dns(self) -> None:
        with patch("app.services.public_https_service.socket.getaddrinfo") as getaddrinfo:
            with self.assertRaisesRegex(PublicHttpsFetchError, "standard HTTPS port"):
                resolve_public_https_target("https://public.example.org:8443/privacy")
            with self.assertRaisesRegex(PublicHttpsFetchError, "user information"):
                resolve_public_https_target("https://user:password@public.example.org/privacy")
        getaddrinfo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
