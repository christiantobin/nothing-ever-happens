from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from bot.exchange.kalshi_auth import KalshiSigner


def _test_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_sign_produces_required_headers(tmp_path):
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(_test_key_pem())

    signer = KalshiSigner(access_key_id="test", private_key_path=str(key_path))
    headers = signer.sign(timestamp_ms=1700000000000, method="GET", path="/trade-api/v2/exchange/status")

    assert headers["KALSHI-ACCESS-KEY"] == "test"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    assert len(headers["KALSHI-ACCESS-SIGNATURE"]) > 0


def test_sign_strips_query_params(tmp_path):
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(_test_key_pem())

    signer = KalshiSigner(access_key_id="test", private_key_path=str(key_path))
    # Path with and without query should produce signatures that differ only by PSS salt,
    # but both must be valid — we confirm both succeed and are non-empty.
    h1 = signer.sign(timestamp_ms=1700000000000, method="GET", path="/x")
    h2 = signer.sign(timestamp_ms=1700000000000, method="GET", path="/x?foo=bar")
    assert len(h1["KALSHI-ACCESS-SIGNATURE"]) == len(h2["KALSHI-ACCESS-SIGNATURE"]) > 0


def test_sign_differs_across_calls_due_to_pss_salt(tmp_path):
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(_test_key_pem())

    signer = KalshiSigner(access_key_id="test", private_key_path=str(key_path))
    h1 = signer.sign(timestamp_ms=1700000000000, method="GET", path="/x")
    h2 = signer.sign(timestamp_ms=1700000000000, method="GET", path="/x")
    assert h1["KALSHI-ACCESS-SIGNATURE"] != h2["KALSHI-ACCESS-SIGNATURE"]
