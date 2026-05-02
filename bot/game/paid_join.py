"""
Paid game join via unified /ws/join WebSocket (v1.6.0+).
Dial /ws/join → welcome → hello {entryType: paid, mode: offchain}
→ sign_required → sign EIP-712 → sign_submit → queued → tx_submitted → joined.
The same socket becomes the gameplay socket — return it to the caller.
"""
import json
import asyncio
import websockets
from bot.config import WS_JOIN_URL, PAID_ENTRY_FEE_SMOLTZ, get_skill_version
from bot.api_client import MoltyAPI, APIError
from bot.web3.eip712_signer import sign_join_paid
from bot.credentials import get_api_key, get_agent_private_key
from bot.utils.logger import get_logger

log = get_logger(__name__)


async def join_paid_game(api: MoltyAPI) -> tuple:
    """
    Connect /ws/join and complete the paid matchmaking handshake (offchain mode).
    Returns (ws, game_id, agent_id). The socket is already the gameplay socket.
    Caller owns the socket and must close it when done.
    """
    # Balance pre-check (mandatory before opening /ws/join for paid entry per paid-games.md §1)
    me = await api.get_accounts_me()
    balance = me.get("balance", 0)
    if balance < PAID_ENTRY_FEE_SMOLTZ:
        raise RuntimeError(
            f"Insufficient sMoltz: {balance}/{PAID_ENTRY_FEE_SMOLTZ}. "
            "Keep playing free rooms to accumulate balance."
        )

    agent_pk = get_agent_private_key()
    if not agent_pk:
        raise RuntimeError("Agent private key not found")

    api_key = get_api_key()
    headers = {
        "X-API-Key": api_key,
        "X-Version": get_skill_version(),
    }

    ws = None
    try:
        log.info("Connecting /ws/join for paid entry (offchain)...")
        ws = await websockets.connect(
            WS_JOIN_URL,
            additional_headers=headers,
            ping_interval=None,
            max_size=2 ** 20,
        )

        # Step 1: Read welcome
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        welcome = json.loads(raw)
        if welcome.get("type") != "welcome":
            raise RuntimeError(f"Expected welcome, got: {welcome.get('type')}")

        decision = welcome.get("decision", "")
        log.info("welcome decision=%s", decision)

        if decision == "ALREADY_IN_GAME":
            log.info("Already in game — socket proxied to gameplay")
            return ws, "", ""

        if decision == "BLOCKED":
            missing = (
                welcome.get("readiness", {})
                .get("paidRoom", {})
                .get("missing", [])
            )
            codes = [m.get("code", m) if isinstance(m, dict) else m for m in missing]
            log.error("Paid join BLOCKED: %s", codes)
            await ws.close()
            raise RuntimeError(f"Paid join BLOCKED: {codes}")

        if decision == "FREE_ONLY":
            await ws.close()
            raise RuntimeError("Server indicated FREE_ONLY — paid not available now")

        if not welcome.get("instruction", {}).get("paid", {}).get("enabled", False):
            await ws.close()
            raise RuntimeError("Paid room not enabled per welcome.instruction")

        # Step 2: Send hello
        await ws.send(json.dumps({
            "type": "hello",
            "entryType": "paid",
            "mode": "offchain",
        }))

        # Step 3: Read sign_required
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        sign_req = json.loads(raw)
        if sign_req.get("type") != "sign_required":
            raise RuntimeError(f"Expected sign_required, got: {sign_req.get('type')}")

        join_intent_id = sign_req.get("joinIntentId", "")
        eip712_data = sign_req.get("message", {})
        log.info("Signing EIP-712 for joinIntentId=%s...", join_intent_id[:12])

        # Step 4: Sign with agent EOA
        signature = sign_join_paid(agent_pk, eip712_data)

        # Step 5: Send sign_submit
        await ws.send(json.dumps({
            "type": "sign_submit",
            "joinIntentId": join_intent_id,
            "signature": signature,
        }))

        # Step 6: Read state machine until joined
        async for raw_msg in ws:
            msg = json.loads(raw_msg)
            mtype = msg.get("type", "")

            if mtype == "queued":
                log.info("Paid join queued — sMoltz deducted, tx pending")
                continue

            if mtype == "tx_submitted":
                log.info("Paid join tx submitted: txHash=%s", msg.get("txHash", "")[:20])
                continue

            if mtype == "joined":
                game_id = msg.get("gameId", "")
                agent_id = msg.get("agentId", "")
                log.info("Paid join complete: game=%s agent=%s", game_id[:12], agent_id[:12])
                return ws, game_id, agent_id

            if mtype == "error":
                code = msg.get("code", "")
                log.error("Paid join error: %s", code)
                await ws.close()
                raise RuntimeError(f"Paid join server error: {code}")

            log.debug("Unexpected paid join msg type=%s", mtype)

        raise RuntimeError("Paid join socket closed before joined frame received")

    except Exception:
        if ws and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass
        raise
