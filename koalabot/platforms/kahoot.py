from __future__ import annotations
import asyncio
import json
import random
import re
import time
from base64 import b64decode
from typing import List, Any, Optional

import aiohttp

from ..core import PlayerStatus

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

KAHOOT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://kahoot.it",
    "Referer": "https://kahoot.it/",
}


def _solve_challenge(session_token: str, challenge_text: str) -> str:
    text = challenge_text.replace("\t", " ")
    encoded = ""
    offset = 0

    m_decode = re.search(r"decode\.call\(this,\s*'([a-zA-Z0-9]+)'\)", text)
    m_offset = re.search(r"var offset\s*=\s*([()+*\s\t0-9]+);", text)
    if m_decode and m_offset:
        encoded = m_decode.group(1)
        offset = int(eval(m_offset.group(1)))
    else:
        m = re.search(r"offset\s*=\s*([0-9]+)", text)
        if m:
            offset = int(m.group(1))
        else:
            try:
                offset = int(eval(text.split("offset = ")[1].split(";")[0]))
            except Exception:
                return ""
        m2 = re.search(r"this,\s*['\"]([^'\"]+)['\"]", text)
        if m2:
            encoded = m2.group(1)
        else:
            try:
                encoded = text.split("this, '")[1].split("'")[0]
            except Exception:
                return ""

    decoded = ""
    for pos, ch in enumerate(encoded):
        decoded += chr(((ord(ch) * pos) + offset) % 77 + 48)

    try:
        token_str = b64decode(session_token).decode("utf-8")
    except Exception:
        return ""

    result = ""
    for i in range(len(token_str)):
        result += chr(ord(token_str[i]) ^ ord(decoded[i % len(decoded)]))
    return result


def _parse_ws_payload(raw: str) -> List[dict]:
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return [data]
    return []


