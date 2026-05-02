const Web = SIP.Web;
const ASSET_BASE_PATH = "../assets/";
const AUDIO_SOURCE_DEFAULT = "default";
const AUDIO_SOURCE_NOISE = "noise";
const AUDIO_SOURCE_TONE = "tone";
const AUDIO_SOURCE_SILENCE = "silence";
const AUDIO_SOURCE_DEVICE_PREFIX = "device:";
const REAL_MIC_AUDIO_CONSTRAINTS = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};
var simpleUser = undefined;
var isOnCall = false;
var isMakingCall = false;
var hasIncomingCall = false;
var localHangup = false;
var longKeyPressTimer = undefined;
var hasStarted = false;
var activeNoiseSource = undefined;

// Handle to the duration interval timer.
var intervalReceipt = undefined;
var muted = false;
var ringing = false;
var ringerTimer = undefined;
var ringtone = new Audio(ASSET_BASE_PATH + "sounds/ringtone.mp3");
var progressTimer = undefined;
var progress = new Audio(ASSET_BASE_PATH + "sounds/progress.mp3");
var activeToneSource = undefined;
var activeToneGain = undefined;
var activeSilenceSource = undefined;
var activeSilenceGain = undefined;

// The total number of seconds that the call is connected.
var totalSeconds = 0;
var autoHangupTimer = undefined;

const queryParams = new URLSearchParams(window.location.search);
const getQueryValue = (name) => {
  const value = queryParams.get(name);
  return value === null ? "" : value.trim();
};
const getQueryFlag = (name) => {
  const value = getQueryValue(name).toLowerCase();
  return value === "1" || value === "true" || value === "yes" || value === "on";
};
const getQueryFlagWithDefault = (name, defaultValue) => {
  return queryParams.has(name) ? getQueryFlag(name) : defaultValue;
};
const getQueryInt = (name) => {
  const value = Number(getQueryValue(name));
  return Number.isFinite(value) ? value : 0;
};
const sipHeaderValue = (value) => String(value || "").replace(/[\r\n]/g, "").trim();
if (getQueryFlag("embedded")) {
  document.body.classList.add("is-embedded");
}
const pageDefaults = window.CALL_CLIENT_DEFAULTS || {};
const setupDefaults = Object.assign(
  {
    wsUrl: "ws://127.0.0.1:5066",
    voiceEventsUrl: "ws://127.0.0.1:8000/events",
    voiceEventsSessionId: "",
    dialNumber: "7000",
    aor: "sip:demo-1001@voice.local",
    contactNumber: "7000",
    contactName: "",
    audioSource: AUDIO_SOURCE_DEFAULT,
    numberToCall: "7000",
    translatePeer: "sip:bob@voice.local",
    sourceLanguage: "en",
    targetLanguage: "fr",
    firstLetter: "A",
    makeCall: true,
    isDarkMode: false,
    enableMediaDebug: true,
    remoteAudioMuted: false,
    voiceEventsMock: false,
    voiceEventsMockTrace: "multilingual_replay",
    onCallErrorText: "Error",
    onCallDeclinedText: "Declined",
    callDisconnectedText: "Call disconnected",
    onRegisteredText: "Registered",
    onCallReceivedText: "",
    onCallAnsweredText: "Connected",
    callingText: "Calling...",
    muteText: "Mute",
    unmuteText: "Unmute",
  },
  pageDefaults.setup || {},
);
const setupState = Object.assign({}, setupDefaults);
const setupValueIds = {
  "ws-url": "wsUrl",
  "voice-events-url": "voiceEventsUrl",
  "voice-events-session-id": "voiceEventsSessionId",
  "dial-number": "dialNumber",
  aor: "aor",
  "contact-number-input": "contactNumber",
  "contact-name-input": "contactName",
  "audio-source": "audioSource",
  "number-to-call": "numberToCall",
  "translate-peer": "translatePeer",
  "source-language": "sourceLanguage",
  "target-language": "targetLanguage",
  "first-letter": "firstLetter",
  "on-call-error-text": "onCallErrorText",
  "on-call-declined-text": "onCallDeclinedText",
  "call-disconnected-text": "callDisconnectedText",
  "on-registered-text": "onRegisteredText",
  "on-call-received-text": "onCallReceivedText",
  "on-call-answered-text": "onCallAnsweredText",
  "calling-text": "callingText",
  "mute-text": "muteText",
  "unmute-text": "unmuteText",
  "voice-events-mock-trace": "voiceEventsMockTrace",
};
const setupCheckedIds = {
  "make-call": "makeCall",
  "is-dark-mode": "isDarkMode",
  "enable-media-debug": "enableMediaDebug",
  "remote-audio-muted": "remoteAudioMuted",
  "voice-events-mock": "voiceEventsMock",
};
const formatHarnessError = (error) => {
  if (!error) {
    return "unknown_error";
  }
  if (typeof error === "string") {
    return error;
  }
  if (error.message) {
    return error.message;
  }
  try {
    return JSON.stringify(error);
  } catch (e) {
    return String(error);
  }
};
const getMediaDebugReport = () => {
  if (
    window.MediaDebugProbe &&
    typeof window.MediaDebugProbe.getLatestReport === "function"
  ) {
    return window.MediaDebugProbe.getLatestReport();
  }
  return "";
};
const harnessCallbackEnabled = () =>
  getQueryFlag("harness") ||
  getQueryFlag("smoke_test") ||
  getQueryFlag("enable_harness_callback");
const automationState = {
  scenario: getQueryValue("scenario"),
  callbackUrl: harnessCallbackEnabled() ? getQueryValue("callback_url") : "",
  autoStart: getQueryFlagWithDefault("auto_start", pageDefaults.autoStart === true),
  autoAnswer: getQueryFlagWithDefault("auto_answer", pageDefaults.autoAnswer === true),
  autoHangupMs: getQueryInt("auto_hangup_ms"),
  closeOnComplete: getQueryFlag("close_on_complete"),
  started: false,
  lastEvent: "page_loaded",
  eventCount: 0,
  callAnswered: false,
  registered: false,
  localHangup: false,
  lastError: "",
};
window.CompanyCallHarnessState = automationState;

const updateHarnessState = (patch) => {
  Object.assign(automationState, patch);
  window.CompanyCallHarnessState = automationState;
};

const emitHarnessEvent = (eventType, detail) => {
  const payload = Object.assign(
    {
      event: eventType,
      at: new Date().toISOString(),
      callAnswered: automationState.callAnswered,
      eventCount: automationState.eventCount + 1,
      localHangup: automationState.localHangup,
      mediaDebugReport: getMediaDebugReport(),
      started: automationState.started,
    },
    detail || {},
  );
  updateHarnessState({
    eventCount: payload.eventCount,
    lastEvent: eventType,
    lastError: payload.error || automationState.lastError,
  });
  window.dispatchEvent(
    new CustomEvent("Company:harness-event", {
      detail: payload,
    }),
  );
  if (!automationState.callbackUrl) {
    return;
  }
  fetch(automationState.callbackUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  }).catch((error) => {
    console.error("Unable to send harness callback", error);
  });
};

const emitRegistered = (aor, source) => {
  if (automationState.registered) {
    return;
  }
  updateHarnessState({ registered: true });
  emitHarnessEvent("registered", {
    aor: aor,
    source: source,
  });
};

const clearAutoHangupTimer = () => {
  if (!autoHangupTimer) {
    return;
  }
  clearTimeout(autoHangupTimer);
  autoHangupTimer = undefined;
};

const scheduleAutoHangup = () => {
  clearAutoHangupTimer();
  if (automationState.autoHangupMs <= 0) {
    return;
  }
  autoHangupTimer = setTimeout(() => {
    if (!simpleUser || !isOnCall) {
      return;
    }
    localHangup = true;
    updateHarnessState({ localHangup: true });
    emitHarnessEvent("auto_hangup_started", {
      afterMs: automationState.autoHangupMs,
    });
    simpleUser.hangup().catch((error) => {
      emitHarnessEvent("auto_hangup_failed", {
        afterMs: automationState.autoHangupMs,
        error: formatHarnessError(error),
      });
      console.error(error);
    });
  }, automationState.autoHangupMs);
};

const formatDuration = (digits) => {
  return digits.toString().padStart(2, "0");
};

const stopRinging = () => {
  if (ringerTimer) {
    clearInterval(ringerTimer);
  }
  ringtone.pause();
  ringtone.currentTime = 0;
};

const stopProgress = () => {
  if (progressTimer) {
    clearInterval(progressTimer);
  }
  progress.pause();
  progress.currentTime = 0;
};

const hide = (element) => {
  element.style.visibility = "hidden";
};

const remove = (element) => {
  element.classList.add("hidden");
};

const show = (element) => {
  element.style.visibility = "visible";
  element.classList.remove("hidden");
};

const disableButton = (element) => {
  element.disabled = true;
  element.classList.add("disabled");
};

const enableButton = (element) => {
  element.disabled = false;
  element.classList.remove("disabled");
};

const VOICE_METRIC_NAMES = [
  "speechPartial",
  "finalLlm",
  "llmTts",
  "ttsDuration",
  "ttsPlayback",
  "finalAgent",
];

const createVoiceMetricStats = () => ({
  samples: {},
  order: [],
  latest: undefined,
  total: 0,
});

const voiceObservability = {
  socket: null,
  mockTimers: [],
  mockTraceName: "",
  events: [],
  bufferedEvents: [],
  transcript: [],
  partialTranscriptText: "",
  pendingUserEntry: null,
  pendingAgentEntry: null,
  paused: false,
  eventsExpanded: false,
  localSeq: 0,
  currentSessionId: "",
  markers: {},
  metricValues: {
    speechPartial: "n/a",
    finalLlm: "n/a",
    llmTts: "n/a",
    ttsDuration: "n/a",
    ttsPlayback: "n/a",
    finalAgent: "n/a",
  },
  metricStats: {
    speechPartial: createVoiceMetricStats(),
    finalLlm: createVoiceMetricStats(),
    llmTts: createVoiceMetricStats(),
    ttsDuration: createVoiceMetricStats(),
    ttsPlayback: createVoiceMetricStats(),
    finalAgent: createVoiceMetricStats(),
  },
};

const createVoiceEventsSessionId = () => {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return "browser-" + window.crypto.randomUUID();
  }
  return (
    "browser-" +
    Date.now().toString(36) +
    "-" +
    Math.random().toString(36).slice(2, 10)
  );
};

const ensureVoiceEventsSessionId = (options) => {
  if (!hasVoiceObservability()) {
    return "";
  }
  const current = String(options.voiceEventsSessionId || "").trim();
  const sessionId = current || createVoiceEventsSessionId();
  options.voiceEventsSessionId = sessionId;
  setupState.voiceEventsSessionId = sessionId;
  voiceObservability.currentSessionId = sessionId;
  setSetupValue("voice-events-session-id", sessionId);
  return sessionId;
};

const browserLatencyMarkers = {
  callStartedAt: undefined,
  getUserMediaStartedAt: undefined,
  emitted: {},
};

const voiceEventElements = {
  root: document.getElementById("voice-observability"),
  sessionLabel: document.getElementById("voice-session-label"),
  timeline: document.getElementById("voice-event-timeline"),
  transcriptHistory: document.getElementById("voice-transcript-history"),
  partialTranscript: document.getElementById("voice-partial-transcript"),
  clearButton: document.getElementById("voice-events-clear"),
  pauseButton: document.getElementById("voice-events-pause"),
  expandButton: document.getElementById("voice-events-toggle-expanded"),
  copyDebugButton: document.getElementById("voice-events-copy-debug"),
  exportButton: document.getElementById("voice-events-export"),
  statuses: {
    call: document.getElementById("voice-status-call"),
    media: document.getElementById("voice-status-media"),
    user: document.getElementById("voice-status-user"),
    agent: document.getElementById("voice-status-agent"),
  },
  quality: {
    score: document.getElementById("voice-quality-score"),
    jitter: document.getElementById("voice-quality-jitter"),
    loss: document.getElementById("voice-quality-loss"),
    rtt: document.getElementById("voice-quality-rtt"),
    note: document.getElementById("voice-quality-note"),
    scoreCard: document.querySelector('[data-quality="score"]'),
  },
  metrics: {
    speechPartial: {
      first: document.getElementById("voice-metric-speech-partial-first"),
      latest: document.getElementById("voice-metric-speech-partial"),
      average: document.getElementById("voice-metric-speech-partial-average"),
      count: document.getElementById("voice-metric-speech-partial-count"),
    },
    finalLlm: {
      first: document.getElementById("voice-metric-final-llm-first"),
      latest: document.getElementById("voice-metric-final-llm"),
      average: document.getElementById("voice-metric-final-llm-average"),
      count: document.getElementById("voice-metric-final-llm-count"),
    },
    llmTts: {
      first: document.getElementById("voice-metric-llm-tts-first"),
      latest: document.getElementById("voice-metric-llm-tts"),
      average: document.getElementById("voice-metric-llm-tts-average"),
      count: document.getElementById("voice-metric-llm-tts-count"),
    },
    ttsDuration: {
      first: document.getElementById("voice-metric-tts-duration-first"),
      latest: document.getElementById("voice-metric-tts-duration"),
      average: document.getElementById("voice-metric-tts-duration-average"),
      count: document.getElementById("voice-metric-tts-duration-count"),
    },
    ttsPlayback: {
      first: document.getElementById("voice-metric-tts-playback-first"),
      latest: document.getElementById("voice-metric-tts-playback"),
      average: document.getElementById("voice-metric-tts-playback-average"),
      count: document.getElementById("voice-metric-tts-playback-count"),
    },
    finalAgent: {
      first: document.getElementById("voice-metric-final-agent-first"),
      latest: document.getElementById("voice-metric-final-agent"),
      average: document.getElementById("voice-metric-final-agent-average"),
      count: document.getElementById("voice-metric-final-agent-count"),
    },
  },
  flowPanel: document.getElementById("voice-flow-panel"),
  flowGrid: document.getElementById("voice-flow-grid"),
  flowSteps: Array.from(document.querySelectorAll("[data-flow-step]")),
  flowEventRows: Array.from(document.querySelectorAll("[data-flow-event]")),
  flowDetails: {
    caller: document.getElementById("voice-flow-caller"),
    stt: document.getElementById("voice-flow-stt"),
    policy: document.getElementById("voice-flow-policy"),
    llm: document.getElementById("voice-flow-llm"),
    tts: document.getElementById("voice-flow-tts"),
  },
};

