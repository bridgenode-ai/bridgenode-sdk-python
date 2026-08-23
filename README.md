# bridgenode-llm

[![PyPI version](https://img.shields.io/pypi/v/bridgenode-llm.svg)](https://pypi.org/project/bridgenode-llm/)
[![Downloads](https://img.shields.io/pypi/dm/bridgenode-llm.svg)](https://pypi.org/project/bridgenode-llm/)
[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-yellow.svg)](https://opensource.org/license/mit-0/)
[![Python versions](https://img.shields.io/pypi/pyversions/bridgenode-llm.svg)](https://pypi.org/project/bridgenode-llm/)
[![CI](https://img.shields.io/github/actions/workflow/status/bridgenode-ai/bridgenode-sdk-python/ci.yml)](https://github.com/bridgenode-ai/bridgenode-sdk-python/actions)
[![BridgeNode on x402-list](https://x402-list.com/badge/bridgenode.svg)](https://x402-list.com/services/bridgenode?utm_source=badge&utm_medium=referral&utm_campaign=embed)

BridgeNode Python SDK — AI inference for AI agents, no API keys. Pay per request with **Solana USDC via x402**. The payment handshake is fully automatic, and fees are sponsored — the agent needs no SOL.

## Features

- **No API keys, no registration** — pay per request with USDC
- **Automatic x402 handshake** — 402 → signed transaction → 200, fully hidden
- **Fee sponsorship (gasless)** — the agent needs zero SOL
- **Fail-closed spending policy** — per-call and daily caps checked before signing
- **Receipt verification** — every response receipt is verified (invalid → error)
- **Smart routing** — `mode: auto / eco / premium` (model selection per prompt complexity)
- **MCP support** — BridgeNode is also available as an MCP server

## BridgeNode vs traditional APIs

| | BridgeNode | Traditional AI APIs |
|---|---|---|
| Sign-up | None | Account + verification |
| API key | Not needed | Required (leak risk) |
| Billing | Pay-per-request (USDC) | Subscription / prepaid credits |
| Setup | Wallet only | SDK + key + quotas |
| Fees | Gas sponsored | Network fees on you |
| Spending control | Fail-closed caps | Account-level limits |
| Identity | Wallet signature (SIWX) | API key |

## Installation

```bash
pip install bridgenode-llm
```

Or install the full toolkit (SDK + CLI):

```bash
pip install bridgenode
```

## Usage

```python
from bridgenode_llm import LLMClient

client = LLMClient()  # key from .env (BRIDGENODE_WALLET_KEY)
resp = client.chat("deepseek-v4-flash", [
    {"role": "user", "content": "Hello!"}])
print(resp["choices"][0]["message"]["content"])
```

Everything is handled automatically: payment, retry, receipt verification. No API key required.

### Smart routing

```python
# Let BridgeNode pick the model: auto / eco / premium
resp = client.chat(None, "Explain quantum computing", mode="auto")
```

## Configuration (.env)

```bash
# Required — your Solana wallet private key (base58)
BRIDGENODE_WALLET_KEY=...

# Optional
# BRIDGENODE_BASE_URL=https://bridgenode.cc/v1
# BRIDGENODE_MAX_PER_CALL=0.05   # spending policy: max USD per call (fail-closed)
# BRIDGENODE_DAILY_CAP=1.0       # spending policy: max USD per day
```

## Security

- **Receipt verification:** after each response, the payment receipt is verified (success, network, payer, signature over your transaction, amount). Invalid receipt → `BridgenodeError`.
- **Spending policy (fail-closed):** `BRIDGENODE_MAX_PER_CALL` + `BRIDGENODE_DAILY_CAP` are checked BEFORE signing; exceeded → blocked, no payment.
- **SIWX:** automatic known-agent identification — falls back to payment if auth fails.

## Requirements

- Python ≥ 3.11
- A Solana wallet with a USDC token account (ATA)

## FAQ

**Do agents need SOL?** No — fees are sponsored. The agent pays only the USDC request amount.

**Is there an API key?** No. Your wallet signature is the identity (SIWX); payment is the access.

**What happens if the spending cap is exceeded?** The request is blocked before signing — fail-closed, no payment is made.

**Which models are available?** See https://bridgenode.cc/v1/models (live list with prices).

**How is the payment receipt verified?** Every response includes a receipt; the SDK verifies its signature, network, payer and amount — invalid receipts raise `BridgenodeError`.

**Can I use this in CI / headless agents?** Yes — no browser, no keys, just a wallet private key in env.

## Related packages

The BridgeNode toolkit on PyPI:

- `bridgenode-llm` — Python SDK (this package): https://pypi.org/project/bridgenode-llm
- `bridgenode-cli` — command-line interface: https://pypi.org/project/bridgenode-cli
- `bridgenode` — full toolkit (SDK + CLI): https://pypi.org/project/bridgenode
- `bridgenode-sdk` — SDK alias package: https://pypi.org/project/bridgenode-sdk
- `bridgenode-mcp` — MCP server package: https://pypi.org/project/bridgenode-mcp
- `bridgenode-skill` — agent skill package: https://pypi.org/project/bridgenode-skill

## Links

- Website: https://bridgenode.cc
- Models & prices: https://bridgenode.cc/v1/models
- Documentation: https://bridgenode.cc/llms.txt
- Protocol: x402 V2 (https://docs.x402.org)
