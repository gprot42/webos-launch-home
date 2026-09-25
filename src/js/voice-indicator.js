/**
 * Top-right mic + AI badge while a voice assistant is listening.
 *
 * Driven only by the assistant's events on the local socket (voice-ws.js):
 *   sessionStarted / sessionCatchup {listening | early | reason}  → show
 *   listeningEnded / sessionEnded / error                         → hide
 * Nothing is read from the assistant's files. (It used to poll a state file
 * through a root shell every 250ms, even while other apps were in front.)
 *
 * Do NOT show from remote keydown — LG key codes are unreliable and lit the
 * badge without KEY_VOICE. Hide aggressively when the service says idle.
 */

import {
  startVoiceWs,
  addVoiceListener
} from './voice-ws.js';

/** Absolute ceiling if a sessionEnded is missed. */
const MAX_VISIBLE_MS = 8000;

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
  let stopped = true;
  let sessionGen = 0;
  let removeWsListener = null;

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
  }

  function armTimers() {
    clearTimeout(maxTimer);
    const gen = sessionGen;
    maxTimer = setTimeout(function () {
      if (gen !== sessionGen) return;
      setVisible(false);
    }, MAX_VISIBLE_MS);
  }

  function markLive() {
    sessionGen += 1;
    setVisible(true);
  }

  function endSession() {
    sessionGen += 1;
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

  function start() {
    if (!stopped) return;
    stopped = false;
    endSession();
    startVoiceWs();
    if (removeWsListener) removeWsListener();
    removeWsListener = addVoiceListener(handleEvent);
    // No keydown path — LG remote codes falsely triggered the badge.
  }

  function stop() {
    stopped = true;
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
