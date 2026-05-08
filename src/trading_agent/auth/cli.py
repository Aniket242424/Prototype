"""Console entrypoint for `ta-auth` (re-exports scripts.upstox_auth_cli.main)."""
from scripts.upstox_auth_cli import main  # noqa: F401

if __name__ == "__main__":
    main()
