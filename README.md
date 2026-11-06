# gmail-mcp

Multi-account Gmail MCP server (stdio). Read, search, draft, reply, forward and send, with a human confirmation before anything is sent.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Each user brings their own Google OAuth client; nothing secret ships in this repo.

1. **Google Cloud**: create a project, enable the Gmail API, and create an OAuth client of type "Desktop app". Download the JSON to `~/.gmail-mcp/credentials.json` (or set `GMAIL_MCP_CREDENTIALS`). If the consent screen is in Testing mode, add each Gmail address as a test user. Note: Google expires Testing-mode tokens after 7 days; the server tells the agent to re-link when that happens, or publish the app to production to stop it.
2. **Install**:
   ```
   git clone https://github.com/Shravanth007/Gmail-MCP.git
   cd Gmail-MCP
   uv sync
   ```
3. **Link accounts** (repeat per account): `uv run gmail-mcp add-account --alias work`
   Add a shared group too: `uv run gmail-mcp add-account --alias work1 --group work`.
   Change it any time: `gmail-mcp assign EMAIL_OR_ALIAS GROUP` (use `-` as GROUP to clear).
   Also: `gmail-mcp list-accounts` (email, alias, group), `gmail-mcp remove-account EMAIL`.

   An **alias** is a unique nickname for one account. A **group** is a label several accounts can share (e.g. `work`, `personal`). Aliases and groups share one namespace, so a name can't be both.
4. **Connect a client**

   Claude Desktop (`claude_desktop_config.json`):
   ```json
   {
     "mcpServers": {
       "gmail": {
         "command": "uv",
         "args": ["--directory", "C:\\path\\to\\Gmail-MCP", "run", "gmail-mcp"]
       }
     }
   }
   ```
   Claude Code: `claude mcp add gmail -- uv --directory "C:\path\to\Gmail-MCP" run gmail-mcp`

## Security

- OAuth tokens live in the OS keychain (Windows Credential Manager / macOS Keychain / Secret Service), encrypted by the OS credential store (DPAPI on Windows), never in plain files. `accounts.json` holds only emails and aliases.
- Scopes are least privilege: `gmail.readonly` and `gmail.compose` (read, draft, send; no delete or modify).
- The account email comes from Google's `getProfile`, never from user input.
- Replying to your own message addresses the original recipients, as Gmail does. Forwards carry every named attachment, including ones nested in signed mail.
- Sends and account removal confirm through a client dialog (MCP elicitation, supported by Claude Code 2.1.76+). The dialog shows from/to/cc/bcc, bare recipient addresses (so display names can't hide the destination), subject, attachments and body. On clients without elicitation the server falls back to a preview plus a single-use `confirm_token` (5 min TTL, bound to account and action); changed arguments are rejected, and for `send_draft` the token is bound to the draft's message id, so a draft edited after the preview cannot be sent with the old token.
- `GMAIL_MCP_DRAFTS_ONLY=1` removes the four send tools entirely (drafts can still be created).
- Linking uses `prompt=consent` so a refresh token is always issued; nothing is stored if Google omits it.
- `remove-account` revokes the token with Google and deletes it from the keychain.
- Logs go to stderr and never include tokens or message bodies.

### Threat model

- Protects against: the AI sending mail without a human click, and cross-account mix-ups.
- Does not protect against malware running as your user (Credential Manager is per-user, not per-app).
- Does not protect against a manipulated AI leaking mail through OTHER tools in the same session.
- Does not protect against stolen refresh tokens until revoked (`gmail-mcp remove-account` or myaccount.google.com/permissions).

Recommended: never "always allow" the send tools, use the server in a session without web/browser tools when handling sensitive mail, and keep disk encryption and 2-step verification on.

## Tools

- `list_accounts`: connected accounts (email, alias, group) and a `groups` map.
- `remove_account`: unlink an account (confirmed through a dialog, or `confirm_token` fallback). Linking is CLI-only (`gmail-mcp add-account`).
- `assign_account`: set or clear an account's group.
- `search_emails`: Gmail search syntax. `account` may be an email, alias or group (or omitted for all accounts); every result row carries the `account` it came from, and per-account failures are reported under `errors`. All other tools, including send/reply/forward, need exactly one account (email or alias).
- `get_thread`: read a thread.
- `list_drafts`: list drafts.
- `create_draft` / `create_reply_draft`: save a draft; nothing is sent.
- `send_email`, `reply_to_email`, `forward_email`, `send_draft`: confirmed send (dialog, or `confirm_token` fallback). Not registered when `GMAIL_MCP_DRAFTS_ONLY=1`.
