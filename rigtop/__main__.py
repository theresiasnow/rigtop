"""Allow running as ``python -m rigtop`` or as a PyInstaller bundle."""

import multiprocessing

multiprocessing.freeze_support()  # required for PyInstaller on Windows

from rigtop.cli import main  # noqa: E402

main()
