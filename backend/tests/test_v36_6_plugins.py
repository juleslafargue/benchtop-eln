"""v0.36.6: plugin files with mistakes get a plain message naming the mistake, never an Internal Server Error, and
limits such as min_rows and decimals are checked. Sections made before keep opening and saving."""
from __future__ import annotations

import pytest

HEAD = '[plugin]\nid = "bad-366"\nname = "Bad"\n'
SEC = HEAD + '[[sections]]\nid = "s"\n'
NUM = SEC + '[[sections.fields]]\nid = "f"\ntype = "number"\n'
TABLE = SEC + '[[sections.tables]]\nid = "t"\n{}\n[[sections.tables.columns]]\nid = "c"\n'

BAD = {
    "sections is text": ('sections = "x"\n' + HEAD, "sections must be a list"),
    "sections are numbers": ('sections = [1, 2]\n' + HEAD, "each section must be a table"),
    "readers is text": ('readers = "x"\n' + HEAD, "readers must be a list"),
    "fields is a number": (SEC + 'fields = 5\n', "fields must be a list"),
    "tables is one table": (SEC + '[sections.tables]\nid = "t"\n', "tables must be a list"),
    "a table that is text": (SEC + 'tables = ["t"]\n', "each table must be a table"),
    "columns is text": (SEC + '[[sections.tables]]\nid = "t"\ncolumns = "abc"\n', "columns must be a list"),
    "an option that is a table": (SEC + '[[sections.fields]]\nid = "f"\ntype = "select"\noptions = [{a = 1}]\n', "each option"),
    "61 options": (SEC + '[[sections.fields]]\nid = "f"\ntype = "select"\noptions = [' + ",".join(f'"o{i}"' for i in range(61)) + "]\n",
                   "up to 60 options"),
    "plugin id a number": ('[plugin]\nid = 5\n[[sections]]\nid = "s"\n[[sections.fields]]\nid = "f"\n', "plugin id"),
    "api in quotes": (HEAD + 'api = "1"\n[[sections]]\nid = "s"\n[[sections.fields]]\nid = "f"\n', "api must be a whole number"),
    "required in quotes": (SEC + '[[sections.fields]]\nid = "f"\nrequired = "no"\n', "required must be true or false"),
    "number default in words": (NUM + 'default = "abc"\n', "default must be a number"),
    "select default not an option": (SEC + '[[sections.fields]]\nid = "f"\ntype = "select"\noptions = ["a"]\ndefault = "b"\n',
                                     "isn't one of the options"),
    "min above max": (NUM + 'min = 10\nmax = 1\n', "min (10) is larger than max (1)"),
    "decimals 2.5": (NUM + 'decimals = 2.5\n', "decimals must be a whole number from 0 to 10"),
    "decimals -1": (NUM + 'decimals = -1\n', "decimals must be from 0 to 10"),
    "decimals 500": (NUM + 'decimals = 500\n', "decimals must be from 0 to 10"),
    "min_rows -3": (TABLE.format("min_rows = -3"), "min_rows must be from 0 to 500, not -3"),
    "min_rows 2.5": (TABLE.format("min_rows = 2.5"), "min_rows must be a whole number from 0 to 500"),
    "min_rows 1000": (TABLE.format("min_rows = 1000"), "min_rows must be from 0 to 500, not 1000"),
    "min_rows 100 million": (TABLE.format("min_rows = 100000000"), "min_rows must be from 0 to 500"),
    "min_rows in words": (TABLE.format('min_rows = "three"'), "min_rows must be a whole number"),
    "min_rows true": (TABLE.format("min_rows = true"), "min_rows must be a whole number"),
    "reader extensions numbers": (HEAD + '[[readers]]\nid = "r"\ndetect = "x"\nextensions = [1]\n', "extensions must be a list"),
    "reader column 1.5": (HEAD + '[[readers]]\nid = "r"\ndetect = "x"\nx = 1.5\n', "x must be a column number"),
    "reader column a list": (HEAD + '[[readers]]\nid = "r"\ndetect = "x"\ny = [2]\n', "y must be a column number"),
    "reader scale 0": (HEAD + '[[readers]]\nid = "r"\ndetect = "x"\nx_scale = 0\n', "x_scale can't be 0"),
    "reader scale in words": (HEAD + '[[readers]]\nid = "r"\ndetect = "x"\ny_scale = "big"\n', "y_scale must be a number"),
    "reader flag in quotes": (HEAD + '[[readers]]\nid = "r"\ndetect = "x"\nreversed = "yes"\n', "reversed must be true or false"),
    "reader detect a list": (HEAD + '[[readers]]\nid = "r"\ndetect = ["x"]\n', "detect must be text"),
}


