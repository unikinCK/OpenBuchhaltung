"""Hilfsbefehle des Push-Relays.

``python -m push_relay new-instance-key <name>`` erzeugt einen Instanzschlüssel: Der
Schlüssel kommt als ``PUSH_RELAY_TOKEN`` in die .env der Instanz, der Eintrag
``<name>:<sha256>`` in ``RELAY_INSTANCE_KEYS`` des Relays.
"""

from __future__ import annotations

import re
import secrets
import sys

from push_relay.app import hash_instance_key

USAGE = "Aufruf: python -m push_relay new-instance-key <name>"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "new-instance-key":
        print(USAGE, file=sys.stderr)
        return 2
    name = argv[1]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        print("Name: 1–64 Zeichen aus Buchstaben, Ziffern, . _ -", file=sys.stderr)
        return 2
    key = secrets.token_urlsafe(32)
    print(f"PUSH_RELAY_TOKEN={key}")
    print(f"RELAY_INSTANCE_KEYS-Eintrag: {name}:{hash_instance_key(key)}")
    print("Der Schlüssel wird nur einmal angezeigt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
