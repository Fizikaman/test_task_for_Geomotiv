"""Local API entry point; importing the package does not load the server."""

lazy import uvicorn

from article_graph.app import create_app


def main() -> None:
    # Configuration and router registration must succeed before serving HTTP.
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
