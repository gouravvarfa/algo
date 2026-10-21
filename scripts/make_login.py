r"""Create the login settings for a hosted deployment. Prints lines for the server's environment file
(e.g. /etc/algo-trading.env). The password itself is never stored, only its PBKDF2 hash.

    python scripts/make_login.py <username>
"""
import getpass, secrets, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import hash_password  # noqa: E402

user = sys.argv[1] if len(sys.argv) > 1 else input("username: ").strip()
pw = getpass.getpass("password (min 12 chars): ")
if len(pw) < 12 or pw != getpass.getpass("repeat: "):
    sys.exit("password too short or not repeated correctly")
print(f"APP_USERNAME={user}\nAPP_PASSWORD_HASH={hash_password(pw)}\nAPP_SECRET={secrets.token_hex(32)}")
