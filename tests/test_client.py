"""test_client.py — LLMClient tests (fix.md 4.2 z1–z2).

z1: automatic x402 handshake (402 → PAYMENT-SIGNATURE → 200).
z2: PAYMENT-RESPONSE receipt verification (success/network/payer/TX signature/
amount) + spending policy (MAX_PER_CALL, DAILY_CAP — fail-closed).

Mocked HTTP transport: first request → 402 with PAYMENT-REQUIRED;
retry with PAYMENT-SIGNATURE → 200 with a valid receipt (the mock server signs
the TX message as fee payer — like a real facilitator §8.2). No real
network, no real RPC (mint metadata mocked).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from x402.mechanisms.svm.mint_cache import MintMetadata
from x402.schemas import PaymentPayload, SettleResponse

from bridgenode_llm import LLMClient, BridgenodeError

NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
BLOCKHASH = "EZ3rST5dvHmbanh75jc4PuLfV96vp9fEYBVeNk4FfM1k"


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    """Minimal base58 encode (no external dependency)."""
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


def _test_keypair() -> Keypair:
    return Keypair()


def _test_wallet_key(kp: Keypair) -> str:
    """Base58 private key (64 bytes)."""
    return _b58encode(bytes(kp))


def _envelope(pay_to: str, fee_payer: str, memo: str = "pi_test123",
              amount: str = "2000", siwx: bool = False,
              asset: str = USDC, network: str = NETWORK) -> dict:
    """402 V2 envelope (§3.1) — as the server sends it."""
    env = {
        "x402Version": 2,
        "error": "PAYMENT-SIGNATURE header is required",
        "resource": {
            "url": "https://bridgenode.cc/v1/chat/completions",
            "description": "AI inference",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "amount": amount,
                "asset": asset,
                "payTo": pay_to,
                "maxTimeoutSeconds": 30,
                "extra": {
                    "feePayer": fee_payer,
                    "memo": memo,
                    "recentBlockhash": BLOCKHASH,
                    "lastValidBlockHeight": "291470237",
                },
            }
        ],
    }
    if siwx:
        now = datetime.now(timezone.utc)
        env["extensions"] = {
            "sign-in-with-x": {
                "info": {
                    "domain": "bridgenode.cc",
                    "uri": "https://bridgenode.cc/v1/chat/completions",
                    "version": "1",
                    "nonce": "nonce1234567890abcdef",
                    "issuedAt": now.isoformat().replace("+00:00", "Z"),
                    "expirationTime": (now + timedelta(minutes=5))
                    .isoformat().replace("+00:00", "Z"),
                    "resources": ["https://bridgenode.cc/v1/chat/completions"],
                },
                "supportedChains": [
                    {"chainId": NETWORK, "type": "ed25519"},
                ],
            }
        }
    return env


def _openai_response() -> dict:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "Hello!"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _receipt_header(payment_header: str, fee_payer_kp: Keypair,
                    client_wallet: str, **overrides) -> str:
    """Creates a valid PAYMENT-RESPONSE receipt: fee payer signs OUR TX message."""
    payload = PaymentPayload.model_validate_json(base64.b64decode(payment_header))
    tx = VersionedTransaction.from_bytes(
        base64.b64decode(payload.payload["transaction"]))
    msg_bytes = bytes([0x80]) + bytes(tx.message)  # same as client signature
    sig = fee_payer_kp.sign_message(msg_bytes)
    settle_kwargs = {
        "success": True,
        "transaction": str(sig),
        "network": NETWORK,
        "payer": client_wallet,
        "amount": "2000",
    }
    settle_kwargs.update(overrides)
    settle = SettleResponse(**settle_kwargs)
    return base64.b64encode(
        settle.model_dump_json(by_alias=True, exclude_none=True).encode()).decode()


@pytest.fixture(autouse=True)
def _mock_mint_metadata(monkeypatch):
    """Mint metadata without RPC (tests without network)."""
    def fake(client, network, mint, cache):
        return MintMetadata(decimals=6, token_program=Pubkey.from_string(TOKEN_PROGRAM))
    monkeypatch.setattr(
        "x402.mechanisms.svm.exact.client.get_cached_mint_metadata", fake)


def _make_server(fee_payer_kp: Keypair, client_wallet: str,
                 amount: str = "2000", receipt_overrides: dict | None = None,
                 no_receipt: bool = False, siwx: bool = False,
                 siwx_granted: bool = False,
                 asset: str = USDC, network: str = NETWORK):
    """Creates a mock server handler: 402 → valid receipt → 200.

    Returns (handler, seen) — seen: list of requests (for tests).
    ``siwx``: 402 with SIWX challenge (§5.7); ``siwx_granted``: SIWX retry
    answered with 200 immediately (known agent without payment).
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "url": str(request.url),
            "has_payment": request.headers.get("PAYMENT-SIGNATURE") is not None,
            "has_siwx": request.headers.get("SIGN-IN-WITH-X") is not None,
            "siwx_header": request.headers.get("SIGN-IN-WITH-X"),
            "body": json.loads(request.content),
        })
        if not request.headers.get("PAYMENT-SIGNATURE"):
            # SIWX retry with a known agent → 200 without payment (§5.7)
            if siwx_granted and request.headers.get("SIGN-IN-WITH-X"):
                return httpx.Response(200, json=_openai_response())
            env = _envelope(pay_to=client_wallet,
                            fee_payer=str(fee_payer_kp.pubkey()),
                            amount=amount, siwx=siwx,
                            asset=asset, network=network)
            return httpx.Response(
                402,
                headers={"PAYMENT-REQUIRED": base64.b64encode(
                    json.dumps(env).encode()).decode()},
                json=env,
            )
        headers = {}
        if not no_receipt:
            overrides = dict(receipt_overrides or {})
            if amount != "2000" and "amount" not in overrides:
                overrides["amount"] = amount
            headers["PAYMENT-RESPONSE"] = _receipt_header(
                request.headers["PAYMENT-SIGNATURE"], fee_payer_kp,
                client_wallet, **overrides)
        return httpx.Response(200, json=_openai_response(), headers=headers)

    return handler, seen


