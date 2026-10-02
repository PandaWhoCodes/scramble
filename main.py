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

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Literal

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

import httpx

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
    normalize_guess,
    smallest_team,
)

app = FastAPI(title="Scramble", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=200)

# ─────────────────────────────────── real-time notification config
_RT_URL = os.getenv("REALTIME_WORKER_URL", "").rstrip("/")
_RT_SECRET = os.getenv("NOTIFY_SECRET", "")
_rt_client: httpx.AsyncClient | None = None

store = Store()

# Background thread pool for DB writes — never blocks the FastAPI event loop
_db_pool = ThreadPoolExecutor(max_workers=1)

_TEMPLATES = Path(__file__).parent / "templates"
INDEX_HTML = (_TEMPLATES / "index.html").read_text(encoding="utf-8")
HOST_HTML = (_TEMPLATES / "host.html").read_text(encoding="utf-8")
PROJECTOR_HTML = (_TEMPLATES / "projector.html").read_text(encoding="utf-8")

# ─────────────────────────────────────────────────────── helpers

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def gen_code(n=5):
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


def hash_pin(pin: str) -> str:
    return hashlib.sha256(("scramble::" + (pin or "")).encode()).hexdigest()


def _join_url(request: Request, room: dict) -> str:
    base = os.getenv("BASE_URL")
    if not base:
        host = request.headers.get("host", "").lower() if request else ""
        if "scrambles.fly.dev" in host:
            base = "https://scrambles.fly.dev"
        elif request:
            base = str(request.base_url).rstrip("/")
        else:
            base = "https://scrambles.fly.dev"
    return f"{base.rstrip('/')}/?c={room['join_code']}"


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
    "guess_cooldowns": {}, # {pid: last_guess_timestamp} for buzzer rate limit
}


def _bump():
    """Increment version counter. Clients with stale ETags get fresh data."""
    _mem["version"] += 1


def _persist(fn, *args):
    """Fire-and-forget DB write. Runs in a background thread, never blocks
    the event loop or any request handler."""
    try:
        _db_pool.submit(fn, *args)
    except Exception as e:
        print(f"[persist] submit error: {e}")


def _notify(event_type: str, **data):
    """Fire-and-forget real-time notification to the Cloudflare Worker.
    Non-blocking: schedules an async task that POSTs a tiny event to the
    Worker's /notify/:gameId endpoint. If the Worker is down or not
    configured, this is silently ignored — clients fall back to polling."""
    if not _RT_URL or not _rt_client:
        return
    room = _mem["room"]
    if not room:
        return
    game_id = room["session_id"]
    payload = {"type": event_type, **data}

    async def _send():
        try:
            await _rt_client.post(
                f"{_RT_URL}/notify/{game_id}",
                json=payload,
                headers={"Authorization": f"Bearer {_RT_SECRET}"},
                timeout=5.0,
            )
        except Exception as e:
            print(f"[realtime] notify error ({event_type}): {e}")

    try:
        asyncio.get_event_loop().create_task(_send())
    except RuntimeError:
        pass  # no event loop — shouldn't happen in FastAPI but be safe


def _find_player(pid: str):
    return next((p for p in _mem["players"] if p["pid"] == pid), None)


def _require_room():
    if not _mem["room"]:
        raise HTTPException(404, "No open room.")
    return _mem["room"]


def _require_host(pin: str | None, room: dict):
    if not hmac.compare_digest(hash_pin(pin or ""), room["pin_hash"]):
        raise HTTPException(401, "Wrong host PIN.")


def _redeal_active_round_if_live(room: dict):
    """Re-deal the current active round's pieces across current player rosters if the round is live."""
    cur = room.get("current_round", -1)
    answers = _mem.get("answers", [])
    if room.get("phase") == "live" and 0 <= cur < len(answers):
        snap = make_snapshot(
            cur,
            answers[cur],
            _mem["players"],
            room.get("edge_marks", True),
            room.get("allow_flips", True),
        )
        _mem["rounds"][cur] = snap
        _persist(store.save_round, room["session_id"], cur, snap, snap["made_at"])
        _notify("REDEAL", round=cur)


