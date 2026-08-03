/**
 * Shared VoxRelay WebSocket (ws://127.0.0.1:8677).
 *
 * VoxRelay used to allow only one client and closed prior sockets on connect.
 * Launch Home still keeps a single connection so config RPC + the voice badge
 * share one pipe (no reconnect thrash when Settings loads AI options).
 */

const WS_URI = 'ws://127.0.0.1:8677';

let socket = null;
let opening = null;
let retryTimer = null;
let retryCount = 0;
let stopped = true;
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
  if (stopped) return;
  clearTimeout(retryTimer);
  retryCount += 1;
  // First retries are aggressive so a dropped sessionStarted is recovered fast.
  const delay = retryCount <= 3
    ? 80 * retryCount
    : Math.min(4000, 200 + retryCount * 150);
  retryTimer = setTimeout(connect, delay);
}

function connect() {
  if (stopped) return;
  if (socket && (socket.readyState === 0 || socket.readyState === 1)) return;
  if (opening) return opening;

  opening = new Promise(function (resolve, reject) {
    let ws;
    try {
      ws = new WebSocket(WS_URI);
    } catch (err) {
      opening = null;
      scheduleRetry();
      reject(err);
      return;
    }
    const timer = setTimeout(function () {
      try { ws.close(); } catch (e) { /* ignore */ }
      if (opening) {
        opening = null;
        scheduleRetry();
        reject(new Error('VoxRelay WebSocket timeout'));
      }
    }, 8000);
    ws.onopen = function () {
      clearTimeout(timer);
      socket = ws;
      opening = null;
      retryCount = 0;
      resolve(ws);
    };
    ws.onmessage = function (ev) {
      let data = null;
      try {
        data = JSON.parse(ev.data);
      } catch (err) {
        return;
      }
      if (!data) return;
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
      if (opening) {
        clearTimeout(timer);
        opening = null;
      }
      scheduleRetry();
    };
  });
  return opening;
}

export function startVoxrelayWs() {
  stopped = false;
  connect();
}

export function stopVoxrelayWs() {
  stopped = true;
  clearTimeout(retryTimer);
  retryTimer = null;
  if (socket) {
    try { socket.close(); } catch (err) { /* ignore */ }
    socket = null;
  }
  opening = null;
}

export function addVoxrelayListener(fn) {
  if (typeof fn !== 'function') return function () {};
  listeners.push(fn);
  return function remove() {
    const idx = listeners.indexOf(fn);
    if (idx >= 0) listeners.splice(idx, 1);
  };
}

export function ensureVoxrelayWs() {
  stopped = false;
  if (socket && socket.readyState === 1) return Promise.resolve(socket);
  return connect();
}

export function voxrelaySend(obj) {
  return ensureVoxrelayWs().then(function (ws) {
    ws.send(JSON.stringify(obj));
  });
}

export function isVoxrelayWsOpen() {
  return !!(socket && socket.readyState === 1);
}
