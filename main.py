"""Scramble — team letter-unjumble party game. FastAPI backend.

Routes:
  GET  /                    player page (join via QR / room code)
  GET  /host                host console (hidden; protected by the room PIN)
  GET  /api/room            public room info
  POST /api/room            open a room (host sets PIN, optionally answers)
  GET  /api/state?pid=      THE endpoint phones live on (2s cache) — returns
                            everything a phone needs to render its exact spot
  POST /api/join            join / rejoin (requires QR room code)
  POST /api/ready           toggle the ready flag
  GET  /api/qr?code=        QR PNG encoding the join URL
  GET  /api/host/state      full picture: players, answers, current deal (PIN)
  POST /api/host/answers    replace the answer list (PIN)
  POST /api/host/teams      set team count while in lobby (PIN)
  POST /api/host/assign     deal colors, phase -> live (PIN)
  POST /api/host/round      {"action": "next" | "back" | "redeal"} (PIN)
  POST /api/host/lobby      end game, keep crowd, back to lobby (PIN)
  POST /api/close           delete the room entirely (PIN)

Resilience model: the DB is the source of truth. Phones poll /api/state and
render it idempotently, so refreshes and connection drops land players exactly
where the game is. Round deals are snapshotted in the DB at activation, so
Next/Back/refresh always replays identical letters.

Single-instance assumptions (fine on one Fly.io machine): the state cache and
join rate limiter are in-process. Move to Redis if you ever scale out.
"""

import hashlib
import hmac
import os
import secrets
import time
from io import BytesIO
from pathlib import Path

import qrcode
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

try:  # optional .env for local dev
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from db import Store
from game import (
    ANSWER_MAX,
    MAX_ANSWERS,
    MAX_TEAMS,
    NAME_MAX,
    PALETTE,
    clean_answer,
    clean_text,
    deal_colors,
    make_snapshot,
    smallest_team,
)

app = FastAPI(title="Scramble", docs_url=None, redoc_url=None)
store = Store()

_TEMPLATES = Path(__file__).parent / "templates"
INDEX_HTML = (_TEMPLATES / "index.html").read_text(encoding="utf-8")
HOST_HTML = (_TEMPLATES / "host.html").read_text(encoding="utf-8")

# ------------------------------------------------------------ helpers

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def gen_code(n=5):
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


def hash_pin(pin: str) -> str:
    return hashlib.sha256(("scramble::" + (pin or "")).encode()).hexdigest()


def require_room():
    room = store.get_room()
    if not room:
        raise HTTPException(404, "No open room.")
    return room


def require_host(pin: str | None, room: dict):
    if not hmac.compare_digest(hash_pin(pin or ""), room["pin_hash"]):
        raise HTTPException(401, "Wrong host PIN.")


def join_url(request: Request, room: dict) -> str:
    base = os.getenv("BASE_URL") or str(request.base_url).rstrip("/")
    return f"{base}/?c={room['join_code']}"


# In-process cache for the hot /api/state path. Everything a phone needs is
# derived from these three reads; 100 phones polling every ~3s become ~1 DB
# round-trip per 2 seconds.
_cache = {"t": 0.0, "room": None, "players": None, "snapshot": None, "n_answers": 0}
_CACHE_TTL = 2.0


def cached_state():
    now = time.time()
    if now - _cache["t"] > _CACHE_TTL:
        room = store.get_room()
        players, snapshot, n_answers = None, None, 0
        if room:
            players = store.list_players(room["session_id"])
            n_answers = len(store.list_answers(room["session_id"]))
            if room["phase"] == "live" and room["current_round"] >= 0:
                snapshot = store.get_round(room["session_id"], room["current_round"])
        _cache.update(t=now, room=room, players=players, snapshot=snapshot, n_answers=n_answers)
    return _cache["room"], _cache["players"], _cache["snapshot"], _cache["n_answers"]


def bust_cache():
    _cache["t"] = 0.0


_join_hits: dict[str, list[float]] = {}


def join_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _join_hits.get(ip, []) if now - t < 60]
    if len(hits) >= 15:
        _join_hits[ip] = hits
        return False
    hits.append(now)
    _join_hits[ip] = hits
    return True


# ------------------------------------------------------------- models


class OpenRoomBody(BaseModel):
    pin: str = Field(min_length=4, max_length=64)
    answers: list[str] = Field(default_factory=list, max_length=MAX_ANSWERS)


class JoinBody(BaseModel):
    code: str = Field(max_length=16)
    pid: str = Field(min_length=4, max_length=64)
    name: str = Field(max_length=NAME_MAX * 2)


class ReadyBody(BaseModel):
    code: str = Field(max_length=16)
    pid: str = Field(max_length=64)
    ready: bool


class AnswersBody(BaseModel):
    answers: list[str] = Field(max_length=MAX_ANSWERS)


class TeamsBody(BaseModel):
    team_count: int = Field(ge=2, le=MAX_TEAMS)


