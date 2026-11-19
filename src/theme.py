"""Consistent light palette for every ttk state."""
from tkinter import ttk

BACKGROUND = "#f3f4f6"
SURFACE = "#ffffff"
TEXT = "#202833"
SECONDARY = "#526171"
BLUE = "#245c9c"


def configure(root):
    root.configure(background=BACKGROUND)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=("Segoe UI", 10), background=BACKGROUND, foreground=TEXT,
                    bordercolor="#c5ccd4", lightcolor=BACKGROUND, darkcolor=BACKGROUND)
    for name in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton", "TNotebook"):
        style.configure(name, background=BACKGROUND, foreground=TEXT)
    style.configure("Muted.TLabel", foreground=SECONDARY)
    style.configure("Panel.TFrame", background=SURFACE)
    style.configure("Panel.TLabel", background=SURFACE, foreground=SECONDARY)
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(name, fieldbackground=SURFACE, foreground=TEXT, insertcolor=TEXT,
                        selectbackground=BLUE, selectforeground="white", padding=5)
        style.map(name, fieldbackground=[("disabled", "#e8ebef"), ("readonly", SURFACE)],
                  foreground=[("disabled", "#697583"), ("readonly", TEXT)])
    for name in ("TButton", "TMenubutton"):
        style.configure(name, background="#e8edf3", foreground=TEXT, padding=(10, 7), borderwidth=1)
        style.map(name, background=[("disabled", "#eceff2"), ("pressed", "#d0dbea"), ("active", "#dce6f2")],
                  foreground=[("disabled", "#697583"), ("active", TEXT)])
    style.configure("Accent.TButton", background=BLUE, foreground="white")
    style.map("Accent.TButton", background=[("disabled", "#e0e5eb"), ("pressed", "#194777"), ("active", "#2e6cb1")],
              foreground=[("disabled", "#697583"), ("!disabled", "white")])
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                    rowheight=29, borderwidth=1)
    style.configure("Treeview.Heading", background="#e8edf3", foreground=TEXT, padding=(7, 7), relief="flat")
    style.map("Treeview.Heading", background=[("active", "#dce6f2")])
    style.map("Treeview", background=[("selected", "#d6e6f8")], foreground=[("selected", "#133d69")])
    style.configure("TNotebook.Tab", background="#e3e8ee", foreground=TEXT, padding=(12, 8))
    style.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", "#dce6f2")],
              foreground=[("selected", "#163f6b"), ("active", TEXT)])
    style.configure("TPanedwindow", background="#dce1e7")
    style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#e2e7ed", borderwidth=0)
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background="#c3ccd7", troughcolor="#edf0f4", arrowcolor=SECONDARY,
                        borderwidth=0, relief="flat")
        style.map(name, background=[("active", "#9eafc3")])
    root.option_add("*Menu.background", SURFACE)
    root.option_add("*Menu.foreground", TEXT)
    root.option_add("*Menu.activeBackground", "#d6e6f8")
    root.option_add("*Menu.activeForeground", "#133d69")
    return style
