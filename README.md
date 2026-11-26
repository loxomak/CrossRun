# CrossRun

**Run AMD64 (x86-64) Linux applications on your 64-bit ARM Raspberry Pi.**

CrossRun is a small application manager and launcher for Raspberry Pi OS. It
installs and configures the translation layer, keeps a library of your x86-64
programs, unpacks AppImages natively, and starts everything with the right
environment. It ships with both a command line tool and a GTK3 desktop app.

> CrossRun does **not** recompile AMD64 machine code into ARM64 ahead of time.
> It drives two proven userspace translation layers:
>
> * **[Box64](https://github.com/ptitSeb/box64)** – an x86-64 dynamic recompiler
>   for ARM64, usually the fastest option for Linux applications.
> * **[QEMU user-mode](https://www.qemu.org/)** – instruction-level emulation,
>   the most compatible fallback.
>
> Expect performance well below native. Simple tools work nicely on a Pi 4/5;
> large games and GPU-heavy applications often do not.

## Features

* `crossrun doctor` – checks the OS, Pi model, backends, binfmt, libraries and
  the application library.
* One-command setup – builds Box64 from source (with Pi 4/Pi 5 tuning), installs
  `qemu-user-static`, registers the x86-64 binfmt handler, and fetches x86-64
  runtime libraries.
* A **private, rootless x86-64 sysroot** – no foreign packages are installed
  system-wide, so Raspberry Pi OS's arm64 packages stay untouched.
* Import static or dynamically linked AMD64 ELF binaries, type 2 AppImages and
  extracted application directories.
* Native AppImage extraction with `unsquashfs`; the x86-64 runtime never has to
  execute just to unpack the payload.
* GTK3 interface: add, run, run in a terminal, remove, setup and doctor.
* `crossrun selftest` – verifies translation with a built-in, hand-assembled
  static AMD64 ELF; no downloads or extra test files required.
* Pure Python standard library; the GUI additionally uses PyGObject, which is
  preinstalled on Raspberry Pi OS.

## Requirements

| Item | Requirement |
| --- | --- |
| Hardware | Raspberry Pi 4 or 5 recommended (Pi 3 works for light apps; Pi 5 is fastest) |
| OS | 64-bit Raspberry Pi OS (Bookworm/Trixie) or another Debian/Ubuntu **arm64** system |
| Software | Python 3.9+, `sudo` for setup; `python3-gi` + `gir1.2-gtk-3.0` for the GUI |
| Disk | ~2 GB free for the Box64 source/build tree and the private sysroot |
| Payload | 64-bit x86-64 **Linux** programs only |

A 32-bit (`armhf`) OS cannot run CrossRun's ARM64 backends. 32-bit x86 Windows
or Linux programs are not supported (that would need Box86), and Windows `.exe`
files need Wine, which is out of scope.

## Install

```bash
cd crossrun
./install.sh
```

The installer writes `crossrun` and `crossrun-gui` wrappers into
`~/.local/bin`, installs a desktop entry and icon, and prints the next steps.
Ensure `~/.local/bin` is on your `PATH`. The wrappers reference this checkout,
so rerun `./install.sh` if you move the project folder.

You can also run it without installing:

```bash
python3 -m crossrun --help
```

or install it as a Python package in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/crossrun --help
```

## Quick start

```bash
# 1. See what is available on this machine
crossrun doctor

# 2. Install Box64 + QEMU user-mode and the x86-64 runtime libraries
crossrun setup --backend both --with-libs

# 3. Prove that translation works
crossrun selftest

# 4. Import an AMD64 program, AppImage or extracted folder
crossrun add ~/Downloads/Tool-x86_64.AppImage
crossrun add ./game/game --name Game

# 5. Run it
crossrun list
crossrun run Game

# Or use the desktop interface
crossrun gui
```

`setup` is the only command that needs administrator rights. CrossRun invokes
`sudo` for the individual apt/dpkg/systemctl/cmake-install steps and shows every
command before it runs. Use `--dry-run` to inspect the plan without changing
anything.

### What setup actually does

`crossrun setup --backend both --with-libs`:

1. Enables the `amd64` package architecture in apt (so package lists for amd64
   can be downloaded; no amd64 package is installed into the system).
2. `apt-get update`, then installs `squashfs-tools`, `git`, `cmake`,
   `build-essential` and `qemu-user-static`.
3. Clones and builds Box64 with `-DARM_DYNAREC=ON` and, on a Pi 4/5, the
   matching `-DRPI4ARM64=ON` / `-DRPI5ARM64=ON` tuning flags; installs it to
   `/usr/local`.
4. Restarts `systemd-binfmt` so the `qemu-x86_64` handler is active.
5. Downloads the amd64 `libc6`, `libstdc++6`, `libgcc-s1` and `zlib1g` `.deb`
   files and unpacks them into `~/.local/share/crossrun/sysroot`.

Box64 is built from source because upstream does not publish a Debian package.
On a Pi 5 this takes roughly 10-20 minutes; a Pi 4 takes longer. The build is
incremental, so retrying after an interruption resumes where it stopped.

## Command reference

```
crossrun doctor [--json]
crossrun setup [--backend both|box64|qemu] [--with-libs] [--jobs N] [--yes] [--dry-run]
crossrun add PATH [--name NAME] [--backend auto|box64|qemu] [--entry REL] [--arg ARG]... [--env KEY=VALUE]...
crossrun list [--json]
crossrun info APP [--json]
crossrun remove APP [--yes]
crossrun run APP [--backend box64|qemu|auto] [--dry-run] [-- ARGS...]
crossrun exec BINARY [--backend box64|qemu|auto] [--dry-run] [-- ARGS...]
crossrun selftest [--backend box64|qemu|auto] [--json]
crossrun libs [PACKAGE ...] [--dry-run]
crossrun gui
```

Options must precede `APP`/`BINARY`, because everything after those positionals
is passed to the application. Use `--` to separate your program's arguments:

```bash
crossrun run --backend qemu Game -- --fullscreen
crossrun exec ./hello -- arg1 arg2
```

`auto` (the default) prefers Box64 when installed and falls back to QEMU. Each
library entry remembers its preferred backend; `run --backend` overrides it for
one launch. `crossrun run --dry-run Game` prints the exact command, including
the environment variables CrossRun would set.

### Adding programs

* **ELF binary** – must be 64-bit x86-64. Static and dynamically linked
  binaries (including PIE) are accepted. The file is used in place, so keep it
  where it is.
* **AppImage** – type 2 AppImages are unpacked with `unsquashfs` into
  `~/.local/share/crossrun/apps/<id>/AppDir`. The AppRun wrapper is usually a
  shell script that expects transparent x86 execution; CrossRun therefore looks
  for the real AMD64 executable inside the AppDir and launches that directly.
  Libraries shipped in the AppDir are exposed through
  `BOX64_LD_LIBRARY_PATH` when using Box64. Wrapper-specific environment tweaks
  are skipped.
* **Directory** – an extracted application tree. CrossRun uses `AppRun` if it is
  an AMD64 ELF, otherwise the only executable AMD64 ELF it can find. If there
  are several candidates, choose one with `--entry usr/bin/app`.

`crossrun add` refuses ARM64 and 32-bit x86 binaries with an explanation. The
same source path cannot be imported twice; to re-import after moving a file,
remove the old entry first.

Some applications rely on environment variables that their launcher script
would normally set. Use `--env` (repeatable) to store them with the entry:

```bash
crossrun add ./MyJavaApp --name MyApp --entry jdk/bin/java \
  --env 'CLASSPATH=./lib/app.jar:./lib/*' \
  --arg=--add-opens=java.base/java.lang=ALL-UNNAMED \
  --arg com.example.Main
```

Note the `--arg=...` form: argparse would otherwise treat a value beginning
with a dash as another option.

### Extra x86-64 libraries

Many applications need more than the base runtime. Download additional Debian
amd64 packages into the private sysroot without touching the system:

```bash
crossrun libs libgl1 libx11-6 libxext6 libfontconfig1
```

The same mechanism is used for the base libraries. If the private sysroot does
not exist yet, CrossRun bootstraps the base runtime automatically before adding
the requested packages. `--dry-run` shows the exact `apt-get download` and
`dpkg-deb -x` commands.

## Desktop application

`crossrun gui` (or the CrossRun entry in the application menu) opens:

* a status banner showing the detected backends and versions;
* a library list with **Add...**, **Add Folder...**, **Run**, **Run in
  Terminal**, **Remove** and **Refresh**;
* **Setup**, which opens a terminal running the same `crossrun setup` command so
  you can type your password and watch progress;
* a **Doctor** window with the full environment report.

Double-clicking an application runs it. "Run in Terminal" is useful for console
programs and for reading error output.

## Where files live

| Path | Contents |
| --- | --- |
| `~/.local/share/crossrun/apps.json` | application library (mode 0600) |
| `~/.local/share/crossrun/apps/<id>/AppDir` | extracted AppImages |
| `~/.local/share/crossrun/sysroot` | private x86-64 libraries |
| `~/.local/share/crossrun/qemu-prefix` | symlink shim when host multiarch libs exist |
| `~/.cache/crossrun/src/box64` | Box64 source and build tree |
| `~/.cache/crossrun/debs` | downloaded amd64 `.deb` files |

Set `XDG_DATA_HOME` / `XDG_CACHE_HOME` to relocate them.

## How a launch is assembled

Box64:

```bash
BOX64_NOBANNER=1 \
BOX64_LD_LIBRARY_PATH=<app libs>:<sysroot libs> \
box64 /path/to/application <args>
```

QEMU (when no system-wide x86-64 loader is available):

```bash
QEMU_LD_PREFIX=~/.local/share/crossrun/sysroot \
qemu-x86_64-static /path/to/application <args>
```

QEMU's prefix mode also rewrites other absolute paths the application opens, so
configuration files under `/etc` may not resolve. When a real multiarch loader
is present at `/lib64/ld-linux-x86-64.so.2`, CrossRun omits the prefix and lets
the system libraries load normally. The `qemu-x86_64` binfmt handler installed
by `qemu-user-static` additionally allows static AMD64 binaries (and AppRun
scripts' inner calls, when system libraries are present) to start transparently.

## Why a private sysroot?

The obvious approach on Debian is:

```bash
sudo dpkg --add-architecture amd64
sudo apt-get install libc6:amd64
```

On Raspberry Pi OS this often fails. The arm64 libc is built by the Raspberry Pi
project and carries a `+rpt` version suffix (for example
`2.41-12+rpt1+deb13u4`), while the amd64 libc comes from Debian
(`2.41-12+deb13u5`). glibc requires exactly matching versions across
architectures and declares `Breaks: libc6:amd64 (!= ...)`, so apt refuses:

```
libc6:arm64 ... Breaks libc6:amd64 (!= 2.41-12+rpt1+deb13u4)
```

CrossRun avoids the conflict completely: it only downloads the amd64 `.deb`
files and unpacks them under `~/.local/share/crossrun/sysroot`. QEMU is pointed
there with `QEMU_LD_PREFIX`, and Box64 receives those directories through
`BOX64_LD_LIBRARY_PATH`. Your system's arm64 libraries and package database are
never modified, and the operation does not need root.

## Limitations and expectations

* **Performance**: instructions are translated at runtime. CPU-heavy programs
  can be several times slower than on a PC. A Pi 5 is strongly recommended; a
  Pi 4 works for smaller tools; a Pi 3 is only suitable for simple programs.
* **Graphics**: x86 OpenGL/Vulkan drivers cannot run on ARM directly. Box64 can
  wrap the host's GL libraries for some applications, but many 3D games and
  Electron/Chromium apps will fail or render incorrectly. This is a limitation
  of the translation layers, not something a launcher can fix.
* **AppImage AppRun wrappers** are skipped. They are usually shell scripts that
  rely on transparent x86 execution; CrossRun launches the real bundled binary
  instead. Environment tweaks made by AppRun may be lost.
* **QEMU prefix mode** redirects absolute paths; programs that insist on
  `/etc/...` configuration may not find it.
* **Kernel modules, services, anti-cheat, DRM and hardware-specific drivers**
  are not supported. Windows `.exe` files need Wine (not managed here), and
  32-bit x86 needs Box86 (not managed here).
* **No sandbox**: translated programs run with your user's permissions, exactly
  like native programs.

## Troubleshooting

Start with:

```bash
crossrun doctor
crossrun selftest
```

* **"no AMD64 translation backend is installed"** – run
  `crossrun setup --backend both --with-libs`.
* **Box64 not found after setup** – the source build failed. Re-run
  `crossrun setup --backend box64` and read the compiler output. The source tree
  is cached in `~/.cache/crossrun/src/box64`, so retries are incremental.
* **`Could not open '/lib64/ld-linux-x86-64.so.2'`** – the x86-64 runtime is
  missing or incomplete. Run `crossrun setup --with-libs` (it uses the private
  sysroot; do not try `apt-get install libc6:amd64` on Raspberry Pi OS).
* **AppImage says "unsquashfs is required"** – install `squashfs-tools`:
  `sudo apt-get install squashfs-tools`.
* **"not a type 2 AppImage"** – type 1 AppImages are not supported. Ask the
  vendor for a type 2 build, or extract it on an x86 machine and add the folder.
* **"Exec format error" when running a binary directly** – launch it through
  CrossRun (`crossrun exec ./binary` or `crossrun run NAME`) instead of
  executing it in a shell. The binfmt handler only helps when a suitable guest
  loader is visible.
* **Application crashes or a blank window** – try the other backend:
  `crossrun run --backend qemu NAME`, then look at
  `crossrun run --dry-run NAME` and run that command in a terminal to see the
  application's own error messages.
* **Missing shared library** – add the matching amd64 package, e.g.
  `crossrun libs libgl1 libx11-6`. Package names are Debian amd64 package names
  without the `:amd64` suffix.
* **AppImage imported but nothing happens** – the wrong executable may have been
  chosen. Re-add the extracted folder with an explicit entrypoint:
  `crossrun add ~/.local/share/crossrun/apps/<id>/AppDir --entry usr/bin/program`.
* **Permission denied** – `chmod +x` the binary, and avoid `noexec` mounts.
* **Corrupt library file** – inspect `~/.local/share/crossrun/apps.json`; it is
  plain JSON and mode 0600. Remove the file to start with an empty library.

## Development

Validated on a Raspberry Pi 5 Model B (8 GB) running Debian 13 trixie with
QEMU user-mode 10.0.13 and Box64 built from source: the static self-test, a
dynamically linked AMD64 `hello`, and a real `appimagetool` AppImage all ran
successfully.

```
crossrun/
├── crossrun/
│   ├── cli.py            argparse command line interface
│   ├── gui.py            GTK3 interface
│   └── core/
│       ├── apps.py       import, AppImage extraction, launch plans
│       ├── backends.py   Box64/QEMU detection and command construction
│       ├── doctor.py     environment checks
│       ├── elf.py        ELF/AppImage parsing without external tools
│       ├── installer.py  setup plans and binfmt inspection
│       ├── platform.py   Pi model, distro and data directories
│       ├── registry.py   atomic JSON application library
│       ├── runner.py     dry-run/streaming subprocess execution
│       ├── runtime.py    private amd64 sysroot from .deb files
│       └── selftest.py   generated static AMD64 test program
├── tests/                unittest suite (no third-party dependencies)
├── assets/               desktop entry and icon
└── install.sh            per-user installation
```

Run the test suite from the project root:

```bash
python3 -m unittest discover -v -s tests -t .
```

The 70+ tests use synthetic ELF/AppImage files, temporary registries, fake
`apt-get`/`dpkg-deb`/backend scripts and dry-run runners, so they need neither a
Raspberry Pi nor network access. A real end-to-end check is:

```bash
crossrun selftest --backend qemu
crossrun selftest --backend box64
crossrun libs --dry-run
```

The self-test does not download anything: CrossRun hand-assembles a minimal
x86-64 ELF that issues `write(2)` and `exit(2)` syscalls directly, then runs it
through the selected backend.

To add a backend, implement the `Backend` interface and a `BackendInfo`
detector in `crossrun/core/backends.py`, teach `backend_for` about it, and add
tests for the generated command line.

## Security notes

* `setup` prints every privileged command it runs. Review them first with
  `--dry-run`; CrossRun never hides `sudo` behind a GUI you cannot audit.
* The GUI hands privileged setup work to a terminal, so you see the password
  prompt and the live output.
* No sandbox or container is used. Only run AMD64 programs you trust.
* AppImages are parsed with `unsquashfs`; keep `squashfs-tools` updated.
* The application library is written atomically with mode 0600.

## Uninstall

```bash
crossrun list                 # remove entries you no longer need
crossrun remove --yes NAME
rm -rf ~/.local/share/crossrun ~/.cache/crossrun
rm -f ~/.local/bin/crossrun ~/.local/bin/crossrun-gui \
      ~/.local/share/applications/crossrun.desktop \
      ~/.local/share/icons/hicolor/scalable/apps/crossrun.svg
sudo rm -f /usr/local/bin/box64
sudo apt-get remove --purge qemu-user-static squashfs-tools
sudo dpkg --remove-architecture amd64   # only if you enabled it solely for CrossRun
```

Removing `qemu-user-static` also removes its binfmt handlers; do not do that if
other software relies on them.

## License

MIT. See `LICENSE`.
