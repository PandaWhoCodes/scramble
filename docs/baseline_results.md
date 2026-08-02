# Scramble Baseline Load Test Results

> Tested against: `https://scrambles.fly.dev`  
> Date: 2026-08-02  
> Server: 1x shared CPU, 1GB RAM, Singapore region

---

## Side-by-Side Comparison

| Metric | 15 Players | 50 Players | Target |
|--------|-----------|-----------|--------|
| **p50 (median)** | 1,015ms | 1,744ms | < 200ms |
| **p95** | 4,032ms | **10,021ms** | < 500ms |
| **p99** | 7,131ms | **13,593ms** | < 2,000ms |
| **Max** | 7,605ms | **15,334ms** | — |
| **Mean** | 1,352ms | 3,027ms | — |
| **Error rate** | 0% | 0% | < 1% |
| **Total requests** | 243 | 613 | — |
| **Join failures** | 0/15 | **35/50** 🚨 | 0 |

> [!CAUTION]
> At 50 players, **35 out of 50 failed to join** because the rate limiter (`join_rate_ok`) caps at 15 joins per 60 seconds per IP. When everyone is on the same Wi-Fi (e.g. a venue basement), they all share one public IP. This means **only 15 people can join per minute from the same network** — a critical bug for the exact use case this app is designed for.

---

## Per-Endpoint Deep Dive (50 Players)

| Endpoint | Count | p50 | p95 | p99 | Max |
|----------|-------|-----|-----|-----|-----|
| `GET /api/state` (player poll) | 488 | 1,749ms | 8,519ms | 10,205ms | 11,333ms |
| `GET /api/host/state` (host poll) | 18 | 2,607ms | **10,867ms** | 10,929ms | 10,944ms |
| `POST /api/ready` | 50 | 8,526ms | **14,987ms** | 15,330ms | 15,334ms |
| `POST /api/host/assign` | 1 | — | — | — | **4,506ms** |
| `POST /api/room` | 1 | — | — | — | **2,400ms** |
| `POST /api/join` | 50 | 610ms | 1,072ms | 1,135ms | 1,182ms |

### Key Observations

1. **`POST /api/ready` is the worst endpoint**: Median 8.5 seconds, p95 nearly 15 seconds. This is because every ready toggle calls `bust_cache()`, causing the next poll to trigger a full 4-query DB rebuild while holding the global lock. With 15+ players readying up simultaneously, each one busts the cache and they all pile up.

2. **`GET /api/host/state` is consistently 2x worse than player state**: Because it bypasses the cache and makes 4 direct remote DB queries every single time. With the global lock, each host poll blocks player polls too.

3. **`GET /api/state` median is 1.7 seconds**: Even the cached player endpoint takes nearly 2 seconds. This is the network round-trip to Turso when the cache expires (every 2s), compounded by lock contention.

4. **Throughput degrades over time**: In the 50-player test, windows at 90-125s show RPS dropping to 2.6-4.0 with p50 of 4-10 seconds. The server is drowning.

---

## Latency Distribution (50 Players)

```
    0– 100ms:   18 (  2.9%) █
  100– 250ms:    8 (  1.3%) 
  250– 500ms:   39 (  6.4%) ███
  500–1000ms:   97 ( 15.8%) ████████
 1000–2000ms:  166 ( 27.1%) ██████████████
 2000–5000ms:  165 ( 26.9%) ██████████████
 5000–10000ms: 88 ( 14.4%) ███████
10000–30000ms: 32 (  5.2%) ███
```

> [!WARNING]
> Over **73%** of all requests take more than 1 second. Over **20%** take more than 5 seconds. This is the "20-30 second lag" experience you described — and that's without the additional latency of a slow basement Wi-Fi connection on top.

---

## Bugs Found

### 1. Rate Limiter Blocks Venue Joins
The `join_rate_ok()` function in [main.py:129](file:///Users/ashish/Documents/PERSONAL/scramble/main.py#L129-L137) limits to 15 joins per 60 seconds **per IP**. At a venue where everyone shares one Wi-Fi, this means only 15 people can join per minute. This is a showstopper for any event with more than 15 people.

### 2. Global Lock Serialization  
Confirmed: every DB operation goes through a single `threading.Lock`, causing all requests to queue single-file behind remote Turso queries.

### 3. Host Endpoint Cache Bypass
Confirmed: `/api/host/state` makes 4 uncached remote queries on every call, and each one holds the lock while doing so.

### 4. Cache Bust Thundering Herd
Every mutating action resets the cache timestamp, forcing the next poll to rebuild from scratch while blocking all other requests.

---

## Verdict

> [!IMPORTANT]
> The test suite successfully replicates the exact problems experienced during the real game. The baseline is established. We can now implement fixes and re-run these same tests to verify improvements.

**Passing criteria for the next run:**
- p95 < 500ms (currently 10,021ms — need 20× improvement)
- p99 < 2,000ms (currently 13,593ms — need 7× improvement)
- 50/50 players must join successfully from one IP
- Error rate < 1%