def _calc_winner_summary(room: dict) -> dict | None:
    if not room or room.get("phase") != "finished":
        return None
    scores = room.get("scores", {})
    custom_teams = room.get("custom_teams", [])
    players = _mem.get("players", [])
    team_count = room.get("team_count", 1)

    team_indices = set()
    for ct in custom_teams:
        team_indices.add(ct["color_idx"])
    for p in players:
        if p["color_idx"] >= 0:
            team_indices.add(p["color_idx"])
    for i in range(team_count):
        team_indices.add(i)

    team_list = []
    for ci in sorted(team_indices):
        ct = next((t for t in custom_teams if t["color_idx"] == ci), None)
        palette_item = PALETTE[ci % len(PALETTE)]
        name = ct["name"] if ct else ("Team " + str(ci + 1))
        hex_col = ct.get("hex", palette_item["hex"]) if ct else palette_item["hex"]
        fg_col = ct.get("fg", palette_item.get("fg", "#ffffff")) if ct else palette_item.get("fg", "#ffffff")
        pts = int(scores.get(str(ci), 0))
        members = [p["name"] for p in players if p["color_idx"] == ci]
        team_list.append({
            "idx": ci,
            "color_idx": ci,
            "name": name,
            "hex": hex_col,
            "fg": fg_col,
            "score": pts,
            "members": members,
        })

    team_list.sort(key=lambda t: (t["score"], len(t["members"])), reverse=True)
    top_score = team_list[0]["score"] if team_list else 0
    winners = [t for t in team_list if t["score"] == top_score] if team_list else []
    is_tie = len(winners) > 1

    return {
        "top_score": top_score,
        "is_tie": is_tie,
        "winners": winners,
        "leaderboard": team_list,
    }


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
    global _rt_client
    store.connect()
    room, players, answers, rounds = store.load_all()
    _mem["room"] = room
    _mem["players"] = players
    _mem["answers"] = answers
    _mem["rounds"] = rounds
    _mem["version"] = 0
    if _RT_URL:
        _rt_client = httpx.AsyncClient()
        print(f"[startup] Real-time enabled: {_RT_URL}")
    print(f"[startup] Loaded: room={'yes' if room else 'no'}, "
          f"players={len(players)}, answers={len(answers)}, rounds={len(rounds)}")


@app.on_event("shutdown")
async def _shutdown():
    global _rt_client
    if _rt_client:
        await _rt_client.aclose()
        _rt_client = None


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
    game_mode: Literal["classic", "competitive"] = "classic"


class ModeBody(BaseModel):
    mode: Literal["classic", "competitive"]


class TeamSelectBody(BaseModel):
    code: str = Field(max_length=16)
    pid: str = Field(min_length=4, max_length=64)
    color_idx: int = Field(ge=-1, le=MAX_TEAMS - 1)


class CreateTeamBody(BaseModel):
    code: str = Field(max_length=16)
    pid: str = Field(min_length=4, max_length=64)
    name: str = Field(min_length=1, max_length=30)


class TeamLimitBody(BaseModel):
    max_team_members: int = Field(ge=1, le=20)


class SubmitAnswerBody(BaseModel):
    code: str = Field(max_length=16)
    pid: str = Field(min_length=4, max_length=64)
    guess: str = Field(max_length=100)


class ScoreAdjustBody(BaseModel):
    color_idx: int = Field(ge=0, le=MAX_TEAMS - 1)
    delta: int | None = None
    score: int | None = None


class JoinBody(BaseModel):
    code: str = Field(max_length=16)
    pid: str = Field(min_length=4, max_length=64)
    name: str = Field(max_length=NAME_MAX * 2)


class FitDims(BaseModel):
    w: int = Field(ge=100, le=4000)
    h: int = Field(ge=100, le=4000)


class ReadyBody(BaseModel):
    code: str = Field(max_length=16)
    pid: str = Field(max_length=64)
    ready: bool
    fit: FitDims | None = None


class FitBody(BaseModel):
    code: str
    pid: str
    w: int = Field(ge=100, le=4000)
    h: int = Field(ge=100, le=4000)


class AnswersBody(BaseModel):
    answers: list[str] = Field(max_length=MAX_ANSWERS)


class TeamsBody(BaseModel):
    team_count: int = Field(ge=1, le=MAX_TEAMS)


class SettingsBody(BaseModel):
    edge_marks: bool
    allow_flips: bool
    piece_orient: Literal["auto", "portrait", "landscape"] = "auto"


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


