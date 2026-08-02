"""Scramble — team letter-unjumble party game. FastAPI backend.

Routes:
  GET  /                    player page (join via QR / room code)
  GET  /host                host console (hidden; protected by the room PIN)
  GET  /api/room            public room info
  POST /api/room            open a room (host sets PIN, optionally answers)
  GET  /api/state?pid=      THE endpoint phones live on — returns everything a
                            phone needs to render its exact spot in the game
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

Architecture:
  Memory-first: all game state lives in Python dicts for instant reads (~µs).
  Writes update memory first, then persist to DB asynchronously in a background
  thread pool — never blocking the request. The DB is the recovery source on
  restart, not the hot path. ETag headers let clients skip re-downloading
  unchanged payloads (304 Not Modified). GZip compresses everything.

  Single async worker: with memory-first reads and non-blocking writes, one
  uvicorn worker handles 1000+ req/s. Multiple workers would need shared state
  (Redis, etc.) — unnecessary at this scale.
"""

import hashlib
import hmac
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import qrcode
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

try:  # optional .env for local dev
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from concurrent.futures import ProcessPoolExecutor

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
app.add_middleware(GZipMiddleware, minimum_size=200)

store = Store()

def _db_worker_init():
    global _worker_store
    from db import Store
    _worker_store = Store()
    _worker_store.connect()

def _db_worker_task(fn_name, *args):
    try:
        getattr(_worker_store, fn_name)(*args)
    except Exception as e:
        print(f"[worker] DB error on {fn_name}: {e}")

# Background process pool for DB writes — never blocks the FastAPI GIL
_db_pool = ProcessPoolExecutor(max_workers=1, initializer=_db_worker_init)

_TEMPLATES = Path(__file__).parent / "templates"
INDEX_HTML = (_TEMPLATES / "index.html").read_text(encoding="utf-8")
HOST_HTML = (_TEMPLATES / "host.html").read_text(encoding="utf-8")

# ─────────────────────────────────────────────────────── helpers

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def gen_code(n=5):
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


def hash_pin(pin: str) -> str:
    return hashlib.sha256(("scramble::" + (pin or "")).encode()).hexdigest()


def _join_url(request: Request, room: dict) -> str:
    base = os.getenv("BASE_URL") or str(request.base_url).rstrip("/")
    return f"{base}/?c={room['join_code']}"


# ─────────────────────────────────────── in-memory state (the hot path)
# Every read comes from here — instant dict lookups, zero DB, zero locks.
# Every write updates here first, then fires a background DB persist.

_mem = {
    "room": None,       # dict matching get_room() shape, or None
    "players": [],      # [{"pid","name","ready","color_idx"}, ...]
    "answers": [],      # ["LOVE", "HERO", ...]
    "rounds": {},       # {0: snapshot, 1: snapshot, ...}
    "version": 0,       # monotonic; bumped on every mutation, used for ETags
    "kicked_pids": set(), # pids that have been removed by the host
}


def _bump():
    """Increment version counter. Clients with stale ETags get fresh data."""
    _mem["version"] += 1


def _persist(fn, *args):
    """Fire-and-forget DB write. Runs in a background process, never blocks
    the event loop or any request handler."""
    try:
        _db_pool.submit(_db_worker_task, fn.__name__, *args)
    except Exception as e:
        print(f"[persist] submit error: {e}")


def _find_player(pid: str):
    return next((p for p in _mem["players"] if p["pid"] == pid), None)


def _require_room():
    if not _mem["room"]:
        raise HTTPException(404, "No open room.")
    return _mem["room"]


def _require_host(pin: str | None, room: dict):
    if not hmac.compare_digest(hash_pin(pin or ""), room["pin_hash"]):
        raise HTTPException(401, "Wrong host PIN.")


def _etag_response(request: Request, data: dict, extra_headers: dict | None = None):
    """Build a JSONResponse with ETag. Returns 304 if the client already has
    the current version."""
    etag = f'W/"{_mem["version"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    hdrs = {"ETag": etag, "Cache-Control": "no-cache"}
    if extra_headers:
        hdrs.update(extra_headers)
    return JSONResponse(data, headers=hdrs)