class SettingsBody(BaseModel):
    edge_marks: bool
    allow_flips: bool


class RoundBody(BaseModel):
    action: str  # "next" | "back" | "redeal"


# -------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/host", response_class=HTMLResponse)
def host_page():
    return HOST_HTML


# --------------------------------------------------------- player API


@app.get("/api/room")
def room_info():
    room, _, _, _ = cached_state()
    if not room:
        return {"exists": False}
    return {"exists": True, "phase": room["phase"]}


@app.post("/api/room")
def open_room(body: OpenRoomBody, request: Request):
    if store.get_room():
        raise HTTPException(409, "A room is already open. Enter its PIN at /host, or close it first.")
    pin = body.pin.strip()
    if len(pin) < 4:
        raise HTTPException(400, "PIN must be at least 4 characters.")
    session_id = secrets.token_urlsafe(6)
    store.open_room(session_id, gen_code(), hash_pin(pin), int(time.time() * 1000))
    answers = [clean_answer(a) for a in body.answers]
    answers = [a for a in answers if a][:MAX_ANSWERS]
    if answers:
        store.replace_answers(session_id, answers)
    bust_cache()
    room = store.get_room()
    return {"ok": True, "join_code": room["join_code"], "join_url": join_url(request, room)}


@app.get("/api/state")
def state(pid: str = ""):
    """Everything one phone needs to render its exact place in the game."""
    room, players, snapshot, n_answers = cached_state()
    if not room:
        return {"exists": False}

    players = players or []
    me = next((p for p in players if p["pid"] == pid), None)
    out = {
        "exists": True,
        "session_id": room["session_id"],
        "phase": room["phase"],
        "current_round": room["current_round"],
        "total_rounds": n_answers,
        "counts": {"players": len(players), "ready": sum(1 for p in players if p["ready"])},
        "you": None,
    }
    if not me:
        return out  # client auto-rejoins with its stored name + code

    you = {"name": me["name"], "ready": me["ready"], "color": None, "team_number": None, "teammates": []}
    if me["color_idx"] >= 0:
        you["color"] = PALETTE[me["color_idx"] % len(PALETTE)]
        you["team_number"] = me["color_idx"] + 1
        you["teammates"] = [
            p["name"] for p in players if p["color_idx"] == me["color_idx"] and p["pid"] != pid
        ]
    out["you"] = you

    if room["phase"] == "live" and snapshot and room["current_round"] == snapshot["idx"]:
        a = snapshot["assignments"].get(pid)  # None -> joined after the deal
        rnd = {
            "idx": snapshot["idx"],
            "mode": snapshot.get("mode", "pieces"),
            "shape": snapshot["shape"],
            "letter_count": snapshot["letter_count"],
            "in_round": a is not None,
        }
        if snapshot.get("mode") == "puzzle":
            if a is None or a.get("solver"):
                rnd["solver"] = a is not None
            else:
                g = snapshot["teams"][str(me["color_idx"])]["groups"][a["g"]]
                rnd["solver"] = False
                rnd["tile"] = {
                    "text": g["text"],
                    "w": g["w"],
                    "h": g["h"],
                    "vb": a["vb"],
                    "rot": a["rot"],
                    "marks": g["marks"],
                }
        else:
            rnd["pieces"] = a
        out["round"] = rnd
    else:
        out["round"] = None
    return out


@app.post("/api/join")
def join(body: JoinBody, request: Request):
    room = require_room()
    if body.code.strip().upper() != room["join_code"]:
        raise HTTPException(403, "Wrong room code. Scan the QR at the venue or check the code on screen.")
    ip = request.client.host if request.client else "?"
    if not join_rate_ok(ip):
        raise HTTPException(429, "Too many joins from this connection — slow down a little.")
    name = clean_text(body.name, NAME_MAX)
    if not name:
        raise HTTPException(400, "Name required.")

    pid = clean_text(body.pid, 64)
    store.upsert_player(room["session_id"], pid, name, int(time.time() * 1000))

    # Late joiner while the game is live: slot into the smallest team now;
    # they get letters when the next round is dealt.
    me = store.get_player(room["session_id"], pid)
    if room["phase"] == "live" and me["color_idx"] < 0:
        store.set_color(room["session_id"], pid, smallest_team(store.list_players(room["session_id"]), room["team_count"]))
    bust_cache()
    return {"ok": True, "pid": pid, "session_id": room["session_id"]}


@app.post("/api/ready")
def ready(body: ReadyBody):
    room = require_room()
    if body.code.strip().upper() != room["join_code"]:
        raise HTTPException(403, "Wrong room code.")
    if not store.get_player(room["session_id"], clean_text(body.pid, 64)):
        raise HTTPException(404, "Join first.")
    store.set_ready(room["session_id"], clean_text(body.pid, 64), body.ready)
    bust_cache()
    return {"ok": True}