async def _kahoot_bot(
    session: aiohttp.ClientSession,
    pin: str,
    name: str,
    stop_event: asyncio.Event,
    update_cb,
) -> PlayerStatus:
    status = PlayerStatus(name=name, platform="kahoot", status="joining")
    update_cb(status)

    ws: Optional[aiohttp.ClientWebSocketResponse] = None
    msg_id = 0

    def next_id() -> str:
        nonlocal msg_id
        msg_id += 1
        return str(msg_id)

    async def send_msg(ws_conn: aiohttp.ClientWebSocketResponse, payload: dict) -> None:
        payload = dict(payload)
        payload["id"] = payload.get("id") or next_id()
        await ws_conn.send_str(json.dumps([payload]))

    try:
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

        ws_url = f"wss://kahoot.it/cometd/{pin}/{session_id}"
        ws = await session.ws_connect(ws_url, headers={"User-Agent": USER_AGENT}, timeout=12)

        await send_msg(ws, {
            "version": "1.0",
            "minimumVersion": "1.0",
            "channel": "/meta/handshake",
            "supportedConnectionTypes": ["websocket", "long-polling", "callback-polling"],
            "advice": {"timeout": 60000, "interval": 0},
            "ext": {
                "ack": True,
                "timesync": {"tc": int(time.time() * 1000), "l": 0, "o": 0},
            },
        })

        client_id: Optional[str] = None
        ack = 0
        namerator_sent = False
        answered_questions = 0
        handshake_deadline = time.time() + 12

        while client_id is None and time.time() < handshake_deadline:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            for packet in _parse_ws_payload(msg.data):
                if packet.get("channel") == "/meta/handshake" and packet.get("clientId"):
                    client_id = packet["clientId"]
                    break
                if packet.get("error"):
                    status.status = "failed"
                    status.error = str(packet.get("error"))[:120]
                    update_cb(status)
                    await ws.close()
                    return status

        if not client_id:
            status.status = "failed"
            status.error = "handshake no clientId"
            update_cb(status)
            await ws.close()
            return status

        await send_msg(ws, {
            "channel": "/meta/connect",
            "clientId": client_id,
            "connectionType": "websocket",
            "advice": {"timeout": 0},
            "ext": {
                "ack": ack,
                "timesync": {"tc": int(time.time() * 1000), "l": 262, "o": -14},
            },
        })
        ack += 1

        for ch in ("/service/controller", "/service/player", "/service/status"):
            await send_msg(ws, {
                "channel": "/meta/subscribe",
                "clientId": client_id,
                "subscription": ch,
            })

        await send_msg(ws, {
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
                }),
            },
        })

        while not stop_event.is_set():
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.5)
            except asyncio.TimeoutError:
                if stop_event.is_set():
                    break
                try:
                    await send_msg(ws, {
                        "channel": "/meta/connect",
                        "clientId": client_id,
                        "connectionType": "websocket",
                        "ext": {
                            "ack": ack,
                            "timesync": {"tc": int(time.time() * 1000), "l": 262, "o": -14},
                        },
                    })
                    ack += 1
                except Exception:
                    break
                continue

            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
                continue

            for packet in _parse_ws_payload(msg.data):
                if packet.get("error"):
                    if not status.joined:
                        status.status = "failed"
                        status.error = str(packet.get("error"))[:120]
                        update_cb(status)
                        await ws.close()
                        return status
                    break

                channel = packet.get("channel", "")
                data = packet.get("data") or {}

                if channel == "/meta/connect":
                    try:
                        await send_msg(ws, {
                            "channel": "/meta/connect",
                            "clientId": client_id,
                            "connectionType": "websocket",
                            "ext": {
                                "ack": ack,
                                "timesync": {"tc": int(time.time() * 1000), "l": 262, "o": -14},
                            },
                        })
                        ack += 1
                    except Exception:
                        pass

                if channel == "/service/controller":
                    ctrl_type = data.get("type") if isinstance(data, dict) else None
                    if ctrl_type == "loginResponse" and not namerator_sent:
                        status.joined = True
                        status.status = "joined"
                        status.last_action = "joined lobby"
                        update_cb(status)
                        await send_msg(ws, {
                            "channel": "/service/controller",
                            "clientId": client_id,
                            "data": {
                                "gameid": pin,
                                "type": "message",
                                "host": "kahoot.it",
                                "id": 4,
                                "content": json.dumps({"usingNamerator": False}),
                            },
                        })
                        namerator_sent = True

                content_raw = data.get("content") if isinstance(data, dict) else None
                content: Any = {}
                if isinstance(content_raw, str):
                    try:
                        content = json.loads(content_raw)
                    except Exception:
                        content = {}
                elif isinstance(content_raw, dict):
                    content = content_raw

                msg_id_num = data.get("id") if isinstance(data, dict) else None
                if msg_id_num == 2 or (isinstance(content, dict) and "questionIndex" in content):
                    choice = random.randint(0, 3)
                    await asyncio.sleep(random.uniform(1.2, 4.5))
                    try:
                        await send_msg(ws, {
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
                                        "device": {
                                            "userAgent": USER_AGENT,
                                            "screen": {"width": 1920, "height": 1080},
                                        },
                                    },
                                }),
                            },
                        })
                        answered_questions += 1
                        status.answers = answered_questions
                        status.status = "answering"
                        status.last_action = f"answered Q{answered_questions}"
                        update_cb(status)
                    except Exception:
                        pass

                if msg_id_num in (3, 10) or any(
                    x in json.dumps(packet).lower() for x in ("game_over", "reset_controller", "game over")
                ):
                    status.status = "completed"
                    status.last_action = "game ended"
                    update_cb(status)
                    break

            if status.status == "completed":
                break

        if status.status not in ("completed", "failed"):
            status.status = "completed" if status.joined else "failed"
            if status.joined:
                status.last_action = "disconnected after game"
        if ws:
            await ws.close()

    except asyncio.CancelledError:
        status.status = "completed"
        raise
    except Exception as e:
        status.status = "failed" if not status.joined else "completed"
        status.error = str(e)[:120]
        status.last_action = "error"
        update_cb(status)
    finally:
        if ws and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass
        update_cb(status)

    return status


async def spawn_kahoot_bots(pin: str, names: List[str], join_delay_ms: int = 150) -> List[PlayerStatus]:
    pin = str(pin).strip()
    results: List[PlayerStatus] = [PlayerStatus(name=n) for n in names]
    stop_event = asyncio.Event()

    connector = aiohttp.TCPConnector(limit_per_host=0, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=12, sock_read=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=KAHOOT_HEADERS) as session:
        tasks = []

        async def _wrapped(i: int, nm: str):
            await asyncio.sleep(min(0.04 * (i % 25), 1.5))
            ps = await _kahoot_bot(session, pin, nm, stop_event, lambda s: None)
            results[i] = ps
            return ps

        for idx, nm in enumerate(names):
            async def _run(i=idx, n=nm):
                await asyncio.sleep((i * join_delay_ms) / 1000.0)
                await _wrapped(i, n)
            tasks.append(asyncio.create_task(_run()))

        await asyncio.gather(*tasks, return_exceptions=True)
        stop_event.set()

    return results