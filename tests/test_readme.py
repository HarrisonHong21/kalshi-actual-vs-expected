"""The README must quote the script's output exactly. study.py writes the headline numbers to
output/key_numbers.json and the tables to output/tables.md; these tests fail if the README has
drifted from either, for example after the study is re-run on more data."""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()
NUMBERS = json.loads((ROOT / "output" / "key_numbers.json").read_text())
TABLE_ROWS = [line for line in (ROOT / "output" / "tables.md").read_text().splitlines() if line.startswith("|")]


def test_readme_quotes_every_headline_number():
    missing = {name: value for name, value in NUMBERS.items() if isinstance(value, str) and value not in README}
    assert not missing


def test_readme_tables_match_the_output_row_for_row():
    missing = [row for row in TABLE_ROWS if row not in README]
    assert not missing


def test_claim_that_no_pooled_range_excludes_zero_still_holds():
    # The README and the PDF both say this in words, so it has to stay true.
    assert NUMBERS["all_pooled_ranges_include_zero"] is True
