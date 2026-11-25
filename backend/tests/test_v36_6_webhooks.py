"""v0.36.6: webhooks with slow, silent, flooding or missing receivers; where a webhook may send; group-only records."""
import json
import socket
import threading
import time

import pytest

from app import webhooks
from app.config import settings
from tests.test_v2 import new_exp


class Receiver:
    """A stand-in receiver on this computer: answers properly, never answers, answers a byte at a time, floods, or
    redirects. Counts the requests it gets and the bytes it manages to send back."""

    def __init__(self, mode: str, location: str = ""):
        self.mode, self.location, self.hits, self.sent = mode, location, [], 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(50)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/hook"
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        try:
            c.recv(65536)
            self.hits.append(time.monotonic())
            if self.mode == "ok":
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            elif self.mode == "redirect":
                c.sendall(f"HTTP/1.1 302 Found\r\nLocation: {self.location}\r\nContent-Length: 0\r\n\r\n".encode())
            elif self.mode == "trickle":
                c.sendall(b"HTTP/1.1 200 OK\r\n")
                for _ in range(600):
                    c.sendall(b"X")
                    time.sleep(0.5)
            elif self.mode == "silent":
                time.sleep(120)
            elif self.mode == "flood":
                c.sendall(b"HTTP/1.1 500 Oops\r\nContent-Length: 200000000\r\n\r\n")
                chunk = b"x" * 65536
                while self.sent < 200_000_000:
                    c.sendall(chunk)
                    self.sent += len(chunk)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.sock.close()


@pytest.fixture()
def local(monkeypatch):
    """Webhooks may call this computer (as BENCHTOP_WEBHOOK_ALLOW=127.0.0.1 would allow), with no proxy in the way,
    and short limits so the tests don't wait long."""
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(settings, "webhook_allow", ["127.0.0.1"], raising=False)
    monkeypatch.setattr(webhooks, "READ_SECONDS", 1.0, raising=False)
    monkeypatch.setattr(webhooks, "TRY_SECONDS", 3.0, raising=False)
    monkeypatch.setattr(webhooks, "sender", None)
    made = []
    yield made
    for r in made:
        r.close()


@pytest.fixture(autouse=True)
def no_webhooks_left(client, admin):
    """Each test starts and ends with no webhooks, even when one fails part-way."""
    yield
    for h in client.get("/api/webhooks", headers=admin).json()["webhooks"]:
        client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def _hook(client, admin, url, events=("experiment.created",)):
    r = client.post("/api/webhooks", headers=admin, json={"name": "t", "url": url, "events": list(events)})
    assert r.status_code == 201, r.text
    return r.json()


def _timed(fn, limit):
    """fn() on a thread; (seconds it took, result), or (None, None) if it hadn't finished after `limit` seconds."""
    out = {}

    def run():
        t = time.monotonic()
        out["r"] = fn()
        out["t"] = time.monotonic() - t
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(limit)
    return out.get("t"), out.get("r")


# ---------------------------------------------------------------- slow, silent, flooding, gone (item 1)

