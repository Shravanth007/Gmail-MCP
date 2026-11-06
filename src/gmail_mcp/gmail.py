"""Gmail API helpers. Each function takes an authenticated `service`."""
import base64
import email
import email.policy
import html
import re
from email.message import EmailMessage
from email.utils import getaddresses


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _strip_html(s: str) -> str:
    s = re.sub(r"<(style|script|head)\b.*?</\1\s*>", "", s, flags=re.S | re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def _headers(payload: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _decode_part(p: dict) -> str:
    m = re.search(r'charset="?([\w.:-]+)', _headers(p).get("content-type", ""), re.I)
    try:
        return _b64d(p["body"]["data"]).decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:  # unknown charset name
        return _b64d(p["body"]["data"]).decode("utf-8", errors="replace")


def _extract_text(payload: dict) -> str:
    def walk(p, mime):
        if p.get("mimeType") == mime and p.get("body", {}).get("data") and not p.get("filename"):
            return _decode_part(p)
        for sub in p.get("parts", []) or []:
            found = walk(sub, mime)
            if found is not None:
                return found
        return None

    text = walk(payload, "text/plain")
    if text is not None:
        return text
    h = walk(payload, "text/html")
    return _strip_html(h) if h is not None else ""


def search(service, query: str, max_results: int = 10) -> list[dict]:
    res = service.users().messages().list(userId="me", q=query, maxResults=max(1, min(max_results, 50))).execute()
    out = []
    for m in res.get("messages", []):
        full = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        h = _headers(full.get("payload", {}))
        out.append({
            "id": full["id"], "threadId": full["threadId"], "from": h.get("from", ""),
            "to": h.get("to", ""), "subject": h.get("subject", ""), "date": h.get("date", ""),
            "snippet": full.get("snippet", ""),
        })
    return out


def _attachments(payload: dict) -> list[str]:
    names = [payload["filename"]] if payload.get("filename") else []
    for sub in payload.get("parts", []) or []:
        names += _attachments(sub)
    return names


def get_thread(service, thread_id: str) -> dict:
    t = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    msgs = []
    for m in t.get("messages", []):
        p = m.get("payload", {})
        h = _headers(p)
        msgs.append({
            "id": m["id"], "from": h.get("from", ""), "to": h.get("to", ""), "cc": h.get("cc", ""),
            "date": h.get("date", ""), "subject": h.get("subject", ""),
            "body": _extract_text(p), "attachments": _attachments(p),
        })
    return {"id": t["id"], "messages": msgs}


def get_message_raw(service, message_id: str) -> tuple[EmailMessage, str]:
    m = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    msg = email.message_from_bytes(_b64d(m["raw"]), policy=email.policy.default)
    return msg, m.get("threadId")


def build_message(sender, to, subject, body, cc=None, bcc=None, in_reply_to=None, references=None,
                  attachments: list[tuple[str, bytes, str]] | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    for filename, data, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    return msg


def encode(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _plain(msg: EmailMessage) -> str:
    part = msg.get_body(("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    return _strip_html(text) if part.get_content_subtype() == "html" else text


def build_reply(service, sender, message_id, body, reply_all=False) -> tuple[EmailMessage, str]:
    orig, thread_id = get_message_raw(service, message_id)
    own = any(a.lower() == sender.lower() for _, a in getaddresses([str(orig["From"] or "")]))
    if own:  # replying to our own message: address the original recipients, like Gmail does
        to = [str(orig["To"] or "")]
        cc = [str(orig["Cc"])] if reply_all and orig["Cc"] else None
    else:
        to = [str(orig["Reply-To"] or orig["From"])]
        cc = None
        if reply_all:
            skip = {sender.lower(), *(a.lower() for _, a in getaddresses(to))}
            cc = [f"{n} <{a}>" if n else a
                  for n, a in getaddresses([str(orig["To"] or ""), str(orig["Cc"] or "")])
                  if a and a.lower() not in skip]
    subj = str(orig["Subject"] or "")
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    mid = str(orig["Message-ID"] or "")
    refs = (str(orig["References"] or "") + " " + mid).strip()
    quoted = "\n".join("> " + line for line in _plain(orig).splitlines())
    text = f"{body}\n\nOn {orig['Date']}, {orig['From']} wrote:\n{quoted}"
    return build_message(sender, to, subj, text, cc=cc or None, in_reply_to=mid or None,
                         references=refs or None), thread_id


def build_forward(service, sender, message_id, to, body="") -> EmailMessage:
    orig, _ = get_message_raw(service, message_id)
    subj = str(orig["Subject"] or "")
    if not subj.lower().startswith(("fwd:", "fw:")):
        subj = "Fwd: " + subj
    text = (f"{body}\n\n---------- Forwarded message ----------\nFrom: {orig['From']}\n"
            f"Date: {orig['Date']}\nSubject: {orig['Subject']}\nTo: {orig['To']}\n\n{_plain(orig)}")
    msg = build_message(sender, to, subj, text)
    for part in _leaf_attachments(orig):
        name = part.get_filename() or "attachment"
        if part.get_content_type() == "message/rfc822":
            msg.add_attachment(part.get_content(), filename=name)  # attaches the inner message as-is
        else:
            msg.add_attachment(part.get_payload(decode=True) or b"", maintype=part.get_content_maintype(),
                               subtype=part.get_content_subtype(), filename=name)
    return msg


def _leaf_attachments(orig: EmailMessage) -> list[EmailMessage]:
    """Named leaf parts at any nesting depth (multipart/signed, multipart/related...).
    An attached message/rfc822 counts as one attachment; its own parts are not re-listed."""
    out, inside = [], set()
    for part in orig.walk():
        if id(part) in inside or not part.get_filename():
            continue
        if part.get_content_type() == "message/rfc822":
            inside.update(id(p) for p in part.get_content().walk())
        elif part.is_multipart():
            continue
        out.append(part)
    return out


def _draft_body(msg, thread_id):
    return {"raw": encode(msg), **({"threadId": thread_id} if thread_id else {})}


def create_draft(service, msg, thread_id=None) -> dict:
    d = service.users().drafts().create(userId="me", body={"message": _draft_body(msg, thread_id)}).execute()
    return {"draft_id": d["id"], "message_id": d["message"]["id"]}


def send(service, msg, thread_id=None) -> dict:
    r = service.users().messages().send(userId="me", body=_draft_body(msg, thread_id)).execute()
    return {"message_id": r["id"], "thread_id": r.get("threadId")}


def send_draft(service, draft_id) -> dict:
    r = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {"message_id": r["id"], "thread_id": r.get("threadId")}


def list_drafts(service, max_results=10) -> list[dict]:
    res = service.users().drafts().list(userId="me", maxResults=max(1, min(max_results, 50))).execute()
    out = []
    for d in res.get("drafts", []):
        full = service.users().drafts().get(userId="me", id=d["id"], format="metadata").execute()
        h = _headers(full.get("message", {}).get("payload", {}))
        out.append({"draft_id": d["id"], "message_id": full["message"]["id"],
                    "subject": h.get("subject", ""), "to": h.get("to", "")})
    return out