@app.get("/projector", response_class=HTMLResponse)
async def projector_page():
    return PROJECTOR_HTML


@app.get("/api/projector/state")
async def projector_state(request: Request):
    room = _mem["room"]
    if not room:
        return JSONResponse({"exists": False}, headers={"Cache-Control": "no-cache"})

    etag = f'W/"{_mem["version"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    players = _mem["players"]
    data = {
        "exists": True,
        "session_id": room["session_id"],
        "join_code": room["join_code"],
        "join_url": _join_url(request, room),
        "phase": room["phase"],
        "game_mode": room.get("game_mode", "classic"),
        "scores": room.get("scores", {}),
        "round_winner": room.get("round_winner"),
        "winner_summary": _calc_winner_summary(room),
        "custom_teams": room.get("custom_teams", []),
        "max_team_members": room.get("max_team_members", 4),
        "current_round": room["current_round"],
        "total_rounds": len(_mem["answers"]),
        "team_count": room["team_count"],
        "counts": {
            "players": len(players),
            "ready": sum(1 for p in players if p["ready"]),
        },
        "players": [
            {"name": p["name"], "ready": p["ready"], "color_idx": p["color_idx"]}
            for p in players
        ],
        "palette": PALETTE,
        "ws_url": _RT_URL,
    }
    return _etag_response(request, data)


# ─────────────────────────────────────────────────── player API

@app.get("/api/room")
async def room_info():
    room = _mem["room"]
    if not room:
        return {"exists": False}
    return {"exists": True, "phase": room["phase"], "game_mode": room.get("game_mode", "classic")}


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
        "edge_marks": True, "allow_flips": True, "piece_orient": "auto",
        "game_mode": body.game_mode,
        "scores": {}, "round_winner": None, "allow_team_choice": True,
        "custom_teams": [], "max_team_members": 4,
        "opened_at": ts,
    }
    _mem["players"] = []
    _mem["rounds"] = {}
    _mem["kicked_pids"] = set()
    _mem["guess_cooldowns"] = {}
    answers = [clean_answer(a) for a in body.answers]
    answers = [a for a in answers if a][:MAX_ANSWERS]
    _mem["answers"] = answers
    _bump()

    # Persist in background
    _persist(store.open_room, session_id, join_code, pin_h, ts, body.game_mode)
    if answers:
        _persist(store.replace_answers, session_id, answers)

    return {"ok": True, "join_code": join_code, "join_url": _join_url(request, _mem["room"]),
            "game_mode": body.game_mode, "ws_url": _RT_URL}


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
        "game_mode": room.get("game_mode", "classic"),
        "scores": room.get("scores", {}),
        "round_winner": room.get("round_winner"),
        "winner_summary": _calc_winner_summary(room),
        "allow_team_choice": room.get("allow_team_choice", True),
        "custom_teams": room.get("custom_teams", []),
        "max_team_members": room.get("max_team_members", 4),
        "current_round": room["current_round"],
        "total_rounds": len(_mem["answers"]),
        "team_count": room["team_count"],
        "counts": {"players": len(players), "ready": sum(1 for p in players if p["ready"])},
        "settings": {"piece_orient": room.get("piece_orient", "auto")},
        "palette": PALETTE,
        "players": [{"name": p["name"], "ready": p["ready"], "color_idx": p["color_idx"]} for p in players],
        "you": None,
        "ws_url": _RT_URL or None,
    }
    if not me:
        return _etag_response(request, out) if request else JSONResponse(out)

    you = {"name": me["name"], "ready": me["ready"], "color": None, "team_number": None, "team_name": None, "teammates": []}
    if me["color_idx"] >= 0:
        custom_t = next((t for t in room.get("custom_teams", []) if t["color_idx"] == me["color_idx"]), None)
        color_item = PALETTE[me["color_idx"] % len(PALETTE)]
        you["color"] = color_item
        you["team_number"] = me["color_idx"] + 1
        you["team_name"] = custom_t["name"] if custom_t else color_item["name"]
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
            rnd["fit_scale"] = (snapshot.get("fit_scale") or {}).get(str(me["color_idx"]))
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

    # Late joiner while the game is live: slot into the smallest team (Classic mode only)
    if room["phase"] == "live" and existing["color_idx"] < 0:
        if room.get("game_mode") != "competitive":
            existing["color_idx"] = smallest_team(_mem["players"], room["team_count"])
            _persist(store.set_color, room["session_id"], pid, existing["color_idx"])

    _bump()
    _persist(store.upsert_player, room["session_id"], pid, name, ts)
    _notify("PLAYER_JOINED", pid=pid, name=name)

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
    if body.fit:
        player["fit"] = {"w": body.fit.w, "h": body.fit.h}
    _bump()
    _persist(store.set_ready, room["session_id"], pid, body.ready)
    _notify("PLAYER_READY", pid=pid, ready=body.ready)

    return {"ok": True}


