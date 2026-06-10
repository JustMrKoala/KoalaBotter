"""Blooket bot spawner - closer port of the original implementation.

Original uses:
- PUT https://api.blooket.com/api/firebase/join → fbToken
- Google verifyCustomToken → idToken
- Probe/discover Firebase WS shard (or fallback)
- Send Firebase wire protocol messages (t:'d' style) for auth + player data + answers
- Random blook on join
- Detect questions and game end from realtime payloads
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

BLOOKET = {
    "join": "https://api.blooket.com/api/firebase/join",
    "verify": "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyCustomToken?key=AIzaSyCA-cTOnX19f6LFnDVVsHXya3k6ByP_MnU",
    "checkPin": lambda pin: f"https://api.blooket.com/api/firebase/id?id={pin}",
}

# From original
SERVER_CODES = [
    1476, 2018, 2025, 2037, 1570, 2520, 2050, 522, 1402, 2034,
    1444, 1755, 1758, 1757, 1756, 1751, 1755,
]

BLOOKS = [
    'Chick', 'Chicken', 'Cow', 'Goat', 'Horse', 'Pig', 'Sheep', 'Duck', 'Dog', 'Cat',
    'Rabbit', 'Goldfish', 'Hamster', 'Turtle', 'Kitten', 'Puppy', 'Bear', 'Moose', 'Fox',
]

def random_blook() -> str:
    return random.choice(BLOOKS)

def _authorize_message(token: str) -> str:
    return json.dumps({"t": "d", "d": {"r": 1, "a": "auth", "b": {"cred": token}}})

def _join_message(pin: str, name: str, blook: str) -> str:
    return json.dumps({
        "t": "d",
        "d": {"r": 2, "a": "p", "b": {"p": f"/{pin}/c/{name}", "d": {"b": blook}}}
    })

def _answer_message(pin: str, name: str, choice: int, req_id: int) -> str:
    return json.dumps({
        "t": "d",
        "d": {"r": req_id, "a": "p", "b": {"p": f"/{pin}/c/{name}", "d": {"c": choice}}}
    })

async def _find_socket_url(session: aiohttp.ClientSession, server_code: int) -> str:
    fallback = f"wss://s-usc1c-nss-200.firebaseio.com/.ws?v=5&ns=blooket-{server_code}"
    probe = None
    try:
        probe = await session.ws_connect(fallback, timeout=aiohttp.ClientTimeout(total=4))
        try:
            msg = await asyncio.wait_for(probe.receive(), timeout=3.5)
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data and data.get("d", {}).get("t") == "r" and data.get("d", {}).get("d"):
                        host = data["d"]["d"]
                        return f"wss://{host}/.ws?v=5&ns=blooket-{server_code}"
                except Exception:
                    pass
        except asyncio.TimeoutError:
            pass
        finally:
            await probe.close()
    except Exception:
        pass
    return fallback

async def _get_auth_token(session: aiohttp.ClientSession, pin: str, name: str) -> str:
    # Check pin is alive
    check_url = BLOOKET["checkPin"](pin)
    async with session.get(check_url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=10)) as r:
        alive = await r.json()
        if not r.ok or not alive.get("success"):
            raise RuntimeError(alive.get("msg") or "Game not found or not active")

    # Join to get fbToken
    join_headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Referer": "https://www.blooket.com/",
    }
    async with session.put(
        BLOOKET["join"],
        json={"id": pin, "name": name},
        headers=join_headers,
        timeout=aiohttp.ClientTimeout(total=12),
    ) as r:
        join_res = await r.json()
        if not r.ok or not join_res.get("fbToken"):
            raise RuntimeError(join_res.get("msg") or "Failed to join game")

    # Exchange for Google idToken
    verify_headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    async with session.post(
        BLOOKET["verify"],
        json={"returnSecureToken": True, "token": join_res["fbToken"]},
        headers=verify_headers,
        timeout=aiohttp.ClientTimeout(total=12),
    ) as r:
        verify_res = await r.json()
        if not r.ok or not verify_res.get("idToken"):
            raise RuntimeError("Failed to verify Blooket token")
        return verify_res["idToken"]

def _should_answer_from_payload(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    return any(k in data for k in ("q", "question", "questionText", "answers", "correct"))

def _is_game_end_message(msg: dict, raw_text: str) -> bool:
    try:
        payload = (msg or {}).get("d", {}).get("d", {}).get("b", {}).get("d")
        path = (msg or {}).get("d", {}).get("d", {}).get("b", {}).get("p")
        if isinstance(payload, dict):
            if payload.get("stg") in ("end", "final") or payload.get("ended") is True:
                return True
            if payload.get("state") == "end" or payload.get("gameOver") is True:
                return True
            if payload.get("winner") is not None and payload.get("k") is True:
                return True
        if isinstance(path, str) and ("/end" in path or "/final" in path or "/state" in path):
            return True
    except Exception:
        pass
    if raw_text and any(x in raw_text.lower() for x in ("game end", "gameend", "ended", "winner", "final")):
        return True
    return False

async def _blooket_bot(
    session: aiohttp.ClientSession,
    pin: str,
    name: str,
    stop_event: asyncio.Event,
    update_cb,
) -> PlayerStatus:
    status = PlayerStatus(name=name, platform="blooket", status="joining")
    update_cb(status)

    headers = {"User-Agent": USER_AGENT}

    ws: Optional[aiohttp.ClientWebSocketResponse] = None
    req_id = 3
    last_answer_at = 0.0

    try:
        # 1. Auth tokens (fb + google)
        token = await _get_auth_token(session, pin, name)

        # 2. Discover socket shard
        code = random.choice(SERVER_CODES)
        socket_url = await _find_socket_url(session, code)

        # 3. Connect WS
        ws = await session.ws_connect(
            socket_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        )

        # Send auth + join with random blook
        blook = random_blook()
        await ws.send_str(_authorize_message(token))
        await ws.send_str(_join_message(pin, name, blook))

        status.joined = True
        status.status = "joined"
        status.last_action = f"joined as {blook}"
        update_cb(status)

        # Main message loop
        while not stop_event.is_set():
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.2)
            except asyncio.TimeoutError:
                if stop_event.is_set():
                    break
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                raw_text = msg.data
                try:
                    data = json.loads(raw_text)
                except Exception:
                    data = None

                if data and _is_game_end_message(data, raw_text):
                    status.status = "completed"
                    status.last_action = "game ended"
                    update_cb(status)
                    break

                payload = None
                try:
                    payload = (data or {}).get("d", {}).get("d", {}).get("b", {}).get("d")
                except Exception:
                    payload = None

                if _should_answer_from_payload(payload):
                    now = time.time()
                    if now - last_answer_at < 0.4:
                        continue
                    last_answer_at = now

                    choice = random.randint(0, 3)
                    try:
                        await ws.send_str(_answer_message(pin, name, choice, req_id))
                        req_id += 1
                        status.answers += 1
                        status.last_action = f"answered ({choice})"
                        update_cb(status)
                    except Exception:
                        pass

            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                if not status.joined:
                    status.status = "failed"
                    status.error = "closed before join"
                else:
                    status.status = "completed"
                    status.last_action = "session closed"
                update_cb(status)
                break

        # If we get here without explicit end, mark completed if joined
        if status.status not in ("completed", "failed"):
            status.status = "completed" if status.joined else "failed"

    except asyncio.CancelledError:
        status.status = "completed"
        raise
    except Exception as e:
        if not status.joined:
            status.status = "failed"
            status.error = str(e)[:120]
        else:
            status.status = "completed"
            status.last_action = "error after join"
        update_cb(status)
    finally:
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        update_cb(status)

    return status

async def spawn_blooket_bots(pin: str, names: List[str], join_delay_ms: int = 200) -> List[PlayerStatus]:
    results: List[PlayerStatus] = [PlayerStatus(name=n) for n in names]
    stop_event = asyncio.Event()

    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for i, nm in enumerate(names):
            async def _run(idx=i, nm2=nm):
                await asyncio.sleep((idx * join_delay_ms) / 1000.0)
                ps = await _blooket_bot(session, str(pin).strip(), nm2, stop_event, lambda s: None)
                results[idx] = ps
            tasks.append(asyncio.create_task(_run()))
        await asyncio.gather(*tasks, return_exceptions=True)
        stop_event.set()

    return results
