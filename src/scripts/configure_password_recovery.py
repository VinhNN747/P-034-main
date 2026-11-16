"""Apply and verify the recovery OTP email template on a hosted Supabase project.

Preview: python src/scripts/configure_password_recovery.py
Apply:   python src/scripts/configure_password_recovery.py --apply
Check:   python src/scripts/configure_password_recovery.py --check
Uses SUPABASE_ACCESS_TOKEN from the environment or local .env, never a public key.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "deploy" / "supabase" / "reset-password-email.html"
SUBJECT = "Taskway — Mã xác nhận đặt lại mật khẩu / Password reset code"
CONFIG_KEYS = ("mailer_templates_recovery_content", "mailer_subjects_recovery", "uri_allow_list")


def desired_config(current: dict, template: str, callbacks: list[str]) -> dict:
    allowed = [item.strip() for item in str(current.get("uri_allow_list") or "").split(",") if item.strip()]
    return {
        "mailer_templates_recovery_content": template,
        "mailer_subjects_recovery": SUBJECT,
        "uri_allow_list": ",".join(dict.fromkeys([*allowed, *callbacks])),
    }


def configuration_matches(current: dict, desired: dict) -> bool:
    return all(current.get(key) == value for key, value in desired.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply the OTP template and verify it by reading it back")
    mode.add_argument("--check", action="store_true", help="Check the cloud template without changing it")
    args = parser.parse_args()
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    supabase_url = values.get("SUPABASE_URL", "") or ""
    project_ref = values.get("SUPABASE_PROJECT_REF")
    if not project_ref:
        host = urlsplit(supabase_url).hostname or ""
        if host.endswith(".supabase.co"):
            project_ref = host.removesuffix(".supabase.co")
    if not project_ref or not re.fullmatch(r"[a-z0-9]{20}", project_ref):
        print("Cannot determine hosted project. Set SUPABASE_PROJECT_REF to the intended project ref.")
        return 1
    template = TEMPLATE.read_text(encoding="utf-8")
    if "{{ .Token }}" not in template or "{{ .ConfirmationURL }}" in template:
        print("The local template must display .Token and must not contain a password reset link.")
        return 1
    callbacks = []
    for origin in (values.get("CORS_ORIGINS") or "").split(","):
        parsed = urlsplit(origin.strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password:
            callbacks.append(f"{parsed.scheme}://{parsed.netloc}/auth/confirm")
    print(f"Target project: {project_ref}")
    print(f"Dashboard: https://supabase.com/dashboard/project/{project_ref}/auth/templates")
    print("Changes: recovery email body (.Token, no link), subject, and missing frontend callbacks.")
    print("Callbacks: " + ", ".join(callbacks))
    if not (args.apply or args.check):
        print("Preview only. Use --apply to update the project, or --check to inspect it.")
        return 0
    token = values.get("SUPABASE_ACCESS_TOKEN")
    if not token:
        print("Missing SUPABASE_ACCESS_TOKEN. The publishable key cannot read or update email templates.")
        return 2
    endpoint = f"https://api.supabase.com/v1/projects/{project_ref}/config/auth"
    try:
        with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"}) as client:
            response = client.get(endpoint)
            if not response.is_success:
                print(f"Unable to read Auth config (HTTP {response.status_code}). Check the token's project and Auth config read permission.")
                return 2
            current = response.json()
            desired = desired_config(current, template, callbacks)
            if configuration_matches(current, desired):
                print("Verified: the cloud recovery template displays an OTP and all callbacks are configured.")
                return 0
            if args.check:
                print("The cloud recovery email or redirect list differs from the OTP configuration. Run --apply.")
                return 1
            backup_dir = ROOT / "tmp"
            backup_dir.mkdir(exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / f"supabase-recovery-{project_ref}-{timestamp}.json"
            # Back up only the fields we change; never serialize SMTP/API secrets.
            backup_path.write_text(json.dumps({key: current.get(key) for key in CONFIG_KEYS}, ensure_ascii=False, indent=2), encoding="utf-8")
            updated = client.patch(endpoint, json=desired)
            if not updated.is_success:
                print(f"Unable to update Auth config (HTTP {updated.status_code}). Check Auth config write permission.")
                return 2
            verified = client.get(endpoint)
            if not verified.is_success or not configuration_matches(verified.json(), desired):
                print("Update was submitted but read-back verification failed. Do not assume the email template is active.")
                return 1
            print("Applied and verified: future recovery emails display an OTP without a reset link.")
            print(f"Previous template backup: {backup_path}")
            return 0
    except (httpx.HTTPError, ValueError):
        print("Unable to access Supabase Management API. No credentials or provider response were logged.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