def test_a_receiver_that_never_answers_or_answers_a_byte_at_a_time_is_given_up_quickly(client, admin, local):
    silent, trickle = Receiver("silent"), Receiver("trickle")
    local += [silent, trickle]
    for rcv, most in ((silent, 2.5), (trickle, 5)):
        h = _hook(client, admin, rcv.url)
        took, r = _timed(lambda: client.post(f"/api/webhooks/{h['id']}/test", headers=admin), 12)
        assert took is not None and took < most, f"{rcv.mode}: Send test took {took} s"
        d = r.json()
        assert not d["ok"] and "within" in d["error"] and "Error" not in d["error"], d
        client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_a_flood_of_a_reply_is_not_read(client, admin, local):
    flood = Receiver("flood")
    local.append(flood)
    h = _hook(client, admin, flood.url)
    took, r = _timed(lambda: client.post(f"/api/webhooks/{h['id']}/test", headers=admin), 30)
    assert r is not None and r.json()["status_code"] == 500 and r.json()["error"] == "HTTP 500"
    time.sleep(0.3)
    assert flood.sent < 20_000_000, f"Benchtop read {flood.sent / 1e6:.0f} MB of the reply"
    client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_a_receiver_that_is_gone_gets_a_plain_message(client, admin, local):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{s.getsockname()[1]}/hook"
    s.close()
    h = _hook(client, admin, url)
    d = client.post(f"/api/webhooks/{h['id']}/test", headers=admin).json()
    assert not d["ok"] and d["error"].startswith("Couldn't connect to 127.0.0.1") and "Errno" not in d["error"], d
    client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_a_silent_receiver_never_holds_up_another_webhook_or_the_request(client, admin, users, notebook, local, monkeypatch):
    monkeypatch.setattr(webhooks, "synchronous", False)
    silent, good = Receiver("silent"), Receiver("ok")
    local += [silent, good]
    hs = [_hook(client, admin, silent.url), _hook(client, admin, good.url)]
    t0 = time.monotonic()
    for _ in range(3):
        t = time.monotonic()
        new_exp(client, users["alice"], notebook)
        assert time.monotonic() - t < 1.5  # the request never waits for a delivery
    deadline = time.monotonic() + 8
    while len(good.hits) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(good.hits) == 3 and good.hits[-1] - t0 < 3, f"the good receiver got {len(good.hits)} in time"
    for h in hs:
        client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_retries_are_bounded_and_one_delivery_is_one_row(client, admin, users, notebook, monkeypatch):
    calls = []
    monkeypatch.setattr(webhooks, "synchronous", False)
    monkeypatch.setattr(webhooks, "RETRY_AFTER", (0.2, 0.2), raising=False)
    monkeypatch.setattr(webhooks, "sender", lambda url, raw, headers: calls.append(time.monotonic()) or 503)
    h = _hook(client, admin, "https://example.org/busy")
    new_exp(client, users["alice"], notebook)
    deadline = time.monotonic() + 10
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(1)
    assert len(calls) == 3  # three tries in all, never more
    rows = client.get(f"/api/webhooks/{h['id']}/deliveries", headers=admin).json()
    assert len(rows) == 1 and rows[0]["attempts"] == 3 and rows[0]["status_code"] == 503 and not rows[0]["ok"]
    assert "Trying again" not in rows[0]["error"]
    client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_the_admin_page_says_plainly_that_a_webhook_is_failing(client, admin, users, notebook, monkeypatch):
    monkeypatch.setattr(webhooks, "synchronous", True)
    code = {"n": 200}
    monkeypatch.setattr(webhooks, "sender", lambda *a: code["n"])
    h = _hook(client, admin, "https://example.org/sometimes")
    new_exp(client, users["alice"], notebook)
    code["n"] = 404
    for _ in range(3):
        new_exp(client, users["alice"], notebook)
    hook = next(x for x in client.get("/api/webhooks", headers=admin).json()["webhooks"] if x["id"] == h["id"])
    f = hook["failing"]
    assert f["count"] == 3 and f["last_ok_at"] and f["error"] == "HTTP 404"
    code["n"] = 200
    new_exp(client, users["alice"], notebook)
    hook = next(x for x in client.get("/api/webhooks", headers=admin).json()["webhooks"] if x["id"] == h["id"])
    assert hook["failing"] is None
    client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_deliveries_still_waiting_for_a_deleted_webhook_never_go_to_a_new_one(client, admin, users, notebook, local, monkeypatch):
    """A webhook deleted while its deliveries wait, and a new one made at once (it can get the same number)."""
    monkeypatch.setattr(webhooks, "synchronous", False)
    silent = Receiver("silent")
    local.append(silent)
    old = _hook(client, admin, silent.url)
    for _ in range(3):
        new_exp(client, users["alice"], notebook)
    time.sleep(0.3)
    client.delete(f"/api/webhooks/{old['id']}", headers=admin)
    calls = []
    monkeypatch.setattr(webhooks, "sender", lambda url, raw, headers: calls.append(json.loads(raw)["experiment"]["eid"]) or 200)
    new = _hook(client, admin, "https://example.org/new-receiver")
    exp = new_exp(client, users["alice"], notebook)
    deadline = time.monotonic() + 10
    while webhooks.pending() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls == [exp["eid"]], calls
    client.delete(f"/api/webhooks/{new['id']}", headers=admin)


