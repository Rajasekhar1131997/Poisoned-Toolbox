import json

import pytest

import game
from game import Session, handle, load_level, public_state


def play(session, *inputs, coach=None):
    """Send inputs in order and return the text of the last reply."""
    reply = None
    for text in inputs:
        reply = handle(session, text, coach)
    return reply[-1]["text"]


# --- levels -------------------------------------------------------------------

@pytest.mark.parametrize("level_id", sorted(game.LEVEL_FILES))
def test_levels_load_with_required_fields(level_id):
    level = load_level(level_id)
    assert level["id"] == level_id
    for key in ("title", "topics", "briefing", "teaching", "evidence", "answer", "bonus", "hints", "lesson"):
        assert key in level
    assert len(level["hints"]) == 3


@pytest.mark.parametrize("bad", [0, 4, -1, "1", "../README.md", 1.0, True, None])
def test_load_level_rejects_anything_outside_allowlist(bad):
    with pytest.raises(ValueError):
        load_level(bad)


def test_load_level_returns_a_copy():
    load_level(1)["title"] = "tampered"
    assert load_level(1)["title"] == "The Sticky Note"


@pytest.mark.parametrize("level_id", sorted(game.LEVEL_FILES))
def test_coach_context_never_contains_answers_or_evidence(level_id):
    level = load_level(level_id)
    ctx = game.coach_context(level)
    assert set(ctx) == {"title", "topics", "briefing", "teaching"}
    text = json.dumps(ctx).lower()
    for item in level["answer"] + level["bonus"]:
        if item["kind"] != "msg":
            assert item["id"] not in text


def test_weather_tool_highlight_covers_hidden_block():
    tool = load_level(2)["evidence"]["tools"][0]
    start, end = tool["highlight"]
    span = tool["description"][start:end]
    assert span.startswith("<IMPORTANT>") and span.endswith("</IMPORTANT>")


def test_fake_secrets_look_fake():
    raw = json.dumps(load_level(2))
    assert "FAKE-ssh-key-DO-NOT-USE" in raw
    assert "BEGIN" not in raw


# --- parser -------------------------------------------------------------------

def test_empty_input_is_ignored():
    assert handle(Session(), "   ") == []


def test_long_input_is_rejected():
    s = Session()
    assert "too long" in play(s, "x" * (game.MAX_INPUT + 1))
    assert s.history == []


@pytest.mark.parametrize("text", ["/", "/bogus", "/help"])
def test_help_and_unknown_commands_show_help(text):
    assert "/flag <kind> <id>" in play(Session(), text)


def test_commands_are_case_insensitive():
    s = Session()
    assert "Correct" in play(s, "/FLAG MSG #3")


@pytest.mark.parametrize("text", ["/flag", "/flag msg", "/flag banana 3", "/inspect tool"])
def test_malformed_commands_show_usage_without_penalty(text):
    s = Session()
    assert "Usage" in play(s, text)
    assert s.wrong == 0


def test_inspect_shows_evidence():
    s = Session()
    assert "admin mode" in play(s, "/inspect msg 3")
    assert "No msg" in play(s, "/inspect msg 99")


# --- level 1 ------------------------------------------------------------------

def test_level1_wrong_flag_then_correct():
    s = Session()
    assert "-5" in play(s, "/flag msg 1")
    reply = play(s, "/flag msg 3")
    assert "Case closed" in reply and "Lesson:" in reply
    assert s.solved
    assert s.results[1] == {"score": 95, "stars": 2}


def test_perfect_level_gets_three_stars():
    s = Session()
    play(s, "/flag msg 3")
    assert s.results[1] == {"score": 100, "stars": 3}


def test_hints_cost_points_and_run_out():
    s = Session()
    for n in range(1, 4):
        assert f"Hint {n}/3" in play(s, "/hint")
    assert "No hints left" in play(s, "/hint")
    assert s.hints_used == 3
    play(s, "/flag msg 3")
    assert s.results[1] == {"score": 70, "stars": 2}


def test_score_never_goes_negative():
    s = Session()
    for _ in range(30):
        play(s, "/flag msg 1")
    play(s, "/flag msg 3")
    assert s.results[1]["score"] == 0
    assert s.results[1]["stars"] == 1


def test_no_penalties_after_solving():
    s = Session()
    play(s, "/flag msg 3", "/flag msg 1", "/hint")
    assert s.wrong == 0 and s.hints_used == 0


def test_next_requires_solving():
    s = Session()
    assert "Solve this case first" in play(s, "/next")
    assert s.level == 1
    play(s, "/flag msg 3")
    assert "The Helpful Weather Tool" in play(s, "/next")
    assert s.level == 2


def test_start_resets_level_progress():
    s = Session()
    play(s, "/hint", "/flag msg 3", "/start")
    assert not s.solved and s.hints_used == 0 and 1 not in s.results


# --- level 2 ------------------------------------------------------------------

def test_level2_decoys_are_wrong():
    s = Session(level=2)
    for decoy in ("/flag tool shell_exec", "/flag tool read_file", "/flag msg 2"):
        assert "Not the culprit" in play(s, decoy)
    assert s.wrong == 3


def test_level2_bonus_before_and_after_solving():
    before = Session(level=2)
    play(before, "/flag param context", "/flag tool weather_lookup")
    after = Session(level=2)
    play(after, "/flag tool weather_lookup", "/flag param context")
    for s in (before, after):
        assert s.results[2] == {"score": 120, "stars": 3}
    assert "already flagged" in play(after, "/flag param context")


# --- level 3 ------------------------------------------------------------------

