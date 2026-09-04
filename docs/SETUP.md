# Setup Guide

Getting `gws` authenticated against Gmail takes about 20 minutes, and roughly
half the steps cannot be automated. Several of them fail in confusing ways —
this guide calls out each trap where you'd hit it.

---

## 1. Install the prerequisites

| Requirement | Purpose | Verify |
|---|---|---|
| Node.js 18+ | runtime for `gws` | `node --version` |
| [`gws`](https://github.com/googleworkspace/google-workspace-cli) | Google Workspace CLI | `gws --version` |
| Python 3.8+ | the audit pipeline (stdlib only) | `python --version` |
| Google Cloud SDK (`gcloud`) | required by `gws auth setup` | `gcloud --version` |
| A Google Cloud project | hosts your OAuth client | `gcloud config get-value project` |

Python needs **no packages** — the tool is standard library only. There is no
`pip install` step and no virtualenv to create.

### Windows

```powershell
winget install OpenJS.NodeJS
winget install Python.Python.3.12
winget install Google.CloudSDK
```

Then **open a new terminal** (see the PATH note below) and install `gws`:

```powershell
npm install -g @googleworkspace/cli
```

### macOS

```bash
brew install node python
brew install --cask google-cloud-sdk
npm install -g @googleworkspace/cli
```

### Linux

Install Node.js 18+ and Python 3.8+ from your distribution's package manager,
then follow Google's instructions for the Cloud SDK at
<https://cloud.google.com/sdk/docs/install>, and:

```bash
npm install -g @googleworkspace/cli
```

### Get the tool itself

```bash
git clone https://github.com/Overphyl/gmail-inbox-audit.git
cd gmail-inbox-audit
python tests/test_audit.py      # optional: 11 offline tests, no API access
```

### Create a Google Cloud project

Skip this if you already have one you want to use. The Gmail API is free at
this volume and **no billing account is required**.

```bash
gcloud projects create my-gmail-audit --name="Gmail Audit"
gcloud config set project my-gmail-audit
```

Project IDs are globally unique, so `my-gmail-audit` may be taken — add a
suffix if creation fails. You can also create one in the Console at
<https://console.cloud.google.com/projectcreate>.

> **Windows: stale PATH.** Installers modify `PATH`, but already-open shells
> keep the old copy — winget says so explicitly ("restart your shell to use the
> new value"). A tool can be correctly installed and still appear missing.
> Restart your shell, or check the install directory directly:
> ```
> winget    %LOCALAPPDATA%\Microsoft\WinGet\Packages\<pkg>\
> gcloud    C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\
> npm       %APPDATA%\npm\node_modules\
> ```

---

## 2. Authenticate gcloud

```bash
gcloud auth login
gcloud config set project <PROJECT_ID>
```

## 3. Enable the Gmail API

```bash
gws auth setup --project <PROJECT_ID> --dry-run   # preview
gws auth setup --project <PROJECT_ID>
```

Two things to expect:

- It enables **22 APIs**. Only Gmail is needed here, and there's no flag to
  narrow the list.
- **It cannot create the OAuth client.** It stops partway and prints Console
  instructions. This is normal, not a failure — continue to the next step.

---

## 4. Configure the consent screen

`https://console.cloud.google.com/apis/credentials/consent?project=<PROJECT_ID>`

- User Type: **External**
- Fill in **only**: App name, User support email, Developer contact email
- **Add your own Google address as a Test user** — omitting this causes
  `access_denied` at login
- **Leave App home page / Privacy policy / Terms of service BLANK**

> ### Trap: the authorized domain
> Filling in *any* of those three URL fields makes Google demand an "authorized
> domain" — a top-level domain you own and have verified in Search Console.
> `localhost` and IP addresses are rejected, so there is no way to satisfy it
> without owning a domain.
>
> Leave the fields blank and the requirement disappears. The desktop flow
> redirects to `127.0.0.1`, so no domain is involved anywhere in it.

---

## 5. Declare the scope

Google Auth Platform → **Data Access** → Add or remove scopes → add:

```
https://www.googleapis.com/auth/gmail.modify
```

> ### Trap: silently dropped scopes
> Restricted scopes not declared here are **dropped at consent without error**.
> The result is a token that looks correct but returns 403 on every Gmail call.

---

## 6. Create the OAuth client — **Desktop app**

`https://console.cloud.google.com/apis/credentials?project=<PROJECT_ID>`

Create Credentials → OAuth client ID → Application type: **Desktop app**.

### Why Desktop app, and not Web / Chrome extension / Android

`gws` implements exactly one OAuth flow. From its binary:

```
yup-oauth2\src\installed.rs        the "installed app" flow
struct InstalledConfig             parses the `installed` JSON key
127.0.0.1:0                        loopback, OS-assigned random port
client_id client_secret redirect_uri grant_type authorization_code
code_challenge -> 0 occurrences    no PKCE
```

1. It parses the `installed` key. A Web client's JSON uses `web` and fails.
2. It binds port `0` — a different port each run. Only installed-app clients
   get Google's loopback exemption from exact redirect-URI registration; a Web
   client requires the exact port registered in advance.
3. It sends a `client_secret` and has no PKCE, so it cannot complete a *public*
   client flow — and Chrome-extension and Android clients are issued no secret.
4. Chrome-extension clients are pinned to an extension ID, Android clients to a
   package name plus SHA-1 signing fingerprint. A CLI can present neither.

> **The desktop client secret is not confidential**, by Google's own design —
> installed-app secrets ship to user machines and are treated as public. It
> grants nothing on its own; authority lives in the refresh token created after
> browser consent. A `client_secret.json` on disk is the intended arrangement.

---

## 7. Install the credentials

Download the JSON from the creation dialog and save it to:

```
~/.config/gws/client_secret.json                    POSIX
C:\Users\<you>\.config\gws\client_secret.json       Windows
```

Prefer the file over the `GOOGLE_WORKSPACE_CLI_CLIENT_ID` / `_SECRET`
environment variables, so the secret never lands in shell history.

---

## 8. Log in

```bash
gws auth login --scopes https://www.googleapis.com/auth/gmail.modify,openid,https://www.googleapis.com/auth/userinfo.email
```

Use `--scopes` (explicit) rather than `-s gmail` (an interactive picker).

**Tick the Gmail permission checkbox during consent.** Google lets you consent
while leaving optional scopes unchecked, which produces a token that fails
every call.

If no consent screen appears at all, Google replayed a cached grant. Revoke the
app at `https://myaccount.google.com/permissions` and log in again.

---

## 9. Verify — against the API, not `auth status`

> ### Trap: `gws auth status` reports requested scopes, not granted ones
> It will happily list `gmail.modify` on a token that has no Gmail access
> whatsoever. It is not a reliable check.

The only trustworthy verification is a real call:

```bash
gws gmail users getProfile --params '{"userId":"me"}'
```

```powershell
# PowerShell needs escaped quotes - see Platform notes below
gws gmail users getProfile --params '{\"userId\":\"me\"}'
```

Success returns your address and message totals. A 403
`insufficientPermissions` means the grant is wrong — revisit steps 5 and 8.

---

## Scope choice

| Method | Required scope |
|---|---|
| `messages.trash` | `gmail.modify` **or** `https://mail.google.com/` |
| `messages.delete` (permanent) | `https://mail.google.com/` **only** |
| `messages.batchDelete` | `https://mail.google.com/` **only** |

**Use `gmail.modify`. Never `https://mail.google.com/`.** With `gmail.modify`,
trash works and permanent deletion is refused by Google itself — a guarantee
enforced by the API rather than by the tool's good behaviour. Everything lands
in Trash with 30-day recovery.

---

## Platform notes

### PowerShell mangles JSON arguments

PowerShell 5.1 strips inner double quotes when passing arguments to a native
`.exe`, so `'{"userId":"me"}'` arrives as `{userId:me}` and fails with
*"key must be a string at line 1 column 2"*. Escape them:

```powershell
gws gmail users getProfile --params '{\"userId\":\"me\"}'
```

Bash passes single-quoted strings through intact, so the identical command
works there unescaped — which makes this look like an auth problem when it
isn't.

### `gws` on Windows is a `.cmd` shim

Python's `subprocess` cannot execute it by bare name (`WinError 2`). The tool
resolves the real executable automatically; override with `GWS_BIN` if needed:

```
%APPDATA%\npm\node_modules\@googleworkspace\cli\bin\gws.exe
```

### Force UTF-8 on subprocess output

Header values routinely contain non-ASCII. Windows' cp1252 default raises
`UnicodeDecodeError` mid-fetch. The tool sets `encoding="utf-8",
errors="replace"`.

---

## Rate limits

Gmail enforces quota **per minute**, not per second; `messages.get` costs 5
units. Measured against a ~35,000-message mailbox:

| Concurrency | Throughput | Result |
|---|---|---|
| 8 | 21.7 msg/s | clean |
| 16 | 35.7 msg/s | clean |
| 24 | 34.9 msg/s | clean |
| 32 | 44.6 msg/s | **27 of 120 dropped** |

Stay at **12–16**. Above that the API drops messages, which undercounts senders
and corrupts the ranking — a correctness problem, not a performance one. The
tool retries with exponential backoff, but lowering concurrency is the real fix.

Note that quota is consumed by *any* recent activity, so benchmarking
immediately before a real run leaves you throttled at the start.

Rough timings for ~35,000 messages: listing all IDs ~35 s; full header fetch at
concurrency 12 about 20–25 minutes.

---

## Known limitation: 7-day token expiry

An **External** app in *Testing* status issues refresh tokens that expire after
7 days. Gmail scopes are restricted, so publishing the app to escape this
triggers Google's verification review — rarely worth it for personal use.

Re-run `gws auth login` when calls start failing with an auth error. Budget for
this if your cleanup spans more than a week.

---

## A note on Model Armor

Google's Model Armor is sometimes suggested as a safety layer for tools like
this. It does not fit here:

- It is a **content filter**, not an authorization control. It cannot tell that
  a call is a deletion and cannot block one.
- This tool reads with `format=metadata`, so no message body ever reaches a
  model. The injection surface Model Armor would guard is already closed.
- In `block` mode it is actively risky mid-cleanup: spam headers are exactly
  what trips safety classifiers, so a batch can fail partway and leave
  half-applied state.
- It requires `modelarmor.googleapis.com` (not among the 22 APIs enabled
  above), a template, and broad `cloud-platform` scope on your account.

The `gmail.modify` scope is what actually prevents irreversible deletion.
