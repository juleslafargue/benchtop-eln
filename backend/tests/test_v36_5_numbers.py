"""0.36.5: wrong numbers with no warning (found by the 0.36.2 stress test of instrument data and spectra).

- A spectrum's saved analysis keeps the spectrometer frequency typed in for a ¹H file that doesn't give it (and says
  so when none was), and whether negative ¹³C lines are listed; an older analysis keeps having neither.
"""
from __future__ import annotations

import pytest

PW = "lab-bench-pw-2026"


def _exp(client, h, title="Numbers"):
    nb = client.post("/api/notebooks", headers=h, json={"name": f"NB {title}"}).json()
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- spectra: what an analysis saves

@pytest.mark.parametrize("sent, kept", [
    ({"frequency": 60}, {"frequency": 60.0}),
    ({"frequency": "59.7"}, {"frequency": 59.7}),
    ({"frequency": None}, {"frequency": None}),      # a new analysis of a file without a frequency, none typed
    ({"frequency": 5000}, {"frequency": None}),      # not a spectrometer
    ({"frequency": "abc"}, {"frequency": None}),
    ({}, {}),                                        # an older analysis: no key, so the viewer keeps its 400 MHz J
    ({"negatives": True}, {"negatives": True}),
    ({"negatives": "yes"}, {}),
])
def test_a_spectrum_analysis_keeps_the_frequency_typed_in(client, users, sent, kept):
    h = users["alice"]
    exp = _exp(client, h, "Frequency")
    analysis = {"integrals": [[3.8, 4.4]], "ref_idx": 0, "ref_value": 2, "threshold": 0.05, **sent}
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=h,
                    json={"kind": "file", "title": "Spectra", "content": {"attachment_ids": [], "analyses": {"7": analysis}}})
    assert r.status_code == 201, r.text
    sec = next(e for e in client.get(f"/api/experiments/{exp['id']}", headers=h).json()["entries"] if e["title"] == "Spectra")
    got = sec["content"]["analyses"]["7"]
    assert {k: got[k] for k in ("frequency", "negatives") if k in got} == kept
    assert got["integrals"] == [[3.8, 4.4]]
