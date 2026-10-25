"""Platform scorer: verify actual result cells, never accept SQL alone as an answer."""
from collections import Counter
import json


def answer_correctness(expected, answer, tags):
    if not isinstance(expected, dict) or not isinstance(answer, str):
        return 0.0
    if expected.get("errorCode"):
        return float(expected["errorCode"] in answer)
    if "checks" in expected:
        try:
            value = json.loads(answer)
            for check in expected["checks"]:
                actual = value
                for part in check["path"].split("."):
                    actual = actual[int(part)] if isinstance(actual, list) else actual[part]
                if "equals" in check and actual != check["equals"]:
                    return 0.0
                if "contains" in check and check["contains"] not in actual:
                    return 0.0
                if check.get("nonEmpty") and not actual:
                    return 0.0
            return float(bool(expected["checks"]))
        except (ValueError, TypeError, KeyError, IndexError):
            return 0.0
    # The SQL and narrative may include the expected number by coincidence.
    # Require every expected cell in the returned Markdown table.
    table = "\n".join(line for line in answer.splitlines() if line.lstrip().startswith("|"))
    rows = [Counter(value.strip() for value in line.strip().strip("|").split("|"))
            for line in table.splitlines()]
    expected_rows = expected.get("cells")
    if not isinstance(expected_rows, list) or not expected_rows:
        return 0.0
    for expected_row in expected_rows:
        wanted = Counter(str(value) for value in expected_row)
        match = next((index for index, row in enumerate(rows) if not (wanted - row)), None)
        if match is None:
            return 0.0
        rows.pop(match)
    return 1.0
