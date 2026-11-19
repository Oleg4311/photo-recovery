"""Read-only image scanning and append-only recovery logs."""

import hashlib
import json
import ntpath
import os
import re
import stat
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from formats import PHOTO_EXTENSIONS, SIGNATURES, extract, jpeg_parts, kind, png_end
from flash import FlashVolume
from fragments import CLUSTER_SIZES, MAX_WINDOW_MIB, glue
from jpeg_validation import read_header, scan_body, valid_entropy
from ntfs import Volume, partition_offsets, photo_extension
from png_fragments import glue_png
from rescue_map import MappedImage, RescueMap
from sources import ReadOnlyDevice, is_device, open_source, source_size
from catalog import build_catalogue
from targeted import glue_zip, valid_zip

MIB = 1024 * 1024
BLOCK_SIZE = 4 * MIB
TAIL = 11
INITIAL_WINDOW = 512 * 1024


@dataclass
class Summary:
    folder: Path
    decoded: int = 0
    thumbnails: int = 0
    repaired: int = 0
    partial: int = 0
    cache_previews: int = 0
    assembled: int = 0
    ambiguous: int = 0
    assembly_attempts: int = 0
    assembly_capped: int = 0
    unmapped_bytes: int = 0
    duplicates: int = 0
    rejected_ntfs: int = 0
    candidates: int = 0
    volumes: int = 0
    failed_volumes: int = 0
    copies_checked: int = 0
    cancelled: bool = False
    files: int = 0
    unresolved: int = 0
    assembled_other: int = 0

    @property
    def saved(self):
        return (self.decoded + self.thumbnails + self.repaired + self.partial
                + self.cache_previews + self.assembled + self.ambiguous + self.files)


def safe_stem(name: str) -> str:
    name = ntpath.basename(name)
    stem, _ = ntpath.splitext(name)
    return re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", stem).strip(" .")[:70]


def result_directory(folder, category, origin):
    directory = folder / category
    path = origin.get("original_path", "").replace("\\", "/")
    if path:
        directory /= f"volume-{origin.get('volume_offset', 0):x}"
        for part in path.split("/")[:-1]:
            part = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", part).strip(" .")[:80]
            if not part or part in {".", ".."}:
                part = "_"
            if part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
                part = "_" + part
            directory /= part
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def validate_paths(source: Path | None, destination: Path):
    if source is not None:
        if is_device(source):
            source = str(source)
        else:
            if str(source).upper().startswith(("\\\\.\\", "\\\\?\\GLOBALROOT")):
                raise ValueError("Недопустимый путь устройства.")
            source = Path(source).resolve(strict=True)
            if not stat.S_ISREG(source.stat().st_mode):
                raise ValueError("Укажите обычный файл образа диска.")
            if source.suffix.lower() in {".e01", ".ex01", ".vhd", ".vhdx", ".vmdk", ".qcow2", ".zip", ".gz", ".7z"}:
                raise ValueError("Нужен несжатый посекторный образ RAW, DD или IMG.")
    destination = destination.resolve(strict=True)
    if not destination.is_dir():
        raise ValueError("Выберите существующую папку для результатов.")
    return source, destination


def hits(image, size: int, cancelled, progress):
    tail = b""
    for cursor in range(0, size, BLOCK_SIZE):
        if cancelled():
            return
        image.seek(cursor)
        block = image.read(min(BLOCK_SIZE, size - cursor))
        if len(block) != min(BLOCK_SIZE, size - cursor):
            raise OSError("Чтение образа оборвалось.")
        data = tail + block
        base = cursor - len(tail)
        limit = len(data) if base + len(data) == size else max(0, len(data) - TAIL)
        for match in SIGNATURES.finditer(data):
            if match.start() >= limit:
                break
            if cancelled():
                return
            extension = kind(data[match.start():match.start() + 12])
            if extension:
                yield base + match.start(), extension
        progress("Сигнатуры", base + limit, size)
        tail = data[limit:]


