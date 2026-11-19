"""Profile the frozen and current assemblers, then compare validator throughput."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import fragments
import jpeg_validation as current


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TimedReader:
    def __init__(self, stream):
        self.stream = stream
        self.elapsed = 0.0

    def seek(self, offset):
        return self.stream.seek(offset)

    def read(self, count=-1):
        start = time.perf_counter()
        result = self.stream.read(count)
        self.elapsed += time.perf_counter() - start
        return result


if __name__ == "__main__":
    reference = load("reference_validator", ROOT / "benchmark/baseline-0.3/src/jpeg_validation.py")
    old = load("reference_fragments", ROOT / "benchmark/baseline-0.3/src/fragments.py")
    old.read_header, old.scan_body = reference.read_header, reference.scan_body
    old.valid_entropy = reference.valid_entropy
    payload = (ROOT / "benchmark/v04/source-0.jpg").read_bytes()
    packed = (ROOT / "benchmark/v04/case-0.img").read_bytes()
    results = {}
    for label, engine in (("0.3", old), ("0.4", fragments)):
        validation = [0.0]
        def timed(function):
            def wrapped(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return function(*args, **kwargs)
                finally:
                    validation[0] += time.perf_counter() - start
            return wrapped
        engine.valid_entropy = timed(engine.valid_entropy)
        engine.dimensions = timed(engine.dimensions)
        started = time.perf_counter()
        with (ROOT / "benchmark/v04/case-0.img").open("rb") as source:
            reader = TimedReader(source)
            found = engine.glue(reader, 1024, len(packed), 64 * 1024 * 1024, 512, 1, lambda: False)
        total = time.perf_counter() - started
        results[label] = {"read_seconds": reader.elapsed, "validation_seconds": validation[0],
                          "candidate_search_seconds": max(0, total - reader.elapsed - validation[0]),
                          "attempts": found.attempts, "capped": found.capped,
                          "exact": any(item.payload == payload for item in found.matches)}
    throughput = {}
    for label, module in (("0.3", reference), ("0.4", current)):
        header = module.read_header(payload)
        body = module.scan_body(payload, header)
        started = time.perf_counter()
        for _ in range(20):
            assert module.valid_entropy(body, header, lambda: False)
        throughput[label] = time.perf_counter() - started
    results["validator_20_identical_inputs_seconds"] = throughput
    (ROOT / "benchmark/v04/profile.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
