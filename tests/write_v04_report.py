"""Regenerate release tables from saved campaign measurements, without rescanning."""
import csv
import hashlib
import json
from pathlib import Path
import platform

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmark"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    groups = {}
    groups["NIST"] = [r for r in json.loads((BENCH / "nist-v04-fixed/results.json").read_text()) if r["engine"].startswith("PhotoRec")]
    groups["NIST"] += json.loads((BENCH / "nist-release/results.json").read_text())
    groups["Canon"] = [r for r in json.loads((BENCH / "canon-v04/campaign/results.json").read_text()) if r["engine"].startswith("PhotoRec")]
    groups["Canon"] += json.loads((BENCH / "canon-final/results.json").read_text())
    groups["Synthetic"] = json.loads((BENCH / "synthetic-release/results.json").read_text())
    definitions = {
        "NIST": json.loads((BENCH / "nist-graphic-manifest.json").read_text()),
        "Canon": json.loads((BENCH / "canon-v04/manifest.json").read_text()),
        "Synthetic": json.loads((BENCH / "v04/manifest.json").read_text()),
    }
    flat = []
    for dataset, rows in groups.items():
        for row in rows:
            ranks = row.get("ranks")
            case = next(c for c in definitions[dataset]["images"] if c["name"] == row["image"])
            unique_names = {t["sha256"]: t["name"] for t in case["intact"]}.values()
            unique_ranks = [ranks.get(name) for name in unique_names] if ranks is not None else None
            categories = row.get("categories", {})
            alternatives = None
            if row["engine"] in {"0.3", "0.4"}:
                dest = Path(row["command"][3])
                reports = sorted(dest.glob("recovery-*/report.jsonl"), key=lambda p: p.stat().st_mtime_ns)
                events = [json.loads(line) for line in reports[-1].read_text().splitlines()] if reports else []
                counts = {}
                for event in events:
                    if event["event"] == "saved" and event["method"] in {"jpeg_fragments", "png_fragments"}:
                        key = (event["method"], event["image_offset"])
                        counts[key] = counts.get(key, 0) + 1
                alternatives = sum(max(0, value - 1) for value in counts.values())
            flat.append({"dataset": dataset, "engine": row["engine"], "image": row["image"],
                         "exact": len(row["exact"]), "expected": len(row["exact"]) + len(row["missed"]),
                         "top1": sum(rank == 1 for rank in unique_ranks) if ranks is not None else None,
                         "top3": sum(rank is not None and rank <= 3 for rank in unique_ranks) if ranks is not None else None,
                         "alternatives": alternatives, "partial": categories.get("partial_jpeg", 0) if ranks is not None else None,
                         "thumbnails": categories.get("exif_thumbnail", 0) if ranks is not None else None,
                         "unmatched_unique": row["unmatched_unique"], "duplicates": row["duplicate_outputs"],
                         "damaged_ground_truth": None,
                         "seconds": round(row["elapsed_seconds"], 3),
                         "sampled_peak_rss_mib": round(row["peak_rss_bytes_sampled"] / 1048576, 2),
                         "wall_timeout": row["timeout"],
                         "searches_capped": sum(s["capped"] for s in row.get("searches", [])) if ranks is not None else None})
    with (BENCH / "V04-METRICS.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
        writer.writeheader()
        writer.writerows(flat)
    evidence = {"platform": platform.platform(), "python": platform.python_version(),
                "baseline_source": json.loads((BENCH / "baseline-0.3/manifest.json").read_text())["source_sha256"],
                "current_source_files": {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted((ROOT / "src").glob("*.py"))},
                "photorec_binary_sha256": sha(BENCH / "tools/testdisk-7.2/photorec_win.exe"),
                "results_files": {p.relative_to(ROOT).as_posix(): sha(p) for p in (
                    BENCH / "nist-v04-fixed/results.json", BENCH / "nist-release/results.json",
                    BENCH / "canon-v04/campaign/results.json", BENCH / "canon-final/results.json",
                    BENCH / "synthetic-release/results.json")}}
    (BENCH / "V04-EVIDENCE.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    lines = ["# Photo Recovery 0.4 — проверенный выпуск", "",
        "Проверено на Windows 11 10.0.26100 x64, Python 3.13.3, Pillow 13.0.0; сборщик PyInstaller 6.22.3.",
        "16 тестов проходят, включая новый случай трёх частей и сравнение валидаторов на 471 потоке.",
        "GUI: проверены рабочий поток, точный JPEG, диапазоны и предпросмотр. EXE: проверены CLI и запуск/закрытие GUI.", "",
        "## Точные оригиналы SHA-256", "",
        "| Набор / образ | 0.3 | 0.4 | PhotoRec 7.2 Standard | PhotoRec 7.2 BF |",
        "|---|---:|---:|---:|---:|"]
    for dataset, rows in groups.items():
        for name in dict.fromkeys(row["image"] for row in rows):
            cells = []
            for engine in ("0.3", "0.4", "PhotoRec-7.2-standard", "PhotoRec-7.2-bf"):
                row = next((row for row in rows if row["image"] == name and row["engine"] == engine), None)
                if row is None:
                    cells.append("нет измерения")
                else:
                    value = f'{len(row["exact"])}/{len(row["exact"]) + len(row["missed"])}'
                    cells.append(value + (" (лимит 120 с)" if row["timeout"] else ""))
            lines.append(f'| {dataset}: {name} | ' + " | ".join(cells) + " |")
    lines += ["", "## Методика и границы выводов", "",
        "Все параметры и команды сохранены в settings.json и results.json соответствующих кампаний. "
        "Лимит — 120 секунд на образ для каждого движка. Предельные попытки JPEG — 1500, "
        "работа — max(900000, 3 × число блоков), максимум 8 вариантов. Внутренний лимит поиска "
        "не равен тайм-ауту процесса. Есть ограничения числа концов, разрывов и размера кадра.", "",
        "NIST — регрессионный набор, применявшийся при разработке. Все 11 целых оригиналов сохранены. "
        "Ранжирование неоднозначного JPEG на L2 по-прежнему не решено; порядок — порядок поиска, "
        "candidate_rank не является вероятностью подлинности.", "",
        "Независимый публичный источник: [Digital Corpora, Canon2](https://digitalcorpora.org/corpora/disk-images/). "
        "Исходный gen6 E01 преобразован только для тестов через libewf из официального PhotoRec. "
        "SHA-1 raw совпал с опубликованным 4742c325f10583dab1eb4c55d0d45ab3beb99eb3. "
        "37 имён взяты из files-gen6.zip: IMG_0029.JPG и IMG_0039.JPG побайтно одинаковы, "
        "поэтому знаменатель равен 36 уникальным SHA-256. SHA-256 архива, E01, образов и оригиналов зафиксированы. "
        "Три производных образа содержат неизменённые байты первых трёх фотографий, разделённые двумя "
        "заданными промежутками. Весь Canon, включая производные, относится к holdout; снимки этой камеры "
        "не применялись для настройки. Производные случаи зависимы от raw: это не 40 независимых снимков.", "",
        "Синтетический генератор использует отдельный seed для каждого исходного снимка. source-0 — разработка, "
        "source-1..3 — контроль; повторные упаковки не пересекают разделение. Манифест и бюджет созданы до прогонов. "
        "После первых прогонов порядок поиска исправлялся по регрессии на NIST; окончательные синтетические "
        "результаты лежат в synthetic-release. Ранние результаты сохранены отдельно.", "",
        "Подтверждённое преимущество: на трёх публичных производных JPEG с двумя однообразными "
        "промежутками 0.4 восстанавливает 3/3 точных контейнеров, 0.3 и PhotoRec Standard — 0/3. "
        "Это не утверждение о превосходстве на произвольной фрагментации. На исходном Canon 0.4 "
        "теряет шесть уникальных оригиналов; PhotoRec Standard даёт те же 30/36 и работает существенно быстрее.", "",
        "В V04-METRICS.csv приведены метрики каждого образа: top1/top3 — числа известных оригиналов "
        "среди вариантов одной семьи (делитель expected), альтернативы — дополнительные сборки этой семьи. "
        "Для PhotoRec сопоставимый ранг и категории превью/частей не доступны: пустая ячейка означает "
        "нет измерения. Все экспортированные хеши, дубликаты, несовпавшие файлы и исчерпание бюджета есть в JSON. "
        "Несовпадение с эталонами не считается автоматически повреждением.", "",
        "Повреждённость по пикселям и доля уцелевших пикселей частичных кадров не измерялись. "
        "Заливка декодера не засчитывается в восстановление. tests/audit_edge_cases.py показывает "
        "усечённый JPEG с EOI, который открывается декодером, но отвергается строгой проверкой потока "
        "(v04/edge-audit.json). В Canon есть дополнительные декодируемые "
        "результаты вне эталонов; маленький самостоятельный JPEG без данных происхождения "
        "может попадать в decoded: в итоговом Canon-прогоне сохранён один такой файл 160×120. "
        "Он не засчитывается в точные оригиналы. Исправление этой классификации и ранжирование остаются открытыми.", "",
        "RSS опрашивался через psutil каждые 20 мс; для итоговых Python-прогонов суммируется дерево "
        "процессов, включая дочерний интерпретатор venv. Общие страницы могут учитываться дважды. "
        "Это оценка пика, не точный high-water mark ОС. Итоговые прогоны двух версий выполнены "
        "последовательно; ранние диагностические прогоны пересекались со сборкой и другими проверками. "
        "PhotoRec взят из предыдущей кампании, поэтому это не строгая статистика производительности. "
        "В profile.json отдельно измерены реальное чтение файла "
        "(с возможным кэшем ОС), поиск и валидация; сравнение 20 одинаковых потоков — диагностический замер.", "",
        "Первый вариант 0.4 дал регрессию NIST 9/11 из-за расходования бюджета до прежних приоритетных "
        "проверок. Он не выпущен; результаты сохранены в nist-v04/results.json. После исправления "
        "порядка и окончательной проверки получено 11/11 (nist-release). Возможны непросмотренные "
        "альтернативы при исчерпании бюджета; уверенность не калибрована.", "",
        "## Недоступные сравнения", "",
        "| Программа | Состояние | Причина |", "|---|---|---|",
        "| R-Studio 8.16.180499 Windows | нет измерения | Найдена установленная программа; управляемого экспорта из её GUI в этом сеансе не выполнено. Редакция и лицензия не проверены. |",
        "| DMDE | нет измерения | В рабочем проекте/PATH исполняемый файл не найден; повторного экспорта нет. Старый результат Free 4.4.4 на NIST L5 не переносится на новый набор. |",
        "| UFS Explorer | нет измерения | Установленный исполнитель и средство управляемого экспорта в этом сеансе не найдены. |", "",
        "Экспорт ограничен следует ставить только после наблюдаемого ограничения конкретной демоверсии. "
        "Здесь такое ограничение не проверялось; этим программам не присвоены нулевые результаты.", "",
        "## Воспроизведение", "", "```powershell",
        ".\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v",
        "python tests/compare_campaign.py benchmark/nist-graphic-manifest.json benchmark/nist-images benchmark/repeat-nist --search-mib 16",
        "python tests/compare_campaign.py benchmark/canon-v04/manifest.json benchmark/canon-v04 benchmark/repeat-canon --search-mib 1",
        "python tests/compare_campaign.py benchmark/v04/manifest.json benchmark/v04 benchmark/repeat-synthetic --search-mib 1 --engines 0.3 0.4",
        "python tests/profile_validation.py", "python tests/write_v04_report.py", "```", "",
        "Для скриптов измерений установите requirements-benchmark.txt (psutil 7.2.2); движку нужен только Pillow. "
        "Подготовка: tests/fetch_nist.py, tests/fetch_comparison.py, tests/fetch_canon.py, tests/prepare_canon.py. "
        "Генератор tests/benchmark_v04.py generate отказывается перезаписывать зафиксированный манифест. "
        "Для независимого повторения генерации используйте чистую копию проекта без benchmark/v04/manifest.json.", "",
        "Проверка происхождения выполняется compare_campaign через audit_benchmark.check_provenance: "
        "SHA-256 пересчитывается напрямую по диапазонам образа и сверяется с выходным файлом. "
        "Оригинальный образ и карта открываются только для чтения. Возобновление, прямой доступ к дискам, "
        "progressive JPEG, новые RAW/HEIC и отдельное испытание многотерабайтных образов не реализованы/не проверены."]
    (BENCH / "V04-REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
