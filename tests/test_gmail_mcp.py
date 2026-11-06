import asyncio
import base64
import importlib
import time
import types
from email.message import EmailMessage

import keyring
import pytest

from gmail_mcp import accounts, gmail, server


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    store = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(accounts, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(accounts, "REGISTRY_FILE", tmp_path / "accounts.json")
    server._PENDING.clear()


def run(coro):
    return asyncio.run(coro)


def reg(**accts):
    accounts.save_registry({e: {"alias": a, "added": "2026-01-01"} for e, a in accts.items()})


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# ---- resolve_account ----
def test_resolve_single_implicit():
    accounts.save_registry({"a@x.com": {"alias": None, "added": "d"}})
    assert accounts.resolve_account(None) == "a@x.com"


def test_resolve_alias_case_insensitive_and_errors():
    accounts.save_registry({"a@x.com": {"alias": "Work", "added": "d"}, "b@y.com": {"alias": None, "added": "d"}})
    assert accounts.resolve_account("WORK") == "a@x.com"
    assert accounts.resolve_account("B@Y.COM") == "b@y.com"
    with pytest.raises(ValueError, match=r"a@x.com \(Work\), b@y.com"):
        accounts.resolve_account(None)
    with pytest.raises(ValueError, match="Unknown account"):
        accounts.resolve_account("nope")


def test_resolve_no_accounts():
    with pytest.raises(ValueError, match="add-account"):
        accounts.resolve_account(None)


# ---- _extract_text ----
def test_extract_text_prefers_plain_nested_and_html_fallback():
    nested = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>HTML &amp; stuff</p>")}},
            {"mimeType": "text/plain", "body": {"data": b64("plain text")}},
        ]},
        {"mimeType": "application/pdf", "filename": "a.pdf", "body": {}},
    ]}
    assert gmail._extract_text(nested) == "plain text"
    html_only = {"mimeType": "multipart/alternative",
                 "parts": [{"mimeType": "text/html", "body": {"data": b64("<b>Hi</b> &amp; bye")}}]}
    assert gmail._extract_text(html_only) == "Hi & bye"
    assert gmail._extract_text({"mimeType": "text/plain", "body": {}}) == ""


# ---- build_message ----
def test_build_message():
    m = gmail.build_message("me@x.com", ["a@x.com", "b@x.com"], "Hi", "body", cc=["c@x.com"],
                            attachments=[("f.txt", b"data", "text/plain")])
    assert m["From"] == "me@x.com" and m["To"] == "a@x.com, b@x.com" and m["Cc"] == "c@x.com"
    assert [p.get_filename() for p in m.iter_attachments()] == ["f.txt"]


# ---- reply / forward with a fake service ----
def make_original():
    m = EmailMessage()
    m["From"] = "Alice <alice@x.com>"
    m["To"] = "me@x.com, bob@x.com"
    m["Cc"] = "carol@x.com"
    m["Subject"] = "Plans"
    m["Date"] = "Mon, 1 Jan 2026 10:00:00 +0000"
    m["Message-ID"] = "<m2@x.com>"
    m["References"] = "<m1@x.com>"
    m.set_content("line one\nline two")
    m.add_attachment(b"PDFDATA", maintype="application", subtype="pdf", filename="doc.pdf")
    return m


class Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeService:
    def __init__(self):
        self.sent = []
        self.raw = base64.urlsafe_b64encode(make_original().as_bytes()).decode()

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **kw):
        return Call(lambda: {"raw": self.raw, "threadId": "T1"})

    def send(self, userId, body):
        self.sent.append(body)
        return Call(lambda: {"id": "sent1", "threadId": body.get("threadId", "newT")})


def test_build_reply_all_excludes_sender():
    msg, tid = gmail.build_reply(FakeService(), "ME@x.com", "m2", "thanks", reply_all=True)
    assert tid == "T1"
    assert msg["Subject"] == "Re: Plans" and msg["To"] == "Alice <alice@x.com>"
    assert "me@x.com" not in msg["Cc"].lower() and "bob@x.com" in msg["Cc"] and "carol@x.com" in msg["Cc"]
    assert msg["In-Reply-To"] == "<m2@x.com>" and msg["References"] == "<m1@x.com> <m2@x.com>"
    body = msg.get_content()
    assert body.startswith("thanks") and "> line one" in body and "Alice <alice@x.com> wrote:" in body


