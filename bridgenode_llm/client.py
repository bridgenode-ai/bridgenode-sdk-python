"""client.py — LLMClient: automatic x402 V2 handshake (§8.4, fix.md 4.2 steps 1–2).

Flow: POST /v1/chat/completions → 402 (PAYMENT-REQUIRED) → partial TX
(TransferChecked + Memo, §3.3–§3.4) via the official x402 SVM scheme →
PAYMENT-SIGNATURE → retry → 200.

step 2 — Receipt verification + spending policy (fail-closed, like ASG):
- After 200, the `PAYMENT-RESPONSE` receipt is verified: success=true,
  network = Solana mainnet, payer = our wallet, transaction = fee payer
  signature over OUR TX message (Free-Riding protection: a forged receipt
  does not pass), amount matches (if provided). Mismatch → error, not silence.
- Spending policy: `BRIDGENODE_MAX_PER_CALL` (0.05 USD) + `BRIDGENODE_DAILY_CAP`
  (1.0 USD) — checked BEFORE signing (402 amount); exceeded → blocked
  (no payment, error). Daily counter — in-memory (UTC date).

Rules:
- Key from `.env` (`BRIDGENODE_WALLET_KEY`) — no arguments, no interactive
  prompts (§8.4)
- Endpoint: `https://bridgenode.cc/v1` (configurable via
  `BRIDGENODE_BASE_URL` or argument)
- Two separate timeouts (§4.3/§8.4): initial ≥ 30s (queue until 402, §5.7),
  retry ≥ 113s (≤ 115s budget)
- Uses the official x402 client (x402ClientSync + ExactSvmScheme) — no custom
  payment code (taisykles.md: don't reinvent the wheel)
"""

from __future__ import annotations

import base64
import base64
import json
import logging
import os
import time
from datetime import date
from typing import Any

import httpx
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from x402 import x402ClientSync
from x402.extensions.sign_in_with_x import SIGN_IN_WITH_X, create_siwx_client_hook
from x402.http import x402HTTPClientSync
from x402.mechanisms.svm.exact.register import register_exact_svm_client
from x402.mechanisms.svm.signers import KeypairSigner
from x402.schemas import AbortResult

logger = logging.getLogger("bridgenode_llm")

BRIDGENODE_BASE_URL = "https://bridgenode.cc/v1"
NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"  # Solana mainnet (CAIP-2)
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # Solana mainnet USDC mint
USDC_DECIMALS = 6

# §5.7/§8.4: the initial request waits in the queue until 402 (30s queue + 30s
# window); retry with PAYMENT-SIGNATURE — up to the 115s budget (settle 20 + provider 30×3)
INITIAL_TIMEOUT_S = 60.0
RETRY_TIMEOUT_S = 115.0
# §8.4: total flow timeout ≥ sum of both (initial + retry ≈ 175s)
FLOW_TIMEOUT_S = INITIAL_TIMEOUT_S + RETRY_TIMEOUT_S

# Spending policy (fix.md 4.2 step 2): fail-closed, like ASG
DEFAULT_MAX_PER_CALL_USD = 0.05
DEFAULT_DAILY_CAP_USD = 1.0


