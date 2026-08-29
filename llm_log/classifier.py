from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class PrologClassifier:
    def __init__(self, swipl: str | None = None, rules: str | Path | None = None, timeout: float = 5.0):
        self.swipl = swipl or shutil.which("swipl") or "swipl"
        self.rules = Path(rules) if rules else Path(__file__).with_name("prolog") / "classifier.pl"
        self.timeout = timeout

    def classify(self, text: str) -> list[str]:
        process = subprocess.run(
            [self.swipl, "-q", "-s", str(self.rules), "-g", "main", "-t", "halt"],
            input=text,
            text=True,
            capture_output=True,
            check=True,
            timeout=self.timeout,
        )
        labels = json.loads(process.stdout)
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ValueError("classifier did not return a JSON string list")
        return labels
