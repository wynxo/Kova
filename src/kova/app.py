"""Open Kova's local web client in a native window, or the default browser."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from urllib.parse import quote

from kova.client import KovaError, ensure_server
from kova.config import load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Open Kova on your desktop.")
    parser.add_argument("--browser", action="store_true", help="Use your browser instead of a native window.")
    args = parser.parse_args(argv)
    client = None
    try:
        client = ensure_server(load_config())

        def login_url() -> str:
            # A fragment is not sent to HTTP servers or recorded in access logs.
            return f"{client.base_url}/app#ticket={quote(client.app_ticket(), safe='')}"

        if not args.browser:
            try:
                import webview
            except Exception:
                # Optional GUI packages can fail to import when system libraries are absent.
                pass
            else:
                url = login_url()
                try:
                    webview.create_window("Kova", url, width=1200, height=850,
                                          min_size=(360, 560), background_color="#131419")
                    webview.start(private_mode=True)
                    return
                except Exception:
                    print("Kova: native window unavailable; opening your browser.", file=sys.stderr)
                    # The webview may already have consumed its ticket. Always mint anew.
        if not webbrowser.open(login_url()):
            raise KovaError(None, "No browser could be opened. Check your default browser and run kova-app again.")
    except KovaError as exc:
        message = ("The running server needs a restart for the desktop app. Run kova stop, then kova-app."
                   if exc.status == 404 else str(exc))
        print(f"Kova: {message}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if client is not None:
            client.http.close()


if __name__ == "__main__":
    main()
