"""Part 0: the CLI chat. Run from the repository root:

    PYTHONPATH=src python -m services.chat.cli --mandate mandate_a

The token comes from LANTERN_SESSION_TOKEN or a hidden prompt, never argv
(argv shows up in shell history and the process list). /mandate switching
is refused: one session, one mandate. /quit to leave."""
import argparse
import getpass
import os
import sys

from .session import ChatSession, load_world


def main(argv=None, inp=input, out=print):
    ap = argparse.ArgumentParser(description="Credit-watch chat (one mandate per session)")
    ap.add_argument("--principal", default=None)
    ap.add_argument("--mandate", required=True)
    a = ap.parse_args(argv)
    world = load_world()
    principal = a.principal or world["P"]["agent"]["principal"]
    token = os.environ.get("LANTERN_SESSION_TOKEN") or getpass.getpass("session token: ")
    try:
        chat = ChatSession(principal, token, a.mandate, world)
    except PermissionError as e:
        out(f"login refused: {e}")
        return 2
    out(f"signed in as {principal}, bound to {chat.mandate}. /quit to leave.")
    while True:
        try:
            line = inp(f"{chat.mandate}> ").strip()
        except EOFError:
            break
        if line in ("/quit", "/exit"):
            break
        if line.startswith("/mandate"):
            out("a session is bound to one mandate: log in again to switch.")
            continue
        if line:
            reply, _ = chat.ask(line)
            out(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
