"""Demonstrate why a JPEG opening in a pixel decoder is not exact recovery."""
import hashlib
import json
from pathlib import Path

from test_recovery import photo
from formats import dimensions
from jpeg_validation import read_header, scan_body, valid_entropy

original = photo(180, 130)
damaged = original[:len(original) * 2 // 3] + b"\xff\xd9"
header = read_header(damaged)
result = {"scenario": "missing entropy suffix with EOI accepted by pixel decoder",
          "source_sha256": hashlib.sha256(original).hexdigest(),
          "damaged_sha256": hashlib.sha256(damaged).hexdigest(),
          "decoder_opens": dimensions(damaged, "jpg", lambda: False) is not None,
          "exact_entropy_valid": valid_entropy(scan_body(damaged, header), header, lambda: False),
          "deep_mode_behavior": "demoted to partial_jpeg before fragment search"}
assert result["decoder_opens"] and not result["exact_entropy_valid"]
path = Path(__file__).resolve().parents[1] / "benchmark/v04/edge-audit.json"
path.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
