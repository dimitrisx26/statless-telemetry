"""statless-telemetry collector: privacy-first usage telemetry for developer CLIs."""

from importlib import metadata

__all__ = ["__version__"]

try:
    __version__ = metadata.version("statless-telemetry-collector")
except metadata.PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0.dev0"
