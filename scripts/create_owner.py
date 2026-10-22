r"""Create the OWNER: the first developer, who can then create every other user (admins, developers, users) from the admin API.
Prints a one-time setup link (valid 24 h): open it, set a password, scan the QR code with an authenticator app, confirm the first code.

    python scripts/create_owner.py <username> "<full name>" [--base-url http://127.0.0.1:8020]

If that user already exists but never finished setup (lost link), this prints a NEW link for the same user.
Creating the first user switches the app from the old single-user login (or no login) to the password + TOTP login.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import audit, platform_db  # noqa: E402
from app.security import users  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Create the owner (first developer) and print a one-time setup link.")
    p.add_argument("username")
    p.add_argument("name")
    p.add_argument("--base-url", default="http://127.0.0.1:8020")
    a = p.parse_args()

    c = platform_db.connect()
    existing = c.execute("SELECT * FROM users WHERE username=?", (a.username.strip().lower(),)).fetchone()
    if existing:
        if existing["status"] != "INVITED" or existing["created_by"] is not None or existing["role"] != "developer":
            sys.exit(f"'{a.username}' already exists and is not an owner waiting for setup. Nothing changed.")
        token = users.issue_invite(c, existing["id"])
        audit.append(c, "owner.new_link", target_type="user", target_id=existing["id"])
    else:
        if c.execute("SELECT 1 FROM users WHERE role='developer' AND created_by IS NULL").fetchone():
            sys.exit("An owner already exists. Create other users from the admin API (POST /api/admin/users) while logged in as the owner.")
        user, token = users.create_user(c, name=a.name, username=a.username, email=None, role="developer", created_by=None)
        audit.append(c, "owner.create", target_type="user", target_id=user["id"], detail={"username": user["username"]})
    print(f"Open this link within 24 hours (it works once):\n\n  {a.base_url.rstrip('/')}/setup/{token}\n")


if __name__ == "__main__":
    main()
