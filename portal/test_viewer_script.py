import subprocess

from portal.app import VIEWER_SCRIPT

NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(0, "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function element() {
  return {
    dataset: {}, hidden: true, href: "", textContent: "", value: "", style: {}, listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    querySelector() { return element(); },
    focus() {}, dispatchEvent() {},
  };
}

function viewer(responses) {
  let nextTimerId = 1;
  const timers = [];
  const observers = [];
  const elements = Object.fromEntries([
    "remote-frame", "status", "typing-panel", "typing-form", "typing-input",
    "viewer-recovery", "viewer-recovery-link", "scroll-up", "scroll-down",
    "show-keyboard", "hide-keyboard", "fullscreen",
  ].map((name) => [name, element()]));
  const frame = elements["remote-frame"];
  frame.src = "";
  frame.contentDocument = null;
  frame.contentWindow = {focus() {}, WheelEvent: class { static DOM_DELTA_PIXEL = 0; }};

  function setTimeoutFake(callback, delay) {
    const timer = {id: nextTimerId++, callback, delay, cleared: false};
    timers.push(timer);
    return timer.id;
  }
  function clearTimeoutFake(id) {
    const timer = timers.find((candidate) => candidate.id === id);
    if (timer) timer.cleared = true;
  }
  function fetchFake(_url, options) {
    const response = responses.shift();
    if (!response) throw new Error("unexpected fetch");
    if (response.hung) {
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(new Error("aborted")), {once: true});
      });
    }
    return Promise.resolve({
      status: response.status,
      ok: response.status >= 200 && response.status < 300,
      json: async () => response.body,
    });
  }

  const windowObject = {
    location: {href: "https://portal.example/viewer"},
    setTimeout: setTimeoutFake,
    clearTimeout: clearTimeoutFake,
  };
  const documentObject = {
    body: {dataset: {csrf: "csrf"}},
    documentElement: {requestFullscreen: async () => {}},
    fullscreenElement: null,
    exitFullscreen: async () => {},
    getElementById(name) { return elements[name]; },
  };
  class MutationObserverFake {
    constructor(callback) { this.callback = callback; observers.push(callback); }
    observe() {}
  }
  const context = vm.createContext({
    window: windowObject,
    document: documentObject,
    fetch: fetchFake,
    MutationObserver: MutationObserverFake,
    AbortController,
    URL,
    Date,
    WeakSet,
  });
  vm.runInContext(source, context);
  return {elements, frame, timers, observers};
}

function activeTimers(state, delay = null) {
  return state.timers.filter((timer) => !timer.cleared && (delay === null || timer.delay === delay));
}

async function settle() {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
}

async function runTimer(state, delay) {
  const timer = activeTimers(state, delay)[0];
  assert(timer, `missing timer ${delay}`);
  timer.cleared = true;
  timer.callback();
  await settle();
}

(async () => {
  const closed = viewer([{status: 200, body: {status: "closed"}}]);
  await runTimer(closed, 0);
  assert(closed.frame.src === "about:blank", "closed viewer did not blank iframe");
  assert(closed.elements["viewer-recovery"].hidden === false, "closed viewer hid recovery action");
  assert(activeTimers(closed).length === 0, "closed viewer kept retrying");

  const unauthorized = viewer([{status: 401}]);
  await runTimer(unauthorized, 0);
  assert(unauthorized.frame.src === "about:blank", "unauthorized viewer did not stop");
  assert(activeTimers(unauthorized).length === 0, "unauthorized viewer kept retrying");

  const unavailable = viewer(Array.from({length: 7}, () => ({status: 200, body: {status: "unavailable"}})));
  await runTimer(unavailable, 0);
  for (const delay of [1500, 3000, 6000, 12000, 15000]) {
    assert(activeTimers(unavailable, delay).length === 1, `missing backoff ${delay}`);
    await runTimer(unavailable, delay);
    assert(!unavailable.frame.src.includes("/vnc/"), "outage reloaded iframe before open confirmation");
  }
  assert(activeTimers(unavailable, 15000).length === 1, "backoff did not stay capped");

  const open = viewer([{status: 200, body: {status: "open"}}]);
  await runTimer(open, 0);
  assert(open.frame.src.includes("/vnc/vnc_lite.html"), "open viewer did not load iframe");
  assert(activeTimers(open, 10000).length === 1, "connecting viewer has no watchdog");

  const stale = viewer([
    {status: 200, body: {status: "open"}},
    {status: 200, body: {status: "closed"}},
  ]);
  await runTimer(stale, 0);
  const remoteStatus = {textContent: "Disconnected"};
  const remoteScreen = {style: {}, addEventListener() {}, querySelector() { return null; }};
  stale.frame.contentDocument = {
    documentElement: {style: {}}, body: {style: {}},
    getElementById(name) {
      if (name === "status") return remoteStatus;
      if (name === "screen") return remoteScreen;
      return null;
    },
  };
  stale.frame.listeners.load();
  stale.observers[0]();
  stale.observers[0]();
  assert(activeTimers(stale, 1500).length === 1, "observer notifications created overlapping retries");
  await runTimer(stale, 1500);
  const terminalMessage = stale.elements.status.textContent;
  stale.observers[0]();
  assert(stale.elements.status.textContent === terminalMessage, "stale observer overwrote terminal status");
  assert(activeTimers(stale).length === 0, "stale observer restarted terminal viewer");

  const hung = viewer([{hung: true}]);
  await runTimer(hung, 0);
  assert(activeTimers(hung, 8000).length === 1, "status request has no abort timeout");
  await runTimer(hung, 8000);
  assert(activeTimers(hung, 1500).length === 1, "timed-out status request did not resume backoff");
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""


def test_viewer_reconnect_state_machine_in_node():
    result = subprocess.run(
        ["node", "-e", NODE_HARNESS],
        input=VIEWER_SCRIPT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
