"""Verify the image's preinstalled Firefox and hidden display without a website."""
import asyncio
import platform
import sys


async def check_browser():
    from invisible_playwright.async_api import InvisiblePlaywright
    from invisible_core.download import cache_dir_for_seal
    from invisible_core.seal import active_seal

    seal = active_seal()
    asset = seal.asset_for(sys.platform, platform.machine())
    executable = cache_dir_for_seal(seal) / asset.entry_rel
    if not executable.is_file():
        raise RuntimeError("Firefox is not preinstalled; build with WITH_X1080X_BROWSER=true")

    # binary_path prevents runtime download; explicit locale/timezone allow the
    # library's best-effort egress lookup to fail under --network=none.
    async with InvisiblePlaywright(
        binary_path=str(executable), headless=True, humanize=False,
        locale="en-US", timezone="UTC",
    ) as browser:
        context = await browser.new_context()
        try:
            page = await context.new_page()
            await page.goto("data:text/html,<title>x1080x-runtime-check</title>", timeout=15000)
            if await page.title() != "x1080x-runtime-check" or await page.evaluate("6 * 7") != 42:
                raise RuntimeError("Firefox page execution failed")
            print(f"x1080x browser runtime OK: Firefox {browser.version} ({platform.machine()})")
        finally:
            await context.close()


async def main():
    await asyncio.wait_for(check_browser(), timeout=120)


if __name__ == "__main__":
    asyncio.run(main())
