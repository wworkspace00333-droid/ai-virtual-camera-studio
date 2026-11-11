"""PyInstaller entry point for the packaged application."""
import sys

from vcstudio.app.main import main

if __name__ == "__main__":
    sys.exit(main())
