from src.config import SnowLumaConfig


def test_ws_url_without_token():
    cfg = SnowLumaConfig(host="127.0.0.1", port=3001, token="")
    assert cfg.ws_url() == "ws://127.0.0.1:3001"


def test_ws_url_with_token():
    cfg = SnowLumaConfig(host="127.0.0.1", port=3001, token="a b")
    assert cfg.ws_url() == "ws://127.0.0.1:3001?access_token=a+b"
