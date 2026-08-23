import runpy
from pathlib import Path

import yaml


CONFIGURE_SCRIPT = Path(__file__).parents[1] / "docker" / "configure-hermes-factory.py"


def test_factory_webhook_uses_configured_upstream_delivery(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("FACTORY_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("FACTORY_NOTIFICATION_DELIVER", "telegram")
    monkeypatch.setenv("FACTORY_NOTIFICATION_CHAT_ID", "123456")

    namespace = runpy.run_path(str(CONFIGURE_SCRIPT))
    namespace["main"]()

    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert config["platforms"]["webhook"]["enabled"] is True
    subscriptions = yaml.safe_load(
        (tmp_path / "webhook_subscriptions.json").read_text(encoding="utf-8")
    )
    route = subscriptions["factory-notifications"]
    assert route["deliver"] == "telegram"
    assert route["deliver_extra"] == {"chat_id": "123456"}


def test_factory_webhook_defaults_to_autonomous_session_log(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("FACTORY_WEBHOOK_ENABLED", "true")
    monkeypatch.delenv("FACTORY_NOTIFICATION_DELIVER", raising=False)
    monkeypatch.delenv("FACTORY_NOTIFICATION_CHAT_ID", raising=False)

    namespace = runpy.run_path(str(CONFIGURE_SCRIPT))
    namespace["main"]()

    subscriptions = yaml.safe_load(
        (tmp_path / "webhook_subscriptions.json").read_text(encoding="utf-8")
    )
    route = subscriptions["factory-notifications"]
    assert route["deliver"] == "log"
    assert "deliver_extra" not in route