# ─────────────────────────────────────────────────── startup

@app.on_event("startup")
async def _startup():
    """Hydrate in-memory state from DB once at boot."""
    store.connect()
    room, players, answers, rounds = store.load_all()
    _mem["room"] = room
    _mem["players"] = players
    _mem["answers"] = answers
    _mem["rounds"] = rounds
    _mem["version"] = 0
    print(f"[startup] Loaded: room={'yes' if room else 'no'}, "
          f"players={len(players)}, answers={len(answers)}, rounds={len(rounds)}")


# ─────────────────────────────────────────── rate limiter (venue-safe)

_join_hits: dict[str, list[float]] = {}


def join_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _join_hits.get(ip, []) if now - t < 60]
    # 100 per minute per IP (was 15 — way too low for a venue with shared Wi-Fi)
    if len(hits) >= 100:
        _join_hits[ip] = hits
        return False
    hits.append(now)
    _join_hits[ip] = hits
    return True


# ─────────────────────────────────────────────────── models

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
    team_count: int = Field(ge=1, le=MAX_TEAMS)


class SettingsBody(BaseModel):
    edge_marks: bool
    allow_flips: bool


class RoundBody(BaseModel):
    action: str  # "next" | "back" | "redeal"

class KickBody(BaseModel):
    pid: str


# ─────────────────────────────────────────────────── pages

@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/host", response_class=HTMLResponse)
async def host_page():
    return HOST_HTML


# ─────────────────────────────────────────────────── player API

@app.get("/api/room")
async def room_info():
    room = _mem["room"]
    if not room:
        return {"exists": False}
    return {"exists": True, "phase": room["phase"]}


@app.post("/api/room")
async def open_room(body: OpenRoomBody, request: Request):
    if _mem["room"]:
        raise HTTPException(409, "A room is already open. Enter its PIN at /host, or close it first.")
    pin = body.pin.strip()
    if len(pin) < 4:
        raise HTTPException(400, "PIN must be at least 4 characters.")
    session_id = secrets.token_urlsafe(6)
    join_code = gen_code()
    pin_h = hash_pin(pin)
    ts = int(time.time() * 1000)

    # Memory first
    _mem["room"] = {
        "session_id": session_id, "phase": "lobby", "join_code": join_code,
        "pin_hash": pin_h, "team_count": 1, "current_round": -1,
        "edge_marks": True, "allow_flips": True, "opened_at": ts,
    }
    _mem["players"] = []
    _mem["rounds"] = {}
    _mem["kicked_pids"] = set()
    answers = [clean_answer(a) for a in body.answers]
    answers = [a for a in answers if a][:MAX_ANSWERS]
    _mem["answers"] = answers
    _bump()

    # Persist in background
    _persist(store.open_room, session_id, join_code, pin_h, ts)
    if answers:
        _persist(store.replace_answers, session_id, answers)

    return {"ok": True, "join_code": join_code, "join_url": _join_url(request, _mem["room"])}


@app.get("/api/state")
async def state(pid: str = "", request: Request = None):
    """Everything one phone needs to render its exact place in the game."""
    room = _mem["room"]
    if not room:
        return JSONResponse({"exists": False}, headers={"Cache-Control": "no-cache"})

    # ETag: if nothing changed since last poll, return 304 (zero bytes)
    if request:
        etag = f'W/"{_mem["version"]}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    if pid in _mem.get("kicked_pids", set()):
        out = {"exists": True, "kicked": True}
        return _etag_response(request, out) if request else JSONResponse(out)

    players = _mem["players"]
    me = next((p for p in players if p["pid"] == pid), None)
    out = {
        "exists": True,
        "session_id": room["session_id"],
        "phase": room["phase"],
        "current_round": room["current_round"],
        "total_rounds": len(_mem["answers"]),
        "counts": {"players": len(players), "ready": sum(1 for p in players if p["ready"])},
        "you": None,
    }
    if not me:
        return _etag_response(request, out) if request else JSONResponse(out)

    you = {"name": me["name"], "ready": me["ready"], "color": None, "team_number": None, "teammates": []}
    if me["color_idx"] >= 0:
        you["color"] = PALETTE[me["color_idx"] % len(PALETTE)]
        you["team_number"] = me["color_idx"] + 1
        you["teammates"] = [
            p["name"] for p in players if p["color_idx"] == me["color_idx"] and p["pid"] != pid
        ]
    out["you"] = you

    snapshot = _mem["rounds"].get(room["current_round"])
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

    return _etag_response(request, out) if request else JSONResponse(out)


