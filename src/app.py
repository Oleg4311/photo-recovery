"""Windows-first desktop interface for the photo recovery engine."""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import ctypes
import argparse
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

from recovery import scan
from catalog import build_catalogue
from sources import is_device, list_devices, open_source
from explorer_ui import ExplorerMixin, create_workspace, logical_devices, default_destination

BG = "#f3f4f6"
PANEL = "#ffffff"
TEXT = "#202833"
MUTED = "#526171"
ACCENT = "#7cafff"
LABELS = {
    "decoded": "Декодируется",
    "exif_thumbnail": "Миниатюра EXIF",
    "repaired_eoi": "Добавлен конец JPEG",
    "partial_jpeg": "Часть кадра JPEG",
    "cache_thumbnail": "Превью из кэша",
    "assembled_jpeg": "Собранный JPEG",
    "assembled_png": "Собранный PNG",
    "ambiguous_jpeg": "Несколько вариантов",
    "file_bytes": "Файл: целостность не проверена",
    "assembled_file": "Собранный архив / Office",
}
METHODS = {
    "ntfs": "NTFS",
    "fat32": "FAT32",
    "exfat": "exFAT",
    "signature": "Секторы",
    "existing_copy": "Копия",
    "windows_cache": "Кэш",
    "jpeg_fragments": "Фрагменты",
    "png_fragments": "Фрагменты",
    "zip_fragments": "Фрагменты ZIP",
}
ALLOCATION = {"free": "кластеры свободны", "allocated": "кластеры заняты",
              "unknown": "занятость неизвестна", "resident": "байты в записи MFT"}
MAPPING = {"contiguous_guess": "предполагаемая непрерывная запись",
           "contiguous_declared": "непрерывная запись по метаданным", "fat_chain": "цепочка FAT",
           "mft": "размещение из MFT", "jpeg_entropy": "структура JPEG проверена", "png_crc": "контрольные суммы PNG проверены"}


