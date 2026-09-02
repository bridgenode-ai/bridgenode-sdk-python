# Changelog

## [0.2.20](https://github.com/bridgenode-ai/bridgenode-llm/compare/bridgenode-llm-v0.2.19...bridgenode-llm-v0.2.20) (2026-09-01)


### Bug Fixes

* **sdk-python:** daily cap keyed by UTC date, not local (B6) ([ed3cf04](https://github.com/bridgenode-ai/bridgenode-llm/commit/ed3cf049172f4a8c2199985226e00a2b5efe22f1))
* **sdk-python:** guard retry sleep against negative Retry-After (B5) ([3080b69](https://github.com/bridgenode-ai/bridgenode-llm/commit/3080b69c87d41990bb2f94fd94a3bd0e7a674127))
* **sdk:** map network read errors to BridgenodeError (B7) ([da3e028](https://github.com/bridgenode-ai/bridgenode-llm/commit/da3e028a86d0d0995977bc3838f4d3bb66c67fef))
* **tests:** zero warnings — close AlertManager httpx client in same loop; filter x402 upstream deprecation ([7597657](https://github.com/bridgenode-ai/bridgenode-llm/commit/759765702082487c3211638d51218168608f4d82))


### Reverts

* remove x402 filterwarnings — tests must SHOW warnings, not hide them (Leo 08-31) ([7033eb3](https://github.com/bridgenode-ai/bridgenode-llm/commit/7033eb3c9b2485d4554c9915d8e4ea3e8c4ce6f2))

## [0.2.19](https://github.com/bridgenode-ai/bridgenode-llm/compare/bridgenode-llm-v0.2.18...bridgenode-llm-v0.2.19) (2026-08-30)


### Bug Fixes

* **release:** sync manifest versions + CI check-version-drift (P1-1) ([4e581ad](https://github.com/bridgenode-ai/bridgenode-llm/commit/4e581ad6a31641e5a83ac27251de27441801c918))
* **sdk-python:** malformed 402 amount → BridgenodeError, no raw ValueError (B-4, §8.4) ([2298078](https://github.com/bridgenode-ai/bridgenode-llm/commit/22980780cd6763deac2ead552efb50c5d6df9fa1))
* **sdk-python:** x402 bounds &gt;=2.20.0,&lt;3 — handle_402_response requires request_url (B-5) ([48a2ba8](https://github.com/bridgenode-ai/bridgenode-llm/commit/48a2ba8492ff7cb5a24029cabdf9225b369de8e2))


### Documentation

* **readme:** add star CTA to sdk-python, sdk-ts, mcp READMEs (A1) ([17fd97b](https://github.com/bridgenode-ai/bridgenode-llm/commit/17fd97b31b80ce827fce115374adb1776cdff946))

## [0.2.18](https://github.com/bridgenode-ai/bridgenode-llm/compare/bridgenode-llm-v0.2.17...bridgenode-llm-v0.2.18) (2026-08-30)


### Bug Fixes

* **deps:** x402 bounds `>=2.20.0,<3` — handle_402_response requires `request_url` (2.19 breaks) (B-5, §8.4)

## [0.2.17](https://github.com/bridgenode-ai/bridgenode-llm/compare/bridgenode-llm-v0.2.16...bridgenode-llm-v0.2.17) (2026-08-26)


### Bug Fixes

* **release:** sync manifest versions + CI check-version-drift (P1-1) ([4e581ad](https://github.com/bridgenode-ai/bridgenode-llm/commit/4e581ad6a31641e5a83ac27251de27441801c918))
* **sdk-python:** malformed 402 amount → BridgenodeError, no raw ValueError (B-4, §8.4) ([2298078](https://github.com/bridgenode-ai/bridgenode-llm/commit/22980780cd6763deac2ead552efb50c5d6df9fa1))
