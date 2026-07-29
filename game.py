"""Game logic for Scramble — two dealing modes.

PIECES mode (answers of 5+ letters): each team member gets a contiguous,
in-order text piece of the answer; the jumble is which piece lands on which
phone. Extra members become Solvers.

PUZZLE mode (answers of 4 letters or fewer): the rendered word itself is
sliced like a jigsaw. Each team independently gets the best split plan for
its size — a rows×cols grid of the word canvas, optionally as letter groups
("EV" as 2×2 + "E" solo) — and every phone receives a crop rectangle plus a
rotation (portrait/landscape/upside-down, legality driven by the crop's
aspect ratio). Phones physically assemble to form the word. Optional
edge-match marks (half-dots that complete across a seam) confirm correct
placement + orientation.

Snapshots are computed once per round activation and stored in the DB, so
Next/Back replays the exact same deal and a mid-round refresh can't reshuffle
anyone's piece, tile, rotation, or solver role. Host settings (edge marks,
upside-down) are baked into each snapshot at deal time.
"""

import math
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

# --- puzzle mode geometry (canvas units) ---
PUZZLE_MAX_LETTERS = 4       # answers this short deal as jigsaw tiles
LU, LH = 100, 130            # per-letter cell width, canvas height
_ASPECT_P = 108 / 234        # portrait phone screen aspect
_ASPECT_L = 234 / 108
MARKC = ["#FFD60A", "#FF2D92", "#32ADE6", "#BEF264", "#FF9500", "#AF52DE", "#00C7BE", "#FF3B30"]


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


def make_snapshot(idx, answer, players, edge_marks=True, allow_flips=True):
    """Dispatch on answer length: PUZZLE mode for short answers (<= 4
    letters), PIECES mode otherwise. Settings are baked into the snapshot so
    a stored round replays identically regardless of later toggle changes."""
    if len(answer_letters(answer)) <= PUZZLE_MAX_LETTERS:
        return _puzzle_snapshot(idx, answer, players, edge_marks, allow_flips)
    return _pieces_snapshot(idx, answer, players)