@app.get("/api/qr")
def qr(request: Request, code: str = ""):
    room = require_room()
    if code.strip().upper() != room["join_code"]:
        raise HTTPException(404, "Unknown code.")
    img = qrcode.make(join_url(request, room), box_size=10, border=2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


# ----------------------------------------------------------- host API


@app.get("/api/host/state")
def host_state(request: Request, x_host_pin: str | None = Header(default=None)):
    room = require_room()
    require_host(x_host_pin, room)
    players = store.list_players(room["session_id"])
    answers = store.list_answers(room["session_id"])
    snapshot = (
        store.get_round(room["session_id"], room["current_round"])
        if room["phase"] == "live" and room["current_round"] >= 0
        else None
    )
    return {
        "phase": room["phase"],
        "session_id": room["session_id"],
        "join_code": room["join_code"],
        "join_url": join_url(request, room),
        "team_count": room["team_count"],
        "current_round": room["current_round"],
        "settings": {"edge_marks": room["edge_marks"], "allow_flips": room["allow_flips"]},
        "players": players,
        "answers": answers,
        "snapshot": snapshot,
        "palette": PALETTE,
        "max_teams": MAX_TEAMS,
        "answer_max": ANSWER_MAX,
    }


@app.post("/api/host/answers")
def set_answers(body: AnswersBody, x_host_pin: str | None = Header(default=None)):
    room = require_room()
    require_host(x_host_pin, room)
    answers = [clean_answer(a) for a in body.answers]
    answers = [a for a in answers if a][:MAX_ANSWERS]
    store.replace_answers(room["session_id"], answers)
    if room["current_round"] >= len(answers):
        store.set_current_round(len(answers) - 1 if answers else -1)
    bust_cache()
    return {"ok": True, "count": len(answers)}


@app.post("/api/host/teams")
def set_teams(body: TeamsBody, x_host_pin: str | None = Header(default=None)):
    room = require_room()
    require_host(x_host_pin, room)
    if room["phase"] != "lobby":
        raise HTTPException(409, "Team count can only change in the lobby.")
    store.set_team_count(body.team_count)
    bust_cache()
    return {"ok": True}


@app.post("/api/host/settings")
def set_settings(body: SettingsBody, x_host_pin: str | None = Header(default=None)):
    """Puzzle-mode toggles. Baked into snapshots at deal time, so changes
    apply from the next Next/Re-deal — never mid-round."""
    room = require_room()
    require_host(x_host_pin, room)
    store.set_settings(body.edge_marks, body.allow_flips)
    bust_cache()
    return {"ok": True}


@app.post("/api/host/assign")
def assign_colors(x_host_pin: str | None = Header(default=None)):
    room = require_room()
    require_host(x_host_pin, room)
    if room["phase"] != "lobby":
        raise HTTPException(409, "Colors are already assigned.")
    players = store.list_players(room["session_id"])
    if len(players) < 2:
        raise HTTPException(400, "You need at least 2 players.")
    if len(players) < room["team_count"]:
        raise HTTPException(400, f"Fewer players than teams — reduce teams or wait for more players.")
    for pid, ci in deal_colors(players, room["team_count"]).items():
        store.set_color(room["session_id"], pid, ci)
    store.set_phase("live")
    store.set_current_round(-1)
    bust_cache()
    return {"ok": True}


@app.post("/api/host/round")
def round_nav(body: RoundBody, x_host_pin: str | None = Header(default=None)):
    room = require_room()
    require_host(x_host_pin, room)
    if room["phase"] != "live":
        raise HTTPException(409, "Assign colors first.")
    answers = store.list_answers(room["session_id"])
    cur = room["current_round"]

    if body.action == "next":
        nxt = cur + 1
        if nxt >= len(answers):
            raise HTTPException(409, "No more answers — add a few more below.")
        if not store.get_round(room["session_id"], nxt):
            snap = make_snapshot(nxt, answers[nxt], store.list_players(room["session_id"]),
                                 room["edge_marks"], room["allow_flips"])
            store.save_round(room["session_id"], nxt, snap, snap["made_at"])
        store.set_current_round(nxt)
    elif body.action == "back":
        store.set_current_round(max(-1, cur - 1))
    elif body.action == "redeal":
        if cur < 0:
            raise HTTPException(400, "No active round to re-deal.")
        snap = make_snapshot(cur, answers[cur], store.list_players(room["session_id"]),
                             room["edge_marks"], room["allow_flips"])
        store.save_round(room["session_id"], cur, snap, snap["made_at"])
    else:
        raise HTTPException(400, "Unknown action.")
    bust_cache()
    return {"ok": True}


@app.post("/api/host/lobby")
def back_to_lobby(x_host_pin: str | None = Header(default=None)):
    room = require_room()
    require_host(x_host_pin, room)
    store.reset_to_lobby()
    bust_cache()
    return {"ok": True}


@app.post("/api/close")
def close_room(x_host_pin: str | None = Header(default=None)):
    room = require_room()
    require_host(x_host_pin, room)
    store.close_room()
    bust_cache()
    return {"ok": True}