def _make_client(handler, wallet_key=None, **kwargs) -> LLMClient:
    """LLMClient with BRIDGENODE_WALLET_KEY env (key ONLY from .env, §8.4)."""
    transport = httpx.MockTransport(handler)
    saved = os.environ.get("BRIDGENODE_WALLET_KEY")
    os.environ["BRIDGENODE_WALLET_KEY"] = (
        wallet_key or _test_wallet_key(_test_keypair()))
    try:
        return LLMClient(
            base_url="http://test/v1",
            transport=transport,
            **kwargs,
        )
    finally:
        if saved is None:
            os.environ.pop("BRIDGENODE_WALLET_KEY", None)
        else:
            os.environ["BRIDGENODE_WALLET_KEY"] = saved


# ── Handshake (z1) ───────────────────────────────────────────────────────────

def test_chat_handshake_success():
    """402 → partial TX → PAYMENT-SIGNATURE → retry → 200 + valid receipt."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    client_wallet = str(client_kp.pubkey())
    handler, seen = _make_server(fee_kp, client_wallet)

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        resp = client.chat("deepseek-v4-flash",
                           [{"role": "user", "content": "Hello!"}])

    assert resp["choices"][0]["message"]["content"] == "Hello!"
    assert len(seen) == 2
    assert seen[0]["has_payment"] is False
    assert seen[1]["has_payment"] is True
    assert seen[0]["body"] == seen[1]["body"]  # price-bind (§4.1.1)
    assert client.last_receipt["success"] is True
    assert client.last_receipt["network"] == NETWORK
    assert client.last_receipt["payer"] == client_wallet


def test_chat_with_mode_and_max_tokens():
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, _seen = _make_server(fee_kp, str(client_kp.pubkey()))

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        resp = client.chat("deepseek-v4-flash",
                           [{"role": "user", "content": "hi"}],
                           max_tokens=123, mode="auto")
    assert resp["choices"][0]["message"]["content"] == "Hello!"


def test_chat_mode_without_model_omits_model_key():
    """Z25: `model=None` + `mode` → body WITHOUT `model` key (not JSON null)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()))

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        resp = client.chat(None, [{"role": "user", "content": "hi"}],
                           mode="auto")

    assert resp["choices"][0]["message"]["content"] == "Hello!"
    for req in seen:
        assert "model" not in req["body"]
        assert req["body"]["mode"] == "auto"