# ---------------------------------------------------------------- where a webhook may send (item 2)

INSIDE = ["http://127.0.0.1:8000/api", "http://localhost:8000/", "http://LOCALHOST./x", "http://app.localhost/x", "http://[::1]/",
          "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://2130706433/", "http://0x7f.1/", "http://0177.0.0.1/",
          "http://127.1/", "http://0.0.0.0:8000/", "http://0/", "http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/",
          "http://10.0.0.5/hook", "http://172.17.0.1:5432/", "http://192.168.1.1/", "http://100.64.0.1/", "http://[fd00:ec2::254]/",
          "http://[fc00::1]/", "http://[64:ff9b::a00:1]/", "http://[2002:7f00:1::]/", "http://224.0.0.1/", "http://255.255.255.255/"]


@pytest.mark.parametrize("url", INSIDE)
def test_a_webhook_cant_point_at_this_computer_or_the_private_network(client, admin, url):
    r = client.post("/api/webhooks", headers=admin, json={"url": url, "events": ["experiment.created"]})
    assert r.status_code == 422, (url, r.text)
    assert "BENCHTOP_WEBHOOK_ALLOW" in r.json()["detail"], r.text


def test_an_existing_webhook_cant_be_moved_there_either(client, admin):
    h = _hook(client, admin, "https://hooks.example.org/x")
    r = client.patch(f"/api/webhooks/{h['id']}", headers=admin, json={"url": "http://169.254.169.254/"})
    assert r.status_code == 422
    assert client.post("/api/webhooks", headers=admin, json={"url": "http://[::1/", "events": ["experiment.created"]}).status_code == 422
    assert client.post("/api/webhooks", headers=admin, json={"url": "http://x.org:port/", "events": ["experiment.created"]}).status_code == 422
    client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_the_server_can_allow_its_labs_own_receiver(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow", ["10.20.0.0/16", "chat.lab.internal", "127.0.0.1"], raising=False)
    for url in ("http://10.20.3.4/hook", "https://chat.lab.internal/hooks/x", "http://127.0.0.1:9/hook"):
        r = client.post("/api/webhooks", headers=admin, json={"url": url, "events": ["experiment.created"]})
        assert r.status_code == 201, (url, r.text)
        client.delete(f"/api/webhooks/{r.json()['id']}", headers=admin)
    assert client.post("/api/webhooks", headers=admin, json={"url": "http://10.21.0.1/", "events": ["experiment.created"]}).status_code == 422


def test_a_name_that_points_inside_when_its_called_is_refused(client, admin, monkeypatch, local):
    """A name checked when the webhook was saved, then pointed at this computer: each delivery looks the name up,
    checks the address and calls that address."""
    rcv = Receiver("ok")
    local.append(rcv)
    port = int(rcv.url.split(":")[2].split("/")[0])
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "receiver.example.org":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr(settings, "webhook_allow", [], raising=False)
    h = _hook(client, admin, f"http://receiver.example.org:{port}/hook")
    d = client.post(f"/api/webhooks/{h['id']}/test", headers=admin).json()
    assert not d["ok"] and "receiver.example.org (127.0.0.1)" in d["error"] and "BENCHTOP_WEBHOOK_ALLOW" in d["error"], d
    assert rcv.hits == []
    client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_the_address_checked_is_the_address_called(monkeypatch):
    """No second look-up between the check and the call (a name can't be switched to an inside address in between)."""
    answers = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))])
    seen = {}

    class Stop(Exception):
        pass

    def stream(self, method, url, **kw):
        seen.update(url=str(url), host=kw["headers"].get("Host"), sni=kw.get("extensions", {}).get("sni_hostname"))
        raise Stop
    monkeypatch.setattr(webhooks.httpx.Client, "stream", stream)
    with pytest.raises(Stop):
        webhooks._post("https://hooks.example.org/x?y=1", b"{}", {})
    assert seen == {"url": "https://93.184.216.34:443/x?y=1", "host": "hooks.example.org", "sni": "hooks.example.org"}


