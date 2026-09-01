/**
 * VoxRelay config client for Launch Home settings.
 *
 * Primary path: shared WebSocket RPC to the VoxRelay daemon
 * (ws://127.0.0.1:8677) — same connection the voice badge uses, so Settings
 * and the mic indicator never kick each other off the single daemon port.
 * Fallback: read/write /home/root/.config/voxrelay/config.json via Homebrew
 * Channel root exec.
 */

import {execRoot} from './luna.js';
import {
  ensureVoxrelayWs,
  voxrelaySend,
  addVoxrelayListener
} from './voxrelay-ws.js';

const CONFIG_PATH = '/home/root/.config/voxrelay/config.json';
const XAI_ERROR_PATH = '/tmp/voxrelay-xai-error.json';
const WS_TIMEOUT_MS = 8000;

export const TTS_VOICES = [
  'carina', 'zagan', 'helix', 'orion', 'luna', 'iris', 'altair',
  'zenith', 'perseus', 'helios', 'lux', 'kepler', 'rigel', 'cosmo',
  'celeste', 'ursa', 'sirius', 'lumen', 'castor', 'naksh', 'atlas'
];

export const CHAT_MODELS = [
  {value: 'grok-4.6', label: 'grok-4.6'},
  {value: 'grok-4.5', label: 'grok-4.5 (fallback)'}
];

export const GEMINI_MODELS = [
  {value: 'gemini-3.7-flash', label: 'Gemini 3.7 Flash'},
  {value: 'gemini-3.1-pro-preview', label: 'Gemini 3.1 Pro'},
  {value: 'gemini-3.5-live-translate-preview', label: 'Live translate preview'}
];

export const GEMINI_STT_MODELS = [
  {value: 'gemini-3.5-transcribe', label: 'Gemini 3.5 Transcribe'},
  {value: 'gemini-3.7-flash', label: 'Gemini 3.7 Flash'}
];

export const OPENROUTER_MODELS = [
  {value: 'openai/gpt-4o-mini', label: 'GPT-4o mini'},
  {value: 'openai/gpt-4o', label: 'GPT-4o'},
  {value: 'google/gemini-3.7-flash', label: 'Gemini 3.7 Flash'},
  {value: 'anthropic/claude-sonnet-4', label: 'Claude Sonnet 4'},
  {value: 'x-ai/grok-4', label: 'Grok 4'}
];

/** OpenRouter speech-to-text models (usage ranking, Aug 2026). */
export const OPENROUTER_STT_MODELS = [
  {value: 'openai/gpt-4o-mini-transcribe', label: 'GPT-4o Mini Transcribe'},
  {value: 'openai/gpt-4o-transcribe', label: 'GPT-4o Transcribe'},
  {value: 'mistralai/voxtral-mini-transcribe', label: 'Voxtral Mini Transcribe'},
  {value: 'nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b', label: 'Nemotron ASR 0.6B'},
  {value: 'mistralai/voxtral-small-24b-2507-stt', label: 'Voxtral Small STT'},
  {value: 'openai/whisper-large-v3-turbo', label: 'Whisper Large V3 Turbo'},
  {value: 'openai/whisper-1', label: 'Whisper 1'},
  {value: 'x-ai/grok-stt-1.0', label: 'Grok STT 1.0'},
  {value: 'deepgram/nova-3', label: 'Deepgram Nova-3'},
  {value: 'google/chirp-3', label: 'Google Chirp 3'}
];

export const VOICE_MODELS = [
  {value: 'grok-voice-think-fast-2.0', label: 'Voice think fast 2.0'},
  {value: 'grok-voice-think-fast-1.0', label: 'Voice think fast 1.0'}
];

export const STT_LANGUAGES = [
  {value: 'en', label: 'English'},
  {value: 'es', label: 'Spanish'},
  {value: 'fr', label: 'French'},
  {value: 'de', label: 'German'},
  {value: 'it', label: 'Italian'},
  {value: 'pt', label: 'Portuguese'},
  {value: 'ja', label: 'Japanese'},
  {value: 'ko', label: 'Korean'},
  {value: 'zh', label: 'Chinese'},
  {value: 'vi', label: 'Vietnamese'}
];

let wsSeq = 0;
const pending = {};

function shellQuote(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function readExecStdout(res) {
  let text = (res && res.stdoutString ? String(res.stdoutString) : '').trim();
  if (!text && res && res.stdoutBytes) {
    try { text = String(atob(res.stdoutBytes)).trim(); } catch (err) { /* ignore */ }
  }
  return text;
}

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
  else p.reject(new Error(payload.error || 'VoxRelay config request failed'));
});

