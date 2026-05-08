"""Compile all .proto files under src/trading_agent/market_data/protos/."""
from __future__ import annotations

import sys
from pathlib import Path

import click


@click.command()
def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    proto_dir = repo_root / "src" / "trading_agent" / "market_data" / "protos"
    if not proto_dir.exists():
        click.echo(f"Proto dir missing: {proto_dir}")
        sys.exit(1)

    protos = list(proto_dir.glob("*.proto"))
    if not protos:
        click.echo("No .proto files found.")
        sys.exit(1)

    from grpc_tools import protoc  # type: ignore[import-not-found]

    for p in protos:
        click.echo(f"Compiling {p.name}...")
        argv = [
            "protoc",
            f"-I{proto_dir}",
            f"--python_out={proto_dir}",
            str(p),
        ]
        rc = protoc.main(argv)
        if rc != 0:
            click.echo(click.style(f"[FAIL] {p.name} (rc={rc})", fg="red"))
            sys.exit(rc)
        click.echo(click.style(f"[OK] {p.name}", fg="green"))


if __name__ == "__main__":
    main()