const hasVoiceObservability = () => Boolean(voiceEventElements.root);

const setVoiceStatus = (name, value, state) => {
  const element = voiceEventElements.statuses[name];
  if (!element) {
    return;
  }
  element.textContent = value;
  const chip = element.closest(".voice-status-chip");
  if (!chip) {
    return;
  }
  chip.classList.remove("is-active", "is-warn", "is-error");
  if (state) {
    chip.classList.add(state);
  }
};

const voiceMetricSampleKey = (sampleId) => {
  if (sampleId === undefined || sampleId === null || sampleId === "") {
    return "sample-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }
  return String(sampleId);
};

const voiceMetricAverage = (stats) => {
  if (!stats || !stats.order.length) {
    return undefined;
  }
  return stats.total / stats.order.length;
};

const renderVoiceMetric = (name) => {
  const cells = voiceEventElements.metrics[name] || {};
  const stats = voiceObservability.metricStats[name] || createVoiceMetricStats();
  const count = stats.order.length;
  const first = count ? stats.samples[stats.order[0]] : undefined;
  const latest = stats.latest;
  const average = voiceMetricAverage(stats);
  const latestText = formatVoiceLatency(latest);
  voiceObservability.metricValues[name] = latestText;
  if (cells.first) {
    cells.first.textContent = formatVoiceLatency(first);
  }
  if (cells.latest) {
    cells.latest.textContent = latestText;
  }
  if (cells.average) {
    cells.average.textContent = formatVoiceLatency(average);
  }
  if (cells.count) {
    cells.count.textContent = String(count);
  }
};

const setVoiceMetric = (name, milliseconds, sampleId) => {
  const value = Number(milliseconds);
  if (!Number.isFinite(value) || value < 0) {
    return;
  }
  let stats = voiceObservability.metricStats[name];
  if (!stats) {
    stats = createVoiceMetricStats();
    voiceObservability.metricStats[name] = stats;
  }
  const key = voiceMetricSampleKey(sampleId);
  if (Object.prototype.hasOwnProperty.call(stats.samples, key)) {
    stats.total -= stats.samples[key];
  } else {
    stats.order.push(key);
  }
  stats.samples[key] = value;
  stats.latest = value;
  stats.total += value;
  renderVoiceMetric(name);
};

const resetVoiceMetric = (name) => {
  voiceObservability.metricStats[name] = createVoiceMetricStats();
  voiceObservability.metricValues[name] = "n/a";
  renderVoiceMetric(name);
};

const voiceMetricRawMs = (value) => {
  return Number.isFinite(value) ? Math.round(value * 10) / 10 : null;
};

const voiceMetricDebugValues = () => {
  return VOICE_METRIC_NAMES.reduce((metrics, name) => {
    const stats = voiceObservability.metricStats[name] || createVoiceMetricStats();
    const count = stats.order.length;
    const first = count ? stats.samples[stats.order[0]] : undefined;
    const latest = stats.latest;
    const average = voiceMetricAverage(stats);
    metrics[name] = {
      first_ms: voiceMetricRawMs(first),
      latest_ms: voiceMetricRawMs(latest),
      average_ms: voiceMetricRawMs(average),
      first: formatVoiceLatency(first),
      latest: formatVoiceLatency(latest),
      average: formatVoiceLatency(average),
      count: count,
    };
    return metrics;
  }, {});
};

const formatQualityNumber = (value, digits, suffix) => {
  if (!Number.isFinite(value)) {
    return "n/a";
  }
  return value.toFixed(digits) + (suffix || "");
};

const classifyAudioQuality = (snapshot, delta, warnings) => {
  if (!snapshot) {
    return {
      level: "collecting",
      state: "",
      jitter: "n/a",
      loss: "n/a",
      rtt: "n/a",
      note: "Waiting for WebRTC stats.",
    };
  }
  const inbound = snapshot.inbound || {};
  const remoteInbound = snapshot.remoteInbound || {};
  const candidatePair = snapshot.candidatePair || {};
  const received = Number(inbound.packetsReceived || 0);
  const lost = Number(inbound.packetsLost || 0);
  const total = received + lost;
  const lossPct = total > 0 ? (lost / total) * 100 : undefined;
  const jitterMs = Number(inbound.jitterMs);
  const rttMs = Number(
    remoteInbound.roundTripTimeMs ?? candidatePair.currentRoundTripTimeMs
  );
  const warningList = Array.isArray(warnings) ? warnings : [];
  let level = "good";
  let state = "is-good";
  if (
    warningList.length ||
    (Number.isFinite(lossPct) && lossPct >= 3) ||
    (Number.isFinite(jitterMs) && jitterMs >= 30) ||
    (Number.isFinite(rttMs) && rttMs >= 400)
  ) {
    level = "degraded";
    state = "is-warn";
  }
  if (
    snapshot.connectionState === "failed" ||
    snapshot.iceConnectionState === "failed" ||
    (Number.isFinite(lossPct) && lossPct >= 10) ||
    (Number.isFinite(jitterMs) && jitterMs >= 80) ||
    (Number.isFinite(rttMs) && rttMs >= 900)
  ) {
    level = "poor";
    state = "is-error";
  }
  const rateNotes = [];
  if (delta) {
    if (Number.isFinite(delta.inboundKbps)) {
      rateNotes.push("in " + formatQualityNumber(delta.inboundKbps, 1, " kbps"));
    }
    if (Number.isFinite(delta.outboundKbps)) {
      rateNotes.push("out " + formatQualityNumber(delta.outboundKbps, 1, " kbps"));
    }
  }
  const noteParts = warningList.length ? warningList : rateNotes;
  return {
    level: level,
    state: state,
    jitter: formatQualityNumber(jitterMs, 1, " ms"),
    loss:
      lossPct === undefined
        ? "n/a"
        : formatQualityNumber(lossPct, 2, "%"),
    rtt: formatQualityNumber(rttMs, 1, " ms"),
    note: noteParts.length ? noteParts.join(", ") : "No media warnings.",
  };
};

const updateAudioQualitySummary = (detail) => {
  const quality = classifyAudioQuality(
    detail && detail.snapshot,
    detail && detail.delta,
    detail && detail.warnings
  );
  if (voiceEventElements.quality.score) {
    voiceEventElements.quality.score.textContent = quality.level;
  }
  if (voiceEventElements.quality.jitter) {
    voiceEventElements.quality.jitter.textContent = quality.jitter;
  }
  if (voiceEventElements.quality.loss) {
    voiceEventElements.quality.loss.textContent = quality.loss;
  }
  if (voiceEventElements.quality.rtt) {
    voiceEventElements.quality.rtt.textContent = quality.rtt;
  }
  if (voiceEventElements.quality.note) {
    voiceEventElements.quality.note.textContent = quality.note;
  }
  const card = voiceEventElements.quality.scoreCard;
  if (card) {
    card.classList.remove("is-good", "is-warn", "is-error");
    if (quality.state) {
      card.classList.add(quality.state);
    }
  }
};

const setActiveVoiceFlowPanelStep = (name) => {
  const activeStep = name || "";
  if (voiceEventElements.flowPanel) {
    voiceEventElements.flowPanel.dataset.activeStep = activeStep;
  }
  voiceEventElements.flowSteps.forEach((step) => {
    if (activeStep && step.dataset.flowStep === activeStep) {
      step.setAttribute("aria-current", "step");
      return;
    }
    step.removeAttribute("aria-current");
  });
};

const setVoiceFlowStep = (name, state, detail) => {
  if (!voiceEventElements.flowSteps.length) {
    return;
  }
  const step = voiceEventElements.flowSteps.find(
    (item) => item.dataset.flowStep === name
  );
  if (!step) {
    return;
  }
  step.classList.remove("is-active", "is-complete", "is-warn", "is-error");
  if (state) {
    step.classList.add(state);
  }
  const detailElement = voiceEventElements.flowDetails[name];
  if (detailElement && detail) {
    detailElement.textContent = detail;
  }
  if (state === "is-active") {
    setActiveVoiceFlowPanelStep(name);
    return;
  }
  if (voiceEventElements.flowPanel?.dataset.activeStep === name) {
    setActiveVoiceFlowPanelStep("");
  }
};

const activateVoiceFlowStep = (name, detail) => {
  voiceEventElements.flowSteps.forEach((step) => {
    step.classList.remove("is-active");
  });
  setVoiceFlowStep(name, "is-active", detail);
};

const completeVoiceFlowStep = (name, detail) => {
  setVoiceFlowStep(name, "is-complete", detail);
};

const warnVoiceFlowStep = (name, detail) => {
  setVoiceFlowStep(name, "is-warn", detail);
};

const errorVoiceFlowStep = (name, detail) => {
  setVoiceFlowStep(name, "is-error", detail);
};

const setVoiceFlowEventByType = (eventType, event, state, options) => {
  if (!voiceEventElements.flowEventRows.length) {
    return;
  }
  const clearActive = !options || options.clearActive !== false;
  if (clearActive) {
    voiceEventElements.flowEventRows.forEach((row) => {
      row.classList.remove("is-active");
    });
  }
  const row = voiceEventElements.flowEventRows.find(
    (item) => item.dataset.flowEvent === eventType
  );
  if (!row) {
    return;
  }
  row.classList.remove("is-complete", "is-warn", "is-error");
  if (state) {
    row.classList.add(state);
  }
  const time = row.querySelector("time");
  if (time) {
    time.textContent = shortVoiceTime(event);
    time.title =
      eventType === event.type ? event.ts : event.ts + " from " + event.type;
  }
};

const setVoiceFlowEvent = (event, state) => {
  setVoiceFlowEventByType(event.type, event, state);
};

const policyEvaluationDetail = (payload) => {
  const evaluationMs = numberPayloadValue(payload, "policy_evaluation_ms");
  const decisionMs = numberPayloadValue(payload, "policy_decision_ms");
  const semanticMs = numberPayloadValue(payload, "semantic_ms");
  if (evaluationMs !== undefined) {
    return "in " + formatVoiceLatency(evaluationMs);
  }
  if (decisionMs !== undefined && semanticMs !== undefined) {
    return (
      "semantic " +
      formatVoiceLatency(semanticMs) +
      ", decision " +
      formatVoiceLatency(decisionMs)
    );
  }
  if (decisionMs !== undefined) {
    return "decision in " + formatVoiceLatency(decisionMs);
  }
  return "";
};

const appendDetail = (label, detail) => {
  return detail ? label + " " + detail : label;
};

const resetVoiceFlowEventRows = () => {
  voiceEventElements.flowEventRows.forEach((row) => {
    row.classList.remove("is-active", "is-complete", "is-warn", "is-error");
    const time = row.querySelector("time");
    if (time) {
      time.textContent = "--";
      time.removeAttribute("title");
    }
  });
};

const resetVoiceFlow = () => {
  voiceEventElements.flowSteps.forEach((step) => {
    step.classList.remove("is-active", "is-complete", "is-warn", "is-error");
    step.removeAttribute("aria-current");
  });
  if (voiceEventElements.flowPanel) {
    voiceEventElements.flowPanel.dataset.activeStep = "";
  }
  Object.keys(voiceEventElements.flowDetails).forEach((key) => {
    if (voiceEventElements.flowDetails[key]) {
      voiceEventElements.flowDetails[key].textContent = "Waiting";
    }
  });
  resetVoiceFlowEventRows();
};

const updateVoiceExpandButton = () => {
  if (!voiceEventElements.expandButton) {
    return;
  }
  voiceEventElements.expandButton.textContent = voiceObservability.eventsExpanded
    ? "Collapse all"
    : "Expand all";
  voiceEventElements.expandButton.disabled = voiceObservability.events.length === 0;
};

const setVoiceTimelineExpanded = (expanded) => {
  voiceObservability.eventsExpanded = expanded;
  if (voiceEventElements.timeline) {
    voiceEventElements.timeline
      .querySelectorAll(".voice-event-row details")
      .forEach((details) => {
        details.open = expanded;
      });
  }
  updateVoiceExpandButton();
};

const buildVoiceDebugPayload = () => {
  const mediaDebugReport = getMediaDebugReport();
  const latencyMetricStats = voiceMetricDebugValues();
  return {
    exported_at: new Date().toISOString(),
    session_id: voiceObservability.currentSessionId,
    mock_trace: voiceObservability.mockTraceName,
    event_count: voiceObservability.events.length,
    latency_metrics: voiceObservability.metricValues,
    latency_metric_stats: latencyMetricStats,
    latency_data: {
      latest: voiceObservability.metricValues,
      stats: latencyMetricStats,
    },
    audio_quality: {
      score: voiceEventElements.quality.score?.textContent || "",
      jitter: voiceEventElements.quality.jitter?.textContent || "",
      loss: voiceEventElements.quality.loss?.textContent || "",
      rtt: voiceEventElements.quality.rtt?.textContent || "",
      note: voiceEventElements.quality.note?.textContent || "",
    },
    media_debug_report: mediaDebugReport,
    rtp_debug: mediaDebugReport,
    events: voiceObservability.events,
  };
};

const refreshMediaDebugReport = async () => {
  if (
    window.MediaDebugProbe &&
    typeof window.MediaDebugProbe.sampleOnce === "function"
  ) {
    if (
      typeof window.MediaDebugProbe.isRunning === "function" &&
      !window.MediaDebugProbe.isRunning()
    ) {
      return;
    }
    try {
      await window.MediaDebugProbe.sampleOnce();
    } catch (error) {
      console.warn("Unable to refresh media debug report", error);
    }
  }
};

const writeTextToClipboard = async (text) => {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) {
    throw new Error("clipboard copy failed");
  }
};

const setTemporaryButtonText = (button, text) => {
  if (!button) {
    return;
  }
  const originalText = button.dataset.originalText || button.textContent;
  button.dataset.originalText = originalText;
  button.textContent = text;
  window.setTimeout(() => {
    button.textContent = button.dataset.originalText;
  }, 1600);
};

const formatVoiceLatency = (milliseconds) => {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) {
    return "n/a";
  }
  if (milliseconds >= 1000) {
    return (milliseconds / 1000).toFixed(2) + "s";
  }
  return Math.round(milliseconds) + "ms";
};

const voiceEventTime = (event) => {
  const parsed = Date.parse(event.ts || "");
  return Number.isFinite(parsed) ? parsed : Date.now();
};

const padVoiceTimePart = (value, width) => {
  return String(value).padStart(width, "0");
};

