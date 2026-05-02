# Web Client Responsibilities

This directory is a static browser client served directly by nginx from the
repository checkout. There is no frontend build step.

Current layout:

- `index.html` is the home page for choosing a demo.
- `call/call.html` is the main SIP.js/WebRTC demo page.
- `translation-demo/translation_demo.html` embeds the caller and translation
  peer in one guided live demo page.
- `call/call.js` contains call control, demo harness controls, simulated audio
  sources, voice-event rendering, and WebRTC quality telemetry.
- `translation-demo/translation_demo.js` orchestrates the embedded translation
  demo clients from browser events exposed by `call/call.js`. The embedded Bob
  client is configured as a silent receiver so the one-laptop demo does not feed
  Person A's microphone back into the peer leg, while Bob's speaker monitor
  remains audible by default for local demo feedback.
- `call/` and `translation-demo/` contain page-specific styling.
- `assets/sounds/` contains the browser UI tones used by `call/call.js`.
- `assets/sip-0.21.2.min.js` is the vendored SIP.js browser bundle.
  Its release URL, package metadata, and MIT license text are recorded in
  `assets/sip.min.js.LICENSE.txt`.
- `assets/sounds/README.md` records the current MP3 provenance gap; treat those
  sounds as local demo placeholders until they are replaced with clearly
  licensed assets.

The `callback_url` query parameter is smoke-test harness only. It is ignored
unless the page is launched with `harness=1`, `smoke_test=1`, or
`enable_harness_callback=1`.

When reviewing the file, the main sections are:

- Setup/query parsing and harness events near the top.
- Voice observability and transcript rendering before the media helpers.
- Simulated media sources and browser audio constraints in the middle.
- SIP.js call setup, call controls, and page initialization near the bottom.

If this became a maintained frontend, the first split should be by behavior:
observability panel, media-source helpers, SIP call controller, and demo
harness. For this repo, a no-build static client keeps the live demo path
simple and reviewable.