@app.post("/api/join")
async def join(body: JoinBody, request: Request):
    room = _require_room()
    if body.code.strip().upper() != room["join_code"]:
        raise HTTPException(403, "Wrong room code. Scan the QR at the venue or check the code on screen.")
    ip = request.client.host if request.client else "?"
    if not join_rate_ok(ip):
        raise HTTPException(429, "Too many joins from this connection — slow down a little.")
    name = clean_text(body.name, NAME_MAX)
    if not name:
        raise HTTPException(400, "Name required.")

    pid = clean_text(body.pid, 64)
    if pid in _mem.get("kicked_pids", set()):
        raise HTTPException(403, "You have been removed from this game by the host.")
    
    ts = int(time.time() * 1000)

    # Memory first
    existing = _find_player(pid)
    if existing:
        existing["name"] = name  # update name, keep ready/color
    else:
        existing = {"pid": pid, "name": name, "ready": False, "color_idx": -1}
        _mem["players"].append(existing)

    # Late joiner while the game is live: slot into the smallest team
    if room["phase"] == "live" and existing["color_idx"] < 0:
        existing["color_idx"] = smallest_team(_mem["players"], room["team_count"])
        _persist(store.set_color, room["session_id"], pid, existing["color_idx"])

    _bump()
    _persist(store.upsert_player, room["session_id"], pid, name, ts)

    return {"ok": True, "pid": pid, "session_id": room["session_id"]}


@app.post("/api/ready")
async def ready(body: ReadyBody):
    room = _require_room()
    if body.code.strip().upper() != room["join_code"]:
        raise HTTPException(403, "Wrong room code.")
    pid = clean_text(body.pid, 64)
    player = _find_player(pid)
    if not player:
        raise HTTPException(404, "Join first.")

    player["ready"] = body.ready
    _bump()
    _persist(store.set_ready, room["session_id"], pid, body.ready)

    return {"ok": True}