class BridgenodeError(Exception):
    """BridgeNode SDK error — server error body (OpenAI format, §5.6)."""

    def __init__(self, message: str, status_code: int | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _error_message(resp: httpx.Response) -> str:
    """Server error message from the OpenAI-format body (§5.6)."""
    try:
        data = resp.json()
        err = data.get("error", {})
        if isinstance(err, dict) and err.get("message"):
            return err["message"]
    except (ValueError, AttributeError):
        pass
    return f"HTTP {resp.status_code}"


# fix.md §3: server errorReason → agent-readable hint (if known)
_ERROR_REASON_HINTS = {
    "insufficient_funds": " — fund your wallet with USDC",
}


class LLMClient:
    """BridgeNode client: AI inference with x402 payment (Solana USDC).

    Usage:
        from bridgenode_llm import LLMClient
        client = LLMClient()          # key from .env (BRIDGENODE_WALLET_KEY)
        resp = client.chat("deepseek-v4-flash", [
            {"role": "user", "content": "Hello!"}])

    step 2: verifies the PAYMENT-RESPONSE receipt after 200 (error if forged) and
    spending policy BEFORE payment (fail-closed).
    """

    def __init__(
        self,
        base_url: str | None = None,
        rpc_url: str | None = None,
        initial_timeout: float = INITIAL_TIMEOUT_S,
        retry_timeout: float = RETRY_TIMEOUT_S,
        flow_timeout: float = FLOW_TIMEOUT_S,
        max_per_call_usd: float | None = None,
        daily_cap_usd: float | None = None,
        env_path: str = ".env",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Creates the client. Key ONLY from `.env` (§8.4 — no arguments)."""
        if load_dotenv is not None:
            load_dotenv(env_path)

        self.wallet_key = os.environ.get("BRIDGENODE_WALLET_KEY")
        if not self.wallet_key:
            raise BridgenodeError(
                "BRIDGENODE_WALLET_KEY missing — set it in .env "
                "(your Solana wallet private key, base58)")

        self.base_url = (base_url or os.environ.get("BRIDGENODE_BASE_URL")
                         or BRIDGENODE_BASE_URL).rstrip("/")
        self.initial_timeout = initial_timeout
        self.retry_timeout = retry_timeout
        self.flow_timeout = flow_timeout

        # Spending policy (step 2): argument > env > default
        self.max_per_call = max_per_call_usd if max_per_call_usd is not None else float(
            os.environ.get("BRIDGENODE_MAX_PER_CALL", DEFAULT_MAX_PER_CALL_USD))
        self.daily_cap = daily_cap_usd if daily_cap_usd is not None else float(
            os.environ.get("BRIDGENODE_DAILY_CAP", DEFAULT_DAILY_CAP_USD))
        self._daily_spend: dict[str, float] = {}

        # Official x402 client + SVM signer (§8.2, P4)
        signer = KeypairSigner.from_base58(self.wallet_key)
        self._signer = signer
        self.wallet_address = signer.address
        self._x402 = x402ClientSync()
        register_exact_svm_client(
            self._x402, signer,
            rpc_url=rpc_url or os.environ.get("BRIDGENODE_RPC_URL"),
        )
        # Official practice (docs.x402.org lifecycle-hooks): spending policy
        # enforced IN CODE, next to the payment client — protection remains even
        # if env vars are missing; the hook runs BEFORE payload creation (§8.4)
        self._x402.on_before_payment_creation(self._spending_policy_hook)
        self._http_helper = x402HTTPClientSync(self._x402)
        self._http = httpx.Client(transport=transport)

        # Last verified receipt (PAYMENT-RESPONSE) — for introspection
        self.last_receipt: dict[str, Any] | None = None

    def _post(self, url: str, *, json: dict | None = None,
              headers: dict | None = None,
              timeout: float | None = None) -> httpx.Response:
        """POST with network errors → BridgenodeError (fix.md 11).

        httpx.ConnectError / TimeoutException would otherwise leak as raw
        httpx exceptions — the agent expects BridgenodeError everywhere.
        """
        try:
            return self._http.post(url, json=json, headers=headers,
                                   timeout=timeout)
        except httpx.ConnectError as exc:
            raise BridgenodeError(f"Connection failed: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise BridgenodeError(f"Request timed out: {exc}") from exc

    def _send_stream(self, req: httpx.Request) -> httpx.Response:
        """Streaming send with network errors → BridgenodeError (fix.md 11)."""
        try:
            return self._http.send(req, stream=True)
        except httpx.ConnectError as exc:
            raise BridgenodeError(f"Connection failed: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise BridgenodeError(f"Request timed out: {exc}") from exc

    # ── API ────────────────────────────────────────────────────────────────

    def chat(self, model: str | None, messages: str | list[dict],
             max_tokens: int | None = None, mode: str | None = None,
             stream: bool = False) -> dict[str, Any] | Any:
        """Single chat completion via the automatic x402 handshake (§4.1).

        item 41 (§8.4 example): ``messages`` can be a string (automatically
        converted to ``[{"role": "user", "content": ...}]``) or the
        OpenAI format (list[dict]) — the server still receives an OpenAI body.

        ``stream=True`` (optional, §5.5): returns an iterator of OpenAI SSE
        chunks (``dict`` with ``choices[].delta``), terminated by the
        ``[DONE]`` marker; the receipt is verified and spend recorded BEFORE
        the first chunk is yielded (billing boundary, §5.5). Default (False)
        returns the full JSON response — backward-compatible.

        step 2: spending policy BEFORE signing; PAYMENT-RESPONSE receipt
        verification after 200. Errors → BridgenodeError.
        """
        # item 41: string prompt → OpenAI messages format (client side, §8.4)
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        url = f"{self.base_url}/chat/completions"
        # item 25: `model` omitted when None — body without `model: null`
        # (when sending only `mode`, the server would get JSON null → possible 400)
        body: dict[str, Any] = {"messages": messages}
        if model is not None:
            body["model"] = model
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if mode is not None:
            body["mode"] = mode
        if stream:
            body["stream"] = True
        headers = {"Content-Type": "application/json"}
        payload = None  # PaymentPayload — for step 2 receipt verification

        # item 42 (§8.4): total flow timeout — the whole handshake (initial + SIWX
        # + payment retry) must fit within the budget; exceeded → BridgenodeError
        deadline = time.monotonic() + self.flow_timeout

        def _flow_timeout(call_timeout: float) -> float:
            """Remaining flow budget (min with call timeout); exceeded — error."""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgenodeError(
                    f"Flow timeout exceeded ({self.flow_timeout:.0f}s, §8.4)")
            return min(call_timeout, remaining)

        # 1) Initial request (no payment): queue until 402 (§5.7)
        # Client-side retry (§5.7 "Agentas retry'ina"): 503 (queue full /
        # wait timeout) and 429 (per-agent queue cap / 402 rate limit) are
        # retried with backoff — BEFORE any payment (nothing was charged,
        # retry is free). Retry-After header is honoured when present.
        # After payment: NO retry (single retry with PAYMENT-SIGNATURE only).
        retries = 3
        backoff_s = 1.0
        for attempt in range(retries + 1):
            resp = self._post(url, json=body, headers=headers,
                               timeout=_flow_timeout(self.initial_timeout))
            if resp.status_code not in (503, 429):
                break
            if attempt >= retries:
                break
            retry_after = None
            try:
                ra = resp.headers.get("Retry-After")
                if ra:
                    retry_after = float(ra)
            except (TypeError, ValueError):
                retry_after = None
            wait = min(retry_after if retry_after is not None
                       else backoff_s * (2 ** attempt), 15.0)
            time.sleep(wait)

        # 2) 402 → SIWX (step 3) first, then spending policy + payment (§5.7)
        if resp.status_code == 402:
            get_header, _body_data = self._resp_headers(resp)
            payment_required = self._http_helper.get_payment_required_response(
                get_header, resp.content)
            # SIWX: 402 with challenge → sign → retry with SIGN-IN-WITH-X
            # (official create_siwx_client_hook); auth fails → payment (§5.7)
            siwx_header = self._build_siwx_header(payment_required)
            if siwx_header:
                resp = self._post(
                    url, json=body, headers={**headers, SIGN_IN_WITH_X: siwx_header},
                    timeout=_flow_timeout(self.initial_timeout))

            if resp.status_code == 402:
                # Fallback to payment: spending policy BEFORE signing (fail-closed)
                get_header, _body_data = self._resp_headers(resp)
                payment_required = self._http_helper.get_payment_required_response(
                    get_header, resp.content)
                # item 23: fail-closed — pick a supported accepts entry
                # (exact + Solana mainnet + USDC, §3.1); the SDK does not check
                # asset — verified here, BEFORE signing (no TX for other mint/network)
                selected = self._select_payment_requirement(payment_required)
                amount_atomic = int(selected.amount)
                amount_usd = amount_atomic / (10 ** USDC_DECIMALS)
                self._check_spending(amount_usd)

                pay_headers, payload = self._http_helper.handle_402_response(
                    dict(resp.headers), resp.content)
                # item 22: payment retry WITHOUT SIGN-IN-WITH-X — official pattern
                # "SIWX or payment" (nonce is single-use, already consumed in
                # the SIWX retry; §5.7) — hook_headers only for the SIWX retry
                retry_headers = {**headers, **pay_headers}
                if stream:
                    # SSE (§5.5): stream the retry — headers are available
                    # immediately, the body is read chunk-by-chunk below
                    req = self._http.build_request(
                        "POST", url, json=body, headers=retry_headers,
                        timeout=_flow_timeout(self.retry_timeout))
                    resp = self._send_stream(req)
                else:
                    resp = self._post(url, json=body,
                                       headers=retry_headers,
                                       timeout=_flow_timeout(self.retry_timeout))

        if resp.status_code != 200:
            # fix.md §3: 402 with PAYMENT-RESPONSE — relay the server errorReason
            # (e.g., insufficient_funds) so the agent understands and acts
            if stream:
                resp.read()  # streamed body — materialize before parsing
            message = _error_message(resp)
            if resp.status_code == 402:
                try:
                    get_header, _ = self._resp_headers(resp)
                    settle = self._http_helper.get_payment_settle_response(get_header)
                    if not settle.success and settle.error_reason:
                        hint = _ERROR_REASON_HINTS.get(settle.error_reason, "")
                        message = f"Payment failed: {settle.error_reason}{hint}"
                except Exception:
                    pass  # no PAYMENT-RESPONSE — initial 402 (no payment)
            raise BridgenodeError(message, status_code=resp.status_code)

        # P3#19 (fix.md item 16): spend recorded ONLY after a successful 200 — retry
        # failure (5xx) → the server refunds, a pessimistic cap is unnecessary
        # (step 2: receipt verification BEFORE recording spend — if the receipt
        # is forged, the spend is NOT recorded, daily cap stays intact; R16/Ž16)
        if payload is not None:
            self._verify_receipt(payload, resp)
            self._record_spend(amount_usd)
        if stream:
            return self._iter_sse(resp)
        return resp.json()

    def _iter_sse(self, resp: httpx.Response) -> Any:
        """Yield OpenAI SSE chunks from a streamed response (§5.5).

        Each ``data:`` line is parsed as JSON and yielded as a dict; the
        stream ends at ``data: [DONE]``. The response is closed when the
        iterator is exhausted (or the caller stops iterating).
        """
        try:
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue  # keep-alive comment or partial line — skip
        finally:
            resp.close()

    def list_models(self) -> list[dict[str, Any]]:
        """List available models + prices from GET /v1/models (§5.2).

        Public endpoint — no payment, no authentication. Returns the
        ``data`` array (model id, pricing.prompt/completion,
        context_window, max_output_tokens). Errors → BridgenodeError.
        """
        url = f"{self.base_url}/models"
        try:
            resp = self._http.get(url, timeout=self.initial_timeout)
        except httpx.HTTPError as exc:
            raise BridgenodeError(f"models request failed: {exc}") from exc
        if resp.status_code != 200:
            raise BridgenodeError(
                _error_message(resp), status_code=resp.status_code)
        data = resp.json()
        return data.get("data", [])

    # ── SIWX (step 3, §5.7) ────────────────────────────────────────────────────

    def _build_siwx_header(self, payment_required) -> str | None:
        """SIGN-IN-WITH-X header from the 402 SIWX challenge (official hook, §5.7).

        Uses the official ``create_siwx_client_hook`` (P4) — our signer is a
        solders Keypair, so the signature is sync; the hook is async →
        ``asyncio.run``. Returns None if the 402 has no SIWX extension or the
        chain is unsupported.

        P3#18 (fix.md item 16): call from a RUNNING event loop → SIWX skipped
        (fallback to payment, §5.7) — ``asyncio.run`` would raise RuntimeError.
        Documented: the sync SDK targets non-async contexts.
        """
        import asyncio
        from types import SimpleNamespace

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no running loop — asyncio.run() is safe
        else:
            logger.warning(
                "SIWX skipped — called from a running event loop "
                "(fallback to payment, §5.7; P3#18)")
            return None

        try:
            hook = create_siwx_client_hook(self._signer)
            result = asyncio.run(hook(
                SimpleNamespace(payment_required=payment_required)))
        except Exception:
            return None  # no SIWX — fallback to payment (§5.7)
        if result is None:
            return None
        return result.headers.get(SIGN_IN_WITH_X)

    # ── Supported entry selection (item 23, §3.1) ──────────────────────────────

    def _select_payment_requirement(self, payment_required):
        """Fail-closed: supported accepts entry (exact + Solana mainnet + USDC).

        The SDK picks the FIRST entry whose scheme/network it supports (exact
        SVM, Solana mainnet) — it does not check the asset. So we verify here
        BEFORE signing: the first SDK-supported entry MUST be USDC; otherwise
        (different mint, different network, or empty accepts) →
        BridgenodeError — no TX (§3.1 "agent SDKs automatically select a
        supported entry").
        """
        for req in payment_required.accepts:
            if req.scheme != "exact":
                continue
            if str(req.network) != NETWORK:
                continue
            if req.asset != USDC:
                raise BridgenodeError(
                    f"Unsupported payment asset {req.asset} — expected USDC "
                    f"({USDC}); no payment made")
            return req
        raise BridgenodeError(
            "No supported payment requirement (exact + Solana mainnet + USDC) "
            "— no payment made")

    # ── Spending policy (step 2, fail-closed) ──────────────────────────────────

    def _spending_policy_hook(self, context) -> AbortResult | None:
        """Spending policy as a lifecycle hook (official practice, §8.4).

        Registered as ``on_before_payment_creation`` — runs BEFORE payment
        payload creation, next to the payment client. Returns AbortResult if
        the 402 amount exceeds MAX_PER_CALL or DAILY_CAP — no TX.
        """
        try:
            amount_atomic = int(context.selected_requirements.amount)
        except (AttributeError, TypeError, ValueError):
            return AbortResult(reason="Spending policy: invalid amount")
        amount_usd = amount_atomic / (10 ** USDC_DECIMALS)
        try:
            self._check_spending(amount_usd)
        except BridgenodeError as exc:
            return AbortResult(reason=str(exc))
        return None

    def _check_spending(self, amount_usd: float) -> None:
        """Fail-closed: MAX_PER_CALL or DAILY_CAP exceeded → error, no payment."""
        if amount_usd > self.max_per_call:
            raise BridgenodeError(
                f"Spending policy: ${amount_usd:.4f} exceeds max per call "
                f"${self.max_per_call:.2f} — blocked (no payment made)")
        today = date.today().isoformat()
        spent = self._daily_spend.get(today, 0.0)
        if spent + amount_usd > self.daily_cap:
            raise BridgenodeError(
                f"Spending policy: daily cap ${self.daily_cap:.2f} exceeded "
                f"(spent ${spent:.4f} + ${amount_usd:.4f}) — blocked (no payment made)")

    def _record_spend(self, amount_usd: float) -> None:
        """Records the spent amount (UTC date; in-memory)."""
        today = date.today().isoformat()
        self._daily_spend[today] = self._daily_spend.get(today, 0.0) + amount_usd

    # ── Receipt verification (step 2) ──────────────────────────────────────────

    @staticmethod
    def _resp_headers(resp: httpx.Response):
        """Case-insensitive header getter (like the x402 helpers)."""
        normalized = {k.upper(): v for k, v in resp.headers.items()}

        def get_header(name: str) -> str | None:
            return normalized.get(name.upper())

        return get_header, None

    def _verify_receipt(self, payload: Any, resp: httpx.Response) -> None:
        """Verifies the PAYMENT-RESPONSE receipt (§7, Free-Riding protection).

        Required: success=true, network = Solana mainnet, payer = our wallet,
        transaction = fee payer signature over OUR TX message (forged/incorrect
        receipt → error, not silence), amount matches (if provided).
        """
        get_header, _ = self._resp_headers(resp)
        try:
            settle = self._http_helper.get_payment_settle_response(get_header)
        except ValueError as exc:
            raise BridgenodeError(
                f"PAYMENT-RESPONSE receipt missing: {exc}") from exc

        if not settle.success:
            raise BridgenodeError(
                f"Payment failed: {settle.error_reason or 'unknown'}")

        if str(settle.network) != NETWORK:
            raise BridgenodeError(
                f"Receipt network mismatch: {settle.network} != {NETWORK}")

        if settle.payer != self.wallet_address:
            raise BridgenodeError(
                f"Receipt payer mismatch: {settle.payer} != {self.wallet_address}")

        # transaction = fee payer signature over our TX message (Free-Riding:
        # the server must prove it settled EXACTLY our TX)
        if payload is None:
            raise BridgenodeError("Receipt verification: payment payload missing")
        try:
            tx_b64 = payload.payload.get("transaction")
            tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
            # The client signed bytes([0x80]) + bytes(message) (SDK scheme)
            msg_bytes = bytes([0x80]) + bytes(tx.message)
            fee_payer = Pubkey.from_string(payload.accepted.extra["feePayer"])
            sig = Signature.from_string(settle.transaction)
        except Exception as exc:
            raise BridgenodeError(
                f"Receipt verification failed: {exc}") from exc

        if not sig.verify(fee_payer, msg_bytes):
            raise BridgenodeError(
                "Receipt transaction does not match our TX — possible fraud")

        if settle.amount is not None and int(settle.amount) != int(payload.accepted.amount):
            raise BridgenodeError(
                f"Receipt amount mismatch: {settle.amount} != {payload.accepted.amount}")

        self.last_receipt = {
            "success": settle.success,
            "transaction": settle.transaction,
            "network": str(settle.network),
            "payer": settle.payer,
            "amount": settle.amount,
        }

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
