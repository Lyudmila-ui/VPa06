#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор демонстрационных чеков из HTML-шаблонов и CSV.

Скрипт подставляет значения колонок в плейсхолдеры {{название_колонки}},
сохраняет отдельный HTML и PDF на каждую выбранную запись и не выполняет
фискализацию.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import unquote, urlparse


APP_TITLE = "Генератор чеков"
SETTINGS_FILE = "settings.json"
PREVIEW_NAME = "vpa06_receipt_preview.html"
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
FONT_URL_RE = re.compile(
    r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)",
    flags=re.IGNORECASE,
)
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
SKIP_DIRECTORIES = {".git", "__pycache__", "node_modules", ".venv", "venv"}
TEMPLATE_SUFFIXES = {".html", ".htm"}
CSV_SUFFIXES = {".csv"}
ROLE_ORDER = ("number", "date", "client", "amount")
ROLE_TITLES = {
    "number": "Номер",
    "date": "Дата",
    "client": "Клиент",
    "amount": "Сумма",
}
ROLE_HINTS = {
    "number": (
        "номер чека",
        "номер документа",
        "receipt number",
        "invoice number",
        "номер",
        "чек",
        "receipt",
        "invoice",
        "number",
        "№",
    ),
    "date": ("дата чека", "дата документа", "дата", "date"),
    "client": (
        "имя клиента",
        "покупатель",
        "клиент",
        "фио",
        "customer",
        "client",
    ),
    "amount": ("итоговая сумма", "сумма чека", "итого", "сумма", "amount", "total", "стоимость"),
}
ENCODING_OPTIONS = (
    ("auto", "Авто"),
    ("utf-8", "UTF-8"),
    ("utf-8-sig", "UTF-8 с BOM"),
    ("cp1251", "Windows-1251"),
)
DELIMITER_OPTIONS = (
    ("auto", "Авто"),
    (",", "Запятая"),
    (";", "Точка с запятой"),
    ("\t", "Табуляция"),
)
ROW_SENTINEL = "__row__"
AUTO_SENTINEL = ""


