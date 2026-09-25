import {getAppInfo, listApps, readFileAsDataUrl} from './luna.js';
import {getAppIdCandidates, getBuiltinAppIcon, getBuiltinAppTitle} from './app-icons.js';
import {withAssetVersion} from './compat.js';
import {APP_VERSION} from './version.js';

// Cache of resolved native icons (file:// path -> data URI) so repeated
// renders don't re-read the same file over the root bus. Failures aren't
// cached: root may answer next time.
const nativeIconCache = new Map();

// What the TV said about each app (title, icon), kept across restarts. When
// the TV can't be asked (root through Homebrew Channel not answering, Luna
// late after power-on) a tile keeps its real name and icon instead of a name
// made from its id and two letters.
const APP_MEMORY_KEY = 'lounge.apps.v1';
// Home row icons the TV has (read as root), kept as images: they show without
// root, and without a root file read per icon at every start. Capped, since
// this shares the page's storage with the settings.
const ICON_MEMORY_KEY = 'lounge.appIcons.v1';
const ICON_MEMORY_MAX = 400000; // characters, all kept icons
const ICON_MAX = 60000; // characters, one icon
const ICON_RECHECK_MS = 7 * 24 * 60 * 60 * 1000;

function readStored(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || '{}');
    return value && typeof value === 'object' ? value : {};
  } catch (err) {
    return {};
  }
}

let appMemory = null;
let iconMemory = null;
let saveTimer = null;
const unsaved = {};

function appMemoryAll() {
  if (!appMemory) appMemory = readStored(APP_MEMORY_KEY);
  return appMemory;
}

function iconMemoryAll() {
  if (!iconMemory) iconMemory = readStored(ICON_MEMORY_KEY);
  return iconMemory;
}

// One write for a burst of changes (a catalog load remembers every app).
function saveSoon(key) {
  unsaved[key] = true;
  if (saveTimer) return;
  saveTimer = setTimeout(function () {
    saveTimer = null;
    if (unsaved[APP_MEMORY_KEY]) {
      unsaved[APP_MEMORY_KEY] = false;
      try {
        localStorage.setItem(APP_MEMORY_KEY, JSON.stringify(appMemoryAll()));
      } catch (err) { /* storage full: names are asked for again next time */ }
    }
    if (unsaved[ICON_MEMORY_KEY]) {
      unsaved[ICON_MEMORY_KEY] = false;
      try {
        localStorage.setItem(ICON_MEMORY_KEY, JSON.stringify(iconMemoryAll()));
      } catch (err) {
        // Never crowd out the settings: drop the kept icons instead.
        iconMemory = {};
        try { localStorage.removeItem(ICON_MEMORY_KEY); } catch (err2) { /* ignore */ }
      }
    }
  }, 1500);
}

function rememberApp(id, title, icon) {
  if (!id || !title) return;
  const all = appMemoryAll();
  const known = all[id];
  const next = {t: String(title), i: icon || (known && known.i) || ''};
  if (known && known.t === next.t && known.i === next.i) return;
  all[id] = next;
  saveSoon(APP_MEMORY_KEY);
}

/** Remember the titles and icons in TV app records (e.g. LG's launch points). */
export function rememberApps(records) {
  (records || []).forEach(function (record) {
    if (record) normalizeAppRecord(record);
  });
}

function knownApp(id) {
  const all = appMemoryAll();
  const ids = getAppIdCandidates(id);
  for (let i = 0; i < ids.length; i += 1) {
    if (all[ids[i]]) return all[ids[i]];
  }
  return null;
}

function keptIcon(path) {
  const kept = iconMemoryAll()[path];
  return kept && kept.d ? kept : null;
}

function keepIcon(path, dataUrl) {
  if (!dataUrl || dataUrl.length > ICON_MAX) return;
  const all = iconMemoryAll();
  all[path] = {d: dataUrl, at: Date.now()};
  // Oldest out first until under the cap.
  const paths = Object.keys(all).sort(function (a, b) {
    return (all[a].at || 0) - (all[b].at || 0);
  });
  let total = paths.reduce(function (n, p) { return n + (all[p].d || '').length; }, 0);
  while (total > ICON_MEMORY_MAX && paths.length > 1) {
    const oldest = paths.shift();
    total -= (all[oldest].d || '').length;
    delete all[oldest];
  }
  saveSoon(ICON_MEMORY_KEY);
}

/**
 * Point an <img> at an app icon. Bundled/remote icons are set directly; native
 * icons (file:// paths outside the sandbox) are read as root and inlined as a
 * data URI, because WAM blocks direct file:// <img> loads. On failure the
 * original value is set so the element's own error/fallback handler still runs.
 * opts.keep: also keep the image across restarts (home row tiles).
 */
