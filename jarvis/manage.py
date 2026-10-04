"""Account management on the host: python -m jarvis.manage set-password | enroll-totp | confirm-totp CODE."""
import getpass
import sys

from jarvis import auth


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "set-password":
        user = input("username [owner]: ").strip() or "owner"
        password = getpass.getpass("password (12+ chars): ")
        if password != getpass.getpass("again: "):
            sys.exit("passwords differ")
        auth.set_password(user, password)
        print("saved")
    elif cmd == "enroll-totp":
        secret, uri = auth.enroll_totp()
        print(f"Add this to your authenticator app:\n{uri}\nsecret: {secret}\nThen run: python -m jarvis.manage confirm-totp CODE")
    elif cmd == "confirm-totp" and len(sys.argv) > 2:
        print("enabled" if auth.confirm_totp(sys.argv[2]) else "wrong code")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