def test_chat_string_prompt_same_body_as_list():
    """Z41: string prompt is converted to OpenAI messages — identical body
    as with list[dict] (protocol §8.4 example `chat(model, "Hello!")`)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()))

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        client.chat("deepseek-v4-flash", "Hello!")

    expected = [{"role": "user", "content": "Hello!"}]
    for req in seen:
        assert req["body"]["messages"] == expected
        assert req["body"]["model"] == "deepseek-v4-flash"


def test_server_error_raises():
    """Non-402 error → BridgenodeError with the server message (§5.6)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"message": "Unknown model", "type": "invalid_request_error",
                      "code": "model_not_found"}})

    with _make_client(handler) as client:
        with pytest.raises(BridgenodeError) as exc:
            client.chat("bogus-model", [{"role": "user", "content": "hi"}])
    assert exc.value.status_code == 400
    assert "Unknown model" in str(exc.value)


# ── Receipt verification (z2) ──────────────────────────────────────────────────

def test_receipt_missing_header_raises():
    """200 WITHOUT PAYMENT-RESPONSE receipt → error (not silent, §4.2 z2)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, _seen = _make_server(fee_kp, str(client_kp.pubkey()),
                                  no_receipt=True)

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="receipt missing"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])


def test_receipt_success_false_raises():
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, _seen = _make_server(
        fee_kp, str(client_kp.pubkey()),
        receipt_overrides={"success": False, "error_reason": "simulation_failed"})

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="Payment failed"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])


def test_402_error_reason_passthrough():
    """402 retry with PAYMENT-RESPONSE errorReason → the agent sees the reason
    (fix.md §3: the server errorReason is passed through, with guidance if known)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    client_wallet = str(client_kp.pubkey())

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.headers.get("PAYMENT-SIGNATURE"):
            env = _envelope(pay_to=client_wallet,
                            fee_payer=str(fee_kp.pubkey()))
            return httpx.Response(
                402,
                headers={"PAYMENT-REQUIRED": base64.b64encode(
                    json.dumps(env).encode()).decode()},
                json=env,
            )
        # Retry with payment → the server rejects (insufficient balance, §5.6)
        settle = SettleResponse(
            success=False, error_reason="insufficient_funds",
            transaction="", network=NETWORK, payer=client_wallet)
        return httpx.Response(
            402,
            headers={"PAYMENT-RESPONSE": base64.b64encode(
                settle.model_dump_json(by_alias=True,
                                       exclude_none=True).encode()).decode()},
            json={"error": {
                "message": "Payment verification failed: insufficient_funds",
                "type": "invalid_request_error", "code": "verify_failed"}},
        )

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError) as exc:
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])
    assert exc.value.status_code == 402
    assert "insufficient_funds" in str(exc.value)
    assert "fund your wallet" in str(exc.value)


def test_receipt_wrong_network_raises():
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, _seen = _make_server(
        fee_kp, str(client_kp.pubkey()),
        receipt_overrides={"network": "eip155:1"})

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="network mismatch"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])


def test_receipt_wrong_payer_raises():
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, _seen = _make_server(
        fee_kp, str(client_kp.pubkey()),
        receipt_overrides={"payer": "OtherWalletPubkey1111111111111111111111"})

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="payer mismatch"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])


