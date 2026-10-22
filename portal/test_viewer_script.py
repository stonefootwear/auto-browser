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
  let currentTime = 0;
  let dateNow = 0;
  const timers = [];
  const observers = [];
  const elements = Object.fromEntries([
    "remote-frame", "status", "typing-panel", "typing-form", "typing-input",
    "viewer-recovery", "viewer-recovery-link", "scroll-up", "scroll-down",
    "show-keyboard", "hide-keyboard", "fullscreen",
  ].map((name) => [name, element()]));
  const frame = elements["remote-frame"];
  let frameSrc = "";
  frame.srcAssignments = [];
  Object.defineProperty(frame, "src", {
    get() { return frameSrc; },
    set(value) { frameSrc = value; frame.srcAssignments.push(value); },
  });
  frame.contentDocument = null;
  frame.contentWindow = {focus() {}, WheelEvent: class { static DOM_DELTA_PIXEL = 0; }};

  function setTimeoutFake(callback, delay) {
    const timer = {id: nextTimerId++, callback, delay, due: currentTime + delay, cleared: false};
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
    constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
    observe() {}
    disconnect() { this.disconnected = true; }
  }
  const context = vm.createContext({
    window: windowObject,
    document: documentObject,
    fetch: fetchFake,
    MutationObserver: MutationObserverFake,
    AbortController,
    URL,
    Date: {now() { dateNow += 1; return dateNow; }},
    WeakSet,
  });
  vm.runInContext(source, context);
  return {
    elements, frame, timers, observers,
    async advanceBy(elapsed) {
      const targetTime = currentTime + elapsed;
      while (true) {
        const timer = timers
          .filter((candidate) => !candidate.cleared && candidate.due <= targetTime)
          .sort((left, right) => left.due - right.due)[0];
        if (!timer) break;
        currentTime = timer.due;
        timer.cleared = true;
        timer.callback();
        await settle();
      }
      currentTime = targetTime;
    },
  };
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
  assert(activeTimers(open, 10000).length === 0, "RFB watchdog started before iframe modules loaded");
  assert(activeTimers(open, 30000).length === 1, "loading viewer has no bounded load watchdog");

  const slow = viewer([{status: 200, body: {status: "open"}}]);
  await runTimer(slow, 0);
  const slowSrc = slow.frame.src;
  assert(activeTimers(slow, 10000).length === 0, "slow iframe was given a premature RFB deadline");
  await slow.advanceBy(12000);
  assert(slow.frame.srcAssignments.length === 1, "slow iframe reloaded before its load event");
  const slowRemoteStatus = {textContent: "Connecting"};
  const slowRemoteScreen = {style: {}, addEventListener() {}, querySelector() { return null; }};
  slow.frame.contentDocument = {
    URL: slow.frame.src,
    documentElement: {style: {}}, body: {style: {}},
    getElementById(name) {
      if (name === "status") return slowRemoteStatus;
      if (name === "screen") return slowRemoteScreen;
      return null;
    },
  };
  slow.frame.listeners.load();
  assert(activeTimers(slow, 30000).length === 0, "completed iframe kept its load watchdog");
  assert(activeTimers(slow, 10000).length === 1, "loaded iframe did not start the RFB watchdog");
  slowRemoteStatus.textContent = "Connected to browser";
  slow.observers[0].callback();
  assert(activeTimers(slow, 10000).length === 0, "connected RFB kept its watchdog");
  assert(slow.frame.src === slowSrc && slow.frame.srcAssignments.length === 1, "slow successful load replaced the iframe");

  const connectFailure = viewer([
    {status: 200, body: {status: "open"}},
    {status: 200, body: {status: "open"}},
  ]);
  await runTimer(connectFailure, 0);
  const failedRemoteStatus = {textContent: "Connecting"};
  const failedRemoteScreen = {style: {}, addEventListener() {}, querySelector() { return null; }};
  connectFailure.frame.contentDocument = {
    URL: connectFailure.frame.src,
    documentElement: {style: {}}, body: {style: {}},
    getElementById(name) {
      if (name === "status") return failedRemoteStatus;
      if (name === "screen") return failedRemoteScreen;
      return null;
    },
  };
  connectFailure.frame.listeners.load();
  await runTimer(connectFailure, 10000);
  assert(activeTimers(connectFailure, 1500).length === 1, "post-load RFB timeout did not schedule recovery");
  await runTimer(connectFailure, 1500);
  assert(connectFailure.frame.srcAssignments.length === 2, "post-load RFB timeout did not replace the failed iframe");
  assert(activeTimers(connectFailure, 30000).length === 1, "replacement iframe has no load watchdog");

  const staleLoad = viewer([{status: 200, body: {status: "open"}}]);
  await runTimer(staleLoad, 0);
  const staleLoadStatus = {textContent: "Connecting"};
  const staleLoadScreen = {style: {}, addEventListener() {}, querySelector() { return null; }};
  staleLoad.frame.contentDocument = {
    URL: "about:blank",
    documentElement: {style: {}}, body: {style: {}},
    getElementById(name) {
      if (name === "status") return staleLoadStatus;
      if (name === "screen") return staleLoadScreen;
      return null;
    },
  };
  staleLoad.frame.listeners.load();
  assert(activeTimers(staleLoad, 30000).length === 1, "stale document canceled the real frame-load deadline");
  assert(activeTimers(staleLoad, 10000).length === 0, "stale document started an RFB watchdog");
  assert(staleLoad.observers.length === 0, "stale document installed a status observer");
  staleLoad.frame.contentDocument.URL = staleLoad.frame.src;
  staleLoad.frame.listeners.load();
  assert(activeTimers(staleLoad, 30000).length === 0, "real frame load kept its load deadline");
  assert(activeTimers(staleLoad, 10000).length === 1, "real frame load did not start its RFB deadline");

  const oldObserver = viewer([
    {status: 200, body: {status: "open"}},
    {status: 200, body: {status: "open"}},
  ]);
  await runTimer(oldObserver, 0);
  const firstRemoteStatus = {textContent: "Connecting"};
  const observerScreen = {style: {}, addEventListener() {}, querySelector() { return null; }};
  oldObserver.frame.contentDocument = {
    URL: oldObserver.frame.src,
    documentElement: {style: {}}, body: {style: {}},
    getElementById(name) {
      if (name === "status") return firstRemoteStatus;
      if (name === "screen") return observerScreen;
      return null;
    },
  };
  oldObserver.frame.listeners.load();
  const firstObserver = oldObserver.observers[0];
  await runTimer(oldObserver, 10000);
  await runTimer(oldObserver, 1500);
  assert(firstObserver.disconnected, "new viewer attempt kept the old status observer connected");
  const secondRemoteStatus = {textContent: "Connecting"};
  oldObserver.frame.contentDocument = {
    URL: oldObserver.frame.src,
    documentElement: {style: {}}, body: {style: {}},
    getElementById(name) {
      if (name === "status") return secondRemoteStatus;
      if (name === "screen") return observerScreen;
      return null;
    },
  };
  oldObserver.frame.listeners.load();
  assert(activeTimers(oldObserver, 10000).length === 1, "new viewer attempt has no RFB watchdog");
  firstRemoteStatus.textContent = "Connected to stale browser";
  firstObserver.callback();
  assert(activeTimers(oldObserver, 10000).length === 1, "queued old observer cleared the new RFB watchdog");
  assert(oldObserver.elements.status.dataset.state !== "connected", "queued old observer marked the new attempt connected");
  secondRemoteStatus.textContent = "Connected to current browser";
  oldObserver.observers[1].callback();
  assert(activeTimers(oldObserver, 10000).length === 0, "current observer did not clear its RFB watchdog");

  const stale = viewer([
    {status: 200, body: {status: "open"}},
    {status: 200, body: {status: "closed"}},
  ]);
  await runTimer(stale, 0);
  const remoteStatus = {textContent: "Disconnected"};
  const remoteScreen = {style: {}, addEventListener() {}, querySelector() { return null; }};
  stale.frame.contentDocument = {
    URL: stale.frame.src,
    documentElement: {style: {}}, body: {style: {}},
    getElementById(name) {
      if (name === "status") return remoteStatus;
      if (name === "screen") return remoteScreen;
      return null;
    },
  };
  stale.frame.listeners.load();
  stale.observers[0].callback();
  stale.observers[0].callback();
  assert(activeTimers(stale, 1500).length === 1, "observer notifications created overlapping retries");
  await runTimer(stale, 1500);
  const terminalMessage = stale.elements.status.textContent;
  stale.observers[0].callback();
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