def test_build_forward_reattaches_and_no_double_prefix():
    msg = gmail.build_forward(FakeService(), "me@x.com", "m2", ["z@x.com"], "fyi")
    assert msg["Subject"] == "Fwd: Plans"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["doc.pdf"]
    assert "Forwarded message" in msg.get_body(("plain",)).get_content()
    svc = FakeService()
    svc.raw = base64.urlsafe_b64encode(b"Subject: FW: x\nFrom: a@x.com\n\nhi\n").decode()
    assert gmail.build_forward(svc, "me@x.com", "m", ["z@x.com"])["Subject"] == "FW: x"


# ---- confirmation gate ----
def test_gate_single_use_account_kind_expiry(monkeypatch):
    out = server._stage("a@x.com", "send_email", {}, {"k": 1})
    with pytest.raises(ValueError, match="different account"):
        server._take(out["confirm_token"], "b@x.com", "send_email")
    out = server._stage("a@x.com", "send_email", {}, {"k": 1})
    with pytest.raises(ValueError, match="different action"):
        server._take(out["confirm_token"], "a@x.com", "forward_email")
    out = server._stage("a@x.com", "send_email", {}, {"k": 1})
    assert server._take(out["confirm_token"], "a@x.com", "send_email") == {"k": 1}
    with pytest.raises(ValueError, match="invalid or expired"):
        server._take(out["confirm_token"], "a@x.com", "send_email")
    out = server._stage("a@x.com", "send_email", {}, {"k": 1})
    real = time.time()
    monkeypatch.setattr(server.time, "time", lambda: real + server.CONFIRM_TTL + 1)
    with pytest.raises(ValueError, match="invalid or expired"):
        server._take(out["confirm_token"], "a@x.com", "send_email")


# ---- send_email tool ----
def test_send_email_two_step(monkeypatch):
    accounts.save_registry({"me@x.com": {"alias": None, "added": "d"}})
    svc = FakeService()
    monkeypatch.setattr(accounts, "gmail_service", lambda e: svc)
    kw = dict(to=["a@x.com"], subject="S", body="B")
    first = run(server.send_email(**kw))
    assert first["status"] == "needs_confirmation" and svc.sent == []
    assert first["preview"]["to"] == "a@x.com" and first["from_account"] == "me@x.com"
    done = run(server.send_email(**kw, confirm_token=first["confirm_token"]))
    assert len(svc.sent) == 1 and done["from_account"] == "me@x.com" and done["message_id"] == "sent1"
    with pytest.raises(ValueError, match="invalid or expired"):
        run(server.send_email(**kw, confirm_token=first["confirm_token"]))
    assert len(svc.sent) == 1
    tok = run(server.send_email(**kw))["confirm_token"]
    with pytest.raises(ValueError, match="arguments changed"):
        run(server.send_email(to=["evil@x.com"], subject="S", body="B", confirm_token=tok))
    assert len(svc.sent) == 1
    with pytest.raises(ValueError, match="Invalid email"):
        run(server.send_email(to=["not-an-address"], subject="S", body="B"))


def test_forward_reattaches_text_attachment_and_addr_validation():
    o = EmailMessage()
    o["From"], o["To"], o["Subject"], o["Message-ID"] = "x@y.com", "me@z.com", "Report", "<m1@y.com>"
    o.set_content("see attached")
    o.add_attachment("a,b\n1,2\n", filename="data.csv")
    raw = base64.urlsafe_b64encode(o.as_bytes()).decode()

    class Svc:
        def users(self): return self
        def messages(self): return self
        def get(self, **kw): return self
        def execute(self): return {"raw": raw, "threadId": "t1"}

    fwd = gmail.build_forward(Svc(), "me@z.com", "m1", ["dest@q.com"])
    atts = list(fwd.iter_attachments())
    assert [a.get_filename() for a in atts] == ["data.csv"]
    assert atts[0].get_content() == "a,b\n1,2\n"
    assert str(fwd["Subject"]) == "Fwd: Report"
    with pytest.raises(ValueError, match="Invalid email"):
        server._check_addrs(["a@b.com, c@d.com"], "to")
    assert server._check_addrs(["Bob <bob@b.com>"], "to") == ["Bob <bob@b.com>"]