def test_receipt_wrong_transaction_raises():
    """Forged TX (not the fee payer's signature over our message) → error."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    # Fake receipt: a valid 64-byte signature, but of another key (not the fee payer's)
    fake_kp = _test_keypair()
    fake_sig = fake_kp.sign_message(b"fake message")
    handler, _seen = _make_server(
        fee_kp, str(client_kp.pubkey()),
        receipt_overrides={"transaction": str(fake_sig)})

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="does not match our TX"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])


def test_receipt_wrong_amount_raises():
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, _seen = _make_server(
        fee_kp, str(client_kp.pubkey()),
        receipt_overrides={"amount": "9999"})

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="amount mismatch"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])


# ── Supported entry selection (Z23, §3.1) ───────────────────────────────

def test_payment_wrong_asset_blocks():
    """402 with another mint (not USDC) → error BEFORE signing (no TX, Z23)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    other_mint = "So11111111111111111111111111111111111111112"  # SOL mint
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()),
                                 asset=other_mint)

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="Unsupported payment asset"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])
    # No payment — retry with PAYMENT-SIGNATURE did not happen
    assert len(seen) == 1
    assert seen[0]["has_payment"] is False


def test_payment_wrong_network_blocks():
    """402 with another network → error BEFORE signing (no TX, Z23)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    other_network = "solana:4sGjMW1sUnHzSxGspuhpqLDx6wiyjNtZ"  # devnet
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()),
                                 network=other_network)

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="No supported payment requirement"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])
    # No payment — retry with PAYMENT-SIGNATURE did not happen
    assert len(seen) == 1
    assert seen[0]["has_payment"] is False


def test_payment_empty_accepts_blocks():
    """Empty accepts → error BEFORE signing (no TX, Z23)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()

    def handler(request: httpx.Request) -> httpx.Response:
        env = _envelope(pay_to=str(client_kp.pubkey()),
                        fee_payer=str(fee_kp.pubkey()))
        env["accepts"] = []
        return httpx.Response(
            402,
            headers={"PAYMENT-REQUIRED": base64.b64encode(
                json.dumps(env).encode()).decode()},
            json=env,
        )

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="No supported payment requirement"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])


# ── SIWX (z3, §5.7) ────────────────────────────────────────────────────────

def test_siwx_header_sent_on_402():
    """402 with SIWX challenge → client signs → retry with SIGN-IN-WITH-X."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()), siwx=True)

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        resp = client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])

    assert resp["choices"][0]["message"]["content"] == "Hello!"
    assert len(seen) == 3  # pradinis + SIWX retry + payment retry
    assert seen[0]["has_siwx"] is False
    assert seen[1]["has_siwx"] is True
    assert seen[1]["has_payment"] is False
    # Z22: payment retry WITHOUT SIGN-IN-WITH-X — official pattern "SIWX or
    # payment" (nonce is one-time, already used; §5.7)
    assert seen[2]["has_payment"] is True
    assert seen[2]["has_siwx"] is False


def test_siwx_no_challenge_normal_payment():
    """402 WITHOUT SIWX extension → no SIGN-IN-WITH-X, just payment (2 requests)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()), siwx=False)

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        resp = client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])

    assert resp["choices"][0]["message"]["content"] == "Hello!"
    assert len(seen) == 2
    assert seen[1]["has_siwx"] is False


def test_siwx_granted_direct_200():
    """Known agent: SIWX retry → 200 without payment (§5.7) — payload None, no receipt."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()),
                                 siwx=True, siwx_granted=True)

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        resp = client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])

    assert resp["choices"][0]["message"]["content"] == "Hello!"
    assert len(seen) == 2  # initial + SIWX retry; payment did not happen
    assert seen[1]["has_siwx"] is True
    assert seen[1]["has_payment"] is False


def test_siwx_header_cryptographically_valid():
    """SIGN-IN-WITH-X header — officially verifiable (Ed25519, §5.7)."""
    import asyncio

    from x402.extensions.sign_in_with_x import parse_siwx_header, verify_siwx_signature

    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()), siwx=True)

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])

    header = seen[1]["siwx_header"]
    assert header
    payload = parse_siwx_header(header)
    assert payload.address == str(client_kp.pubkey())
    assert payload.domain == "bridgenode.cc"
    assert payload.nonce == "nonce1234567890abcdef"
    result = asyncio.run(verify_siwx_signature(payload))
    assert result.is_valid is True
    assert result.payer == str(client_kp.pubkey())


# ── Spending policy (z2, fail-closed) ────────────────────────────────────────

def test_spending_max_per_call_blocks():
    """402 amount > MAX_PER_CALL → blocked BEFORE signing (no payment)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()),
                                 amount="60000")  # $0.06 > $0.05 default

    with _make_client(handler, _test_wallet_key(client_kp)) as client:
        with pytest.raises(BridgenodeError, match="max per call"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])
    # Payment was not signed — retry (2nd request) did not happen
    assert len(seen) == 1
    assert seen[0]["has_payment"] is False


