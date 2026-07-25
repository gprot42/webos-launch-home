import {fetchJson, joinPath} from './usb.js';

const BUILTIN_MANIFEST = 'assets/music/manifest.json';
const BUILTIN_BASE = 'assets/music/';

let manifestCache = null;

export function normalizeMusicConfig(music) {
  const out = Object.assign({}, music || {});

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

  return out;
}

export async function loadBuiltinMusicManifest() {
  if (manifestCache) return manifestCache;

  try {
    const manifest = await fetchJson(BUILTIN_MANIFEST);
    manifestCache = (manifest && manifest.tracks) || [];
  } catch (err) {
    manifestCache = [];
  }

  return manifestCache;
}

export function getBuiltinTrackUrl(id, manifest) {
  const list = manifest || manifestCache || [];
  const entry = list.find(function (item) {
    return item.id === id;
  });

  if (!entry) return '';
  return joinPath(BUILTIN_BASE, entry.file);
}

function toTrack(entry) {
  return {
    url: joinPath(BUILTIN_BASE, entry.file),
    title: entry.title || entry.id,
    artist: entry.artist || '',
    description: entry.description || '',
    id: entry.id
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
  const manifest = await loadBuiltinMusicManifest();
  if (!manifest.length) return [];

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

  let startIndex = list.findIndex(function (item) {
    return item.id === startId;
  });
  if (startIndex < 0) startIndex = 0;

  const ordered = list.slice(startIndex).concat(list.slice(0, startIndex));
  return ordered.map(toTrack);
}