def _svc(raw):
    class Svc:
        def users(self): return self
        def messages(self): return self
        def get(self, **kw): return self
        def execute(self): return {"raw": raw, "threadId": "t1"}
    return Svc()


def _raw(msg):
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def test_alias_cannot_shadow_email_and_at_rejected(monkeypatch):
    reg(**{"a@x.com": "b@y.com", "b@y.com": None})
    assert accounts.resolve_account("b@y.com") == "b@y.com"
    assert accounts.resolve_account("a@x.com") == "a@x.com"
    monkeypatch.setattr(accounts, "CREDENTIALS_FILE", accounts.REGISTRY_FILE)  # exists; fail before OAuth
    with pytest.raises(ValueError, match="alias must not"):
        accounts.add_account("x@y.com")


def test_reply_to_own_message_targets_original_recipients():
    o = EmailMessage()
    o["From"], o["To"], o["Cc"], o["Subject"] = "Me <me@z.com>", "bob@b.com", "carol@c.com", "Hi"
    o["Message-ID"] = "<m1@z.com>"
    o.set_content("original")
    msg, tid = gmail.build_reply(_svc(_raw(o)), "me@z.com", "m1", "follow up", reply_all=True)
    assert str(msg["To"]) == "bob@b.com" and str(msg["Cc"]) == "carol@c.com" and tid == "t1"
    msg, _ = gmail.build_reply(_svc(_raw(o)), "me@z.com", "m1", "follow up")
    assert str(msg["To"]) == "bob@b.com" and msg["Cc"] is None


def test_forward_collects_nested_attachments_and_preview_lists_them():
    inner = EmailMessage(); inner["Subject"] = "inner"; inner.set_content("inner body")
    inner.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="inner.pdf")
    o = EmailMessage(); o["From"], o["To"], o["Subject"] = "x@y.com", "me@z.com", "Signed"
    o.set_content("body")
    o.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="doc.pdf")
    o.add_attachment(inner, filename="fwd.eml")
    # wrap in multipart/signed so attachments are one level deeper than iter_attachments() sees
    signed = EmailMessage(); signed["From"], signed["To"], signed["Subject"] = "x@y.com", "me@z.com", "Signed"
    signed.make_mixed(); signed.attach(o)
    signed.add_attachment(b"sig", maintype="application", subtype="pkcs7-signature", filename="smime.p7s")
    fwd = gmail.build_forward(_svc(_raw(signed)), "me@z.com", "m1", ["d@q.com"])
    names = sorted(server._preview(fwd)["attachments"])
    assert names == ["doc.pdf", "fwd.eml", "smime.p7s"], names


def test_decode_part_honours_charset():
    data = base64.urlsafe_b64encode("caf\xe9".encode("latin-1")).decode()
    p = {"headers": [{"name": "Content-Type", "value": 'text/plain; charset="ISO-8859-1"'}], "body": {"data": data}}
    assert gmail._decode_part(p) == "café"


def test_remove_account_tool_is_gated(monkeypatch):
    reg(**{"a@x.com": "work"})
    called = []
    monkeypatch.setattr(accounts, "remove_account", lambda a: called.append(a) or a)
    first = run(server.remove_account("work"))
    assert first["status"] == "needs_confirmation" and not called
    assert run(server.remove_account("work", confirm_token=first["confirm_token"])) == {"removed": "a@x.com", "from_account": "a@x.com"}
    assert called == ["a@x.com"]


# ---- account groups ----
def greg():
    accounts.save_registry({
        "a@x.com": {"alias": "alpha", "group": "work", "added": "d"},
        "b@y.com": {"alias": None, "group": "work", "added": "d"},
        "c@z.com": {"alias": "gamma", "added": "d"},  # old-style entry without group
    })


def test_resolve_many():
    greg()
    assert accounts.resolve_many(None) == ["a@x.com", "b@y.com", "c@z.com"]
    assert accounts.resolve_many("WORK") == ["a@x.com", "b@y.com"]
    assert accounts.resolve_many("gamma") == ["c@z.com"]
    assert accounts.resolve_many("b@y.com") == ["b@y.com"]
    with pytest.raises(ValueError, match="Unknown account or group 'nope'"):
        accounts.resolve_many("nope")


