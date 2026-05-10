from pathlib import Path
import asyncio

from dispatch.app import DispatchApp


def test_dashboard_high_res_snapshot() -> None:
    """Capture a high-resolution dashboard snapshot (SVG from Textual renderer)."""
    app = DispatchApp()
    out = Path("tests/snapshots/dashboard-hires.svg")
    out.parent.mkdir(parents=True, exist_ok=True)

    async def run() -> None:
        async with app.run_test(size=(240, 72)) as pilot:
            await pilot.pause(0.5)
            app.save_screenshot(filename=str(out))

    asyncio.run(run())
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<svg")
    assert 'viewBox="0 0 2946 1806.8"' in text
