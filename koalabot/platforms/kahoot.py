"""Kahoot bot spawner - async, high performance, pure Python (aiohttp).

Protocol based on public reverse engineering (reserve/session + cometd over WS + challenge solve).
"""

from __future__ import annotations
import asyncio
import json
import random
import re
import time
from base64 import b64decode
from typing import List

import aiohttp

from ..core import PlayerStatus, MAX_BOTS

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)

KAHOOT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://kahoot.it",
    "Referer": "https://kahoot.it/",
}

# Bayeux / CometD style for Kahoot
HANDSHAKE = {
    "version": "1.0",
    "minimumVersion": "1.0",
    "channel": "/meta/handshake",
    "supportedConnectionTypes": ["websocket", "long-polling"],
    "advice": {"timeout": 60000, "interval": 0},
}

def _solve_challenge(session_token: str, challenge_text: str) -> str:
    """Pure-Python challenge solver (ported/adapted from known working implementations)."""
    try:
        text = challenge_text.replace("\t", " ").encode("ascii", "ignore").decode("utf-8")
        # Try to extract offset safely
        m = re.search(r"offset\s*=\s*([0-9]+)", text)
        if m:
            offset = int(m.group(1))
        else:
            # Fallback: eval the small expression (server controlled, small risk)
            offset = int(eval(text.split("offset = ")[1].split(";")[0]))  # nosec - controlled input shape

        # Extract the input string passed to the decoder
        m2 = re.search(r"this,\s*['\"]([^'\"]+)['\"]", text)
        if m2:
            encoded = m2.group(1)
        else:
            # Original style fallback
            encoded = text.split("this, '")[1].split("'")[0]

        # decode(offset, input)
        decoded = ""
        for pos, ch in enumerate(encoded):
            decoded += chr(((ord(ch) * pos) + offset) % 77 + 48)

        # XOR with base64-decoded session token
        token_bytes = b64decode(session_token)
        sol_chars = [ord(c) for c in decoded]
        tok_chars = list(token_bytes)
        result = bytes(tok_chars[i] ^ sol_chars[i % len(sol_chars)] for i in range(len(tok_chars)))
        return result.decode("utf-8", errors="replace")
    except Exception:
        # Last resort: return empty so join will likely fail fast
        return ""

