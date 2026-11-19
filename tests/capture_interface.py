"""Capture only this application's window for a visual review."""
import ctypes
import sys
from pathlib import Path
import tkinter as tk
from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from app import Application
from catalog import build_catalogue

root = tk.Tk()
app = Application(root)
image = ROOT / "benchmark/v07-gui/selected.img"
app.set_location(str(image))
app.populate_catalogue(str(image), build_catalogue(image))
for ident in app.catalog_tree.get_children():
    app.catalog_tree.focus(ident)
    app.expand_catalogue()
    app.catalog_tree.item(ident, open=True)
    for child in app.catalog_tree.get_children(ident):
        app.catalog_tree.focus(child)
        app.expand_catalogue()
        app.catalog_tree.item(child, open=True)
root.update()
root.after(1500, root.quit)
root.mainloop()
hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
ImageGrab.grab(window=hwnd).save(ROOT / "benchmark/v07-gui/interface-reviewed.png")
root.destroy()