export function setIconSrc(imgEl, iconUrl, opts) {
  if (!imgEl || !iconUrl) return;
  // The icon as asked for (error handlers compare against this; .src is the
  // resolved, versioned URL).
  imgEl.dataset.iconUrl = iconUrl;
  if (iconUrl.indexOf('file://') !== 0) {
    // Bundled icons get ?v=<version>: WAM caches by URL, so a changed PNG
    // would otherwise keep showing the old image after an update.
    imgEl.src = withAssetVersion(iconUrl, APP_VERSION);
    return;
  }
  if (nativeIconCache.has(iconUrl)) {
    imgEl.src = nativeIconCache.get(iconUrl);
    return;
  }
  const kept = keptIcon(iconUrl);
  if (kept) {
    nativeIconCache.set(iconUrl, kept.d);
    imgEl.src = kept.d;
    // A week-old copy is read again in the background, in case the icon changed.
    if (Date.now() - (kept.at || 0) < ICON_RECHECK_MS) return;
  }
  readFileAsDataUrl(iconUrl).then(function (dataUrl) {
    if (!dataUrl) {
      if (!kept) imgEl.src = iconUrl;
      return;
    }
    nativeIconCache.set(iconUrl, dataUrl);
    if ((opts && opts.keep) || kept) keepIcon(iconUrl, dataUrl);
    if (!kept || kept.d !== dataUrl) imgEl.src = dataUrl;
  }).catch(function () {
    if (!kept) imgEl.src = iconUrl;
  });
}

/**
 * Like setIconSrc, but defers reading native icons until the <img> scrolls into
 * view. This avoids firing a root file read for every row in a long list up
 * front (which stalls low-spec TVs and can leave icons blank), and reloads them
 * reliably as the user scrolls. Falls back to eager loading when
 * IntersectionObserver is unavailable or the icon is already cached/non-native.
 */
export function lazyLoadIcon(imgEl, iconUrl, scrollRoot) {
  if (!imgEl || !iconUrl) return;
  const isNative = iconUrl.indexOf('file://') === 0;
  if (!isNative || nativeIconCache.has(iconUrl) || keptIcon(iconUrl) ||
      typeof IntersectionObserver === 'undefined') {
    setIconSrc(imgEl, iconUrl);
    return;
  }
  const observer = new IntersectionObserver(function (entries) {
    for (let i = 0; i < entries.length; i += 1) {
      if (entries[i].isIntersecting) {
        observer.disconnect();
        setIconSrc(imgEl, iconUrl);
        return;
      }
    }
  }, {root: scrollRoot || null, rootMargin: '160px'});
  observer.observe(imgEl);
}

const APP_INSTALL_ROOTS = [
  '/media/cryptofs/apps/usr/palm/applications',
  '/usr/palm/applications',
  '/media/developer/apps/usr/palm/applications'
];

// Words that don't say which app it is ("com.webos.app.discovery").
const GENERIC_ID_WORDS = /^(app|apps|tv|webos|lge|lg|web)$/i;

/**
 * A name from an app id, for when the TV can't be asked. Reverse-domain ids
 * lose the domain: "org.webosbrew.custom-screensaver" is "Custom
 * Screensaver", where every Homebrew app used to read "Org Webosbrew …".
 */
