"""
Kite Connect authentication flow.

Kite Connect requires a daily re-authentication: you log in manually (2FA
required, this cannot be automated without your credentials/TOTP device),
capture a request_token from the redirect, then exchange it for an
access_token that's valid until it expires (typically end of trading day /
early next morning).

Usage:
    python kite_auth.py login
        -> prints the login URL to open in your browser

    python kite_auth.py exchange <request_token>
        -> exchanges the request_token for an access_token, saves it locally

Run `login` once, log in through your browser, copy the request_token from
the redirect URL, then run `exchange` with it. Do this once per trading day.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv
import os

from kiteconnect import KiteConnect

# Load API key/secret from .env (never hardcoded, never committed)
ENV_PATH = Path(__file__).parent.parent.parent / ".env"
load_dotenv(ENV_PATH)

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")

# The daily access_token is cached separately from .env (it changes every
# day, .env holds the static key/secret) — also gitignored.
TOKEN_CACHE_PATH = Path(__file__).parent.parent.parent / ".kite_token_cache"


def get_login_url() -> str:
    if not API_KEY:
        raise RuntimeError(
            "KITE_API_KEY not found. Check that .env exists at the project "
            "root and contains KITE_API_KEY=<your key>."
        )
    kite = KiteConnect(api_key=API_KEY)
    return kite.login_url()


def exchange_request_token(request_token: str) -> str:
    """
    Exchange a request_token (captured from the login redirect) for an
    access_token. Saves the access_token to a local cache file for reuse
    by other scripts today. Returns the access_token.
    """
    if not API_KEY or not API_SECRET:
        raise RuntimeError(
            "KITE_API_KEY or KITE_API_SECRET not found. Check .env at the "
            "project root."
        )

    kite = KiteConnect(api_key=API_KEY)
    session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]

    TOKEN_CACHE_PATH.write_text(access_token, encoding="utf-8")
    print(f"Access token saved to: {TOKEN_CACHE_PATH}")
    print("This token is valid until it expires (typically end of trading "
          "day / early next morning). Re-run this flow tomorrow.")

    return access_token


def get_cached_access_token() -> str | None:
    """Read the cached access_token, if one exists. Does NOT validate that
    it's still valid — an expired token will simply fail on first real API
    call, which is the caller's job to handle."""
    if TOKEN_CACHE_PATH.exists():
        return TOKEN_CACHE_PATH.read_text(encoding="utf-8").strip()
    return None


def get_authenticated_kite() -> KiteConnect:
    """
    Convenience: returns a KiteConnect instance with the cached access_token
    already set, ready to make API calls. Raises if no cached token exists
    (meaning you need to run the login/exchange flow first).
    """
    token = get_cached_access_token()
    if not token:
        raise RuntimeError(
            "No cached access_token found. Run:\n"
            "  python src\\data\\kite_auth.py login\n"
            "then log in via the printed URL, then:\n"
            "  python src\\data\\kite_auth.py exchange <request_token>"
        )
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(token)
    return kite


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python kite_auth.py login")
        print("  python kite_auth.py exchange <request_token>")
        print("  python kite_auth.py test        (verify a cached token still works)")
        sys.exit(1)

    command = sys.argv[1]

    if command == "login":
        url = get_login_url()
        print("\nOpen this URL in your browser and log in (2FA required):\n")
        print(url)
        print("\nAfter login, you'll be redirected to your app's redirect URL.")
        print("Copy the 'request_token' value from that URL's query string,")
        print("then run: python src\\data\\kite_auth.py exchange <request_token>")

    elif command == "exchange":
        if len(sys.argv) < 3:
            print("Usage: python kite_auth.py exchange <request_token>")
            sys.exit(1)
        exchange_request_token(sys.argv[2])

    elif command == "test":
        try:
            kite = get_authenticated_kite()
            profile = kite.profile()
            print(f"Token is valid. Logged in as: {profile.get('user_name')} "
                  f"({profile.get('user_id')})")
        except Exception as e:
            print(f"Token test FAILED: {e}")
            print("Your cached token may have expired — run the login/exchange flow again.")
            sys.exit(1)

    else:
        print(f"Unknown command '{command}'. Use: login | exchange | test")
        sys.exit(1)