async def _kahoot_bot(
    session: aiohttp.ClientSession,
    pin: str,
    name: str,
    stop_event: asyncio.Event,
    update_cb,  # callable(status: PlayerStatus)
) -> PlayerStatus:
    status = PlayerStatus(name=name, platform="kahoot", status="joining")
    update_cb(status)

    try:
        # 1. Reserve session
        ts = int(time.time() * 1000)
        reserve_url = f"https://kahoot.it/reserve/session/{pin}/?{ts}"
        async with session.get(reserve_url, headers=KAHOOT_HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                status.status = "failed"
                status.error = f"reserve {resp.status}"
                update_cb(status)
                return status
            data = await resp.json()
            session_token = resp.headers.get("x-kahoot-session-token")
            challenge = data.get("challenge", "")

        if not session_token or not challenge:
            status.status = "failed"
            status.error = "no token/challenge"
            update_cb(status)
            return status

        session_id = _solve_challenge(session_token, challenge)
        if not session_id:
            status.status = "failed"
            status.error = "challenge solve failed"
            update_cb(status)
            return status

        # 2. Connect WS
        ws_url = f"wss://play.kahoot.it/cometd/{pin}/{session_id}"
        ws = await session.ws_connect(ws_url, headers={"User-Agent": USER_AGENT}, timeout=12)

        # Handshake
        await ws.send_json({**HANDSHAKE, "id": "1"})
        msg = await ws.receive_json(timeout=8)
        client_id = msg.get("clientId")
        if not client_id:
            await ws.close()
            status.status = "failed"
            status.error = "handshake no clientId"
            update_cb(status)
            return status

        # Connect
        await ws.send_json({
            "channel": "/meta/connect",
            "clientId": client_id,
            "connectionType": "websocket",
            "id": "2",
            "advice": {"timeout": 0},
        })

        # Subscribe channels (controller, player, status)
        for ch in ("/service/controller", "/service/player", "/service/status"):
            await ws.send_json({
                "channel": "/meta/subscribe",
                "clientId": client_id,
                "subscription": ch,
                "id": str(random.randint(10, 99)),
            })

        # Login / join
        login_msg = {
            "channel": "/service/controller",
            "clientId": client_id,
            "data": {
                "type": "login",
                "gameid": pin,
                "host": "kahoot.it",
                "name": name,
                "content": json.dumps({
                    "device": {
                        "userAgent": USER_AGENT,
                        "screen": {"width": 1920, "height": 1080},
                    },
                    "usingNamerator": False,
                }),
            },
            "id": "3",
        }
        await ws.send_json(login_msg)

        status.joined = True
        status.status = "joined"
        status.last_action = "joined lobby"
        update_cb(status)

        # Main receive loop - answer questions randomly, count answers
        answer_choices = [0, 1, 2, 3]
        answered_questions = 0

        while not stop_event.is_set():
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.5)
            except asyncio.TimeoutError:
                if stop_event.is_set():
                    break
                # send lightweight connect to keep alive
                try:
                    await ws.send_json({
                        "channel": "/meta/connect",
                        "clientId": client_id,
                        "connectionType": "websocket",
                        "id": str(random.randint(100, 999)),
                    })
                except Exception:
                    break
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                # Detect question start (various shapes seen in wild)
                content = data.get("data") or data.get("content") or {}
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except Exception:
                        content = {}

                msg_type = data.get("msgType") or data.get("type") or ""
                # Common patterns for question ready / start
                if "question" in str(msg_type).lower() or "START_QUESTION" in str(data) or content.get("type") == "quiz":
                    # Random answer after small realistic delay
                    choice = random.choice(answer_choices)
                    delay = random.uniform(1.2, 4.5)
                    await asyncio.sleep(delay)

                    try:
                        answer_payload = {
                            "channel": "/service/controller",
                            "clientId": client_id,
                            "data": {
                                "type": "message",
                                "gameid": pin,
                                "host": "kahoot.it",
                                "id": 45,
                                "content": json.dumps({
                                    "choice": choice,
                                    "meta": {
                                        "lag": random.randint(10, 120),
                                        "device": {"userAgent": USER_AGENT, "screen": {"width": 1920, "height": 1080}},
                                    },
                                }),
                            },
                            "id": str(random.randint(1000, 9999)),
                        }
                        await ws.send_json(answer_payload)
                        answered_questions += 1
                        status.answers = answered_questions
                        status.last_action = f"answered Q{answered_questions}"
                        update_cb(status)
                    except Exception:
                        pass

                if any(x in str(data) for x in ("GAME_OVER", "RESET_CONTROLLER", "game over", "end")):
                    status.status = "completed"
                    status.last_action = "game ended"
                    update_cb(status)
                    break

            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

        if status.status not in ("completed", "failed"):
            status.status = "completed"
            status.last_action = "disconnected after game"
        await ws.close()

    except asyncio.CancelledError:
        status.status = "completed"
        raise
    except Exception as e:
        status.joined = status.joined  # keep whatever we achieved
        status.status = "failed" if not status.joined else "completed"
        status.error = str(e)[:120]
        status.last_action = "error"
        update_cb(status)
    finally:
        update_cb(status)

    return status

async def spawn_kahoot_bots(pin: str, names: List[str], join_delay_ms: int = 150) -> List[PlayerStatus]:
    """Spawn many Kahoot bots concurrently with small stagger."""
    pin = str(pin).strip()
    if not pin or not pin.isdigit():
        # still try; some pins have letters in special modes but rare
        pass

    results: List[PlayerStatus] = [PlayerStatus(name=n) for n in names]
    stop_event = asyncio.Event()

    connector = aiohttp.TCPConnector(limit_per_host=0, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=12, sock_read=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=KAHOOT_HEADERS) as session:
        tasks = []

        async def _wrapped(i: int, nm: str):
            # small stagger to be nice to servers and look more human
            await asyncio.sleep(min(0.04 * (i % 25), 1.5))
            ps = await _kahoot_bot(session, pin, nm, stop_event, lambda s: None)  # update inside runner
            results[i] = ps
            return ps

        for idx, nm in enumerate(names):
            async def _run(i=idx, n=nm):
                await asyncio.sleep((i * join_delay_ms) / 1000.0)
                await _wrapped(i, n)
            tasks.append(asyncio.create_task(_run()))

        # We return control to caller who will drive updates via a different mechanism
        # Actually we want live updates. Better: pass a callback that the GUI runner will provide.
        # For direct use, we still run them.

        # Simpler: run with a queue or just gather and let outer code poll (we'll use live callback in runner)
        # Here we just await them all for the library-style call.
        await asyncio.gather(*tasks, return_exceptions=True)
        stop_event.set()

    return results
