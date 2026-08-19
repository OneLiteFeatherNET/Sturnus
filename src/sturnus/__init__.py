"""Sturnus — Discord voice transcription with Outline document output."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sturnus")
except PackageNotFoundError:  # pragma: no cover - package not installed
    __version__ = "0.0.0+unknown"