function wsCall(method, params) {
  return ensureVoxrelayWs().then(function () {
    return new Promise(function (resolve, reject) {
      const id = 'lh' + (++wsSeq);
      const timer = setTimeout(function () {
        if (pending[id]) {
          delete pending[id];
          reject(new Error('VoxRelay request timed out'));
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

function fallbackGetConfig() {
  return execRoot('cat ' + shellQuote(CONFIG_PATH) + ' 2>/dev/null || echo {}')
    .then(function (res) {
      const text = readExecStdout(res) || '{}';
      let cfg = {};
      try { cfg = JSON.parse(text); } catch (err) { cfg = {}; }
      const key = cfg.xai_api_key || '';
      const configured = key && key !== 'xai-...' && key.indexOf('•') < 0;
      return {
        returnValue: true,
        config: {
          xai_api_key_masked: configured
            ? (key.slice(0, 4) + '••••••••' + key.slice(-4))
            : '',
          xai_api_key_full: configured ? key : '',
          api_key_configured: !!configured,
          stt_language: cfg.stt_language || 'en',
          chat_model: cfg.chat_model || 'grok-4.6',
          voice_model: cfg.voice_model || 'grok-voice-think-fast-2.0',
          overlay_auto_dismiss_sec: cfg.overlay_auto_dismiss_sec || 12,
          close_native_aiplatform: cfg.close_native_aiplatform !== false,
          tts_enabled: cfg.tts_enabled !== false,
          tts_voice: cfg.tts_voice || 'iris',
          tts_speed: cfg.tts_speed != null ? cfg.tts_speed : 1.0,
          web_search: cfg.web_search !== false
        }
      };
    });
}

function fallbackSetConfig(updates) {
  // Merge into existing file via python so we do not clobber other keys.
  const payload = JSON.stringify(updates || {});
  const py =
    'python3 -c ' + shellQuote(
      'import json,os\n' +
      'p=' + JSON.stringify(CONFIG_PATH) + '\n' +
      'u=json.loads(' + JSON.stringify(payload) + ')\n' +
      'd={}\n' +
      'if os.path.exists(p):\n' +
      '  try: d=json.load(open(p))\n' +
      '  except Exception: d={}\n' +
      'd.update(u)\n' +
      'os.makedirs(os.path.dirname(p),exist_ok=True)\n' +
      'json.dump(d,open(p,"w"),indent=2)\n' +
      'print("ok")\n'
    ) +
    '; systemctl restart voxrelay.service >/dev/null 2>&1 || true; echo done';
  return execRoot(py).then(function (res) {
    const out = readExecStdout(res);
    if (out.indexOf('ok') < 0 && out.indexOf('done') < 0) {
      throw new Error('Could not write VoxRelay config');
    }
    return {returnValue: true, saved: true, restarting: true};
  });
}

export function getVoxrelayConfig() {
  return wsCall('getConfig', {})
    .catch(function () {
      return fallbackGetConfig();
    })
    .then(function (res) {
      return (res && res.config) || res || {};
    });
}

function readLastXaiErrorFile() {
  return execRoot('cat ' + shellQuote(XAI_ERROR_PATH) + ' 2>/dev/null || true')
    .then(function (res) {
      const text = readExecStdout(res);
      if (!text) return null;
      try {
        const obj = JSON.parse(text);
        if (obj && obj.message) return obj;
      } catch (err) { /* ignore */ }
      return null;
    })
    .catch(function () {
      return null;
    });
}

export function getVoxrelayStatus() {
  return wsCall('getStatus', {})
    .catch(function () {
      return execRoot(
        'systemctl is-active voxrelay.service 2>/dev/null || echo inactive'
      ).then(function (res) {
        const active = readExecStdout(res).indexOf('active') === 0;
        return {
          returnValue: true,
          daemonActive: active,
          apiKeyConfigured: false
        };
      });
    })
    .then(function (status) {
      const out = status || {};
      const provider = out.aiProvider;
      // Gemini / OpenRouter must not inherit leftover xAI credit/token errors
      // from getStatus or /tmp/voxrelay-xai-error.json.
      if (provider === 'gemini' || provider === 'openrouter') {
        if (out.lastXaiError) delete out.lastXaiError;
        return out;
      }
      if (out.lastXaiError && out.lastXaiError.message) {
        return out;
      }
      return readLastXaiErrorFile().then(function (err) {
        if (err) out.lastXaiError = err;
        return out;
      });
    });
}

export function setVoxrelayConfig(updates) {
  return wsCall('setConfig', updates || {})
    .catch(function () {
      return fallbackSetConfig(updates || {});
    });
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
