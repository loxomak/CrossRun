"""Exception hierarchy used across CrossRun."""

from __future__ import annotations


class CrossRunError(RuntimeError):
    """Base class for all expected CrossRun failures."""


class UnsupportedHostError(CrossRunError):
    """The machine cannot translate AMD64 binaries (for example x86 or armhf)."""


class MissingBackendError(CrossRunError):
    """No usable AMD64 translation backend is installed."""


class UnsupportedPayloadError(CrossRunError):
    """The selected file/directory is not a runnable AMD64 payload."""


class ExtractionError(CrossRunError):
    """An AppImage could not be unpacked."""


class AppNotFoundError(CrossRunError):
    """No registered application matches the given id or name."""


class AmbiguousAppError(CrossRunError):
    """Several registered applications share the requested name."""


class AppExistsError(CrossRunError):
    """The payload is already registered."""


class RegistryError(CrossRunError):
    """The registry file is unreadable or corrupt."""


class SetupError(CrossRunError):
    """A setup/installation step failed."""


class CommandError(CrossRunError):
    """A subprocess exited unsuccessfully or could not be started."""