@app.post("/api/fit")
async def fit(body: FitBody):
    room = _require_room()
    if body.code.strip().upper() != room["join_code"]:
        raise HTTPException(403, "Wrong room code.")
    p = _find_player(body.pid)
    if p:
        p["fit"] = {"w": body.w, "h": body.h}
    return {"ok": True}


@app.post("/api/team/create")
async def create_team(body: CreateTeamBody):
    room = _require_room()
    if body.code.strip().upper() != room["join_code"]:
        raise HTTPException(403, "Wrong room code.")
    if room.get("game_mode") != "competitive":
        raise HTTPException(400, "Team creation is only available in competitive mode.")

    name = clean_text(body.name, 24).strip()
    if not name:
        raise HTTPException(400, "Team name cannot be empty.")

    pid = clean_text(body.pid, 64)
    player = _find_player(pid)
    if not player:
        raise HTTPException(404, "Join first.")

    custom_teams = room.setdefault("custom_teams", [])
    if any(t["name"].lower() == name.lower() for t in custom_teams):
        raise HTTPException(400, "A team with that name already exists.")

    if len(custom_teams) >= MAX_TEAMS:
        raise HTTPException(400, f"Maximum number of teams ({MAX_TEAMS}) reached.")

    # Find the first unused color_idx in 0..MAX_TEAMS-1
    used_idxs = {t["color_idx"] for t in custom_teams}
    new_idx = next((i for i in range(MAX_TEAMS) if i not in used_idxs), len(custom_teams))
    color_item = PALETTE[new_idx % len(PALETTE)]

    team_entry = {
        "idx": new_idx,
        "name": name,
        "color_idx": new_idx,
        "hex": color_item["hex"],
        "fg": color_item["fg"],
        "created_by": pid,
    }
    custom_teams.append(team_entry)
    room["team_count"] = max(room["team_count"], len(custom_teams))
    player["color_idx"] = new_idx

    _redeal_active_round_if_live(room)
    _bump()
    _persist(store.set_custom_teams, custom_teams)
    _persist(store.set_team_count, room["team_count"])
    _persist(store.set_color, room["session_id"], pid, new_idx)
    _notify("TEAM_CREATED", team=team_entry, pid=pid)

    return {"ok": True, "team": team_entry, "color_idx": new_idx}


@app.post("/api/team/select")
async def select_team(body: TeamSelectBody):
    room = _require_room()
    if body.code.strip().upper() != room["join_code"]:
        raise HTTPException(403, "Wrong room code.")
    if room.get("game_mode") != "competitive":
        raise HTTPException(400, "Team selection is only available in competitive mode.")

    pid = clean_text(body.pid, 64)
    player = _find_player(pid)
    if not player:
        raise HTTPException(404, "Join first.")

    # Leaving team / unassigning
    if body.color_idx == -1:
        player["color_idx"] = -1
        _redeal_active_round_if_live(room)
        _bump()
        _persist(store.set_color, room["session_id"], pid, -1)
        _notify("PLAYER_TEAM_CHANGED", pid=pid, color_idx=-1)
        return {"ok": True, "color_idx": -1}

    # Validate team existence
    custom_teams = room.get("custom_teams", [])
    if custom_teams:
        if not any(t["color_idx"] == body.color_idx for t in custom_teams):
            raise HTTPException(400, "Selected team does not exist.")
    else:
        if body.color_idx < 0 or body.color_idx >= room["team_count"]:
            raise HTTPException(400, "Invalid team.")

    # Capacity check
    max_members = room.get("max_team_members", 4)
    current_members = [p for p in _mem["players"] if p["color_idx"] == body.color_idx and p["pid"] != pid]
    if len(current_members) >= max_members:
        raise HTTPException(400, f"Team is full (max {max_members} members).")

    player["color_idx"] = body.color_idx
    _redeal_active_round_if_live(room)
    _bump()
    _persist(store.set_color, room["session_id"], pid, body.color_idx)
    _notify("PLAYER_TEAM_CHANGED", pid=pid, color_idx=body.color_idx)
    return {"ok": True, "color_idx": body.color_idx}


