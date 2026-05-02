## Minimal FreeSWITCH Configuration

The default "vanilla" configuration that comes with FreeSWITCH has
been designed as a showcase of the configurability of the myriad of
features that FreeSWITCH comes with out of the box. While it is very
helpful in tinkering with FreeSWITCH, it has a lot of extraneous stuff
enabled/configured for use in a production system. This configuration
aims to take the reverse stance -- it attempts to be a starting point
for configuring a new system by "adding" required features (instead of
removing them as one would do if one starts with the default
configuration).

This folder also includes the corresponding `modules.conf` that lists
the modules that are required to get this configuration working.

The Docker image includes local diagnostics such as packet/SIP inspection tools
for this proof of concept. They are intended for reviewer troubleshooting inside
the Compose network, not as a production hardening statement. The inbound
FreeSWITCH event socket is ACL-restricted to the Docker subnet and is not
host-published by the default Compose file.

### Test

The active local dialplan is exercised through Kamailio and FreeSWITCH:

- `7000` enters the assistant flow and opens an outbound event-socket
  connection to `services/voice_gateway`.
- `7100` enters the translation bridge flow and parks both translated legs.

Check the rendered configuration with:

```bash
uv run pytest tests/config/test_freeswitch.py
```

For the browser demo, start the stack and place calls through
`web_client/call/call.html`; the old direct `sip:stub` instruction is not the
current test path.

### Upstream

The configuration in this folder comes from
[mx4492/freeswitch-minimal-conf](https://github.com/mx4492/freeswitch-minimal-conf/commit/270941d6f2dca279f1bb8762d072940273d5ae11).

### Other Minimal Configurations

* [voxserv/freeswitch_conf_minimal](https://github.com/voxserv/freeswitch_conf_minimal)
