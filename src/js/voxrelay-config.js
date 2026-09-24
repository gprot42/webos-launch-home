/**
 * AI Voice settings client.
 *
 * Talks to a voice assistant only through its local event socket
 * (voxrelay-ws.js, ws://127.0.0.1:8677) with these requests:
 *   getConfig, setConfig, getStatus,
 *   startSuperGrokLogin, cancelSuperGrokLogin, signOutSuperGrok, importSuperGrokAuth
 *
 * Choice lists (models, languages) come from the service in config.options,
 * so this app carries none of them. No file paths, service names or restart
 * commands either: if nothing answers on the socket, the AI Voice tab says the
 * service isn't reachable. That keeps Launch Home and the voice service
 * independently releasable.
 */

import {
  ensureVoxrelayWs,
  voxrelaySend,
  addVoxrelayListener
} from './voxrelay-ws.js';

const WS_TIMEOUT_MS = 8000;

let wsSeq = 0;
const pending = {};

// configResult is {event, id, ok, result|error} at the message root.
addVoxrelayListener(function (eventName, payload) {
  if (eventName !== 'configResult') return;
  const id = payload && payload.id;
  if (id == null) return;
  const p = pending[id];
  if (!p) return;
  delete pending[id];
  clearTimeout(p.timer);
  if (payload.ok) p.resolve(payload.result || {});
  else p.reject(new Error(payload.error || 'Voice service request failed'));
});

function wsCall(method, params) {
  return ensureVoxrelayWs().then(function () {
    return new Promise(function (resolve, reject) {
      const id = 'lh' + (++wsSeq);
      const timer = setTimeout(function () {
        if (pending[id]) {
          delete pending[id];
          reject(new Error('Voice service request timed out'));
        }
      }, WS_TIMEOUT_MS);
      pending[id] = {resolve: resolve, reject: reject, timer: timer};
      voxrelaySend({type: method, params: params || {}, id: id}).catch(function (err) {
        delete pending[id];
        clearTimeout(timer);
        reject(err);
      });
    });
  });
}

/** Config values plus `options` (the service's own picker choices). */
export function getVoxrelayConfig() {
  return wsCall('getConfig', {}).then(function (res) {
    return (res && res.config) || res || {};
  });
}

export function getVoxrelayStatus() {
  return wsCall('getStatus', {}).then(function (status) {
    const out = status || {};
    // Gemini / OpenRouter must not show leftover xAI credit/token errors.
    if (out.aiProvider === 'gemini' || out.aiProvider === 'openrouter') {
      delete out.lastXaiError;
    }
    return out;
  });
}

export function setVoxrelayConfig(updates) {
  return wsCall('setConfig', updates || {});
}

export function startSuperGrokLogin() {
  return wsCall('startSuperGrokLogin', {});
}

export function cancelSuperGrokLogin() {
  return wsCall('cancelSuperGrokLogin', {});
}

export function signOutSuperGrok() {
  return wsCall('signOutSuperGrok', {});
}

export function importSuperGrokAuth(path) {
  return wsCall('importSuperGrokAuth', path ? {path: path} : {});
}