class EncodingRequired(Exception):
    """UTF-8 не подошёл, пользователю нужно выбрать кодировку."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class RendererError(Exception):
    """WeasyPrint или системные библиотеки недоступны."""


@dataclass
class CsvTable:
    path: Path
    encoding: str
    delimiter: str
    headers: list[str]
    rows: list[dict[str, str]]
    warnings: list[str] = field(default_factory=list)


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def default_output_dir() -> Path:
    return app_dir() / "output"


def default_settings() -> dict:
    base = app_dir()
    templates = base / "templates"
    data = base / "data"
    return {
        "template_folders": [str(templates.resolve())] if templates.is_dir() else [],
        "csv_folders": [str(data.resolve())] if data.is_dir() else [],
        "extra_templates": [],
        "extra_csvs": [],
        "last_template": "",
        "last_csv": "",
        "output_folder": str(default_output_dir()),
        "open_pdf": True,
        "encoding": "auto",
        "delimiter": "auto",
        "display_columns": {role: AUTO_SENTINEL for role in ROLE_ORDER},
        "mappings": {},
    }


def settings_path() -> Path:
    return app_dir() / SETTINGS_FILE


def load_settings() -> tuple[dict, str | None]:
    path = settings_path()
    defaults = default_settings()
    if not path.exists():
        return defaults, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        backup = path.with_suffix(".json.bak")
        try:
            path.replace(backup)
        except OSError:
            backup = path
        return defaults, (
            "Файл настроек повреждён и не использован. "
            f"Резервная копия: {backup}. Причина: {exc}"
        )
    if not isinstance(raw, dict):
        return defaults, "Файл настроек имеет неожиданный формат. Используются настройки по умолчанию."
    for key, value in defaults.items():
        raw.setdefault(key, value)
    columns = raw.get("display_columns")
    if not isinstance(columns, dict):
        raw["display_columns"] = dict(defaults["display_columns"])
    else:
        for role, value in defaults["display_columns"].items():
            columns.setdefault(role, value)
    if not isinstance(raw.get("mappings"), dict):
        raw["mappings"] = {}
    for key in ("template_folders", "csv_folders", "extra_templates", "extra_csvs"):
        if not isinstance(raw.get(key), list):
            raw[key] = []
    return raw, None


def save_settings(settings: dict) -> None:
    path = settings_path()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def encoding_label(value: str) -> str:
    for key, label in ENCODING_OPTIONS:
        if key == value:
            return label
    return value


def delimiter_label(value: str) -> str:
    for key, label in DELIMITER_OPTIONS:
        if key == value:
            return label
    if value == "\t":
        return "Табуляция"
    return value


def normalize_header(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    text = re.sub(r"[_\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def decode_auto(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise EncodingRequired(
            "Файл не читается как UTF-8. Выберите кодировку: UTF-8, UTF-8 с BOM или Windows-1251. "
            f"Техническая причина: {exc}"
        ) from exc


def decode_chosen(data: bytes, encoding: str) -> str:
    if encoding not in {item[0] for item in ENCODING_OPTIONS if item[0] != "auto"}:
        raise ValueError(f"Неизвестная кодировка: {encoding}")
    text = data.decode(encoding)
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def sniff_delimiter(text: str) -> str:
    sample = text[:65536]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = None
    if dialect is not None and dialect.delimiter in {",", ";", "\t"}:
        return dialect.delimiter
    first = ""
    for line in sample.splitlines():
        if line.strip():
            first = line
            break
    counts = {",": first.count(","), ";": first.count(";"), "\t": first.count("\t")}
    best = max(counts, key=lambda item: counts[item])
    if counts[best] > 0:
        return best
    return ";"


def parse_csv_text(text: str, delimiter: str) -> tuple[list[str], list[dict[str, str]], list[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"')
    try:
        header_row = next(reader)
    except StopIteration as exc:
        raise ValueError("Файл пуст.") from exc
    if not any(cell.strip() for cell in header_row):
        raise ValueError("В файле нет строки заголовков.")
    headers: list[str] = []
    seen: dict[str, int] = {}
    warnings: list[str] = []
    for index, cell in enumerate(header_row, start=1):
        name = cell.strip() or f"колонка_{index}"
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}_{seen[name]}"
            warnings.append(f"Повторяющийся заголовок. Колонка {index} будет называться «{name}».")
        headers.append(name)
    rows: list[dict[str, str]] = []
    extra_values = False
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        if len(row) > len(headers):
            extra_values = True
            row = row[: len(headers)]
        if len(row) < len(headers):
            row = [*row, *[""] * (len(headers) - len(row))]
        rows.append({headers[index]: row[index] for index in range(len(headers))})
    if extra_values:
        warnings.append("В некоторых строках больше значений, чем заголовков. Лишние значения пропущены.")
    return headers, rows, warnings


def read_csv_file(path: Path, encoding_mode: str, delimiter_mode: str) -> CsvTable:
    data = path.read_bytes()
    if not data.strip():
        raise ValueError("Файл пуст.")
    if encoding_mode == "auto":
        text, encoding = decode_auto(data)
    else:
        try:
            text = decode_chosen(data, encoding_mode)
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"Не удалось прочитать файл в кодировке {encoding_label(encoding_mode)}. "
                "Выберите другую кодировку и нажмите «Перечитать»."
            ) from exc
        encoding = encoding_mode
    delimiter = sniff_delimiter(text) if delimiter_mode == "auto" else delimiter_mode
    if delimiter not in {",", ";", "\t"}:
        raise ValueError("Выберите разделитель: запятая, точка с запятой или табуляция.")
    headers, rows, warnings = parse_csv_text(text, delimiter)
    return CsvTable(path, encoding, delimiter, headers, rows, warnings)


def read_template(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def extract_placeholders(template: str) -> list[str]:
    found: list[str] = []
    for match in PLACEHOLDER_RE.finditer(template):
        name = match.group(1).strip()
        if name and name not in found:
            found.append(name)
    return found


def auto_match(placeholders: list[str], headers: list[str]) -> tuple[dict[str, str], list[str]]:
    by_exact = {header: header for header in headers}
    by_normal: dict[str, str] = {}
    for header in headers:
        by_normal.setdefault(normalize_header(header), header)
    matched: dict[str, str] = {}
    missing: list[str] = []
    for name in placeholders:
        if name in by_exact:
            matched[name] = name
        elif normalize_header(name) in by_normal:
            matched[name] = by_normal[normalize_header(name)]
        else:
            missing.append(name)
    return matched, missing


def apply_saved_mapping(
    placeholders: list[str],
    headers: list[str],
    saved: dict,
) -> tuple[dict[str, str], list[str]]:
    matched, missing = auto_match(placeholders, headers)
    still_missing: list[str] = []
    for name in missing:
        column = saved.get(name) if isinstance(saved, dict) else None
        if isinstance(column, str) and column in headers:
            matched[name] = column
        else:
            still_missing.append(name)
    return matched, still_missing


def render_template(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key not in values:
            return match.group(0)
        return values[key]

    return PLACEHOLDER_RE.sub(replace, template)


def escaped_row(row: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for placeholder, column in mapping.items():
        raw = row.get(column, "")
        rendered[placeholder] = html.escape("" if raw is None else str(raw), quote=True)
    return rendered


def missing_field_message(record_number: int, missing: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for placeholder, column in missing:
        if placeholder == column:
            parts.append(f"«{placeholder}»")
        else:
            parts.append(f"«{placeholder}» (колонка «{column}»)")
    quoted = ", ".join(parts)
    if len(missing) == 1:
        return f"Запись №{record_number}: нет значения для поля {quoted}."
    return f"Запись №{record_number}: нет значения для полей {quoted}."


def empty_placeholders(row: dict[str, str], mapping: dict[str, str]) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for placeholder, column in mapping.items():
        value = row.get(column, "")
        if value is None or str(value).strip() == "":
            missing.append((placeholder, column))
    return missing


def guess_column(headers: list[str], hints: tuple[str, ...], used: set[str]) -> str | None:
    prepared = [(header, normalize_header(header)) for header in headers if header not in used]
    for hint in hints:
        for header, normalized in prepared:
            if normalized == hint:
                return header
    for hint in hints:
        hint_tokens = hint.split()
        for header, normalized in prepared:
            tokens = normalized.split()
            if len(hint_tokens) == 1 and hint in tokens:
                return header
            if len(hint_tokens) > 1 and hint in normalized:
                return header
    return None


def resolve_display_columns(headers: list[str], preferences: dict) -> dict[str, str | None]:
    resolved: dict[str, str | None] = {}
    used: set[str] = set()
    for role in ROLE_ORDER:
        preference = preferences.get(role, AUTO_SENTINEL) if isinstance(preferences, dict) else AUTO_SENTINEL
        if preference == ROW_SENTINEL:
            resolved[role] = None
            continue
        if isinstance(preference, str) and preference in headers:
            resolved[role] = preference
            used.add(preference)
            continue
        guessed = guess_column(headers, ROLE_HINTS[role], used)
        resolved[role] = guessed
        if guessed:
            used.add(guessed)
    return resolved


def display_value(row: dict[str, str], record_number: int, column: str | None) -> str:
    if column is None:
        return str(record_number)
    return row.get(column, "")


def shorten(value: str, limit: int = 80) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def safe_filename(name: str) -> str:
    cleaned = INVALID_FILENAME_RE.sub("_", name).replace("\u00a0", " ")
    cleaned = " ".join(cleaned.split()).strip(" .")
    cleaned = cleaned.strip(".")
    if not cleaned:
        cleaned = "чек"
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rstrip(" .")
    head = cleaned.split(".", 1)[0].upper()
    if head in RESERVED_STEMS or cleaned.upper() in RESERVED_STEMS:
        cleaned = f"_{cleaned}"
    return cleaned or "чек"


def allocate_stem(directory: Path, stem: str) -> str:
    candidate = stem
    index = 2
    while (directory / f"{candidate}.html").exists() or (directory / f"{candidate}.pdf").exists():
        candidate = f"{stem}_{index}"
        index += 1
        if index > 10000:
            raise OSError("Не удалось подобрать уникальное имя файла.")
    return candidate


def receipt_stem(record_number: int, row: dict[str, str], display: dict[str, str | None]) -> str:
    parts: list[str] = []
    number_column = display.get("number")
    if number_column:
        number_value = str(row.get(number_column, "")).strip()
        if number_value:
            parts.append(number_value)
    if not parts:
        parts.append(f"строка-{record_number}")
    for role in ("date", "client"):
        column = display.get(role)
        if not column:
            continue
        value = str(row.get(column, "")).strip()
        if value:
            parts.append(value)
    return safe_filename("_".join(parts))


def template_base_url(template_path: Path) -> str:
    url = template_path.parent.resolve().as_uri()
    return url if url.endswith("/") else url + "/"


def ensure_base(document: str, base_url: str) -> str:
    if re.search(r"<base\b", document, flags=re.IGNORECASE):
        return document
    tag = f'<base href="{html.escape(base_url, quote=True)}">'
    match = re.search(r"<head\b[^>]*>", document, flags=re.IGNORECASE)
    if match:
        return document[: match.end()] + tag + document[match.end() :]
    return tag + document


def local_font_files(template: str, template_path: Path) -> list[Path]:
    files: list[Path] = []
    for match in FONT_URL_RE.finditer(template):
        raw = match.group(2).strip()
        lower = raw.lower().split("?", 1)[0].split("#", 1)[0]
        if lower.startswith(("data:", "http://", "https://", "//")):
            continue
        if not lower.endswith((".ttf", ".otf", ".woff", ".woff2", ".ttc")):
            continue
        if raw.startswith("file:"):
            parsed = urlparse(raw)
            raw_path = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", raw_path):
                raw_path = raw_path[1:]
            resolved = Path(raw_path)
        else:
            candidate = Path(raw)
            resolved = candidate if candidate.is_absolute() else template_path.parent / candidate
        files.append(resolved.resolve())
    return files


def scan_files(folders: list[str], suffixes: set[str]) -> tuple[list[Path], list[str]]:
    found: list[Path] = []
    seen: set[Path] = set()
    warnings: list[str] = []
    for folder in folders:
        root = Path(folder)
        if not root.is_dir():
            warnings.append(f"Папка не найдена: {folder}")
            continue
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRECTORIES]
            for filename in filenames:
                if Path(filename).suffix.lower() not in suffixes:
                    continue
                path = (Path(current) / filename).resolve()
                if path in seen:
                    continue
                seen.add(path)
                found.append(path)
    found.sort(key=lambda item: (item.name.casefold(), str(item).casefold()))
    return found, warnings


def merge_extra(found: list[Path], extra: list[str]) -> list[Path]:
    seen = set(found)
    for item in extra:
        if not item:
            continue
        path = Path(item)
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    found.sort(key=lambda item: (item.name.casefold(), str(item).casefold()))
    return found


def open_path(path: Path) -> None:
    target = str(path.resolve())
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.run(["open", target], check=False)
        return
    raise OSError(
        "Открытие файла в системной программе предусмотрено для Windows и macOS. "
        f"Текущая система: {sys.platform}. Файл: {target}"
    )


def renderer_error_text(exc: Exception) -> str:
    text = str(exc)
    lowered = text.casefold()
    if "no module named" in lowered and "weasyprint" in lowered:
        return (
            "Библиотека WeasyPrint не установлена. В папке проекта выполните "
            "python -m pip install -r requirements.txt. "
            "На Windows дополнительно нужен Pango из MSYS2 UCRT64, на macOS — Pango из Homebrew. "
            "Порядок установки описан в README.md."
        )
    hint = ""
    if any(token in lowered for token in ("library", "pango", "gobject", "dll", "libgobject")):
        hint = (
            " На Windows установите Pango через MSYS2 UCRT64 и задайте "
            "WEASYPRINT_DLL_DIRECTORIES=C:\\msys64\\ucrt64\\bin. "
            "На macOS установите Pango: brew install pango, и при необходимости "
            "DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib."
        )
    return f"Не удалось загрузить WeasyPrint. {text}.{hint}"


def load_renderer():
    try:
        from weasyprint import HTML
        from weasyprint.text.fonts import FontConfiguration
    except Exception as exc:
        raise RendererError(renderer_error_text(exc)) from exc
    return HTML, FontConfiguration


def generate_receipts(job: dict, messages: queue.Queue, cancel_event: threading.Event) -> None:
    output_dir = Path(job["output_dir"])
    mapping: dict[str, str] = job["mapping"]
    display: dict[str, str | None] = job["display"]
    records: list[dict] = job["records"]
    created = 0
    errors: list[str] = []
    cancelled = False
    total = len(records)
    html_factory = None
    font_config = None

    for index, record in enumerate(records, start=1):
        if cancel_event.is_set():
            cancelled = True
            break
        number = int(record["number"])
        row = record["values"]
        messages.put(("progress", index, total, number))
        empty = empty_placeholders(row, mapping)
        if empty:
            errors.append(missing_field_message(number, empty))
            messages.put(("log", errors[-1]))
            continue
        if html_factory is None:
            try:
                html_factory, font_configuration = load_renderer()
                font_config = font_configuration()
            except RendererError as exc:
                errors.append(str(exc))
                messages.put(("log", errors[-1]))
                break
        html_path: Path | None = None
        try:
            rendered = ensure_base(
                render_template(job["template"], escaped_row(row, mapping)),
                job["base_url"],
            )
            stem = allocate_stem(output_dir, receipt_stem(number, row, display))
            html_path = output_dir / f"{stem}.html"
            pdf_path = output_dir / f"{stem}.pdf"
            html_path.write_text(rendered, encoding="utf-8")
            html_factory(string=rendered, base_url=job["base_url"]).write_pdf(
                str(pdf_path),
                font_config=font_config,
            )
            created += 1
            messages.put(("log", f"Запись №{number}: созданы {html_path.name} и {pdf_path.name}."))
            if job["open_pdf"]:
                messages.put(("open", str(pdf_path)))
        except Exception as exc:
            if html_path is not None and html_path.exists() and not (output_dir / f"{html_path.stem}.pdf").exists():
                try:
                    html_path.unlink()
                except OSError:
                    pass
            errors.append(f"Запись №{number}: не удалось создать чек. {exc}")
            messages.put(("log", errors[-1]))

    messages.put(("done", created, errors, cancelled))


class ReceiptApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1220x880")
        self.minsize(980, 700)
        self.settings, settings_warning = load_settings()
        self.messages: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.table: CsvTable | None = None
        self.template_text = ""
        self.loaded_template: Path | None = None
        self.loaded_csv: Path | None = None
        self.template_files: list[Path] = []
        self.csv_files: list[Path] = []
        self.template_by_iid: dict[str, Path] = {}
        self.csv_by_iid: dict[str, Path] = {}
        self._building = True
        self._closed = False
        self._build()
        self._building = False
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.rescan("template", persist=False)
        self.rescan("csv", persist=False)
        self._restore_last_files()
        self.after(200, self._poll)
        if settings_warning:
            self.log(settings_warning)
            messagebox.showwarning(APP_TITLE, settings_warning, parent=self)

    def _build(self) -> None:
        style = ttk.Style(self)
        if sys.platform == "win32" and "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=2)
        self.rowconfigure(4, weight=3)
        self.rowconfigure(10, weight=1)

        header = ttk.Frame(self, padding=(12, 10, 12, 4))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text=APP_TITLE, font=("", 16, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "Демонстрационные документы. Создание HTML и PDF не выполняет фискализацию "
                "и не формирует кассовый чек."
            ),
            wraplength=1100,
        ).pack(anchor="w", pady=(2, 0))

        browsers = ttk.Frame(self, padding=(12, 4, 12, 4))
        browsers.grid(row=1, column=0, sticky="nsew")
        browsers.columnconfigure(0, weight=1)
        browsers.columnconfigure(1, weight=1)
        browsers.rowconfigure(0, weight=1)
        self.template_panel = self._build_browser(browsers, 0, "template", "Шаблоны HTML")
        self.csv_panel = self._build_browser(browsers, 1, "csv", "Файлы CSV")

        controls = ttk.Frame(self, padding=(12, 4, 12, 0))
        controls.grid(row=2, column=0, sticky="ew")
        ttk.Label(controls, text="Кодировка").pack(side="left")
        self.encoding_var = tk.StringVar(value=encoding_label(self.settings.get("encoding", "auto")))
        self.encoding_box = ttk.Combobox(
            controls,
            textvariable=self.encoding_var,
            values=[label for _, label in ENCODING_OPTIONS],
            state="readonly",
            width=18,
        )
        self.encoding_box.pack(side="left", padx=(6, 12))
        self.encoding_box.bind("<<ComboboxSelected>>", lambda _event: self.reload_csv())
        ttk.Label(controls, text="Разделитель").pack(side="left")
        self.delimiter_var = tk.StringVar(value=delimiter_label(self.settings.get("delimiter", "auto")))
        self.delimiter_box = ttk.Combobox(
            controls,
            textvariable=self.delimiter_var,
            values=[label for _, label in DELIMITER_OPTIONS],
            state="readonly",
            width=20,
        )
        self.delimiter_box.pack(side="left", padx=(6, 12))
        self.delimiter_box.bind("<<ComboboxSelected>>", lambda _event: self.reload_csv())
        ttk.Button(controls, text="Перечитать", command=self.reload_csv).pack(side="left")
        ttk.Button(controls, text="Колонки списка", command=self.configure_columns).pack(side="left", padx=(8, 0))

        self.columns_var = tk.StringVar(value="Колонки CSV появятся после выбора файла.")
        ttk.Label(self, textvariable=self.columns_var, padding=(12, 4, 12, 0), wraplength=1160).grid(
            row=3, column=0, sticky="ew"
        )

        records_frame = ttk.Frame(self, padding=(12, 4, 12, 0))
        records_frame.grid(row=4, column=0, sticky="nsew")
        records_frame.rowconfigure(0, weight=1)
        records_frame.columnconfigure(0, weight=1)
        self.records = ttk.Treeview(
            records_frame,
            columns=("record", "number", "date", "client", "amount"),
            show="headings",
            selectmode="extended",
        )
        for key, title, width in (
            ("record", "Запись", 90),
            ("number", "Номер", 160),
            ("date", "Дата", 140),
            ("client", "Клиент", 260),
            ("amount", "Сумма", 140),
        ):
            self.records.heading(key, text=title)
            self.records.column(key, width=width, anchor="w", stretch=True)
        records_scroll_y = ttk.Scrollbar(records_frame, orient="vertical", command=self.records.yview)
        records_scroll_x = ttk.Scrollbar(records_frame, orient="horizontal", command=self.records.xview)
        self.records.configure(yscrollcommand=records_scroll_y.set, xscrollcommand=records_scroll_x.set)
        self.records.grid(row=0, column=0, sticky="nsew")
        records_scroll_y.grid(row=0, column=1, sticky="ns")
        records_scroll_x.grid(row=1, column=0, sticky="ew")
        self.records.bind("<<TreeviewSelect>>", lambda _event: self._update_record_preview())

        record_buttons = ttk.Frame(self, padding=(12, 4, 12, 0))
        record_buttons.grid(row=5, column=0, sticky="ew")
        ttk.Button(record_buttons, text="Выбрать все", command=self.select_all_records).pack(side="left")
        ttk.Button(record_buttons, text="Снять выбор", command=self.clear_record_selection).pack(side="left", padx=(8, 0))
        self.selection_var = tk.StringVar(value="Выбрано: 0 из 0")
        ttk.Label(record_buttons, textvariable=self.selection_var).pack(side="left", padx=(12, 0))

        preview_frame = ttk.Frame(self, padding=(12, 4, 12, 0))
        preview_frame.grid(row=6, column=0, sticky="ew")
        preview_frame.columnconfigure(0, weight=1)
        ttk.Label(preview_frame, text="Просмотр записи").grid(row=0, column=0, sticky="w")
        self.preview = tk.Text(preview_frame, height=5, wrap="word")
        preview_scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=preview_scroll.set)
        self.preview.grid(row=1, column=0, sticky="ew")
        preview_scroll.grid(row=1, column=1, sticky="ns")

        output = ttk.Frame(self, padding=(12, 8, 12, 0))
        output.grid(row=7, column=0, sticky="ew")
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="Папка результата").grid(row=0, column=0, sticky="w")
        self.output_var = tk.StringVar(value=self.settings.get("output_folder") or str(default_output_dir()))
        ttk.Entry(output, textvariable=self.output_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(output, text="Обзор", command=self.choose_output).grid(row=0, column=2, sticky="e")
        self.open_pdf_var = tk.BooleanVar(value=bool(self.settings.get("open_pdf", True)))
        ttk.Checkbutton(
            output,
            text="Открыть PDF после создания",
            variable=self.open_pdf_var,
            command=self._persist,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        actions = ttk.Frame(self, padding=(12, 8, 12, 0))
        actions.grid(row=8, column=0, sticky="ew")
        self.preview_button = ttk.Button(actions, text="Предпросмотр HTML", command=self.preview_html)
        self.preview_button.pack(side="left")
        self.create_button = ttk.Button(actions, text="Создать чеки", command=self.create_receipts)
        self.create_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="Остановить", command=self.stop_generation, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Открыть папку результата", command=self.open_output).pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="Добавьте папки с шаблонами и CSV или выберите файлы напрямую.")
        ttk.Label(self, textvariable=self.status_var, padding=(12, 6, 12, 0), wraplength=1160).grid(
            row=9, column=0, sticky="ew"
        )

        log_frame = ttk.Frame(self, padding=(12, 4, 12, 12))
        log_frame.grid(row=10, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=6, wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

    def _build_browser(self, parent: ttk.Frame, column: int, kind: str, title: str) -> dict:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0))
        frame.rowconfigure(3, weight=1)
        frame.columnconfigure(0, weight=1)

        folders = tk.Listbox(frame, height=3, exportselection=False)
        folders_scroll = ttk.Scrollbar(frame, orient="vertical", command=folders.yview)
        folders.configure(yscrollcommand=folders_scroll.set)
        folders.grid(row=0, column=0, sticky="ew")
        folders_scroll.grid(row=0, column=1, sticky="ns")

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(buttons, text="Добавить папку", command=lambda: self.add_folder(kind)).pack(side="left")
        ttk.Button(buttons, text="Удалить папку", command=lambda: self.remove_folder(kind)).pack(side="left", padx=4)
        ttk.Button(buttons, text="Обновить", command=lambda: self.rescan(kind)).pack(side="left")
        ttk.Button(buttons, text="Выбрать файл", command=lambda: self.pick_file(kind)).pack(side="left", padx=4)

        search_row = ttk.Frame(frame)
        search_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="Поиск").grid(row=0, column=0, sticky="w")
        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=search_var)
        search_entry.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        search_var.trace_add("write", lambda *_args, item_kind=kind: self.refill_files(item_kind))

        tree = ttk.Treeview(frame, columns=("name", "path"), show="headings", selectmode="browse")
        tree.heading("name", text="Название")
        tree.heading("path", text="Путь")
        tree.column("name", width=180, anchor="w")
        tree.column("path", width=360, anchor="w")
        tree_scroll_y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree_scroll_x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)
        tree.grid(row=3, column=0, sticky="nsew")
        tree_scroll_y.grid(row=3, column=1, sticky="ns")
        tree_scroll_x.grid(row=4, column=0, sticky="ew")
        tree.bind("<<TreeviewSelect>>", lambda _event, item_kind=kind: self._on_file_selected(item_kind))

        panel = {"folders": folders, "tree": tree, "search": search_var}
        self._fill_folder_list(panel, kind)
        return panel

    def _panel(self, kind: str) -> dict:
        return self.template_panel if kind == "template" else self.csv_panel

    def _folder_key(self, kind: str) -> str:
        return "template_folders" if kind == "template" else "csv_folders"

    def _extra_key(self, kind: str) -> str:
        return "extra_templates" if kind == "template" else "extra_csvs"

    def _suffixes(self, kind: str) -> set[str]:
        return TEMPLATE_SUFFIXES if kind == "template" else CSV_SUFFIXES

    def _fill_folder_list(self, panel: dict, kind: str) -> None:
        listing = panel["folders"]
        listing.delete(0, "end")
        for folder in self.settings.get(self._folder_key(kind), []):
            listing.insert("end", folder)

    def log(self, text: str) -> None:
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _option_value(self, options: tuple[tuple[str, str], ...], label: str, fallback: str) -> str:
        for value, item_label in options:
            if item_label == label:
                return value
        return fallback

    def encoding_value(self) -> str:
        return self._option_value(ENCODING_OPTIONS, self.encoding_var.get(), "auto")

    def delimiter_value(self) -> str:
        return self._option_value(DELIMITER_OPTIONS, self.delimiter_var.get(), "auto")

    def _set_option(self, variable: tk.StringVar, options: tuple[tuple[str, str], ...], value: str) -> None:
        variable.set(self._option_value_label(options, value))

    def _option_value_label(self, options: tuple[tuple[str, str], ...], value: str) -> str:
        for item_value, label in options:
            if item_value == value:
                return label
        return options[0][1]

    def _persist(self) -> None:
        if self._building:
            return
        self.settings["encoding"] = self.encoding_value()
        self.settings["delimiter"] = self.delimiter_value()
        self.settings["output_folder"] = self.output_var.get().strip()
        self.settings["open_pdf"] = bool(self.open_pdf_var.get())
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.set_status(f"Не удалось сохранить настройки: {exc}")

    def add_folder(self, kind: str) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="Выберите папку с шаблонами" if kind == "template" else "Выберите папку с CSV",
            mustexist=True,
        )
        if not selected:
            return
        folder = str(Path(selected).resolve())
        key = self._folder_key(kind)
        folders = [str(Path(item).resolve()) for item in self.settings.get(key, []) if item]
        if folder in folders:
            messagebox.showinfo(APP_TITLE, "Эта папка уже добавлена.", parent=self)
            return
        folders.append(folder)
        self.settings[key] = folders
        self._fill_folder_list(self._panel(kind), kind)
        self.rescan(kind)

    def remove_folder(self, kind: str) -> None:
        panel = self._panel(kind)
        selection = panel["folders"].curselection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Выберите папку в списке над кнопками.", parent=self)
            return
        folders = list(self.settings.get(self._folder_key(kind), []))
        del folders[selection[0]]
        self.settings[self._folder_key(kind)] = folders
        self._fill_folder_list(panel, kind)
        self.rescan(kind)

    def rescan(self, kind: str, persist: bool = True) -> None:
        found, warnings = scan_files(self.settings.get(self._folder_key(kind), []), self._suffixes(kind))
        found = merge_extra(found, self.settings.get(self._extra_key(kind), []))
        if kind == "template":
            self.template_files = found
        else:
            self.csv_files = found
        for warning in warnings:
            self.log(warning)
        self.refill_files(kind)
        if persist:
            self._persist()
        if not self._selected_path(kind):
            title = "шаблонов" if kind == "template" else "CSV"
            self.set_status(f"Найдено {title}: {len(found)}.")

    def refill_files(self, kind: str) -> None:
        panel = self._panel(kind)
        tree: ttk.Treeview = panel["tree"]
        query = panel["search"].get().strip().casefold()
        files = self.template_files if kind == "template" else self.csv_files
        current = self._remembered_path(kind)
        mapping = self.template_by_iid if kind == "template" else self.csv_by_iid
        mapping.clear()
        tree.delete(*tree.get_children())
        selected_iid = ""
        for index, path in enumerate(files):
            if query and query not in path.name.casefold():
                continue
            iid = f"{kind}-{index}"
            mapping[iid] = path
            tree.insert("", "end", iid=iid, values=(path.name, str(path)))
            if current is not None and path == current:
                selected_iid = iid
        if selected_iid:
            tree.selection_set(selected_iid)
            tree.focus(selected_iid)
            tree.see(selected_iid)

    def _remembered_path(self, kind: str) -> Path | None:
        key = "last_template" if kind == "template" else "last_csv"
        raw = self.settings.get(key) or ""
        if not raw:
            return None
        path = Path(raw)
        return path.resolve() if path.exists() else None

    def _selected_path(self, kind: str) -> Path | None:
        panel = self._panel(kind)
        tree: ttk.Treeview = panel["tree"]
        mapping = self.template_by_iid if kind == "template" else self.csv_by_iid
        selection = tree.selection()
        if not selection:
            return None
        return mapping.get(selection[0])

    def _select_path(self, kind: str, path: Path) -> None:
        mapping = self.template_by_iid if kind == "template" else self.csv_by_iid
        tree: ttk.Treeview = self._panel(kind)["tree"]
        resolved = path.resolve()
        for iid, item in mapping.items():
            if item == resolved:
                tree.selection_set(iid)
                tree.focus(iid)
                tree.see(iid)
                return

    def pick_file(self, kind: str) -> None:
        if kind == "template":
            selected = filedialog.askopenfilename(
                parent=self,
                title="Выберите HTML-шаблон",
                filetypes=(("HTML", "*.html *.htm"), ("Все файлы", "*.*")),
            )
        else:
            selected = filedialog.askopenfilename(
                parent=self,
                title="Выберите CSV-файл",
                filetypes=(("CSV", "*.csv"), ("Все файлы", "*.*")),
            )
        if not selected:
            return
        path = Path(selected).resolve()
        if not path.is_file():
            messagebox.showerror(APP_TITLE, f"Файл не найден: {path}", parent=self)
            return
        if kind == "template":
            self.settings["last_template"] = str(path)
        else:
            self.settings["last_csv"] = str(path)
        extra_key = self._extra_key(kind)
        extras = [str(Path(item).resolve()) for item in self.settings.get(extra_key, []) if item]
        if str(path) not in extras and path not in (self.template_files if kind == "template" else self.csv_files):
            extras.append(str(path))
            self.settings[extra_key] = extras
        self.rescan(kind, persist=False)
        self._select_path(kind, path)
        self._on_file_selected(kind)
        self._persist()

    def _restore_last_files(self) -> None:
        last_template = self.settings.get("last_template") or ""
        last_csv = self.settings.get("last_csv") or ""
        if last_template and Path(last_template).is_file():
            self._select_path("template", Path(last_template))
            self._on_file_selected("template")
        elif self.template_files:
            self._select_path("template", self.template_files[0])
            self._on_file_selected("template")
        if last_csv and Path(last_csv).is_file():
            self._select_path("csv", Path(last_csv))
            self._on_file_selected("csv")
        elif self.csv_files:
            self._select_path("csv", self.csv_files[0])
            self._on_file_selected("csv")

    def _on_file_selected(self, kind: str) -> None:
        path = self._selected_path(kind)
        if path is None:
            return
        path = path.resolve()
        if kind == "template":
            if self.loaded_template == path and self.template_text:
                return
            try:
                self.template_text = read_template(path)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, f"Не удалось прочитать шаблон.\n{exc}", parent=self)
                return
            self.loaded_template = path
            self.settings["last_template"] = str(path)
            self.set_status(f"Шаблон: {path.name}")
            self._persist()
            return
        if self.loaded_csv == path and self.table is not None:
            return
        self.settings["last_csv"] = str(path)
        self.load_csv(path)

    def reload_csv(self) -> None:
        if self._generating():
            return
        path = self._selected_path("csv")
        if path is None:
            self.set_status("Сначала выберите CSV-файл.")
            return
        self.load_csv(path)

    def load_csv(self, path: Path) -> None:
        encoding = self.encoding_value()
        delimiter = self.delimiter_value()
        try:
            table = read_csv_file(path, encoding, delimiter)
        except EncodingRequired as exc:
            chosen = self.ask_encoding(path, exc.detail)
            if not chosen:
                self.table = None
                self._clear_records()
                self.set_status("Автоматическое чтение не удалось. Выберите кодировку и нажмите «Перечитать».")
                return
            self._set_option(self.encoding_var, ENCODING_OPTIONS, chosen)
            try:
                table = read_csv_file(path, chosen, delimiter)
            except (OSError, UnicodeDecodeError, ValueError) as second:
                self.table = None
                self._clear_records()
                messagebox.showerror(APP_TITLE, str(second), parent=self)
                return
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self.table = None
            self._clear_records()
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
            return
        self.table = table
        self.loaded_csv = path.resolve()
        self.settings["last_csv"] = str(path)
        if table.encoding != "auto":
            self._set_option(self.encoding_var, ENCODING_OPTIONS, self.encoding_value())
        self.columns_var.set("Колонки: " + ", ".join(table.headers))
        self._fill_records()
        for warning in table.warnings:
            self.log(warning)
        if table.rows:
            self.set_status(
                f"Прочитано записей: {len(table.rows)}. Кодировка: {encoding_label(table.encoding)}. "
                f"Разделитель: {delimiter_label(table.delimiter)}. "
                "Отметьте записи или нажмите «Выбрать все»."
            )
        else:
            self.set_status("В файле нет записей. Добавьте строки под заголовком.")
        self._persist()

    def _clear_records(self) -> None:
        self.records.delete(*self.records.get_children())
        self.preview.delete("1.0", "end")
        self.selection_var.set("Выбрано: 0 из 0")
        self.columns_var.set("Колонки CSV появятся после выбора файла.")

    def _fill_records(self) -> None:
        self.records.delete(*self.records.get_children())
        self.preview.delete("1.0", "end")
        if self.table is None:
            self.selection_var.set("Выбрано: 0 из 0")
            return
        display = resolve_display_columns(self.table.headers, self.settings.get("display_columns", {}))
        self.records.heading("record", text="Запись")
        for role in ROLE_ORDER:
            column = display[role]
            suffix = "№ строки" if column is None else column
            self.records.heading(role, text=f"{ROLE_TITLES[role]} ({suffix})")
        for index, row in enumerate(self.table.rows, start=1):
            self.records.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    f"№ {index}",
                    shorten(display_value(row, index, display["number"])),
                    shorten(display_value(row, index, display["date"])),
                    shorten(display_value(row, index, display["client"])),
                    shorten(display_value(row, index, display["amount"])),
                ),
            )
        self._update_record_preview()

    def select_all_records(self) -> None:
        children = self.records.get_children()
        if children:
            self.records.selection_set(children)
        self._update_record_preview()

    def clear_record_selection(self) -> None:
        self.records.selection_remove(self.records.selection())
        self._update_record_preview()

    def _selected_record_numbers(self) -> list[int]:
        numbers = []
        for iid in self.records.selection():
            try:
                numbers.append(int(iid))
            except ValueError:
                continue
        return sorted(set(numbers))

    def _update_record_preview(self) -> None:
        total = len(self.records.get_children())
        numbers = self._selected_record_numbers()
        self.selection_var.set(f"Выбрано: {len(numbers)} из {total}")
        self.preview.delete("1.0", "end")
        if self.table is None or not numbers:
            return
        focus = self.records.focus()
        number = int(focus) if focus and focus.isdigit() and int(focus) in numbers else numbers[0]
        row = self.table.rows[number - 1]
        lines = [f"Запись №{number}"]
        for header in self.table.headers:
            lines.append(f"{header}: {row.get(header, '')}")
        self.preview.insert("1.0", "\n".join(lines))

    def configure_columns(self) -> None:
        if self.table is None:
            messagebox.showinfo(APP_TITLE, "Сначала выберите CSV-файл.", parent=self)
            return
        headers = self.table.headers
        current = self.settings.get("display_columns", {})
        dialog = tk.Toplevel(self)
        dialog.title("Колонки списка")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text=(
                "Выберите, какие колонки показывать как номер, дату, клиента и сумму. "
                "«Авто» ищет подходящее название. «Номер строки» оставляет порядковый номер записи."
            ),
            wraplength=460,
            padding=12,
        ).pack(anchor="w")
        choices = ["Авто", "Номер строки", *headers]
        variables: dict[str, tk.StringVar] = {}
        for role in ROLE_ORDER:
            row = ttk.Frame(dialog, padding=(12, 2, 12, 2))
            row.pack(fill="x")
            ttk.Label(row, text=ROLE_TITLES[role], width=12).pack(side="left")
            preference = current.get(role, AUTO_SENTINEL)
            if preference == ROW_SENTINEL:
                label = "Номер строки"
            elif preference in headers:
                label = preference
            else:
                label = "Авто"
            variable = tk.StringVar(value=label)
            variables[role] = variable
            ttk.Combobox(row, textvariable=variable, values=choices, state="readonly", width=36).pack(
                side="left", fill="x", expand=True
            )
        result = {"saved": False}

        def save() -> None:
            mapped: dict[str, str] = {}
            for role, variable in variables.items():
                label = variable.get()
                if label == "Авто":
                    mapped[role] = AUTO_SENTINEL
                elif label == "Номер строки":
                    mapped[role] = ROW_SENTINEL
                elif label in headers:
                    mapped[role] = label
                else:
                    messagebox.showerror(APP_TITLE, f"Неизвестная колонка для поля «{ROLE_TITLES[role]}».", parent=dialog)
                    return
            self.settings["display_columns"] = mapped
            result["saved"] = True
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Сохранить", command=save).pack(side="right")
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)
        if result["saved"]:
            self._fill_records()
            self._persist()

    def ask_encoding(self, path: Path, detail: str) -> str | None:
        dialog = tk.Toplevel(self)
        dialog.title("Кодировка CSV")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text=f"Не удалось автоматически прочитать «{path.name}».\n{detail}",
            wraplength=460,
            padding=12,
        ).pack(anchor="w")
        choice = tk.StringVar(value="cp1251")
        for value, label in ENCODING_OPTIONS:
            if value == "auto":
                continue
            ttk.Radiobutton(dialog, text=label, value=value, variable=choice).pack(anchor="w", padx=12, pady=2)
        result = {"value": None}

        def accept() -> None:
            result["value"] = choice.get()
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Прочитать", command=accept).pack(side="right")
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)
        return result["value"]

    def ask_mapping(self, matched: dict[str, str], missing: list[str], headers: list[str]) -> dict[str, str] | None:
        dialog = tk.Toplevel(self)
        dialog.title("Сопоставление полей")
        dialog.transient(self)
        dialog.resizable(True, True)
        dialog.grab_set()
        dialog.geometry("640x460")
        ttk.Label(
            dialog,
            text=(
                "Названия в шаблоне и в CSV различаются. Выберите колонку CSV для каждого поля шаблона. "
                "Без сопоставления чек для записи создать нельзя."
            ),
            wraplength=600,
            padding=12,
        ).pack(anchor="w")
        if matched:
            ttk.Label(
                dialog,
                text="Уже сопоставлено: " + ", ".join(f"{name} → {column}" for name, column in matched.items()),
                wraplength=600,
                padding=(12, 0, 12, 8),
            ).pack(anchor="w")
        body = ttk.Frame(dialog, padding=(12, 0, 12, 0))
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        variables: dict[str, tk.StringVar] = {}
        for name in missing:
            row = ttk.Frame(inner, padding=(0, 3))
            row.pack(fill="x")
            ttk.Label(row, text=name, width=28).pack(side="left")
            variable = tk.StringVar()
            variables[name] = variable
            ttk.Combobox(row, textvariable=variable, values=headers, state="readonly", width=36).pack(side="left")
        result: dict[str, dict[str, str] | None] = {"value": None}

        def accept() -> None:
            chosen = dict(matched)
            for name, variable in variables.items():
                column = variable.get().strip()
                if column not in headers:
                    messagebox.showerror(
                        APP_TITLE,
                        f"Укажите колонку CSV для поля «{name}».",
                        parent=dialog,
                    )
                    return
                chosen[name] = column
            result["value"] = chosen
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Продолжить", command=accept).pack(side="right")
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)
        value = result["value"]
        return value if isinstance(value, dict) else None

    def choose_output(self) -> None:
        current = self.output_var.get().strip()
        initial = current if current and Path(current).is_dir() else str(default_output_dir())
        selected = filedialog.askdirectory(parent=self, title="Папка результата", initialdir=initial, mustexist=False)
        if not selected:
            return
        self.output_var.set(str(Path(selected).resolve()))
        self._persist()

    def output_directory(self) -> Path | None:
        raw = self.output_var.get().strip() or str(default_output_dir())
        path = Path(raw)
        if path.exists() and not path.is_dir():
            messagebox.showerror(APP_TITLE, f"Путь результата не является папкой:\n{path}", parent=self)
            return None
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Не удалось создать папку результата.\n{exc}", parent=self)
            return None
        self.output_var.set(str(path.resolve()))
        self._persist()
        return path.resolve()

    def open_output(self) -> None:
        directory = self.output_directory()
        if directory is None:
            return
        try:
            open_path(directory)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self)

    def _current_template_path(self) -> Path | None:
        path = self._selected_path("template")
        if path is None:
            last = self.settings.get("last_template") or ""
            if last and Path(last).is_file():
                return Path(last)
        return path

    def _prepare_job(self, numbers: list[int]) -> dict | None:
        template_path = self._current_template_path()
        if template_path is None or not template_path.is_file():
            messagebox.showinfo(APP_TITLE, "Выберите HTML-шаблон.", parent=self)
            return None
        if self.table is None:
            messagebox.showinfo(APP_TITLE, "Выберите CSV-файл.", parent=self)
            return None
        if not numbers:
            messagebox.showinfo(APP_TITLE, "Выберите хотя бы одну запись.", parent=self)
            return None
        try:
            template = read_template(template_path)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Не удалось прочитать шаблон.\n{exc}", parent=self)
            return None
        self.template_text = template
        placeholders = extract_placeholders(template)
        if not placeholders:
            messagebox.showerror(
                APP_TITLE,
                "В шаблоне нет полей вида {{название_колонки}}. Добавьте плейсхолдеры или выберите другой шаблон.",
                parent=self,
            )
            return None
        mapping_key = f"{template_path.resolve()}||{self.table.path.resolve()}"
        saved = self.settings.get("mappings", {}).get(mapping_key, {})
        matched, missing = apply_saved_mapping(placeholders, self.table.headers, saved)
        if missing:
            chosen = self.ask_mapping(matched, missing, self.table.headers)
            if chosen is None:
                self.set_status("Сопоставление полей отменено.")
                return None
            matched = chosen
        self.settings.setdefault("mappings", {})[mapping_key] = matched
        self._persist()
        records = []
        for number in numbers:
            if number < 1 or number > len(self.table.rows):
                continue
            records.append({"number": number, "values": dict(self.table.rows[number - 1])})
        if not records:
            messagebox.showinfo(APP_TITLE, "Выберите хотя бы одну запись.", parent=self)
            return None
        return {
            "template_path": template_path,
            "template": template,
            "base_url": template_base_url(template_path),
            "mapping": matched,
            "records": records,
            "display": resolve_display_columns(self.table.headers, self.settings.get("display_columns", {})),
        }

    def _font_problem(self, template: str, template_path: Path) -> str | None:
        fonts = local_font_files(template, template_path)
        if not fonts:
            return (
                "В шаблоне нет локального файла шрифта в @font-face. "
                f"Положите Roboto или Liberation Sans в папку {app_dir() / 'fonts'} "
                "и подключите файл через url(...)."
            )
        missing = [path for path in fonts if not path.is_file()]
        if not missing:
            return None
        lines = "\n".join(str(path) for path in missing)
        return (
            "Не найден файл шрифта. Положите Roboto (Roboto-Regular.ttf и Roboto-Bold.ttf) "
            f"или Liberation Sans в папку {app_dir() / 'fonts'} и проверьте путь в @font-face.\n{lines}"
        )

    def preview_html(self) -> None:
        if self._generating():
            self.set_status("Дождитесь окончания создания чеков или нажмите «Остановить».")
            return
        focus = self.records.focus()
        numbers = self._selected_record_numbers()
        if focus and focus.isdigit() and int(focus) in numbers:
            numbers = [int(focus)]
        elif numbers:
            numbers = numbers[:1]
        job = self._prepare_job(numbers)
        if job is None:
            return
        record = job["records"][0]
        empty = empty_placeholders(record["values"], job["mapping"])
        if empty:
            messagebox.showerror(
                APP_TITLE,
                missing_field_message(record["number"], empty),
                parent=self,
            )
            return
        font_problem = self._font_problem(job["template"], job["template_path"])
        if font_problem:
            messagebox.showwarning(APP_TITLE, font_problem, parent=self)
        rendered = ensure_base(
            render_template(job["template"], escaped_row(record["values"], job["mapping"])),
            job["base_url"],
        )
        preview_path = Path(tempfile.gettempdir()) / PREVIEW_NAME
        try:
            preview_path.write_text(rendered, encoding="utf-8")
            webbrowser.open(preview_path.as_uri())
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Не удалось открыть предварительный HTML.\n{exc}", parent=self)
            return
        self.set_status(f"В браузере открыт предварительный HTML записи №{record['number']}. PDF не создавался.")

    def create_receipts(self) -> None:
        if self._generating():
            self.set_status("Создание чеков уже выполняется.")
            return
        job = self._prepare_job(self._selected_record_numbers())
        if job is None:
            return
        font_problem = self._font_problem(job["template"], job["template_path"])
        if font_problem:
            messagebox.showerror(APP_TITLE, font_problem, parent=self)
            return
        output_dir = self.output_directory()
        if output_dir is None:
            return
        job["output_dir"] = str(output_dir)
        job["open_pdf"] = bool(self.open_pdf_var.get())
        self.cancel_event.clear()
        self._set_busy(True)
        self.log_text.delete("1.0", "end")
        self.set_status(f"Создание чеков: 0 из {len(job['records'])}.")
        self.worker = threading.Thread(
            target=generate_receipts,
            args=(job, self.messages, self.cancel_event),
            daemon=True,
        )
        self.worker.start()

    def stop_generation(self) -> None:
        if self._generating():
            self.cancel_event.set()
            self.set_status("Остановка после текущего чека.")

    def _generating(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.create_button.state(["disabled"])
            self.preview_button.state(["disabled"])
            self.stop_button.state(["!disabled"])
        else:
            self.create_button.state(["!disabled"])
            self.preview_button.state(["!disabled"])
            self.stop_button.state(["disabled"])

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                message = self.messages.get_nowait()
                self._handle_message(message)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if not self._closed:
            self.after(100, self._poll)

    def _handle_message(self, message: tuple) -> None:
        kind = message[0]
        if kind == "progress":
            _kind, index, total, number = message
            self.set_status(f"Создаётся чек {index} из {total}. Запись №{number}.")
            return
        if kind == "log":
            self.log(message[1])
            return
        if kind == "open":
            path = Path(message[1])
            try:
                open_path(path)
            except OSError as exc:
                self.log(f"Не удалось открыть PDF {path.name}: {exc}")
            return
        if kind == "done":
            _kind, created, errors, cancelled = message
            self._set_busy(False)
            self.worker = None
            self._show_result(created, errors, cancelled)

    def _show_result(self, created: int, errors: list[str], cancelled: bool) -> None:
        title = "Создание остановлено." if cancelled else "Создание завершено."
        self.set_status(f"{title} Успешно создано чеков: {created}. Записей с ошибками: {len(errors)}.")
        dialog = tk.Toplevel(self)
        dialog.title("Результат")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("640x420")
        ttk.Label(dialog, text=title, padding=(12, 12, 12, 4), font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(dialog, text=f"Успешно создано чеков: {created}", padding=(12, 0)).pack(anchor="w")
        ttk.Label(dialog, text=f"Записей с ошибками: {len(errors)}", padding=(12, 0, 12, 8)).pack(anchor="w")
        if errors:
            box = tk.Text(dialog, wrap="word", height=12)
            box.pack(fill="both", expand=True, padx=12, pady=(0, 8))
            box.insert("1.0", "\n".join(errors))
            box.configure(state="disabled")
        else:
            ttk.Label(dialog, text="Ошибок по записям нет.", padding=(12, 0, 12, 8)).pack(anchor="w")
        buttons = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Открыть папку результата", command=self.open_output).pack(side="left")
        ttk.Button(buttons, text="Закрыть", command=dialog.destroy).pack(side="right")

    def _on_close(self) -> None:
        self._closed = True
        self.cancel_event.set()
        try:
            self._persist()
        except OSError:
            pass
        self.destroy()


def main() -> None:
    app = ReceiptApp()
    app.mainloop()


if __name__ == "__main__":
    main()
