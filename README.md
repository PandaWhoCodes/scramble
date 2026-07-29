# Scramble

A team letter-unjumble party game for live events, built on the same rails as
Hueddle: FastAPI + Turso (SQLite fallback), QR + room code to join, a hidden
PIN-protected host console at **`/host`**, and phones that are pure renderers
of server state.

## The game

1. Players scan the QR, enter a name once — a UUID in localStorage keeps their
   identity across refreshes, reconnects and phone-lock forever.
2. Players tap **Ready**; the host watches the ready count, picks a team count
   (2–12), and hits **Assign colors & go live**. Every phone turns its team color.
3. The host shows a question on their own slides (PPT stays theirs — this app
   only handles **answers**). Hitting **Next** deals the current answer within
   *each* team as **contiguous, in-order pieces** — one piece per phone,
   lengths ±1. A phone's letters always read in answer order; the jumble is
   *which piece landed on which phone*. Teams solve by physically lining up
   their phones, not by staring at their own screen.
4. If a team has more people than the word has letters, the extras become
   **Solvers** — no piece; they direct the line-up.
   A solo team would see the whole answer, so that one phone gets up to 3
   shuffled pieces instead — a self-contained mini-puzzle.
5. **PUZZLE mode — answers of 4 letters or fewer.** Short answers deal as
   jigsaw tiles of the *drawn* word instead of text pieces. Each team
   independently gets the best split plan for its size (auto-picked by the
   same ranking as the lab): a rows×cols slice of the word canvas, sometimes
   as letter groups ("EV" as a 2×2 + "E" solo for a team of 5). Every phone
   receives a viewBox crop plus a rotation — wide tiles force landscape,
   tall tiles portrait, and upside-down is in the mix — so players must find
   both their spot *and* how to hold their phone. Phone screens are
   deliberately minimal: just the tile (or piece), nothing else — a tap
   toggles a small round-number pill and a rotate hint. Optional **edge-match
   marks** (half-dots that complete across a seam) confirm correct placement.
   Two host toggles control this: *Edge-match marks* and *Allow upside-down*,
   baked into each deal so replays are stable (Re-deal applies changes now).

   One honest trade-off: in puzzle mode a phone renders its crop from the
   full group text, so the answer is technically present in the page source
   during the round. Fine for party play; if it ever matters, the hardening
   path is server-side tile rendering to PNG.
5. **Back** returns to the previous round (the exact same deal is replayed
   from its stored snapshot), **Re-deal** reshuffles the current round — e.g.
   after a late joiner, who gets a team immediately but letters only when the
   next deal happens.

Phones show the word's *shape* (word count + letter boxes) but never the
answer; the answer is only visible on the PIN-protected host console.

## Resilience model (the whole point)

The DB is the single source of truth. Phones poll one endpoint,
`GET /api/state?pid=`, and render whatever it says — lobby, team screen, your
letters, solver view. That makes every failure boring:

- **Refresh / phone died / browser killed the tab** → localStorage pid +
  render-from-state lands you exactly where the game is, same letters.
- **Connection drop / bad venue WiFi** → the poll loop backs off (up to 15s),
  shows a small "reconnecting…" pill, and *keeps your last screen* — your
  letters stay up, which is all your team needs mid-round. A `visibilitychange`
  or `online` event forces an immediate re-sync.
- **Pieces can't reshuffle under you** → each round's deal is computed once
  and snapshotted in the `round` table; Next/Back/refresh replay it verbatim.
- **Host closes the game or resets to lobby** → phones follow on the next poll;
  if the server no longer knows a pid (fresh session), the phone silently
  re-joins with its stored name + code.

The hot endpoint sits behind a 2-second in-process cache, so ~100 phones
polling every ~3s cost about one DB round-trip per 2 seconds.

## Quickstart (local, zero config)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/host — set a PIN, paste a few answers (one per
line) — then join from http://127.0.0.1:8000/ in a second tab or your phone.
No Turso vars → it uses a local `scramble.db` SQLite file with the same schema.

## Turso

```bash
turso db create scramble
turso db show scramble --url          # -> TURSO_DATABASE_URL
turso db tokens create scramble       # -> TURSO_AUTH_TOKEN
```

Set both in `.env` (or Fly secrets) and the store switches to the official
`libsql` client transparently.

## Deploying (Fly.io)

```bash
fly launch --no-deploy
fly secrets set TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... BASE_URL=https://<app>.fly.dev
fly deploy
```

Run **one machine**: the state cache and join rate limiter are in-process.
No AI keys needed — this game is pure logic, so it costs nothing to run
beyond the machine.

## Host cheat-sheet

- Answers are editable mid-game; already-played rounds keep their original
  deal (snapshots), and **Re-deal** refreshes the current round to the latest
  roster + answer text.
- Letters and digits only in answers, up to 40 characters; spaces define the
  word shape players see.
- The per-team grid on the console shows exactly who holds which letters —
  handy for judging and for helping a stuck team.
- **End game → back to lobby** keeps the crowd and answers but clears colors
  and ready flags — perfect for running a second game the same night.

## Event-day checklist

- [ ] Test venue WiFi with a few phones; the game tolerates drops, but joining needs one good request.
- [ ] Set `BASE_URL` so the QR encodes your public URL; project `/host`'s QR card.
- [ ] Difficulty = number of pieces. Teams of 4–8 are the sweet spot (4–8
      pieces to order); 2–3 person teams work but solve fast, so give them
      longer multi-word answers.
- [ ] Answers shorter than the team size create Solvers; that's a feature, brief them.
- [ ] Judge with the per-team letters grid on the console; the answer is only on your screen.
