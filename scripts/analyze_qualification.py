"""Validate saved qualification artifacts offline and print compact evidence summaries."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.qualification_report import (  # noqa: E402
    analyze_stability,
    validate_functional_report,
)

parser = argparse.ArgumentParser()
parser.add_argument("reports", nargs="+", type=Path)
args = parser.parse_args()
for path in args.reports:
    report = json.loads(path.read_text())
    try:
        validate_functional_report(report)
        functional = "PASS"
    except ValueError as error:
        functional = str(error)
    soak = report["phases"]["mixed_soak"]
    print(json.dumps({"file": str(path), "functional": functional,
        "duration": soak["elapsed_seconds"], "responses": soak["responses"],
        "workflows": soak["workflow_statuses"], "p95_ms": soak["p95_ms"],
        "analysis": analyze_stability(report)}, indent=2))
