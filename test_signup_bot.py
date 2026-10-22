"""Offline tests: python -m pytest -q   (no network needed)"""
import asyncio
import json

import aiohttp
import pytest

import signup_bot as sb

FORM_ID = "1FAIpQLSfFwNjixl2Kp9V4iW0TvpY3b585CJs2dzRA-dSU0dG3ROYtYw"
LINK = f"https://docs.google.com/forms/d/e/{FORM_ID}/viewform?usp=publish-editor"
BASE = f"https://docs.google.com/forms/d/e/{FORM_ID}"


def make_html(title="10/28/2026 Comp Practice Sign-Up", extra_required=False):
    items = [
        [111, "Name", None, 0, [[987654321, None, 1]]],
        [222, "Comments / Suggestions for Practice", None, 0, [[876543210, None, 0]]],
    ]
    if extra_required:
        items.append([333, "Are you playing One-Day Doubles?", None, 2,
                      [[765432109, [["Yes"], ["No"]], 1]]])
    data = [None, ["Date: Wednesday...", items, None, None, None, None, None, None, title],
            "/forms", title, None]
    return (
        "<html><body><form>"
        '<input type="hidden" name="fvv" value="1">'
        '<input type="hidden" name="fbzx" value="-4815162342">'
        f"</form><script>var FB_PUBLIC_LOAD_DATA_ = {json.dumps(data)};\n</script></body></html>"
    )


def cfg(**kw):
    c = sb.Config(token="t", group_id="42", name="Sanjith Devineni")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_extract_links():
    text = f"Here's the link to today's practice!\n{LINK}\nSee you there."
    assert sb.extract_form_links(text) == [LINK]
    assert sb.extract_form_links("sign up: https://forms.gle/AbC123xyz.") == ["https://forms.gle/AbC123xyz"]
    assert sb.extract_form_links("no links here") == []


def test_parse_form():
    f = sb.parse_form(make_html(), LINK)
    assert f.base_url == BASE
    assert f.title == "10/28/2026 Comp Practice Sign-Up"
    assert [(q.title, q.entry_id, q.required) for q in f.questions] == [
        ("Name", 987654321, True), ("Comments / Suggestions for Practice", 876543210, False)]
    assert f.fbzx == "-4815162342"


def test_parse_form_requires_data():
    with pytest.raises(sb.FormError):
        sb.parse_form("<html>sign in</html>", LINK)


def test_build_answers_name_only():
    payload, missing = sb.build_answers(sb.parse_form(make_html(), LINK), cfg())
    assert missing == []
    assert payload["entry.987654321"] == "Sanjith Devineni"
    assert "entry.876543210" not in payload  # optional comments left blank
    assert payload["fbzx"] == "-4815162342" and payload["pageHistory"] == "0"


def test_extra_required_question_blocks_unless_configured():
    f = sb.parse_form(make_html(extra_required=True), LINK)
    _, missing = sb.build_answers(f, cfg())
    assert missing == ["Are you playing One-Day Doubles?"]
    payload, missing = sb.build_answers(f, cfg(extra_answers={"one-day doubles": "yes"}))
    assert missing == [] and payload["entry.765432109"] == "Yes"


def test_title_filter():
    assert sb.title_matches("10/28/2026 Comp Practice Sign-Up", ["comp practice"])
    assert not sb.title_matches("Tournament Carpool", ["comp practice"])


# ---------------------------------------------------------------- end-to-end (mocked HTTP)

class FakeGoogle:
    def __init__(self, html, confirm=True):
        self.html, self.confirm, self.posts = html, confirm, []

    async def fetch(self, url):
        return self.html, url

    async def post(self, url, payload, referer):
        self.posts.append((url, dict(payload)))
        body = "<div>Your response has been recorded.</div>" if self.confirm else "<form>error</form>"
        return (200 if self.confirm else 400), body, url


async def run_bot(google, message_sources, c=None):
    async with aiohttp.ClientSession() as s:
        bot = sb.SignupBot(c or cfg(), s)
        bot.submitted.clear()
        bot._save_state = lambda: None
        bot._fetch, bot._post = google.fetch, google.post
        # same message arriving via push AND poll simultaneously
        await asyncio.gather(*(bot.on_message(m, via) for m, via in message_sources))
        return bot


def gm_msg(text, mid="1001", group="42", user="555"):
    return {"id": mid, "group_id": group, "user_id": user, "text": text, "attachments": []}


def test_end_to_end_submits_once():
    g = FakeGoogle(make_html())
    msg = gm_msg(f"Here's the link to today's practice!\n{LINK}")
    bot = asyncio.run(run_bot(g, [(msg, "push"), (msg, "poll"), (dict(msg), "catch-up")]))
    assert len(g.posts) == 1
    url, payload = g.posts[0]
    assert url == BASE + "/formResponse"
    assert payload["entry.987654321"] == "Sanjith Devineni"
    assert bot.done.is_set() and BASE in bot.submitted


def test_ignores_other_groups_senders_and_titles():
    g = FakeGoogle(make_html())
    asyncio.run(run_bot(g, [(gm_msg(LINK, group="99"), "push")]))
    asyncio.run(run_bot(g, [(gm_msg(LINK, user="777"), "push")], cfg(allowed_senders={"555"})))
    asyncio.run(run_bot(FakeGoogle(make_html(title="Carpool for Saturday")), [(gm_msg(LINK), "push")]))
    assert g.posts == []


def test_unanswerable_question_does_not_submit():
    g = FakeGoogle(make_html(extra_required=True))
    bot = asyncio.run(run_bot(g, [(gm_msg(LINK), "push")]))
    assert g.posts == [] and not bot.done.is_set()


def test_rejected_submission_reported():
    g = FakeGoogle(make_html(), confirm=False)
    bot = asyncio.run(run_bot(g, [(gm_msg(LINK), "push")]))
    assert len(g.posts) == 1 and not bot.done.is_set() and BASE not in bot.submitted


def test_dry_run_never_posts():
    g = FakeGoogle(make_html())
    bot = asyncio.run(run_bot(g, [(gm_msg(LINK), "push")], cfg(dry_run=True)))
    assert g.posts == [] and bot.done.is_set()


def test_push_loop_delivers_line_create():
    """Simulate GroupMe's Faye push protocol and check a pushed message triggers a submission."""
    g = FakeGoogle(make_html())

    async def scenario():
        async with aiohttp.ClientSession() as s:
            bot = sb.SignupBot(cfg(), s)
            bot.submitted.clear()
            bot._save_state = lambda: None
            bot._fetch, bot._post = g.fetch, g.post
            bot.user_id = "555"
            calls = []

            async def fake_faye(msgs, timeout=20):
                ch = msgs[0]["channel"]
                calls.append(ch)
                if ch == "/meta/handshake":
                    return [{"channel": ch, "successful": True, "clientId": "abc"}]
                if ch == "/meta/subscribe":
                    assert msgs[0]["subscription"] == "/user/555"
                    return [{"channel": ch, "successful": True}]
                await asyncio.sleep(0.01)
                return [{"channel": "/meta/connect", "successful": True},
                        {"channel": "/user/555", "data": {"type": "line.create",
                                                          "subject": gm_msg(LINK, mid="2002")}}]

            bot._faye = fake_faye
            task = asyncio.create_task(bot.push_loop())
            await asyncio.wait_for(bot.done.wait(), 2)
            task.cancel()
            return calls

    calls = asyncio.run(scenario())
    assert calls[:2] == ["/meta/handshake", "/meta/subscribe"]
    assert len(g.posts) == 1