def raw_extract(image, offset, size, maximum, cancelled):
    """Increase the read window only when a supported candidate has no known end."""
    budget = min(maximum, size - offset)
    window = min(INITIAL_WINDOW, budget)
    image.seek(offset)
    data = image.read(window)
    if len(data) != window:
        raise OSError("Чтение образа оборвалось.")
    extension = kind(data[:12])
    if extension == "jpg" and len(data) >= 4 and data[3] not in (
        0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xcc, 0xdb, 0xdd, 0xfe, 0xff, *range(0xe0, 0xf0)
    ):
        return []
    if extension == "png" and data[8:16] != b"\x00\x00\x00\x0dIHDR":
        return []
    if extension == "webp" and data[12:16] not in (b"VP8 ", b"VP8L", b"VP8X"):
        return []
    if extension == "webp":
        declared = 8 + int.from_bytes(data[4:8], "little")
        if declared > budget or declared < 20:
            return []
        budget = declared
        if len(data) > budget:
            data = data[:budget]
    results = []
    while not cancelled():
        results = list(extract(data, cancelled))
        if any(result[2] in {"decoded", "partial_jpeg"} for result in results) or len(data) >= budget:
            break

        # A valid container end can be found even if the decoder rejects its pixels.
        if extension == "jpg" and jpeg_parts(data, cancelled)[0] is not None:
            break
        if extension == "png" and png_end(data, cancelled) is not None:
            break

        target = min(budget, len(data) * 4)
        more = image.read(target - len(data))
        if len(more) != target - len(data):
            raise OSError("Чтение образа оборвалось.")
        data += more
    return results


