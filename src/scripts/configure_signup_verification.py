"""Configure hosted Supabase signup emails to require an email OTP.

Preview: python src/scripts/configure_signup_verification.py
Apply:   python src/scripts/configure_signup_verification.py --apply
Check:   python src/scripts/configure_signup_verification.py --check
Reads SUPABASE_ACCESS_TOKEN from the environment or .env; never logs credentials.
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
TEMPLATE = ROOT / "src" / "deploy" / "supabase" / "confirm-signup-email.html"
SUBJECT = "Taskway — Mã OTP xác nhận đăng ký / Signup verification code"
CONFIG_KEYS = ("mailer_autoconfirm", "mailer_templates_confirmation_content", "mailer_subjects_confirmation")


def desired_config(template: str) -> dict:
    return {
        "mailer_autoconfirm": False,
        "mailer_templates_confirmation_content": template,
        "mailer_subjects_confirmation": SUBJECT,
    }


def configuration_matches(current: dict, desired: dict) -> bool:
    return all(current.get(key) == value for key, value in desired.items())


def is_otp_template(template: str) -> bool:
    # Match recovery: the email contains a code, no confirmation button/link.
    return "{{ .Token }}" in template and not re.search(r"\bhref\s*=", template, re.IGNORECASE) and not any(
        forbidden in template
        for forbidden in (".ConfirmationURL", ".TokenHash")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply signup OTP settings and verify by reading them back")
    mode.add_argument("--check", action="store_true", help="Check cloud signup OTP settings without changing them")
    args = parser.parse_args()
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    project_ref = values.get("SUPABASE_PROJECT_REF")
    if not project_ref:
        host = urlsplit(values.get("SUPABASE_URL") or "").hostname or ""
        if host.endswith(".supabase.co"):
            project_ref = host.removesuffix(".supabase.co")
    if not project_ref or not re.fullmatch(r"[a-z0-9]{20}", project_ref):
        print("Cannot determine hosted project. Set SUPABASE_PROJECT_REF to the intended project ref.")
        return 1
    template = TEMPLATE.read_text(encoding="utf-8")
    if not is_otp_template(template):
        print("The signup template must display .Token and must not contain links.")
        return 1
    desired = desired_config(template)
    print(f"Target project: {project_ref}")
    print("Changes: require email confirmation, signup email with OTP only (like recovery), and signup subject.")
    if not (args.apply or args.check):
        print("Preview only. Use --apply to update the project, or --check to inspect it.")
        return 0
    token = values.get("SUPABASE_ACCESS_TOKEN")
    if not token:
        print("Missing SUPABASE_ACCESS_TOKEN. The publishable key cannot update Auth settings.")
        return 2
    endpoint = f"https://api.supabase.com/v1/projects/{project_ref}/config/auth"
    try:
        with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"}) as client:
            response = client.get(endpoint)
            if not response.is_success:
                print(f"Unable to read Auth config (HTTP {response.status_code}). Check project access.")
                return 2
            current = response.json()
            if configuration_matches(current, desired):
                print("Verified: signup emails contain an OTP and email confirmation is required.")
                return 0
            if args.check:
                print("Cloud signup settings differ from the required OTP configuration. Run --apply.")
                return 1
            backup_dir = ROOT / "tmp"
            backup_dir.mkdir(exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / f"supabase-signup-{project_ref}-{timestamp}.json"
            # Back up only changed fields. Never serialize SMTP/API secrets.
            backup_path.write_text(json.dumps({key: current.get(key) for key in CONFIG_KEYS}, ensure_ascii=False, indent=2), encoding="utf-8")
            updated = client.patch(endpoint, json=desired)
            if not updated.is_success:
                print(f"Unable to update Auth config (HTTP {updated.status_code}). Check Auth config write permission.")
                return 2
            verified = client.get(endpoint)
            if not verified.is_success or not configuration_matches(verified.json(), desired):
                print("Read-back verification failed. Do not assume signup OTP settings are active.")
                return 1
            print("Applied and verified: future signup emails contain an OTP; unconfirmed users cannot sign in.")
            print(f"Previous signup settings backup: {backup_path}")
            return 0
    except (httpx.HTTPError, ValueError):
        print("Unable to access Supabase Management API. No credentials or provider response were logged.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
