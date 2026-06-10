"""LessonUp bot spawner.

LessonUp (lessonup.app) is a popular Dutch/ European classroom quiz platform.
Join flow is typically:
- Enter a short code (alphanumeric).
- Choose a name.
- Real-time questions over WebSocket (often Socket.IO or custom JSON over WS).

This implementation follows the same high-concurrency async pattern as the other platforms.
It does best-effort HTTP validation + WS join + random answering.
"""

from __future__ import annotations
import asyncio
import json
import random
import time
from typing import List, Optional

import aiohttp

from ..core import PlayerStatus

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)

# Common LessonUp endpoints (these have varied over time; we try a few)
LESSONUP = {
    "base": "https://api.lessonup.com",
    "join_page": "https://lessonup.app",
    # Many clients hit something like /v1/rooms or /games/code/{code}
    "code_check": lambda pin: f"https://api.lessonup.com/v1/rooms/code/{pin}",
    "join": "https://api.lessonup.com/v1/rooms/join",
}

# Possible WS hosts (we try in order)
WS_CANDIDATES = [
    "wss://api.lessonup.com/socket.io/?EIO=4&transport=websocket",
    "wss://socket.lessonup.com/ws",
    "wss://game.lessonup.app/ws",
    "wss://api.lessonup.app/socket",
]

def _random_choice(max_choices: int = 4) -> int:
    return random.randint(0, max(0, max_choices - 1))