const shortVoiceTime = (event) => {
  const date = new Date(voiceEventTime(event));
  return (
    padVoiceTimePart(date.getHours(), 2) +
    ":" +
    padVoiceTimePart(date.getMinutes(), 2) +
    ":" +
    padVoiceTimePart(date.getSeconds(), 2) +
    "." +
    padVoiceTimePart(date.getMilliseconds(), 3)
  );
};

const normalizeVoiceEvent = (event) => {
  const payload = event && typeof event.payload === "object" && event.payload
    ? event.payload
    : {};
  return {
    seq: Number.isFinite(Number(event && event.seq)) ? Number(event.seq) : 0,
    ts: event && event.ts ? String(event.ts) : new Date().toISOString(),
    session_id: event && event.session_id ? String(event.session_id) : "browser",
    call_id: event && event.call_id ? String(event.call_id) : "",
    source: event && event.source ? String(event.source) : "system",
    type: event && event.type ? String(event.type) : "system.debug",
    payload: payload,
  };
};

const makeLocalVoiceEvent = (source, type, payload) => {
  voiceObservability.localSeq -= 1;
  return normalizeVoiceEvent({
    seq: voiceObservability.localSeq,
    ts: new Date().toISOString(),
    session_id: voiceObservability.currentSessionId || "browser",
    call_id: "",
    source: source,
    type: type,
    payload: payload || {},
  });
};

const resetVoiceMetrics = () => {
  voiceObservability.markers = {};
  VOICE_METRIC_NAMES.forEach((name) => {
    resetVoiceMetric(name);
  });
};

const clearVoiceEvents = () => {
  if (!hasVoiceObservability()) {
    return;
  }
  voiceObservability.events = [];
  voiceObservability.bufferedEvents = [];
  voiceObservability.transcript = [];
  voiceObservability.partialTranscriptText = "";
  voiceObservability.pendingUserEntry = null;
  voiceObservability.pendingAgentEntry = null;
  voiceObservability.eventsExpanded = false;
  resetVoiceMetrics();
  resetVoiceFlow();
  setVoiceStatus("call", "idle", "");
  setVoiceStatus("media", "idle", "");
  setVoiceStatus("user", "no", "");
  setVoiceStatus("agent", "no", "");
  if (voiceEventElements.timeline) {
    voiceEventElements.timeline.innerHTML =
      '<div class="voice-empty-state">No events yet.</div>';
  }
  if (voiceEventElements.transcriptHistory) {
    voiceEventElements.transcriptHistory.innerHTML =
      '<div class="voice-empty-state">No transcript yet.</div>';
  }
  if (voiceEventElements.partialTranscript) {
    voiceEventElements.partialTranscript.textContent =
      "No partial transcript.";
    voiceEventElements.partialTranscript.classList.remove("is-active");
  }
  updateVoiceExpandButton();
};

const summarizeVoiceEvent = (event) => {
  const payload = event.payload || {};
  const text =
    payload.text ||
    payload.source_text ||
    payload.error?.message ||
    payload.detail?.error?.message ||
    payload.error ||
    payload.detail;
  const summaryText =
    text && typeof text === "object" ? JSON.stringify(text) : text;
  if (event.type === "policy.decision") {
    return payload.action || "decision";
  }
  if (event.type === "policy.evaluation_started") {
    return payload.is_final ? "final input" : "partial input";
  }
  if (event.type === "policy.evaluation_finished") {
    const timingDetail = policyEvaluationDetail(payload);
    return appendDetail(payload.action || "evaluated", timingDetail);
  }
  if (event.type === "policy.semantic_frame") {
    return [payload.speech_act, payload.intent].filter(Boolean).join(" / ") || "semantic frame";
  }
  if (event.type === "stt.speech_started") {
    return "provider speech start";
  }
  if (event.type === "stt.activity_started") {
    return "inferred speech start";
  }
  if (event.type === "stt.speech_stopped") {
    return "provider speech stop";
  }
  if (event.type === "stt.stream_started") {
    return "stream started";
  }
  if (event.type === "stt.stream_finished") {
    return payload.reason ? "stream finished: " + payload.reason : "stream finished";
  }
  if (event.type === "turn.started") {
    return payload.turn_id ? "turn " + payload.turn_id : "turn started";
  }
  if (event.type === "turn.latency") {
    return (
      "llm " +
      formatVoiceLatency(payload.llm_request_ms) +
      ", tts enqueue " +
      formatVoiceLatency(payload.tts_enqueue_ms) +
      ", start estimate " +
      formatVoiceLatency(payload.estimated_start_delay_ms) +
      ", playback " +
      formatVoiceLatency(payload.estimated_playback_ms)
    );
  }
  if (event.type === "translation.latency") {
    return (
      "translation " +
      formatVoiceLatency(payload.translation_request_ms || payload.llm_request_ms) +
      ", tts enqueue " +
      formatVoiceLatency(payload.tts_enqueue_ms)
    );
  }
  if (event.type === "media.stream_start_ack") {
    return "stream start " + (payload.command_success ? "ok" : "failed") +
      " in " + formatVoiceLatency(payload.command_latency_ms);
  }
  if (event.type === "llm.upstream_finished") {
    return "upstream done in " + formatVoiceLatency(payload.latency_ms);
  }
  if (event.type === "browser.call_started") {
    return payload.destination ? "call started: " + payload.destination : "call started";
  }
  if (event.type === "browser.sip_invite_sent") {
    return "INVITE sent in " + formatVoiceLatency(payload.call_elapsed_ms);
  }
  if (event.type === "browser.call_established") {
    return "established in " + formatVoiceLatency(payload.call_elapsed_ms);
  }
  if (event.type === "webrtc.get_user_media_ready") {
    return "capture ready in " + formatVoiceLatency(payload.capture_setup_ms);
  }
  if (event.type === "webrtc.ice_connected") {
    return "ICE connected in " + formatVoiceLatency(payload.call_elapsed_ms);
  }
  if (event.type === "webrtc.first_outbound_rtp") {
    return "first outbound RTP in " + formatVoiceLatency(payload.call_elapsed_ms);
  }
  if (event.type === "webrtc.first_inbound_rtp") {
    return "first inbound RTP in " + formatVoiceLatency(payload.call_elapsed_ms);
  }
  if (event.type === "user.barge_in_detected") {
    return summaryText ? "interruption: " + summaryText : "interruption";
  }
  if (event.type === "stt.partial") {
    return summaryText ? "partial: " + summaryText : "partial";
  }
  if (event.type === "stt.final") {
    return summaryText ? "final: " + summaryText : "final";
  }
  if (event.type === "stt.endpoint") {
    return "speech endpoint";
  }
  if (event.type === "llm.final_text") {
    return summaryText ? "response: " + summaryText : "response";
  }
  if (event.type === "llm.request_finished") {
    return "llm done in " + formatVoiceLatency(payload.latency_ms);
  }
  if (event.type === "tool.call_started") {
    return payload.tool_name
      ? "tool started: " + payload.tool_name
      : "tool started";
  }
  if (event.type === "tool.call_progress") {
    return payload.message || "tool progress";
  }
  if (event.type === "tool.call_completed") {
    return payload.tool_name
      ? "tool completed: " + payload.tool_name
      : "tool completed";
  }
  if (event.type === "tts.enqueued") {
    const startDelay = numberPayloadValue(payload, "estimated_start_delay_ms");
    const queueText = "queued in " + formatVoiceLatency(payload.enqueue_latency_ms);
    return startDelay === undefined
      ? queueText
      : queueText + ", audio in ~" + formatVoiceLatency(startDelay);
  }
  if (event.type === "tts.cancel_requested") {
    return "cancel requested";
  }
  if (event.type === "tts.break_sent") {
    return "break sent in " + formatVoiceLatency(payload.command_latency_ms);
  }
  if (event.type === "tts.started") {
    return summaryText ? "tts request: " + summaryText : "tts request";
  }
  if (event.type === "agent.speaking_started") {
    return summaryText ? "audio estimate: " + summaryText : "audio estimate";
  }
  if (event.type.endsWith(".error")) {
    return summaryText ? String(summaryText) : "error";
  }
  return summaryText ? String(summaryText) : "";
};

const renderVoiceTimelineEvent = (event) => {
  if (!voiceEventElements.timeline) {
    return;
  }
  const existingEmpty = voiceEventElements.timeline.querySelector(".voice-empty-state");
  if (existingEmpty) {
    existingEmpty.remove();
  }
  const row = document.createElement("div");
  row.className = "voice-event-row";

  const details = document.createElement("details");
  details.open = voiceObservability.eventsExpanded;
  const summary = document.createElement("summary");
  const main = document.createElement("div");
  main.className = "voice-event-main";

  const time = document.createElement("span");
  time.className = "voice-event-time";
  time.textContent = shortVoiceTime(event);
  time.title = event.ts;
  const source = document.createElement("span");
  source.className = "voice-event-source";
  source.textContent = event.source || "unknown";
  source.title = "source: " + (event.source || "unknown");
  const type = document.createElement("span");
  type.className = "voice-event-type";
  type.textContent = event.type;
  const shortSummary = document.createElement("span");
  const summaryText = summarizeVoiceEvent(event);
  shortSummary.className = "voice-event-summary";
  shortSummary.textContent = summaryText;
  if (!summaryText) {
    shortSummary.classList.add("is-empty");
    shortSummary.setAttribute("aria-hidden", "true");
  }
  main.appendChild(time);
  main.appendChild(source);
  main.appendChild(type);
  main.appendChild(shortSummary);
  summary.appendChild(main);

  const json = document.createElement("pre");
  json.className = "voice-event-json";
  json.textContent = JSON.stringify(event, null, 2);
  details.appendChild(summary);
  details.appendChild(json);
  row.appendChild(details);
  voiceEventElements.timeline.prepend(row);

  while (voiceEventElements.timeline.children.length > 200) {
    voiceEventElements.timeline.removeChild(voiceEventElements.timeline.lastChild);
  }
  updateVoiceExpandButton();
};

const renderVoiceTranscript = () => {
  if (!voiceEventElements.transcriptHistory) {
    return;
  }
  voiceEventElements.transcriptHistory.innerHTML = "";
  if (voiceObservability.transcript.length === 0) {
    voiceEventElements.transcriptHistory.innerHTML =
      '<div class="voice-empty-state">No transcript yet.</div>';
    return;
  }
  voiceObservability.transcript.slice(-80).forEach((entry) => {
    const line = document.createElement("div");
    const classes = ["voice-transcript-line", entry.role];
    if (entry.state === "partial" || entry.state === "thinking") {
      classes.push("is-" + entry.state);
    }
    line.className = classes.join(" ");
    const role = document.createElement("span");
    role.className = "voice-transcript-role";
    role.textContent = entry.role === "agent" ? "Agent" : "User";
    const body = document.createElement("span");
    body.className = "voice-transcript-body";
    if (entry.state === "thinking") {
      const dots = document.createElement("span");
      dots.className = "voice-waiting-dots";
      dots.setAttribute("aria-label", "...");
      for (let i = 0; i < 3; i += 1) {
        const dot = document.createElement("span");
        dot.textContent = ".";
        dots.appendChild(dot);
      }
      body.appendChild(dots);
    } else {
      body.textContent = entry.text;
    }
    line.appendChild(role);
    line.appendChild(body);
    voiceEventElements.transcriptHistory.appendChild(line);
  });
  voiceEventElements.transcriptHistory.scrollTop =
    voiceEventElements.transcriptHistory.scrollHeight;
};

const setPartialTranscriptText = (text) => {
  if (!voiceEventElements.partialTranscript) {
    return;
  }
  const trimmed = String(text || "").trim();
  voiceEventElements.partialTranscript.textContent = trimmed || "No partial transcript.";
  voiceEventElements.partialTranscript.classList.toggle("is-active", Boolean(trimmed));
};

const mergePartialTranscriptText = (current, incoming) => {
  const currentText = String(current || "");
  const incomingText = String(incoming || "");
  const currentTrimmed = currentText.trim();
  const incomingTrimmed = incomingText.trim();
  if (!incomingTrimmed) {
    return currentText;
  }
  if (incomingTrimmed === "Listening...") {
    return currentTrimmed || incomingTrimmed;
  }
  if (!currentTrimmed || currentTrimmed === "Listening...") {
    return incomingTrimmed;
  }
  if (incomingTrimmed.startsWith(currentTrimmed)) {
    return incomingTrimmed;
  }
  if (currentTrimmed.endsWith(incomingTrimmed)) {
    return currentTrimmed;
  }
  if (/^[,.;:!?)]/.test(incomingText)) {
    return currentTrimmed + incomingText;
  }
  if (/^\s/.test(incomingText) || /\s$/.test(currentText)) {
    return currentText + incomingText;
  }
  return currentTrimmed + incomingText;
};

const updatePendingUserTranscript = (text) => {
  const nextText = mergePartialTranscriptText(
    voiceObservability.partialTranscriptText,
    text
  );
  voiceObservability.partialTranscriptText = nextText;
  if (!voiceObservability.pendingUserEntry) {
    voiceObservability.pendingUserEntry = {
      role: "user",
      text: nextText,
      state: "partial",
    };
    voiceObservability.transcript.push(voiceObservability.pendingUserEntry);
  } else {
    voiceObservability.pendingUserEntry.text = nextText;
    voiceObservability.pendingUserEntry.state = "partial";
  }
  setPartialTranscriptText(nextText);
  renderVoiceTranscript();
};

const finalizePendingUserTranscript = (text) => {
  const finalText = String(
    text || voiceObservability.partialTranscriptText || ""
  ).trim();
  if (!finalText) {
    voiceObservability.partialTranscriptText = "";
    voiceObservability.pendingUserEntry = null;
    setPartialTranscriptText("");
    return;
  }
  if (voiceObservability.pendingUserEntry) {
    voiceObservability.pendingUserEntry.text = finalText;
    voiceObservability.pendingUserEntry.state = "final";
  } else {
    voiceObservability.transcript.push({
      role: "user",
      text: finalText,
      state: "final",
    });
  }
  voiceObservability.partialTranscriptText = "";
  voiceObservability.pendingUserEntry = null;
  setPartialTranscriptText("");
  renderVoiceTranscript();
};

const showPendingAgentResponse = () => {
  if (voiceObservability.pendingAgentEntry) {
    return;
  }
  voiceObservability.pendingAgentEntry = {
    role: "agent",
    text: "",
    state: "thinking",
  };
  voiceObservability.transcript.push(voiceObservability.pendingAgentEntry);
  renderVoiceTranscript();
};