@pytest.mark.parametrize("name", list(BAD))
def test_a_plugin_with_a_mistake_gets_a_plain_message(client, admin, name):
    text, says = BAD[name]
    r = client.post("/api/plugins/check", headers=admin, files={"file": ("p.toml", text.encode())})
    assert r.status_code == 200 and r.json()["ok"] is False and says in r.json()["error"], (name, r.status_code, r.text)
    r = client.post("/api/plugins", headers=admin, files={"file": ("p.toml", text.encode())})
    assert r.status_code == 422 and says in r.json()["detail"], (name, r.status_code, r.text)


GOOD = TABLE.format("min_rows = 500").replace('id = "c"\n', 'id = "c"\ntype = "number"\ndecimals = 0\nmin = 0\nmax = 0\n') + \
    '[[sections.fields]]\nid = "g"\ntype = "select"\noptions = [1, "two"]\ndefault = "two"\n'


def test_the_edges_of_each_limit_are_accepted(client, admin):
    r = client.post("/api/plugins/check", headers=admin, files={"file": ("p.toml", GOOD.encode())})
    assert r.json()["ok"] is True, r.text


def test_a_broken_file_in_the_plugins_folder_doesnt_break_the_lists(client, admin, users):
    from app import plugins
    f = plugins.user_dir() / "dropped-366.toml"
    f.write_text('sections = "x"\n[plugin]\nid = "dropped-366"\n', encoding="utf-8")
    try:
        r = client.get("/api/plugins", headers=admin)
        assert r.status_code == 200, r.text
        p = next(p for p in r.json()["plugins"] if p["id"] == "dropped-366")
        assert not p["enabled"] and "sections must be a list" in p["errors"][0]
        assert client.get("/api/plugins/sections", headers=users["alice"]).status_code == 200
        assert client.get("/api/plugins/readers", headers=users["alice"]).status_code == 200
    finally:
        f.unlink()


ODD = """
[plugin]
id = "odd-limits-366"
name = "Odd limits"

[[sections]]
id = "log"
name = "Log"
  [[sections.fields]]
  id = "dose"
  type = "number"
  decimals = 2
  [[sections.tables]]
  id = "rows"
  min_rows = 2
    [[sections.tables.columns]]
    id = "what"
"""


def test_a_section_stored_with_odd_limits_still_opens_and_saves_tidied(client, admin, users, notebook):
    """A definition stored with a section before 0.36.6 could hold min_rows = -3 or decimals = 500 (the checks let
    them through). Such a section must still save; the odd values are tidied, never refused."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Entry
    assert client.post("/api/plugins", headers=admin, files={"file": ("odd.toml", ODD.encode())}).status_code == 201
    a = users["alice"]
    exp = client.post("/api/experiments", headers=a, json={"title": "Odd limits", "notebook_id": notebook["id"],
                                                           "sections": "no_chemistry"}).json()
    e = client.post(f"/api/experiments/{exp['id']}/entries", headers=a, json={"kind": "custom", "plugin_section": "odd-limits-366/log"}).json()
    with Session(engine) as s:  # what an older Benchtop would have stored
        en = s.get(Entry, e["id"])
        c = dict(en.content)
        schema = {**c["schema"], "fields": [{**c["schema"]["fields"][0], "decimals": 500}],
                  "tables": [{**c["schema"]["tables"][0], "min_rows": -3}]}
        en.content = {**c, "schema": schema}
        s.add(en)
        s.commit()
    got = client.get(f"/api/experiments/{exp['id']}", headers=a).json()
    content = next(x for x in got["entries"] if x["id"] == e["id"])["content"]
    r = client.put(f"/api/entries/{e['id']}", headers=a, json={"content": {**content, "values": {"dose": 1.5}}})
    assert r.status_code == 200, r.text
    sch = r.json()["content"]["schema"]
    assert sch["fields"][0]["decimals"] == 10 and sch["tables"][0]["min_rows"] == 0
    assert r.json()["content"]["values"]["dose"] == 1.5
    client.delete("/api/plugins/odd-limits-366", headers=admin)
