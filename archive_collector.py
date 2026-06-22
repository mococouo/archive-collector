from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Iterable

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk


DEFAULT_EXTENSIONS = [
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tgz",
    ".tbz2",
    ".txz",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
]

DEFAULT_LANGUAGE = "en"

BUILT_IN_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "language.name": "English",
        "app.title": "Archive Collector",
        "app.subtitle": "Collect archive files from a folder into one destination for easier batch extraction.",
        "label.source_folder": "Source folder",
        "label.destination_folder": "Destination folder",
        "label.extensions": "Extensions",
        "label.language": "Language",
        "option.recursive": "Scan subfolders",
        "option.move": "Move instead of copy",
        "button.browse": "Browse",
        "button.start": "Start collecting",
        "button.open_destination": "Open destination",
        "frame.log": "Log",
        "status.ready": "Ready",
        "status.processing": "Processing...",
        "status.done": "Done",
        "status.failed": "Failed",
        "dialog.source.title": "Choose source folder",
        "dialog.destination.title": "Choose destination folder",
        "error.missing_paths.title": "Missing folders",
        "error.missing_paths.message": "Choose both source and destination folders.",
        "info.title": "Information",
        "info.destination_required": "Choose a destination folder first.",
        "info.destination_missing": "The destination folder does not exist yet. Run a task or create it manually.",
        "error.open_failed.title": "Could not open folder",
        "message.done.title": "Done",
        "error.processing_failed.title": "Processing failed",
        "error.source_missing": "Source folder does not exist: {path}",
        "error.same_folder": "Source and destination must be different folders.",
        "mode.copy": "copy",
        "mode.move": "move",
        "common.yes": "yes",
        "common.no": "no",
        "log.source": "Source: {path}",
        "log.destination": "Destination: {path}",
        "log.mode": "Mode: {mode}",
        "log.recursive": "Recursive: {value}",
        "log.extensions": "Extensions: {extensions}",
        "log.ok": "[OK] {source} -> {destination}",
        "log.err": "[ERR] {path}: {error}",
        "log.done": "Done: {found} found, {transferred} transferred, {errors} errors.",
        "summary.format": (
            "Found {found} archive files, processed {processed}, skipped {skipped} regular files, "
            "failed {errors}."
        ),
        "cli.description": "Collect archive files from a folder into one destination folder.",
        "cli.source.help": "Source folder",
        "cli.destination.help": "Destination folder",
        "cli.cli.help": "Run in terminal mode.",
        "cli.gui.help": "Force the graphical interface.",
        "cli.move.help": "Move files instead of copying them.",
        "cli.flat.help": "Scan only the top level of the source folder.",
        "cli.extensions.help": "Comma-separated archive extensions, for example .zip,.rar,.7z",
        "cli.lang.help": "Language code for GUI, logs, and summaries. Default: en.",
        "cli.list_languages.help": "List available languages.",
        "cli.requires_paths": "CLI mode requires both source and destination folders.",
    }
}

_TRANSLATIONS_CACHE: dict[str, dict[str, str]] | None = None


@dataclass
class CollectionSummary:
    scanned: int = 0
    matched: int = 0
    copied: int = 0
    moved: int = 0
    skipped: int = 0
    errors: int = 0


def locale_directory() -> Path:
    return Path(__file__).resolve().with_name("locales")


