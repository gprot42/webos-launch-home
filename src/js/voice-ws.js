/**
 * Socket to Launch Home's own voice assistant (voice/daemon, ws://127.0.0.1:8678).
 *
 * The assistant sends {event, payload} messages (listening state, appLaunch,
 * transcriptFinal) and answers AI Voice settings requests as configResult.
 * On connect Launch Home says hello and only uses the socket once the reply
 * names Launch Home's service, so another voice assistant on this TV (e.g.
 * VoxRelay, port 8677) is never talked to. Runs only while voice is turned
 * on in Settings; one shared connection serves the badge, voice launching
 * and settings.
 */

const WS_URI = 'ws://127.0.0.1:8678';
const SERVICE_NAME = 'launch-home-voice';
const HELLO_ID = 'lh-hello';

let socket = null;
let opening = null;
let retryTimer = null;
let retryCount = 0;
let stopped = true;
// Voice turned on in Settings (launcher.voiceEnabled).
let enabled = false;
const listeners = [];

function notify(eventName, payload) {
  for (let i = 0; i < listeners.length; i += 1) {
    try {
      listeners[i](eventName, payload || {});
    } catch (err) {
      /* ignore listener errors */
    }
  }
}

function scheduleRetry() {
  if (stopped || !enabled) return;
  clearTimeout(retryTimer);
  retryCount += 1;
  // First retries are aggressive so a dropped sessionStarted is recovered fast.
  const delay = retryCount <= 3
    ? 80 * retryCount
    : Math.min(4000, 200 + retryCount * 150);
  retryTimer = setTimeout(function () {
    connect().catch(function () { /* next retry is already scheduled */ });
  }, delay);
}

function connect() {
  if (stopped) return Promise.reject(new Error('Voice socket stopped'));
  if (!enabled) return Promise.reject(new Error('Voice assistant is off'));
  if (socket && socket.readyState === 1) return Promise.resolve(socket);
  if (opening) return opening;

  opening = new Promise(function (resolve, reject) {
    let ws;
    let verified = false;
    try {
      ws = new WebSocket(WS_URI);
    } catch (err) {
      opening = null;
      scheduleRetry();
      reject(err);
      return;
    }
    function fail(message) {
      clearTimeout(timer);
      if (opening) {
        opening = null;
        reject(new Error(message));
      }
      try { ws.close(); } catch (e) { /* ignore */ }
    }
    // Covers both the connect and the hello reply; onclose then retries.
    const timer = setTimeout(function () {
      fail('Voice socket timeout');
    }, 8000);
    ws.onopen = function () {
      try {
        ws.send(JSON.stringify({type: 'hello', id: HELLO_ID, params: {role: 'launcher'}}));
      } catch (err) {
        fail('Voice socket hello failed');
      }
    };
    ws.onmessage = function (ev) {
      let data = null;
      try {
        data = JSON.parse(ev.data);
      } catch (err) {
        return;
      }
      if (!data) return;
      if (!verified) {
        if (data.event !== 'configResult' || data.id !== HELLO_ID) return;
        if (!data.ok || !data.result || data.result.service !== SERVICE_NAME) {
          fail('Port 8678 is not Launch Home\'s voice service');
          return;
        }
        verified = true;
        clearTimeout(timer);
        socket = ws;
        opening = null;
        retryCount = 0;
        resolve(ws);
        return;
      }
      // Session events: {event, payload}. Config RPC: {event, id, ok, result}.
      // Pass the full message so listeners can read id/ok or payload.
      const payload = data.payload != null
        ? Object.assign({id: data.id, ok: data.ok}, data.payload)
        : data;
      notify(data.event, payload);
    };
    ws.onerror = function () {
      try { ws.close(); } catch (err) { /* ignore */ }
    };
    ws.onclose = function () {
      if (socket === ws) socket = null;
      if (!verified) fail('Voice socket closed');
      scheduleRetry();
    };
  });
  return opening;
}

function closeSocket() {
  clearTimeout(retryTimer);
  retryTimer = null;
  if (socket) {
    try { socket.close(); } catch (err) { /* ignore */ }
    socket = null;
  }
  opening = null;
}

/** Voice turned on/off in Settings: connect (if started) or drop the socket. */
export function setVoiceWsEnabled(on) {
  enabled = !!on;
  if (!enabled) {
    closeSocket();
    return;
  }
  retryCount = 0;
  if (!stopped) connect().catch(function () { /* retries on its own */ });
}

export function startVoiceWs() {
  stopped = false;
  if (enabled) connect().catch(function () { /* retries on its own */ });
}

export function stopVoiceWs() {
  stopped = true;
  closeSocket();
}

export function addVoiceListener(fn) {
  if (typeof fn !== 'function') return function () {};
  listeners.push(fn);
  return function remove() {
    const idx = listeners.indexOf(fn);
    if (idx >= 0) listeners.splice(idx, 1);
  };
}

export function ensureVoiceWs() {
  stopped = false;
  return connect();
}

export function voiceSend(obj) {
  return ensureVoiceWs().then(function (ws) {
    ws.send(JSON.stringify(obj));
  });
}

export function isVoiceWsOpen() {
  return !!(socket && socket.readyState === 1);
}
