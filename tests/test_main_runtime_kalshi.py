import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def key_path(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "kalshi-key.pem"
    p.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(p)


def test_build_kalshi_exchange_returns_client_with_creds(monkeypatch, key_path):
    monkeypatch.setenv("KALSHI_ACCESS_KEY_ID", "test-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", key_path)
    monkeypatch.setenv("KALSHI_ENV", "demo")

    from bot.exchange.kalshi import KalshiExchangeClient
    from bot.main import _build_kalshi_exchange

    client = _build_kalshi_exchange(allow_trading=False)
    assert isinstance(client, KalshiExchangeClient)


def test_build_kalshi_exchange_falls_back_to_paper_without_creds(monkeypatch):
    monkeypatch.delenv("KALSHI_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)

    from bot.exchange.paper import PaperExchangeClient
    from bot.main import _build_kalshi_exchange

    client = _build_kalshi_exchange(allow_trading=False)
    assert isinstance(client, PaperExchangeClient)


def test_build_kalshi_exchange_rejects_bad_env(monkeypatch, key_path):
    monkeypatch.setenv("KALSHI_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", key_path)
    monkeypatch.setenv("KALSHI_ENV", "staging")

    from bot.main import _build_kalshi_exchange

    with pytest.raises(ValueError, match="KALSHI_ENV"):
        _build_kalshi_exchange(allow_trading=False)
