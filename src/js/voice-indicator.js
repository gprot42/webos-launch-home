/**
 * Top-right mic + AI badge while VoxRelay is capturing voice.
 *
 * Fast paths (in order of latency):
 *  1. Magic Remote voice keydown (when the event reaches the app)
 *  2. Shared WebSocket sessionStarted / sessionCatchup / listeningEnded
 *  3. Poll /tmp/voxrelay-voice-state.json via root (daemon writes on press)
 *
 * The badge means "mic is live" — not "AI is thinking". It must not light up
 * from color buttons, stale session_live, or leftover status text.
 */

import {execRoot} from './luna.js';
import {
  startVoxrelayWs,
  addVoxrelayListener
} from './voxrelay-ws.js';

const STATE_PATH = '/tmp/voxrelay-voice-state.json';
/** Poll often — root cat is the safety net when WS is mid-reconnect. */
const POLL_MS = 200;
/** Hard cap so a missed sessionEnded cannot leave the badge forever. */
const MAX_VISIBLE_MS = 12000;
/** State-file ts older than this is ignored (stale / wrong clock). */
const STATE_FRESH_SEC = 12;

function isVoiceKey(ev) {
  if (!ev) return false;
  const code = Number(ev.keyCode || ev.which || 0);
  // Do NOT treat color keys as voice: RED=403 GREEN=404 YELLOW=405 BLUE=406.
  // Confirmed LG / webOS Magic Remote voice / mic codes (firmware-dependent).
  if (code === 407 || code === 0x199 || code === 1536) return true;
  const key = String(ev.key || '').toLowerCase();
  if (key === 'microphone' || key === 'audiovoice') return true;
  // Exact "voice" only — not substrings that match other identifiers.
  if (key === 'voice') return true;
  const id = String(ev.keyIdentifier || '');
  if (id === 'Voice' || id === 'Microphone' || id === 'AudioVoice') return true;
  return false;
}

function payloadIsMicLive(payload) {
  if (!payload || typeof payload !== 'object') return false;
  // Explicit flags only. Do not infer from status strings ("Listening…" can
  // linger in catch-up payloads after the mic has stopped).
  if (payload.listening === true) return true;
  if (payload.capture_active === true) return true;
  return false;
}

export function createVoiceIndicator(rootEl) {
  if (!rootEl) {
    return {start: function () {}, stop: function () {}, show: function () {}, hide: function () {}};
  }

  let visible = false;
  let maxTimer = null;
  let pollTimer = null;
  let stopped = true;
  let sessionGen = 0;
  let pollInFlight = false;
  let removeWsListener = null;
  /** Wall-clock of last positive mic signal (keydown / sessionStarted / poll). */
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
      if (next) armMaxHide();
      return;
    }
    visible = next;
    paint(visible);
    if (visible) armMaxHide();
    else {
      clearTimeout(maxTimer);
      maxTimer = null;
    }
  }

  function armMaxHide() {
    clearTimeout(maxTimer);
    const gen = sessionGen;
    maxTimer = setTimeout(function () {
      if (gen !== sessionGen) return;
      setVisible(false);
    }, MAX_VISIBLE_MS);
  }

  function beginSession() {
    sessionGen += 1;
    lastLiveAt = Date.now();
    setVisible(true);
  }

  function endSession() {
    sessionGen += 1;
    setVisible(false);
  }

  function handleEvent(eventName, payload) {
    const ev = String(eventName || '');
    if (ev === 'sessionStarted') {
      beginSession();
      return;
    }
    if (ev === 'sessionCatchup') {
      // Only show if the daemon says capture is still active — not merely that
      // the last status string was "Listening…".
      if (payloadIsMicLive(payload)) beginSession();
      else if (visible) endSession();
      return;
    }
    // Ignore bare "status" events for show — "Listening…" is also used as a
    // sticky last_status for the overlay and used to re-light the badge on every
    // reconnect / config RPC path. Hide only happens via sessionEnded etc.
    if (ev === 'status') {
      return;
    }
    if (ev === 'listeningEnded' || ev === 'sessionEnded' || ev === 'error') {
      // Mic badge tracks capture, not the answer/TTS phase.
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
          // No state file: if badge is up with no recent live signal, clear it.
          if (visible && Date.now() - lastLiveAt > 3000) endSession();
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
        // Reject future timestamps (clock skew) and stale entries.
        const fresh = age >= 0 && age <= STATE_FRESH_SEC;
        // Mic live = listening or capture_active only.
        // session_live stays true through answer/TTS and must NOT keep the mic up.
        const micLive = !!(json.listening || json.capture_active);
        if (micLive && fresh) {
          beginSession();
        } else if (visible) {
          // Explicit idle, or stale "live" flag — hide.
          if (!micLive || !fresh) endSession();
        }
      })
      .catch(function () { /* ignore */ })
      .then(function () {
        pollInFlight = false;
      });
  }

  function onKeyDown(ev) {
    // Instant feedback only for real voice keys (not RED/color keys).
    if (isVoiceKey(ev)) beginSession();
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
    document.addEventListener('keydown', onKeyDown, true);
  }

  function stop() {
    stopped = true;
    clearInterval(pollTimer);
    pollTimer = null;
    clearTimeout(maxTimer);
    maxTimer = null;
    document.removeEventListener('keydown', onKeyDown, true);
    if (removeWsListener) {
      removeWsListener();
      removeWsListener = null;
    }
    endSession();
  }

  return {
    start: start,
    stop: stop,
    show: function () { beginSession(); },
    hide: function () { endSession(); }
  };
}
