# Contributor Guidance

This file is intentionally tracked. It captures repository-specific guardrails
for human and automated contributors working on the local telecom proof of
concept.

## Scope

- Applies to the whole repository.
- The upstream reference docs under `docs/freeswitch-docs`, `docs/kamailio-docs`, and `docs/rtpengine-docs` are optional local-only material and are ignored by Git.
- Before making FreeSWITCH, Kamailio, or RTPengine behaviour changes, restore the relevant local reference docs and base the change on them.

## Repository Direction

- Keep the checked-in runtime as a local single-node Docker Compose proof of concept.
- Do not implement partial local HA or imply production HA, cross-node dialog recovery, multi-region routing, production TLS termination, or production secret management exists in this repo.
- Document any production HA direction separately, and label it as not implemented unless the behaviour is actually present.
- `services/voice_gateway` is the publisher for structured voice-session metadata events.
- Browser clients subscribe to structured voice events over WebSocket for local demo observability.
- Raw audio stays on the SIP/WebRTC/RTP/media WebSocket path. Do not publish raw audio on the observability event channel.
- Treat the structured event stream as local diagnosis and demo observability, not production tracing, metrics, retention, or audit infrastructure.

## Kamailio

- Applies to files under `kamailio/`.
- Before editing `kamailio/kamailio.cfg` or route fragments under `kamailio/config/`, read `docs/kamailio-docs`.
- For any RTPengine-related behaviour, including `rtpengine_*` commands, also read `docs/rtpengine-docs`.
- Base behaviour changes on those docs and record the shortest relevant excerpts in review notes.
- Do not invent modules, parameters, route blocks, or behaviour.
- Keep changes minimal and scoped.
- Support both direct SIP traffic and WebSocket traffic.
- For WebRTC traffic, assume the client connects to nginx first, nginx terminates TLS, and nginx relays to Kamailio.
- Treat WebRTC signalling as two WebSocket hops: client -> nginx, then nginx -> Kamailio.
- Kamailio may run as multiple instances.
- SIP dialog state is stored in a database and any replica may receive in-dialog requests.
- Do not design WebSocket signalling for cross-replica failover.
- A WebSocket stays attached to one Kamailio replica. If that replica dies, the socket dies.
- For endpoints registered over WebSocket, use the `Path` information from `REGISTER` to route requests to the correct Kamailio instance.

## RTPengine

- Applies to files under `rtpengine/`.
- Before changing RTPengine configuration or behaviour, read `docs/rtpengine-docs`.
- Base behaviour changes on those docs and record the shortest relevant excerpts in review notes.
- Do not invent flags, config keys, or failover behaviour.
- Keep changes minimal and scoped.
- RTPengine runs as active/passive pairs.
- Kamailio selects a healthy active RTPengine at call start.
- Assume failover is between an active node and its paired passive node.
- Assume state for failover is backed by Redis.
- Do not design RTPengine handling around arbitrary cross-node rebalance.

## FreeSWITCH

- Applies to files under `freeswitch/`.
- Before changing files under `freeswitch/`, read `docs/freeswitch-docs`.
- Base behaviour changes on those docs and record the shortest relevant excerpts in review notes.
- Do not invent modules, variables, dialplan behaviour, or config directives.
- Do not replace or "fix" placeholders such as `{{name}}`.
- `freeswitch/freeswitch-entrypoint.sh` resolves those placeholders at startup.
- Keep changes minimal and scoped.
- FreeSWITCH resiliency is achieved via multiple independent instances.
- Do not design call handling that assumes replicas share live call state.
- Kamailio selects a healthy FreeSWITCH instance through the dispatcher module.
- Each SIP dialog handled by FreeSWITCH creates an outbound event-socket / outbound ESL listener connection to `services/voice_gateway`.
- Do not design call handling that assumes a call survives a FreeSWITCH failure.
- If FreeSWITCH dies, the call is lost.

## Browser Replay Fixtures

- `docs/replay-fixtures/multilingual_replay_trace.json` is a static browser replay fixture, not proof that the peer-to-peer translation chat path has been tested.
- When peer-to-peer translation coverage changes, update, rename, or replace that fixture as needed.
- Keep fixture references in `docs/replay-fixtures/README.md`, `README.md`, `web_client/call/call.js`, `scripts/webrtc_smoke.py`, and tests in sync.

## Done When

- Kamailio changes align with `docs/kamailio-docs`.
- RTPengine-related Kamailio changes align with `docs/rtpengine-docs`.
- RTPengine changes align with `docs/rtpengine-docs`.
- FreeSWITCH changes align with `docs/freeswitch-docs`.
- Any required deploy changes are reflected in `docker-compose.yml`.
- Runtime and docs continue to present the checked-in stack as a local single-node proof of concept unless production behaviour has actually been implemented.
- Voice-event changes keep raw audio on the media path and publish only structured metadata events through `services/voice_gateway`.
