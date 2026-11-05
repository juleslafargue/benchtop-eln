# Connecting other programs: API tokens and webhooks

Benchtop's web interface talks to the server through a REST API, and other programs can use the same API. While Benchtop is running, the full list of endpoints is at `/docs` (for example http://localhost:8000/docs).

## API tokens: programs that call Benchtop

A script on an instrument PC, a data pipeline or a notebook (Jupyter, R) signs in with a **personal access token** instead of your password.

1. Go to **Account → API tokens → New token…**.
2. Name the token, and choose **Read only** or **Read & write** and when it expires.
3. Copy the token: Benchtop shows it **once**. It starts with `bt_`.

Send it as a bearer token:

```bash
curl -H "Authorization: Bearer bt_…" http://localhost:8000/api/experiments
```

```python
import httpx

BT = httpx.Client(base_url="http://localhost:8000", headers={"Authorization": "Bearer bt_…"})

# experiments in a notebook
for e in BT.get("/api/experiments", params={"notebook_id": 3}).json():
    print(e["eid"], e["title"], e["status"])

# attach an instrument file to an experiment (needs a read & write token)
with open("run42_HPLC.csv", "rb") as f:
    BT.post("/api/experiments/25/attachments", files={"file": f})
```

`GET /api/experiments` lists the experiments you can open (read token). `notebook_id`, `status`, `owner_id` and
`templates=true` narrow the list, and it comes in pages (see [Paging](#paging-fetching-every-row-of-a-list)). Each
experiment has `id`, `eid`, `title`, `description`, `status`, `notebook_id`, `notebook_name`, `is_template`,
`template_id`, `tags`, `owner` (`id`, `username`, `full_name`), `created_at`, `updated_at` and `sample_codes`.

Usually you don't need a script for instrument files: an admin can point Benchtop at the instrument's folder
(**Admin → Instruments**) and files named with an experiment number arrive by themselves. The Inbox has an API
too, for programs that sort files:

| Endpoint | What it does |
|---|---|
| `GET /api/inbox?state=inbox` | files waiting (`ignored` and `attached` for the other tabs); only those you may see |
| `GET /api/inbox/{id}/files/{n}` | download one of a waiting arrival's files |
| `POST /api/inbox/{id}/assign` `{"experiment_id": 25}` | attach it to a draft you can edit |
| `POST /api/inbox/{id}/follow-up` `{"notebook_id": 3}` | make a follow-up of the signed record it names, with the file in it |
| `POST /api/inbox/{id}/ignore`, `/unignore` | set it aside, or bring it back |
| `POST /api/experiments/{id}/follow-up` | a follow-up of a signed record, without a file |
| `POST /api/attachments/{id}/not-mine` | send an instrument file back to the Inbox (drafts only) |

Records travel between lab notebooks as **.eln** files (the ELN Consortium's format):

| Endpoint | What it does |
|---|---|
| `GET /api/experiments/{id}/export.eln` | one experiment |
| `GET /api/notebooks/{id}/export.eln` | every experiment in a notebook that you can open |
| `GET /api/export.eln` | everything you can open |
| `POST /api/eln/preview` (multipart `file`) | upload a .eln file; returns a `token` and what's inside (`items`, each with a `key`, and `already`: the experiment it was imported as before, `id` and `eid`, or `null`) without importing anything |
| `POST /api/eln/import/{token}` `{"notebook_id": 3, "items": ["0", "2"]}` | import those experiments; returns `created` (new EXP numbers) and any `failed`. While one import of an upload is running, another of the same upload gets 422 |
| `DELETE /api/eln/import/{token}` | discard an upload you decided not to import (otherwise deleted after a day) |

```python
with open("elabftw-export.eln", "rb") as f:
    p = BT.post("/api/eln/preview", files={"file": f}).json()
keep = [i["key"] for i in p["items"] if not i["already"]]          # skip ones imported before
print(BT.post(f"/api/eln/import/{p['token']}", json={"notebook_id": 3, "items": keep}).json())
```

A References section (`"kind": "references"`) holds `{"items": [...]}`, each item with `doi`, `title`, `authors` (a list,
family name first), `journal`, `year`, `volume`, `issue`, `pages` and `note`, all text. To fill one in from a DOI:

| Endpoint | What it does |
|---|---|
| `GET /api/doi?q=10.1021/jo971176v` | the paper's details from doi.org, as one such item (`q` may be a doi.org link). 400: not a DOI; 404: no such DOI; 502: doi.org unreachable or no details; 503: lookups turned off (`BENCHTOP_DOI=off`); 403: a read-only account. Either kind of token |

What tokens can and can't do:

- A token acts as **you**. It sees what you can see, and everything it does is recorded in the audit trail under your name.
- **Read only** tokens can only read.
- Tokens can **never** sign, witness, return, withdraw or void records, or publish them on Zenodo. They also can't manage tokens, passwords, sessions, users, webhooks or plugins. Those actions always need a person signed in.
- Only a SHA-256 hash of each token is stored. Revoke a token on the same page and it stops working straight away. Disabling an account stops all of its tokens.

Errors come back as JSON with a `detail`: a message you can show a person (for a 422 from a parameter check, a list
that names the parameter). On every endpoint:

- 401: the token is invalid, expired or revoked.
- 403: a read-only token tried to change something, or tokens never do this.

In the tables below, **Token** says which kind of token an endpoint needs: **read** (either kind) or **read & write**.

## Paging: fetching every row of a list

Lists come back as plain JSON lists, one page at a time. These lists take two query parameters:

- `limit`: at most this many rows, from 1 to 1000.
- `offset`: skip this many rows first (0 if left out).

Each answer has an `X-Total-Count` header: how many rows match, before `limit` and `offset`. If it's more than the
rows you have, there are more pages. A `limit` or `offset` out of range gets 422.

| List | Order | Rows when `limit` is left out |
|---|---|---|
| `GET /api/experiments` | most recently changed first | 200 |
| `GET /api/materials` | by name | 500 |
| `GET /api/lots` | by material name, then oldest lot first | all |
| `GET /api/lots/{id}/transactions` | newest first | all |
| `GET /api/compounds` | by name | all |
| `GET /api/search` | best match first | 100 of each kind |

Search answers with three lists in one object, and `limit` and `offset` apply to each of them. Its
`X-Total-Experiments`, `X-Total-Materials` and `X-Total-Files` headers count each list; `X-Total-Count` is the largest
of the three, so paging until `offset` reaches it fetches all three in full.

Before 0.31, `GET /api/materials` stopped at 500 without saying so, and had no `offset`.

```python
def fetch_all(path, page=1000, **params):
    """Every row of a list, one page at a time."""
    rows, offset = [], 0
    while True:
        r = BT.get(path, params={**params, "limit": page, "offset": offset})
        r.raise_for_status()
        rows += r.json()
        offset += page
        if offset >= int(r.headers["X-Total-Count"]):
            return rows

materials = fetch_all("/api/materials")
on_hand = fetch_all("/api/lots", status="available")
print(len(materials), "materials,", len(on_hand), "containers on hand")
```

`fetch_all` works for every list in the table except search, which answers with an object: add up each page's
`experiments`, `materials` and `files` in the same loop instead.

Pages are fetched one at a time, so a row added or changed in between can be missed or come twice. Experiments
move to the front of their list when they change. If that matters, drop duplicates by `id` and fetch again when
`X-Total-Count` has changed.

## Inventory: materials, containers and usage

The inventory has two levels. A **material** is a chemical, registered once (`MAT-00012`); a structure can be
registered only once. A **lot** is one container of it (a bottle, vial or bag) with a barcode, an amount and a
location. Each lot keeps a ledger of what was received, what was used and any corrections.

| Endpoint | What it does | Token |
|---|---|---|
| `GET /api/materials?q=…&kind=…` | materials by name. `q` matches part of the name, CAS number, synonyms or formula; `kind` is `reagent`, `solvent`, `sample`, `product` or `other` | read |
| `GET /api/materials/{id}` | one material with its `lots`, and `used_in`: the experiments you can open whose reaction tables use it. 404: no such material | read |
| `POST /api/materials` `{"name": "Triethylamine", "cas": "121-44-8", "smiles": "CCN(CC)CC", "density": 0.726}` | register a material; returns it (201). Only `name` is needed. 409: the structure is registered already (the message gives its MAT number); 422: a structure Benchtop can't read | read & write |
| `PATCH /api/materials/{id}` | change `name`, `kind`, `smiles`, `cas`, `density`, `synonyms`, `hazards`, `notes` or `mw`. 404: no such material; 422: a structure Benchtop can't read | read & write |
| `POST /api/materials/{id}/lots` `{"barcode": "SA-0042", "amount": 100, "unit": "g", "location": "Cabinet B2", "supplier": "Sigma-Aldrich"}` | receive a lot; returns it (201). Also `catalog_number`, `purity` (%) and `expires_at`. Without a barcode, Benchtop makes one like `MAT-00012-L0034`. 400: unknown unit, or an amount that isn't positive; 404: no such material | read & write |
| `GET /api/lots?q=…&location=…&status=…` | lots, each with its `material_name` and `mid`. `q` matches part of a barcode, material name or CAS number; `location` matches part of the location; `status` is `available`, `empty`, `quarantined` or `disposed` | read |
| `PATCH /api/lots/{id}` | change `location`, `status`, `purity`, `supplier` or `expires_at`. 404: no such lot | read & write |
| `POST /api/lots/{id}/transactions` `{"amount": 1.25, "unit": "g"}` | record an amount taken out of a lot (`"direction": "in"` puts an amount back, for a correction). Optional: `experiment_id` and `note`. Returns the updated `lot` and the new `transaction_id` (201). A lot that reaches zero becomes `empty`. 400: unknown unit or direction, an amount that isn't positive, or a mass for a liquid (or a volume for a solid) when the material has no density; 404: no such lot, or an experiment you can't open; 409: not that much left | read & write |
| `GET /api/lots/{id}/transactions` | the lot's ledger, newest first. 404: no such lot | read |
| `POST /api/lots/transactions/{id}/reverse` | undo a ledger entry by adding the opposite one; the original stays in the ledger. Returns `lot` and `transaction_id` (201). 404: no such entry; 409: already reversed, itself a reversal, or not that much left | read & write |
| `POST /api/materials/register-product` `{"experiment_id": 25, "name": "KX-107", "smiles": "…", "amount_mg": 182}` | register a product of an experiment you can open, with a lot of `amount_mg` mg if given (also `purity` and `location`). A structure registered already is reused. Returns `material` and `lot` (`null` without an amount) (201). 404: no such experiment, or one you can't open; 422: a structure Benchtop can't read | read & write |

Units are `mg`, `g`, `kg`, `uL`, `mL` and `L`. An amount used is converted to the lot's unit; between mass and volume
that needs the material's `density` (g/mL).

- A **material** has `id`, `mid`, `name`, `kind`, `cas`, `smiles`, `formula`, `mw`, `inchikey`, `density`, `synonyms`,
  `hazards`, `notes`, `created_at`, `lot_count` and `available_lots`.
- A **lot** has `id`, `material_id`, `barcode`, `supplier`, `catalog_number`, `purity`, `amount`, `unit`, `location`,
  `status`, `received_at` and `expires_at`.
- A **ledger entry** has `id`, `delta` (in the lot's unit; negative when used), `balance_after`, `note`, `created_at`,
  `user` (`id`, `username`, `full_name`) and `experiment` (`id` and `eid`, or `null`; the `id` is `null` for an
  experiment you can't open).

Times are UTC, written like `2026-10-17T21:14:03Z`; numbers you didn't give are `null`.

Look up a bottle by its barcode:

```python
def find_bottle(barcode):
    """The lot with exactly this barcode, or None (q also matches part of a barcode)."""
    for lot in BT.get("/api/lots", params={"q": barcode}).json():
        if lot["barcode"] == barcode:
            return lot
    return None

lot = find_bottle("SA-0042")
print(lot["material_name"], lot["mid"], lot["amount"], lot["unit"], lot["location"])
```

Record what you used (add `"experiment_id": 25` to link it to an experiment):

```python
r = BT.post(f"/api/lots/{lot['id']}/transactions", json={"amount": 1.25, "unit": "g", "note": "Suzuki coupling"})
if r.status_code == 409:
    print(r.json()["detail"])  # Only 0.8 g left in SA-0042
else:
    r.raise_for_status()
    left = r.json()["lot"]
    print("left:", left["amount"], left["unit"])
    # a mistake? BT.post(f"/api/lots/transactions/{r.json()['transaction_id']}/reverse") undoes it
```

Load a CSV of bottles, one per row, skipping barcodes already registered. Each row's material is found by its CAS
number (or its name, without one) and registered if it's new. Rename the columns to match your file, for example an
export from another inventory system:

```python
import csv

# bottles.csv columns: name, cas, barcode, amount, unit, location, supplier
with open("bottles.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if find_bottle(row["barcode"]):
            print("already registered:", row["barcode"])
            continue
        cas = row["cas"].strip()
        found = BT.get("/api/materials", params={"q": cas or row["name"], "limit": 1000}).json()
        if cas:
            mat = next((m for m in found if m["cas"] == cas), None)
        else:
            mat = next((m for m in found if m["name"].lower() == row["name"].lower()), None)
        if mat is None:
            r = BT.post("/api/materials", json={"name": row["name"], "cas": cas})
            r.raise_for_status()
            mat = r.json()
        r = BT.post(f"/api/materials/{mat['id']}/lots",
                    json={"barcode": row["barcode"], "amount": float(row["amount"]), "unit": row["unit"],
                          "location": row["location"], "supplier": row["supplier"]})
        r.raise_for_status()
        print("added", row["barcode"], "to", mat["mid"], mat["name"])
```

## Compound registry

| Endpoint | What it does | Token |
|---|---|---|
| `GET /api/compounds` | one row per structure, by name: every product in a reaction table of an experiment you can open, and every material registered as a `product` or `sample` | read |

Each row has `name`, `smiles`, `formula` and `mw`; the descriptors `logp`, `tpsa`, `hbd`, `hba`, `rotatable_bonds`,
`heavy_atoms`, `aromatic_rings`, `fsp3` and `qed` (calculated with RDKit; `qed` is `null` when RDKit can't work it out);
`lipinski_violations` and `veber_violations`, lists of the rules broken (like `"MW > 500"`), empty when none are;
`experiments`, newest first, each with `id`, `eid`, `title`, `status`, `date` and `yield_pct`; `best_yield`; and
`material` (`id` and `mid`, or `null` when it isn't registered).

```python
for c in BT.get("/api/compounds").json():
    if c["material"] is None and c["experiments"]:
        print(c["name"] or c["formula"], "made in", c["experiments"][0]["eid"], "is not registered")
```

## Sample codes

| Endpoint | What it does | Token |
|---|---|---|
| `GET /api/samples/lookup?code=JL-013` | the experiment a sample code was given to: `code` (written as Benchtop writes it) and `experiment` (the same fields as `GET /api/experiments` except `sample_codes`). The code can also be typed `jl-13`, `JL_013`, `JL 013` or `JL013`. 404: no experiment you can open has that code, which is also the answer for a code never given out or taken off a draft | read |

```python
r = BT.get("/api/samples/lookup", params={"code": "JL-013"})
if r.status_code == 404:
    print("No experiment you can open has JL-013")
else:
    e = r.json()["experiment"]
    print(r.json()["code"], "is from", e["eid"], e["title"])
```

## Search

| Endpoint | What it does | Token |
|---|---|---|
| `GET /api/search?q=…` | experiments, materials and files that contain every word of `q` (or "quoted phrase"). Experiments match on their ID, title, tags, notebook, author, sample codes, section text, table values and file names; materials on their name, MAT number, CAS number, synonyms, formula or lot barcodes | read |
| `GET /api/search?q=…&mode=substructure` | experiments whose reaction tables, and materials whose structures, contain this structure (SMILES or SMARTS) | read |
| `GET /api/search?q=…&mode=similarity&threshold=0.6` | structures similar to this one (SMILES); `threshold` from 0 to 1, 0.5 if left out | read |

Only experiments you can open are searched. `status` (`open`, `signed`, `witnessed`, `voided` or `locked`),
`notebook_id` and `mine=true` narrow the experiments. `q` is cut at 500 characters; an empty `q` gives empty lists.
400: unknown `mode`; 422: a structure Benchtop can't read.

The answer has four lists:

- `experiments`: the same fields as `GET /api/experiments` except `sample_codes`, plus `score`, `match` (where it
  matched) and, in text mode, `snippet`.
- `materials`: `id`, `mid`, `name`, `smiles`, `formula`, `mw`, `cas`, `kind` and `score`.
- `files` (text mode only): `id`, `filename`, `size`, `experiment_id`, `eid`, `title` and `created_at`.
- `terms`: the words searched for (text mode only).

```python
r = BT.get("/api/search", params={"q": "Suzuki", "status": "witnessed", "limit": 50})
print(r.headers["X-Total-Experiments"], "witnessed experiments mention Suzuki")
for e in r.json()["experiments"]:
    print(e["eid"], e["title"], "·", e["match"])
```

## Webhooks: Benchtop calls other programs

An admin adds webhooks under **Admin → Integrations** (Webhooks). Benchtop then calls the address when one of the chosen events happens:

| Event | When |
|---|---|
| `experiment.created` | An experiment is created or cloned |
| `experiment.signed` | The author signs (it now needs a witness) |
| `experiment.witnessed` | It's witnessed |
| `experiment.returned` | A witness returns it to the author |
| `experiment.withdrawn` | The author withdraws their signature |
| `experiment.voided` | It's voided |
| `comment.created` | Someone comments on an experiment |
| `material.created` | A material is registered |

### Slack and Teams

Paste an incoming-webhook address from Slack or Microsoft Teams and pick **Slack message** or **Teams message**. Each event becomes one line with a link, for example:

> Priya Nair signed EXP-00025: KX-107 reductive amination with morpholine (STAB) · Open

Slack addresses are recognised automatically. Discord, Mattermost and Rocket.Chat accept the Slack format too.

### JSON, for your own code

The **JSON** format sends the whole event:

```json
{
  "event": "experiment.signed",
  "timestamp": "2026-10-17T21:14:03Z",
  "benchtop": "0.10.0",
  "actor": {"username": "pnair", "name": "Priya Nair"},
  "experiment": {"id": 25, "eid": "EXP-00025", "title": "KX-107: reductive amination…", "status": "signed",
                 "author": "Priya Nair", "notebook": "KX-series kinase inhibitor analogues",
                 "url": "https://eln.mylab.ca/experiments/25"}
}
```

Each request carries these headers:
- `X-Benchtop-Event`: the event name.
- `X-Benchtop-Delivery`: a unique id.
- `X-Benchtop-Signature: sha256=<hex>`: an HMAC-SHA256 of the raw body, made with the webhook's secret. The secret is shown when you create the webhook, and **New secret** makes another.

Check the signature before trusting a request:

```python
import hashlib, hmac

def is_from_benchtop(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")
```

### What's sent, and when

- **Identifiers only, never record content.** A payload has the experiment's number, title, status, author, notebook and link, plus who did what. A returned or voided experiment also carries the reason given.
- **Private notebooks are left out** unless you tick *Include experiments in private notebooks*.
- **Events are sent only after the change is saved.** A failed action, like a wrong signing password, sends nothing.
- **Delivery runs in the background.** It's retried up to three times when the receiver is down or busy (network errors, HTTP 5xx or 429). It isn't retried when the receiver refuses (other 4xx).
- **History:** each webhook keeps its last 50 deliveries. **Send test** sends a test message.
- **Links:** set `BENCHTOP_PUBLIC_URL` (for example `https://eln.mylab.ca`) so the links in messages point somewhere colleagues can open.

## What stays stable

- The endpoints, parameters, fields and error codes documented in this file are kept stable across releases.
- Things can be added: new endpoints, new parameters and new fields in an answer. Write scripts that ignore fields
  they don't know.
- Any removal or change of something documented here is announced in the release notes.
- Anything not documented here, seen only at `/docs`, may change with the app.

`backend/tests/test_api_contract.py` calls each endpoint documented here with an API token and checks the documented
fields, so a change can't slip into a release unnoticed.
