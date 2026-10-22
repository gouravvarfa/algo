r"""Forgot the password, lost the authenticator, or locked out? Run this ON THE SERVER to get a new one-time setup link for a user.

    python scripts/reset_user.py <username> [--base-url http://127.0.0.1:8020]

The user's old password, authenticator and backup codes stop working, every session of theirs is closed, and the link lets them choose a NEW
password and scan a NEW QR code (valid 24 h, usable once). Anyone who can run this already controls the server and its database, so it asks
for no login. Other users should ask an admin instead: POST /api/admin/users/{id}/reset-totp does the same from inside the app.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import audit, platform_db  # noqa: E402
from app.security import users  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Reset a user's password and authenticator; prints a one-time setup link.")
    p.add_argument("username")
    p.add_argument("--base-url", default="http://127.0.0.1:8020")
    a = p.parse_args()
    c = platform_db.connect()
    try:
        user, token = users.reset_account_as_operator(c, a.username)
    except users.AuthError as exc:
        sys.exit(exc.message)
    audit.append(c, "operator.reset", target_type="user", target_id=user["id"], detail={"username": user["username"], "via": "scripts/reset_user.py"})
    print(f"'{user['username']}' has been reset. Their old password, authenticator and backup codes no longer work.\n"
          f"Open this link within 24 hours (it works once) to choose a new password and scan a new QR code:\n\n  {a.base_url.rstrip('/')}/setup/{token}\n")


if __name__ == "__main__":
    main()
