import os

from bestfit.factory import create_app


def main() -> None:
    app = create_app()
    # Cursor's cloud preview connects to the pod address, not 127.0.0.1.
    # A laptop without BESTFIT_HOST keeps the app on localhost only.
    if os.environ.get("BESTFIT_HOST"):
        host = os.environ["BESTFIT_HOST"]
    elif os.environ.get("CURSOR_AGENT"):
        host = "0.0.0.0"
    else:
        host = "127.0.0.1"
    app.run(
        host=host,
        port=int(os.environ.get("BESTFIT_PORT", "5050")),
        debug=os.environ.get("BESTFIT_DEBUG") == "1",
    )


if __name__ == "__main__":
    main()
