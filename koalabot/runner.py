"""Runner that replicates the original quizGameBot behavior + live-updating version for the GUI."""

from __future__ import annotations
import asyncio
from typing import List, Callable, Optional, Dict, Any

from .core import (
    normalize_platform,
    clamp_number,
    build_player_names,
    MAX_BOTS,
    PLATFORMS_META,
    PlayerStatus,
    BotRunResult,
    make_summary,
)
from .platforms import spawn_kahoot_bots, spawn_blooket_bots, spawn_gimkit_bots, spawn_lessonup_bots

SPAWNERS = {
    "kahoot": spawn_kahoot_bots,
    "blooket": spawn_blooket_bots,
    "gimkit": spawn_gimkit_bots,
    "lessonup": spawn_lessonup_bots,
}

async def quiz_game_bot(
    *,
    platform: str,
    game_pin: str,
    player_count: int = 5,
    name_prefix: str = "Bot",
) -> BotRunResult:
    """Direct port of the JS quizGameBot export. Returns the same shape of result."""
    selected = normalize_platform(platform)
    pin = str(game_pin or "").replace(" ", "")
    if not pin:
        raise ValueError("gamePin is required")

    count = clamp_number(player_count, 1, MAX_BOTS, 5)
    names = build_player_names(name_prefix, count)
    meta = PLATFORMS_META[selected]
    spawner = SPAWNERS[selected]

    players: List[PlayerStatus] = await spawner(pin, names)  # spawners accept optional join_delay_ms like original

    summary = make_summary(players)

    return BotRunResult(
        platform=meta["label"],
        game_pin=pin,
        join_url=meta["join_url"](pin),
        config={
            "playerCount": count,
            "namePrefix": (name_prefix or "").strip() or "Bot",
            "runMode": "until_game_end",
            "answerMode": "random",
            "maxBots": MAX_BOTS,
        },
        summary=summary,
        players=[p.to_dict() for p in players],
        note=(
            f"Spawned {summary['joined']} bot(s) on {meta['label']}. "
            "Bots stay active until the game ends."
            if summary["joined"]
            else "No bots joined. Verify the game pin, that the host has started the lobby, and that the game is not locked."
        ),
    )

# ---------------- Live version used by the Tkinter GUI ----------------

async def run_live(
    *,
    platform: str,
    game_pin: str,
    player_count: int,
    name_prefix: str,
    on_player_update: Callable[[PlayerStatus], None],
    on_log: Callable[[str], None],
    stop_event: asyncio.Event,
) -> BotRunResult:
    """Launch bots with live callbacks for GUI. Same semantics as quiz_game_bot."""
    selected = normalize_platform(platform)
    pin = str(game_pin or "").replace(" ", "")
    if not pin:
        raise ValueError("gamePin is required")

    count = clamp_number(player_count, 1, MAX_BOTS, 5)
    names = build_player_names(name_prefix, count)
    meta = PLATFORMS_META[selected]
    spawner = SPAWNERS[selected]

    on_log(f"Starting {count} bots on {meta['label']} (pin {pin})...")

    # We launch the spawner but override with per-bot live callbacks.
    # Because the spawners above are gather-based, we re-implement a thin live launcher here
    # for maximum responsiveness in the GUI.

    results: List[PlayerStatus] = [PlayerStatus(name=n) for n in names]

    # Small per-bot wrapper that calls the platform _xxx_bot if exposed, else falls back.
    # For simplicity and to avoid code dupe, we call the spawner (it will run) but also
    # start a poller that reflects final state. Better: call individual bot coros.

    # Use the internal functions by importing them (they are not exported).
    # To keep clean we just drive per-bot tasks here with direct calls.

    from .platforms.kahoot import _kahoot_bot  # type: ignore
    from .platforms.blooket import _blooket_bot  # type: ignore
    from .platforms.gimkit import _gimkit_bot  # type: ignore
    from .platforms.lessonup import _lessonup_bot  # type: ignore

    BOT_IMPL = {
        "kahoot": _kahoot_bot,
        "blooket": _blooket_bot,
        "gimkit": _gimkit_bot,
        "lessonup": _lessonup_bot,
    }[selected]

    connector = None  # each bot creates its own light session for isolation (simpler + robust)
    tasks: List[asyncio.Task] = []

    async def launch_one(idx: int, nm: str):
        # tiny stagger
        await asyncio.sleep(0.012 * (idx % 40))
        # fresh session per bot keeps things simple and matches real clients
        import aiohttp
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=0),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=25),
        ) as sess:
            ps = await BOT_IMPL(
                sess, pin, nm, stop_event,
                lambda s: (results.__setitem__(idx, s), on_player_update(s))
            )
            results[idx] = ps
            on_player_update(ps)

    for i, nm in enumerate(names):
        tasks.append(asyncio.create_task(launch_one(i, nm)))

    # Wait for completion or external stop
    await asyncio.gather(*tasks, return_exceptions=True)

    summary = make_summary(results)
    on_log(f"Finished. Joined: {summary['joined']}/{summary['attempted']}  |  Answers sent: {summary['totalAnswers']}")

    return BotRunResult(
        platform=meta["label"],
        game_pin=pin,
        join_url=meta["join_url"](pin),
        config={
            "playerCount": count,
            "namePrefix": (name_prefix or "").strip() or "Bot",
            "runMode": "until_game_end",
            "answerMode": "random",
            "maxBots": MAX_BOTS,
        },
        summary=summary,
        players=[p.to_dict() for p in results],
        note=(
            f"Spawned {summary['joined']} bot(s) on {meta['label']}."
            if summary["joined"]
            else "No bots joined. Check the PIN and that the host lobby is open."
        ),
    )