def _pieces_snapshot(idx, answer, players):
    """Deal the answer as contiguous, IN-ORDER text pieces — one per phone.

    The jumble lives BETWEEN phones (which piece lands on which phone), never
    inside one. Team of n on an L-letter answer: min(n, L) members each get
    one contiguous piece (lengths ±1), dealt to a shuffled member order;
    leftovers are Solvers ([]). Solo teams get up to 3 shuffled pieces.
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
        "mode": "pieces",
        "answer": answer,
        "shape": shape(answer),
        "letter_count": len(letters),
        "assignments": assignments,
        "made_at": int(time.time() * 1000),
    }


# ============================ puzzle mode ============================
# The word is drawn once on a canvas (per-letter cells of LU x LH units,
# textLength-pinned so every phone reproduces identical geometry) and each
# tile is a viewBox crop of that canvas. Server picks the plan and the
# rotations; phones just render.


def _grids(m):
    """Factor pairs (rows, cols) with r*c == m, each side <= 4."""
    return [(r, c) for r in range(1, 5) for c in range(1, 5) if r * c == m]


def _compositions(n, k):
    """Ordered splits of n into k positive parts (small n only)."""
    if k == 1:
        return [[n]] if n >= 1 else []
    out = []
    for f in range(1, n - k + 2):
        for rest in _compositions(n - f, k - 1):
            out.append([f] + rest)
    return out


def _plan_score(plan):
    s = plan["solvers"] * 2 + (len(plan["groups"]) - 1) * 0.35
    for g in plan["groups"]:
        a = (len(g["text"]) * LU / g["cols"]) / (LH / g["rows"])
        s += min(abs(math.log(a / _ASPECT_L)), abs(math.log(a / _ASPECT_P)))
        if len(plan["groups"]) == 1 and g["cols"] == 1 and g["rows"] > 1:
            s -= 0.5  # the "stacked wide strips" aesthetic reads best
    return s


def _best_plan(letters_str, n):
    """Rank split plans for one team of n phones; return the winner.

    Single-group plans slice the whole word rows x cols (allowing a few
    solvers); mixed plans split the letters into contiguous groups, each with
    its own grid, using every phone. Scored by phone-friendliness of tile
    aspect ratios, solver count, and simplicity — same ranking as the lab.
    """
    out = []
    for m in range(n, max(0, n - 4), -1):
        for r, c in _grids(m):
            out.append({"groups": [{"text": letters_str, "rows": r, "cols": c}], "solvers": n - m})
    L = len(letters_str)
    if L >= 2 and n >= 2:
        for k in range(2, L + 1):
            for part in _compositions(L, k):
                texts, i = [], 0
                for ln in part:
                    texts.append(letters_str[i : i + ln])
                    i += ln
                for comp in _compositions(n, len(part)):
                    gs, ok = [], True
                    for m, txt in zip(comp, texts):
                        g = _grids(m)
                        if not g:
                            ok = False
                            break
                        gs.append({"text": txt, "rows": g[0][0], "cols": g[0][1]})
                    if ok:
                        out.append({"groups": gs, "solvers": 0})
    for p in out:
        p["score"] = _plan_score(p)
    out.sort(key=lambda p: p["score"])
    return out[0]


def _legal_rots(w, h, allow_flips):
    """Aspect decides orientation: wide tiles need landscape phones, tall
    tiles portrait, near-square anything. Flips add 180/270."""
    a = w / h
    rots = [90, 270] if a > 1.15 else [0, 180] if a < 0.87 else [0, 90, 180, 270]
    if not allow_flips:
        rots = [r for r in rots if r in (0, 90)]
    return rots


def _puzzle_snapshot(idx, answer, players, edge_marks, allow_flips):
    letters_str = "".join(answer_letters(answer))
    teams = {}
    for p in players:
        if p["color_idx"] >= 0:
            teams.setdefault(p["color_idx"], []).append(p["pid"])

    assignments, team_plans = {}, {}
    for ci, pids in teams.items():
        plan = _best_plan(letters_str, len(pids))
        groups, tiles, mark_i = [], [], 0
        for gi, g in enumerate(plan["groups"]):
            W = len(g["text"]) * LU
            tw, th = W / g["cols"], LH / g["rows"]
            marks = []
            if edge_marks:
                for r in range(g["rows"]):          # vertical cut lines
                    for c in range(g["cols"] - 1):
                        marks.append({"x": (c + 1) * tw, "y": (r + 0.5) * th, "c": MARKC[mark_i % len(MARKC)]})
                        mark_i += 1
                for r in range(g["rows"] - 1):      # horizontal cut lines
                    for c in range(g["cols"]):
                        marks.append({"x": (c + 0.5) * tw, "y": (r + 1) * th, "c": MARKC[mark_i % len(MARKC)]})
                        mark_i += 1
            groups.append({"text": g["text"], "rows": g["rows"], "cols": g["cols"], "w": W, "h": LH, "marks": marks})
            for r in range(g["rows"]):
                for c in range(g["cols"]):
                    tiles.append({
                        "g": gi,
                        "rc": [r, c],
                        "vb": [round(c * tw, 2), round(r * th, 2), round(tw, 2), round(th, 2)],
                        "rot": random.choice(_legal_rots(tw, th, allow_flips)),
                    })
        order = pids[:]
        random.shuffle(order)  # random tile owners AND a random solver
        for i, pid in enumerate(order):
            assignments[pid] = tiles[i] if i < len(tiles) else {"solver": True}
        team_plans[str(ci)] = {"groups": groups}

    return {
        "idx": idx,
        "mode": "puzzle",
        "answer": answer,
        "shape": shape(answer),
        "letter_count": len(letters_str),
        "teams": team_plans,
        "assignments": assignments,
        "settings": {"edge_marks": bool(edge_marks), "allow_flips": bool(allow_flips)},
        "made_at": int(time.time() * 1000),
    }
