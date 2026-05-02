(function () {
  const PASS_THROUGH_QUERY_KEYS = [
    "ws_url",
    "voice_events_url",
    "audio_source",
    "enable_media_debug",
    "translate_peer",
  ];

  const DEFAULT_SOURCE_LANGUAGE = "en";
  const DEFAULT_TARGET_LANGUAGE = "fr";
  const DEFAULT_TRANSLATE_PEER = "sip:bob@voice.local";
  const LANGUAGE_NAMES = {
    en: "English",
    fr: "French",
  };

  const queryParams = new URLSearchParams(window.location.search);
  const elements = {
    back: document.getElementById("demo-back"),
    status: document.getElementById("demo-status"),
    startButtons: Array.from(document.querySelectorAll("[data-start-demo]")),
    hangup: document.getElementById("hangup-demo"),
    reset: document.getElementById("reset-demo"),
    routeTitle: document.getElementById("route-title"),
    instruction: document.getElementById("demo-instruction"),
    sourceText: document.getElementById("source-text"),
    translatedText: document.getElementById("translated-text"),
    translationLatency: document.getElementById("translation-latency"),
  };

  const flowState = {
    waitingForReceiver: false,
    selectedRoute: null,
  };

  const clients = {
    caller: {
      label: "Person A",
      frame: document.getElementById("caller-frame"),
      status: document.getElementById("caller-status"),
      events: [],
      waiters: [],
      boundWindow: null,
    },
    bob: {
      label: "Person B",
      frame: document.getElementById("bob-frame"),
      status: document.getElementById("bob-status"),
      events: [],
      waiters: [],
      boundWindow: null,
    },
  };

  const setDemoStatus = (message, state) => {
    if (!elements.status) {
      return;
    }
    elements.status.textContent = message;
    elements.status.dataset.state = state || "";
  };

  const setClientStatus = (client, message, state) => {
    if (!client.status) {
      return;
    }
    client.status.textContent = message;
    client.status.dataset.state = state || "";
  };

  const shortText = (value, maxLength) => {
    const text = String(value || "").trim();
    if (!text) {
      return "";
    }
    return text.length > maxLength ? text.slice(0, maxLength - 1) + "..." : text;
  };

  const formatLatency = (value) => {
    const ms = Number(value);
    if (!Number.isFinite(ms)) {
      return "n/a";
    }
    if (ms < 1000) {
      return Math.round(ms) + " ms";
    }
    return (ms / 1000).toFixed(2) + " s";
  };

  const languageName = (code) => {
    const clean = String(code || "").trim().toLowerCase();
    return LANGUAGE_NAMES[clean] || clean.toUpperCase() || "the source language";
  };

  const makeRoute = (sourceLanguage, targetLanguage) => ({
    sourceLanguage: String(sourceLanguage || DEFAULT_SOURCE_LANGUAGE).trim().toLowerCase(),
    targetLanguage: String(targetLanguage || DEFAULT_TARGET_LANGUAGE).trim().toLowerCase(),
  });

  const defaultRoute = () =>
    makeRoute(
      queryParams.get("source_language") || DEFAULT_SOURCE_LANGUAGE,
      queryParams.get("target_language") || DEFAULT_TARGET_LANGUAGE,
    );

  const routeLabel = (route) =>
    `${languageName(route.sourceLanguage)} to ${languageName(route.targetLanguage)}`;

  const routeFromButton = (button) =>
    makeRoute(button.dataset.sourceLanguage, button.dataset.targetLanguage);

  const selectedRoute = () => flowState.selectedRoute || defaultRoute();
  const queryFlag = (name) => {
    const value = String(queryParams.get(name) || "").trim().toLowerCase();
    return value === "1" || value === "true" || value === "yes" || value === "on";
  };

  const setStartButtonsDisabled = (disabled) => {
    elements.startButtons.forEach((button) => {
      button.disabled = disabled;
    });
  };

  const setRouteUi = (route, instruction) => {
    if (elements.routeTitle) {
      elements.routeTitle.textContent = route ? routeLabel(route) : "Choose a direction";
    }
    if (elements.instruction) {
      elements.instruction.textContent =
        instruction ||
        (route
          ? `When the call is live, speak ${languageName(route.sourceLanguage)} into this laptop.`
          : "Choose a direction to prepare the call.");
    }
  };

  const buildClientUrl = (page, defaults) => {
    const url = new URL(page, window.location.href);
    Object.entries(defaults).forEach(([key, value]) => {
      url.searchParams.set(key, value);
    });
    PASS_THROUGH_QUERY_KEYS.forEach((key) => {
      if (queryParams.has(key)) {
        url.searchParams.set(key, queryParams.get(key));
      }
    });
    return url.toString();
  };

  const forceClientParams = (urlString, params) => {
    const url = new URL(urlString);
    Object.entries(params).forEach(([key, value]) => {
      url.searchParams.set(key, value);
    });
    return url.toString();
  };

  const translatePeer = () =>
    queryParams.get("translate_peer") || DEFAULT_TRANSLATE_PEER;

  const callerUrl = (route) =>
    buildClientUrl("../call/call.html", {
      auto_start: "0",
      embedded: "1",
      make_call: "1",
      aor: "sip:demo-1001@voice.local",
      number_to_call: "7100",
      dial_number: "7100",
      contact_number: "7100",
      contact_name: "Translation service",
      first_letter: "A",
      translate_peer: translatePeer(),
      source_language: route.sourceLanguage,
      target_language: route.targetLanguage,
    });

  const bobUrl = (route) =>
    forceClientParams(
      buildClientUrl("../call/call.html", {
        auto_start: "0",
        embedded: "1",
        auto_answer: "1",
        make_call: "0",
        aor: translatePeer(),
        dial_number: "bob",
        contact_number: "bob",
        contact_name: "Bob",
        first_letter: "B",
        translate_peer: translatePeer(),
        source_language: route.sourceLanguage,
        target_language: route.targetLanguage,
        audio_source: "silence",
        remote_audio_muted: "0",
      }),
      {
        audio_source: "silence",
        remote_audio_muted: queryFlag("mute_receiver_audio") ? "1" : "0",
      },
    );

  const setStep = (name, state, detail) => {
    const step = document.querySelector('[data-step="' + name + '"]');
    const detailElement = document.getElementById("step-" + name + "-detail");
    if (step) {
      step.dataset.state = state || "";
    }
    if (detailElement && detail) {
      detailElement.textContent = detail;
    }
  };

  const resetPipeline = (route) => {
    flowState.waitingForReceiver = false;
    ["caller", "stt", "translation", "tts", "receiver"].forEach((name) => {
      setStep(name, "", "Waiting");
    });
    if (route) {
      setStep("caller", "", `Waiting for ${languageName(route.sourceLanguage)} speech`);
    }
    if (elements.sourceText) {
      elements.sourceText.textContent = "No transcript yet";
    }
    if (elements.translatedText) {
      elements.translatedText.textContent = "No translated text yet";
    }
    if (elements.translationLatency) {
      elements.translationLatency.textContent = "n/a";
    }
  };

  const resetTurnPipeline = () => {
    flowState.waitingForReceiver = false;
    ["caller", "stt", "translation", "tts", "receiver"].forEach((name) => {
      setStep(name, "", "Waiting");
    });
    const route = selectedRoute();
    setStep("caller", "", `Waiting for ${languageName(route.sourceLanguage)} speech`);
  };

  const resolveClientWaiters = (client, eventName, detail) => {
    const remaining = [];
    client.waiters.forEach((waiter) => {
      if (waiter.eventName === eventName) {
        clearTimeout(waiter.timer);
        waiter.resolve(detail);
      } else {
        remaining.push(waiter);
      }
    });
    client.waiters = remaining;
  };

  const waitForHarnessEvent = (client, eventName, timeoutMs) => {
    if (client.events.includes(eventName)) {
      return Promise.resolve();
    }
    const harness = getHarness(client);
    if (eventName === "registered" && harness?.getState?.().registered) {
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        client.waiters = client.waiters.filter((waiter) => waiter.timer !== timer);
        reject(new Error(client.label + " did not emit " + eventName));
      }, timeoutMs);
      client.waiters.push({ eventName, resolve, timer });
    });
  };

  const getHarness = (client) => {
    try {
      return client.frame.contentWindow?.CompanyCallHarness || null;
    } catch (error) {
      return null;
    }
  };

  const waitForHarness = (client, timeoutMs) =>
    new Promise((resolve, reject) => {
      const deadline = Date.now() + timeoutMs;
      const poll = () => {
        const harness = getHarness(client);
        if (harness?.start) {
          resolve(harness);
          return;
        }
        if (Date.now() >= deadline) {
          reject(new Error(client.label + " client did not become ready"));
          return;
        }
        setTimeout(poll, 100);
      };
      poll();
    });

  const updateClientFromHarnessEvent = (client, detail) => {
    const eventName = String(detail.event || "");
    if (!eventName) {
      return;
    }
    client.events.push(eventName);
    resolveClientWaiters(client, eventName, detail);

    if (eventName === "page_ready") {
      setClientStatus(client, "Ready", "");
    } else if (eventName === "page_starting") {
      setClientStatus(client, "Starting", "active");
    } else if (eventName === "registered") {
      setClientStatus(client, "Registered", "good");
    } else if (eventName === "outbound_call_started") {
      setClientStatus(client, "Calling", "active");
    } else if (eventName === "call_received") {
      setClientStatus(client, "Incoming", "active");
    } else if (eventName === "call_answered") {
      setClientStatus(client, "Connected", "good");
    } else if (eventName === "call_hangup") {
      setClientStatus(client, "Disconnected", "");
    } else if (eventName.endsWith("_failed") || eventName === "call_rejected") {
      setClientStatus(client, "Error", "error");
      setDemoStatus(client.label + " failed", "error");
    }
  };

  const updatePipelineFromVoiceEvent = (client, event) => {
    const type = event.type;
    const payload = event.payload || {};
    const reason = String(payload.reason || "");

    if (client === clients.caller && type === "user.speech_started") {
      resetTurnPipeline();
      setStep("caller", "active", "Speaking");
    } else if (client === clients.caller && type === "user.speech_stopped") {
      setStep("caller", "complete", "Stopped");
    } else if (client === clients.caller && type === "stt.partial") {
      setStep("caller", "complete", "Speech detected");
      setStep("stt", "active", shortText(payload.text, 42) || "Partial");
      if (elements.sourceText && payload.text) {
        elements.sourceText.textContent = shortText(payload.text, 120);
      }
    } else if (client === clients.caller && type === "stt.final") {
      setStep("stt", "complete", "Final transcript");
      setStep("translation", "active", "Waiting for translation");
      if (elements.sourceText && payload.text) {
        elements.sourceText.textContent = shortText(payload.text, 120);
      }
    } else if (type === "llm.request_started") {
      setStep("translation", "active", "Translating");
    } else if (type === "llm.request_finished") {
      setStep(
        "translation",
        "complete",
        "Done in " + formatLatency(payload.latency_ms),
      );
    } else if (type === "llm.final_text") {
      setStep("translation", "complete", "Translated");
      if (elements.translatedText && payload.text) {
        elements.translatedText.textContent = shortText(payload.text, 120);
      }
    } else if (type === "tts.started" || type === "tts.enqueue_started") {
      if (reason === "translation_response" || flowState.waitingForReceiver) {
        setStep("tts", "active", "Queueing speech");
        flowState.waitingForReceiver = true;
      }
    } else if (type === "tts.enqueued") {
      if (reason === "translation_response" || flowState.waitingForReceiver) {
        setStep("tts", "complete", "Queued in " + formatLatency(payload.enqueue_latency_ms));
        setStep("receiver", "active", "Waiting for audio");
        flowState.waitingForReceiver = true;
      }
    } else if (type === "agent.speaking_started") {
      if (reason === "translation_response" || flowState.waitingForReceiver) {
        setStep("receiver", "active", "Audio playing");
        flowState.waitingForReceiver = true;
      }
    } else if (
      client === clients.bob &&
      type === "webrtc.first_inbound_rtp" &&
      flowState.waitingForReceiver
    ) {
      setStep("receiver", "active", "Inbound RTP");
    } else if (type === "agent.speaking_stopped" || type === "tts.finished") {
      if (reason === "translation_response" || flowState.waitingForReceiver) {
        setStep("receiver", "complete", "Playback finished");
        flowState.waitingForReceiver = false;
      }
    } else if (type === "translation.latency") {
      if (elements.translationLatency) {
        elements.translationLatency.textContent = formatLatency(
          payload.translation_request_ms || payload.llm_request_ms,
        );
      }
      if (payload.tts_enqueue_ms !== undefined) {
        setStep("tts", "complete", "Queued in " + formatLatency(payload.tts_enqueue_ms));
      }
    } else if (type.endsWith(".error") || type === "system.error") {
      if (type.startsWith("stt.")) {
        setStep("stt", "error", "Error");
      } else if (type.startsWith("llm.")) {
        setStep("translation", "error", "Error");
      } else if (type.startsWith("tts.")) {
        setStep("tts", "error", "Error");
      }
    }
  };

  const updatePipelineFromMediaQuality = (client, detail) => {
    if (client !== clients.bob || !flowState.waitingForReceiver) {
      return;
    }
    const inbound = detail?.snapshot?.inbound || {};
    const packets = Number(inbound.packetsReceived || 0);
    const bytes = Number(inbound.bytesReceived || 0);
    if (packets > 0 || bytes > 0) {
      setStep("receiver", "active", "Inbound RTP");
    }
  };

  const bindFrameEvents = (client) => {
    let frameWindow;
    try {
      frameWindow = client.frame.contentWindow;
    } catch (error) {
      return;
    }
    if (!frameWindow || frameWindow === client.boundWindow) {
      return;
    }
    client.boundWindow = frameWindow;
    frameWindow.addEventListener("Company:harness-event", (event) => {
      updateClientFromHarnessEvent(client, event.detail || {});
    });
    frameWindow.addEventListener("Company:voice-event", (event) => {
      updatePipelineFromVoiceEvent(client, event.detail || {});
    });
    frameWindow.addEventListener("Company:media-quality", (event) => {
      updatePipelineFromMediaQuality(client, event.detail || {});
    });
  };

  const blankFrames = () => {
    Object.values(clients).forEach((client) => {
      client.events = [];
      client.waiters.forEach((waiter) => clearTimeout(waiter.timer));
      client.waiters = [];
      client.boundWindow = null;
      client.frame.src = "about:blank";
      setClientStatus(client, "Idle", "");
    });
  };

  const waitForFrameLoad = (client, url, timeoutMs) =>
    new Promise((resolve, reject) => {
      const onLoad = () => {
        client.frame.removeEventListener("load", onLoad);
        clearTimeout(timer);
        bindFrameEvents(client);
        waitForHarness(client, timeoutMs).then(resolve).catch(reject);
      };
      const timer = setTimeout(() => {
        client.frame.removeEventListener("load", onLoad);
        reject(new Error(client.label + " frame did not load"));
      }, timeoutMs);
      client.frame.addEventListener("load", onLoad);
      client.frame.src = url;
    });

  const loadFrames = async (route) => {
    blankFrames();
    resetPipeline(route);
    setRouteUi(route);
    await new Promise((resolve) => setTimeout(resolve, 0));
    Object.values(clients).forEach((client) => {
      setClientStatus(client, "Loading", "active");
    });
    await Promise.all([
      waitForFrameLoad(clients.caller, callerUrl(route), 15000),
      waitForFrameLoad(clients.bob, bobUrl(route), 15000),
    ]);
    Object.values(clients).forEach((client) => {
      setClientStatus(client, "Ready", "");
    });
  };

  const resetToReady = () => {
    blankFrames();
    const route = flowState.selectedRoute;
    resetPipeline(route);
    setRouteUi(route);
    setDemoStatus("Ready", "");
    setStartButtonsDisabled(false);
    elements.hangup.disabled = true;
  };

  const safeHangup = (client) => {
    const harness = getHarness(client);
    const state = harness?.getState?.();
    const hasCallStarted = client.events.some((name) =>
      [
        "outbound_call_started",
        "outbound_call_sent",
        "call_received",
        "call_answered",
      ].includes(name),
    );
    if (state?.callAnswered || hasCallStarted) {
      harness?.hangup?.();
    }
  };

  const waitForStop = (promise, timeoutMs) =>
    new Promise((resolve) => {
      const timer = setTimeout(resolve, timeoutMs);
      Promise.resolve(promise)
        .catch(() => undefined)
        .finally(() => {
          clearTimeout(timer);
          resolve();
        });
    });

  const stopClient = (client) => {
    try {
      const harness = getHarness(client);
      if (harness?.stop) {
        return harness.stop();
      }
      safeHangup(client);
    } catch (error) {
      console.error(error);
    }
    return Promise.resolve();
  };

  const terminateDemo = async () => {
    setDemoStatus("Leaving", "active");
    if (elements.back) {
      elements.back.disabled = true;
    }
    setStartButtonsDisabled(true);
    elements.hangup.disabled = true;
    await waitForStop(
      Promise.all(Object.values(clients).map((client) => stopClient(client))),
      2500,
    );
    Object.values(clients).forEach((client) => {
      client.waiters.forEach((waiter) => clearTimeout(waiter.timer));
      client.waiters = [];
      client.frame.src = "about:blank";
    });
  };

  const goBack = async () => {
    await terminateDemo();
    if (window.history.length > 1) {
      window.history.back();
    } else {
      window.location.href = "../";
    }
  };

  const startDemo = async (route) => {
    flowState.selectedRoute = route;
    setStartButtonsDisabled(true);
    elements.hangup.disabled = false;
    resetPipeline(route);
    setRouteUi(route, `Preparing ${routeLabel(route)}. The page will prompt you when to speak.`);
    try {
      setDemoStatus("Preparing clients", "active");
      await loadFrames(route);

      setDemoStatus("Starting Person B", "active");
      const bobHarness = getHarness(clients.bob) || (await waitForHarness(clients.bob, 15000));
      bobHarness.start();
      await waitForHarnessEvent(clients.bob, "registered", 20000);

      setDemoStatus("Starting Person A", "active");
      const callerHarness =
        getHarness(clients.caller) || (await waitForHarness(clients.caller, 15000));
      callerHarness.start();
      setDemoStatus("Connecting call", "active");
      Promise.all([
        waitForHarnessEvent(clients.caller, "call_answered", 30000),
        waitForHarnessEvent(clients.bob, "call_answered", 30000),
      ])
        .then(() => {
          setDemoStatus(
            `Live. Speak ${languageName(route.sourceLanguage)} into this laptop.`,
            "good",
          );
          setRouteUi(
            route,
            `Speak ${languageName(route.sourceLanguage)} now. The translation will be delivered to Bob.`,
          );
          setStep("caller", "active", `Say something in ${languageName(route.sourceLanguage)}`);
        })
        .catch((error) => setDemoStatus(error.message, "error"));
    } catch (error) {
      setStartButtonsDisabled(false);
      elements.hangup.disabled = true;
      setDemoStatus(error.message || "Unable to start demo", "error");
    }
  };

  const hangupDemo = () => {
    setDemoStatus("Hangup requested", "active");
    safeHangup(clients.caller);
    safeHangup(clients.bob);
  };

  const resetDemo = () => {
    safeHangup(clients.caller);
    safeHangup(clients.bob);
    resetToReady();
  };

  Object.values(clients).forEach((client) => {
    client.frame.addEventListener("load", () => {
      bindFrameEvents(client);
      if (getHarness(client)) {
        setClientStatus(client, "Ready", "");
      }
    });
  });

  elements.startButtons.forEach((button) => {
    button.addEventListener("click", () => startDemo(routeFromButton(button)));
  });
  elements.hangup.addEventListener("click", hangupDemo);
  elements.reset.addEventListener("click", resetDemo);
  if (elements.back) {
    elements.back.addEventListener("click", (event) => {
      event.preventDefault();
      goBack();
    });
  }

  if (queryParams.has("source_language") || queryParams.has("target_language")) {
    flowState.selectedRoute = defaultRoute();
  }
  resetToReady();
})();
