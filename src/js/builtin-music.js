import {fetchJson, joinPath} from './usb.js';
import {resolveAppUrl, withAssetVersion} from './compat.js';
import {APP_VERSION} from './version.js';

const BUILTIN_MANIFEST = 'assets/music/manifest.json';
const BUILTIN_BASE = 'assets/music/';

/**
 * Hard-coded list matching assets/music/manifest.json.
 * Used when XHR/fetch of the manifest fails (common on webOS 4.x local files).
 */
export const BUILTIN_MUSIC_FALLBACK = [
  {id: 'midnight-lounge', title: 'Midnight Lounge', artist: 'Ambient', file: 'midnight-lounge.mp3'},
  {id: 'starlight-drift', title: 'Starlight Drift', artist: 'Ambient', file: 'starlight-drift.mp3'},
  {id: 'ocean-haze', title: 'Ocean Haze', artist: 'Ambient', file: 'ocean-haze.mp3'},
  {id: 'warm-glow', title: 'Warm Glow', artist: 'Ambient', file: 'warm-glow.mp3'},
  {id: 'backbay-lounge', title: 'Backbay Lounge', artist: 'Jazz', file: 'backbay-lounge.mp3'},
  {id: 'chill-wave', title: 'Chill Wave', artist: 'Relaxing', file: 'chill-wave.mp3'}
];

let manifestCache = null;

export function normalizeMusicConfig(music) {
  const out = Object.assign({}, music || {});

  if (typeof out.enabled !== 'boolean') {
    out.enabled = true;
  }
  if (!out.source) {
    out.source = out.path ? 'usb' : 'builtin';
  }
  if (!out.builtin) {
    out.builtin = 'midnight-lounge';
  }
  // Which packaged tracks to rotate (ids). Empty = all built-ins.
  if (!Array.isArray(out.builtinPlaylist)) {
    out.builtinPlaylist = [];
  }
  // Which USB tracks to play (urls). Empty = all discovered in folder.
  if (!Array.isArray(out.usbPlaylist)) {
    out.usbPlaylist = [];
  }
  // Track-title chip (music bar). Default off — compact volume only.
  if (typeof out.showBar !== 'boolean') {
    out.showBar = false;
  }
  if (typeof out.volume !== 'number') {
    out.volume = 0.35;
  }

  return out;
}

function normalizeManifestList(list) {
  if (!Array.isArray(list) || !list.length) return null;
  const cleaned = list.filter(function (entry) {
    return entry && entry.id && entry.file;
  });
  return cleaned.length ? cleaned : null;
}

export async function loadBuiltinMusicManifest() {
  if (manifestCache && manifestCache.length) return manifestCache;

  // Always seed with the hard-coded list so music works even if XHR of
  // manifest.json fails (common on webOS local file://).
  manifestCache = BUILTIN_MUSIC_FALLBACK.slice();

  try {
    // Prefer relative URL first — more reliable than absolute file:// for XHR.
    const candidates = [
      withAssetVersion(BUILTIN_MANIFEST, APP_VERSION),
      BUILTIN_MANIFEST,
      withAssetVersion(resolveAppUrl(BUILTIN_MANIFEST), APP_VERSION),
      resolveAppUrl(BUILTIN_MANIFEST)
    ];
    for (let i = 0; i < candidates.length; i += 1) {
      const url = candidates[i];
      if (!url) continue;
      try {
        const manifest = await fetchJson(url);
        const loaded = normalizeManifestList(manifest && manifest.tracks);
        if (loaded && loaded.length) {
          manifestCache = loaded;
          break;
        }
      } catch (err) {
        // try next candidate
      }
    }
  } catch (err) {
    manifestCache = BUILTIN_MUSIC_FALLBACK.slice();
  }

  if (!manifestCache || !manifestCache.length) {
    manifestCache = BUILTIN_MUSIC_FALLBACK.slice();
  }

  return manifestCache;
}

/** Preferred URL for a packaged track (absolute file:// when possible). */
export function builtinTrackUrl(file) {
  if (!file) return '';
  const rel = joinPath(BUILTIN_BASE, file);
  const absolute = resolveAppUrl(rel);
  if (absolute && absolute !== rel) return absolute;
  return withAssetVersion(rel, APP_VERSION);
}

/** URL candidates to try — relative first (most reliable for <audio> on webOS). */
export function builtinTrackCandidates(file) {
  if (!file) return [];
  const rel = joinPath(BUILTIN_BASE, file);
  const absolute = resolveAppUrl(rel);
  const out = [];
  function push(u) {
    if (u && out.indexOf(u) < 0) out.push(u);
  }
  // Packaged relative paths load best in WAM for media elements.
  push(rel);
  push(withAssetVersion(rel, APP_VERSION));
  // Absolute file:// last — some webOS builds block file:// media.
  push(absolute);
  return out;
}

export function getBuiltinTrackUrl(id, manifest) {
  const list = manifest || manifestCache || BUILTIN_MUSIC_FALLBACK;
  const entry = list.find(function (item) {
    return item.id === id;
  });

  if (!entry) return '';
  return builtinTrackUrl(entry.file);
}

function toTrack(entry) {
  return {
    url: builtinTrackUrl(entry.file),
    urls: builtinTrackCandidates(entry.file),
    title: entry.title || entry.id,
    artist: entry.artist || '',
    description: entry.description || '',
    id: entry.id,
    file: entry.file
  };
}

export async function resolveBuiltinTrack(id) {
  const manifest = await loadBuiltinMusicManifest();
  const entry = manifest.find(function (item) {
    return item.id === id;
  }) || manifest[0];

  if (!entry) return null;

  return toTrack(entry);
}

/**
 * @param {string} startId preferred first track
 * @param {string[]} [enabledIds] subset of manifest ids; empty/omitted = all
 */
export async function resolveBuiltinPlaylist(startId, enabledIds) {
  let manifest = await loadBuiltinMusicManifest();
  if (!manifest || !manifest.length) {
    manifest = BUILTIN_MUSIC_FALLBACK.slice();
  }

  let list = manifest;
  if (enabledIds && enabledIds.length) {
    const allowed = {};
    enabledIds.forEach(function (id) {
      if (id) allowed[id] = true;
    });
    list = manifest.filter(function (entry) {
      return allowed[entry.id];
    });
    // If user disabled everything or ids were removed from package, fall back.
    if (!list.length) list = manifest.slice();
  }

  if (!list.length) {
    list = BUILTIN_MUSIC_FALLBACK.slice();
  }

  let startIndex = list.findIndex(function (item) {
    return item.id === startId;
  });
  if (startIndex < 0) startIndex = 0;

  const ordered = list.slice(startIndex).concat(list.slice(0, startIndex));
  return ordered.map(toTrack);
}
