"""Exercise catalogue search, selected deep recovery, generic files and preview."""
import hashlib
import json
from pathlib import Path
import sys
import time
import tkinter as tk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from app import Application
from test_selected import fixture
from test_recovery import photo

data = ROOT / "benchmark/v05-gui"
data.mkdir(exist_ok=True)
payload = photo(192, 144)
image = data / "selected.img"
image.write_bytes(fixture(payload, broken=True))
root = tk.Tk()
root.withdraw()
app = Application(root)
app.source.set(str(image))
app.output.set(str(data))
app.window.set("1")

def wait():
    deadline = time.monotonic() + 120
    while app.running and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert not app.running, "GUI worker timed out"

app.load_catalogue()
wait()
assert app.catalogue is not None
root_id = next(e.id for e in app.catalogue.entries.values() if e.directory and not e.parent)
app.catalog_tree.focus(root_id)
app.expand_catalogue()
pictures = next(e for e in app.catalogue.entries.values() if e.name == "Pictures")
assert app.catalog_tree.parent(pictures.id) == root_id
app.catalog_tree.focus(pictures.id)
app.expand_catalogue()
deleted = next(e for e in app.catalogue.entries.values() if e.name == "Deleted")
assert app.catalog_tree.parent(deleted.id) == pictures.id
target = next(e for e in app.catalogue.entries.values() if e.name == "main.jpg")
app.catalog_query.set("main.jpg")
app.search_catalogue()
assert app.catalog_tree.exists(target.id)
app.catalog_tree.selection_set(target.id)
app.deep_selected()
wait()
expected = hashlib.sha256(payload).hexdigest()
key = next(key for key, row in app.items.items() if row["sha256"] == expected)
assert all(row["original_name"] == "main.jpg" for row in app.items.values())
app.tree.selection_set(key)
app.select()
root.update()
assert app.preview is not None
assert "Pictures/Deleted/main.jpg" in app.details.cget("text")
first_output = str(app.folder)
app.catalog_query.set("unrelated.txt")
app.search_catalogue()
target = next(e for e in app.catalogue.entries.values() if e.name == "unrelated.txt")
app.catalog_tree.selection_set(target.id)
app.deep_selected()
wait()
key = next(key for key, row in app.items.items() if row["category"] == "file_bytes")
app.tree.selection_set(key)
app.select()
assert app.preview is None
assert "не подтверждена" in app.details.cget("text")
(data / "result.json").write_text(json.dumps({"catalogue_and_search": "passed",
    "selected_jpeg_exact_sha256": expected, "preview": "passed", "unrelated_files_excluded": True,
    "generic_file_extraction": "passed", "unsupported_deep_format_label": "passed", "output": first_output}, indent=2), encoding="utf-8")
root.destroy()
print("GUI catalogue, targeted deep recovery, generic files and preview: OK")