@app.get("/api/qr")
async def qr(request: Request, code: str = ""):
    room = _require_room()
    if code.strip().upper() != room["join_code"]:
        raise HTTPException(404, "Unknown code.")
    img = qrcode.make(_join_url(request, room), box_size=10, border=2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


# ─────────────────────────────────────────────────── host API

@app.get("/api/host/state")
async def host_state(request: Request, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)

    # ETag: host sees the same version counter as players
    etag = f'W/"{_mem["version"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    snapshot = (
        _mem["rounds"].get(room["current_round"])
        if room["phase"] == "live" and room["current_round"] >= 0
        else None
    )
    out = {
        "phase": room["phase"],
        "session_id": room["session_id"],
        "join_code": room["join_code"],
        "join_url": _join_url(request, room),
        "team_count": room["team_count"],
        "current_round": room["current_round"],
        "settings": {"edge_marks": room["edge_marks"], "allow_flips": room["allow_flips"]},
        "players": _mem["players"],
        "answers": _mem["answers"],
        "snapshot": snapshot,
        "palette": PALETTE,
        "max_teams": MAX_TEAMS,
        "answer_max": ANSWER_MAX,
    }
    return JSONResponse(out, headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.post("/api/host/answers")
async def set_answers(body: AnswersBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    answers = [clean_answer(a) for a in body.answers]
    answers = [a for a in answers if a][:MAX_ANSWERS]

    _mem["answers"] = answers
    if room["current_round"] >= len(answers):
        room["current_round"] = len(answers) - 1 if answers else -1
        _persist(store.set_current_round, room["current_round"])
    _bump()

    _persist(store.replace_answers, room["session_id"], answers)

    return {"ok": True, "count": len(answers)}


@app.post("/api/host/teams")
async def set_teams(body: TeamsBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    if room["phase"] != "lobby":
        raise HTTPException(409, "Team count can only change in the lobby.")

    room["team_count"] = body.team_count
    _bump()
    _persist(store.set_team_count, body.team_count)

    return {"ok": True}


@app.post("/api/host/settings")
async def set_settings(body: SettingsBody, x_host_pin: str | None = Header(default=None)):
    """Puzzle-mode toggles. Baked into snapshots at deal time, so changes
    apply from the next Next/Re-deal — never mid-round."""
    room = _require_room()
    _require_host(x_host_pin, room)

    room["edge_marks"] = body.edge_marks
    room["allow_flips"] = body.allow_flips
    _bump()
    _persist(store.set_settings, body.edge_marks, body.allow_flips)

    return {"ok": True}


@app.post("/api/host/assign")
async def assign_colors(x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    if room["phase"] != "lobby":
        raise HTTPException(409, "Colors are already assigned.")
    players = _mem["players"]
    if len(players) < 2:
        raise HTTPException(400, "You need at least 2 players.")
    if len(players) < room["team_count"]:
        raise HTTPException(400, f"Fewer players than teams — reduce teams or wait for more players.")

    # Memory first
    color_map = deal_colors(players, room["team_count"])
    for p in players:
        p["color_idx"] = color_map.get(p["pid"], p["color_idx"])
    room["phase"] = "live"
    room["current_round"] = -1
    _bump()

    # Persist in background
    for pid, ci in color_map.items():
        _persist(store.set_color, room["session_id"], pid, ci)
    _persist(store.set_phase, "live")
    _persist(store.set_current_round, -1)

    return {"ok": True}


@app.post("/api/host/round")
async def round_nav(body: RoundBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    if room["phase"] != "live":
        raise HTTPException(409, "Assign colors first.")
    answers = _mem["answers"]
    cur = room["current_round"]

    if body.action == "next":
        nxt = cur + 1
        if nxt >= len(answers):
            raise HTTPException(409, "No more answers — add a few more below.")
        if nxt not in _mem["rounds"]:
            snap = make_snapshot(nxt, answers[nxt], _mem["players"],
                                 room["edge_marks"], room["allow_flips"])
            _mem["rounds"][nxt] = snap
            _persist(store.save_round, room["session_id"], nxt, snap, snap["made_at"])
        room["current_round"] = nxt
        _persist(store.set_current_round, nxt)
    elif body.action == "back":
        room["current_round"] = max(-1, cur - 1)
        _persist(store.set_current_round, room["current_round"])
    elif body.action == "redeal":
        if cur < 0:
            raise HTTPException(400, "No active round to re-deal.")
        snap = make_snapshot(cur, answers[cur], _mem["players"],
                             room["edge_marks"], room["allow_flips"])
        _mem["rounds"][cur] = snap
        _persist(store.save_round, room["session_id"], cur, snap, snap["made_at"])
    else:
        raise HTTPException(400, "Unknown action.")

    _bump()
    return {"ok": True}


@app.post("/api/host/lobby")
async def back_to_lobby(x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)

    room["phase"] = "lobby"
    room["current_round"] = -1
    for p in _mem["players"]:
        p["ready"] = False
        p["color_idx"] = -1
    _mem["rounds"] = {}
    _bump()

    _persist(store.reset_to_lobby)

    return {"ok": True}


@app.post("/api/host/kick")
async def kick_player(body: KickBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)

    player = _find_player(body.pid)
    if player:
        _mem["players"].remove(player)
        if "kicked_pids" not in _mem:
            _mem["kicked_pids"] = set()
        _mem["kicked_pids"].add(body.pid)
        _bump()
        _persist(store.remove_player, room["session_id"], body.pid)

    return {"ok": True}


@app.post("/api/close")
async def close_room(x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)

    _mem["room"] = None
    _mem["players"] = []
    _mem["answers"] = []
    _mem["rounds"] = {}
    _mem["kicked_pids"] = set()
    _bump()

    _persist(store.close_room)

    return {"ok": True}
