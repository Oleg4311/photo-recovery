"""Exercise browsing and scan-only workflow without an output directory."""
import sys
import time
from pathlib import Path
import tkinter as tk

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from app import Application
from explorer_ui import logical_devices
from test_selected import fixture
from test_recovery import photo

root = tk.Tk()
root.withdraw()
app = Application(root)
assert app.output.get(), "Destination must be filled automatically"
app.install_devices([])
volumes = logical_devices()
assert any(row.get("mount", "").upper() == "C:\\" for row in volumes)
c_node = next(ident for ident, (_, mount) in app.browse_paths.items() if mount and mount.upper() == "C:\\")
app.source_tree.selection_set(c_node)
app.select_source()
assert app.source.get() == "\\\\.\\C:"
app.source_tree.focus(c_node)
app.expand_source()
deadline = time.monotonic() + 20
while not any(mount and mount.lower() == "c:\\users" for _, mount in app.browse_paths.values()):
    assert time.monotonic() < deadline, "C: folders were not loaded"
    root.update()
    time.sleep(.02)
app.options()
for child in root.winfo_children():
    if isinstance(child, tk.Toplevel):
        child.destroy()
data = ROOT / "benchmark/v07-gui"
data.mkdir(exist_ok=True)
image = data / "selected.img"
image.write_bytes(fixture(photo(192, 144), broken=True))
destination = data / ("not-created-by-scanning-" + str(time.time_ns()))
assert not destination.exists()
app.output.set(str(destination))
app.set_location(str(image), "C:\\Pictures\\Deleted")
app.load_catalogue()
deadline = time.monotonic() + 120
while app.running:
    assert time.monotonic() < deadline
    root.update()
    time.sleep(.02)
assert app.catalogue is not None
assert not destination.exists(), "Scanning must not create output"
app.catalog_query.set(".txt")
app.search_catalogue()
assert not app.catalog_tree.get_children(), "Other folders leaked into scope"
app.catalog_query.set("main.jpg")
app.search_catalogue()
assert len(app.catalog_tree.get_children()) == 1
app.catalog_tree.selection_set(app.catalog_tree.get_children()[0])
app.deep_selected()
while app.running:
    assert time.monotonic() < deadline
    root.update()
    time.sleep(.02)
assert destination.is_dir(), "Recovery should create destination automatically"
assert app.items and all(item["original_name"] == "main.jpg" for item in app.items.values())
root.deiconify()
root.update()
from PIL import ImageGrab
ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(),
                    root.winfo_rootx() + root.winfo_width(), root.winfo_rooty() + root.winfo_height())).save(data / "interface.png")
root.destroy()
print("C: browsing, default destination, scan without output, scoped deep recovery: OK")
