# Job application email tracker

Checks your Gmail every few minutes and uses Claude to spot emails about your job applications. It sorts them into Gmail labels and shows them on a local dashboard at http://localhost:8765.

| Category | Gmail label |
|---|---|
| Interview invite / assessment / offer | `Jobs/Interview` (+ macOS notification) |
| Rejection | `Jobs/Declined` |
| Position closed, filled, on hold, withdrawn | `Jobs/Not Relevant` |
| Application received / still reviewing | `Jobs/Under Review` |

Each email also gets a company label (`Jobs/Companies/<name>`). The dashboard's calendar shows the interviews found in your emails, your flights (picked out of airline booking emails from the last year), and the upcoming events from your Google Calendar. A pop-up every morning lists the last 24 hours of job emails. Job alerts, newsletters and unrelated mail are left alone.

> **Requirements:** macOS (it uses macOS notifications, pop-ups and Bonjour), Python 3.10+, a Gmail account, and an Anthropic API key. Everything runs on your own Mac. Your emails go only to Gmail and to the Anthropic API.

---

## 1. Get the code

```sh
git clone <this-repo-url> CVintervieweTracker
cd CVintervieweTracker
```

## 2. Install the dependencies

With [uv](https://docs.astral.sh/uv/) (recommended):

```sh
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
```

Or with plain Python:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Add your Anthropic API key

1. Create a key at https://platform.claude.com (*API Keys*). The account needs billing set up.
2. Copy the example settings file and paste in your key:

   ```sh
   cp .env.example .env
   ```

   ```
   ANTHROPIC_API_KEY=sk-ant-your-key-here
   ```

`.env` is git-ignored, so your key never gets committed. A real `ANTHROPIC_API_KEY` environment variable takes priority over the file.

**Cost:** Claude reads each email only once (the results are saved in `jobmail.db`). Before that, a Gmail search drops most emails that aren't about jobs. The first run reads the last 30 days, so it costs the most. After that you pay only for new emails.

## 4. Set up Gmail and Google Calendar access (about 10 minutes, one time)

Each person needs **their own** Google Cloud project and `credentials.json`. Don't share yours.

### 4.1 Create a project and turn on the Gmail and Calendar APIs

1. Go to https://console.cloud.google.com and sign in with the Gmail account you want to track.
2. Click the project picker at the top → **New project** → give it any name (e.g. `job-tracker`) → **Create**. Make sure the new project is selected.
3. Open **APIs & Services → Library**, search for **Gmail API**, and click **Enable**.
4. Go back to the Library, search for **Google Calendar API**, and click **Enable** too.

### 4.2 Set up the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**. In the newer console this is called **Google Auth Platform**. Click **Get started**.
2. **App information:** any app name (e.g. `Job tracker`), and your email as the support email.
3. **Audience:** choose **External**.
4. **Contact information:** your email. Accept the policy and click **Create**.
5. Go to **Audience** (older console: *Test users*), click **Add users**, and add **your own Gmail address**. If you skip this step, Google blocks the sign-in with *"Access blocked: app has not completed the Google verification process"*.

You don't need to publish or verify the app. It stays in **Testing** mode, which is fine for personal use.

### 4.3 Create the OAuth client and download `credentials.json`

1. Go to **Credentials** (or **Clients** in Google Auth Platform) → **Create credentials → OAuth client ID**.
2. Application type: **Desktop app**. Use any name, then click **Create**.
3. Click **Download JSON**.
4. Rename the file to `credentials.json` and put it in the project folder, next to `jobmail.py`.

### 4.4 First sign-in

On the first run (next step), a browser window opens and asks you to sign in to Google:

- You will see **"Google hasn't verified this app"**. That's expected, because it's your own app. Click **Continue** (or **Advanced → Go to Job tracker (unsafe)**).
- Allow both permissions:
  - **Read, compose and send emails / manage labels** (`gmail.modify`). The app uses this only to read emails and add labels. It never sends or deletes anything.
  - **See your calendars** (`calendar.readonly`). This is read-only, so the app can't change your calendar.

The sign-in is saved to `token.json`, so you only do it once.

> **Signed out after 7 days?** Google expires the sign-in for apps in *Testing* mode after about a week. When this happens, the log shows an `invalid_grant` error. Delete `token.json` and run the app again to sign in.

## 5. Run it

```sh
.venv/bin/python jobmail.py                # dashboard + check Gmail every 15 min
.venv/bin/python jobmail.py --interval 5   # check every 5 min
.venv/bin/python jobmail.py --days 90      # look further back on the first run (default 30)
.venv/bin/python jobmail.py --once         # sort once and exit, no dashboard
.venv/bin/python jobmail.py --no-browser   # don't open the dashboard on start
.venv/bin/python jobmail.py --alert-now    # show the daily summary pop-up right now
```

The dashboard opens at http://localhost:8765. You can also run `./open_dashboard.sh`, which starts the app in the background if it isn't already running and then opens the dashboard.

## Settings (`.env`)

| Setting | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. |
| `DAILY_ALERT_TIME` | `09:00` | Time of the daily "last 24 hours" pop-up. Leave empty to turn it off. |
| `DASHBOARD_PORT` | `8765` | Dashboard port. |
| `DASHBOARD_HOST` | `127.0.0.1` | Set to `0.0.0.0` so other devices on your Wi-Fi can open the dashboard. |
| `DASHBOARD_NAME` | — | Publishes the dashboard as `http://<name>.local` on your network. |
| `DASHBOARD_PASSWORD` | — | Password that other devices must enter (any username works; your own Mac never asks). |

### Open it from your phone or other computers

```
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=80
DASHBOARD_NAME=jobs
DASHBOARD_PASSWORD=pick-something
```

Other devices on the same Wi-Fi can then open **http://jobs.local**. If `.local` names don't work on a device, use the Mac's IP address instead (it's printed when the app starts).

## Start automatically at login (optional)

1. Run the app once by hand so that `token.json` exists.
2. Open `com.zmaori.jobmail.plist` and **replace every `/Users/zmaori/Projects/CVintervieweTracker` with the path to your own copy** (run `pwd` in the project folder to get it). You can also rename the file and its `Label`, for example to `com.yourname.jobmail`.
3. Load it:

   ```sh
   cp com.zmaori.jobmail.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.zmaori.jobmail.plist
   ```

From then on the app runs in the background. Logs go to `jobmail.log`. To stop it: `launchctl unload ~/Library/LaunchAgents/com.zmaori.jobmail.plist`.

Tip: in Safari, open http://localhost:8765 and choose *File → Add to Dock* to get a dashboard app icon.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Missing credentials.json` | Do step 4.3 and put the file next to `jobmail.py`. |
| `Set ANTHROPIC_API_KEY in .env` | Do step 3. |
| "Access blocked … has not completed the Google verification process" | Add your Gmail address as a test user (step 4.2, part 5). |
| `invalid_grant` / `Token has been expired or revoked` | Delete `token.json` and run again. |
| `Gmail API has not been used in project … or it is disabled` | Enable the Gmail API (step 4.1) in the **same** project as your OAuth client. |
| Calendar tab says "Google Calendar not synced" | Enable the **Google Calendar API** (step 4.1, part 4). The calendar syncs again on the next check, or click **Check now**. Email sorting keeps working while the calendar is off. |
| Want to re-sort everything from scratch | Stop the app and delete `jobmail.db`. Gmail labels that were already added stay. |
| Port already in use | The app is already running (maybe via launchd). Open the dashboard instead, or set a different `DASHBOARD_PORT`. |

## Files

| File | In git? | What it is |
|---|---|---|
| `jobmail.py` | yes | The app: Gmail polling, Claude sorting, dashboard server. |
| `dashboard.html` | yes | The dashboard page. |
| `.env.example` | yes | Template for `.env`. |
| `open_dashboard.sh` | yes | Starts the app if needed and opens the dashboard. |
| `com.zmaori.jobmail.plist` | yes | launchd file for starting at login. |
| `.env` | **no** | Your API key and settings. |
| `credentials.json` | **no** | Your Google OAuth client. |
| `token.json` | **no** | Your Google sign-in (Gmail + Calendar). |
| `jobmail.db`, `jobmail.log`, `.daily_alert` | **no** | Your local data. |

**Never commit `.env`, `credentials.json` or `token.json`.** They give access to your Anthropic account and your Gmail. They're already in `.gitignore`.
