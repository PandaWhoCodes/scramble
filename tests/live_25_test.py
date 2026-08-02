import asyncio
from playwright.async_api import async_playwright, Page, BrowserContext

BASE_URL = "https://scrambles.fly.dev"
PLAYERS = 25
CODE = "SGX59"

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
    name = f"Bot-{idx}"
    await page.fill("#name", name)
    await page.click("#b-name")
    
    # Lobby screen
    try:
        await page.wait_for_selector("#s-lobby:not(.hidden)", timeout=15000)
    except Exception as e:
        err_text = await page.inner_text(".err.show") if await page.is_visible(".err.show") else "No error text"
        print(f"[Bot-{idx}] Failed to reach lobby. Error UI says: '{err_text}'")
        await page.screenshot(path=f"bot_{idx}_failed.png")
        raise e
    
    # Ready up
    await page.click("#b-ready")
    await page.wait_for_selector("#b-ready.on")
    print(f"[Bot-{idx}] Ready!")
    return page

async def host_setup(context: BrowserContext) -> tuple[Page, str]:
    page = await context.new_page()
    await page.goto(BASE_URL + "/host")
    await page.wait_for_selector("#s-setup:not(.hidden), #s-gate:not(.hidden), #s-dash:not(.hidden)")
    if await page.is_visible("#s-setup:not(.hidden)"):
        await page.fill("#su-pin", "1234")
        await page.fill("#su-ans", "TESTING\nSCRAMBLE\nLOAD\nPLAYWRIGHT")
        await page.click("#b-setup")
    elif await page.is_visible("#s-gate:not(.hidden)"):
        await page.fill("#g-pin", "1234")
        await page.click("#b-gate")
        try:
            await page.wait_for_selector("#s-dash:not(.hidden)", timeout=3000)
            page.on("dialog", lambda dialog: dialog.accept())
            await page.click("#b-close")
            await page.wait_for_selector("#s-setup:not(.hidden)")
            await page.fill("#su-pin", "1234")
            await page.fill("#su-ans", "TESTING\nSCRAMBLE\nLOAD\nPLAYWRIGHT")
            await page.click("#b-setup")
        except Exception:
            pass # already in setup
    await page.wait_for_selector("#s-dash:not(.hidden)")
    join_code = await page.inner_text("#jcode")
    return page, join_code

async def run_test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        host_context = await browser.new_context()
        print("Creating room as Host...")
        host_page, code = await host_setup(host_context)
        print(f"\n======================================")
        print(f"ROOM CREATED! Code: {code}")
        print(f"GO TO: {BASE_URL}/host")
        print(f"Enter PIN: 1234 to control the game!")
        print(f"======================================\n")
        
        print(f"Launching {PLAYERS} Players to join room {code}...")
        iphone_13 = p.devices['iPhone 13']
        player_contexts = []
        for _ in range(PLAYERS):
            ctx = await browser.new_context(**iphone_13)
            player_contexts.append(ctx)
            
        print(f"[Players] Joining {PLAYERS} players (staggered to save CPU and respect rate limits)...")
        sem = asyncio.Semaphore(1)
        async def safe_join(ctx, i, code):
            async with sem:
                await asyncio.sleep(2.6)
                return await player_join(ctx, i, code)
        join_tasks = [safe_join(ctx, i+1, code) for i, ctx in enumerate(player_contexts)]
        player_pages = await asyncio.gather(*join_tasks)
        print(f"[Players] All {PLAYERS} bots joined and readied up!")
        print(f"Holding connection open indefinitely. You can now test your host controls.")
        print(f"Press Ctrl+C to stop.")
        
        while True:
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(run_test())
