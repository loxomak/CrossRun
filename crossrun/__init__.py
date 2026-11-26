"""CrossRun - run AMD64 (x86-64) Linux applications on ARM64 Raspberry Pi.

CrossRun does not recompile binaries ahead of time.  It configures and drives
the two proven userspace translation layers for ARM64 Linux:

* Box64  - x86-64 dynamic recompiler, best performance for most applications.
* QEMU   - user-mode emulation with Debian multiarch x86-64 libraries, the
           most compatible fallback.

The package intentionally depends only on the Python standard library.  The
optional GTK3 user interface uses PyGObject, which ships with Raspberry Pi OS.
"""

from __future__ import annotations

__version__ = "0.1.0"
APP_NAME = "CrossRun"
TAGLINE = "Run AMD64 applications on your Raspberry Pi"

__all__ = ["APP_NAME", "TAGLINE", "__version__"]
