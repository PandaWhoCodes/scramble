import asyncio
import time
import sys
import statistics
from playwright.async_api import async_playwright, Page, BrowserContext

BASE_URL = "https://scrambles.fly.dev"
PLAYERS = 10
PIN = "testpin123"

async def host_setup(context: BrowserContext) -> tuple[Page, str]:
    page = await context.new_page()
    await page.goto(BASE_URL + "/host")
    
    # Wait for the boot() API call to finish and reveal a section
    await page.wait_for_selector("#s-setup:not(.hidden), #s-gate:not(.hidden), #s-dash:not(.hidden)")
    
    # Check if we are at the setup screen
    if await page.is_visible("#s-setup:not(.hidden)"):
        await page.fill("#su-pin", PIN)
        await page.fill("#su-ans", "HELLO\nWORLD\nPLAYWRIGHT")
        await page.click("#b-setup")
    elif await page.is_visible("#s-gate:not(.hidden)"):
        # Unlock existing
        await page.fill("#g-pin", PIN)
        await page.click("#b-gate")
        await page.wait_for_selector("#s-dash:not(.hidden)")
        
        # We need a clean slate. Close the old room.
        page.on("dialog", lambda dialog: dialog.accept())
        await page.click("#b-close")
        
        # Now create the new room
        await page.wait_for_selector("#s-setup:not(.hidden)")
        await page.fill("#su-pin", PIN)
        await page.fill("#su-ans", "HELLO\nWORLD\nPLAYWRIGHT")
        await page.click("#b-setup")

    # Wait for dashboard
    await page.wait_for_selector("#s-dash:not(.hidden)")
    
    # Get join code
    join_code = await page.inner_text("#jcode")
    print(f"[Host] Room open. Join code: {join_code}")
    return page, join_code

async def player_join(context: BrowserContext, idx: int, code: str) -> Page:
    page = await context.new_page()
    await page.goto(f"{BASE_URL}?c={code}")
    
    # Wait for initial boot
    await page.wait_for_selector("#s-code:not(.hidden), #s-name:not(.hidden), #s-lobby:not(.hidden)")
    
    # If code is prefilled but we are at code screen
    if await page.is_visible("#s-code:not(.hidden)"):
        await page.click("#b-code")
    
    # Name screen
    await page.wait_for_selector("#s-name:not(.hidden)")
    name = f"P{idx}"
    await page.fill("#name", name)
    await page.click("#b-name")
    
    # Lobby screen
    try:
        await page.wait_for_selector("#s-lobby:not(.hidden)", timeout=5000)
    except Exception as e:
        err_text = await page.inner_text("#e-name")
        print(f"[Player {idx}] Failed to reach lobby. Error UI says: '{err_text}'")
        await page.screenshot(path=f"tests/player_{idx}_timeout.png")
        raise e
    
    # Ready up
    await page.click("#b-ready")
    await page.wait_for_selector("#b-ready.on")
    
    return page

async def run_test():
    async with async_playwright() as p:
        # Launch browser
        browser = await p.chromium.launch(headless=True)
        
        print(f"Launching 1 Host and {PLAYERS} Players...")
        
        # 1. Host
        host_context = await browser.new_context()
        host_page, join_code = await host_setup(host_context)
        
        # 2. Players
        iphone_13 = p.devices['iPhone 13']
        player_contexts = []
        for _ in range(PLAYERS):
            ctx = await browser.new_context(**iphone_13)
            player_contexts.append(ctx)
            
        print(f"[Players] Joining {PLAYERS} players (staggered to save CPU)...")
        sem = asyncio.Semaphore(3)
        async def safe_join(ctx, i, code):
            async with sem:
                return await player_join(ctx, i, code)
        join_tasks = [safe_join(ctx, i+1, join_code) for i, ctx in enumerate(player_contexts)]
        player_pages = await asyncio.gather(*join_tasks)
        print(f"[Players] All {PLAYERS} players joined and readied up.")
        
        # 3. Host assigns colors
        print("[Host] Assigning colors...")
        await host_page.click("#b-assign")
        
        # Wait for host UI to show team screen (meaning we are live)
        await host_page.wait_for_selector("#c-round:not(.hidden)")
        print("[Host] Game is LIVE.")
        
        # Make sure all players transition to team screen
        # On team screen, #st-team is visible
        print("[Players] Waiting for team screens...")
        team_screen_tasks = [page.wait_for_selector("#st-team:not(.hidden)", state="visible") for page in player_pages]
        await asyncio.gather(*team_screen_tasks)
        print("[Players] All players on team screen.")
        
        # 4. Measure Latency
        print("\n" + "="*50)
        print(" MEASURING END-TO-END UI LATENCY")
        print("="*50)
        print("[Host] Clicking Next Round...")
        
        # Setup listeners for players FIRST
        async def wait_for_round(page: Page, idx: int, t0: float, results: list):
            # Wait for the round container to become visible
            await page.wait_for_selector("#st-round:not(.hidden)", state="visible")
            t1 = time.time()
            latency = (t1 - t0) * 1000
            results.append(latency)
        
        latencies = []
        
        # Get host ready to click
        t0 = time.time()
        
        # Fire both the host click and the player waits concurrently
        wait_tasks = [wait_for_round(page, i, t0, latencies) for i, page in enumerate(player_pages)]
        
        # Click host
        await host_page.click("#b-next")
        
        # Await all players to see the update
        await asyncio.gather(*wait_tasks)
        
        # 5. Report
        print(f"\n[Results] All {PLAYERS} players received the UI update.")
        latencies.sort()
        print(f"Min Latency: {latencies[0]:.1f}ms")
        print(f"Max Latency: {latencies[-1]:.1f}ms")
        print(f"Median (p50): {statistics.median(latencies):.1f}ms")
        if len(latencies) >= 20:
            print(f"p95 Latency: {latencies[int(len(latencies)*0.95)]:.1f}ms")
        
        # Cleanup
        print("\n[Host] Closing room...")
        
        # Wait a moment to ensure no pending async tasks crash
        await asyncio.sleep(1)
        
        # Accept the confirm dialog
        host_page.on("dialog", lambda dialog: dialog.accept())
        await host_page.click("#b-close")
        
        await browser.close()
        print("Done.")

if __name__ == "__main__":
    asyncio.run(run_test())
