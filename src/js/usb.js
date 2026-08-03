import {compatFetch} from './compat.js';
import {getStorageDevices} from './luna.js';

const AUDIO_EXT = ['.mp3', '.aac', '.flac', '.ogg', '.m4a', '.wav'];
const IMAGE_EXT = ['.jpg', '.jpeg', '.png', '.webp', '.gif'];

function stripUrlQuery(name) {
  // "assets/music/foo.mp3?v=0.0.58" must still match .mp3
  const s = String(name || '');
  const q = s.indexOf('?');
  const h = s.indexOf('#');
  let end = s.length;
  if (q >= 0 && q < end) end = q;
  if (h >= 0 && h < end) end = h;
  return s.slice(0, end);
}

export function isAudioFile(name) {
  const lower = stripUrlQuery(name).toLowerCase();
  return AUDIO_EXT.some(function (ext) {
    return lower.endsWith(ext);
  });
}

export function isImageFile(name) {
  const lower = stripUrlQuery(name).toLowerCase();
  return IMAGE_EXT.some(function (ext) {
    return lower.endsWith(ext);
  });
}

export function normalizeTrack(entry, basePath) {
  if (typeof entry === 'string') {
    const url = entry.indexOf('/') === 0 ? entry : joinPath(basePath, entry);
    return {url: url, title: '', artist: ''};
  }

  if (entry && typeof entry === 'object' && entry.file) {
    const url = entry.file.indexOf('/') === 0 ? entry.file : joinPath(basePath, entry.file);
    return {
      url: url,
      title: entry.title || '',
      artist: entry.artist || ''
    };
  }

  return null;
}

export async function findLoungeRoots() {
  const roots = [];

  try {
    const res = await getStorageDevices();
    const devices = (res && res.storageDeviceList) || [];

    for (const device of devices) {
      const drives = device.storageDriveList || [];
      for (const drive of drives) {
        if (!drive.isMounted || !drive.mountName) continue;
        roots.push(joinPath(drive.mountName, 'lounge'));
      }
    }
  } catch (err) {
    // USB discovery may fail in browser dev mode.
  }

  return roots;
}

export function joinPath(base, part) {
  if (!base) return part;
  if (base.endsWith('/')) return base + part;
  return base + '/' + part;
}

export async function fetchText(url) {
  const res = await compatFetch(url);
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.text();
}

export async function fetchJson(url) {
  const text = await fetchText(url);
  return JSON.parse(text);
}

function resolveTrackUrl(trimmed, basePath) {
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  if (trimmed.indexOf('/') === 0) return trimmed;
  return joinPath(basePath, trimmed);
}

export function parseM3u(text, basePath) {
  const tracks = [];
  const lines = text.split(/\r?\n/);
  let pendingMeta = null;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;

    if (trimmed.indexOf('#EXTINF:') === 0) {
      const meta = trimmed.slice('#EXTINF:'.length);
      const comma = meta.indexOf(',');
      const titlePart = comma >= 0 ? meta.slice(comma + 1).trim() : '';
      let artist = '';
      let title = titlePart;
      const dash = titlePart.indexOf(' - ');

      if (dash >= 0) {
        artist = titlePart.slice(0, dash).trim();
        title = titlePart.slice(dash + 3).trim();
      }

      pendingMeta = {artist: artist, title: title};
      continue;
    }

    if (trimmed.charAt(0) === '#') continue;

    const url = resolveTrackUrl(trimmed, basePath);
    tracks.push({
      url: url,
      title: (pendingMeta && pendingMeta.title) || '',
      artist: (pendingMeta && pendingMeta.artist) || ''
    });
    pendingMeta = null;
  }

  return tracks;
}

export async function loadUsbConfig() {
  const roots = await findLoungeRoots();

  for (const root of roots) {
    try {
      const config = await fetchJson(joinPath(root, 'config.json'));
      return {root: root, config: config};
    } catch (err) {
      // Try next volume.
    }
  }

  return null;
}

export async function discoverMusicTracks(musicPath) {
  if (!musicPath) return [];

  const playlistUrl = joinPath(musicPath, 'playlist.m3u');
  try {
    const text = await fetchText(playlistUrl);
    const tracks = parseM3u(text, musicPath).filter(function (track) {
      return isAudioFile(track.url);
    });
    if (tracks.length) return tracks;
  } catch (err) {
    // Fall through to manifest.
  }

  const manifestUrl = joinPath(musicPath, 'tracks.json');
  try {
    const manifest = await fetchJson(manifestUrl);
    if (Array.isArray(manifest)) {
      return manifest
        .map(function (entry) {
          return normalizeTrack(entry, musicPath);
        })
        .filter(Boolean)
        .filter(function (track) {
          return isAudioFile(track.url);
        });
    }
  } catch (err) {
    // No manifest.
  }

  return [];
}

export async function discoverBackgroundImages(backgroundPath) {
  if (!backgroundPath) return [];

  const manifestUrl = joinPath(backgroundPath, 'images.json');
  try {
    const manifest = await fetchJson(manifestUrl);
    if (Array.isArray(manifest)) {
      return manifest
        .map(function (entry) {
          if (typeof entry === 'string') {
            return entry.indexOf('/') === 0 ? entry : joinPath(backgroundPath, entry);
          }
          return null;
        })
        .filter(Boolean)
        .filter(isImageFile);
    }
  } catch (err) {
    // No manifest.
  }

  return [];
}

export async function resolveLoungePaths(config) {
  const usb = await loadUsbConfig();
  const roots = usb ? [usb.root] : await findLoungeRoots();
  const root = roots[0] || '';

  const musicPath = (config.music && config.music.path) || (root ? joinPath(root, 'music') : '');
  const backgroundPath = (config.background && config.background.path) || (root ? joinPath(root, 'backgrounds') : '');

  return {
    root: root,
    usbConfig: usb ? usb.config : null,
    musicPath: musicPath,
    backgroundPath: backgroundPath
  };
}