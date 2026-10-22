#!/usr/bin/env python3
"""
Comp Practice sign-up bot.

Watches a GroupMe group chat in real time. The moment someone posts a Google
Forms link whose title matches (e.g. "10/28/2026 Comp Practice Sign-Up"), it
fetches the form, fills in your name, and submits it -- typically well under
a second after the message lands.

Two listeners run side by side and de-duplicate each other:
  * GroupMe push (Faye long-polling) -- near-instant delivery
  * GroupMe REST polling every ~1.5s -- fallback if push hiccups

Commands
  python signup_bot.py run              listen until WINDOW_END (or first successful sign-up)
  python signup_bot.py run --forever    listen indefinitely
  python signup_bot.py groups           list your GroupMe groups and their IDs
  python signup_bot.py members          list members of GROUPME_GROUP_ID (for ALLOWED_SENDER_IDS)
  python signup_bot.py inspect URL      show what the bot would submit to a form (no submit)
  python signup_bot.py submit URL       submit to a form right now (use with your own test form)

Configuration is read from environment variables (or a .env file next to this script);
see .env.example.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

TZ = ZoneInfo("America/New_York")
GM_API = "https://api.groupme.com/v3"
GM_PUSH = "https://push.groupme.com/faye"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "submitted.json"

FORM_LINK_RE = re.compile(
    r"https?://(?:docs\.google\.com/forms/[^\s<>\"']+|forms\.gle/[A-Za-z0-9_-]+)", re.I
)
FORM_BASE_RE = re.compile(r"(https://docs\.google\.com/forms/(?:u/\d+/)?d/(?:e/)?[A-Za-z0-9_-]+)")
NAME_TITLE_RE = re.compile(r"^\s*(?:your\s+|full\s+|first\s+(?:and|&)\s+last\s+)?name\b", re.I)
CONFIRM_MARKERS = (
    "Your response has been recorded",
    "freebirdFormviewerViewResponseConfirmationMessage",
    'class="vHW8K"',
    "Submit another response",
)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

log = logging.getLogger("signup")


# --------------------------------------------------------------------------- config

def load_dotenv(path: Path = HERE / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


@dataclass
class Config:
    token: str = ""
    group_id: str = ""
    name: str = ""
    title_keywords: list[str] = field(default_factory=lambda: ["comp practice"])
    allowed_senders: set[str] = field(default_factory=set)
    extra_answers: dict[str, str] = field(default_factory=dict)
    ntfy_topic: str | None = None
    window_end: dtime | None = dtime(14, 30)
    dry_run: bool = False
    poll_interval: float = 1.5
    exit_after_submit: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        cfg = cls(
            token=e("GROUPME_TOKEN", "").strip(),
            group_id=e("GROUPME_GROUP_ID", "").strip(),
            name=e("SIGNUP_NAME", "").strip(),
            ntfy_topic=(e("NTFY_TOPIC") or "").strip() or None,
            dry_run=e("DRY_RUN", "0").lower() in ("1", "true", "yes"),
            poll_interval=float(e("POLL_INTERVAL", "1.5")),
            exit_after_submit=e("EXIT_AFTER_SUBMIT", "1").lower() in ("1", "true", "yes"),
        )
        if e("TITLE_KEYWORDS"):
            cfg.title_keywords = [k.strip().lower() for k in e("TITLE_KEYWORDS").split(",") if k.strip()]
        if e("ALLOWED_SENDER_IDS"):
            cfg.allowed_senders = {s.strip() for s in e("ALLOWED_SENDER_IDS").split(",") if s.strip()}
        if e("EXTRA_ANSWERS"):
            cfg.extra_answers = json.loads(e("EXTRA_ANSWERS"))
        we = (e("WINDOW_END") or "14:30").strip()
        if we.lower() in ("", "none", "off"):
            cfg.window_end = None
        else:
            h, m = we.split(":")
            cfg.window_end = dtime(int(h), int(m))
        return cfg

    def require(self, *fields: str) -> None:
        missing = [f for f in fields if not getattr(self, f)]
        if missing:
            env = {"token": "GROUPME_TOKEN", "group_id": "GROUPME_GROUP_ID", "name": "SIGNUP_NAME"}
            sys.exit("Missing config: " + ", ".join(env.get(f, f) for f in missing))


# --------------------------------------------------------------------------- form parsing

class FormError(Exception):
    pass


@dataclass
class Question:
    title: str
    entry_id: int
    required: bool
    qtype: int
    options: list[str]


@dataclass
class ParsedForm:
    base_url: str
    title: str
    questions: list[Question]
    fbzx: str | None
    page_count: int


def extract_form_links(text: str) -> list[str]:
    links = []
    for raw in FORM_LINK_RE.findall(text or ""):
        link = raw.rstrip(".,;:!?)]}")
        if link not in links:
            links.append(link)
    return links


def form_base(url: str) -> str | None:
    m = FORM_BASE_RE.search(url)
    return m.group(1) if m else None


def parse_form(html: str, final_url: str) -> ParsedForm:
    base = form_base(final_url)
    if not base:
        raise FormError(f"Not a Google Form URL: {final_url}")
    m = re.search(r"var FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", html, re.S)
    if not m:
        raise FormError("Form data not found (form may require Google sign-in or be closed)")
    data = json.loads(m.group(1))
    info = data[1]
    title = ""
    if len(info) > 8 and isinstance(info[8], str):
        title = info[8]
    if not title and len(data) > 3 and isinstance(data[3], str):
        title = data[3]

    questions: list[Question] = []
    pages = 1
    for item in info[1] or []:
        qtype = item[3] if len(item) > 3 else None
        if qtype == 8:  # page break
            pages += 1
            continue
        if len(item) < 5 or not item[4]:
            continue  # title/description/image blocks
        for sub in item[4]:
            opts = [o[0] for o in (sub[1] or []) if o and isinstance(o[0], str) and o[0]] if len(sub) > 1 else []
            questions.append(Question(
                title=(item[1] or "").strip(),
                entry_id=int(sub[0]),
                required=bool(sub[2]) if len(sub) > 2 else False,
                qtype=qtype if qtype is not None else -1,
                options=opts,
            ))

    fbzx = None
    for tag in re.findall(r"<input[^>]*>", html):
        if re.search(r'name="fbzx"', tag):
            v = re.search(r'value="([^"]*)"', tag)
            fbzx = v.group(1) if v else None
            break
    return ParsedForm(base, title, questions, fbzx, pages)


def title_matches(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return all(k in t for k in keywords)


def build_answers(form: ParsedForm, cfg: Config) -> tuple[dict[str, str], list[str]]:
    """Return (payload, unanswered_required_question_titles)."""
    answers: dict[str, str] = {}
    missing: list[str] = []
    name_done = False
    for q in form.questions:
        val: str | None = None
        for key, v in cfg.extra_answers.items():
            if key.lower() in q.title.lower():
                val = v
                break
        if val is None and not name_done and NAME_TITLE_RE.search(q.title):
            val = cfg.name
            name_done = True
        if val is not None and q.options:
            match = next((o for o in q.options if o.lower() == val.lower()), None) or \
                    next((o for o in q.options if val.lower() in o.lower()), None)
            val = match
        if val is None:
            if q.required:
                missing.append(q.title or f"entry.{q.entry_id}")
            continue
        answers[f"entry.{q.entry_id}"] = val
    if not name_done:
        # No question titled "Name": fall back to the first required short-answer field.
        first = next((q for q in form.questions if q.qtype == 0 and q.required
                      and f"entry.{q.entry_id}" not in answers), None)
        if first:
            answers[f"entry.{first.entry_id}"] = cfg.name
            if first.title in missing:
                missing.remove(first.title)
        else:
            missing.append("(no Name field found)")

    payload = dict(answers)
    payload["fvv"] = "1"
    payload["pageHistory"] = ",".join(str(i) for i in range(form.page_count))
    if form.fbzx:
        payload["fbzx"] = form.fbzx
        payload["partialResponse"] = json.dumps([None, None, form.fbzx])
    return payload, missing


def looks_confirmed(body: str) -> bool:
    return any(mk in body for mk in CONFIRM_MARKERS)


# --------------------------------------------------------------------------- the bot

class SignupBot:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.s = session
        self.seen_msgs: set[str] = set()
        self.seen_links: set[str] = set()
        self.submitted: set[str] = set(self._load_state())
        self.done = asyncio.Event()
        self.user_id: str | None = None

    # ---- state
    @staticmethod
    def _load_state() -> list[str]:
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return []

    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps(sorted(self.submitted), indent=1))
        except Exception as e:  # read-only FS etc.
            log.debug("could not save state: %s", e)

    # ---- notifications
    async def notify(self, title: str, msg: str, urgent: bool = False, click: str | None = None) -> None:
        log.info("NOTIFY %s: %s", title, msg)
        if not self.cfg.ntfy_topic:
            return
        headers = {"Title": title, "Priority": "5" if urgent else "3"}
        if click:
            headers["Click"] = click
        try:
            async with self.s.post(f"https://ntfy.sh/{self.cfg.ntfy_topic}", data=msg.encode(),
                                   headers=headers, timeout=aiohttp.ClientTimeout(total=10)):
                pass
        except Exception as e:
            log.warning("ntfy failed: %s", e)

    # ---- HTTP to Google (thin wrappers so tests can swap them)
    async def _fetch(self, url: str) -> tuple[str, str]:
        async with self.s.get(url, headers={"User-Agent": UA}, allow_redirects=True,
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.text(), str(r.url)

    async def _post(self, url: str, payload: dict, referer: str) -> tuple[int, str, str]:
        async with self.s.post(url, data=payload, allow_redirects=True,
                               headers={"User-Agent": UA, "Referer": referer, "Origin": "https://docs.google.com"},
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status, await r.text(), str(r.url)

    async def submit_form(self, form: ParsedForm, payload: dict) -> tuple[bool, str]:
        url = form.base_url + "/formResponse"
        referer = form.base_url + "/viewform"
        for attempt in range(3):
            try:
                status, body, final = await self._post(url, payload, referer)
            except aiohttp.ClientConnectorError as e:  # never reached Google: safe to retry
                log.warning("connect error (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(0.25)
                continue
            if "closedform" in final:
                return False, "form is closed"
            if status == 200 and looks_confirmed(body):
                return True, "confirmed by Google"
            if status == 200:
                return True, "HTTP 200 but no confirmation text seen -- please double-check"
            if status >= 500:
                await asyncio.sleep(0.25)
                continue
            return False, f"Google rejected the submission (HTTP {status})"
        return False, "could not reach Google after 3 tries"

    # ---- core: a form link was seen
    async def handle_link(self, link: str, t_seen: float, via: str) -> None:
        key = link.split("?")[0]
        if key in self.seen_links:
            return
        self.seen_links.add(key)
        log.info("[%s] form link: %s", via, link)
        try:
            html, final = await self._fetch(link)
        except Exception as e:
            await self.notify("Sign-up bot: couldn't open form", f"{e}\n{link}", urgent=True, click=link)
            return
        if "accounts.google.com" in final:
            await self.notify("Sign-up bot: form needs sign-in", f"Sign up manually: {link}", urgent=True, click=link)
            return
        if "closedform" in final:
            await self.notify("Sign-up bot: form closed", link, urgent=True, click=link)
            return
        try:
            form = parse_form(html, final)
        except FormError as e:
            await self.notify("Sign-up bot: couldn't read form", f"{e}\n{link}", urgent=True, click=link)
            return
        if form.base_url in self.submitted:
            log.info("already submitted to %s; skipping", form.base_url)
            return
        if not title_matches(form.title, self.cfg.title_keywords):
            log.info("title %r doesn't match %s; ignoring", form.title, self.cfg.title_keywords)
            return
        payload, missing = build_answers(form, self.cfg)
        if missing:
            await self.notify("Sign-up bot: SIGN UP MANUALLY",
                              f"'{form.title}' has required questions I can't answer: {', '.join(missing)}\n{link}",
                              urgent=True, click=link)
            return
        if self.cfg.dry_run:
            log.info("DRY RUN -- would POST to %s: %s", form.base_url + "/formResponse", payload)
            self.submitted.add(form.base_url)
            self.done.set()
            return
        ok, detail = await self.submit_form(form, payload)
        ms = (time.perf_counter() - t_seen) * 1000
        if ok:
            self.submitted.add(form.base_url)
            self._save_state()
            await self.notify("Signed up for practice ✅",
                              f"{form.title} as '{self.cfg.name}' -- {detail} ({ms:.0f} ms after the link arrived)")
            if self.cfg.exit_after_submit:
                self.done.set()
        else:
            self.seen_links.discard(key)  # allow a retry if the link is re-posted
            await self.notify("Sign-up bot FAILED -- sign up manually",
                              f"{form.title}: {detail}\n{link}", urgent=True, click=link)

    async def on_message(self, msg: dict, via: str) -> None:
        t_seen = time.perf_counter()
        mid = str(msg.get("id", ""))
        if not mid or mid in self.seen_msgs:
            return
        self.seen_msgs.add(mid)
        if str(msg.get("group_id")) != str(self.cfg.group_id):
            return
        if self.cfg.allowed_senders and str(msg.get("user_id")) not in self.cfg.allowed_senders:
            return
        texts = [msg.get("text") or ""]
        for att in msg.get("attachments") or []:
            if isinstance(att, dict) and att.get("url"):
                texts.append(att["url"])
        links = extract_form_links(" ".join(texts))
        await asyncio.gather(*(self.handle_link(l, t_seen, via) for l in links))

    # ---- GroupMe
    async def gm_get(self, path: str, **params) -> dict | None:
        params["token"] = self.cfg.token
        async with self.s.get(GM_API + path, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 304:
                return None
            if r.status == 401:
                raise SystemExit("GroupMe rejected the token (401). Check GROUPME_TOKEN.")
            r.raise_for_status()
            return (await r.json())["response"]

    async def _faye(self, msgs: list[dict], timeout: float = 20) -> list[dict]:
        async with self.s.post(GM_PUSH, json=msgs, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def push_loop(self) -> None:
        if not self.user_id:
            me = await self.gm_get("/users/me")
            self.user_id = str(me["id"])
        backoff = 1
        while not self.done.is_set():
            try:
                hs = await self._faye([{"channel": "/meta/handshake", "version": "1.0",
                                        "supportedConnectionTypes": ["long-polling"], "id": "1"}])
                cid = hs[0]["clientId"]
                sub = await self._faye([{"channel": "/meta/subscribe", "clientId": cid,
                                         "subscription": f"/user/{self.user_id}", "id": "2",
                                         "ext": {"access_token": self.cfg.token, "timestamp": int(time.time())}}])
                if not sub[0].get("successful"):
                    raise RuntimeError(f"subscribe failed: {sub[0].get('error')}")
                log.info("push: connected")
                backoff, n = 1, 3
                while not self.done.is_set():
                    resp = await self._faye([{"channel": "/meta/connect", "clientId": cid,
                                              "connectionType": "long-polling", "id": str(n)}], timeout=120)
                    n += 1
                    reconnect = False
                    for m in resp:
                        if m.get("channel") == "/meta/connect":
                            if not m.get("successful"):
                                reconnect = True
                        else:
                            data = m.get("data") or {}
                            if data.get("type") == "line.create" and isinstance(data.get("subject"), dict):
                                asyncio.create_task(self.on_message(data["subject"], "push"))
                    if reconnect:
                        log.info("push: server asked to re-handshake")
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("push: %s (retrying in %ss)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def poll_loop(self) -> None:
        last_id: int | None = None
        today = datetime.now(TZ).date()
        while not self.done.is_set():
            try:
                if last_id is None:
                    resp = await self.gm_get(f"/groups/{self.cfg.group_id}/messages", limit=20)
                    msgs = sorted(resp["messages"], key=lambda m: int(m["id"])) if resp else []
                    last_id = int(msgs[-1]["id"]) if msgs else 0
                    log.info("poll: watching group %s (catching up on today's messages)", self.cfg.group_id)
                    # Catch-up: a form posted earlier today (e.g. bot started late) still gets handled.
                    for m in msgs:
                        if datetime.fromtimestamp(m["created_at"], TZ).date() == today:
                            await self.on_message(m, "catch-up")
                else:
                    resp = await self.gm_get(f"/groups/{self.cfg.group_id}/messages", after_id=last_id, limit=20)
                    for m in sorted((resp or {}).get("messages", []), key=lambda m: int(m["id"])):
                        last_id = max(last_id, int(m["id"]))
                        asyncio.create_task(self.on_message(m, "poll"))
            except SystemExit:
                raise
            except Exception as e:
                log.warning("poll: %s", e)
            await asyncio.sleep(self.cfg.poll_interval)

    async def run(self, forever: bool) -> None:
        end = None
        if not forever and self.cfg.window_end:
            end = datetime.combine(datetime.now(TZ).date(), self.cfg.window_end, TZ)
            if datetime.now(TZ) >= end:
                log.info("Past WINDOW_END (%s); nothing to do.", self.cfg.window_end)
                return
            log.info("Listening until %s ET%s", end.strftime("%-I:%M %p"),
                     " (DRY RUN)" if self.cfg.dry_run else "")
        tasks = [asyncio.create_task(self.poll_loop()), asyncio.create_task(self.push_loop())]
        try:
            if end:
                timeout = (end - datetime.now(TZ)).total_seconds()
                try:
                    await asyncio.wait_for(self.done.wait(), timeout)
                except asyncio.TimeoutError:
                    log.info("Window ended without a matching form.")
                    await self.notify("Sign-up bot: no form seen",
                                      f"No matching form was posted before {self.cfg.window_end:%H:%M}.")
            else:
                await self.done.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------- CLI

async def amain(args) -> None:
    cfg = Config.from_env()
    async with aiohttp.ClientSession() as s:
        bot = SignupBot(cfg, s)
        if args.cmd == "run":
            cfg.require("token", "group_id", "name")
            await bot.run(args.forever)
        elif args.cmd == "groups":
            cfg.require("token")
            for g in await bot.gm_get("/groups", per_page=100, omit="memberships") or []:
                print(f"{g['id']:>12}  {g['name']}")
        elif args.cmd == "members":
            cfg.require("token", "group_id")
            g = await bot.gm_get(f"/groups/{cfg.group_id}")
            for m in sorted(g["members"], key=lambda m: m["nickname"].lower()):
                print(f"{m['user_id']:>12}  {m['nickname']}")
        elif args.cmd in ("inspect", "submit"):
            cfg.require("name")
            html, final = await bot._fetch(args.url)
            form = parse_form(html, final)
            payload, missing = build_answers(form, cfg)
            print(f"Title: {form.title}")
            print(f"Title matches {cfg.title_keywords}: {title_matches(form.title, cfg.title_keywords)}")
            for q in form.questions:
                print(f"  entry.{q.entry_id:<12} {'*' if q.required else ' '} {q.title}"
                      + (f"  options={q.options}" if q.options else ""))
            print("Would submit:", {k: v for k, v in payload.items() if k.startswith("entry.")})
            if missing:
                print("Unanswerable required questions:", missing)
            if args.cmd == "submit":
                if missing:
                    sys.exit("Not submitting: required questions unanswered.")
                t0 = time.perf_counter()
                ok, detail = await bot.submit_form(form, payload)
                print(("OK: " if ok else "FAILED: ") + detail, f"({(time.perf_counter()-t0)*1000:.0f} ms)")


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Auto sign-up for Comp Practice via GroupMe + Google Forms")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--forever", action="store_true", help="ignore WINDOW_END and keep listening")
    sub.add_parser("groups")
    sub.add_parser("members")
    for c in ("inspect", "submit"):
        sub.add_parser(c).add_argument("url")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
