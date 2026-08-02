#!/usr/bin/env python3
"""Scramble Load Test Suite

Simulates N concurrent players + host through a full game lifecycle:
  1. Host opens a room with answers
  2. N players join with unique PIDs
  3. All players mark ready
  4. Host assigns colors (game goes live)
  5. Host advances through rounds
  6. Throughout: all players + host poll continuously

Measures per-endpoint p50/p95/p99 latencies, error rates, and throughput.

Usage:
  python tests/load_test.py --url https://scrambles.fly.dev --players 15
  python tests/load_test.py --url https://scrambles.fly.dev --players 50 --rounds 5
  python tests/load_test.py --url http://localhost:8080 --players 15
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

import aiohttp


# ─── Configuration ───────────────────────────────────────────────────

DEFAULT_ANSWERS = ["LOVE", "HERO", "BRAVE", "OPEN SOURCE", "SCRAMBLE",
                   "PYTHON", "FAST", "GAME", "PUZZLE", "TEAM"]
HOST_PIN = "loadtest1234"
POLL_INTERVAL_PLAYER = 1.0   # seconds between player polls (matches user request)
POLL_INTERVAL_HOST = 1.0     # seconds between host polls
PLAYER_JOIN_STAGGER = 0.15   # seconds between each player joining (simulates real arrival)


# ─── Data Collection ─────────────────────────────────────────────────

@dataclass
class RequestRecord:
    endpoint: str
    method: str
    status: int
    latency_ms: float
    timestamp: float
    error: str = ""
    size_bytes: int = 0


@dataclass
class LoadTestResults:
    records: list = field(default_factory=list)
    phase_timestamps: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    patchy_internet: bool = False

    def add(self, rec: RequestRecord):
        self.records.append(rec)
        if rec.error:
            self.errors.append(rec)

    def mark_phase(self, name: str):
        self.phase_timestamps[name] = time.time()


# ─── HTTP Client Wrapper ─────────────────────────────────────────────

class ScrambleClient:
    def __init__(self, base_url: str, results: LoadTestResults, label: str = ""):
        self.base_url = base_url.rstrip("/")
        self.results = results
        self.label = label

    async def _request(self, session: aiohttp.ClientSession, method: str, path: str,
                       json_body=None, headers=None) -> tuple[int, dict | None, float]:
        url = f"{self.base_url}{path}"
        t0 = time.monotonic()
        error = ""
        status = 0
        body = None
        size = 0

        # --- Patchy Internet Simulation ---
        if self.results.patchy_internet:
            import random
            import asyncio
            # 15% chance to completely drop the request (timeout)
            if random.random() < 0.15:
                await asyncio.sleep(2.0)
                error = "TIMEOUT (SIMULATED)"
                latency = (time.monotonic() - t0) * 1000
                self.results.add(RequestRecord(
                    endpoint=f"{method} {path.split('?')[0]}", method=method, status=0,
                    latency_ms=latency, timestamp=time.time(), error=error, size_bytes=0
                ))
                return 0, None, latency
            
            # 30% chance for a massive latency spike (2 to 6 seconds)
            if random.random() < 0.30:
                await asyncio.sleep(random.uniform(2.0, 6.0))

        try:
            kwargs = {"headers": headers or {}}
            if json_body is not None:
                kwargs["json"] = json_body
            async with session.request(method, url, **kwargs) as resp:
                status = resp.status
                raw = await resp.read()
                size = len(raw)
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, Exception):
                    body = None
        except asyncio.TimeoutError:
            error = "TIMEOUT"
            status = 0
        except aiohttp.ClientError as e:
            error = f"CLIENT_ERROR: {type(e).__name__}: {e}"
            status = 0
        except Exception as e:
            error = f"UNKNOWN: {type(e).__name__}: {e}"
            status = 0

        latency = (time.monotonic() - t0) * 1000
        self.results.add(RequestRecord(
            endpoint=f"{method} {path.split('?')[0]}",
            method=method,
            status=status,
            latency_ms=latency,
            timestamp=time.time(),
            error=error,
            size_bytes=size,
        ))
        return status, body, latency

    async def get(self, session, path, headers=None):
        return await self._request(session, "GET", path, headers=headers)

    async def post(self, session, path, json_body=None, headers=None):
        return await self._request(session, "POST", path, json_body=json_body, headers=headers)


# ─── Player Simulator ────────────────────────────────────────────────

class PlayerSim:
    def __init__(self, client: ScrambleClient, player_id: int):
        self.client = client
        self.pid = f"loadtest-{player_id}-{uuid.uuid4().hex[:8]}"
        self.name = f"Player_{player_id}"
        self.player_id = player_id
        self._polling = False
        self._poll_task = None

    async def join(self, session: aiohttp.ClientSession, code: str):
        status, body, lat = await self.client.post(session, "/api/join", {
            "code": code, "pid": self.pid, "name": self.name
        })
        return status == 200

    async def ready(self, session: aiohttp.ClientSession, code: str):
        status, body, lat = await self.client.post(session, "/api/ready", {
            "code": code, "pid": self.pid, "ready": True
        })
        return status == 200

    async def poll_state(self, session: aiohttp.ClientSession):
        status, body, lat = await self.client.get(
            session, f"/api/state?pid={self.pid}"
        )
        return status, body, lat

    async def start_polling(self, session: aiohttp.ClientSession, stop_event: asyncio.Event):
        """Continuously poll /api/state until stop_event is set."""
        self._polling = True
        while not stop_event.is_set():
            await self.poll_state(session)
            # Add jitter to avoid thundering herd in the test itself
            jitter = 0.5 * (hash(self.pid) % 100) / 100.0
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_PLAYER + jitter)
            except asyncio.TimeoutError:
                pass
        self._polling = False


# ─── Host Simulator ──────────────────────────────────────────────────

class HostSim:
    def __init__(self, client: ScrambleClient):
        self.client = client
        self.pin = HOST_PIN
        self.headers = {"X-Host-Pin": self.pin}

    async def open_room(self, session: aiohttp.ClientSession, answers: list[str]):
        status, body, lat = await self.client.post(session, "/api/room", {
            "pin": self.pin, "answers": answers
        })
        if status == 200 and body:
            return body.get("join_code")
        # Room might already exist — try to close it first
        if status == 409:
            await self.close_room(session)
            await asyncio.sleep(0.5)
            status, body, lat = await self.client.post(session, "/api/room", {
                "pin": self.pin, "answers": answers
            })
            if status == 200 and body:
                return body.get("join_code")
        return None

    async def close_room(self, session: aiohttp.ClientSession):
        await self.client.post(session, "/api/close", headers=self.headers)

    async def set_teams(self, session: aiohttp.ClientSession, count: int):
        status, body, lat = await self.client.post(
            session, "/api/host/teams", {"team_count": count}, headers=self.headers
        )
        return status == 200

    async def assign_colors(self, session: aiohttp.ClientSession):
        status, body, lat = await self.client.post(
            session, "/api/host/assign", {}, headers=self.headers
        )
        return status == 200

    async def next_round(self, session: aiohttp.ClientSession):
        status, body, lat = await self.client.post(
            session, "/api/host/round", {"action": "next"}, headers=self.headers
        )
        return status == 200

    async def poll_host_state(self, session: aiohttp.ClientSession):
        status, body, lat = await self.client.get(
            session, "/api/host/state", headers=self.headers
        )
        return status, body, lat

    async def start_polling(self, session: aiohttp.ClientSession, stop_event: asyncio.Event):
        while not stop_event.is_set():
            await self.poll_host_state(session)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_HOST)
            except asyncio.TimeoutError:
                pass


# ─── Test Orchestrator ────────────────────────────────────────────────

async def run_load_test(base_url: str, num_players: int, num_rounds: int,
                        num_host_screens: int = 2, poll_duration_per_round: float = 15.0,
                        num_teams: int = 0, patchy_internet: bool = False):
    """Run the full load test lifecycle."""
    results = LoadTestResults(patchy_internet=patchy_internet)
    client = ScrambleClient(base_url, results, "main")

    host = HostSim(client)
    players = [PlayerSim(client, i + 1) for i in range(num_players)]

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    connector = aiohttp.TCPConnector(limit=100, limit_per_host=100, ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # ── Phase 1: Open Room ──
        print(f"\n{'='*60}")
        print(f"  SCRAMBLE LOAD TEST")
        print(f"  Target:  {base_url}")
        print(f"  Players: {num_players}")
        print(f"  Rounds:  {num_rounds}")
        print(f"  Host screens: {num_host_screens}")
        print(f"{'='*60}\n")

        results.mark_phase("start")
        print("📦 Phase 1: Opening room...")
        join_code = await host.open_room(session, DEFAULT_ANSWERS[:num_rounds])
        if not join_code:
            print("❌ FATAL: Could not open room. Aborting.")
            return results
        print(f"   Room opened. Join code: {join_code}")
        results.mark_phase("room_opened")

        # ── Phase 2: Players Join ──
        print(f"\n👥 Phase 2: {num_players} players joining (staggered)...")
        results.mark_phase("join_start")
        join_successes = 0
        for p in players:
            ok = await p.join(session, join_code)
            if ok:
                join_successes += 1
            else:
                print(f"   ⚠️  Player {p.player_id} failed to join")
            await asyncio.sleep(PLAYER_JOIN_STAGGER)
        print(f"   {join_successes}/{num_players} joined successfully")
        results.mark_phase("join_complete")

        # ── Phase 3: All Players Ready Up ──
        print(f"\n✅ Phase 3: All players readying up...")
        results.mark_phase("ready_start")
        ready_tasks = [p.ready(session, join_code) for p in players]
        ready_results = await asyncio.gather(*ready_tasks, return_exceptions=True)
        ready_ok = sum(1 for r in ready_results if r is True)
        print(f"   {ready_ok}/{num_players} readied up")
        results.mark_phase("ready_complete")

        # Set team count
        team_count = num_teams if num_teams > 0 else max(1, num_players // 4)
        await host.set_teams(session, team_count)
        print(f"   Teams set to {team_count}")

        # ── Phase 4: Assign Colors (go live) ──
        print(f"\n🎨 Phase 4: Assigning colors...")
        results.mark_phase("assign_start")
        ok = await host.assign_colors(session)
        if not ok:
            print("❌ FATAL: Could not assign colors. Aborting.")
            await host.close_room(session)
            return results
        print("   Colors assigned — game is LIVE")
        results.mark_phase("game_live")

        # ── Phase 5: Rounds with continuous polling ──
        for round_num in range(num_rounds):
            round_label = f"round_{round_num + 1}"
            print(f"\n🔄 Phase 5.{round_num + 1}: Round {round_num + 1}/{num_rounds}")
            print(f"   Starting round + {poll_duration_per_round}s of concurrent polling...")

            results.mark_phase(f"{round_label}_start")

            # Host advances to next round
            ok = await host.next_round(session)
            if not ok:
                print(f"   ⚠️  Failed to start round {round_num + 1}")
                continue

            # Start concurrent polling: all players + host screens
            stop_event = asyncio.Event()
            poll_tasks = []

            # Player polling
            for p in players:
                poll_tasks.append(asyncio.create_task(
                    p.start_polling(session, stop_event)
                ))

            # Host screen polling (simulate multiple host tabs)
            for i in range(num_host_screens):
                poll_tasks.append(asyncio.create_task(
                    host.start_polling(session, stop_event)
                ))

            # Let everyone poll for the specified duration
            await asyncio.sleep(poll_duration_per_round)

            # Stop all polling
            stop_event.set()
            await asyncio.gather(*poll_tasks, return_exceptions=True)

            results.mark_phase(f"{round_label}_end")

            # Count requests in this round
            round_start_ts = results.phase_timestamps[f"{round_label}_start"]
            round_end_ts = results.phase_timestamps[f"{round_label}_end"]
            round_records = [r for r in results.records
                           if round_start_ts <= r.timestamp <= round_end_ts]
            round_errors = [r for r in round_records if r.error or r.status >= 400]
            latencies = [r.latency_ms for r in round_records if not r.error]

            print(f"   Requests: {len(round_records)} | "
                  f"Errors: {len(round_errors)} | "
                  f"p50: {_percentile(latencies, 50):.0f}ms | "
                  f"p95: {_percentile(latencies, 95):.0f}ms | "
                  f"p99: {_percentile(latencies, 99):.0f}ms")

        # ── Cleanup ──
        print(f"\n🧹 Cleaning up...")
        await host.close_room(session)
        results.mark_phase("end")
        print("   Room closed.")

    return results


# ─── Report Generator ─────────────────────────────────────────────────

def _percentile(data, p):
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[f]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def generate_report(results: LoadTestResults, num_players: int, base_url: str) -> str:
    lines = []
    lines.append("")
    lines.append("=" * 70)
    lines.append("  SCRAMBLE LOAD TEST — FULL REPORT")
    lines.append("=" * 70)

    total_duration = results.phase_timestamps.get("end", 0) - results.phase_timestamps.get("start", 0)
    lines.append(f"\n  Target:       {base_url}")
    lines.append(f"  Players:      {num_players}")
    lines.append(f"  Duration:     {total_duration:.1f}s")
    lines.append(f"  Total Reqs:   {len(results.records)}")
    lines.append(f"  Total Errors: {len(results.errors)}")

    # ── Per-endpoint breakdown ──
    by_endpoint = defaultdict(list)
    for r in results.records:
        by_endpoint[r.endpoint].append(r)

    lines.append(f"\n{'─' * 70}")
    lines.append(f"  PER-ENDPOINT BREAKDOWN")
    lines.append(f"{'─' * 70}")
    lines.append(f"  {'Endpoint':<30} {'Count':>6} {'Err':>4} {'p50':>7} {'p95':>7} {'p99':>7} {'Max':>7} {'Avg Size':>9}")
    lines.append(f"  {'─'*30} {'─'*6} {'─'*4} {'─'*7} {'─'*7} {'─'*7} {'─'*7} {'─'*9}")

    for endpoint in sorted(by_endpoint.keys()):
        records = by_endpoint[endpoint]
        latencies = [r.latency_ms for r in records if not r.error]
        errors = sum(1 for r in records if r.error or r.status >= 400)
        sizes = [r.size_bytes for r in records if r.size_bytes > 0]
        avg_size = statistics.mean(sizes) if sizes else 0

        lines.append(
            f"  {endpoint:<30} {len(records):>6} {errors:>4} "
            f"{_percentile(latencies, 50):>6.0f}ms {_percentile(latencies, 95):>6.0f}ms "
            f"{_percentile(latencies, 99):>6.0f}ms {max(latencies) if latencies else 0:>6.0f}ms "
            f"{avg_size:>8.0f}B"
        )

    # ── Latency distribution ──
    all_latencies = [r.latency_ms for r in results.records if not r.error]
    if all_latencies:
        lines.append(f"\n{'─' * 70}")
        lines.append(f"  OVERALL LATENCY DISTRIBUTION")
        lines.append(f"{'─' * 70}")
        lines.append(f"  Min:    {min(all_latencies):>8.1f}ms")
        lines.append(f"  p25:    {_percentile(all_latencies, 25):>8.1f}ms")
        lines.append(f"  p50:    {_percentile(all_latencies, 50):>8.1f}ms")
        lines.append(f"  p75:    {_percentile(all_latencies, 75):>8.1f}ms")
        lines.append(f"  p90:    {_percentile(all_latencies, 90):>8.1f}ms")
        lines.append(f"  p95:    {_percentile(all_latencies, 95):>8.1f}ms")
        lines.append(f"  p99:    {_percentile(all_latencies, 99):>8.1f}ms")
        lines.append(f"  Max:    {max(all_latencies):>8.1f}ms")
        lines.append(f"  Mean:   {statistics.mean(all_latencies):>8.1f}ms")
        lines.append(f"  Stdev:  {statistics.stdev(all_latencies):>8.1f}ms" if len(all_latencies) > 1 else "")

        # Histogram buckets
        buckets = [100, 250, 500, 1000, 2000, 5000, 10000, 30000]
        lines.append(f"\n  Latency Histogram:")
        prev = 0
        for b in buckets:
            count = sum(1 for l in all_latencies if prev < l <= b)
            pct = 100.0 * count / len(all_latencies)
            bar = "█" * int(pct / 2)
            lines.append(f"  {prev:>6}–{b:>5}ms: {count:>5} ({pct:>5.1f}%) {bar}")
            prev = b
        over = sum(1 for l in all_latencies if l > buckets[-1])
        if over:
            pct = 100.0 * over / len(all_latencies)
            lines.append(f"  >{buckets[-1]:>5}ms: {over:>5} ({pct:>5.1f}%) {'█' * int(pct / 2)}")

    # ── Error details ──
    if results.errors:
        lines.append(f"\n{'─' * 70}")
        lines.append(f"  ERRORS ({len(results.errors)} total)")
        lines.append(f"{'─' * 70}")
        error_types = defaultdict(int)
        for e in results.errors:
            key = f"{e.endpoint}: {e.error or f'HTTP {e.status}'}"
            error_types[key] += 1
        for key, count in sorted(error_types.items(), key=lambda x: -x[1]):
            lines.append(f"  {count:>4}× {key}")

    # ── Throughput over time ──
    if results.records:
        lines.append(f"\n{'─' * 70}")
        lines.append(f"  THROUGHPUT OVER TIME (5-second windows)")
        lines.append(f"{'─' * 70}")
        start_ts = results.phase_timestamps.get("start", results.records[0].timestamp)
        window = 5.0
        t = start_ts
        end_ts = results.records[-1].timestamp
        lines.append(f"  {'Window':<12} {'Reqs':>5} {'Err':>4} {'p50':>7} {'p95':>7} {'RPS':>6}")
        lines.append(f"  {'─'*12} {'─'*5} {'─'*4} {'─'*7} {'─'*7} {'─'*6}")
        while t < end_ts:
            window_recs = [r for r in results.records if t <= r.timestamp < t + window]
            if window_recs:
                lats = [r.latency_ms for r in window_recs if not r.error]
                errs = sum(1 for r in window_recs if r.error or r.status >= 400)
                rps = len(window_recs) / window
                offset = t - start_ts
                lines.append(
                    f"  {offset:>5.0f}–{offset+window:>4.0f}s {len(window_recs):>5} {errs:>4} "
                    f"{_percentile(lats, 50):>6.0f}ms {_percentile(lats, 95):>6.0f}ms {rps:>5.1f}"
                )
            t += window

    # ── Verdicts ──
    lines.append(f"\n{'─' * 70}")
    lines.append(f"  VERDICT")
    lines.append(f"{'─' * 70}")

    p95 = _percentile(all_latencies, 95) if all_latencies else 0
    p99 = _percentile(all_latencies, 99) if all_latencies else 0
    error_rate = len(results.errors) / max(1, len(results.records)) * 100

    checks = [
        ("p95 < 500ms", p95 < 500, f"{p95:.0f}ms"),
        ("p99 < 2000ms", p99 < 2000, f"{p99:.0f}ms"),
        ("Error rate < 1%", error_rate < 1, f"{error_rate:.1f}%"),
        ("No timeouts", not any(r.error == "TIMEOUT" for r in results.records),
         f"{sum(1 for r in results.records if r.error == 'TIMEOUT')} timeouts"),
    ]

    all_pass = True
    for label, passed, actual in checks:
        icon = "✅" if passed else "❌"
        lines.append(f"  {icon} {label:<25} (actual: {actual})")
        if not passed:
            all_pass = False

    lines.append("")
    if all_pass:
        lines.append("  🎉 ALL CHECKS PASSED")
    else:
        lines.append("  💥 SOME CHECKS FAILED — performance is degraded")

    lines.append(f"\n{'=' * 70}\n")
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Scramble Load Test")
    parser.add_argument("--url", default="https://scrambles.fly.dev",
                        help="Base URL of the Scramble server")
    parser.add_argument("--players", type=int, default=15,
                        help="Number of concurrent players to simulate")
    parser.add_argument("--rounds", type=int, default=3,
                        help="Number of game rounds to play")
    parser.add_argument("--host-screens", type=int, default=2,
                        help="Number of host screens polling simultaneously")
    parser.add_argument("--poll-duration", type=float, default=15.0,
                        help="Seconds of concurrent polling per round")
    parser.add_argument("--output", default=None,
                        help="File to write the report to (in addition to stdout)")
    parser.add_argument("--teams", type=int, default=0,
                        help="Number of teams to create (0 to auto-calculate)")
    parser.add_argument("--patchy", action="store_true",
                        help="Simulate a terrible internet connection (drops and high latency spikes)")
    args = parser.parse_args()

    results = await run_load_test(
        base_url=args.url,
        num_players=args.players,
        num_rounds=args.rounds,
        num_host_screens=args.host_screens,
        poll_duration_per_round=args.poll_duration,
        num_teams=args.teams,
        patchy_internet=args.patchy,
    )

    report = generate_report(results, args.players, args.url)
    print(report)

    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"Report saved to {args.output}")

    # Exit with error code if checks failed
    all_latencies = [r.latency_ms for r in results.records if not r.error]
    p95 = _percentile(all_latencies, 95) if all_latencies else 0
    error_rate = len(results.errors) / max(1, len(results.records)) * 100
    if p95 >= 500 or error_rate >= 1:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
