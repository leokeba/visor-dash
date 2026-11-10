"""Entry point for the packaged app (PyInstaller needs a plain script, not a package __main__)."""
import sys

from visor_dash.cli import main

if __name__ == "__main__":
    sys.exit(main())
