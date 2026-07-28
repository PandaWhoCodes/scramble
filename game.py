"""Game logic for Scramble.

The core move: for each color team independently, take the round's answer,
shuffle its letters, and deal them round-robin across that team's members —
so per-member letter counts differ by at most 1. If a team has more people
than the answer has letters, the leftover members become Solvers (no letters;
they see the word shape and direct the arranging).

Snapshots are computed once per round activation and stored in the DB, so
Next/Back replays the exact same deal and a mid-round refresh can't reshuffle
anyone's letters.
"""

import random
import re
import time

PALETTE = [
    {"name": "Flame", "hex": "#FF3B30", "fg": "#FFFFFF"},
    {"name": "Sky", "hex": "#0A84FF", "fg": "#FFFFFF"},
    {"name": "Leaf", "hex": "#2ECC71", "fg": "#06341B"},
    {"name": "Sun", "hex": "#FFD60A", "fg": "#3D2F00"},
    {"name": "Grape", "hex": "#AF52DE", "fg": "#FFFFFF"},
    {"name": "Tangerine", "hex": "#FF9500", "fg": "#3D2400"},
    {"name": "Punch", "hex": "#FF2D92", "fg": "#FFFFFF"},
    {"name": "Teal", "hex": "#00C7BE", "fg": "#00332F"},
    {"name": "Indigo", "hex": "#5856D6", "fg": "#FFFFFF"},
    {"name": "Lime", "hex": "#BEF264", "fg": "#1A2E05"},
    {"name": "Ocean", "hex": "#32ADE6", "fg": "#062A3A"},
    {"name": "Cocoa", "hex": "#A2845E", "fg": "#FFFFFF"},
]
MAX_TEAMS = len(PALETTE)

NAME_MAX = 30
ANSWER_MAX = 40  # characters per answer, spaces included
MAX_ANSWERS = 60


def clean_text(s, mx):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:mx]


def clean_answer(s):
    """Answers keep only letters, digits and single spaces."""
    s = re.sub(r"[^A-Za-z0-9 ]+", "", str(s or ""))
    return clean_text(s, ANSWER_MAX)


def answer_letters(answer):
    return [ch for ch in answer.upper() if ch.isalnum()]


def shape(answer):
    """Word lengths, e.g. 'open source' -> [4, 6]."""
    return [len([c for c in w if c.isalnum()]) for w in answer.split() if any(c.isalnum() for c in w)]


def deal_colors(players, team_count):
    """Shuffle players and deal color_idx round-robin -> {pid: color_idx}."""
    pids = [p["pid"] for p in players]
    random.shuffle(pids)
    return {pid: i % team_count for i, pid in enumerate(pids)}


def smallest_team(players, team_count):
    """Team index with the fewest members (for late joiners)."""
    counts = [0] * team_count
    for p in players:
        if 0 <= p["color_idx"] < team_count:
            counts[p["color_idx"]] += 1
    return counts.index(min(counts))


def _contiguous_pieces(letters, k):
    """Split an ordered letter list into k contiguous pieces, sizes ±1."""
    n = len(letters)
    base, extra = divmod(n, k)
    pieces, i = [], 0
    for j in range(k):
        size = base + (1 if j < extra else 0)
        pieces.append("".join(letters[i : i + size]))
        i += size
    return pieces


def make_snapshot(idx, answer, players):
    """Deal the answer as contiguous, IN-ORDER pieces — one piece per phone.

    The jumble lives BETWEEN phones (which piece lands on which phone), never
    inside one: a phone's letters always read in answer order, so the team
    solves by physically arranging their phones, not by staring at their own
    screen.

    - Team of n on an L-letter answer: min(n, L) members each get one
      contiguous piece (lengths ±1), dealt to a shuffled member order.
      Leftover members are Solvers ([] — no piece, they direct).
    - Solo team (n == 1): one in-order piece would just reveal the answer,
      so that phone gets up to 3 pieces in shuffled order instead — a
      self-contained mini-puzzle.

    assignments[pid] is a list of piece strings: usually one, [] for Solvers,
    several only in the solo fallback.
    """
    letters = answer_letters(answer)
    teams = {}
    for p in players:
        if p["color_idx"] >= 0:
            teams.setdefault(p["color_idx"], []).append(p["pid"])

    assignments = {}
    for _, pids in teams.items():
        order = pids[:]
        random.shuffle(order)  # who gets which piece — this IS the jumble
        if len(order) == 1:
            k = min(3, max(1, len(letters)))
            pieces = _contiguous_pieces(letters, k)
            random.shuffle(pieces)
            assignments[order[0]] = pieces
            continue
        k = min(len(order), len(letters))
        pieces = _contiguous_pieces(letters, k) if k else []
        for i, pid in enumerate(order):
            assignments[pid] = [pieces[i]] if i < k else []

    return {
        "idx": idx,
        "answer": answer,
        "shape": shape(answer),
        "letter_count": len(letters),
        "assignments": assignments,
        "made_at": int(time.time() * 1000),
    }
