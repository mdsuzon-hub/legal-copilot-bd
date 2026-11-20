import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _act(no, title, published, nsec, names):
    recs = []
    for i in range(nsec):
        recs.append(dict(
            id=0, source_file="x.json", volume_name="Regulation V of 1799 to Act I of 1872",
            act_name=title, act_no=no, published_date=published,
            short_title=f"A Regulation concerning {title.lower()}.",
            preamble="1. Whereas it is expedient to make provision; the Governor in Council enacts as follows.",
            repealed=False, repealed_notice="", related_act=["1"], num_of_sections=nsec,
            notes=["[1] The word \"Bengal\" was omitted by Article 6."],
            section_id=i + 1, section_name=f"{i + 2}. {names(i)}",
            section_text=f"{i + 2}. The Judge shall proceed under rule {i + 2} of this Regulation 1[Bangladesh]"
                         f" and the executors shall hand over the estate in case number {i + 2}."
                         + (" See also 16A of the former rule." if no == "XIX" and i == nsec - 1 else ""),
            section_related_acts=["388"]))
    return recs


@pytest.fixture()
def records():
    recs = (_act("V", "THE [***] WILLS AND INTESTACY REGULATION, 1799", "3rd May, 1799", 7,
                 lambda i: f"Estates case {i}")
            + _act("XIX", "THE [***] CHARITABLE ENDOWMENTS REGULATION, 1810", "14th December, 1810", 16,
                   lambda i: f"Endowment matter {i}"))
    for n, r in enumerate(recs):
        r["id"] = n
    return recs


@pytest.fixture()
def corpus(tmp_path, records):
    """Concatenated pretty-printed JSON (the sample.jsonl layout)."""
    p = tmp_path / "sample.jsonl"
    p.write_text("\n".join(json.dumps(r, indent=4, ensure_ascii=False) for r in records), encoding="utf-8")
    return p
