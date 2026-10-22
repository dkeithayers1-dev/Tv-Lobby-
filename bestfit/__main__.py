import os

from bestfit.factory import create_app


def main() -> None:
    app = create_app()
    app.run(
        host=os.environ.get("BESTFIT_HOST", "127.0.0.1"),
        port=int(os.environ.get("BESTFIT_PORT", "5050")),
        debug=os.environ.get("BESTFIT_DEBUG") == "1",
    )


if __name__ == "__main__":
    main()
