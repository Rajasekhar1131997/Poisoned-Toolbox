"""Pure game logic for Poisoned Toolbox: command parser, level engine, win checks, scoring.

The engine is deterministic. It never calls an LLM itself: free-text messages are
forwarded to a Coach callable supplied by the caller, and the Coach only ever sees
each level's teaching context (never the evidence, answers or hints).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

LEVELS_DIR = Path(__file__).parent / "levels"
# Fixed allowlist: levels are only ever loaded by integer id, never by a user-supplied path.
LEVEL_FILES = {
    1: "01_sticky_note.json",
    2: "02_weather_tool.json",
    3: "03_least_privilege.json",
}
MAX_LEVEL = max(LEVEL_FILES)

LEVEL_POINTS = 100
BONUS_POINTS = 20
HINT_COST = 10
WRONG_COST = 5
MAX_INPUT = 500
MAX_COACH_CALLS = 30
COACH_HISTORY = 8
MAX_HISTORY = 100

FLAG_KINDS = ("msg", "tool", "param", "doc")
INSPECT_KINDS = ("msg", "tool", "doc", "scope")
COACH_FIELDS = ("title", "topics", "briefing", "teaching")

# coach(context, history, text) -> reply. history holds only player chat and Coach replies.
Coach = Callable[[dict, list, str], str]


@lru_cache(maxsize=None)
def _read_level(level_id: int) -> str:
    return (LEVELS_DIR / LEVEL_FILES[level_id]).read_text(encoding="utf-8")


def load_level(level_id: int) -> dict:
    if not isinstance(level_id, int) or isinstance(level_id, bool) or level_id not in LEVEL_FILES:
        raise ValueError(f"unknown level: {level_id!r}")
    return copy.deepcopy(json.loads(_read_level(level_id)))


def coach_context(level: dict) -> dict:
    """The only part of a level the Coach may see."""
    return {key: level[key] for key in COACH_FIELDS}


@dataclass
class Session:
    level: int = 1
    results: dict = field(default_factory=dict)  # level id -> {"score": int, "stars": int}
    hints_used: int = 0
    wrong: int = 0
    found: set = field(default_factory=set)  # {(kind, id)} correctly flagged this level
    solved: bool = False
    scopes: list = field(default_factory=list)
    coach_calls: int = 0
    history: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.reset_level()

    def reset_level(self) -> None:
        level = load_level(self.level)
        self.results.pop(self.level, None)
        self.hints_used = 0
        self.wrong = 0
        self.found = set()
        self.solved = False
        self.scopes = [s["id"] for s in level["evidence"].get("scopes", [])]

    @property
    def total(self) -> int:
        return sum(r["score"] for r in self.results.values())


def handle(session: Session, text: str, coach: Coach | None = None) -> list:
    """Apply one chat input and return the new messages as [{"role", "text"}]."""
    text = text.strip()
    if not text:
        return []
    if len(text) > MAX_INPUT:
        return [_game(f"Message too long (max {MAX_INPUT} characters).")]

    if text.startswith("/"):
        replies = [_game(_command(session, text))]
    else:
        replies = [_ask_coach(session, text, coach)]

    session.history.append({"role": "player", "text": text})
    session.history.extend(replies)
    del session.history[:-MAX_HISTORY]
    return replies


def public_state(session: Session) -> dict:
    """Everything the frontend may render. Never includes answers, unrevealed hints or the simulation."""
    level = load_level(session.level)
    evidence = level["evidence"]
    return {
        "level": session.level,
        "max_level": MAX_LEVEL,
        "title": level["title"],
        "topics": level["topics"],
        "briefing": level["briefing"],
        "evidence": evidence,
        "scopes_granted": list(session.scopes),
        "hints": level["hints"][: session.hints_used],
        "hints_total": len(level["hints"]),
        "found": sorted([kind, item] for kind, item in session.found),
        "solved": session.solved,
        "lesson": level["lesson"] if session.solved else None,
        "level_score": _level_score(session),
        "score": session.total,
        "results": {str(k): v for k, v in sorted(session.results.items())},
    }


# --- commands ---------------------------------------------------------------

HELP = """Commands:
/start              restart the current level
/next               go to the next level (once this one is solved)
/case               show the case file
/inspect <kind> <id> read one item (kind: msg | tool | doc | scope)
/flag <kind> <id>   submit an answer (kind: msg | tool | param | doc)
/hint               next hint (-10 pts)
/revoke <scope>     remove a permission (case 3)
/replay             re-run the attack against the current permissions (case 3)
/score              show your score
Anything else goes to the Coach."""


def _command(session: Session, text: str) -> str:
    parts = text[1:].split()
    if not parts:
        return HELP
    name, args = parts[0].lower(), parts[1:]
    handler = COMMANDS.get(name)
    if handler is None:
        return f"Unknown command /{name}.\n\n{HELP}"
    return handler(session, args)


def _cmd_help(session: Session, args: list) -> str:
    return HELP


def _cmd_start(session: Session, args: list) -> str:
    session.reset_level()
    return _briefing(load_level(session.level))


def _cmd_next(session: Session, args: list) -> str:
    if not session.solved:
        return "Solve this case first. Stuck? Try /hint."
    if session.level >= MAX_LEVEL:
        return "That was the last case!\n\n" + _cmd_score(session, [])
    session.level += 1
    session.reset_level()
    return _briefing(load_level(session.level))


def _cmd_case(session: Session, args: list) -> str:
    level = load_level(session.level)
    evidence = level["evidence"]
    lines = [_briefing(level), "", "Evidence:"]
    if evidence["trace"]:
        lines.append("  messages: " + ", ".join(str(m["id"]) for m in evidence["trace"]))
    if evidence["tools"]:
        lines.append("  tools: " + ", ".join(t["id"] for t in evidence["tools"]))
    if evidence["docs"]:
        lines.append("  docs: " + ", ".join(d["id"] for d in evidence["docs"]))
    if "scopes" in evidence:
        lines.append("  scopes granted: " + (", ".join(session.scopes) or "(none)"))
    return "\n".join(lines)


def _cmd_inspect(session: Session, args: list) -> str:
    if len(args) != 2 or args[0].lower() not in INSPECT_KINDS:
        return "Usage: /inspect <kind> <id> (kind: " + " | ".join(INSPECT_KINDS) + ")"
    kind, item_id = args[0].lower(), _normalize(args[1])
    evidence = load_level(session.level)["evidence"]
    if kind == "msg":
        for m in evidence["trace"]:
            if str(m["id"]) == item_id:
                return f"Message {m['id']} [{m['role']}]:\n{m['text']}"
    elif kind == "tool":
        for t in evidence["tools"]:
            if t["id"] == item_id:
                params = ", ".join(f"{k}: {v}" for k, v in t["schema"].items()) or "(none)"
                return f"Tool {t['id']}\nDescription: {t['description']}\nParameters: {params}"
    elif kind == "doc":
        for d in evidence["docs"]:
            if d["id"] == item_id:
                return f"Document {d['id']}:\n{d['text']}"
    elif kind == "scope":
        for s in evidence.get("scopes", []):
            if s["id"] == item_id:
                status = "granted" if s["id"] in session.scopes else "revoked"
                return f"Scope {s['id']} ({status}): {s['description']}"
    return f"No {kind} {item_id!r} in this case file. Try /case."


def _cmd_flag(session: Session, args: list) -> str:
    level = load_level(session.level)
    if not level["answer"]:
        return "This case isn't solved by flagging. Use /revoke <scope> and /replay."
    if len(args) != 2 or args[0].lower() not in FLAG_KINDS:
        return "Usage: /flag <kind> <id> (kind: " + " | ".join(FLAG_KINDS) + ")"
    item = (args[0].lower(), _normalize(args[1]))
    answers = {(a["kind"], a["id"]) for a in level["answer"]}
    bonuses = {(b["kind"], b["id"]) for b in level["bonus"]}

    if item in session.found:
        return "You already flagged that."
    if item in bonuses:
        session.found.add(item)
        if session.solved:
            _record(session, level)
        return f"Bonus! {item[0]} {item[1]} was the exfiltration channel. +{BONUS_POINTS} pts."
    if item in answers:
        session.found.add(item)
        if answers <= session.found:
            return _solve(session, level, "Correct! Case closed.")
        return "Correct. Keep going, there is more to flag."
    if session.solved:
        return "Case already closed. Type /next for the next case."
    session.wrong += 1
    return f"Not the culprit. -{WRONG_COST} pts. Stuck? Try /hint."


def _cmd_hint(session: Session, args: list) -> str:
    hints = load_level(session.level)["hints"]
    if session.solved:
        return "Case already closed. Type /next for the next case."
    if session.hints_used >= len(hints):
        return "No hints left. You've got this."
    session.hints_used += 1
    return f"Hint {session.hints_used}/{len(hints)} (-{HINT_COST} pts): {hints[session.hints_used - 1]}"


def _cmd_revoke(session: Session, args: list) -> str:
    known = [s["id"] for s in load_level(session.level)["evidence"].get("scopes", [])]
    if not known:
        return "There are no permissions to revoke in this case."
    if session.solved:
        return "Case already closed. Type /next for the next case."
    if len(args) != 1:
        return "Usage: /revoke <scope>"
    scope = _normalize(args[0])
    if scope not in known:
        return f"Unknown scope {scope!r}. Scopes: {', '.join(known)}"
    if scope not in session.scopes:
        return f"{scope} is already revoked."
    session.scopes.remove(scope)
    return f"Revoked {scope}. Still granted: {', '.join(session.scopes) or '(none)'}. Type /replay to test."


def _cmd_replay(session: Session, args: list) -> str:
    level = load_level(session.level)
    sim = level.get("simulation")
    if sim is None:
        return "There's nothing to replay in this case."
    if session.solved:
        return "Case already closed. Type /next for the next case."
    granted = set(session.scopes)
    missing = [s for s in sim["task_needs"] if s not in granted]
    if missing:
        session.wrong += 1
        return (f"Replay: the calendar summary failed without {', '.join(missing)}. "
                f"You broke the product! -{WRONG_COST} pts. Type /start to restore all scopes.")
    read = [s for s in sim["attack_read"] if s in granted]
    exfil = [s for s in sim["attack_exfil"] if s in granted]
    if read and exfil:
        session.wrong += 1
        how = (f"read the key and sent it out with {read[0]}" if read[0] == exfil[0]
               else f"read the key with {read[0]} and sent it out with {exfil[0]}")
        return f"Replay: attack succeeded. The agent {how}. -{WRONG_COST} pts."
    stars = 3 if granted == set(sim["task_needs"]) else 2
    return _solve(session, level, "Replay: attack blocked, and the calendar summary still works!", stars)


def _cmd_score(session: Session, args: list) -> str:
    lines = [f"Total score: {session.total}"]
    for level_id in sorted(LEVEL_FILES):
        result = session.results.get(level_id)
        title = load_level(level_id)["title"]
        if result:
            lines.append(f"  Case {level_id}, {title}: {result['score']} pts, {'★' * result['stars']}")
        else:
            lines.append(f"  Case {level_id}, {title}: unsolved")
    return "\n".join(lines)


COMMANDS = {
    "help": _cmd_help,
    "start": _cmd_start,
    "next": _cmd_next,
    "case": _cmd_case,
    "inspect": _cmd_inspect,
    "flag": _cmd_flag,
    "hint": _cmd_hint,
    "revoke": _cmd_revoke,
    "replay": _cmd_replay,
    "score": _cmd_score,
}


# --- helpers ----------------------------------------------------------------

def _game(text: str) -> dict:
    return {"role": "game", "text": text}


def _normalize(item_id: str) -> str:
    return item_id.strip().lower().lstrip("#")


def _briefing(level: dict) -> str:
    return f"Case {level['id']}: {level['title']}\nTopics: {', '.join(level['topics'])}\n\n{level['briefing']}"


def _level_score(session: Session) -> int:
    level = load_level(session.level)
    bonus = sum(1 for b in level["bonus"] if (b["kind"], b["id"]) in session.found)
    base = max(0, LEVEL_POINTS - HINT_COST * session.hints_used - WRONG_COST * session.wrong)
    return base + BONUS_POINTS * bonus


def _flag_stars(session: Session) -> int:
    if session.hints_used == 0 and session.wrong == 0:
        return 3
    return 2 if _level_score(session) >= 70 else 1


def _record(session: Session, level: dict, stars: int | None = None) -> None:
    if stars is None:
        stars = session.results.get(session.level, {}).get("stars") or _flag_stars(session)
    session.results[session.level] = {"score": _level_score(session), "stars": stars}


def _solve(session: Session, level: dict, headline: str, stars: int | None = None) -> str:
    session.solved = True
    _record(session, level, stars)
    result = session.results[session.level]
    nxt = "Type /next for the next case." if session.level < MAX_LEVEL else "That was the last case! Type /score."
    return (f"{headline} {'★' * result['stars']} ({result['score']} pts)\n\n"
            f"Lesson: {level['lesson']}\n\n{nxt}")


def _ask_coach(session: Session, text: str, coach: Coach | None) -> dict:
    if coach is None:
        return _game("The Coach is offline right now. Commands still work: /help")
    if session.coach_calls >= MAX_COACH_CALLS:
        return _game("The Coach needs a break. Commands still work: /help")
    session.coach_calls += 1
    # Only free-text chat and Coach replies. Commands and game replies are excluded because
    # /inspect output and flag results would hand the Coach the evidence and the answers.
    history = [
        m for m in session.history
        if m["role"] == "coach" or (m["role"] == "player" and not m["text"].startswith("/"))
    ][-COACH_HISTORY:]
    try:
        reply = coach(coach_context(load_level(session.level)), history, text)
    except Exception:
        return _game("The Coach couldn't answer just now. Try again, or use /hint.")
    return {"role": "coach", "text": reply}