const removePendingAgentResponse = () => {
  const pendingEntry = voiceObservability.pendingAgentEntry;
  if (!pendingEntry) {
    return false;
  }
  voiceObservability.transcript = voiceObservability.transcript.filter(
    (entry) => entry !== pendingEntry
  );
  voiceObservability.pendingAgentEntry = null;
  return true;
};

const TRANSCRIPT_TTS_REASONS = new Set([
  "assistant_greeting",
  "goodbye",
  "policy_confirmation",
  "policy_safe_fallback",
  "soft_interjection_checkin",
  "translation_goodbye",
  "turn_hold_clarification",
  "turn_hold_filler",
]);

const shouldShowTtsInTranscript = (event) => {
  if (event.type !== "tts.started") {
    return false;
  }
  const payload = event.payload || {};
  const reason = String(payload.reason || "");
  return TRANSCRIPT_TTS_REASONS.has(reason);
};

const numberPayloadValue = (payload, name) => {
  const value = Number(payload && payload[name]);
  return Number.isFinite(value) ? value : undefined;
};

const isFallbackSttEvent = (event) => {
  const payload = event?.payload || {};
  return (
    Boolean(payload.fallback) ||
    String(payload.provider || "") === "local_fallback" ||
    String(payload.fallback_reason || "") === "offline speech activity detector"
  );
};

const voiceEventSampleId = (event, fallback) => {
  const payload = event.payload || {};
  return (
    payload.turn_id ||
    payload.item_id ||
    payload.event_id ||
    fallback ||
    event.seq ||
    event.ts
  );
};

const updateVoiceMetrics = (event) => {
  const at = voiceEventTime(event);
  const payload = event.payload || {};
  if (event.type === "user.speech_started") {
    voiceObservability.markers.speechStartedAt = at;
    voiceObservability.markers.speechSampleId = voiceEventSampleId(
      event,
      "speech:" + at
    );
    voiceObservability.markers.firstPartialForTurn = false;
  }
  if (event.type === "stt.partial" && !voiceObservability.markers.firstPartialForTurn) {
    if (voiceObservability.markers.speechStartedAt) {
      setVoiceMetric(
        "speechPartial",
        at - voiceObservability.markers.speechStartedAt,
        voiceObservability.markers.speechSampleId || voiceEventSampleId(event)
      );
    }
    voiceObservability.markers.firstPartialForTurn = true;
  }
  if (event.type === "stt.final") {
    voiceObservability.markers.finalTranscriptAt = at;
    voiceObservability.markers.finalTranscriptSampleId = voiceEventSampleId(
      event,
      voiceObservability.markers.speechSampleId || "final:" + at
    );
  }
  if (event.type === "llm.request_started" && voiceObservability.markers.finalTranscriptAt) {
    voiceObservability.markers.llmRequestStartedAt = at;
    voiceObservability.markers.llmSampleId = voiceEventSampleId(
      event,
      voiceObservability.markers.finalTranscriptSampleId || "llm:" + at
    );
    setVoiceMetric(
      "finalLlm",
      at - voiceObservability.markers.finalTranscriptAt,
      voiceObservability.markers.llmSampleId
    );
  }
  if (event.type === "llm.request_finished") {
    const sampleId = voiceEventSampleId(
      event,
      voiceObservability.markers.llmSampleId || "llm:" + at
    );
    const latency = numberPayloadValue(payload, "latency_ms");
    if (latency !== undefined) {
      setVoiceMetric("llmTts", latency, sampleId);
    } else if (voiceObservability.markers.llmRequestStartedAt) {
      setVoiceMetric(
        "llmTts",
        at - voiceObservability.markers.llmRequestStartedAt,
        sampleId
      );
    }
  }
  if (event.type === "llm.final_text") {
    voiceObservability.markers.llmFinalAt = at;
    if (voiceObservability.markers.llmRequestStartedAt) {
      setVoiceMetric(
        "llmTts",
        at - voiceObservability.markers.llmRequestStartedAt,
        voiceEventSampleId(event, voiceObservability.markers.llmSampleId)
      );
    }
  }
  if (event.type === "tts.started") {
    voiceObservability.markers.ttsStartedAt = at;
    voiceObservability.markers.ttsSampleId = voiceEventSampleId(
      event,
      voiceObservability.markers.llmSampleId || "tts:" + at
    );
  }
  if (event.type === "tts.enqueued") {
    const sampleId = voiceEventSampleId(
      event,
      voiceObservability.markers.ttsSampleId || "tts:" + at
    );
    const enqueueLatency = numberPayloadValue(payload, "enqueue_latency_ms");
    if (enqueueLatency !== undefined) {
      setVoiceMetric("ttsDuration", enqueueLatency, sampleId);
    } else if (voiceObservability.markers.ttsStartedAt) {
      setVoiceMetric(
        "ttsDuration",
        at - voiceObservability.markers.ttsStartedAt,
        sampleId
      );
    }
    const playbackMs = numberPayloadValue(payload, "estimated_playback_ms");
    if (playbackMs !== undefined) {
      setVoiceMetric("ttsPlayback", playbackMs, sampleId);
    }
  }
  if (event.type === "agent.speaking_started" && voiceObservability.markers.finalTranscriptAt) {
    voiceObservability.markers.agentSpeakingStartedAt = at;
    setVoiceMetric(
      "finalAgent",
      at - voiceObservability.markers.finalTranscriptAt,
      voiceEventSampleId(event, voiceObservability.markers.ttsSampleId)
    );
  }
  if (event.type === "tts.finished") {
    const sampleId = voiceEventSampleId(
      event,
      voiceObservability.markers.ttsSampleId || "tts:" + at
    );
    const playbackMs = numberPayloadValue(payload, "estimated_playback_ms");
    if (playbackMs !== undefined) {
      setVoiceMetric("ttsPlayback", playbackMs, sampleId);
    } else if (voiceObservability.markers.agentSpeakingStartedAt) {
      setVoiceMetric(
        "ttsPlayback",
        at - voiceObservability.markers.agentSpeakingStartedAt,
        sampleId
      );
    }
  }
  if (event.type === "turn.latency") {
    const sampleId = voiceEventSampleId(event, "turn:" + at);
    const finalLlmMs = numberPayloadValue(payload, "final_to_llm_request_ms");
    const llmMs = numberPayloadValue(payload, "llm_request_ms");
    const ttsMs = numberPayloadValue(payload, "tts_enqueue_ms");
    const playbackMs = numberPayloadValue(payload, "estimated_playback_ms");
    const finalAudioMs =
      numberPayloadValue(payload, "final_to_estimated_audio_ms") ??
      numberPayloadValue(payload, "final_to_tts_enqueued_ms");
    if (finalLlmMs !== undefined) {
      setVoiceMetric("finalLlm", finalLlmMs, sampleId);
    }
    if (llmMs !== undefined) {
      setVoiceMetric("llmTts", llmMs, sampleId);
    }
    if (ttsMs !== undefined) {
      setVoiceMetric("ttsDuration", ttsMs, sampleId);
    }
    if (playbackMs !== undefined) {
      setVoiceMetric("ttsPlayback", playbackMs, sampleId);
    }
    if (finalAudioMs !== undefined) {
      setVoiceMetric("finalAgent", finalAudioMs, sampleId);
    }
  }
};

const updateVoiceStatusFromEvent = (event) => {
  const type = event.type;
  const payload = event.payload || {};
  if (type === "call.created") {
    setVoiceStatus("call", "created", "is-warn");
  } else if (type === "call.answered" || type === "call.connected") {
    setVoiceStatus("call", "connected", "is-active");
  } else if (type === "call.hangup") {
    setVoiceStatus("call", "disconnected", "is-error");
    setVoiceStatus("media", "disconnected", "is-error");
    setVoiceStatus("user", "no", "");
    setVoiceStatus("agent", "no", "");
  } else if (type === "browser.call_started") {
    setVoiceStatus("call", "dialing", "is-warn");
  } else if (type === "browser.call_established") {
    setVoiceStatus("call", "connected", "is-active");
  } else if (type === "media.stream_start_requested") {
    setVoiceStatus("media", "starting", "is-warn");
  } else if (type === "media.stream_start_ack") {
    setVoiceStatus("media", payload.command_success ? "stream requested" : "stream failed", payload.command_success ? "is-active" : "is-error");
  } else if (type === "media.connected") {
    setVoiceStatus("media", "connected", "is-active");
  } else if (type === "media.disconnected") {
    setVoiceStatus("media", "disconnected", "is-error");
  } else if (type === "user.speech_started") {
    setVoiceStatus("user", "yes", "is-active");
  } else if (type === "user.speech_stopped") {
    setVoiceStatus("user", "no", "");
  } else if (type === "agent.speaking_started") {
    setVoiceStatus("agent", "yes", "is-active");
  } else if (type === "agent.speaking_stopped") {
    setVoiceStatus("agent", "no", "");
  } else if (type === "stt.partial") {
    if (isFallbackSttEvent(event)) {
      setVoiceStatus("stt", "fallback listening", "is-warn");
    } else {
      setVoiceStatus("stt", "partial", "is-active");
    }
  } else if (type === "stt.final") {
    if (isFallbackSttEvent(event)) {
      setVoiceStatus("stt", "fallback/no transcript", "is-warn");
    } else {
      setVoiceStatus("stt", "final", "is-active");
    }
  } else if (type === "agent.thinking_started" || type === "llm.request_started") {
    setVoiceStatus("llm", "thinking", "is-warn");
  } else if (type === "llm.partial_text") {
    setVoiceStatus("llm", "streaming", "is-active");
  } else if (type === "tool.call_started" || type === "tool.call_progress") {
    setVoiceStatus("llm", "tool", "is-warn");
  } else if (type === "tool.call_completed") {
    setVoiceStatus("llm", "tool done", "is-active");
  } else if (type === "agent.thinking_finished" || type === "llm.final_text" || type === "llm.request_finished") {
    setVoiceStatus("llm", "done", "is-active");
  } else if (type === "tts.started" || type === "tts.enqueue_started") {
    setVoiceStatus("tts", "started", "is-active");
  } else if (type === "tts.enqueued") {
    setVoiceStatus("tts", "queued", "is-active");
  } else if (type === "tts.finished") {
    setVoiceStatus("tts", "finished", "");
  } else if (type === "tts.cancelled") {
    setVoiceStatus("tts", "cancelled", "is-warn");
  } else if (type === "user.barge_in_detected") {
    setVoiceStatus("barge", "detected", "is-warn");
  } else if (type === "policy.decision") {
    setVoiceStatus("policy", payload.action || "decision", "is-active");
  } else if (type === "policy.blocked_action") {
    setVoiceStatus("policy", "blocked", "is-error");
  }
  if (type.endsWith(".error") || type === "system.error") {
    const statusName = type.split(".")[0];
    if (voiceEventElements.statuses[statusName]) {
      setVoiceStatus(statusName, "error", "is-error");
    }
  }
};