def scan(source: Path | None, destination: Path, max_mib: int = 64, *, metadata=True,
         raw=True, copies=None, deep=False, cluster_size=512, search_mib=2, mapfile=None,
         map_delta=0, cancelled=lambda: False, progress=lambda *_: None,
         on_event=lambda *_: None, selected_entries=None, all_types=False) -> Summary:
    if not 1 <= max_mib <= 16384:
        raise ValueError("Лимит файла должен быть от 1 до 16384 МиБ.")
    scoped = selected_entries is not None
    if scoped and (not selected_entries or not metadata or raw or copies is not None or source is None):
        raise ValueError("Для выбранных записей включите файловые системы и отключите общий поиск по секторам и копиям.")
    if source is None and copies is None:
        raise ValueError("Выберите образ диска или папку с копиями.")
    if deep and (source is None or (not raw and not scoped)):
        raise ValueError("Для сборки фрагментов нужен образ и включённый поиск по секторам.")
    if deep and (cluster_size not in CLUSTER_SIZES or not 1 <= search_mib <= MAX_WINDOW_MIB):
        raise ValueError(f"Выберите размер сектора/кластера от 512 до 65536 байт и окно от 1 до {MAX_WINDOW_MIB} МиБ.")
    if map_delta and mapfile is None:
        raise ValueError("Смещение карты имеет смысл только вместе с картой ddrescue.")
    if mapfile is not None and source is None:
        raise ValueError("Для карты ddrescue укажите соответствующий образ.")
    if not metadata and not raw and copies is None:
        raise ValueError("Выберите хотя бы один метод поиска.")
    source, destination = validate_paths(source, destination)
    if copies is not None:
        copies = copies.resolve(strict=True)
        if not copies.is_dir():
            raise ValueError("Папка с копиями недоступна.")
    maximum = max_mib * MIB
    with (open_source(source) if source is not None else nullcontext()) as image:
        device = isinstance(image, ReadOnlyDevice)
        if device:
            image.guard_destination(destination)
        start_stat = os.fstat(image.fileno()) if image is not None and not device else None
        size = source_size(image) if image is not None else 0
        if start_stat is not None and (not size or not stat.S_ISREG(start_stat.st_mode)):
            raise ValueError("Пустой или неподдерживаемый образ.")
        catalogue = build_catalogue(source, cancelled, progress, selected=set(selected_entries)) if scoped else None
        targets = catalogue.targets(selected_entries) if catalogue and not catalogue.cancelled else {}
        handled_targets = set()
        mapping = None
        if mapfile is not None:
            mapfile = mapfile.resolve(strict=True)
            info = mapfile.stat()
            if not stat.S_ISREG(info.st_mode) or mapfile == source:
                raise ValueError("Укажите отдельный текстовый файл карты ddrescue.")
            if info.st_size > 64 * MIB:
                raise ValueError("Карта ddrescue больше 64 МиБ.")
            mapping = RescueMap.load(mapfile, size, map_delta)
            image = MappedImage(image, mapping)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        folder = destination / f"recovery-{stamp}-{uuid4().hex[:6]}"
        folder.mkdir()
        summary = Summary(folder, unmapped_bytes=size - mapping.mapped_bytes if mapping else 0)
        saved = {}
        known_thumbnails = set()
        checked_extents = set()

        with (folder / "report.jsonl").open("x", encoding="utf-8", newline="\n") as log:
            def record(event, **fields):
                entry = {"event": event, **fields}
                log.write(json.dumps(entry, ensure_ascii=True) + "\n")
                if event in {"start", "saved", "error", "finish"}:
                    log.flush()
                on_event(entry)

            def save(result, *, method, origin, name="", allocation="", offset=None):
                payload, extension, category, (width, height), original_length, relative = result
                position = offset + relative if offset is not None else None
                if category == "exif_thumbnail" and position is not None:
                    known_thumbnails.add(position)
                digest = hashlib.sha256(payload).hexdigest()
                path = saved.get(digest)
                if path:
                    summary.duplicates += 1
                    event = "duplicate"
                else:
                    subdirectory = result_directory(folder, category, origin)
                    stem = safe_stem(name) or (f"offset-{position:016x}" if position is not None else "photo")
                    path = subdirectory / f"{summary.saved + 1:06d}_{stem}.{extension}"
                    temp = path.with_suffix(".part")
                    try:
                        with temp.open("xb") as handle:
                            handle.write(payload)
                        temp.replace(path)
                    finally:
                        temp.unlink(missing_ok=True)
                    saved[digest] = path
                    if category == "decoded":
                        summary.decoded += 1
                    elif category == "exif_thumbnail":
                        summary.thumbnails += 1
                    elif category in {"assembled_jpeg", "assembled_png"}:
                        summary.assembled += 1
                    elif category == "ambiguous_jpeg":
                        summary.ambiguous += 1
                    elif category == "cache_thumbnail":
                        summary.cache_previews += 1
                    elif category == "partial_jpeg":
                        summary.partial += 1
                    elif category in {"file_bytes", "assembled_file"}:
                        summary.files += 1
                        summary.assembled_other += category == "assembled_file"
                    else:
                        summary.repaired += 1
                    event = "saved"
                record(event, file=path.relative_to(folder).as_posix(), method=method,
                       category=category, original_name=name, allocation=allocation,
                       width=width, height=height, image_offset=position,
                       logical_offset=relative, original_bytes=original_length,
                       sha256=digest, **origin)

            def save_stream(item, volume, origin, method):
                length = item.size
                extents = []
                for extent in item.extents:
                    take = min(length, extent.size)
                    if take:
                        extents.append((extent.offset, take))
                        length -= take
                if mapping and any(not mapping.complete(start, take) for start, take in extents):
                    record("rejected", method=method, original_name=item.name, reason="В размещении файла есть непрочитанные диапазоны.", **origin)
                    return
                directory = result_directory(folder, "file_bytes", origin)
                suffix = photo_extension(item.name).lstrip(".")
                suffix = suffix if re.fullmatch(r"[\w-]{1,24}", suffix) else "bin"
                path = directory / f"{summary.saved + 1:06d}_{safe_stem(item.name) or 'file'}.{suffix}"
                temp = path.with_suffix(".part")
                digest = hashlib.sha256()
                try:
                    with temp.open("xb") as output:
                        position = 0
                        while position < item.size:
                            if cancelled():
                                return
                            count = min(MIB, item.size - position)
                            block = item.resident[position:position + count] if method == "ntfs" and item.resident is not None else volume.read_stream(item.extents, position, count)
                            if len(block) != count:
                                raise OSError("Неполное чтение выбранного файла.")
                            output.write(block)
                            digest.update(block)
                            position += count
                            progress("Извлечение " + item.name, position, item.size)
                    digest = digest.hexdigest()
                    event = "duplicate" if digest in saved else "saved"
                    if event == "saved":
                        temp.replace(path)
                        saved[digest] = path
                        summary.files += 1
                    else:
                        path = saved[digest]
                        summary.duplicates += 1
                    record(event, file=path.relative_to(folder).as_posix(), method=method,
                           category="file_bytes", original_name=item.name, allocation=item.allocation,
                           width=0, height=0, image_offset=extents[0][0] if extents else None,
                           logical_offset=0, original_bytes=item.size, sha256=digest,
                           **{**origin, "extents": extents, "integrity": origin.get("integrity", "not_verified")})
                finally:
                    temp.unlink(missing_ok=True)

            def targeted_assembly(item, volume, method, origin, data, results):
                if not scoped or not deep or not item.extents or cancelled():
                    return False
                extension = kind(data[:12])
                if data.startswith(b"PK"):
                    extension = "zip"
                strong = False
                for index, result in enumerate(results):
                    if result[2] != "decoded":
                        continue
                    if extension == "jpg":
                        header = read_header(result[0])
                        strong = header is not None and len(result[0]) == item.size and valid_entropy(scan_body(result[0], header), header, cancelled)
                        if header is not None and not strong:
                            results[index] = (*result[:2], "partial_jpeg", *result[3:])
                    else:
                        strong = len(result[0]) == item.size
                if extension == "zip" and valid_zip(data, cancelled, [512 * MIB]):
                    origin["integrity"] = "zip_crc"
                    strong = True
                if strong:
                    record("targeted_search", target_id=origin["target_id"], outcome="metadata_validated")
                    return False
                first = item.extents[0].offset
                cluster = volume.cluster if method == "ntfs" else volume.cluster_size
                candidates, attempts, capped = [], 0, False
                supported = extension in {"jpg", "png", "zip"}
                if extension == "jpg":
                    search = glue(image, first, size, min(item.size, 256 * MIB), cluster, search_mib, cancelled, mapping)
                    candidates, attempts, capped = search.matches, search.attempts, search.capped
                    supported = search.supported
                elif extension == "png":
                    candidate = glue_png(image, first, size, min(item.size, 256 * MIB), cluster, search_mib, cancelled, mapping)
                    if candidate:
                        candidates, attempts = [candidate], candidate.attempts
                elif extension == "zip":
                    search = glue_zip(image, first, size, item.size, cluster, search_mib, cancelled, mapping)
                    candidates, attempts, capped = search.matches, search.attempts, search.capped
                candidates = [candidate for candidate in candidates if len(candidate.payload) == item.size]
                summary.assembly_attempts += attempts
                summary.assembly_capped += capped
                record("targeted_search", target_id=origin["target_id"], format=extension,
                       attempts=attempts, capped=capped, matches=len(candidates),
                       outcome="candidates" if candidates else "not_reconstructed",
                       supported=supported, expected_bytes=item.size,
                       window_mib=search_mib, source_offset=first)
                if not candidates:
                    summary.unresolved += 1
                for rank, candidate in enumerate(candidates, 1):
                    if cancelled():
                        break
                    category = "assembled_file" if extension == "zip" else "ambiguous_jpeg" if extension == "jpg" and len(candidates) > 1 else "assembled_jpeg" if extension == "jpg" else "assembled_png"
                    candidate_origin = {**origin, "extents": candidate.extents,
                                        "mapping": "zip_crc" if extension == "zip" else "jpeg_entropy" if extension == "jpg" else "png_crc",
                                        "candidate_rank": rank, "candidate_count": len(candidates),
                                        "search_capped": capped, "authenticity_verified": False,
                                        "integrity": "zip_crc" if extension == "zip" else "structure_validated"}
                    suffix = photo_extension(item.name).lstrip(".") or extension
                    save((candidate.payload, suffix, category, candidate.size, len(candidate.payload), 0),
                         method="zip_fragments" if extension == "zip" else "jpeg_fragments" if extension == "jpg" else "png_fragments",
                         name=item.name, allocation=item.allocation, offset=first, origin=candidate_origin)
                return bool(candidates)

            record("start", engine_version="0.7", source=str(source) if source is not None else None, source_bytes=size,
                   max_file_bytes=maximum, folder=str(folder), deep=deep,
                   cluster_size=cluster_size if deep else None, search_mib=search_mib if deep else None,
                   mapfile=str(mapfile) if mapfile else None, map_delta=map_delta,
                   unmapped_bytes=summary.unmapped_bytes, device_read_only=device,
                   live_source=device, selected_entries=selected_entries, all_types=all_types,
                   metadata=metadata, raw=raw)
            if catalogue:
                for warning in catalogue.warnings:
                    record("warning", reason=warning)
            try:
                if metadata and image is not None:
                    progress("NTFS", 0, 0)
                    for offset in partition_offsets(image, size):
                        if cancelled():
                            break
                        image.seek(offset)
                        boot = image.read(512)
                        if boot[510:] != b"\x55\xaa":
                            continue
                        signature = boot[3:11]
                        if signature == b"NTFS    ":
                            method = "ntfs"
                        elif signature == b"EXFAT   ":
                            method = "exfat"
                        elif boot[82:90] == b"FAT32   ":
                            method = "fat32"
                        else:
                            continue
                        try:
                            volume = Volume(image, size, offset) if method == "ntfs" else FlashVolume(image, size, offset, boot)
                            summary.volumes += 1
                            update = lambda done, total: progress(method.upper(), done, total)
                            for item in volume.deleted(maximum, cancelled, update):
                                if cancelled():
                                    break
                                locator = item.record_number if method == "ntfs" else item.directory_offset
                                target_key = (method, offset, locator, item.sequence)
                                chosen = targets.get(target_key)
                                if scoped and chosen is None:
                                    continue
                                if chosen:
                                    handled_targets.add(chosen.id)
                                resident = item.resident if method == "ntfs" else None
                                origin = {"volume_offset": offset, "extents": [list(extent) for extent in item.extents]}
                                if method == "ntfs":
                                    origin["mft_record"] = item.record_number
                                    origin["mapping"] = "mft"
                                else:
                                    origin["directory_offset"] = item.directory_offset
                                    origin["mapping"] = item.mapping
                                if chosen:
                                    origin.update(original_path=chosen.path, path_known=chosen.path_known, target_id=chosen.id)
                                head = resident[:12] if resident is not None else volume.read_stream(item.extents, 0, min(12, item.size)) if item.size else b""
                                photo = bool(kind(head)) or photo_extension(item.name) in PHOTO_EXTENSIONS
                                if not photo:
                                    if all_types:
                                        if scoped and deep and head.startswith(b"PK") and item.size <= 64 * MIB:
                                            data = resident if resident is not None else volume.read_stream(item.extents, 0, item.size)
                                            targeted_assembly(item, volume, method, origin, data, [])
                                        elif scoped and deep:
                                            record("targeted_search", target_id=chosen.id, supported=False, outcome="metadata_only", reason="Для этого типа файла сборка фрагментов не реализована.")
                                            summary.unresolved += 1
                                        save_stream(item, volume, origin, method)
                                    continue
                                if item.size > 256 * MIB:
                                    if all_types:
                                        save_stream(item, volume, origin, method)
                                    continue
                                data = resident if resident is not None else volume.read_stream(item.extents, 0, item.size) if item.size else b""
                                contiguous = len(item.extents) == 1
                                start_at = item.extents[0].offset if contiguous else None
                                allow_repair = item.allocation in {"free", "resident"} and origin["mapping"] != "contiguous_guess"
                                results = list(extract(data, cancelled, repair_end=allow_repair))
                                if mapping and item.extents:
                                    def complete(read_length):
                                        for extent in item.extents:
                                            take = min(read_length, extent.size)
                                            if not mapping.complete(extent.offset, take):
                                                return False
                                            read_length -= take
                                            if read_length <= 0:
                                                return True
                                        return False
                                    results = [result for result in results if complete(result[4] + result[5])]
                                reconstructed = targeted_assembly(item, volume, method, origin, data, results)
                                if all_types and not results and not reconstructed:
                                    save_stream(item, volume, origin, method)
                                if contiguous and any(
                                    result[2] == "decoded" or
                                    (not deep and result[2] in {"repaired_eoi", "partial_jpeg"})
                                    for result in results
                                ):
                                    checked_extents.add(start_at)
                                if not results and not cancelled():
                                    summary.rejected_ntfs += method == "ntfs"
                                    record("rejected", method=method, original_name=item.name,
                                           allocation=item.allocation, **origin)
                                for result in results:
                                    save(result, method=method, name=item.name, allocation=item.allocation,
                                         origin=origin, offset=start_at)
                        except ValueError as error:
                            summary.failed_volumes += 1
                            record("volume_error", method=method, offset=offset, reason=str(error))
                if scoped and not cancelled():
                    for target in targets.values():
                        if target.id not in handled_targets:
                            summary.unresolved += 1
                            record("targeted_search", target_id=target.id, original_path=target.path,
                                   outcome="metadata_unreadable", supported=False,
                                   reason="Размер файла больше установленного лимита." if target.size > maximum else "Размещение потока не поддерживается или запись изменилась после чтения списка.")
                if raw and image is not None and not cancelled():
                    progress("Сигнатуры", 0, size)
                    for offset, extension in hits(image, size, cancelled, progress):
                        if offset in checked_extents or offset in known_thumbnails:
                            continue
                        summary.candidates += 1
                        results = raw_extract(image, offset, size, min(maximum, 256 * MIB), cancelled)
                        if mapping:
                            results = [result for result in results if
                                       mapping.complete(offset + result[5], result[4])]
                        strong_contiguous = False
                        if deep and extension == "jpg":
                            for index, result in enumerate(results):
                                payload, ext, category, dims, length, relative = result
                                if category != "decoded":
                                    continue
                                header = read_header(payload)
                                if header is None:
                                    strong_contiguous = True
                                elif valid_entropy(scan_body(payload, header), header, cancelled):
                                    strong_contiguous = True
                                else:
                                    results[index] = (payload, ext, "partial_jpeg", dims, length, relative)
                        if deep and extension == "jpg" and not strong_contiguous and not cancelled():
                            progress("Сборка JPEG", offset, size)
                            search = glue(image, offset, size, min(maximum, 256 * MIB), cluster_size, search_mib, cancelled, mapping)
                            summary.assembly_attempts += search.attempts
                            summary.assembly_capped += search.capped
                            if search.supported and (search.attempts or search.capped):
                                record("fragment_search", image_offset=offset, attempts=search.attempts,
                                       capped=search.capped, matches=len(search.matches), match_limit=search.match_limit,
                                       read_seconds=search.read_seconds, search_seconds=search.search_seconds,
                                       validation_seconds=search.validation_seconds)
                            if search.matches and not cancelled():
                                category = "ambiguous_jpeg" if len(search.matches) > 1 else "assembled_jpeg"
                                for rank, candidate in enumerate(search.matches, 1):
                                    provenance = {"extents": candidate.extents, "mapping": "jpeg_entropy",
                                                  "candidate_count": len(search.matches),
                                                  "search_capped": search.capped,
                                                  "candidate_rank": rank,
                                                  "confidence_reason": "exact_baseline_entropy; source_extents_verified",
                                                  "authenticity_verified": False}
                                    save((candidate.payload, "jpg", category, candidate.size, len(candidate.payload), 0),
                                         method="jpeg_fragments", offset=offset, origin=provenance)
                            if search.partial and not search.matches and not any(
                                result[2] == "partial_jpeg" for result in results
                            ):
                                results.append(search.partial)
                        if deep and extension == "png" and not results and not cancelled():
                            progress("Сборка PNG", offset, size)
                            candidate = glue_png(image, offset, size, min(maximum, 256 * MIB), cluster_size, search_mib, cancelled, mapping)
                            if candidate:
                                summary.assembly_attempts += candidate.attempts
                                provenance = {"extents": candidate.extents, "mapping": "png_crc"}
                                save((candidate.payload, "png", "assembled_png", candidate.size, len(candidate.payload), 0),
                                     method="png_fragments", offset=offset, origin=provenance)
                        for result in results:
                            save(result, method="signature", offset=offset, origin={})
                if copies is not None and not cancelled():
                    progress("Копии и кэш", 0, 0)
                    def copy_error(error):
                        record("warning", reason=str(error))
                    for directory, subdirectories, filenames in os.walk(copies, onerror=copy_error):
                        subdirectories[:] = [
                            name for name in subdirectories
                            if Path(directory, name) != folder
                            and not getattr(Path(directory, name), "is_junction", lambda: False)()
                        ]
                        for filename in filenames:
                            if cancelled():
                                break
                            path = Path(directory, filename)
                            is_cache = filename.lower().startswith("thumbcache_") and path.suffix.lower() == ".db"
                            if path == source or path.is_symlink() or (not is_cache and path.suffix.lower() not in PHOTO_EXTENSIONS):
                                continue
                            try:
                                with path.open("rb") as handle:
                                    info = os.fstat(handle.fileno())
                                    if not stat.S_ISREG(info.st_mode) or (not is_cache and info.st_size > maximum):
                                        continue
                                    summary.copies_checked += 1
                                    progress("Копии и кэш", summary.copies_checked, 0)
                                    if is_cache:
                                        if handle.read(4) != b"CMMM":
                                            continue
                                        for offset, _extension in hits(handle, info.st_size, cancelled, lambda *_: None):
                                            for found in raw_extract(handle, offset, info.st_size, maximum, cancelled):
                                                payload, ext, category, dims, length, relative = found
                                                if category == "decoded":
                                                    save((payload, ext, "cache_thumbnail", dims, length, relative),
                                                         method="windows_cache", name=filename,
                                                         origin={"copy_path": str(path), "cache_offset": offset + relative})
                                        continue
                                    data = handle.read(maximum + 1)
                            except OSError as error:
                                copy_error(error)
                                continue
                            if len(data) > maximum:
                                continue
                            for result in extract(data, cancelled, repair_end=True):
                                save(result, method="existing_copy", name=filename, origin={"copy_path": str(path)})
                        if cancelled():
                            break
                if image is not None and start_stat is not None:
                    end_stat = os.fstat(image.fileno())
                    if (end_stat.st_size, end_stat.st_mtime_ns) != (size, start_stat.st_mtime_ns):
                        record("warning", reason="Файл образа изменился во время чтения. Результаты требуют проверки.")
            except (OSError, ValueError) as error:
                record("error", reason=str(error))
                raise
            finally:
                summary.cancelled = cancelled()
                counts = asdict(summary)
                del counts["folder"]
                record("finish", **counts)
        return summary
