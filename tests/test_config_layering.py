"""Tests for the layered config: env > store > .env > defaults.

Env vars are automation's contract and always win; the store (data/config.json)
is what the admin UI writes and must persist + apply live; env-owned keys must
be refused by save_config so UI edits are never silent no-ops."""

import json

import pytest

from gitbot import config


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "STORE_PATH", path)
    # save_config live-applies to the singleton — restore it after the test.
    snapshot = config.settings.model_dump()
    yield path
    for k, v in snapshot.items():
        setattr(config.settings, k, v)


def test_default_when_nothing_set(store, monkeypatch):
    monkeypatch.delenv("GITBOT_BOT_USERNAME", raising=False)
    assert config.Settings().bot_username == "gitbot"


def test_store_beats_default(store, monkeypatch):
    monkeypatch.delenv("GITBOT_BOT_USERNAME", raising=False)
    store.write_text(json.dumps({"bot_username": "storebot"}))
    assert config.Settings().bot_username == "storebot"


def test_env_beats_store(store, monkeypatch):
    store.write_text(json.dumps({"bot_username": "storebot"}))
    monkeypatch.setenv("GITBOT_BOT_USERNAME", "envbot")
    assert config.Settings().bot_username == "envbot"


def test_store_ignores_unknown_keys(store, monkeypatch):
    monkeypatch.delenv("GITBOT_BOT_USERNAME", raising=False)
    store.write_text(json.dumps({"bot_username": "storebot", "not_a_field": 1}))
    s = config.Settings()
    assert s.bot_username == "storebot"
    assert not hasattr(s, "not_a_field")


def test_save_config_persists_and_applies_live(store, monkeypatch):
    monkeypatch.delenv("GITBOT_QUESTION_THRESHOLD", raising=False)
    applied, locked = config.save_config({"question_threshold": 4})
    assert applied == ["question_threshold"] and locked == []
    # live-applied to the singleton
    assert config.settings.question_threshold == 4
    # persisted: a fresh Settings() reads it back from the store
    assert config.Settings().question_threshold == 4


def test_save_config_coerces_types(store, monkeypatch):
    monkeypatch.delenv("GITBOT_GITLAB_SSL_VERIFY", raising=False)
    config.save_config({"gitlab_ssl_verify": "false"})  # string from a UI select
    assert config.settings.gitlab_ssl_verify is False
    assert json.loads(store.read_text())["gitlab_ssl_verify"] is False


def test_save_config_refuses_env_owned_keys(store, monkeypatch):
    monkeypatch.setenv("GITBOT_BOT_USERNAME", "envbot")
    applied, locked = config.save_config({"bot_username": "sneaky"})
    assert applied == [] and locked == ["bot_username"]
    assert not store.exists()  # nothing written


def test_save_config_ignores_unknown_keys(store):
    applied, locked = config.save_config({"nonsense": "x"})
    assert applied == [] and locked == []


def test_config_sources_provenance(store, monkeypatch):
    monkeypatch.setenv("GITBOT_GITLAB_URL", "https://env.example")
    monkeypatch.delenv("GITBOT_BOT_USERNAME", raising=False)
    monkeypatch.delenv("GITBOT_QUESTION_THRESHOLD", raising=False)
    store.write_text(json.dumps({"bot_username": "storebot"}))
    src = config.config_sources()
    assert src["gitlab_url"] == "env"
    assert src["bot_username"] == "store"
    assert src["question_threshold"] == "default"