def test_a_redirect_is_not_followed(client, admin, local):
    inside = Receiver("ok")
    redirect = Receiver("redirect", location=inside.url)
    local += [inside, redirect]
    h = _hook(client, admin, redirect.url)
    d = client.post(f"/api/webhooks/{h['id']}/test", headers=admin).json()
    assert not d["ok"] and d["status_code"] == 302 and "Redirects aren't followed" in d["error"]
    assert inside.hits == []
    client.delete(f"/api/webhooks/{h['id']}", headers=admin)


# ---------------------------------------------------------------- group-only records (item 3)

@pytest.fixture()
def capture(monkeypatch):
    sent = []
    monkeypatch.setattr(webhooks, "synchronous", True)
    monkeypatch.setattr(webhooks, "sender", lambda url, body, headers: sent.append(json.loads(body)) or 200)
    return sent


def test_group_only_records_stay_out_of_webhooks(client, admin, users, capture):
    g = client.post("/api/groups", headers=admin, json={"name": "Webhook fish lab"}).json()
    alice_id = client.get("/api/auth/me", headers=users["alice"]).json()["id"]
    client.put(f"/api/groups/{g['id']}/members/{alice_id}", headers=admin, json={"role": "member"})
    nb = client.post("/api/notebooks", headers=users["alice"], json={"name": "Fish", "group_id": g["id"], "visibility": "group"}).json()
    h = _hook(client, admin, "https://example.org/lab-channel", events=("experiment.created", "comment.created"))
    exp = client.post("/api/experiments", headers=users["alice"], json={"title": "Unpublished zebrafish line", "notebook_id": nb["id"]}).json()
    client.post(f"/api/experiments/{exp['id']}/comments", headers=users["alice"], json={"body": "note"})
    assert capture == [], [p["experiment"]["title"] for p in capture]
    # an open notebook's records still go, and ticking "include" sends the rest too
    open_nb = client.post("/api/notebooks", headers=users["alice"], json={"name": "Open"}).json()
    new_exp(client, users["alice"], open_nb)
    assert len(capture) == 1
    client.patch(f"/api/webhooks/{h['id']}", headers=admin, json={"include_private": True})
    client.post("/api/experiments", headers=users["alice"], json={"title": "Second fish", "notebook_id": nb["id"]})
    assert capture[-1]["experiment"]["title"] == "Second fish"
    client.delete(f"/api/webhooks/{h['id']}", headers=admin)


def test_someone_who_left_the_group_gets_no_replies_and_no_count_of_hidden_ones(client, admin, users):
    g = client.post("/api/groups", headers=admin, json={"name": "Reply lab"}).json()
    ids = {u: client.get("/api/auth/me", headers=users[u]).json()["id"] for u in ("alice", "bob")}
    for u in ("alice", "bob"):
        client.put(f"/api/groups/{g['id']}/members/{ids[u]}", headers=admin, json={"role": "member"})
    nb = client.post("/api/notebooks", headers=users["alice"], json={"name": "Reply NB", "group_id": g["id"], "visibility": "group"}).json()
    exp = client.post("/api/experiments", headers=users["alice"], json={"title": "Group-only reply test", "notebook_id": nb["id"]}).json()
    client.post("/api/notifications/read", headers=users["bob"], json={"all": True})
    first = client.post(f"/api/experiments/{exp['id']}/comments", headers=users["bob"], json={"body": "Looks fine"}).json()
    client.post(f"/api/experiments/{exp['id']}/comments", headers=users["alice"], json={"body": "Thanks", "parent_id": first["id"]})
    assert client.get("/api/notifications", headers=users["bob"]).json()["unread"] == 1
    client.delete(f"/api/groups/{g['id']}/members/{ids['bob']}", headers=admin)
    client.post(f"/api/experiments/{exp['id']}/comments", headers=users["alice"], json={"body": "One more thing", "parent_id": first["id"]})
    r = client.get("/api/notifications", headers=users["bob"]).json()
    assert r["unread"] == 0 and all(n["experiment"] is None or n["experiment"]["id"] != exp["id"] for n in r["items"]), r
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Notification
    with Session(engine) as s:  # nothing stored for him about it either (it held the reply's first words)
        texts = [n.text for n in s.exec(select(Notification).where(Notification.user_id == ids["bob"], Notification.experiment_id == exp["id"]))]
    assert not any("One more thing" in t for t in texts), texts
