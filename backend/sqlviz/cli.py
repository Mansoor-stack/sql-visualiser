from __future__ import annotations

import argparse
import ipaddress
import os
import sys

from . import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sql-visualiser", description="Step-by-step MySQL query visualiser.")
    parser.add_argument("--host", default=os.getenv("SQLVIZ_HOST", "127.0.0.1"), help="Bind address (default: 127.0.0.1, env SQLVIZ_HOST)")
    parser.add_argument("--port", type=int, default=int(os.getenv("SQLVIZ_PORT", "8080")), help="Port (default: 8080, env SQLVIZ_PORT)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    from .settings import ConfigError, ai, limits, mysql
    try:
        mysql(), ai(), limits()
    except ConfigError as error:
        sys.exit(f"Configuration error: {error}")

    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        print(
            f"Warning: binding to {args.host}. SQL Visualiser is a single-user tool without authentication; "
            "only do this inside a container or on a trusted machine.",
            file=sys.stderr,
        )

    import uvicorn

    print(f"SQL Visualiser running at http://localhost:{args.port}", file=sys.stderr)
    uvicorn.run("sqlviz.api:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