def test_level3_flags_redirect_to_revoke():
    assert "/revoke" in play(Session(level=3), "/flag tool weather_lookup")


def test_level3_all_scopes_attack_succeeds():
    s = Session(level=3)
    assert "attack succeeded" in play(s, "/replay")
    assert not s.solved and s.wrong == 1


def test_level3_shell_exec_alone_still_exfiltrates():
    s = Session(level=3)
    play(s, "/revoke fs.read", "/revoke net.any", "/revoke email.send", "/revoke fs.write")
    reply = play(s, "/replay")
    assert "attack succeeded" in reply and "shell.exec" in reply
    assert not s.solved


def test_level3_minimal_set_gets_three_stars():
    s = Session(level=3)
    for scope in ("fs.read", "fs.write", "net.any", "email.send", "shell.exec"):
        play(s, f"/revoke {scope}")
    assert "attack blocked" in play(s, "/replay")
    assert s.results[3] == {"score": 100, "stars": 3}


def test_level3_blocked_with_extra_scopes_gets_two_stars():
    s = Session(level=3)
    play(s, "/revoke fs.read", "/revoke shell.exec")
    assert "attack blocked" in play(s, "/replay")
    assert s.results[3]["stars"] == 2


def test_level3_revoking_calendar_breaks_the_product():
    s = Session(level=3)
    play(s, "/revoke calendar.read")
    assert "broke the product" in play(s, "/replay")
    assert not s.solved


def test_level3_revoke_validation_and_start_restores():
    s = Session(level=3)
    assert "Unknown scope" in play(s, "/revoke root")
    play(s, "/revoke fs.read")
    assert "already revoked" in play(s, "/revoke fs.read")
    play(s, "/start")
    assert "fs.read" in s.scopes and len(s.scopes) == 6


def test_revoke_and_replay_outside_level3():
    s = Session()
    assert "no permissions" in play(s, "/revoke fs.read")
    assert "nothing to replay" in play(s, "/replay")


# --- full game ----------------------------------------------------------------

def test_full_playthrough():
    s = Session()
    play(s, "/flag msg 3", "/next", "/flag tool weather_lookup", "/flag param context", "/next")
    for scope in ("fs.read", "fs.write", "net.any", "email.send", "shell.exec"):
        play(s, f"/revoke {scope}")
    play(s, "/replay")
    assert s.total == 320
    assert "last case" in play(s, "/next")
    assert "Total score: 320" in play(s, "/score")


# --- coach --------------------------------------------------------------------

class FakeCoach:
    def __init__(self, reply="Tool poisoning hides instructions in tool descriptions."):
        self.reply = reply
        self.calls = []

    def __call__(self, context, history, text):
        self.calls.append((context, history, text))
        return self.reply


def test_free_text_goes_to_coach_with_teaching_context_only():
    coach = FakeCoach()
    s = Session(level=2)
    replies = handle(s, "what is tool poisoning?", coach)
    assert replies == [{"role": "coach", "text": coach.reply}]
    context, history, text = coach.calls[0]
    assert set(context) == {"title", "topics", "briefing", "teaching"}
    assert text == "what is tool poisoning?" and history == []


def test_coach_history_excludes_commands_and_game_replies():
    coach = FakeCoach()
    s = Session(level=2)
    play(s, "hello", coach=coach)
    play(s, "/inspect tool weather_lookup", "/flag tool weather_lookup", coach=coach)
    play(s, "what now?", coach=coach)
    _, history, _ = coach.calls[-1]
    assert [m["role"] for m in history] == ["player", "coach"]
    assert "IMPORTANT" not in json.dumps(history)
    assert "weather_lookup" not in json.dumps(history)


def test_coach_history_is_capped():
    coach = FakeCoach()
    s = Session()
    for i in range(10):
        play(s, f"question {i}", coach=coach)
    assert len(coach.calls[-1][1]) == game.COACH_HISTORY


def test_coach_offline_and_call_cap():
    s = Session()
    assert "offline" in play(s, "hi")
    coach = FakeCoach()
    for _ in range(game.MAX_COACH_CALLS):
        play(s, "hi", coach=coach)
    assert "needs a break" in play(s, "hi", coach=coach)
    assert len(coach.calls) == game.MAX_COACH_CALLS


def test_coach_failure_is_contained():
    def broken(context, history, text):
        raise RuntimeError("API down")

    replies = handle(Session(), "hi", broken)
    assert replies[0]["role"] == "game" and "couldn't answer" in replies[0]["text"]


# --- public state -------------------------------------------------------------

@pytest.mark.parametrize("level_id", sorted(game.LEVEL_FILES))
def test_public_state_hides_answers(level_id):
    s = Session(level=level_id)
    state = public_state(s)
    assert state["hints"] == [] and state["lesson"] is None
    raw = json.dumps(state)
    for key in ("answer", "bonus", "simulation", "task_needs"):
        assert f'"{key}"' not in raw
    json.dumps(state)  # must be JSON-serializable for the API


def test_public_state_reveals_hints_and_lesson_as_earned():
    s = Session()
    play(s, "/hint")
    assert len(public_state(s)["hints"]) == 1
    play(s, "/flag msg 3")
    state = public_state(s)
    assert state["solved"] and state["lesson"] and state["found"] == [["msg", "3"]]
    assert state["results"] == {"1": {"score": 90, "stars": 2}}


def test_history_is_bounded():
    s = Session()
    for _ in range(game.MAX_HISTORY):
        play(s, "/score")
    assert len(s.history) == game.MAX_HISTORY
