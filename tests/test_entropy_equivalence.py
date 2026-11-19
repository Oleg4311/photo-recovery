"""Differential check against the frozen 0.3 validator on identical bytes."""
import importlib.util
from pathlib import Path
import random
import sys
import unittest

from test_recovery import photo
from jpeg_validation import read_header, scan_body, valid_entropy


class EntropyEquivalence(unittest.TestCase):
    def test_mutated_truncated_and_valid_streams(self):
        path = Path(__file__).resolve().parents[1] / "benchmark/baseline-0.3/src/jpeg_validation.py"
        spec = importlib.util.spec_from_file_location("reference_jpeg", path)
        reference = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = reference
        spec.loader.exec_module(reference)
        rng = random.Random(10404)
        for width, height in ((16, 16), (49, 35), (120, 90)):
            payload = photo(width, height)
            header, old = read_header(payload), reference.read_header(payload)
            body = scan_body(payload, header)
            cases = [body, b"", body[:-1], body + b"\0", body + b"\xff\xd0",
                     body + b"\xff\x00", body + b"\xff\x00\xff\x00"]
            for _ in range(150):
                changed = bytearray(body)
                position = rng.randrange(len(changed))
                changed[position] ^= rng.randrange(1, 256)
                cases.append(bytes(changed))
            for encoded in cases:
                self.assertEqual(valid_entropy(encoded, header, lambda: False),
                                 reference.valid_entropy(encoded, old, lambda: False))
            self.assertFalse(valid_entropy(body, header, lambda: True))
