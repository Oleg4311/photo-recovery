"""Disk explorer controls; browsing never creates recovery output."""
import ctypes
import os
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def default_destination():
    return str(Path.home() / "Photo Recovery" / "Recovered")


def logical_devices():
    if os.name != "nt":
        return []
    kernel = ctypes.windll.kernel32
    mask = kernel.GetLogicalDrives()
    rows = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        letter = chr(65 + index) + ":"
        mount = letter + "\\"
        kind = kernel.GetDriveTypeW(mount)
        if kind not in (2, 3, 4):
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        kernel.GetVolumeInformationW(mount, label, 261, None, None, None, fs, 261)
        rows.append({"path": "\\\\.\\" + letter, "mount": mount,
                     "label": f"{letter}  {label.value or ('Съёмный диск' if kind == 2 else 'Диск')}  {fs.value}"})
    return rows


def create_workspace(app):
    root = app.root
    root.geometry("1400x850")
    root.minsize(1100, 650)
    app.browse_paths = {}
    app.folder_scope = None
    app.source = tk.StringVar()
    app.output = tk.StringVar(value=default_destination())
    app.copies = tk.StringVar()
    app.map = tk.StringVar()
    app.metadata = tk.BooleanVar(value=True)
    app.raw = tk.BooleanVar(value=True)
    app.all_types = tk.BooleanVar(value=True)
    app.max_mib = tk.StringVar(value="4096")
    app.deep = tk.BooleanVar(value=False)
    app.cluster = tk.StringVar(value="512")
    app.window = tk.StringVar(value="2")
    outer = ttk.Frame(root, padding=10)
    outer.pack(fill="both", expand=True)
    tools = ttk.Frame(outer)
    tools.pack(fill="x", pady=(0, 8))
    for text, command in (("Обновить диски", app.refresh_devices),
                          ("Открыть образ…", app.choose_source),
                          ("Выбрать папку…", app.choose_folder)):
        ttk.Button(tools, text=text, command=command).pack(side="left", padx=2)
    app.start_button = ttk.Button(tools, text="Сканировать", style="Accent.TButton", command=app.load_catalogue)
    app.start_button.pack(side="left", padx=(14, 2))
    ttk.Button(tools, text="Восстановить выбранное", command=app.recover_selected).pack(side="left", padx=2)
    ttk.Button(tools, text="Глубоко восстановить", command=app.deep_selected).pack(side="left", padx=2)
    app.stop_button = ttk.Button(tools, text="Стоп", command=app.stop.set, state="disabled")
    app.stop_button.pack(side="left", padx=2)
    menu = tk.Menu(root, tearoff=False)
    menu.add_command(label="Доступ администратора для чтения дисков", command=app.elevate)
    menu.add_command(label="Восстановить весь диск по метаданным и сигнатурам", command=app.recover_disk)
    menu.add_command(label="Дополнительные настройки…", command=app.options)
    button = ttk.Menubutton(tools, text="Инструменты", menu=menu)
    button.pack(side="right")
    pathbar = ttk.Frame(outer)
    pathbar.pack(fill="x", pady=(0, 6))
    ttk.Label(pathbar, text="Источник:").pack(side="left")
    ttk.Label(pathbar, textvariable=app.source).pack(side="left", padx=8)
    app.location = tk.StringVar(value="Выберите диск слева или откройте папку")
    ttk.Label(outer, textvariable=app.location, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
    content = ttk.Panedwindow(outer, orient="horizontal")
    content.pack(fill="both", expand=True)
    navigation = ttk.Frame(content)
    content.add(navigation, weight=1)
    ttk.Label(navigation, text="ДИСКИ И ПАПКИ").pack(anchor="w", pady=6)
    app.source_tree = ttk.Treeview(navigation, show="tree", selectmode="browse")
    app.source_tree.column("#0", width=270)
    scroll = ttk.Scrollbar(navigation, command=app.source_tree.yview)
    app.source_tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    app.source_tree.pack(fill="both", expand=True)
    app.source_tree.bind("<<TreeviewOpen>>", app.expand_source)
    app.source_tree.bind("<<TreeviewSelect>>", app.select_source)
    left = ttk.Frame(content)
    right = ttk.Frame(content, padding=10, style="Panel.TFrame")
    content.add(left, weight=3)
    content.add(right, weight=1)
    footer = ttk.Frame(outer)
    footer.pack(fill="x", pady=(8, 0))
    ttk.Label(footer, text="Сохранять в:").pack(side="left")
    ttk.Entry(footer, textvariable=app.output).pack(side="left", fill="x", expand=True, padx=6)
    ttk.Button(footer, text="Изменить…", command=app.choose_output).pack(side="left")
    app.open_button = ttk.Button(footer, text="Открыть результаты", command=app.open_folder, state="disabled")
    app.open_button.pack(side="left", padx=6)
    app.bar = ttk.Progressbar(outer, maximum=100)
    app.bar.pack(fill="x", pady=(7, 3))
    app.status = tk.StringVar(value="Просмотр папок и сканирование не требуют выбора места сохранения.")
    ttk.Label(outer, textvariable=app.status, wraplength=1250).pack(anchor="w")
    return outer, content, left, right


class ExplorerMixin:
    def install_devices(self, rows):
        if self.running:
            return
        tree = self.source_tree
        tree.delete(*tree.get_children())
        self.browse_paths.clear()
        volumes = logical_devices()
        seen = {row["path"].casefold() for row in volumes}
        volumes += [row for row in rows if row["path"].casefold() not in seen]
        for index, row in enumerate(volumes):
            ident = "device-" + str(index)
            mount = row.get("mount")
            tree.insert("", "end", iid=ident, text=row["label"])
            self.browse_paths[ident] = (row["path"], mount)
            if mount:
                tree.insert(ident, "end", text="…")
        self.status.set("Выберите диск или папку. «Сканировать» ищет удалённые записи без сохранения файлов.")

    def select_source(self, *_):
        selection = self.source_tree.selection()
        if self.running or not selection or selection[0] not in self.browse_paths:
            return
        source, folder = self.browse_paths[selection[0]]
        self.set_location(source, folder)

    def set_location(self, source, folder=None):
        self.source.set(source)
        self.folder_scope = folder if folder and Path(folder).parent != Path(folder) else None
        self.location.set(folder or source)
        if self.catalogue and self.catalogue_source == source:
            self.populate_catalogue(source, self.catalogue)
        else:
            self.catalog_tree.delete(*self.catalog_tree.get_children())
            self.details.configure(text="Нажмите «Сканировать», чтобы найти удалённые файлы. Папку можно выбрать до сканирования.")

    def expand_source(self, *_):
        ident = self.source_tree.focus()
        if ident not in self.browse_paths:
            return
        source, folder = self.browse_paths[ident]
        if not folder:
            return
        children = self.source_tree.get_children(ident)
        if children and children[0] in self.browse_paths:
            return
        self.source_tree.delete(*children)
        def work():
            try:
                with os.scandir(folder) as entries:
                    paths = sorted((e.path for e in entries if e.is_dir(follow_symlinks=False)), key=str.casefold)
                self.messages.put(("folders", (ident, source, paths, None)))
            except OSError as error:
                self.messages.put(("folders", (ident, source, [], str(error))))
        threading.Thread(target=work, daemon=True).start()

    def install_folders(self, value):
        parent, source, paths, error = value
        if not self.source_tree.exists(parent):
            return
        if error:
            self.status.set("Не удалось открыть папку: " + error)
        for path in paths:
            ident = self.source_tree.insert(parent, "end", text=Path(path).name)
            self.browse_paths[ident] = (source, path)
            self.source_tree.insert(ident, "end", text="…")

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Папка для поиска удалённых файлов")
        if not folder:
            return
        path = Path(folder)
        if not path.drive or path.drive.startswith("\\\\"):
            messagebox.showinfo("Источник", "Для поиска удалённых записей выберите папку на локальном диске или флешке.")
            return
        self.set_location("\\\\.\\" + path.drive, str(path))

    def recover_selected(self):
        self.scope.set("selected")
        self.deep.set(False)
        self.start()

    def recover_disk(self):
        self.scope.set("disk")
        self.deep.set(True)
        self.start()

    def options(self):
        window = tk.Toplevel(self.root)
        window.title("Дополнительные настройки")
        frame = ttk.Frame(window, padding=15)
        frame.pack(fill="both", expand=True)
        for row, (label, variable) in enumerate((("Лимит файла, МиБ", self.max_mib),
                  ("Размер кластера для поиска по сигнатурам", self.cluster), ("Окно глубокой сборки, МиБ", self.window))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(frame, textvariable=variable, width=12).grid(row=row, column=1, padx=8)
        ttk.Label(frame, text="Копии / кэш").grid(row=3, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.copies).grid(row=3, column=1)
        ttk.Button(frame, text="Выбрать…", command=self.choose_copies).grid(row=3, column=2)
        ttk.Button(frame, text="Готово", command=window.destroy).grid(row=5, column=1, pady=10)

    def in_folder_scope(self, entry):
        if not self.folder_scope:
            return True
        relative = Path(self.folder_scope).relative_to(Path(self.folder_scope).anchor).as_posix().strip("/").casefold()
        path = entry.path.replace("\\", "/").strip("/").casefold()
        return entry.path_known and (path == relative or path.startswith(relative + "/"))
