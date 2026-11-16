import json
import sys

import httpx

from src.scripts import configure_password_recovery as configuration


def test_cloud_sync_preserves_existing_callbacks_and_only_updates_recovery_config(monkeypatch, tmp_path):
    template = tmp_path / "recovery.html"
    template.write_text("<p>{{ .Token }}</p>", encoding="utf-8")
    monkeypatch.setattr(configuration, "ROOT", tmp_path)
    monkeypatch.setattr(configuration, "TEMPLATE", template)
    monkeypatch.setattr(configuration, "dotenv_values", lambda _: {})
    monkeypatch.setenv("SUPABASE_PROJECT_REF", "a" * 20)
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setattr(sys, "argv", ["configure_password_recovery.py", "--apply"])
    current = {
        "site_url": "https://taskway.example.com",
        "smtp_pass": "smtp-secret-not-to-be-saved",
        "mailer_templates_recovery_content": "<a href='{{ .ConfirmationURL }}'>Reset</a>",
        "mailer_subjects_recovery": "Reset password",
        "uri_allow_list": "https://taskway.example.com/auth/confirm",
    }
    patched = []

    def handler(request):
        if request.method == "PATCH":
            change = json.loads(request.content)
            patched.append(change)
            current.update(change)
        return httpx.Response(200, json=current)

    client = httpx.Client
    monkeypatch.setattr(configuration.httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(handler),
    ))
    assert configuration.main() == 0
    assert len(patched) == 1
    assert set(patched[0]) == set(configuration.CONFIG_KEYS)
    assert current["site_url"] == "https://taskway.example.com"
    assert current["smtp_pass"] == "smtp-secret-not-to-be-saved"
    assert current["uri_allow_list"] == "https://taskway.example.com/auth/confirm,http://localhost:3000/auth/confirm"
    backup = next((tmp_path / "tmp").glob("supabase-recovery-*.json"))
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert set(saved) == set(configuration.CONFIG_KEYS)
    assert "smtp-secret" not in backup.read_text(encoding="utf-8")


def test_cloud_sync_fails_without_management_credentials(monkeypatch, capsys):
    monkeypatch.setattr(configuration, "dotenv_values", lambda _: {})
    monkeypatch.setenv("SUPABASE_PROJECT_REF", "a" * 20)
    monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["configure_password_recovery.py", "--apply"])
    assert configuration.main() == 2
    assert "Missing SUPABASE_ACCESS_TOKEN" in capsys.readouterr().out