def test_spending_daily_cap_blocks():
    """Daily cap exceeded → blocked (fail-closed)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()),
                                 amount="30000")  # $0.03

    with _make_client(handler, _test_wallet_key(client_kp),
                      max_per_call_usd=0.10, daily_cap_usd=0.05) as client:
        resp = client.chat("deepseek-v4-flash", [{"role": "user", "content": "a"}])
        assert resp["choices"][0]["message"]["content"] == "Hello!"
        # Second call would exceed the daily cap (0.03 + 0.03 > 0.05)
        with pytest.raises(BridgenodeError, match="daily cap"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "b"}])
    # Second call blocked before payment: 3 requests — (a, without payment),
    # (a, with payment), (b, without payment — blocked before retry)
    assert len(seen) == 3
    assert seen[-1]["has_payment"] is False


def test_spending_env_overrides(monkeypatch):
    """BRIDGENODE_MAX_PER_CALL/DAILY_CAP from env (configurable, §8.5)."""
    monkeypatch.setenv("BRIDGENODE_MAX_PER_CALL", "0.5")
    monkeypatch.setenv("BRIDGENODE_DAILY_CAP", "5.0")
    monkeypatch.setenv("BRIDGENODE_WALLET_KEY", _test_wallet_key(_test_keypair()))
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    with LLMClient(transport=transport) as client:
        assert client.max_per_call == 0.5
        assert client.daily_cap == 5.0


# ── Configuration (§8.4) ─────────────────────────────────────────────────────

def test_missing_wallet_key_raises(monkeypatch):
    monkeypatch.delenv("BRIDGENODE_WALLET_KEY", raising=False)
    with pytest.raises(BridgenodeError, match="BRIDGENODE_WALLET_KEY"):
        LLMClient(base_url="http://test/v1")


def test_default_base_url():
    from bridgenode_llm.client import BRIDGENODE_BASE_URL
    assert BRIDGENODE_BASE_URL == "https://bridgenode.cc/v1"


def test_timeouts_defaults():
    """§8.4: initial ≥ 30s (queue until 402), retry ≥ 113s (≤115s budget),
    total flow timeout ≥ initial + retry (Z42)."""
    with _make_client(lambda r: httpx.Response(500)) as client:
        assert client.initial_timeout >= 30.0
        assert client.retry_timeout >= 113.0
        assert client.retry_timeout <= 115.0
        assert client.flow_timeout >= client.initial_timeout + client.retry_timeout


def test_flow_timeout_triggers():
    """Z42: flow timeout exceeded → BridgenodeError BEFORE any request
    (total budget = 0 → immediate error, §8.4)."""
    fee_kp = _test_keypair()
    client_kp = _test_keypair()
    handler, seen = _make_server(fee_kp, str(client_kp.pubkey()))

    with _make_client(handler, _test_wallet_key(client_kp),
                      flow_timeout=0.0) as client:
        with pytest.raises(BridgenodeError, match="Flow timeout"):
            client.chat("deepseek-v4-flash", [{"role": "user", "content": "hi"}])
    # No request was sent
    assert len(seen) == 0


def test_env_override(monkeypatch):
    """BRIDGENODE_BASE_URL from .env — configurable (when the argument is not passed)."""
    monkeypatch.setenv("BRIDGENODE_BASE_URL", "https://alt.example/v1")
    monkeypatch.setenv("BRIDGENODE_WALLET_KEY", _test_wallet_key(_test_keypair()))
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    with LLMClient(transport=transport) as client:
        assert client.base_url == "https://alt.example/v1"
