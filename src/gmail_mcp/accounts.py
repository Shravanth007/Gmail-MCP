"""OAuth, secure token storage (OS keyring) and the account registry (no secrets)."""
import contextlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import keyring
import keyring.errors
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build

CONFIG_DIR = Path(os.environ.get("GMAIL_MCP_HOME", Path.home() / ".gmail-mcp"))
CREDENTIALS_FILE = Path(os.environ.get("GMAIL_MCP_CREDENTIALS", CONFIG_DIR / "credentials.json"))
REGISTRY_FILE = CONFIG_DIR / "accounts.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
KEYRING_SERVICE = "gmail-mcp"


def load_registry() -> dict:
    if not REGISTRY_FILE.exists():
        return {}
    return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))


def save_registry(reg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    try:
        os.chmod(REGISTRY_FILE, 0o600)
    except OSError:
        pass  # not meaningful on Windows


def _describe(reg: dict) -> str:
    out = []
    for e, v in reg.items():
        s = e
        if v.get("alias"):
            s += f" ({v['alias']})"
        if v.get("group"):
            s += f" [{v['group']}]"
        out.append(s)
    return ", ".join(out)


def _validate_name(name: str | None, reg: dict, email: str | None, kind: str = "alias") -> str | None:
    """Clean an alias/group. Aliases and groups share one namespace; groups may be shared by accounts."""
    name = name.strip() if name and name.strip() else None
    if not name:
        return None
    if "@" in name:
        raise ValueError(f"The {kind} must not look like an email address")
    low = name.lower()
    for other, v in reg.items():
        if kind == "alias" and other != email and (v.get("alias") or "").lower() == low:
            raise ValueError(f"Alias '{name}' is already used by {other}")
        if kind == "alias" and (v.get("group") or "").lower() == low:
            raise ValueError(f"'{name}' is already a group name; aliases and groups share one namespace")
        if kind == "group" and (v.get("alias") or "").lower() == low:
            raise ValueError(f"'{name}' is already the alias of {other}; aliases and groups share one namespace")
    return name


def _group_members(reg: dict, key: str) -> list[str]:
    return [e for e, v in reg.items() if v.get("group") and v["group"].lower() == key]


def _match_one(reg: dict, key: str) -> str | None:
    for email in reg:  # exact email first, so an alias can never shadow another account's address
        if key == email.lower():
            return email
    for email, v in reg.items():
        if v.get("alias") and key == v["alias"].lower():
            return email
    return None


def resolve_many(account: str | None) -> list[str]:
    reg = load_registry()
    if not reg:
        raise ValueError("No Gmail accounts connected. Run: gmail-mcp add-account")
    if account is None:
        return list(reg)
    key = account.strip().lower()
    email = _match_one(reg, key)
    if email:
        return [email]
    members = _group_members(reg, key)
    if members:
        return members
    raise ValueError(f"Unknown account or group '{account}'. Connected accounts: {_describe(reg)}")


def resolve_account(account: str | None) -> str:
    reg = load_registry()
    if not reg:
        raise ValueError("No Gmail accounts connected. Run: gmail-mcp add-account")
    if account is None:
        if len(reg) == 1:
            return next(iter(reg))
        raise ValueError(f"Specify `account`. Connected accounts: {_describe(reg)}")
    key = account.strip().lower()
    email = _match_one(reg, key)
    if email:
        return email
    members = _group_members(reg, key)
    if len(members) == 1:
        return members[0]
    if members:
        raise ValueError(f"'{account}' is a group with {len(members)} accounts ({', '.join(members)}). "
                         "This action needs one account: pass its email or alias.")
    raise ValueError(f"Unknown account '{account}'. Connected accounts: {_describe(reg)}")


def assign_group(account: str, group: str | None) -> dict:
    reg = load_registry()
    if not reg:
        raise ValueError("No Gmail accounts connected. Run: gmail-mcp add-account")
    email = _match_one(reg, account.strip().lower())
    if not email:
        raise ValueError(f"Unknown account '{account}'. Connected accounts: {_describe(reg)}")
    group = _validate_name(group, reg, email, "group")
    reg[email]["group"] = group
    save_registry(reg)
    return {"email": email, "alias": reg[email].get("alias"), "group": group}


def add_account(alias: str | None = None, group: str | None = None) -> str:
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"OAuth client file not found at {CREDENTIALS_FILE}. In Google Cloud Console create an "
            "OAuth client of type 'Desktop app', download its JSON and save it there "
            "(or set GMAIL_MCP_CREDENTIALS)."
        )
    _validate_name(alias, {}, None)  # fail fast on obviously bad names, before the browser opens
    _validate_name(group, {}, None, "group")
    reg = load_registry()
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    # stdout is the MCP transport when this runs as a tool: send the auth URL print to stderr.
    # prompt=consent guarantees a refresh_token even if this Google account consented before.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=180, prompt="consent")
        except WSGITimeoutError:
            raise ValueError("Timed out waiting for the browser sign-in (3 minutes). Try again.") from None
    if not creds.refresh_token:
        raise ValueError("Google did not return a refresh token; nothing was saved. Try again.")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    # Identity comes from Google, never from user input.
    email = service.users().getProfile(userId="me").execute()["emailAddress"]
    alias = _validate_name(alias, reg, email) or reg.get(email, {}).get("alias")  # re-linking keeps existing
    group = _validate_name(group, reg, email, "group") or reg.get(email, {}).get("group")
    # Windows Credential Manager limits blobs to ~2560 bytes; creds.to_json() is well under that.
    keyring.set_password(KEYRING_SERVICE, email, creds.to_json())
    reg[email] = {"alias": alias, "group": group, "added": date.today().isoformat()}
    save_registry(reg)
    return email


def remove_account(account: str) -> str:
    email = resolve_account(account)
    data = keyring.get_password(KEYRING_SERVICE, email)
    if data:
        try:
            info = json.loads(data)
            token = info.get("refresh_token") or info.get("token")
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/revoke",
                data=urllib.parse.urlencode({"token": token}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # network/HTTP errors must not block local removal
            print(f"Could not revoke token for {email}: {type(e).__name__}", file=sys.stderr)
    try:
        keyring.delete_password(KEYRING_SERVICE, email)
    except keyring.errors.PasswordDeleteError:
        pass
    reg = load_registry()
    reg.pop(email, None)
    save_registry(reg)
    return email


def get_credentials(email: str) -> Credentials:
    data = keyring.get_password(KEYRING_SERVICE, email)
    if not data:
        raise ValueError(f"Token for {email} not found; re-run gmail-mcp add-account")
    creds = Credentials.from_authorized_user_info(json.loads(data), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            raise ValueError(f"Token for {email} was revoked or expired. Re-run: gmail-mcp add-account")
        keyring.set_password(KEYRING_SERVICE, email, creds.to_json())
    return creds


def gmail_service(email: str):
    return build("gmail", "v1", credentials=get_credentials(email), cache_discovery=False)


def list_accounts() -> list[dict]:
    return [{"email": e, "alias": v.get("alias"), "group": v.get("group")} for e, v in load_registry().items()]