const updateVoiceFlowFromEvent = (event) => {
  const type = event.type;
  const payload = event.payload || {};
  if (type === "stt.speech_started" || type === "stt.activity_started") {
    resetVoiceFlow();
    voiceObservability.lastSttStartSeq = event.seq;
    setVoiceFlowEvent(event, "is-active");
    activateVoiceFlowStep(
      "stt",
      type === "stt.activity_started" ? "Inferred speech start" : "Speech started"
    );
  } else if (type === "user.speech_started") {
    const followsSttStart =
      Number.isFinite(Number(event.seq)) &&
      voiceObservability.lastSttStartSeq === event.seq - 1;
    if (!followsSttStart) {
      resetVoiceFlow();
    }
    setVoiceFlowEvent(event, "is-active");
    setVoiceFlowEventByType(
      payload.source_event === "stt.speech_started"
        ? "stt.speech_started"
        : "stt.activity_started",
      event,
      "is-active",
      {
        clearActive: false,
      }
    );
    activateVoiceFlowStep("caller", "Speaking");
  } else if (type === "stt.partial") {
    const fallback = isFallbackSttEvent(event);
    setVoiceFlowEvent(event, fallback ? "is-warn" : "is-active");
    completeVoiceFlowStep("caller", "Speech detected");
    activateVoiceFlowStep(
      "stt",
      fallback
        ? "Offline detector"
        : "Partial: " + String(payload.text || "").slice(0, 28)
    );
  } else if (type === "stt.final") {
    const fallback = isFallbackSttEvent(event);
    setVoiceFlowEvent(event, fallback ? "is-warn" : "is-complete");
    if (fallback) {
      warnVoiceFlowStep("stt", "No transcript from fallback");
    } else {
      completeVoiceFlowStep("stt", "Final transcript");
    }
    activateVoiceFlowStep("policy", "Classifying");
  } else if (type === "stt.endpoint") {
    setVoiceFlowEvent(event, "is-complete");
    completeVoiceFlowStep("stt", "Endpoint detected");
  } else if (type === "user.speech_stopped") {
    setVoiceFlowEvent(event, "is-complete");
    completeVoiceFlowStep("caller", "Stopped");
  } else if (type === "policy.evaluation_started") {
    setVoiceFlowEvent(event, "is-active");
    activateVoiceFlowStep("policy", "Evaluating");
  } else if (type === "policy.evaluation_finished") {
    const timingDetail = policyEvaluationDetail(payload);
    setVoiceFlowEvent(event, "is-complete");
    activateVoiceFlowStep(
      "policy",
      appendDetail(payload.action || "Evaluated", timingDetail)
    );
  } else if (type === "policy.semantic_frame") {
    setVoiceFlowEvent(event, "is-complete");
    activateVoiceFlowStep(
      "policy",
      [payload.speech_act, payload.intent].filter(Boolean).join(" / ") ||
        "Semantic frame"
    );
  } else if (type === "policy.decision") {
    setVoiceFlowEvent(event, payload.should_interrupt ? "is-warn" : "is-complete");
    const timingDetail = policyEvaluationDetail(payload);
    activateVoiceFlowStep(
      "policy",
      appendDetail(payload.action || "Decision", timingDetail)
    );
    if (payload.action === "RESPOND") {
      if (payload.should_interrupt) {
        warnVoiceFlowStep(
          "policy",
          appendDetail("Interrupt then respond", timingDetail)
        );
      } else {
        completeVoiceFlowStep("policy", appendDetail("Respond", timingDetail));
      }
    } else if (payload.action === "CANCEL_TTS_AND_LISTEN") {
      warnVoiceFlowStep("policy", appendDetail("Cancel TTS", timingDetail));
    } else if (payload.action === "CLARIFY") {
      completeVoiceFlowStep("policy", appendDetail("Clarify", timingDetail));
    } else if (payload.action === "END_CALL") {
      completeVoiceFlowStep("policy", appendDetail("End call", timingDetail));
    }
  } else if (type === "turn.started") {
    setVoiceFlowEvent(event, "is-complete");
    completeVoiceFlowStep("policy", "Submitted");
    activateVoiceFlowStep("llm", "Preparing request");
  } else if (type === "llm.request_started") {
    completeVoiceFlowStep("policy", "Submitted");
    setVoiceFlowEvent(event, "is-active");
    activateVoiceFlowStep("llm", "Waiting for response");
  } else if (type === "llm.partial_text") {
    setVoiceFlowEvent(event, "is-active");
    activateVoiceFlowStep("llm", "Receiving text");
  } else if (type === "llm.upstream_finished") {
    const latency = numberPayloadValue(payload, "latency_ms");
    setVoiceFlowEvent(event, "is-complete");
    activateVoiceFlowStep(
      "llm",
      latency === undefined
        ? "Provider done"
        : "Provider done in " + formatVoiceLatency(latency)
    );
  } else if (type === "tool.call_started") {
    setVoiceFlowEvent(event, "is-active");
    activateVoiceFlowStep("llm", "Tool: " + (payload.tool_name || "lookup"));
  } else if (type === "tool.call_progress") {
    setVoiceFlowEvent(event, "is-active");
    activateVoiceFlowStep("llm", payload.message || "Tool running");
  } else if (type === "tool.call_completed") {
    setVoiceFlowEvent(event, "is-complete");
    activateVoiceFlowStep("llm", "Tool result ready");
  } else if (type === "llm.request_finished") {
    const latency = numberPayloadValue(payload, "latency_ms");
    setVoiceFlowEvent(event, "is-complete");
    completeVoiceFlowStep(
      "llm",
      latency === undefined
        ? "Gateway done"
        : "Gateway done in " + formatVoiceLatency(latency)
    );
  } else if (type === "llm.final_text") {
    setVoiceFlowEvent(event, "is-complete");
    completeVoiceFlowStep("llm", "Final text ready");
  } else if (type === "tts.enqueue_started" || type === "tts.started") {
    setVoiceFlowEvent(event, "is-active");
    activateVoiceFlowStep("tts", "Queueing speech");
  } else if (type === "tts.enqueued") {
    const latency = numberPayloadValue(payload, "enqueue_latency_ms");
    const startDelay = numberPayloadValue(payload, "estimated_start_delay_ms");
    setVoiceFlowEvent(event, "is-complete");
    completeVoiceFlowStep(
      "tts",
      latency === undefined
        ? "Speech queued"
        : "Queued in " +
          formatVoiceLatency(latency) +
          (startDelay === undefined
            ? ""
            : ", audio in ~" + formatVoiceLatency(startDelay))
    );
  } else if (type === "agent.speaking_started") {
    setVoiceFlowEvent(event, "is-active");
    activateVoiceFlowStep("tts", "Audio playing");
  } else if (type === "agent.speaking_stopped") {
    completeVoiceFlowStep("tts", payload.reason ? "Stopped: " + payload.reason : "Stopped");
  } else if (type === "tts.finished") {
    setVoiceFlowEvent(event, "is-complete");
    completeVoiceFlowStep("tts", "Finished");
  } else if (type === "user.barge_in_detected") {
    setVoiceFlowEvent(event, "is-warn");
    warnVoiceFlowStep("policy", "Barge-in");
    activateVoiceFlowStep("stt", "Barge-in");
  } else if (type === "tts.cancel_requested") {
    setVoiceFlowEvent(event, "is-warn");
    warnVoiceFlowStep("tts", "Stop requested");
  } else if (type === "tts.break_sent") {
    setVoiceFlowEvent(event, "is-warn");
    warnVoiceFlowStep("tts", "Break command");
  } else if (type === "tts.cancelled") {
    setVoiceFlowEvent(event, "is-warn");
    warnVoiceFlowStep("tts", "Cancelled");
  } else if (type.endsWith(".error") || type === "system.error") {
    const source = type.split(".")[0];
    const mapped =
      source === "llm" || source === "tool" || source === "tts" || source === "stt"
        ? source === "tool" ? "llm" : source
        : "policy";
    errorVoiceFlowStep(mapped, "Error");
  }
};

const updateVoiceTranscriptFromEvent = (event) => {
  const payload = event.payload || {};
  const text = String(payload.text || "");
  if (event.type === "stt.partial") {
    removePendingAgentResponse();
    updatePendingUserTranscript(text || "Listening...");
  }
  if (event.type === "stt.final") {
    finalizePendingUserTranscript(text);
  }
  if (event.type === "policy.decision") {
    const action = String(payload.action || payload.decision || "").toUpperCase();
    if (
      (action === "RESPOND" || action === "USER_TURN") &&
      payload.is_final !== false
    ) {
      showPendingAgentResponse();
    }
  }
  if (event.type === "llm.request_started" || event.type === "agent.thinking_started") {
    showPendingAgentResponse();
  }
  if (event.type === "llm.final_text") {
    removePendingAgentResponse();
    if (text) {
      voiceObservability.transcript.push({
        role: "agent",
        text: text,
        state: "final",
      });
    }
    renderVoiceTranscript();
  }
  if (shouldShowTtsInTranscript(event) && text) {
    removePendingAgentResponse();
    voiceObservability.transcript.push({
      role: "agent",
      text: text,
      state: "final",
    });
    renderVoiceTranscript();
  }
  if (event.type === "agent.thinking_finished" || event.type === "llm.error") {
    if (removePendingAgentResponse()) {
      renderVoiceTranscript();
    }
  }
};

const applyVoiceEvent = (event, storeEvent) => {
  if (!hasVoiceObservability()) {
    return;
  }
  const normalized = normalizeVoiceEvent(event);
  if (storeEvent !== false) {
    voiceObservability.events.push(normalized);
  }
  if (normalized.session_id && normalized.session_id !== "browser") {
    voiceObservability.currentSessionId = normalized.session_id;
  }
  if (voiceEventElements.sessionLabel) {
    const sessionLabel = normalized.session_id || "unknown";
    const callLabel = normalized.call_id ? " call " + normalized.call_id : "";
    voiceEventElements.sessionLabel.textContent = "session " + sessionLabel + callLabel;
  }
  updateVoiceStatusFromEvent(normalized);
  updateVoiceFlowFromEvent(normalized);
  updateVoiceTranscriptFromEvent(normalized);
  updateVoiceMetrics(normalized);
  renderVoiceTimelineEvent(normalized);
};

const ingestVoiceEvent = (event) => {
  if (!hasVoiceObservability()) {
    return;
  }
  const normalized = normalizeVoiceEvent(event);
  window.dispatchEvent(
    new CustomEvent("Company:voice-event", {
      detail: normalized,
    }),
  );
  if (voiceObservability.paused) {
    voiceObservability.events.push(normalized);
    voiceObservability.bufferedEvents.push(normalized);
    updateVoiceExpandButton();
    return;
  }
  applyVoiceEvent(normalized);
};

const recordLocalVoiceEvent = (source, type, payload) => {
  ingestVoiceEvent(makeLocalVoiceEvent(source, type, payload));
};

const browserNow = () => {
  if (window.performance && typeof window.performance.now === "function") {
    return window.performance.now();
  }
  return Date.now();
};

const browserElapsedMs = (startedAt, finishedAt) => {
  if (!Number.isFinite(startedAt) || !Number.isFinite(finishedAt)) {
    return undefined;
  }
  return Math.max(0, Math.round(finishedAt - startedAt));
};

const recordBrowserLatencyEvent = (type, payload, markerName) => {
  if (markerName && browserLatencyMarkers.emitted[markerName]) {
    return;
  }
  if (markerName) {
    browserLatencyMarkers.emitted[markerName] = true;
  }
  recordLocalVoiceEvent("webrtc", type, payload || {});
};

const resetBrowserLatencyMarkers = () => {
  browserLatencyMarkers.callStartedAt = undefined;
  browserLatencyMarkers.getUserMediaStartedAt = undefined;
  browserLatencyMarkers.emitted = {};
};

const updateBrowserWebrtcLatency = (detail) => {
  const snapshot = detail && detail.snapshot;
  if (!snapshot) {
    return;
  }
  const now = browserNow();
  const callElapsedMs = browserElapsedMs(browserLatencyMarkers.callStartedAt, now);
  if (
    snapshot.connectionState === "connected" ||
    snapshot.iceConnectionState === "connected" ||
    snapshot.iceConnectionState === "completed"
  ) {
    recordBrowserLatencyEvent(
      "webrtc.ice_connected",
      {
        connection_state: snapshot.connectionState,
        ice_connection_state: snapshot.iceConnectionState,
        call_elapsed_ms: callElapsedMs,
      },
      "ice_connected"
    );
  }
  const outboundPackets = Number(snapshot.outbound && snapshot.outbound.packetsSent);
  const outboundBytes = Number(snapshot.outbound && snapshot.outbound.bytesSent);
  if ((outboundPackets > 0 || outboundBytes > 0) && !browserLatencyMarkers.emitted.firstOutboundRtp) {
    recordBrowserLatencyEvent(
      "webrtc.first_outbound_rtp",
      {
        packets_sent: Number.isFinite(outboundPackets) ? outboundPackets : undefined,
        bytes_sent: Number.isFinite(outboundBytes) ? outboundBytes : undefined,
        call_elapsed_ms: callElapsedMs,
      },
      "firstOutboundRtp"
    );
  }
  const inboundPackets = Number(snapshot.inbound && snapshot.inbound.packetsReceived);
  const inboundBytes = Number(snapshot.inbound && snapshot.inbound.bytesReceived);
  if ((inboundPackets > 0 || inboundBytes > 0) && !browserLatencyMarkers.emitted.firstInboundRtp) {
    recordBrowserLatencyEvent(
      "webrtc.first_inbound_rtp",
      {
        packets_received: Number.isFinite(inboundPackets) ? inboundPackets : undefined,
        bytes_received: Number.isFinite(inboundBytes) ? inboundBytes : undefined,
        call_elapsed_ms: callElapsedMs,
      },
      "firstInboundRtp"
    );
  }
};

const buildVoiceEventsUrl = (url, sessionId) => {
  if (!sessionId) {
    return url;
  }
  try {
    const parsed = new URL(url, window.location.href);
    if (parsed.pathname.endsWith("/events")) {
      parsed.searchParams.set("session_id", sessionId);
      return parsed.toString();
    }
  } catch (e) {
    return url;
  }
  return url;
};

const stopVoiceEventMockStream = () => {
  voiceObservability.mockTimers.forEach((timer) => clearTimeout(timer));
  voiceObservability.mockTimers = [];
};

const VOICE_EVENT_MOCK_TRACE_FIXTURES = {
  multilingual_replay: [
    "/replay-fixtures/multilingual_replay_trace.json",
    "../../docs/replay-fixtures/multilingual_replay_trace.json",
    "/docs/replay-fixtures/multilingual_replay_trace.json",
  ],
};

const normalizeVoiceEventMockTraceName = (traceName) => {
  const normalized = String(traceName || "").trim() || "multilingual_replay";
  return VOICE_EVENT_MOCK_TRACE_FIXTURES[normalized]
    ? normalized
    : "multilingual_replay";
};

const normalizeVoiceEventMockTrace = (fixture, traceName) => {
  const normalized = normalizeVoiceEventMockTraceName(traceName || fixture.id);
  const events = Array.isArray(fixture.events) ? fixture.events : [];
  return {
    id: fixture.id || normalized,
    sessionId: fixture.session_id || "mock-" + normalized,
    callId: fixture.call_id || "mock-call",
    events: events.map((event, index) => ({
      seq: Number(event.seq || index + 1),
      atMs: Number(event.at_ms || 0),
      source: event.source || "system",
      type: event.type || "system.debug",
      payload: Object.assign({ mock: true }, event.payload || {}),
    })),
  };
};

const fetchVoiceEventMockTrace = async (traceName) => {
  const normalized = normalizeVoiceEventMockTraceName(traceName);
  const candidates = VOICE_EVENT_MOCK_TRACE_FIXTURES[normalized];
  let lastError = null;
  for (const url of candidates) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) {
        lastError = new Error("HTTP " + response.status + " for " + url);
        continue;
      }
      return normalizeVoiceEventMockTrace(await response.json(), normalized);
    } catch (e) {
      lastError = e;
    }
  }
  throw lastError || new Error("No mock trace fixture candidates configured.");
};

const disconnectVoiceEventStream = () => {
  if (!voiceObservability.socket) {
    return;
  }
  try {
    voiceObservability.socket.close();
  } catch (e) {
    console.warn("Unable to close voice event socket.", e);
  }
  voiceObservability.socket = null;
};

const connectVoiceEventStream = (url, sessionId) => {
  if (!hasVoiceObservability() || !url) {
    return;
  }
  stopVoiceEventMockStream();
  disconnectVoiceEventStream();
  const streamUrl = buildVoiceEventsUrl(url, sessionId);
  try {
    const socket = new WebSocket(streamUrl);
    voiceObservability.socket = socket;
    socket.addEventListener("open", () => {
      recordLocalVoiceEvent("webrtc", "system.debug", {
        message: "voice event stream connected",
        url: streamUrl,
      });
    });
    socket.addEventListener("message", (message) => {
      try {
        ingestVoiceEvent(JSON.parse(message.data));
      } catch (e) {
        recordLocalVoiceEvent("system", "system.warning", {
          message: "invalid voice event payload",
          error: formatHarnessError(e),
        });
      }
    });
    socket.addEventListener("close", () => {
      if (voiceObservability.socket === socket) {
        recordLocalVoiceEvent("webrtc", "system.warning", {
          message: "voice event stream disconnected",
          transport: "voice_events",
        });
        voiceObservability.socket = null;
      }
    });
    socket.addEventListener("error", () => {
      recordLocalVoiceEvent("system", "system.error", {
        message: "voice event stream error",
        url: streamUrl,
      });
    });
  } catch (e) {
    recordLocalVoiceEvent("system", "system.error", {
      message: "voice event stream failed",
      url: streamUrl,
      error: formatHarnessError(e),
    });
  }
};