export function humanizeAppId(id) {
  if (!id) return 'App';

  let parts = String(id).split('.');
  if (parts.length >= 3 && /^(com|org|net|io|tv|de|uk|co|me|fr|jp|kr|app|dev)$/i.test(parts[0])) {
    parts = parts.slice(2);
  }
  const words = parts.join(' ')
    .replace(/[._-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ');
  while (words.length > 1 && GENERIC_ID_WORDS.test(words[0])) words.shift();

  return words
    .map(function (word) {
      if (!word) return '';
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(' ') || 'App';
}

function joinInstallPath(base, relative) {
  return base.replace(/\/$/, '') + '/' + String(relative || '').replace(/^\//, '');
}

function toFileUrl(path) {
  if (!path) return '';
  if (path.indexOf('file://') === 0) return path;
  if (path.charAt(0) === '/') return 'file://' + path;
  return path;
}

function isResolvedIcon(icon) {
  if (!icon) return false;
  return /^https?:\/\//i.test(icon) || icon.indexOf('file://') === 0 || icon.charAt(0) === '/';
}

export function pickBestIcon() {
  const absolute = [];
  const relative = [];

  for (let i = 0; i < arguments.length; i += 1) {
    const icon = arguments[i];
    if (!icon) continue;
    if (isResolvedIcon(icon)) absolute.push(icon);
    else relative.push(icon);
  }

  if (absolute.length) return toFileUrl(absolute[0]);
  if (relative.length) return relative[0];
  return '';
}

export function resolveAppIcon(app) {
  const raw = app.largeIcon || app.icon || app.miniicon || app.mediumIcon || '';
  if (!raw) return '';

  if (/^https?:\/\//i.test(raw)) return raw;
  if (raw.indexOf('file://') === 0) return raw;
  if (raw.indexOf('//') === 0) return 'https:' + raw;
  if (raw.charAt(0) === '/') return 'file://' + raw;

  const base = app.folderPath || app.installPath || app.appPath || '';
  if (base) return toFileUrl(joinInstallPath(base, raw));

  if (app.id) {
    // listLaunchPoints often has only a filename; try common install roots.
    for (let i = 0; i < APP_INSTALL_ROOTS.length; i += 1) {
      return toFileUrl(joinInstallPath(joinInstallPath(APP_INSTALL_ROOTS[i], app.id), raw));
    }
  }

  return raw;
}

// Settings → "Use Launch Home app icons". When off, the TV's own icon wins and
// the bundled one is only a fallback for apps that don't report an icon.
let preferBundledIcons = true;

export function setPreferBundledIcons(on) {
  preferBundledIcons = on !== false;
}

export function prefersBundledIcons() {
  return preferBundledIcons;
}

function applyBuiltinOverrides(record) {
  const builtinIcon = getBuiltinAppIcon(record.id);
  const builtinTitle = getBuiltinAppTitle(record.id);

  return {
    id: record.id,
    launchId: record.launchId || record.id,
    title: builtinTitle || record.title,
    icon: preferBundledIcons
      ? (builtinIcon || record.icon)
      : (record.icon || builtinIcon)
  };
}

export function normalizeAppRecord(raw, fallbackId) {
  const app = (raw && raw.appInfo) || raw || {};
  const id = app.id || (raw && raw.appId) || fallbackId || '';
  const title = app.title || app.displayName || '';
  const icon = resolveAppIcon(app);
  if (title) rememberApp(id, title, icon);
  const known = title && icon ? null : knownApp(id);

  return applyBuiltinOverrides({
    id: id,
    title: title || (known && known.t) || humanizeAppId(id),
    icon: icon || (known && known.i) || ''
  });
}

// Catalogs built from the full installed-apps list (read as root). Only these
// can say an app is not installed; a partial list (no root) can't.
const completeCatalogs = new WeakSet();

/**
 * False only when `catalog` lists every installed app and none of `id`'s ids
 * (aliases included) is among them. Unknown means installed: better a tile that
 * may not open than hiding apps when the list is incomplete.
 */
/** True when `catalog` came from the TV's full app list (read as root). */
export function isCatalogComplete(catalog) {
  return !!catalog && completeCatalogs.has(catalog);
}

export function isAppInstalled(catalog, id) {
  if (!catalog || !completeCatalogs.has(catalog)) return true;
  return getAppIdCandidates(id).some(function (candidate) {
    return !!catalog[candidate];
  });
}

export async function loadAppCatalog() {
  const catalog = {};

  try {
    const res = await listApps();
    (res.apps || []).forEach(function (entry) {
      const normalized = normalizeAppRecord(entry, entry && entry.id);
      if (!normalized.id) return;
      catalog[normalized.id] = normalized;
    });
    if (res.complete) completeCatalogs.add(catalog);
  } catch (err) {
    // listApps is best-effort; pinned apps can still be resolved individually.
  }

  return catalog;
}

export async function resolvePinnedApp(id, catalog) {
  const cached = catalog && catalog[id];
  // Installed ids first (e.g. amazon before amazon.html), so the tile opens
  // the app this TV actually has.
  const candidates = getAppIdCandidates(id).sort(function (a, b) {
    return (catalog && catalog[b] ? 1 : 0) - (catalog && catalog[a] ? 1 : 0);
  });

  for (let i = 0; i < candidates.length; i += 1) {
    const candidate = candidates[i];
    const cachedCandidate = catalog && catalog[candidate];

    try {
      const info = await getAppInfo(candidate);
      const normalized = normalizeAppRecord(info, candidate);
      const merged = applyBuiltinOverrides({
        id: id,
        launchId: candidate,
        title: (cached && cached.title) || (cachedCandidate && cachedCandidate.title) || normalized.title,
        icon: pickBestIcon(
          cached && cached.icon,
          cachedCandidate && cachedCandidate.icon,
          normalized.icon
        )
      });
      if (catalog) catalog[id] = merged;
      return merged;
    } catch (err) {
      if (cachedCandidate) {
        const merged = applyBuiltinOverrides({
          id: id,
          launchId: candidate,
          title: cachedCandidate.title,
          icon: cachedCandidate.icon || ''
        });
        if (catalog) catalog[id] = merged;
        return merged;
      }
    }
  }

  if (cached) return applyBuiltinOverrides({id: id, title: cached.title, icon: cached.icon || ''});
  // The TV can't be asked right now: what it said last time.
  const known = knownApp(id);
  return applyBuiltinOverrides({
    id: id,
    title: (known && known.t) || humanizeAppId(id),
    icon: (known && known.i) || ''
  });
}