"""
Free game join via unified /ws/join WebSocket (v1.6.0+).
Dial /ws/join → read welcome → send hello {entryType: free} → wait for assigned.
The same socket becomes the gameplay socket — return it to the caller.
"""
import json
import asyncio
import websockets
from bot.config import WS_JOIN_URL, get_skill_version
from bot.api_client import APIError
from bot.credentials import get_api_key
from bot.utils.logger import get_logger

log = get_logger(__name__)

MAX_REDIAL = 10


async def join_free_game() -> tuple:
    """
    Connect /ws/join and complete the free matchmaking handshake.
    Returns (ws, game_id, agent_id). The socket is already the gameplay socket.
    game_id/agent_id are empty when decision was ALREADY_IN_GAME (read from first agent_view).
    Caller owns the socket and must close it when done.
    """
    api_key = get_api_key()
    headers = {
        "X-API-Key": api_key,
        "X-Version": get_skill_version(),
    }

    for attempt in range(1, MAX_REDIAL + 1):
        log.info("Free join attempt #%d via /ws/join...", attempt)
        ws = None
        try:
            ws = await websockets.connect(
                WS_JOIN_URL,
                additional_headers=headers,
                ping_interval=None,
                max_size=2 ** 20,
            )

            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            welcome = json.loads(raw)
            if welcome.get("type") != "welcome":
                log.warning("Expected welcome, got: %s", welcome.get("type"))
                await ws.close()
                continue

            decision = welcome.get("decision", "")
            log.info("welcome decision=%s", decision)

            if decision == "ALREADY_IN_GAME":
                log.info("Already in game — socket proxied to gameplay")
                return ws, "", ""

            if decision == "BLOCKED":
                missing = (
                    welcome.get("readiness", {})
                    .get("freeRoom", {})
                    .get("missing", [])
                )
                codes = [m.get("code", m) if isinstance(m, dict) else m for m in missing]
                log.error("Join BLOCKED: %s", codes)
                await ws.close()
                if "NO_IDENTITY" in codes:
                    raise APIError("NO_IDENTITY", "ERC-8004 identity required for free room")
                if "NOT_PRIMARY_AGENT" in codes:
                    raise APIError("NOT_PRIMARY_AGENT", "Not the primary agent for this SC wallet")
                raise RuntimeError(f"Join BLOCKED: {codes}")

            if decision not in ("ASK_ENTRY_TYPE", "FREE_ONLY"):
                log.warning("Unexpected decision: %s — re-dialing", decision)
                await ws.close()
                continue

            await ws.send(json.dumps({"type": "hello", "entryType": "free"}))

            async for raw_msg in ws:
                msg = json.loads(raw_msg)
                mtype = msg.get("type", "")

                if mtype == "queued":
                    log.info("Queued in matchmaking...")
                    continue

                if mtype == "assigned":
                    game_id = msg.get("gameId", "")
                    agent_id = msg.get("agentId", "")
                    log.info("Assigned: game=%s agent=%s", game_id[:12], agent_id[:12])
                    return ws, game_id, agent_id

                if mtype == "not_selected":
                    log.info("not_selected — re-dialing")
                    await ws.close()
                    break

                if mtype == "error":
                    code = msg.get("code", "")
                    log.warning("Join error: code=%s — re-dialing", code)
                    await ws.close()
                    if code == "MATCH_TIMEOUT":
                        await asyncio.sleep(2)
                    break

                log.debug("Unexpected join msg type=%s", mtype)

        except asyncio.TimeoutError:
            log.warning("WS welcome timeout (attempt %d)", attempt)
            if ws and not ws.closed:
                await ws.close()
        except (APIError, RuntimeError):
            raise
        except websockets.exceptions.WebSocketException as e:
            log.warning("WS error (attempt %d): %s", attempt, e)
            if ws and not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    pass
        except Exception:
            if ws and not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise

        if attempt < MAX_REDIAL:
            await asyncio.sleep(min(2 * attempt, 10))

    raise RuntimeError(f"Free join failed after {MAX_REDIAL} attempts")
