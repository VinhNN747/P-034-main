# Email verification

Signup uses the same request-then-verify pattern as password recovery. The
frontend clears any previous session, then calls `/api/account/signup/request`.
The management backend calls Supabase signup and returns only `{pending: true}`
for an unconfirmed user; it never returns session credentials or marks signup
complete. `verifyOtp` with the email and code confirms the user and issues the
session. Supabase enforces OTP expiry, single use, and rate limits. The signup
page supports resending codes and retains only the pending email in session
storage across refreshes. No password or OTP is saved there. Like the recovery
email, the signup email displays an OTP without a confirmation link or button.
Users return to the open signup page to enter it. On another device, users can
open `/sign-up?verify=1` and enter their registered email and the OTP.

Deploy the management backend containing `/api/v1/auth/signup/request` and
`/api/v1/auth/signup/resend`, then the
frontend containing the OTP form, **before** applying the email template, so
users receiving the new email have a place to enter its code. Sign-in, protected
pages and management API access reject accounts without `email_confirmed_at`.
Pending rows in `auth.users` are expected; they do not grant access to the app.

For the current Render deployment, deploy the commit containing these changes
to the backend and frontend services' configured branches. Wait for both to become
Live, then check `/sign-up` shows the Send signup OTP button before applying
the Supabase settings below. Keep the existing Render environment variables;
this change does not require a new frontend or backend secret.

Resends check the account database first. Already confirmed accounts get a
clear message to sign in instead of an HTTP 200 that silently sends no mail.
Provider rate limits and mail failures are reported separately. An accepted
resend request is not a guarantee that the message reached the inbox.

From the repository root, with `SUPABASE_URL` (or `SUPABASE_PROJECT_REF`) and
`SUPABASE_ACCESS_TOKEN` set in the environment or local `.env`:

```powershell
python -m src.scripts.configure_signup_verification
python -m src.scripts.configure_signup_verification --apply
python -m src.scripts.configure_signup_verification --check
```

The script changes only the signup subject, signup email body and
`mailer_autoconfirm=false` (Confirm Email enabled). It backs up the previous
values under the ignored `tmp/` directory and reads the config back to verify
the update. It does not change recovery emails, SMTP, OTP expiry or redirect
settings. Email OTP lengths of 6–10 digits are accepted by the UI.

Manual setup in Supabase Auth: enable Confirm Email, then use
`confirm-signup-email.html` as the **Confirm sign up** template. Display
`{{ .Token }}` instead of a confirmation URL, exactly like password recovery.

Existing link emails are unchanged. Pending users can return to the signup page
and select Resend code (or submit their original email and password again) to
receive an OTP. An invalid legacy link now offers separate signup and password
reset paths.

Verification before release:

1. Register a new email; the OTP form appears and protected pages remain blocked.
2. Verify the email contains an OTP and no confirmation link or button.
3. Enter an incorrect/expired code; registration stays pending.
4. Resend after the cooldown; verify with the latest code.
5. Confirm the success message and that the account can access Taskway.
6. Refresh a pending signup; only the email/OTP step is restored.
7. Try signing in or opening dashboard with an unconfirmed account; login and
   protected pages must refuse access, and management APIs must return 403.
8. Resend for an already confirmed email; the page must explain that no new
   signup email will be sent and offer sign-in.

Local validation: production frontend build, ESLint and Ruff passed; 63
frontend auth/recovery tests and 42 backend auth/recovery/configuration tests
passed. The wider test run has existing failures in incident-report navigation
(`gateway-navigation.test.ts`) and application archiving
(`test_application_api_archives_application`). The navigation files are
unchanged; archiving still fails when using the original auth dependency from
HEAD. These checks do not validate delivery to a real inbox.

References: [Supabase email templates](https://supabase.com/docs/guides/auth/auth-email-templates)
and [Confirm Email settings](https://supabase.com/docs/guides/auth/general-configuration).