def test_resolve_account_with_groups():
    greg()
    accounts.save_registry({**accounts.load_registry(), "c@z.com": {"alias": None, "group": "solo", "added": "d"}})
    assert accounts.resolve_account("solo") == "c@z.com"
    with pytest.raises(ValueError, match=r"group with 2 accounts.*a@x\.com.*b@y\.com"):
        accounts.resolve_account("work")


def test_validate_name():
    greg()
    r = accounts.load_registry()
    with pytest.raises(ValueError, match="already a group"):
        accounts._validate_name("work", r, "c@z.com")
    with pytest.raises(ValueError, match="already the alias"):
        accounts._validate_name("gamma", r, "a@x.com", "group")
    assert accounts._validate_name(" work ", r, "c@z.com", "group") == "work"
    assert accounts._validate_name("  ", r, None) is None
    with pytest.raises(ValueError, match="must not look like an email"):
        accounts._validate_name("a@b", r, None, "group")


def test_assign_group():
    greg()
    out = accounts.assign_group("gamma", "work")
    assert out == {"email": "c@z.com", "alias": "gamma", "group": "work"}
    assert len(accounts.resolve_many("work")) == 3
    assert accounts.assign_group("c@z.com", None)["group"] is None
    assert accounts.list_accounts()[2]["group"] is None


def test_search_emails_across_group(monkeypatch):
    greg()
    monkeypatch.setattr(accounts, "gmail_service", lambda e: e)

    def fake_search(svc, q, n):
        if svc == "b@y.com":
            raise ValueError("token gone")
        return [{"id": svc, "subject": q}]

    monkeypatch.setattr(gmail, "search", fake_search)
    out = server.search_emails("hi", account="work")
    assert out["searched_accounts"] == ["a@x.com", "b@y.com"]
    assert out["results"] == [{"id": "a@x.com", "subject": "hi", "account": "a@x.com"}]
    assert out["errors"] == {"b@y.com": "token gone"}
    monkeypatch.setattr(gmail, "search", lambda svc, q, n: [{"id": svc}])
    out = server.search_emails("hi")
    assert [r["account"] for r in out["results"]] == ["a@x.com", "b@y.com", "c@z.com"] and "errors" not in out


# ---- elicitation, preview, drafts-only ----
def _fake_ctx(supported=True, action="accept", confirm=True):
    async def elicit(message, schema):
        return types.SimpleNamespace(action=action, data=server._Confirm(confirm=confirm))
    session = types.SimpleNamespace(check_client_capability=lambda c: supported)
    return types.SimpleNamespace(session=session, elicit=elicit)


def _send_setup(monkeypatch):
    accounts.save_registry({"me@x.com": {"alias": None, "added": "d"}})
    svc = FakeService()
    monkeypatch.setattr(accounts, "gmail_service", lambda e: svc)
    return svc


def test_send_email_dialog_accept_sends_immediately(monkeypatch):
    svc = _send_setup(monkeypatch)
    out = run(server.send_email(to=["a@x.com"], subject="S", body="B", ctx=_fake_ctx()))
    assert out["confirmed_by"] == "user_dialog" and len(svc.sent) == 1 and not server._PENDING


def test_send_email_dialog_decline_cancels(monkeypatch):
    svc = _send_setup(monkeypatch)
    out = run(server.send_email(to=["a@x.com"], subject="S", body="B", ctx=_fake_ctx(action="decline")))
    assert out["status"] == "cancelled" and svc.sent == [] and not server._PENDING


def test_send_email_no_elicitation_falls_back_to_token(monkeypatch):
    svc = _send_setup(monkeypatch)
    out = run(server.send_email(to=["a@x.com"], subject="S", body="B", ctx=_fake_ctx(supported=False)))
    assert out["status"] == "needs_confirmation" and svc.sent == []


def test_preview_all_recipients_bare_lowercase():
    msg = gmail.build_message("me@x.com", ["Accountant <Evil@X.com>"], "S", "B", bcc=["Z@y.com"])
    assert server._preview(msg)["all_recipients"] == ["evil@x.com", "z@y.com"]


def test_drafts_only_hides_send_tools(monkeypatch):
    monkeypatch.setenv("GMAIL_MCP_DRAFTS_ONLY", "1")
    try:
        importlib.reload(server)
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert "send_email" not in names and "create_draft" in names
    finally:
        monkeypatch.delenv("GMAIL_MCP_DRAFTS_ONLY")
        importlib.reload(server)