const startVoiceEventMockStream = async (traceName) => {
  if (!hasVoiceObservability()) {
    return;
  }
  disconnectVoiceEventStream();
  stopVoiceEventMockStream();
  clearVoiceEvents();
  const normalizedTraceName = normalizeVoiceEventMockTraceName(traceName);
  voiceObservability.mockTraceName = normalizedTraceName;
  let trace;
  try {
    trace = await fetchVoiceEventMockTrace(normalizedTraceName);
  } catch (e) {
    recordLocalVoiceEvent("system", "system.error", {
      message: "voice event mock trace failed",
      trace: normalizedTraceName,
      error: formatHarnessError(e),
    });
    return;
  }
  voiceObservability.currentSessionId = trace.sessionId;
  const base = {
    session_id: trace.sessionId,
    call_id: trace.callId,
    source: "system",
    payload: { mock: true, trace: normalizedTraceName },
  };
  trace.events.forEach((event, index) => {
    const timer = setTimeout(() => {
      ingestVoiceEvent(
        normalizeVoiceEvent({
          ...base,
          seq: event.seq || index + 1,
          ts: new Date().toISOString(),
          source: event.source,
          type: event.type,
          payload: Object.assign({ trace: normalizedTraceName }, event.payload),
        })
      );
    }, event.atMs);
    voiceObservability.mockTimers.push(timer);
  });
};

const exportVoiceEvents = async () => {
  if (!hasVoiceObservability()) {
    return;
  }
  await refreshMediaDebugReport();
  const payload = buildVoiceDebugPayload();
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download =
    "voice-events-" + (voiceObservability.currentSessionId || "session") + ".json";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
};

const copyVoiceDebugBundle = async () => {
  if (!hasVoiceObservability()) {
    return;
  }
  await refreshMediaDebugReport();
  const text = JSON.stringify(buildVoiceDebugPayload(), null, 2);
  try {
    await writeTextToClipboard(text);
    setTemporaryButtonText(voiceEventElements.copyDebugButton, "Copied");
  } catch (error) {
    setTemporaryButtonText(voiceEventElements.copyDebugButton, "Copy failed");
    console.warn("Unable to copy debug bundle", error);
  }
};

const initVoiceObservabilityControls = () => {
  if (!hasVoiceObservability()) {
    return;
  }
  updateVoiceExpandButton();
  if (voiceEventElements.clearButton) {
    voiceEventElements.clearButton.addEventListener("click", clearVoiceEvents);
  }
  if (voiceEventElements.pauseButton) {
    voiceEventElements.pauseButton.addEventListener("click", () => {
      voiceObservability.paused = !voiceObservability.paused;
      voiceEventElements.pauseButton.textContent = voiceObservability.paused
        ? "Resume"
        : "Pause";
      if (!voiceObservability.paused) {
        const buffered = voiceObservability.bufferedEvents;
        voiceObservability.bufferedEvents = [];
        buffered.forEach((event) => applyVoiceEvent(event, false));
      }
    });
  }
  if (voiceEventElements.expandButton) {
    voiceEventElements.expandButton.addEventListener("click", () => {
      setVoiceTimelineExpanded(!voiceObservability.eventsExpanded);
    });
  }
  if (voiceEventElements.copyDebugButton) {
    voiceEventElements.copyDebugButton.addEventListener("click", copyVoiceDebugBundle);
  }
  if (voiceEventElements.exportButton) {
    voiceEventElements.exportButton.addEventListener("click", exportVoiceEvents);
  }
};

var audioContext = new AudioContext();

const resumeAudioContext = () => {
  if (audioContext.state === "suspended") {
    audioContext.resume().catch(() => {});
  }
};

const stopNoiseStream = () => {
  if (!activeNoiseSource) {
    return;
  }
  try {
    activeNoiseSource.stop();
  } catch (e) {
    console.warn("Unable to stop noise source.", e);
  }
  activeNoiseSource.disconnect();
  activeNoiseSource = undefined;
};

const createNoiseStream = () => {
  resumeAudioContext();
  stopSimulatedAudio();

  const sampleRate = audioContext.sampleRate;
  const noiseBuffer = audioContext.createBuffer(1, sampleRate, sampleRate);
  const output = noiseBuffer.getChannelData(0);
  for (let i = 0; i < output.length; i++) {
    output[i] = Math.random() * 2 - 1;
  }

  const source = audioContext.createBufferSource();
  source.buffer = noiseBuffer;
  source.loop = true;

  const gainNode = new GainNode(audioContext, { gain: 0.15 });
  const destination = audioContext.createMediaStreamDestination();
  source.connect(gainNode);
  gainNode.connect(destination);
  source.start(0);

  activeNoiseSource = source;
  return destination.stream;
};

const stopToneStream = () => {
  if (!activeToneSource) {
    return;
  }
  try {
    activeToneSource.stop();
  } catch (e) {
    console.warn("Unable to stop tone source.", e);
  }
  activeToneSource.disconnect();
  if (activeToneGain) {
    activeToneGain.disconnect();
  }
  activeToneSource = undefined;
  activeToneGain = undefined;
};

const stopSilenceStream = () => {
  if (!activeSilenceSource) {
    return;
  }
  try {
    activeSilenceSource.stop();
  } catch (e) {
    console.warn("Unable to stop silence source.", e);
  }
  activeSilenceSource.disconnect();
  if (activeSilenceGain) {
    activeSilenceGain.disconnect();
  }
  activeSilenceSource = undefined;
  activeSilenceGain = undefined;
};

const createSilenceStream = () => {
  resumeAudioContext();
  stopSimulatedAudio();

  const source = new OscillatorNode(audioContext, {
    frequency: 440,
    type: "sine",
  });
  const gainNode = new GainNode(audioContext, { gain: 0 });
  const destination = audioContext.createMediaStreamDestination();
  source.connect(gainNode);
  gainNode.connect(destination);
  source.start(0);

  activeSilenceSource = source;
  activeSilenceGain = gainNode;
  return destination.stream;
};

const createToneSpeechStream = () => {
  resumeAudioContext();
  stopSimulatedAudio();

  const source = new OscillatorNode(audioContext, {
    frequency: 440,
    type: "sine",
  });
  const gainNode = new GainNode(audioContext, { gain: 0 });
  const destination = audioContext.createMediaStreamDestination();
  source.connect(gainNode);
  gainNode.connect(destination);

  const startAt = audioContext.currentTime + 0.05;
  const cycleSeconds = 3.2;
  const speechSeconds = 1.4;
  const rampSeconds = 0.03;
  const level = 0.18;
  for (let offset = 0; offset < 180; offset += cycleSeconds) {
    const cycleStart = startAt + offset;
    gainNode.gain.setValueAtTime(0, cycleStart);
    gainNode.gain.linearRampToValueAtTime(level, cycleStart + rampSeconds);
    gainNode.gain.setValueAtTime(level, cycleStart + speechSeconds);
    gainNode.gain.linearRampToValueAtTime(
      0,
      cycleStart + speechSeconds + rampSeconds
    );
    gainNode.gain.setValueAtTime(0, cycleStart + cycleSeconds);
  }
  source.start(startAt);

  activeToneSource = source;
  activeToneGain = gainNode;
  return destination.stream;
};

const stopSimulatedAudio = () => {
  stopNoiseStream();
  stopToneStream();
  stopSilenceStream();
};

const normalizeAudioConstraints = (constraints, audioSource) => {
  const normalized = Object.assign({}, constraints);
  if (normalized.audio === undefined && normalized.video === undefined) {
    normalized.audio = Object.assign({}, REAL_MIC_AUDIO_CONSTRAINTS);
    normalized.video = false;
  }
  if (normalized.audio === true) {
    normalized.audio = Object.assign({}, REAL_MIC_AUDIO_CONSTRAINTS);
  } else if (typeof normalized.audio === "object" && normalized.audio !== null) {
    normalized.audio = Object.assign(
      {},
      REAL_MIC_AUDIO_CONSTRAINTS,
      normalized.audio
    );
  }
  if (
    audioSource &&
    audioSource.startsWith(AUDIO_SOURCE_DEVICE_PREFIX) &&
    normalized.audio !== false
  ) {
    const deviceId = audioSource.slice(AUDIO_SOURCE_DEVICE_PREFIX.length);
    if (deviceId) {
      if (normalized.audio === true) {
        normalized.audio = { deviceId: { exact: deviceId } };
      } else if (typeof normalized.audio === "object") {
        normalized.audio = Object.assign({}, normalized.audio, {
          deviceId: { exact: deviceId },
        });
      }
    }
  }
  return normalized;
};

const buildMediaStreamFactory = (audioSource) => {
  return (constraints) => {
    if (audioSource === AUDIO_SOURCE_NOISE) {
      browserLatencyMarkers.getUserMediaStartedAt = browserNow();
      recordBrowserLatencyEvent("webrtc.get_user_media_started", {
        audio_source: audioSource,
      });
      const stream = createNoiseStream();
      recordBrowserLatencyEvent("webrtc.get_user_media_ready", {
        audio_source: audioSource,
        capture_setup_ms: browserElapsedMs(
          browserLatencyMarkers.getUserMediaStartedAt,
          browserNow()
        ),
      });
      return Promise.resolve(stream);
    }
    if (audioSource === AUDIO_SOURCE_TONE) {
      browserLatencyMarkers.getUserMediaStartedAt = browserNow();
      recordBrowserLatencyEvent("webrtc.get_user_media_started", {
        audio_source: audioSource,
      });
      const stream = createToneSpeechStream();
      recordBrowserLatencyEvent("webrtc.get_user_media_ready", {
        audio_source: audioSource,
        capture_setup_ms: browserElapsedMs(
          browserLatencyMarkers.getUserMediaStartedAt,
          browserNow()
        ),
      });
      return Promise.resolve(stream);
    }
    if (audioSource === AUDIO_SOURCE_SILENCE) {
      browserLatencyMarkers.getUserMediaStartedAt = browserNow();
      recordBrowserLatencyEvent("webrtc.get_user_media_started", {
        audio_source: audioSource,
      });
      const stream = createSilenceStream();
      recordBrowserLatencyEvent("webrtc.get_user_media_ready", {
        audio_source: audioSource,
        capture_setup_ms: browserElapsedMs(
          browserLatencyMarkers.getUserMediaStartedAt,
          browserNow()
        ),
        track_count: stream && stream.getTracks ? stream.getTracks().length : undefined,
      });
      return Promise.resolve(stream);
    }
    if (
      navigator.mediaDevices === undefined ||
      navigator.mediaDevices.getUserMedia === undefined
    ) {
      return Promise.reject(
        new Error("Media devices not available in insecure contexts.")
      );
    }
    const nextConstraints = normalizeAudioConstraints(
      constraints || {},
      audioSource
    );
    browserLatencyMarkers.getUserMediaStartedAt = browserNow();
    recordBrowserLatencyEvent("webrtc.get_user_media_started", {
      audio_source: audioSource || AUDIO_SOURCE_DEFAULT,
    });
    return navigator.mediaDevices.getUserMedia.call(
      navigator.mediaDevices,
      nextConstraints
    ).then((stream) => {
      recordBrowserLatencyEvent("webrtc.get_user_media_ready", {
        audio_source: audioSource || AUDIO_SOURCE_DEFAULT,
        capture_setup_ms: browserElapsedMs(
          browserLatencyMarkers.getUserMediaStartedAt,
          browserNow()
        ),
        track_count: stream && stream.getTracks ? stream.getTracks().length : undefined,
      });
      return stream;
    });
  };
};

const populateAudioSourceOptions = () => {
  const audioSelect = document.getElementById("audio-source");
  if (!audioSelect) {
    return;
  }
  if (
    navigator.mediaDevices === undefined ||
    navigator.mediaDevices.enumerateDevices === undefined
  ) {
    return;
  }
  navigator.mediaDevices
    .enumerateDevices()
    .then((devices) => {
      const existingValues = new Set(
        Array.from(audioSelect.options).map((option) => option.value)
      );
      let micIndex = 1;
      devices
        .filter((device) => device.kind === "audioinput")
        .forEach((device) => {
          if (
            device.deviceId === "default" ||
            device.deviceId === "communications"
          ) {
            return;
          }
          const value = AUDIO_SOURCE_DEVICE_PREFIX + device.deviceId;
          if (existingValues.has(value)) {
            return;
          }
          const option = document.createElement("option");
          option.value = value;
          option.textContent = device.label || "Microphone " + micIndex;
          audioSelect.appendChild(option);
          existingValues.add(value);
          micIndex += 1;
        });
    })
    .catch((e) => {
      console.warn("Unable to enumerate audio devices.", e);
    });
};

// Sections.
const keypad = document.getElementById("keypad");
const callDetails = document.getElementById("call-details");

// Labels.
const contactNumber = document.getElementById("contact-number");
const contactName = document.getElementById("contact-name");
const infoMessage = document.getElementById("info-message");
const avatarCircle = document.getElementById("avatar");

// Buttons.
const acceptCallButton = document.getElementById("accept-call-button");
const declineCallButton = document.getElementById("decline-call-button");
const hangUpButton = document.getElementById("hang-up-button");
const muteButton = document.getElementById("mute-button");
const deleteButton = document.getElementById("delete-button");
const acceptCall = document.getElementById("accept-call");
const declineCall = document.getElementById("decline-call");
const incomingCallButtons = document.getElementById("incoming-call-buttons");
const inCallButtons = document.getElementById("in-call-buttons");
const callDuration = document.getElementById("call-duration");
const callHangupIcon = document.getElementById("call-hangup-icon");
const callPlaceIcon = document.getElementById("call-place-icon");

// Icons.
const muteIcon = document.getElementById("mute-icon");
const unmuteIcon = document.getElementById("unmute-icon");

const keyPressed = (character) => {
  if (isOnCall) {
    simpleUser
      .sendDTMF(character)
      .then(() => {})
      .catch((e) => {
        console.warn("Unable to send DTMF character.", e);
      });
  } else {
    if (character != "+") {
      const tone = playDtmf(character);
      tone.stop(0.2);
    }
    const e = document.getElementById("dialled-number");
    if (e) {
      e.innerText = e.innerText + "" + character;
    }
  }
};

const deletePressed = () => {
  const e = document.getElementById("dialled-number");
  if (e) {
    e.innerText = e.innerText.slice(0, -1);
  }
};

const onTouchStart = (character) => {
  if (character == "0")
    longKeyPressTimer = setTimeout(() => {
      keyPressed("+");
    }, 500);
};

const onTouchEnd = (character) => {
  if (character == "0") clearTimeout(longKeyPressTimer);
};

