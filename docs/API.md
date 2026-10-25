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
| `POST /api/eln/preview` (multipart `file`) | upload a .eln file; returns a `token` and what's inside (`items`, each with a `key`) without importing anything |
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
| `GET /api/doi?q=10.1021/jo971176v` | the paper's details from doi.org, as one such item (`q` may be a doi.org link). 400: not a DOI; 404: no such DOI; 502: doi.org unreachable or no details; 503: lookups turned off (`BENCHTOP_DOI=off`). Read & write tokens only |

What tokens can and can't do:

- A token acts as **you**. It sees what you can see, and everything it does is recorded in the audit trail under your name.
- **Read only** tokens can only read.
- Tokens can **never** sign, witness, return, withdraw or void records. They also can't manage tokens, passwords, sessions, users, webhooks or plugins. Those actions always need a person signed in.
- Only a SHA-256 hash of each token is stored. Revoke a token on the same page and it stops working straight away. Disabling an account stops all of its tokens.

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