def configure_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def load_translations() -> dict[str, dict[str, str]]:
    translations = {code: values.copy() for code, values in BUILT_IN_TRANSLATIONS.items()}
    directory = locale_directory()
    if not directory.is_dir():
        return translations

    for path in sorted(directory.glob("*.json")):
        code = path.stem.lower().replace("_", "-")
        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw_data, dict):
            continue

        entries = {
            str(key): str(value)
            for key, value in raw_data.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if entries:
            translations.setdefault(code, {}).update(entries)

    return translations


def get_translations() -> dict[str, dict[str, str]]:
    global _TRANSLATIONS_CACHE
    if _TRANSLATIONS_CACHE is None:
        _TRANSLATIONS_CACHE = load_translations()
    return _TRANSLATIONS_CACHE


def normalize_language_code(value: str | None) -> str:
    translations = get_translations()
    text = (value or DEFAULT_LANGUAGE).strip().lower().replace("_", "-")
    aliases = {
        "cn": "zh",
        "zh-cn": "zh",
        "zh-hans": "zh",
        "jp": "ja",
    }
    text = aliases.get(text, text)

    if text in translations:
        return text

    primary = text.split("-", 1)[0]
    if primary in translations:
        return primary

    return DEFAULT_LANGUAGE


def available_languages() -> dict[str, str]:
    translations = get_translations()
    codes = [DEFAULT_LANGUAGE]
    codes.extend(sorted(code for code in translations if code != DEFAULT_LANGUAGE))
    return {
        code: translations[code].get("language.name", code)
        for code in codes
        if code in translations
    }


def language_label(code: str) -> str:
    normalized = normalize_language_code(code)
    return f"{available_languages().get(normalized, normalized)} ({normalized})"


def language_code_from_label(label: str) -> str:
    text = label.strip()
    if text.endswith(")") and "(" in text:
        return text.rsplit("(", 1)[1][:-1].strip()
    return text


class Localizer:
    def __init__(self, language: str | None = None) -> None:
        self.language = normalize_language_code(language)

    def text(self, key: str, **kwargs: object) -> str:
        translations = get_translations()
        template = (
            translations.get(self.language, {}).get(key)
            or translations.get(DEFAULT_LANGUAGE, {}).get(key)
            or key
        )
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template


def normalize_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def normalize_extensions(raw_value: str | Iterable[str] | None) -> list[str]:
    if raw_value is None:
        items: Iterable[str] = DEFAULT_EXTENSIONS
    elif isinstance(raw_value, str):
        items = raw_value.replace("，", ",").split(",")
    else:
        items = raw_value

    normalized: list[str] = []
    seen: set[str] = set()

    for item in items:
        text = str(item).strip().lower()
        if not text:
            continue
        if not text.startswith("."):
            text = "." + text
        if text not in seen:
            seen.add(text)
            normalized.append(text)

    return sorted(normalized, key=len, reverse=True)


def match_extension(path: Path, extensions: list[str]) -> str | None:
    lower_name = path.name.lower()
    for extension in extensions:
        if lower_name.endswith(extension):
            return extension
    return None


def split_archive_name(path: Path, extension: str) -> tuple[str, str]:
    name = path.name
    if name.lower().endswith(extension):
        stem = name[: -len(extension)]
    else:
        stem = path.stem
    stem = stem.rstrip(". ")
    if not stem:
        stem = "archive"
    return stem, extension


def unique_destination(destination: Path, stem: str, extension: str) -> Path:
    candidate = destination / f"{stem}{extension}"
    index = 1
    while candidate.exists():
        candidate = destination / f"{stem} ({index}){extension}"
        index += 1
    return candidate


def open_folder(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def iter_files(source: Path, recursive: bool) -> Iterable[Path]:
    if recursive:
        yield from source.rglob("*")
    else:
        yield from source.iterdir()


def collect_archives(
    source: str | Path,
    destination: str | Path,
    *,
    recursive: bool = True,
    move: bool = False,
    extensions: str | Iterable[str] | None = None,
    logger: Callable[[str], None] | None = None,
    language: str | None = DEFAULT_LANGUAGE,
) -> CollectionSummary:
    messages = Localizer(language)
    source_path = normalize_path(source)
    destination_path = normalize_path(destination)

    if not source_path.exists() or not source_path.is_dir():
        raise ValueError(messages.text("error.source_missing", path=source_path))
    if same_path(source_path, destination_path):
        raise ValueError(messages.text("error.same_folder"))

    destination_path.mkdir(parents=True, exist_ok=True)

    archive_extensions = normalize_extensions(extensions)
    summary = CollectionSummary()
    log = logger or (lambda _message: None)
    destination_inside_source = is_within(destination_path, source_path)

    log(messages.text("log.source", path=source_path))
    log(messages.text("log.destination", path=destination_path))
    log(messages.text("log.mode", mode=messages.text("mode.move" if move else "mode.copy")))
    log(messages.text("log.recursive", value=messages.text("common.yes" if recursive else "common.no")))
    log(messages.text("log.extensions", extensions=", ".join(archive_extensions)))

    for path in iter_files(source_path, recursive):
        if not path.is_file():
            continue
        if destination_inside_source and is_within(path.resolve(strict=False), destination_path):
            continue

        summary.scanned += 1
        extension = match_extension(path, archive_extensions)
        if not extension:
            summary.skipped += 1
            continue

        summary.matched += 1
        stem, normalized_extension = split_archive_name(path, extension)
        target = unique_destination(destination_path, stem, normalized_extension)

        try:
            if move:
                shutil.move(str(path), str(target))
                summary.moved += 1
            else:
                shutil.copy2(path, target)
                summary.copied += 1
            relative = path.relative_to(source_path)
            log(messages.text("log.ok", source=relative, destination=target.name))
        except Exception as exc:  # noqa: BLE001
            summary.errors += 1
            log(messages.text("log.err", path=path, error=exc))

    log(
        messages.text(
            "log.done",
            found=summary.matched,
            transferred=summary.copied + summary.moved,
            errors=summary.errors,
        )
    )
    return summary


def format_summary(summary: CollectionSummary, language: str | None = DEFAULT_LANGUAGE) -> str:
    return Localizer(language).text(
        "summary.format",
        found=summary.matched,
        processed=summary.copied + summary.moved,
        skipped=summary.skipped,
        errors=summary.errors,
    )


class ArchiveCollectorApp:
    def __init__(self, language: str | None = DEFAULT_LANGUAGE) -> None:
        self.localizer = Localizer(language)
        self.localized_widgets: list[tuple[tk.Misc, str]] = []
        self.status_key = "status.ready"

        self.root = tk.Tk()
        self.root.title(self.t("app.title"))
        self.root.geometry("860x620")
        self.root.minsize(760, 560)

        self.source_var = tk.StringVar()
        self.destination_var = tk.StringVar()
        self.extensions_var = tk.StringVar(value=", ".join(DEFAULT_EXTENSIONS))
        self.language_var = tk.StringVar(value=language_label(self.localizer.language))
        self.recursive_var = tk.BooleanVar(value=True)
        self.move_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value=self.t(self.status_key))
        self.worker_running = False
        self.queue: Queue[tuple[str, str | CollectionSummary]] = Queue()

        self._build_ui()
        self.root.after(100, self._poll_queue)

    def t(self, key: str, **kwargs: object) -> str:
        return self.localizer.text(key, **kwargs)

    def _localize(self, widget: tk.Misc, key: str) -> tk.Misc:
        widget.configure(text=self.t(key))
        self.localized_widgets.append((widget, key))
        return widget

    def _set_status(self, key: str) -> None:
        self.status_key = key
        self.status_var.set(self.t(key))

    def _language_values(self) -> list[str]:
        return [f"{name} ({code})" for code, name in available_languages().items()]

    def _change_language(self, _event: tk.Event | None = None) -> None:
        self.localizer = Localizer(language_code_from_label(self.language_var.get()))
        self._refresh_ui_text()

    def _refresh_ui_text(self) -> None:
        self.root.title(self.t("app.title"))
        for widget, key in self.localized_widgets:
            widget.configure(text=self.t(key))
        self.language_combo.configure(values=self._language_values())
        self.language_var.set(language_label(self.localizer.language))
        self.status_var.set(self.t(self.status_key))

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        title_font = tkfont.nametofont("TkDefaultFont").copy()
        title_font.configure(size=16, weight="bold")

        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x")

        title = self._localize(ttk.Label(header, font=title_font), "app.title")
        title.pack(anchor="w")

        subtitle = self._localize(ttk.Label(header, wraplength=780), "app.subtitle")
        subtitle.pack(anchor="w", pady=(4, 12))

        form = ttk.Frame(container)
        form.pack(fill="x")

        self._add_path_row(form, "label.source_folder", self.source_var, self._pick_source)
        self._add_path_row(form, "label.destination_folder", self.destination_var, self._pick_destination)

        options = ttk.Frame(form)
        options.pack(fill="x", pady=(2, 6))

        self._localize(ttk.Checkbutton(options, variable=self.recursive_var), "option.recursive").pack(side="left")
        self._localize(ttk.Checkbutton(options, variable=self.move_var), "option.move").pack(side="left", padx=(14, 0))

        language_row = ttk.Frame(form)
        language_row.pack(fill="x", pady=(0, 10))
        self._localize(ttk.Label(language_row), "label.language").pack(side="left")
        self.language_combo = ttk.Combobox(
            language_row,
            textvariable=self.language_var,
            values=self._language_values(),
            state="readonly",
            width=24,
        )
        self.language_combo.pack(side="left", padx=(8, 0))
        self.language_combo.bind("<<ComboboxSelected>>", self._change_language)

        extensions_row = ttk.Frame(form)
        extensions_row.pack(fill="x", pady=(2, 12))
        self._localize(ttk.Label(extensions_row), "label.extensions").pack(anchor="w")
        extensions_entry = ttk.Entry(extensions_row, textvariable=self.extensions_var)
        extensions_entry.pack(fill="x", pady=(4, 0))

        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=(0, 10))

        self.start_button = self._localize(ttk.Button(buttons, command=self.start_collection), "button.start")
        self.start_button.pack(side="left")

        self.open_button = self._localize(ttk.Button(buttons, command=self.open_destination), "button.open_destination")
        self.open_button.pack(side="left", padx=8)

        ttk.Label(buttons, textvariable=self.status_var).pack(side="right")

        log_frame = self._localize(ttk.LabelFrame(container), "frame.log")
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, height=18, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _add_path_row(
        self,
        parent: ttk.Frame,
        label_key: str,
        variable: tk.StringVar,
        command: Callable[[], None],
    ) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 10))
        self._localize(ttk.Label(row), label_key).pack(anchor="w")

        inner = ttk.Frame(row)
        inner.pack(fill="x", pady=(4, 0))
        entry = ttk.Entry(inner, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        self._localize(ttk.Button(inner, command=command, width=10), "button.browse").pack(side="left", padx=(8, 0))

    def _pick_source(self) -> None:
        selected = filedialog.askdirectory(title=self.t("dialog.source.title"))
        if selected:
            self.source_var.set(selected)

    def _pick_destination(self) -> None:
        selected = filedialog.askdirectory(title=self.t("dialog.destination.title"))
        if selected:
            self.destination_var.set(selected)

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _queue_log(self, message: str) -> None:
        self.queue.put(("log", message))

    def _finish_worker(self, summary: CollectionSummary, language: str) -> None:
        self.queue.put(("done", format_summary(summary, language)))

    def _run_worker(
        self,
        source: str,
        destination: str,
        recursive: bool,
        move: bool,
        extensions: str,
        language: str,
    ) -> None:
        try:
            summary = collect_archives(
                source,
                destination,
                recursive=recursive,
                move=move,
                extensions=extensions,
                logger=self._queue_log,
                language=language,
            )
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", str(exc)))
        else:
            self._finish_worker(summary, language)

    def _set_running(self, running: bool) -> None:
        self.worker_running = running
        state = "disabled" if running else "normal"
        self.start_button.configure(state=state)

    def start_collection(self) -> None:
        if self.worker_running:
            return

        source = self.source_var.get().strip()
        destination = self.destination_var.get().strip()
        if not source or not destination:
            messagebox.showerror(self.t("error.missing_paths.title"), self.t("error.missing_paths.message"))
            return

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self._set_status("status.processing")
        self._set_running(True)

        worker = threading.Thread(
            target=self._run_worker,
            args=(
                source,
                destination,
                self.recursive_var.get(),
                self.move_var.get(),
                self.extensions_var.get(),
                self.localizer.language,
            ),
            daemon=True,
        )
        worker.start()

    def open_destination(self) -> None:
        destination = self.destination_var.get().strip()
        if not destination:
            messagebox.showinfo(self.t("info.title"), self.t("info.destination_required"))
            return
        path = normalize_path(destination)
        if not path.exists():
            messagebox.showinfo(self.t("info.title"), self.t("info.destination_missing"))
            return
        try:
            open_folder(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(self.t("error.open_failed.title"), str(exc))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "done":
                    self._log(str(payload))
                    self._set_status("status.done")
                    self._set_running(False)
                    messagebox.showinfo(self.t("message.done.title"), str(payload))
                elif kind == "error":
                    self._log(f"[ERR] {payload}")
                    self._set_status("status.failed")
                    self._set_running(False)
                    messagebox.showerror(self.t("error.processing_failed.title"), str(payload))
        except Empty:
            pass
        finally:
            self.root.after(100, self._poll_queue)

    def run(self) -> None:
        self.root.mainloop()


def build_parser() -> argparse.ArgumentParser:
    messages = Localizer(DEFAULT_LANGUAGE)
    parser = argparse.ArgumentParser(description=messages.text("cli.description"))
    parser.add_argument("source", nargs="?", help=messages.text("cli.source.help"))
    parser.add_argument("destination", nargs="?", help=messages.text("cli.destination.help"))
    parser.add_argument("--cli", action="store_true", help=messages.text("cli.cli.help"))
    parser.add_argument("--gui", action="store_true", help=messages.text("cli.gui.help"))
    parser.add_argument("--move", action="store_true", help=messages.text("cli.move.help"))
    parser.add_argument("--flat", action="store_true", help=messages.text("cli.flat.help"))
    parser.add_argument(
        "--extensions",
        default="",
        help=messages.text("cli.extensions.help"),
    )
    parser.add_argument("--lang", default=DEFAULT_LANGUAGE, help=messages.text("cli.lang.help"))
    parser.add_argument("--list-languages", action="store_true", help=messages.text("cli.list_languages.help"))
    return parser


def run_cli(args: argparse.Namespace) -> int:
    messages = Localizer(args.lang)
    if not args.source or not args.destination:
        raise SystemExit(messages.text("cli.requires_paths"))

    summary = collect_archives(
        args.source,
        args.destination,
        recursive=not args.flat,
        move=args.move,
        extensions=args.extensions or DEFAULT_EXTENSIONS,
        logger=print,
        language=args.lang,
    )
    print(format_summary(summary, args.lang))
    return 0 if summary.errors == 0 else 1


def main(argv: list[str] | None = None) -> int:
    configure_standard_streams()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_languages:
        for code, name in available_languages().items():
            print(f"{code}\t{name}")
        return 0

    should_use_gui = args.gui or (not args.cli and not args.source and not args.destination)
    if should_use_gui:
        app = ArchiveCollectorApp(language=args.lang)
        app.run()
        return 0

    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