class Application(ExplorerMixin):
    def __init__(self, root):
        self.root = root
        root.title("Photo Recovery 0.7 · Восстановление файлов")
        root.geometry("1240x940")
        root.minsize(930, 720)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.messages = queue.Queue(maxsize=320)
        self.stop = threading.Event()
        self.running = False
        self.folder = None
        self.items = {}
        self.next_item = 0
        self.progress_state = ""
        self.preview = None
        self.catalogue = None
        self.catalogue_source = ""
        self.devices = {}
        self.scope = tk.StringVar(value="disk")

        from theme import configure
        configure(root)

        outer, content, left, right = create_workspace(self)

        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill="both", expand=True)
        self.catalog_tab = ttk.Frame(self.tabs)
        left = ttk.Frame(self.tabs)
        self.result_tab = left
        self.tabs.add(self.catalog_tab, text="Удалённые файлы и папки")
        self.tabs.add(left, text="Результаты восстановления")
        ttk.Label(self.catalog_tab, text="Выберите файл или папку. Ctrl / Shift — несколько записей. Число файлов — найденные записи, не гарантия восстановления.", wraplength=680).pack(anchor="w", pady=6)
        searchbar = ttk.Frame(self.catalog_tab)
        searchbar.pack(fill="x", pady=(0, 6))
        self.catalog_query = tk.StringVar()
        query_entry = ttk.Entry(searchbar, textvariable=self.catalog_query)
        query_entry.pack(side="left", fill="x", expand=True)
        query_entry.bind("<Return>", lambda *_: self.search_catalogue())
        ttk.Button(searchbar, text="Найти по имени / пути", command=self.search_catalogue).pack(side="left", padx=6)
        self.catalog_tree = ttk.Treeview(self.catalog_tab, columns=("state", "bytes"), selectmode="extended")
        self.catalog_tree.heading("#0", text="Папка / файл")
        self.catalog_tree.heading("state", text="Состояние")
        self.catalog_tree.heading("bytes", text="Размер / файлов")
        self.catalog_tree.column("#0", width=330)
        self.catalog_tree.column("state", width=125)
        self.catalog_tree.column("bytes", width=110)
        catalog_scroll = ttk.Scrollbar(self.catalog_tab, orient="vertical", command=self.catalog_tree.yview)
        self.catalog_tree.configure(yscrollcommand=catalog_scroll.set)
        catalog_scroll.pack(side="right", fill="y")
        self.catalog_tree.pack(side="left", fill="both", expand=True)
        self.catalog_tree.tag_configure("deleted", foreground="#a32727")
        self.catalog_tree.bind("<<TreeviewSelect>>", self.describe_target)
        self.catalog_tree.bind("<<TreeviewOpen>>", self.expand_catalogue)

        filters = ttk.Frame(left)
        filters.pack(fill="x", pady=(0, 5))
        ttk.Label(filters, text="Показать").pack(side="left", padx=(0, 8))
        self.filter = tk.StringVar(value="Все результаты")
        filter_menu = ttk.Combobox(filters, state="readonly", textvariable=self.filter,
                                   values=("Все результаты", *LABELS.values()), width=23)
        filter_menu.pack(side="left")
        filter_menu.bind("<<ComboboxSelected>>", self.filter_results)
        table = ttk.Frame(left)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("name", "type", "size", "method"), show="headings")
        for column, label, width in (
            ("name", "Файл", 205), ("type", "Результат", 145), ("size", "Пиксели", 82), ("method", "Источник", 78)
        ):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=55, stretch=column in {"name", "type"})
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind("<Double-Button-1>", self.open_file)
        self.image_label = ttk.Label(right, text="Выберите найденное фото", anchor="center", style="Panel.TLabel")
        self.image_label.pack(fill="both", expand=True)
        self.details = ttk.Label(right, text="", justify="left", wraplength=315, style="Panel.TLabel")
        self.details.pack(side="bottom", fill="x")
        self.root.after(65, self.poll)
        self.root.after(100, self.refresh_devices)

    def refresh_devices(self):
        def work():
            try:
                self.messages.put(("devices", list_devices()))
            except Exception as error:
                self.messages.put(("devices", logical_devices()))
        threading.Thread(target=work, daemon=True).start()

    def elevate(self):
        if self.running:
            messagebox.showinfo("Доступ к дискам", "Сначала остановите текущий поиск.")
            return
        if os.name != "nt":
            return
        if ctypes.windll.shell32.IsUserAnAdmin():
            messagebox.showinfo("Доступ к дискам", "Приложение уже запущено с правами администратора.")
            return
        args = [] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())]
        args += ["--source", self.source.get(), "--destination", self.output.get()]
        if self.folder_scope:
            args += ["--folder", self.folder_scope]
        execute = ctypes.windll.shell32.ShellExecuteW
        execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        execute.restype = ctypes.c_void_p
        result = execute(None, "runas", sys.executable, subprocess.list2cmdline(args), None, 1)
        if result and result > 32:
            self.root.destroy()
        else:
            self.status.set("Windows не предоставила доступ администратора.")

    def load_catalogue(self):
        if self.running:
            return
        source = self.source.get().strip()
        if is_device(source) and os.name == "nt" and not ctypes.windll.shell32.IsUserAnAdmin():
            self.elevate()
            return
        if not source:
            messagebox.showinfo("Удалённые записи", "Выберите диск или образ.")
            return
        self.stop.clear()
        self.running = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.catalog_tree.delete(*self.catalog_tree.get_children())
        self.catalogue = None
        self.status.set("Читаю список удалённых записей…")
        def work():
            try:
                def progress(stage, current, total):
                    if current % 1024 == 0:
                        self.messages.put(("progress", (stage, current, total)))
                folder = None
                if self.folder_scope:
                    folder = Path(self.folder_scope).relative_to(Path(self.folder_scope).anchor).as_posix()
                result = build_catalogue(source, self.stop.is_set, progress, folder=folder)
                self.messages.put(("catalogue", (source, result)))
            except Exception as error:
                self.messages.put(("error", str(error)))
        threading.Thread(target=work, daemon=True).start()

    def populate_catalogue(self, source, catalogue):
        self.catalog_tree.delete(*self.catalog_tree.get_children())
        self.catalogue, self.catalogue_source = catalogue, source
        entries = catalogue.entries
        visible = {e.id for e in entries.values() if e.deleted or (e.directory and e.children)}
        if self.folder_scope:
            relative = Path(self.folder_scope).relative_to(Path(self.folder_scope).anchor).as_posix().strip("/").casefold()
            visible = {ident for ident in visible if entries[ident].path_known and
                       (entries[ident].path.replace("\\", "/").strip("/").casefold() == relative or
                        entries[ident].path.replace("\\", "/").strip("/").casefold().startswith(relative + "/"))}
        for ident in list(visible):
            parent, seen = entries[ident].parent, set()
            while parent in entries and parent not in seen:
                visible.add(parent)
                seen.add(parent)
                parent = entries[parent].parent
        self.catalog_children = {}
        for ident in visible:
            entry = entries[ident]
            self.catalog_children.setdefault(entry.parent if entry.parent in visible else "", []).append(entry)
        for children in self.catalog_children.values():
            children.sort(key=lambda e: (not e.directory, e.name.casefold()))
        self.insert_catalog_children("")
        self.tabs.select(self.catalog_tab)
        self.status.set(("Список остановлен. " if catalogue.cancelled else "Список готов. ") +
                        f"Удалённых файлов: {sum(e.deleted and not e.directory for e in entries.values())}. " +
                        ("; ".join(catalogue.warnings) if catalogue.warnings else "Выберите файл или папку для восстановления."))

    def insert_catalog_children(self, parent):
        for entry in self.catalog_children.get(parent, []):
            if self.catalog_tree.exists(entry.id):
                continue
            self.catalog_tree.insert(parent, "end", iid=entry.id, text=entry.name,
                values=("Удалена" if entry.directory and entry.deleted else "Удалён" if entry.deleted else "Существует",
                        f"{entry.children} файлов" if entry.directory else f"{entry.size:,} байт"),
                tags=("deleted",) if entry.deleted else ())
            if self.catalog_children.get(entry.id):
                self.catalog_tree.insert(entry.id, "end", iid="placeholder-" + entry.id, text="…")

    def expand_catalogue(self, *_):
        ident = self.catalog_tree.focus()
        placeholder = "placeholder-" + ident
        if self.catalog_tree.exists(placeholder):
            self.catalog_tree.delete(placeholder)
            self.insert_catalog_children(ident)

    def search_catalogue(self):
        if not self.catalogue or self.running:
            return
        query = self.catalog_query.get().strip().casefold()
        if not query:
            self.populate_catalogue(self.catalogue_source, self.catalogue)
            return
        self.catalog_tree.delete(*self.catalog_tree.get_children())
        self.catalog_children = {}
        count = 0
        for entry in self.catalogue.entries.values():
            if not entry.deleted or not self.in_folder_scope(entry) or query not in entry.path.casefold():
                continue
            self.catalog_tree.insert("", "end", iid=entry.id, text=entry.path,
                values=("Удалена" if entry.directory else "Удалён", f"{entry.children} файлов" if entry.directory else f"{entry.size:,} байт"),
                tags=("deleted",))
            count += 1
            if count == 3000:
                break
        self.status.set(f"Найдено {count} записей." + (" Уточните имя: показаны первые 3000." if count == 3000 else ""))

    def describe_target(self, *_):
        if not self.catalogue or not self.catalog_tree.selection():
            return
        entry = self.catalogue.entries[self.catalog_tree.selection()[0]]
        self.details.configure(text=entry.path + ("\nИсходный путь восстановлен не полностью." if not entry.path_known else "") +
            "\nГлубокая сборка: JPEG, PNG, ZIP и Office в ZIP. Другие типы извлекаются по сохранившемуся размещению.")

    def deep_selected(self):
        if self.running:
            return
        self.scope.set("selected")
        self.deep.set(True)
        self.start()

    def path_row(self, frame, row, label, choose):
        variable = tk.StringVar()
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
        ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(frame, text="Выбрать", command=choose).grid(row=row, column=2, padx=(8, 0), pady=3)
        return variable

    def choose_source(self):
        selected = filedialog.askopenfilename(filetypes=[("Образы диска", "*.raw *.dd *.img *.bin"), ("Все файлы", "*.*")])
        if selected:
            self.set_location(selected)

    def choose_output(self):
        selected = filedialog.askdirectory()
        if selected:
            self.output.set(selected)

    def choose_copies(self):
        selected = filedialog.askdirectory()
        if selected:
            self.copies.set(selected)

    def choose_map(self):
        selected = filedialog.askopenfilename(filetypes=[("Карты ddrescue", "*.map *.log"), ("Все файлы", "*.*")])
        if selected:
            self.map.set(selected)

    def start(self):
        if self.running:
            return
        try:
            limit = int(self.max_mib.get())
            cluster, window = int(self.cluster.get()), int(self.window.get())
        except ValueError:
            messagebox.showerror("Настройки поиска", "Проверьте числовые параметры поиска.")
            return
        source, destination, copies = (var.get().strip() for var in (self.source, self.output, self.copies))
        metadata, raw, deep = self.metadata.get(), self.raw.get(), self.deep.get()
        selected_entries = None
        all_types = self.all_types.get()
        if self.scope.get() == "selected":
            selected_entries = list(self.catalog_tree.selection())
            if self.catalogue and self.catalogue_source == source and self.folder_scope:
                if selected_entries:
                    targets = self.catalogue.targets(selected_entries)
                    selected_entries = [e.id for e in targets.values() if self.in_folder_scope(e)]
                else:
                    selected_entries = [e.id for e in self.catalogue.entries.values()
                                        if e.deleted and not e.directory and self.in_folder_scope(e)]
            if not self.catalogue or self.catalogue_source != source or not selected_entries:
                messagebox.showinfo("Выбор файлов", "Сначала получите список для этого источника и выберите файл или папку.")
                return
            metadata, raw, copies = True, False, ""
        mapfile = self.map.get().strip()
        if not (source or copies):
            messagebox.showerror("Настройки поиска", "Выберите образ или папку с копиями и укажите, куда сохранить результаты.")
            return

        try:
            target = Path(destination or default_destination()).expanduser().resolve()
            ancestor = target
            while not ancestor.exists():
                ancestor = ancestor.parent
            if is_device(source):
                with open_source(source) as device:
                    device.guard_destination(ancestor)
            target.mkdir(parents=True, exist_ok=True)
            destination = str(target)
            self.output.set(destination)
        except Exception as error:
            messagebox.showerror("Папка сохранения", str(error))
            return

        self.stop.clear()
        self.running = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.folder = None
        self.progress_state = ""
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.next_item = 0
        self.filter.set("Все результаты")
        self.image_label.configure(image="", text="Выберите найденное фото")
        self.preview = None
        self.details.configure(text="")
        self.status.set("Читаю образ…")
        self.tabs.select(self.result_tab)

        def work():
            last_update = 0.0
            last_stage = ""

            def progress(stage, current, total):
                nonlocal last_update, last_stage
                now = time.monotonic()
                if stage != last_stage or (total and current == total) or now - last_update >= 0.15:
                    self.messages.put(("progress", (stage, current, total)))
                    last_update, last_stage = now, stage

            def event(entry):
                if entry["event"] in {"start", "saved", "warning", "volume_error", "targeted_search"}:
                    self.messages.put(("event", entry))

            try:
                summary = scan(Path(source) if source else None, Path(destination), limit, metadata=metadata,
                               raw=raw, copies=Path(copies) if copies else None, deep=deep,
                               cluster_size=cluster, search_mib=window, mapfile=Path(mapfile) if mapfile else None,
                               cancelled=self.stop.is_set, progress=progress, on_event=event,
                               selected_entries=selected_entries, all_types=all_types)
                self.messages.put(("done", summary))
            except Exception as error:
                self.messages.put(("error", str(error)))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        for _ in range(80):
            try:
                event, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if event == "devices":
                self.install_devices(value)
                continue
            elif event == "folders":
                self.install_folders(value)
                continue
            elif event == "notice":
                if not self.running:
                    self.status.set(value)
                continue
            elif event == "catalogue":
                self.running = False
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.bar.stop()
                self.bar.configure(mode="determinate", value=0)
                self.populate_catalogue(*value)
                continue
            elif event == "progress":
                stage, current, total = value
                if total:
                    if self.progress_state == "indeterminate":
                        self.bar.stop()
                    self.bar.configure(mode="determinate", value=min(100, current * 100 / total))
                    self.progress_state = "determinate"
                elif self.progress_state != "indeterminate":
                    self.bar.configure(mode="indeterminate")
                    self.bar.start(18)
                    self.progress_state = "indeterminate"
                self.status.set(f"{stage} · {current * 100 / total:.1f}%" if total else f"{stage} · {current}")
            elif event == "event":
                if value["event"] == "start":
                    self.folder = Path(value["folder"])
                    self.open_button.configure(state="normal")
                elif value["event"] == "saved":
                    self.next_item += 1
                    key = str(self.next_item)
                    self.items[key] = value
                    if self.visible(value):
                        self.append_row(key, value)
                elif value["event"] == "targeted_search":
                    if value.get("outcome") in {"not_reconstructed", "metadata_only", "metadata_unreadable"}:
                        self.status.set("Для выбранного файла полная глубокая сборка не подтверждена. Подробности — в журнале.")
                else:
                    self.status.set(value["reason"])
            else:
                self.bar.stop()
                self.bar.configure(mode="determinate", value=0)
                self.running = False
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                if event == "done":
                    self.folder = value.folder
                    self.open_button.configure(state="normal")
                    result = "Поиск остановлен" if value.cancelled else "Поиск завершён"
                    self.status.set(f"{result} · файлов {value.files} · фото {value.decoded} · исправлено {value.repaired} · превью "
                                    f"{value.thumbnails + value.cache_previews} · частично {value.partial} · "
                                    f"собрано {value.assembled + value.assembled_other} · неоднозначных {value.ambiguous} · "
                                    f"не подтверждено глубоко {value.unresolved} · лимитов {value.assembly_capped} · ошибок томов {value.failed_volumes}")
                    self.bar.configure(value=0 if value.cancelled else 100)
                    if self.tree.selection():
                        self.select()
                else:
                    self.status.set("Поиск прерван из-за ошибки.")
                    messagebox.showerror("Ошибка поиска", value)
        self.root.after(65, self.poll)

    def visible(self, item):
        return self.filter.get() in {"Все результаты", LABELS[item["category"]]}

    def append_row(self, key, item):
        name = item.get("original_path") or item["original_name"] or Path(item["file"]).name
        self.tree.insert("", "end", iid=key, values=(
            name, LABELS[item["category"]], f'{item["width"]} × {item["height"]}' if item["width"] else "—", METHODS[item["method"]]
        ))

    def filter_results(self, _event=None):
        self.tree.delete(*self.tree.get_children())
        self.preview = None
        self.image_label.configure(text="Выберите найденное фото", image="")
        self.details.configure(text="")
        for key, item in self.items.items():
            if self.visible(item):
                self.append_row(key, item)

    def select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        item = self.items[selection[0]]
        offset = item["image_offset"]
        lines = [LABELS[item["category"]], METHODS[item["method"]]]
        if item.get("original_path"):
            lines.append(item["original_path"])
        if item.get("integrity") == "not_verified":
            lines.append("Извлечены байты по записи файловой системы. Целостность файла не подтверждена.")
        elif item.get("integrity") == "zip_crc":
            lines.append("Проверены структура ZIP и CRC всех элементов. Подлинность оригинала не гарантирована.")
        if item["allocation"]:
            lines.append(ALLOCATION.get(item["allocation"], item["allocation"]))
        if item.get("mapping") in MAPPING:
            lines.append(MAPPING[item["mapping"]])
        if offset is not None:
            lines.append(f"Смещение в образе · 0x{offset:X}")
        if item.get("search_capped"):
            lines.append("Часть вариантов не проверена из-за лимита поиска.")
        if item.get("candidate_count", 0) > 1:
            lines.append("Найдено несколько подходящих сборок.")
        if item.get("confidence_reason"):
            lines.append("Проверены структура baseline JPEG и исходные диапазоны. Подлинность не гарантирована.")
        if item.get("mapping") in {"jpeg_entropy", "png_crc"}:
            lines.extend(f"0x{start:X} · {length} байт" for start, length in item["extents"])
        if not self.folder:
            lines.append("Предпросмотр будет доступен после поиска.")
        self.details.configure(text="\n".join(lines))
        if not self.folder:
            return
        if not item["width"]:
            self.image_label.configure(text="Для этого типа файла предпросмотр не поддерживается.\nДвойное нажатие покажет файл в Проводнике.", image="")
            self.preview = None
            return
        try:
            with Image.open(self.folder / item["file"]) as image:
                image.thumbnail((max(240, self.image_label.winfo_width() - 8),
                                 max(140, self.image_label.winfo_height() - 8)))
                self.preview = ImageTk.PhotoImage(ImageOps.exif_transpose(image))
            self.image_label.configure(text="", image=self.preview)
        except (OSError, ValueError):
            self.image_label.configure(text="Предпросмотр недоступен", image="")
            self.preview = None

    @staticmethod
    def show(path):
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except OSError as error:
            messagebox.showerror("Не удалось открыть", str(error))

    def open_folder(self):
        if self.folder:
            self.show(self.folder)

    def open_file(self, _event=None):
        selection = self.tree.selection()
        if self.folder and selection:
            item = self.items[selection[0]]
            path = self.folder / item["file"]
            if item["category"] in {"file_bytes", "assembled_file"}:
                if os.name == "nt":
                    subprocess.Popen(["explorer.exe", "/select,", str(path)])
                else:
                    self.show(path.parent)
            else:
                self.show(path)

    def close(self):
        if self.running:
            if not messagebox.askyesno("Идёт поиск", "Остановить поиск? Уже сохранённые файлы останутся в папке результатов."):
                return
            self.stop.set()
            self.root.after(100, self.close_when_done)
        else:
            self.root.destroy()

    def close_when_done(self):
        if self.running:
            self.root.after(100, self.close_when_done)
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="")
    parser.add_argument("--destination", default="")
    parser.add_argument("--folder", default="")
    args = parser.parse_args()
    root = tk.Tk()
    application = Application(root)
    application.source.set(args.source)
    if args.source:
        application.set_location(args.source, args.folder or None)
    if args.destination:
        application.output.set(args.destination)
    root.mainloop()


if __name__ == "__main__":
    main()
