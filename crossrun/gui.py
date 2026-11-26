"""GTK3 desktop interface for CrossRun.

The GUI is intentionally thin: it uses the same core modules as the CLI, so a
launch from the desktop and a launch from the shell produce identical plans.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk, Pango  # noqa: E402

from . import APP_NAME, TAGLINE, __version__  # noqa: E402
from .core.apps import build_plan, import_app, spawn_plan  # noqa: E402
from .core.backends import detect_backends  # noqa: E402
from .core.doctor import run_doctor  # noqa: E402
from .core.platform import (  # noqa: E402
    data_dir,
    detect_platform,
    find_terminal,
    registry_path,
    terminal_command,
)
from .core.registry import AppEntry, Registry  # noqa: E402
from .core.runner import Runner  # noqa: E402
from .errors import CrossRunError, RegistryError  # noqa: E402

KIND_LABELS = {
    "elf": "ELF binary",
    "appimage": "AppImage",
    "directory": "Directory",
}


class CrossRunWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(
            application=application,
            title=f"{APP_NAME} - {TAGLINE}",
        )
        self.set_default_size(1000, 640)
        self.registry = Registry(registry_path())
        self.platform = detect_platform()
        self._entries: dict[str, AppEntry] = {}
        self._infos = []
        self._build_ui()
        self.refresh()

    # -- construction ----------------------------------------------------
    def _build_ui(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = APP_NAME
        header.props.subtitle = self.platform.describe()
        self.set_titlebar(header)

        setup_button = Gtk.Button(label="Setup")
        setup_button.set_tooltip_text("Install Box64, QEMU user-mode and amd64 libraries")
        setup_button.connect("clicked", self.on_setup)
        header.pack_start(setup_button)

        doctor_button = Gtk.Button.new_from_icon_name(
            "dialog-information-symbolic", Gtk.IconSize.BUTTON
        )
        doctor_button.set_tooltip_text("Run the environment doctor")
        doctor_button.connect("clicked", self.on_doctor)
        header.pack_end(doctor_button)

        menu_button = Gtk.MenuButton()
        menu_button.add(
            Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
        )
        popover = Gtk.Popover()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        menu_box.set_border_width(6)
        for label, callback in (
            ("About CrossRun", self.on_about),
            ("Quit", self.on_quit),
        ):
            button = Gtk.Button(label=label)
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.connect("clicked", callback)
            menu_box.add(button)
        menu_box.show_all()
        popover.add(menu_box)
        menu_button.set_popover(popover)
        header.pack_end(menu_button)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        self.infobar = Gtk.InfoBar()
        self.infobar.set_show_close_button(False)
        self.info_label = Gtk.Label(xalign=0)
        self.info_label.set_line_wrap(True)
        self.info_label.set_margin_start(6)
        self.info_label.set_margin_end(6)
        self.infobar.get_content_area().add(self.info_label)
        self.setup_action = self.infobar.add_button("Set up now", Gtk.ResponseType.OK)
        self.setup_action.set_no_show_all(True)
        self.infobar.connect("response", self.on_setup)
        outer.pack_start(self.infobar, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_visible(True)
        for title, index in (("Application", 1), ("Type", 2), ("Backend", 3)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(index == 1)
            self.view.append_column(column)
        self.view.connect("row-activated", lambda *_: self.on_run(None))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        outer.pack_start(scroller, True, True, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_border_width(8)
        self.add_button = Gtk.Button(label="Add...")
        self.add_button.connect("clicked", self.on_add)
        self.add_dir_button = Gtk.Button(label="Add Folder...")
        self.add_dir_button.connect("clicked", self.on_add_folder)
        self.run_button = Gtk.Button(label="Run")
        self.run_button.connect("clicked", self.on_run)
        self.terminal_button = Gtk.Button(label="Run in Terminal")
        self.terminal_button.connect("clicked", self.on_run_in_terminal)
        self.remove_button = Gtk.Button(label="Remove")
        self.remove_button.connect("clicked", self.on_remove)
        refresh_button = Gtk.Button(label="Refresh")
        refresh_button.connect("clicked", lambda *_: self.refresh())
        for button in (
            self.add_button,
            self.add_dir_button,
            self.run_button,
            self.terminal_button,
            self.remove_button,
            refresh_button,
        ):
            actions.pack_start(button, False, False, 0)

        self.status_label = Gtk.Label(xalign=1)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        actions.pack_end(self.status_label, True, True, 0)
        outer.pack_start(actions, False, False, 0)

        self.view.get_selection().connect("changed", lambda *_: self._update_sensitivity())
        self._update_sensitivity()

    # -- state -----------------------------------------------------------
    def refresh(self) -> None:
        try:
            self.registry.reload()
            entries = self.registry.apps
        except RegistryError as exc:
            entries = []
            self._error("Cannot read the application library", str(exc))
        self.store.clear()
        self._entries.clear()
        for entry in entries:
            self._entries[entry.id] = entry
            self.store.append(
                [
                    entry.id,
                    entry.name,
                    KIND_LABELS.get(entry.kind, entry.kind),
                    entry.backend,
                ]
            )
        if entries:
            self.status_label.set_text(
                f"{len(entries)} application(s) - {self.registry.path}"
            )
        else:
            self.status_label.set_text("No applications yet. Use Add... to import one.")
        self._update_banner()
        self._update_sensitivity()

    def _update_banner(self) -> None:
        self._infos = detect_backends()
        installed = [info for info in self._infos if info.available]
        needs_setup = False
        if not self.platform.is_arm64:
            self.infobar.set_message_type(Gtk.MessageType.ERROR)
            self.info_label.set_text(
                f"This host is {self.platform.machine}, not ARM64. Applications can "
                "be managed here but not run on this machine."
            )
        elif installed:
            self.infobar.set_message_type(Gtk.MessageType.INFO)
            versions = ", ".join(
                f"{info.label} ({info.version})" if info.version else info.label
                for info in installed
            )
            self.info_label.set_text(f"Ready - {versions}")
        else:
            self.infobar.set_message_type(Gtk.MessageType.WARNING)
            self.info_label.set_text(
                "No AMD64 translation backend is installed yet. Run Setup to install "
                "Box64 and QEMU."
            )
            needs_setup = True
        self.infobar.show_all()
        # show_all() would reveal the action button again, so set it afterwards.
        self.setup_action.set_visible(needs_setup)

    def _update_sensitivity(self) -> None:
        selected = self._selected_entry() is not None
        for button in (self.run_button, self.terminal_button, self.remove_button):
            button.set_sensitive(selected)

    def _selected_entry(self):
        model, treeiter = self.view.get_selection().get_selected()
        if treeiter is None:
            return None
        return self._entries.get(model[treeiter][0])

    # -- launching -------------------------------------------------------
    def on_run(self, _button) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        try:
            plan = build_plan(entry, data_root=data_dir())
            spawn_plan(plan)
        except CrossRunError as exc:
            self._error("Could not start the application", str(exc))
            return
        self.status_label.set_text(f"Started {entry.name} with {plan.backend}")
        if plan.notes:
            self._info("Launch notes", "\n".join(plan.notes))

    def on_run_in_terminal(self, _button) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        try:
            plan = build_plan(entry, data_root=data_dir())
        except CrossRunError as exc:
            self._error("Could not start the application", str(exc))
            return
        terminal = find_terminal()
        if terminal is None:
            self._info(
                "No terminal emulator found",
                "Launching without a terminal instead.\n\n" + "\n".join(plan.notes),
            )
            try:
                spawn_plan(plan)
            except CrossRunError as exc:
                self._error("Could not start the application", str(exc))
            return
        cwd = shlex.quote(plan.cwd or str(Path.home()))
        subprocess.Popen(
            terminal_command(terminal, f"cd {cwd} && exec env {plan.command()}")
        )
        self.status_label.set_text(f"Started {entry.name} in a terminal")

    # -- library management ----------------------------------------------
    def on_add(self, _button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Choose an AMD64 binary or AppImage",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL, "Choose", Gtk.ResponseType.ACCEPT
        )
        file_filter = Gtk.FileFilter()
        file_filter.set_name("AMD64 programs (*.AppImage, *.run, ELF binaries)")
        for pattern in ("*.AppImage", "*.appimage", "*.run"):
            file_filter.add_pattern(pattern)
        chooser.add_filter(file_filter)
        everything = Gtk.FileFilter()
        everything.set_name("All files")
        everything.add_pattern("*")
        chooser.add_filter(everything)
        response = chooser.run()
        filename = chooser.get_filename()
        chooser.destroy()
        if response == Gtk.ResponseType.ACCEPT and filename:
            self._import(filename, Path(filename).name)

    def on_add_folder(self, _button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Choose an extracted AMD64 application folder",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL, "Choose", Gtk.ResponseType.ACCEPT
        )
        response = chooser.run()
        filename = chooser.get_filename()
        chooser.destroy()
        if response == Gtk.ResponseType.ACCEPT and filename:
            self._import(filename, Path(filename).name)

    def _import(self, source: str, default_name: str) -> None:
        options = self._ask_import_options(default_name)
        if options is None:
            return
        name, backend, entry_path = options
        try:
            result = import_app(
                source,
                self.registry,
                name=name or None,
                backend=backend,
                entry=entry_path or None,
                runner=Runner(echo=lambda line: None),
            )
        except CrossRunError as exc:
            self._error("Could not import the application", str(exc))
            return
        self.refresh()
        self.status_label.set_text(f"Added {result.entry.name}")
        if result.warnings:
            self._info(f"Imported {result.entry.name}", "\n".join(result.warnings))

    def on_remove(self, _button) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Remove '{entry.name}' from the library?",
        )
        dialog.format_secondary_text(
            "The original payload is kept. Extracted AppImage data is deleted."
        )
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return
        try:
            self.registry.remove(entry.id)
        except CrossRunError as exc:
            self._error("Could not remove the application", str(exc))
            return
        if entry.kind == "appimage":
            extracted = data_dir() / "apps" / entry.id
            if extracted.exists():
                shutil.rmtree(extracted, ignore_errors=True)
        self.refresh()
        self.status_label.set_text(f"Removed {entry.name}")

    # -- dialogs ---------------------------------------------------------
    def _ask_import_options(self, default_name: str):
        stem = default_name
        for suffix in (".appimage", ".AppImage", ".run"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        dialog = Gtk.Dialog(title="Import options", transient_for=self, modal=True)
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL, "Import", Gtk.ResponseType.OK
        )
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.set_border_width(12)
        dialog.get_content_area().add(grid)

        name_entry = Gtk.Entry()
        name_entry.set_text(stem)
        name_entry.set_hexpand(True)
        backend_combo = Gtk.ComboBoxText()
        for key in ("auto", "box64", "qemu"):
            backend_combo.append_text(key)
        backend_combo.set_active(0)
        entry_entry = Gtk.Entry()
        entry_entry.set_placeholder_text("optional, e.g. usr/bin/app")
        entry_entry.set_hexpand(True)

        for row, (label, widget) in enumerate(
            (
                ("Name", name_entry),
                ("Backend", backend_combo),
                ("Entrypoint", entry_entry),
            )
        ):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)

        dialog.show_all()
        response = dialog.run()
        values = (
            name_entry.get_text().strip(),
            backend_combo.get_active_text() or "auto",
            entry_entry.get_text().strip(),
        )
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        return values

    def on_doctor(self, _button) -> None:
        report = run_doctor()
        dialog = Gtk.Dialog(title="CrossRun doctor", transient_for=self, modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(780, 540)
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        view.set_left_margin(8)
        view.set_top_margin(8)
        view.get_buffer().set_text(report.render())
        scroller = Gtk.ScrolledWindow()
        scroller.add(view)
        dialog.get_content_area().add(scroller)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_setup(self, _widget, _response_id=None) -> None:
        dialog = SetupDialog(self)
        dialog.run()
        dialog.destroy()
        self.refresh()

    def on_about(self, _button) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(APP_NAME)
        dialog.set_version(__version__)
        dialog.set_comments(
            TAGLINE
            + "\n\nBox64 and QEMU user-mode perform the translation. CrossRun "
            "installs, configures and launches them."
        )
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.set_logo_icon_name("application-x-executable")
        dialog.run()
        dialog.destroy()

    def on_quit(self, _button) -> None:
        application = self.get_application()
        if application is not None:
            application.quit()
        else:  # pragma: no cover - defensive
            self.destroy()

    def _info(self, title: str, body: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(body)
        dialog.run()
        dialog.destroy()

    def _error(self, title: str, body: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(body)
        dialog.run()
        dialog.destroy()


class SetupDialog(Gtk.Dialog):
    """Explain the installation and hand the privileged work to a terminal."""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(
            title="Install AMD64 translation support",
            transient_for=parent,
            modal=True,
        )
        self.add_buttons("Close", Gtk.ResponseType.CLOSE)
        self.set_default_size(620, 380)

        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(10)

        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>Install translation backends</b>")
        area.add(heading)

        description = Gtk.Label(xalign=0)
        description.set_line_wrap(True)
        description.set_text(
            "CrossRun installs Box64 (a fast x86-64 dynamic recompiler) and QEMU "
            "user-mode (the most compatible fallback). The installer opens in a "
            "terminal so you can enter your administrator password and follow the "
            "progress."
        )
        area.add(description)

        self.box64 = Gtk.CheckButton(label="Box64 - recommended for performance")
        self.box64.set_active(True)
        area.add(self.box64)
        self.qemu = Gtk.CheckButton(label="QEMU user-mode - maximum compatibility")
        self.qemu.set_active(True)
        area.add(self.qemu)
        self.libs = Gtk.CheckButton(
            label="Install amd64 runtime libraries (recommended)"
        )
        self.libs.set_active(True)
        area.add(self.libs)

        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        note.set_text(
            "Box64 is compiled from source, which can take 10-20 minutes on a "
            "Raspberry Pi 5 (longer on a Pi 4). The amd64 runtime libraries are "
            "unpacked into CrossRun's private sysroot; no system package is changed."
        )
        area.add(note)

        launch = Gtk.Button(label="Open installer in a terminal")
        launch.get_style_context().add_class("suggested-action")
        launch.connect("clicked", self._launch)
        area.add(launch)
        self.show_all()

    def _launch(self, _button) -> None:
        choices = []
        if self.box64.get_active():
            choices.append("box64")
        if self.qemu.get_active():
            choices.append("qemu")
        if not choices:
            self._alert("Select at least one backend.")
            return
        backend = "both" if len(choices) == 2 else choices[0]
        command = [sys.executable, "-m", "crossrun", "setup", "--backend", backend]
        if self.libs.get_active():
            command.append("--with-libs")
        project_root = str(Path(__file__).resolve().parents[1])
        shell = (
            f"export PYTHONPATH={shlex.quote(project_root)}${{PYTHONPATH:+:$PYTHONPATH}}; "
            f"{shlex.join(command)}; echo; echo 'Press Enter to close'; read -r"
        )
        terminal = find_terminal()
        if terminal is None:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.CLOSE,
                text="No terminal emulator found",
            )
            dialog.format_secondary_text(
                "Run this command in a terminal:\n\n" + shlex.join(command)
            )
            dialog.run()
            dialog.destroy()
            return
        subprocess.Popen(terminal_command(terminal, shell))
        self.response(Gtk.ResponseType.CLOSE)

    def _alert(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CLOSE,
            text=message,
        )
        dialog.run()
        dialog.destroy()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    initialized, remaining = Gtk.init_check(arguments)
    if not initialized:
        print(
            "crossrun: no graphical display detected; use the command line "
            "interface instead (crossrun --help).",
            file=sys.stderr,
        )
        return 1

    application = Gtk.Application(
        application_id="org.crossrun.CrossRun",
        flags=Gio.ApplicationFlags.FLAGS_NONE,
    )

    def on_activate(app: Gtk.Application) -> None:
        window = CrossRunWindow(app)
        window.show_all()
        app.window = window  # type: ignore[attr-defined]

    application.connect("activate", on_activate)
    return application.run(remaining)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