const placeCall = (number, message, options) => {
  infoMessage.innerText = message;
  isMakingCall = true;
  resetBrowserLatencyMarkers();
  browserLatencyMarkers.callStartedAt = browserNow();
  const trimmedNumber = (number || "").trim();
  const target =
    trimmedNumber.startsWith("sip:") || trimmedNumber.includes("@")
      ? trimmedNumber
      : "sip:" + trimmedNumber + "@voice.local";
  const isTranslationCall =
    trimmedNumber === "7100" || target.indexOf("sip:7100@") === 0;
  const headers = [];
  if (isTranslationCall && options && options.translatePeer) {
    headers.push("X-Translate-Peer: " + sipHeaderValue(options.translatePeer));
  }
  if (isTranslationCall && options && options.sourceLanguage) {
    headers.push("X-Source-Language: " + sipHeaderValue(options.sourceLanguage));
  }
  if (isTranslationCall && options && options.targetLanguage) {
    headers.push("X-Target-Language: " + sipHeaderValue(options.targetLanguage));
  }
  if (options && options.voiceEventsSessionId) {
    headers.push(
      "X-Voice-Events-Session: " + sipHeaderValue(options.voiceEventsSessionId)
    );
  }
  emitHarnessEvent("outbound_call_started", {
    destination: target,
  });
  recordBrowserLatencyEvent("browser.call_started", {
    destination: target,
    direction: "outbound",
  });
  recordLocalVoiceEvent("webrtc", "call.created", {
    destination: target,
  });
  return simpleUser
    .call(target, {
      extraHeaders: headers,
    })
    .then(() => {
      emitHarnessEvent("outbound_call_sent", {
        destination: target,
      });
      recordBrowserLatencyEvent("browser.sip_invite_sent", {
        destination: target,
        call_elapsed_ms: browserElapsedMs(browserLatencyMarkers.callStartedAt, browserNow()),
      });
      progress.play();
      progressTimer = setInterval(() => {
        progress.play();
      }, 3000);
    })
    .catch((error) => {
      isMakingCall = false;
      emitHarnessEvent("outbound_call_failed", {
        destination: target,
        error: formatHarnessError(error),
      });
      throw error;
    });
};

const switchViews = (v1, v2) => {
  remove(v1);
  show(v2);
};

const showCallDetails = () => {
  switchViews(keypad, callDetails);
  switchViews(callPlaceIcon, callHangupIcon);
  hangUpButton.classList.remove("accept-button");
  hangUpButton.classList.add("decline-button");
  remove(deleteButton);
  remove(incomingCallButtons);
  remove(inCallButtons);
  if (isOnCall) {
    show(inCallButtons);
    show(muteButton);
    show(callDuration);
  } else {
    hide(callDuration);
    if (isMakingCall) {
      show(inCallButtons);
      remove(muteButton);
    } else if (hasIncomingCall && !automationState.autoAnswer) {
      show(incomingCallButtons);
    } else {
      remove(muteButton);
    }
  }
};

const showKeyPad = () => {
  switchViews(callDetails, keypad);
  switchViews(incomingCallButtons, inCallButtons);
  if (isOnCall) {
    remove(deleteButton);
    show(muteButton);
    show(callDuration);
    switchViews(callPlaceIcon, callHangupIcon);
    hangUpButton.classList.remove("accept-button");
    hangUpButton.classList.add("decline-button");
  } else {
    hide(muteButton);
    show(deleteButton);
    hide(callDuration);
    switchViews(callHangupIcon, callPlaceIcon);
    hangUpButton.classList.remove("decline-button");
    hangUpButton.classList.add("accept-button");
  }
};

const loadPage = (wsUrl, options) => {
  // Apply dark mode if enabled
  if (options && options.isDarkMode) {
    document.body.classList.add("dark-mode");
  }

  isMakingCall = options.makeCall;

  if (!isMakingCall && options.contactNumber == "") {
    showKeyPad();
  } else {
    showCallDetails();
  }

  avatarCircle.innerText = options.firstLetter;
  contactNumber.innerText = options.contactNumber;

  if (options.contactName) {
    contactName.innerText = options.contactName;
  }

  hangUpButton.addEventListener("click", function (evt) {
    if (isOnCall || isMakingCall) {
      localHangup = true;
      updateHarnessState({ localHangup: true });
      simpleUser
        .hangup()
        .then(() => {
          if (intervalReceipt) {
            clearInterval(intervalReceipt);
          }
        })
        .catch((e) => {
          infoMessage.innerText = options.onCallErrorText;
          console.error(e);
        });
    } else {
      const el = document.getElementById("dialled-number");
      if (el) {
        var number = el.innerText;
        contactNumber.innerText = number;
        placeCall(
          options.numberToCall.replace("_", number),
          options.callingText,
          options
        )
          .catch((e) => {
            infoMessage.innerText = options.onCallErrorText;
            console.error(e);
          });
        isMakingCall = true;
        showCallDetails();
      }
    }
  });

  acceptCallButton.addEventListener("click", function (evt) {
    simpleUser
      .answer()
      .then(() => {
        isOnCall = true;
        hasIncomingCall = false;
        showCallDetails();
      })
      .catch((e) => {
        infoMessage.innerText = options.onCallErrorText;
        console.error(e);
      });
  });

  declineCallButton.addEventListener("click", function (evt) {
    // infoMessage.innerText = options.rejectingCallText;
    simpleUser
      .decline()
      .then(() => {
        hasIncomingCall = false;
        stopRinging();
        infoMessage.innerText = options.onCallDeclinedText;
        showCallDetails();
      })
      .catch((e) => {
        hasIncomingCall = false;
        stopRinging();
        showCallDetails();
        console.error(e);
      });
  });

  muteButton.addEventListener("click", function (evt) {
    if (muted) {
      simpleUser.unmute();
      muted = simpleUser.isMuted();
      if (muted) {
        console.warn("Unable to unmute.");
      } else {
        switchViews(unmuteIcon, muteIcon);
      }
    } else {
      simpleUser.mute();
      muted = simpleUser.isMuted();
      if (muted) {
        switchViews(muteIcon, unmuteIcon);
      } else {
        console.warn("Unable to mute.");
      }
    }
  });

  const mediaStreamFactory = buildMediaStreamFactory(
    options.audioSource || AUDIO_SOURCE_DEFAULT
  );
  const incomingAudio = document.getElementById("incomingAudio");
  if (incomingAudio) {
    incomingAudio.muted = options.remoteAudioMuted === true;
  }
  const sipOptions = {
    aor: options.aor,
    media: {
      // Only Audio for now.
      constraints: {
        audio: Object.assign({}, REAL_MIC_AUDIO_CONSTRAINTS),
        video: false,
      },
      remote: { audio: document.getElementById("incomingAudio") },
    },
    userAgentOptions: {
      userAgentString: "Company-Connect",
      sessionDescriptionHandlerFactory:
        Web.defaultSessionDescriptionHandlerFactory(mediaStreamFactory),
      sessionDescriptionHandlerFactoryOptions: {
        iceGatheringTimeout: 1000,
        peerConnectionConfiguration: {
          iceServers: [],
        },
      },
      transportOptions: {
        server: wsUrl,
        reconnectionTimeout: 4,
        keepAliveInterval: 30,
      },
    },
    delegate: {
      onServerConnect: () => {
        emitHarnessEvent("server_connected", {
          wsUrl: wsUrl,
        });
        recordLocalVoiceEvent("webrtc", "system.debug", {
          message: "sip websocket connected",
          wsUrl: wsUrl,
        });
      },
      onServerDisconnect: () => {
        emitHarnessEvent("server_disconnected", {});
        recordLocalVoiceEvent("webrtc", "system.warning", {
          message: "sip websocket disconnected",
        });
      },
      onCallHangup: () => {
        clearAutoHangupTimer();
        isOnCall = false;
        isMakingCall = false;
        hasIncomingCall = false;
        updateHarnessState({
          callAnswered: false,
          localHangup: localHangup,
        });
        window.dispatchEvent(
          new CustomEvent("Company:call-ended", {
            detail: {
              localHangup: localHangup,
            },
          }),
        );
        emitHarnessEvent("call_hangup", {
          localHangup: localHangup,
        });
        recordLocalVoiceEvent("webrtc", "call.hangup", {
          localHangup: localHangup,
        });
        stopRinging();
        stopProgress();
        stopSimulatedAudio();
        showCallDetails();
        if (!localHangup)
          setTimeout(() => {
            playDisconnectedTone();
          }, 2000);

        infoMessage.innerText = options.callDisconnectedText;
        if (intervalReceipt) {
          clearInterval(intervalReceipt);
        }
        simpleUser
          .unregister()
          .then(() => {
            if (simpleUser.isConnected()) {
                  simpleUser
                .disconnect()
                .then(() => {
                  emitHarnessEvent("sip_disconnected", {});
                })
                .catch((e) => {
                  console.warn("Unable to disconnect.", e);
                });
            }
          })
          .catch((e) => {
            console.warn("Unable to unregister.", e);
          });
        if (automationState.closeOnComplete) {
          setTimeout(() => {
            window.close();
          }, 250);
        }
        localHangup = false;
        updateHarnessState({ localHangup: false });
      },
      // Sip registration was successful.
      onRegistered: () => {
        infoMessage.innerText = options.onRegisteredText;
        emitRegistered(options.aor, "delegate");
      },
      // Respond to an incoming INVITE request.
      onCallReceived: () => {
        hasIncomingCall = true;
        resetBrowserLatencyMarkers();
        browserLatencyMarkers.callStartedAt = browserNow();
        infoMessage.innerText = options.onCallReceivedText;
        emitHarnessEvent("call_received", {});
        recordBrowserLatencyEvent("browser.call_started", {
          direction: "inbound",
        });
        recordLocalVoiceEvent("webrtc", "call.created", {
          direction: "inbound",
        });

        if (ringing == false) {
          ringing = true;
          ringtone.play();
          ringerTimer = setInterval(() => {
            ringtone.play();
          }, 9000);
        }

        // Enable the 'answer' button.
        acceptCall.innerText = options.answerCallText || "Accept";
        // Enable the 'reject' button.
        declineCall.innerText = options.rejectCallText || "Decline";

        showCallDetails();
        if (automationState.autoAnswer) {
          setTimeout(() => {
            acceptCallButton.click();
          }, 50);
        }
      },
      onCallAnswered: () => {
        infoMessage.innerText = options.onCallAnsweredText;
        isOnCall = true;
        hasIncomingCall = false;
        stopRinging();
        stopProgress();
        showCallDetails();
        switchViews(unmuteIcon, muteIcon);
        updateHarnessState({
          callAnswered: true,
        });
        emitHarnessEvent("call_answered", {});
        recordBrowserLatencyEvent("browser.call_established", {
          call_elapsed_ms: browserElapsedMs(browserLatencyMarkers.callStartedAt, browserNow()),
        });
        recordLocalVoiceEvent("webrtc", "call.answered", {});
        recordLocalVoiceEvent("webrtc", "call.connected", {});
        scheduleAutoHangup();

        intervalReceipt = setInterval(() => {
          ++totalSeconds;
          const hours = Math.floor(totalSeconds / 3600);
          const minutes = Math.floor((totalSeconds % 3600) / 60);
          const seconds = (totalSeconds % 3600) % 60;
          const duration =
            (hours > 0 ? hours + ":" : "") +
            formatDuration(minutes) +
            ":" +
            formatDuration(seconds);
          callDuration.innerText = duration;
        }, 1000);
      },
      onCallFailed: (error) => {
        hasIncomingCall = false;
        emitHarnessEvent("call_failed", {
          error: formatHarnessError(error),
        });
        recordLocalVoiceEvent("webrtc", "system.error", {
          error: formatHarnessError(error),
        });
      },
      onCallRejected: () => {
        hasIncomingCall = false;
        emitHarnessEvent("call_rejected", {});
        recordLocalVoiceEvent("webrtc", "call.hangup", {
          reason: "rejected",
        });
      },
    },
  };

  show(document.getElementById("call-content"));

  simpleUser = new Web.SimpleUser(wsUrl, sipOptions);
  window.simpleUser = simpleUser;
  simpleUser
    .connect()
    .then(() => simpleUser.register())
    .then(() => {
      emitRegistered(options.aor, "register_promise");
      if (options.makeCall) {
        return placeCall(options.numberToCall, options.callingText, options);
      }
      return undefined;
    })
    .catch((e) => {
      infoMessage.innerText = options.onCallErrorText;
      emitHarnessEvent("page_start_failed", {
        error: formatHarnessError(e),
      });
      recordLocalVoiceEvent("system", "system.error", {
        error: formatHarnessError(e),
      });
      console.error(e);
    });
};

const getSetupValue = (id) => {
  const element = document.getElementById(id);
  if (element) {
    return element.value.trim();
  }
  const key = setupValueIds[id];
  const value = key ? setupState[key] : "";
  return value === undefined || value === null ? "" : String(value).trim();
};

const getSetupChecked = (id) => {
  const element = document.getElementById(id);
  if (element) {
    return element.checked;
  }
  const key = setupCheckedIds[id];
  return key ? setupState[key] === true : false;
};

const setSetupValue = (id, value) => {
  const element = document.getElementById(id);
  if (element) {
    element.value = value;
  }
  const key = setupValueIds[id];
  if (key) {
    setupState[key] = value;
  }
};

const setSetupChecked = (id, value) => {
  const element = document.getElementById(id);
  if (element) {
    element.checked = value;
  }
  const key = setupCheckedIds[id];
  if (key) {
    setupState[key] = value;
  }
};

const buildOptionsFromForm = () => {
  const dialNumberValue = getSetupValue("dial-number");
  const contactNumberValue =
    getSetupValue("contact-number-input") || dialNumberValue;
  const numberToCallValue = getSetupValue("number-to-call") || dialNumberValue;
  return {
    aor: getSetupValue("aor"),
    contactNumber: contactNumberValue,
    contactName: getSetupValue("contact-name-input"),
    numberToCall: numberToCallValue,
    translatePeer: getSetupValue("translate-peer"),
    sourceLanguage: getSetupValue("source-language"),
    targetLanguage: getSetupValue("target-language"),
    makeCall: getSetupChecked("make-call"),
    firstLetter: getSetupValue("first-letter"),
    onCallErrorText: getSetupValue("on-call-error-text"),
    onCallDeclinedText: getSetupValue("on-call-declined-text"),
    callDisconnectedText: getSetupValue("call-disconnected-text"),
    onRegisteredText: getSetupValue("on-registered-text"),
    onCallReceivedText: getSetupValue("on-call-received-text"),
    onCallAnsweredText: getSetupValue("on-call-answered-text"),
    callingText: getSetupValue("calling-text"),
    muteText: getSetupValue("mute-text"),
    unmuteText: getSetupValue("unmute-text"),
    isDarkMode: getSetupChecked("is-dark-mode"),
    audioSource: getSetupValue("audio-source"),
    remoteAudioMuted: getSetupChecked("remote-audio-muted"),
    voiceEventsUrl: getSetupValue("voice-events-url"),
    voiceEventsSessionId: getSetupValue("voice-events-session-id"),
    voiceEventsMockTrace: getSetupValue("voice-events-mock-trace"),
    voiceEventsMock: getSetupChecked("voice-events-mock"),
  };
};

