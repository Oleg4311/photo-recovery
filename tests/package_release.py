"""Package tested Windows binaries, source snapshot, evidence and SHA-256."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def add_tree(archive, folder, prefix=""):
    for path in sorted(folder.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            archive.write(path, str(Path(prefix) / path.relative_to(folder)))


if __name__ == "__main__":
    assert "OK" in (ROOT / "benchmark/v04/tests-final.log").read_text(encoding="utf-16"), "Final tests failed"
    assert "OK" in (ROOT / "benchmark/v04/packaged-smoke.txt").read_text(encoding="utf-8-sig")
    evidence = json.loads((ROOT / "benchmark/V04-EVIDENCE.json").read_text())
    assert all(sha(ROOT / path) == digest for path, digest in evidence["current_source_files"].items())
    RELEASE.mkdir(exist_ok=True)
    guide = r"""Photo Recovery 0.4 — Windows x64

Проверено на Windows 11, сборка ОС 26100. Python устанавливать не нужно.
Распакуйте архив целиком, сохраняя папки _internal рядом с EXE.
Запуск интерфейса: START.cmd или PhotoRecovery\PhotoRecovery.exe.
Для 3–5 частей JPEG с однообразными промежутками включите
«Собирать разрозненные JPEG и PNG» и задайте границу исходного кластера.
Это ограниченный поиск; сообщение о лимите означает, что проверены не все варианты.

CLI (PowerShell из распакованного каталога):
New-Item -ItemType Directory -Force results
.\PhotoRecoveryCLI\PhotoRecoveryCLI.exe .\demo\demo.img .\results --deep
.\PhotoRecoveryCLI\PhotoRecoveryCLI.exe C:\images\disk.img D:\recovered --deep --cluster-size 512 --search-mib 16
.\PhotoRecoveryCLI\PhotoRecoveryCLI.exe --version

Отчёт: V04-REPORT.md. Метрики каждого образа: V04-METRICS.csv.
Исходники, тесты и точные настройки сборки — в отдельном архиве sources.
Образы открываются на чтение; прямое открытие физических дисков не поддерживается.
Для реального носителя сохраняйте результаты на другой исправный накопитель.
SHA-256 и исходные диапазоны сборок записываются в report.jsonl каждой сессии.
Структурная проверка не доказывает подлинность; альтернативы требуют проверки.
"""
    (RELEASE / "README-WINDOWS.txt").write_text(guide, encoding="utf-8")
    (RELEASE / "START.cmd").write_text('@echo off\nstart "" "%~dp0PhotoRecovery\\PhotoRecovery.exe"\n', encoding="ascii")
    binary = RELEASE / "PhotoRecovery-0.4-windows-x64.zip"
    with zipfile.ZipFile(binary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        add_tree(archive, ROOT / "dist/PhotoRecovery", "PhotoRecovery")
        add_tree(archive, ROOT / "dist/PhotoRecoveryCLI", "PhotoRecoveryCLI")
        add_tree(archive, ROOT / "demo", "demo")
        for path in (RELEASE / "START.cmd", RELEASE / "README-WINDOWS.txt",
                     ROOT / "benchmark/V04-REPORT.md", ROOT / "benchmark/V04-METRICS.csv",
                     ROOT / "benchmark/V04-EVIDENCE.json"):
            archive.write(path, path.name)
    source = RELEASE / "PhotoRecovery-0.4-sources.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in ("src", "tests", "demo"):
            add_tree(archive, ROOT / name, name)
        for name in ("README.md", "requirements.txt", "requirements-benchmark.txt", "start-windows.cmd", "build-windows.ps1"):
            archive.write(ROOT / name, name)
        base = ROOT / "benchmark/baseline-0.3"
        for name in ("src", "tests"):
            add_tree(archive, base / name, f"benchmark/baseline-0.3/{name}")
        archive.write(base / "start-windows.cmd", "benchmark/baseline-0.3/start-windows.cmd")
        for path in sorted((ROOT / "benchmark").rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT)
            # Downloaded tools/images and recovered bytes remain in the workspace;
            # the reproducibility package includes their hashes and fetch scripts.
            if any(part in {"tools", "nist-images", "canon-source"} for part in relative.parts):
                continue
            if path.suffix.lower() in {".json", ".md", ".csv", ".log", ".txt"} or (
                path.parent == ROOT / "benchmark/v04" and path.suffix == ".img") or (
                path.parent == ROOT / "benchmark/v04" and path.name.startswith("source-")):
                archive.write(path, relative)
    manifest = {path.name: {"bytes": path.stat().st_size, "sha256": sha(path)} for path in (binary, source)}
    manifest["executables"] = {str(path.relative_to(ROOT)): sha(path) for path in
        (ROOT / "dist/PhotoRecovery/PhotoRecovery.exe", ROOT / "dist/PhotoRecoveryCLI/PhotoRecoveryCLI.exe")}
    (RELEASE / "SHA256.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
