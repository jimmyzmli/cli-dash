"""CLI entry point for cli-dash."""

import sys
from cli_dash.server import DashServer, AppConfig


def main():
    server = DashServer(AppConfig())
    server.run()


if __name__ == "__main__":
    main()