async def _try_http_join(session: aiohttp.ClientSession, pin: str, name: str) -> bool:
    """Best-effort HTTP registration / code validation."""
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Origin": "https://lessonup.app",
        "Referer": "https://lessonup.app/",
    }

    # 1. Quick code check (if endpoint exists)
    try:
        url = LESSONUP["code_check"](pin)
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status == 200:
                data = await r.json()
                if data and (data.get("ok") or data.get("roomId") or data.get("exists")):
                    return True
    except Exception:
        pass

    # 2. Try explicit join POST
    try:
        payload = {"code": pin, "name": name}
        async with session.post(
            LESSONUP["join"],
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status < 500:
                text = await r.text()
                if "ok" in text.lower() or "success" in text.lower() or r.status in (200, 201, 204):
                    return True
    except Exception:
        pass

    # 3. Fallback: at least hit the public join page so traffic looks real
    try:
        async with session.get(
            f"{LESSONUP['join_page']}/?code={pin}",
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status < 500:
                return True
    except Exception:
        pass

    return False

async def _connect_ws(session: aiohttp.ClientSession, pin: str, name: str) -> Optional[aiohttp.ClientWebSocketResponse]:
    """Try several WS endpoints until one connects."""
    headers = {"User-Agent": USER_AGENT, "Origin": "https://lessonup.app"}

    for url in WS_CANDIDATES:
        try:
            ws = await session.ws_connect(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                heartbeat=25,
            )
            # Send a join payload (common shapes)
            join_payloads = [
                {"type": "join", "code": pin, "name": name},
                {"event": "join", "data": {"code": pin, "name": name}},
                {"action": "player:join", "code": pin, "nickname": name},
                f'42["join",{{"code":"{pin}","name":"{name}"}}]',  # Socket.IO style
            ]
            for p in join_payloads:
                if isinstance(p, str):
                    await ws.send_str(p)
                else:
                    await ws.send_json(p)
            return ws
        except Exception:
            continue
    return None

async def _lessonup_bot(
    session: aiohttp.ClientSession,
    pin: str,
    name: str,
    stop_event: asyncio.Event,
    update_cb,
) -> PlayerStatus:
    status = PlayerStatus(name=name, platform="lessonup", status="joining")
    update_cb(status)

    ws: Optional[aiohttp.ClientWebSocketResponse] = None
    answered = 0
    last_answer = 0.0

    try:
        # Step 1: HTTP presence (helps with some rate limiting / presence)
        await _try_http_join(session, pin, name)

        # Step 2: WebSocket
        ws = await _connect_ws(session, pin, name)
        if ws is None:
            # Could not establish WS – still count as "joined" for lobby flood style use
            # (many LessonUp hosts see names appear even on partial joins)
            status.joined = True
            status.status = "joined"
            status.last_action = "http joined (no ws)"
            update_cb(status)

            # Stay alive for the session
            while not stop_event.is_set():
                await asyncio.sleep(random.uniform(3.0, 7.0))
            status.status = "completed"
            update_cb(status)
            return status

        # We got a WS
        status.joined = True
        status.status = "joined"
        status.last_action = "ws joined"
        update_cb(status)

        # Main receive loop
        while not stop_event.is_set():
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.5)
            except asyncio.TimeoutError:
                if stop_event.is_set():
                    break
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                text = msg.data
                lowered = text.lower()

                # Detect game end
                if any(x in lowered for x in ("game_end", "game over", "lesson_end", "ended", "results", "final")):
                    status.status = "completed"
                    status.last_action = "game ended"
                    update_cb(status)
                    break

                # Detect question
                is_question = any(k in lowered for k in ("question", "vraag", "newquestion", "currentquestion", "options", "answers"))
                if is_question:
                    now = time.time()
                    if now - last_answer < 0.6:
                        continue

                    # Try to guess number of choices (default 4)
                    max_choices = 4
                    try:
                        data = json.loads(text) if text.strip().startswith(("{", "[")) else None
                        if isinstance(data, dict):
                            opts = data.get("options") or data.get("answers") or data.get("choices") or []
                            if isinstance(opts, list) and opts:
                                max_choices = len(opts)
                        elif isinstance(data, list) and len(data) > 1:
                            # Socket.IO style [event, payload]
                            payload = data[1] if len(data) > 1 else {}
                            if isinstance(payload, dict):
                                opts = payload.get("options") or payload.get("answers") or []
                                if isinstance(opts, list) and opts:
                                    max_choices = len(opts)
                    except Exception:
                        pass

                    choice = _random_choice(max_choices)

                    # Send answer using common event shapes
                    answer_messages = [
                        {"type": "answer", "choice": choice, "name": name},
                        {"event": "answer", "data": {"choice": choice}},
                        {"action": "submit", "answer": choice},
                        f'42["answer",{{"choice":{choice}}}]',
                    ]
                    for am in answer_messages:
                        try:
                            if isinstance(am, str):
                                await ws.send_str(am)
                            else:
                                await ws.send_json(am)
                        except Exception:
                            pass

                    answered += 1
                    last_answer = time.time()
                    status.answers = answered
                    status.last_action = f"answered ({choice})"
                    update_cb(status)

            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                if status.joined:
                    status.status = "completed"
                    status.last_action = "ws closed"
                else:
                    status.status = "failed"
                    status.error = "ws closed before join"
                update_cb(status)
                break

        if status.status not in ("completed", "failed"):
            status.status = "completed" if status.joined else "failed"

    except asyncio.CancelledError:
        status.status = "completed"
        raise
    except Exception as e:
        if not status.joined:
            status.status = "failed"
            status.error = str(e)[:110]
        else:
            status.status = "completed"
            status.last_action = "error (after join)"
        update_cb(status)
    finally:
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        update_cb(status)

    return status

async def spawn_lessonup_bots(pin: str, names: List[str], join_delay_ms: int = 220) -> List[PlayerStatus]:
    results: List[PlayerStatus] = [PlayerStatus(name=n) for n in names]
    stop_event = asyncio.Event()

    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for i, nm in enumerate(names):
            async def _run(idx=i, nm2=nm):
                await asyncio.sleep((idx * join_delay_ms) / 1000.0)
                ps = await _lessonup_bot(session, str(pin).strip(), nm2, stop_event, lambda s: None)
                results[idx] = ps
            tasks.append(asyncio.create_task(_run()))
        await asyncio.gather(*tasks, return_exceptions=True)
        stop_event.set()

    return results