@app.post("/api/round/submit")
async def submit_round_answer(body: SubmitAnswerBody):
    room = _require_room()
    if body.code.strip().upper() != room["join_code"]:
        raise HTTPException(403, "Wrong room code.")
    if room.get("game_mode") != "competitive":
        raise HTTPException(400, "Answer submission is only available in competitive mode.")
    if room["phase"] != "live" or room["current_round"] < 0:
        raise HTTPException(400, "No active round.")

    # Check if already won
    if room.get("round_winner"):
        return {"ok": False, "won": True, "winner": room["round_winner"], "message": "Round already solved!"}

    pid = clean_text(body.pid, 64)
    player = _find_player(pid)
    if not player:
        raise HTTPException(404, "Player not found.")
    if player["color_idx"] < 0:
        raise HTTPException(400, "Player has no team assigned.")

    # Anti-spam cooldown per player: 3 seconds
    now = time.time()
    last_guess = _mem.get("guess_cooldowns", {}).get(pid, 0)
    if now - last_guess < 3.0:
        cooldown_left = round(3.0 - (now - last_guess), 1)
        return {"ok": False, "correct": False, "cooldown": cooldown_left, "message": f"Slow down! Wait {cooldown_left}s."}

    _mem.setdefault("guess_cooldowns", {})[pid] = now

    cur = room["current_round"]
    if cur >= len(_mem["answers"]):
        raise HTTPException(400, "Invalid round.")
    correct_raw = _mem["answers"][cur]
    if normalize_guess(body.guess) == normalize_guess(correct_raw):
        team_idx = player["color_idx"]
        custom_t = next((t for t in room.get("custom_teams", []) if t["color_idx"] == team_idx), None)
        team_name = custom_t["name"] if custom_t else PALETTE[team_idx % len(PALETTE)]["name"]
        winner_info = {
            "team_idx": team_idx,
            "team_name": team_name,
            "player_name": player["name"],
            "pid": pid,
            "answer": correct_raw,
            "round": cur,
            "timestamp": int(now * 1000),
        }
        room["round_winner"] = winner_info

        scores = room.setdefault("scores", {})
        team_key = str(team_idx)
        scores[team_key] = scores.get(team_key, 0) + 1

        _bump()
        _persist(store.set_round_winner, winner_info)
        _persist(store.set_scores, scores)
        _notify("ROUND_WIN", winner=winner_info, scores=scores)

        return {"ok": True, "correct": True, "winner": winner_info, "scores": scores}
    else:
        return {"ok": True, "correct": False, "cooldown": 3.0, "message": "Incorrect guess. Try again in 3s."}


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
        "game_mode": room.get("game_mode", "classic"),
        "scores": room.get("scores", {}),
        "round_winner": room.get("round_winner"),
        "winner_summary": _calc_winner_summary(room),
        "allow_team_choice": room.get("allow_team_choice", True),
        "custom_teams": room.get("custom_teams", []),
        "max_team_members": room.get("max_team_members", 4),
        "settings": {
            "edge_marks": room["edge_marks"],
            "allow_flips": room["allow_flips"],
            "piece_orient": room.get("piece_orient", "auto"),
        },
        "players": _mem["players"],
        "answers": _mem["answers"],
        "snapshot": snapshot,
        "palette": PALETTE,
        "max_teams": MAX_TEAMS,
        "answer_max": ANSWER_MAX,
        "ws_url": _RT_URL or None,
    }
    return JSONResponse(out, headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.post("/api/host/mode")
async def set_game_mode(body: ModeBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    room["game_mode"] = body.mode
    _bump()
    _persist(store.set_game_mode, body.mode)
    _notify("MODE_CHANGED", game_mode=body.mode)
    return {"ok": True, "game_mode": body.mode}


@app.post("/api/host/team_limit")
async def set_team_limit(body: TeamLimitBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    room["max_team_members"] = body.max_team_members
    _bump()
    _persist(store.set_max_team_members, body.max_team_members)
    _notify("TEAM_LIMIT_CHANGED", max_team_members=body.max_team_members)
    return {"ok": True, "max_team_members": body.max_team_members}


@app.post("/api/host/scores")
async def adjust_scores(body: ScoreAdjustBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    scores = room.setdefault("scores", {})
    team_key = str(body.color_idx)
    if body.score is not None:
        scores[team_key] = max(0, body.score)
    elif body.delta is not None:
        scores[team_key] = max(0, scores.get(team_key, 0) + body.delta)
    _bump()
    _persist(store.set_scores, scores)
    _notify("SCORES_UPDATED", scores=scores)
    return {"ok": True, "scores": scores}


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
    _notify("ANSWERS_CHANGED", count=len(answers))

    return {"ok": True, "count": len(answers)}


def _rebalance_teams(players, new_team_count, session_id):
    if not players or new_team_count < 1:
        return

    target_base = len(players) // new_team_count
    extras = len(players) % new_team_count
    team_caps = [target_base + (1 if i < extras else 0) for i in range(new_team_count)]
    
    new_assignments = {}
    current_counts = [0] * new_team_count
    unassigned = []
    
    for p in players:
        c = p["color_idx"]
        if 0 <= c < new_team_count and current_counts[c] < team_caps[c]:
            new_assignments[p["pid"]] = c
            current_counts[c] += 1
        else:
            unassigned.append(p)
            
    for p in unassigned:
        for c in range(new_team_count):
            if current_counts[c] < team_caps[c]:
                new_assignments[p["pid"]] = c
                current_counts[c] += 1
                break
                
    for p in players:
        new_c = new_assignments.get(p["pid"], p["color_idx"])
        if p["color_idx"] != new_c:
            p["color_idx"] = new_c
            _persist(store.set_color, session_id, p["pid"], new_c)

@app.post("/api/host/teams")
async def set_teams(body: TeamsBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)

    room["team_count"] = body.team_count
    if room["phase"] == "live":
        _rebalance_teams(_mem["players"], body.team_count, room["session_id"])
        _redeal_active_round_if_live(room)

    _bump()
    _persist(store.set_team_count, body.team_count)
    _notify("TEAMS_CHANGED", team_count=body.team_count)

    return {"ok": True}


@app.post("/api/host/settings")
async def set_settings(body: SettingsBody, x_host_pin: str | None = Header(default=None)):
    """Puzzle-mode toggles. Baked into snapshots at deal time, so changes
    apply from the next Next/Re-deal — never mid-round."""
    room = _require_room()
    _require_host(x_host_pin, room)

    room["edge_marks"] = body.edge_marks
    room["allow_flips"] = body.allow_flips
    room["piece_orient"] = body.piece_orient
    _bump()
    _persist(store.set_settings, body.edge_marks, body.allow_flips, body.piece_orient)
    _notify("SETTINGS_CHANGED")

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

    if room.get("game_mode") == "competitive":
        custom_teams = room.get("custom_teams", [])
        if custom_teams:
            for p in players:
                if p["color_idx"] < 0 or not any(t["color_idx"] == p["color_idx"] for t in custom_teams):
                    team_counts = {t["color_idx"]: sum(1 for pl in players if pl["color_idx"] == t["color_idx"]) for t in custom_teams}
                    smallest_ci = min(team_counts, key=team_counts.get)
                    p["color_idx"] = smallest_ci
                    _persist(store.set_color, room["session_id"], p["pid"], smallest_ci)
        else:
            for p in players:
                if p["color_idx"] < 0 or p["color_idx"] >= room["team_count"]:
                    p["color_idx"] = smallest_team(players, room["team_count"])
                    _persist(store.set_color, room["session_id"], p["pid"], p["color_idx"])
        room["phase"] = "live"
        room["current_round"] = -1
        room["round_winner"] = None
        _bump()
        _persist(store.set_phase, "live")
        _persist(store.set_current_round, -1)
        _persist(store.set_round_winner, None)
        _notify("GAME_START")
        return {"ok": True}

    # Classic mode: Memory first
    color_map = deal_colors(players, room["team_count"])
    for p in players:
        p["color_idx"] = color_map.get(p["pid"], p["color_idx"])
    room["phase"] = "live"
    room["current_round"] = -1
    room["round_winner"] = None
    _bump()

    # Persist in background
    for pid, ci in color_map.items():
        _persist(store.set_color, room["session_id"], pid, ci)
    _persist(store.set_phase, "live")
    _persist(store.set_current_round, -1)
    _persist(store.set_round_winner, None)
    _notify("GAME_START")

    return {"ok": True}


@app.post("/api/host/round")
async def round_nav(body: RoundBody, x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    if room["phase"] not in ("live", "finished"):
        raise HTTPException(409, "Assign colors first.")
    answers = _mem["answers"]
    cur = room["current_round"]

    # Reset round winner on any navigation
    room["round_winner"] = None
    _persist(store.set_round_winner, None)

    if body.action in ("finish", "end"):
        room["phase"] = "finished"
        _persist(store.set_phase, "finished")
        _bump()
        _notify("GAME_END", phase="finished")
        return {"ok": True, "phase": "finished"}

    if body.action == "resume":
        room["phase"] = "live"
        _persist(store.set_phase, "live")
        _bump()
        _notify("NEXT", round=room["current_round"])
        return {"ok": True, "phase": "live"}

    if body.action == "next":
        if room["phase"] == "finished":
            raise HTTPException(400, "Game is finished.")
        nxt = cur + 1
        if nxt >= len(answers):
            # Last round completed: finish game and trigger celebration
            room["phase"] = "finished"
            _persist(store.set_phase, "finished")
            _bump()
            _notify("GAME_END", phase="finished")
            return {"ok": True, "phase": "finished"}
        if nxt not in _mem["rounds"]:
            snap = make_snapshot(nxt, answers[nxt], _mem["players"],
                                 room.get("edge_marks", True), room.get("allow_flips", True))
            _mem["rounds"][nxt] = snap
            _persist(store.save_round, room["session_id"], nxt, snap, snap["made_at"])
        room["current_round"] = nxt
        _persist(store.set_current_round, nxt)
        _bump()
        _notify("NEXT", round=nxt)
    elif body.action == "back":
        if room["phase"] == "finished":
            room["phase"] = "live"
            _persist(store.set_phase, "live")
        room["current_round"] = max(-1, cur - 1)
        _persist(store.set_current_round, room["current_round"])
        _bump()
        _notify("BACK", round=room["current_round"])
    elif body.action == "redeal":
        if cur < 0:
            raise HTTPException(400, "No active round to re-deal.")
        snap = make_snapshot(cur, answers[cur], _mem["players"],
                             room.get("edge_marks", True), room.get("allow_flips", True))
        _mem["rounds"][cur] = snap
        _persist(store.save_round, room["session_id"], cur, snap, snap["made_at"])
        _bump()
        _notify("REDEAL", round=cur)
    else:
        raise HTTPException(400, "Unknown action.")

    return {"ok": True}


@app.post("/api/host/finish")
async def finish_game(x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)
    if room["phase"] not in ("live", "finished"):
        raise HTTPException(400, "Game is not live.")
    room["phase"] = "finished"
    room["round_winner"] = None
    _persist(store.set_round_winner, None)
    _persist(store.set_phase, "finished")
    _bump()
    _notify("GAME_END", phase="finished")
    return {"ok": True, "phase": "finished"}


@app.post("/api/host/lobby")
async def back_to_lobby(x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)

    room["phase"] = "lobby"
    room["current_round"] = -1
    room["round_winner"] = None
    room["scores"] = {}
    room["custom_teams"] = []
    _mem["guess_cooldowns"] = {}
    for p in _mem["players"]:
        p["ready"] = False
        p["color_idx"] = -1
    _mem["rounds"] = {}
    _bump()

    _persist(store.reset_to_lobby)
    _persist(store.set_custom_teams, [])
    _notify("GAME_END")

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
        _notify("PLAYER_KICKED", pid=body.pid)

    return {"ok": True}


@app.post("/api/close")
async def close_room(x_host_pin: str | None = Header(default=None)):
    room = _require_room()
    _require_host(x_host_pin, room)

    _notify("ROOM_CLOSED")

    _mem["room"] = None
    _mem["players"] = []
    _mem["answers"] = []
    _mem["rounds"] = {}
    _mem["kicked_pids"] = set()
    _bump()

    _persist(store.close_room)

    return {"ok": True}
