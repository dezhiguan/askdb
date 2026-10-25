"""Platform scorer: verify actual result cells, never accept SQL alone as an answer."""
from collections import Counter


def answer_correctness(expected, answer, tags):
    if not isinstance(expected, dict) or not isinstance(answer, str):
        return 0.0
    if expected.get("errorCode"):
        return float(expected["errorCode"] in answer)
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