const startCallFromForm = () => {
  if (hasStarted) {
    return;
  }
  resumeAudioContext();
  const wsUrl = getSetupValue("ws-url");
  const options = buildOptionsFromForm();
  ensureVoiceEventsSessionId(options);
  if (options.isDarkMode) {
    document.body.classList.add("dark-mode");
  }
  const callLaunch = document.getElementById("call-launch");
  if (callLaunch) {
    remove(callLaunch);
  }
  updateHarnessState({
    started: true,
    localHangup: false,
  });
  emitHarnessEvent("page_starting", {
    wsUrl: wsUrl,
    aor: options.aor,
    numberToCall: options.numberToCall,
  });
  if (options.voiceEventsMock) {
    isMakingCall = options.makeCall;
    if (!isMakingCall && options.contactNumber == "") {
      showKeyPad();
    } else {
      showCallDetails();
    }
    show(document.getElementById("call-content"));
    avatarCircle.innerText = options.firstLetter;
    infoMessage.innerText = "Mock event stream";
    contactNumber.innerText = options.contactNumber || "mock";
    if (options.contactName) {
      contactName.innerText = options.contactName;
    }
    void startVoiceEventMockStream(options.voiceEventsMockTrace);
    hasStarted = true;

    const callSetup = document.getElementById("call-setup");
    if (callSetup && callSetup.open) {
      callSetup.open = false;
    }

    const startCallButton = document.getElementById("start-call-button");
    if (startCallButton) {
      disableButton(startCallButton);
    }
    return;
  }
  connectVoiceEventStream(options.voiceEventsUrl, options.voiceEventsSessionId);
  loadPage(wsUrl, options);
  hasStarted = true;

  const callSetup = document.getElementById("call-setup");
  if (callSetup && callSetup.open) {
    callSetup.open = false;
  }

  const startCallButton = document.getElementById("start-call-button");
  if (startCallButton) {
    disableButton(startCallButton);
  }
};

var terminationPromise = undefined;

const waitForTermination = (promise, timeoutMs) =>
  new Promise((resolve) => {
    const timer = setTimeout(resolve, timeoutMs);
    Promise.resolve(promise)
      .catch(() => undefined)
      .finally(() => {
        clearTimeout(timer);
        resolve();
      });
  });

const safeCallControl = async (action) => {
  try {
    await action();
  } catch (e) {
    console.warn("Unable to stop call activity.", e);
  }
};

const terminateOngoingActivity = (reason) => {
  if (terminationPromise) {
    return terminationPromise;
  }
  terminationPromise = (async () => {
    clearAutoHangupTimer();
    stopRinging();
    stopProgress();
    stopSimulatedAudio();
    stopVoiceEventMockStream();
    disconnectVoiceEventStream();
    if (
      window.MediaDebugProbe &&
      typeof window.MediaDebugProbe.stop === "function"
    ) {
      window.MediaDebugProbe.stop();
    }
    if (intervalReceipt) {
      clearInterval(intervalReceipt);
      intervalReceipt = undefined;
    }
    if (longKeyPressTimer) {
      clearTimeout(longKeyPressTimer);
      longKeyPressTimer = undefined;
    }

    const activeUser = simpleUser;
    if (activeUser) {
      localHangup = true;
      updateHarnessState({ localHangup: true });
      if (
        hasIncomingCall &&
        !isOnCall &&
        !isMakingCall &&
        typeof activeUser.decline === "function"
      ) {
        await safeCallControl(() => activeUser.decline());
      } else if (
        (isOnCall || isMakingCall || automationState.callAnswered) &&
        typeof activeUser.hangup === "function"
      ) {
        await safeCallControl(() => activeUser.hangup());
      }
      if (typeof activeUser.unregister === "function") {
        await safeCallControl(() => activeUser.unregister());
      }
      let isConnected = false;
      try {
        isConnected =
          typeof activeUser.isConnected === "function" &&
          activeUser.isConnected();
      } catch (e) {
        console.warn("Unable to inspect SIP connection state.", e);
      }
      if (isConnected && typeof activeUser.disconnect === "function") {
        await safeCallControl(() => activeUser.disconnect());
      }
    }

    isOnCall = false;
    isMakingCall = false;
    hasIncomingCall = false;
    ringing = false;
    updateHarnessState({
      callAnswered: false,
      localHangup: false,
      terminationReason: reason || "",
    });
    localHangup = false;
  })().finally(() => {
    terminationPromise = undefined;
  });
  return terminationPromise;
};

const initBackButton = () => {
  const backButton = document.getElementById("page-back-button");
  if (!backButton) {
    return;
  }
  if (getQueryFlag("embedded")) {
    backButton.hidden = true;
    return;
  }
  backButton.addEventListener("click", async (event) => {
    event.preventDefault();
    disableButton(backButton);
    backButton.textContent = "Leaving";
    await waitForTermination(terminateOngoingActivity("back_button"), 2500);
    if (window.history.length > 1) {
      window.history.back();
    } else {
      window.location.href = "../";
    }
  });
};

const applySetupOverridesFromQuery = () => {
  const fieldMappings = [
    { query: "ws_url", id: "ws-url" },
    { query: "voice_events_url", id: "voice-events-url" },
    { query: "voice_events_session_id", id: "voice-events-session-id" },
    { query: "voice_events_mock_trace", id: "voice-events-mock-trace" },
    { query: "dial_number", id: "dial-number" },
    { query: "aor", id: "aor" },
    { query: "contact_number", id: "contact-number-input" },
    { query: "contact_name", id: "contact-name-input" },
    { query: "audio_source", id: "audio-source" },
    { query: "number_to_call", id: "number-to-call" },
    { query: "translate_peer", id: "translate-peer" },
    { query: "source_language", id: "source-language" },
    { query: "target_language", id: "target-language" },
    { query: "first_letter", id: "first-letter" },
    { query: "on_registered_text", id: "on-registered-text" },
    { query: "on_call_answered_text", id: "on-call-answered-text" },
    { query: "calling_text", id: "calling-text" },
  ];
  fieldMappings.forEach((mapping) => {
    const value = getQueryValue(mapping.query);
    if (!value) {
      return;
    }
    setSetupValue(mapping.id, value);
  });

  const checkboxMappings = [
    { query: "make_call", id: "make-call" },
    { query: "is_dark_mode", id: "is-dark-mode" },
    { query: "enable_media_debug", id: "enable-media-debug" },
    { query: "remote_audio_muted", id: "remote-audio-muted" },
    { query: "voice_events_mock", id: "voice-events-mock" },
  ];
  checkboxMappings.forEach((mapping) => {
    if (!queryParams.has(mapping.query)) {
      return;
    }
    setSetupChecked(mapping.id, getQueryFlag(mapping.query));
  });
};

const buildSiblingPageUrl = (pageName) => {
  const nextUrl = new URL(pageName, window.location.href);
  [
    "ws_url",
    "voice_events_url",
    "voice_events_session_id",
    "audio_source",
    "enable_media_debug",
    "is_dark_mode",
    "voice_events_mock",
    "voice_events_mock_trace",
    "close_on_complete",
  ].forEach((name) => {
    if (queryParams.has(name)) {
      nextUrl.searchParams.set(name, getQueryValue(name));
    }
  });
  return nextUrl.toString();
};

const initCallLauncher = () => {
  const startCallButton = document.getElementById("start-call-button");
  if (startCallButton) {
    startCallButton.addEventListener("click", (event) => {
      event.preventDefault();
      startCallFromForm();
    });
  }

  const aiAgentButton = document.getElementById("call-ai-agent-button");
  if (aiAgentButton) {
    aiAgentButton.addEventListener("click", (event) => {
      event.preventDefault();
      window.location.href = buildSiblingPageUrl("call.html");
    });
  }

  const translationButton = document.getElementById(
    "call-translation-service-button"
  );
  if (translationButton) {
    translationButton.addEventListener("click", (event) => {
      event.preventDefault();
      setSetupChecked("make-call", true);
      setSetupValue("dial-number", "7100");
      setSetupValue("number-to-call", "7100");
      setSetupValue("contact-number-input", "7100");
      setSetupValue("contact-name-input", "Translation service");
      startCallFromForm();
    });
  }
};

const initCallSetupForm = () => {
  const callSetupForm = document.getElementById("call-setup-form");
  if (!callSetupForm) {
    return;
  }
  const numberInput = document.getElementById("dial-number");
  const contactNumberInput = document.getElementById("contact-number-input");
  const numberToCallInput = document.getElementById("number-to-call");
  let lastNumberValue = numberInput ? numberInput.value.trim() : "";

  const syncNumberInputs = () => {
    if (!numberInput) {
      return;
    }
    const nextNumber = numberInput.value.trim();
    if (
      contactNumberInput &&
      contactNumberInput.value.trim() === lastNumberValue
    ) {
      contactNumberInput.value = nextNumber;
    }
    if (numberToCallInput) {
      const currentDial = numberToCallInput.value.trim();
      const dialParts = currentDial.split("@");
      if (dialParts.length === 2 && dialParts[0] === lastNumberValue) {
        numberToCallInput.value = nextNumber
          ? nextNumber + "@" + dialParts[1]
          : "";
      } else if (dialParts.length === 1 && currentDial === lastNumberValue) {
        numberToCallInput.value = nextNumber;
      }
    }
    lastNumberValue = nextNumber;
  };

  if (numberInput) {
    numberInput.addEventListener("input", syncNumberInputs);
  }

  callSetupForm.addEventListener("submit", (event) => {
    event.preventDefault();
    startCallFromForm();
  });

  populateAudioSourceOptions();
};

applySetupOverridesFromQuery();
initBackButton();
initCallLauncher();
initCallSetupForm();
initVoiceObservabilityControls();
window.addEventListener("Company:media-quality", (event) => {
  const detail = event.detail || {};
  updateAudioQualitySummary(detail);
  updateBrowserWebrtcLatency(detail);
});
emitHarnessEvent("page_ready", {
  autoStart: automationState.autoStart,
});
if (automationState.autoStart || getQueryFlag("voice_events_mock")) {
  setTimeout(() => {
    startCallFromForm();
  }, 0);
}

window.CompanyCallHarness = {
  getState: () => Object.assign({}, automationState),
  start: startCallFromForm,
  answer: () => acceptCallButton.click(),
  hangup: () => {
    if (simpleUser && (isOnCall || isMakingCall)) {
      hangUpButton.click();
    }
  },
  stop: () => terminateOngoingActivity("harness_stop"),
  classifyAudioQuality: classifyAudioQuality,
  fetchVoiceEventMockTrace: fetchVoiceEventMockTrace,
};

const playDtmf = (key) => {
  switch (key) {
    case "1":
      return playDtmfTone(697, 1209);
    case "2":
      return playDtmfTone(697, 1336);
    case "3":
      return playDtmfTone(697, 1477);
    case "4":
      return playDtmfTone(770, 1209);
    case "5":
      return playDtmfTone(770, 1336);
    case "6":
      return playDtmfTone(770, 1477);
    case "7":
      return playDtmfTone(852, 1209);
    case "8":
      return playDtmfTone(852, 1336);
    case "9":
      return playDtmfTone(852, 1477);
    case "*":
      return playDtmfTone(941, 1209);
    case "0":
      return playDtmfTone(941, 1336);
    case "#":
      return playDtmfTone(941, 1477);
    case "A":
      return playDtmfTone(697, 1633);
    case "B":
      return playDtmfTone(770, 1633);
    case "C":
      return playDtmfTone(852, 1633);
    case "D":
      return playDtmfTone(941, 1633);
  }
};

const playDisconnectedTone = () => {
  document.getElementById("incomingAudio").srcObject = null;
  document.getElementById("incomingAudio").pause();
  var pips = new Audio(ASSET_BASE_PATH + "sounds/pips.mp3");
  pips.play();
};

const playRinging = () => {
  return playTone([1.5, 4.5], 425);
};

const playBusyTone = () => {
  return playTone([0.2, 0.2], 425);
};

const createOscillator = (frequency, type = "sine") => {
  return new OscillatorNode(audioContext, { type, frequency });
};

const createLFO = (onTime, offTime) => {
  const period = onTime + offTime;
  const channels = 1;
  const sampleRate = audioContext.sampleRate;
  const frameCount = sampleRate * period;
  const arrayBuffer = audioContext.createBuffer(
    channels,
    frameCount,
    sampleRate
  );
  var bufferData = arrayBuffer.getChannelData(0);
  for (let i = 0; i < frameCount; i++) {
    if (i / sampleRate > 0 && i / sampleRate < onTime) {
      bufferData[i] = 0.25;
    }
  }
  const bufferSource = audioContext.createBufferSource();
  bufferSource.buffer = arrayBuffer;
  bufferSource.loop = true;
  return bufferSource;
};

const playDtmfTone = (freq1, freq2) => {
  const gainNode = new GainNode(audioContext, { gain: 0.25 });
  gainNode.connect(audioContext.destination);
  const oscillator1 = createOscillator(freq1);
  const oscillator2 = createOscillator(freq2);
  oscillator1.connect(gainNode);
  oscillator2.connect(gainNode);
  oscillator1.start();
  oscillator2.start();
  const context = audioContext;
  return {
    stop(when = 0) {
      oscillator2.onended = () => gainNode.disconnect();
      oscillator1.stop(context.currentTime + when);
      oscillator2.stop(context.currentTime + when);
    },
  };
};

const playTone = (cadence, frequency) => {
  const gainNode = new GainNode(audioContext);
  gainNode.connect(audioContext.destination);
  gainNode.gain.value = 0;
  const oscillator = createOscillator(frequency);
  const lfo = createLFO(...cadence);
  lfo.connect(gainNode.gain);
  oscillator.connect(gainNode);
  oscillator.start();
  lfo.start();
  const context = audioContext;
  return {
    stop(when = 0) {
      oscillator.onended = () => gainNode.disconnect();
      lfo.stop(context.currentTime + when);
      oscillator.stop(context.currentTime + when);
    },
  };
};
