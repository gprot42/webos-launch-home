/**
 * Top-right mic + AI badge while VoxRelay is capturing voice.
 *
 * Trust only the daemon:
 *  - WebSocket sessionStarted / sessionCatchup (capture_active)
 *  - /tmp/voxrelay-voice-state.json (listening / capture_active)
 *
 * Do NOT show from remote keydown — LG key codes are unreliable and lit the
 * badge without KEY_VOICE. Hide aggressively when the daemon says idle.
 */

import {execRoot} from './luna.js';
import {
  startVoxrelayWs,
  addVoxrelayListener
} from './voxrelay-ws.js';

const STATE_PATH = '/tmp/voxrelay-voice-state.json';
const POLL_MS = 250;
/** Without a fresh live signal, hide (missed sessionEnded / stuck flag). */
const LIVE_STALE_MS = 1500;
/** Absolute ceiling if something goes wrong. */
const MAX_VISIBLE_MS = 8000;
/** State-file ts must be this fresh to count as live. */
const STATE_FRESH_SEC = 6;

function payloadIsMicLive(payload) {
  if (!payload || typeof payload !== 'object') return false;
  // Only the explicit mic flag. capture_active stays true through answer /
  // overlay catch-up and was leaving the badge stuck on "Listening".
  if (payload.listening === true) return true;
  if (payload.early === true) return true;
  return false;
}

export function createVoiceIndicator(rootEl) {
  if (!rootEl) {
    return {start: function () {}, stop: function () {}, show: function () {}, hide: function () {}};
  }

  let visible = false;
  let maxTimer = null;
  let pollTimer = null;
  let staleTimer = null;
  let stopped = true;
  let sessionGen = 0;
  let pollInFlight = false;
  let removeWsListener = null;
  /** Last time we got a positive live signal from daemon. */
  let lastLiveAt = 0;

  function paint(on) {
    if (on) {
      rootEl.hidden = false;
      rootEl.removeAttribute('hidden');
      rootEl.setAttribute('aria-hidden', 'false');
      rootEl.classList.add('visible');
      rootEl.classList.add('instant');
      void rootEl.offsetWidth;
      rootEl.classList.remove('instant');
    } else {
      rootEl.classList.remove('visible');
      rootEl.classList.remove('instant');
      rootEl.setAttribute('aria-hidden', 'true');
      rootEl.hidden = true;
      rootEl.setAttribute('hidden', '');
    }
  }

  function setVisible(on) {
    const next = !!on;
    if (next === visible) {
      if (next) armTimers();
      return;
    }
    visible = next;
    paint(visible);
    if (visible) armTimers();
    else clearTimers();
  }

  function clearTimers() {
    clearTimeout(maxTimer);
    maxTimer = null;
    clearTimeout(staleTimer);
    staleTimer = null;
  }

  function armTimers() {
    clearTimeout(maxTimer);
    clearTimeout(staleTimer);
    const gen = sessionGen;
    maxTimer = setTimeout(function () {
      if (gen !== sessionGen) return;
      setVisible(false);
    }, MAX_VISIBLE_MS);
    // If daemon goes quiet (no live poll/WS), drop the badge quickly.
    staleTimer = setTimeout(function () {
      if (gen !== sessionGen) return;
      if (Date.now() - lastLiveAt >= LIVE_STALE_MS) setVisible(false);
    }, LIVE_STALE_MS + 50);
  }

  function markLive() {
    lastLiveAt = Date.now();
    sessionGen += 1;
    setVisible(true);
  }

  function endSession() {
    sessionGen += 1;
    lastLiveAt = 0;
    setVisible(false);
  }

  function handleEvent(eventName, payload) {
    const ev = String(eventName || '');
    if (ev === 'sessionStarted') {
      // Overlay catch-up / error-card relaunch also broadcasts sessionStarted.
      // Only show for a real listen (early press or listening:true).
      if (payload && payload.overlayOnly) return;
      if (payloadIsMicLive(payload) ||
          (payload && payload.reason === 'button_press') ||
          (payload && payload.reason === 'session_started')) {
        markLive();
      }
      return;
    }
    if (ev === 'sessionCatchup') {
      if (payloadIsMicLive(payload)) markLive();
      else endSession();
      return;
    }
    if (ev === 'status') {
      // Never show from status text alone ("Listening…" sticks after sessions).
      return;
    }
    if (ev === 'listeningEnded' || ev === 'sessionEnded' || ev === 'error') {
      endSession();
    }
  }

  function pollStateFile() {
    if (stopped || pollInFlight) return;
    pollInFlight = true;
    execRoot('cat ' + STATE_PATH + ' 2>/dev/null || true')
      .then(function (res) {
        let text = (res && res.stdoutString ? String(res.stdoutString) : '').trim();
        if (!text && res && res.stdoutBytes) {
          try { text = String(atob(res.stdoutBytes)).trim(); } catch (err) { /* ignore */ }
        }
        if (!text) {
          if (visible) endSession();
          return;
        }
        let json = null;
        try {
          json = JSON.parse(text);
        } catch (err) {
          return;
        }
        if (!json) return;
        const ts = Number(json.ts) || 0;
        const age = ts > 0 ? (Date.now() / 1000 - ts) : 9999;
        const fresh = age >= 0 && age <= STATE_FRESH_SEC;
        // Mic open only. capture_active/session_live stay true after the
        // utterance and were keeping this badge on.
        const micLive = json.listening === true;
        if (micLive && fresh) {
          markLive();
        } else {
          // Idle or stale live flag — always hide.
          if (visible) endSession();
        }
      })
      .catch(function () { /* ignore */ })
      .then(function () {
        pollInFlight = false;
      });
  }

  function start() {
    if (!stopped) return;
    stopped = false;
    endSession();
    startVoxrelayWs();
    if (removeWsListener) removeWsListener();
    removeWsListener = addVoxrelayListener(handleEvent);
    clearInterval(pollTimer);
    pollTimer = setInterval(pollStateFile, POLL_MS);
    setTimeout(pollStateFile, 40);
    // No keydown path — LG remote codes falsely triggered the badge.
  }

  function stop() {
    stopped = true;
    clearInterval(pollTimer);
    pollTimer = null;
    clearTimers();
    if (removeWsListener) {
      removeWsListener();
      removeWsListener = null;
    }
    endSession();
  }

  return {
    start: start,
    stop: stop,
    show: function () { markLive(); },
    hide: function () { endSession(); }
  };
}
