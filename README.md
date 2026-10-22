# Comp Practice Sign-Up Bot

Watches the **26-27 Tennis Comp Team** GroupMe chat and submits the Wednesday practice
Google Form with your name the instant the link is posted — typically **under one second**
after the message lands, versus the ~10–30 s it takes a person to see the notification,
open the link, type, and tap Submit.

## How it works

```
GroupMe message ──► push listener (real-time)  ─┐
                    poll listener (every 1.5 s) ─┴─► form link found?
                                                       │
           title contains "Comp Practice"? ◄───────────┘
                       │ yes
           GET viewform → read field IDs (entry.XXXX) from the page
                       │
           POST formResponse with Name = SIGNUP_NAME   (no browser needed)
                       │
           phone notification: ✅ signed up / ⚠️ sign up manually
```

Safety rails:
- Only forms whose **title** contains `comp practice` are submitted (so a carpool or
  tournament form posted in the chat is ignored). Optionally restrict to links posted by
  exec members with `ALLOWED_SENDER_IDS`.
- Submits **once per form** (remembered in `submitted.json` when run locally).
- If the execs add a new **required** question the bot can't answer, it does *not*
  submit a half-filled form — it pushes an urgent "sign up manually" notification with
  the link. Pre-answer likely questions with `EXTRA_ANSWERS`.
- If it starts late, it scans today's earlier messages and still signs you up.

## Setup (≈10 minutes)

1. **GroupMe token** — log in at <https://dev.groupme.com>, click **Access Token** (top
   right), copy it. It acts as your account, so keep it secret.
2. **Install & configure**
   ```bash
   pip install -r requirements.txt
   cp .env.example .env          # then fill in GROUPME_TOKEN and SIGNUP_NAME
   python signup_bot.py groups   # find "26-27 Tennis Comp Team" → put its ID in GROUPME_GROUP_ID
   ```
3. **Notifications (recommended)** — install the free **ntfy** app on your phone,
   subscribe to a long random topic name (e.g. `sanjith-tennis-8f3k2q`), and set
   `NTFY_TOPIC` to the same name.
4. **Test it before Wednesday**
   - Make your own Google Form with a *Name* field and a title containing
     "Comp Practice", then:
     ```bash
     python signup_bot.py inspect "<your test form link>"   # shows what it would send
     python signup_bot.py submit  "<your test form link>"   # actually submits; check Responses tab
     ```
   - Full dress rehearsal: create a GroupMe group with just yourself, temporarily set
     `GROUPME_GROUP_ID` to it, run `python signup_bot.py run --forever`, and post the
     test-form link from your phone. You should get the ✅ notification within a second.

## Running it every Wednesday

**Option A — GitHub Actions (free, nothing to keep on).**
Push this folder to a **private** GitHub repo and add repository secrets
(Settings → Secrets and variables → Actions): `GROUPME_TOKEN`, `GROUPME_GROUP_ID`,
`SIGNUP_NAME`, and optionally `NTFY_TOPIC`, `ALLOWED_SENDER_IDS`, `EXTRA_ANSWERS`.
The workflow in `.github/workflows/signup.yml` starts every Wednesday morning before
10 AM and listens until 2:30 PM ET (it handles the November time change). Use the
**Run workflow** button once to confirm it connects.
Caveats: GitHub occasionally starts scheduled jobs late (a backup run is scheduled for
that), and it pauses schedules in repos with no commits for 60 days — make a commit
now and then during the season.

**Option B — an always-on machine** (a Raspberry Pi, a free cloud VM, a desktop that
doesn't sleep). Add a cron entry in that machine's timezone:
```
45 9 * * 3  cd /path/to/signup-bot && python3 signup_bot.py run >> bot.log 2>&1
```
A laptop works only if it's awake and online the whole window.

## Config reference

| Variable | Default | Meaning |
|---|---|---|
| `GROUPME_TOKEN` | — | Your GroupMe access token |
| `GROUPME_GROUP_ID` | — | Comp team group ID (`python signup_bot.py groups`) |
| `SIGNUP_NAME` | — | Text entered in the Name field |
| `TITLE_KEYWORDS` | `comp practice` | Form title must contain all (comma-separated) |
| `ALLOWED_SENDER_IDS` | *(anyone)* | Only act on links from these user IDs (`python signup_bot.py members`) |
| `EXTRA_ANSWERS` | — | JSON, e.g. `{"one-day doubles": "Yes"}` matched to question titles |
| `NTFY_TOPIC` | — | ntfy.sh topic for phone notifications |
| `WINDOW_END` | `14:30` | Stop listening at this Eastern time (`off` = never) |
| `DRY_RUN` | `0` | `1` = log what would be submitted without submitting |

## Limits worth knowing
- "First" isn't guaranteed: per the form, players in that weekend's tournament get
  priority regardless of order, and if someone else runs a bot it's a race of
  milliseconds.
- If the club switches to a form that requires Google sign-in, the bot can't submit
  and will notify you to do it manually.
- Don't also submit by hand, or you'll appear twice on the list.
