import {getAppInfo, listApps, readFileAsDataUrl} from './luna.js';
import {getAppIdCandidates, getBuiltinAppIcon, getBuiltinAppTitle, isCompanionVoiceApp} from './app-icons.js';

// Cache of resolved native icons (file:// path -> data URI or '' when it failed)
// so repeated renders don't re-read the same file over the root bus.
const nativeIconCache = new Map();

/**
 * Point an <img> at an app icon. Bundled/remote icons are set directly; native
 * icons (file:// paths outside the sandbox) are read as root and inlined as a
 * data URI, because WAM blocks direct file:// <img> loads. On failure the
 * original value is set so the element's own error/fallback handler still runs.
 */
export function setIconSrc(imgEl, iconUrl) {
  if (!imgEl || !iconUrl) return;
  if (iconUrl.indexOf('file://') !== 0) {
    imgEl.src = iconUrl;
    return;
  }
  if (nativeIconCache.has(iconUrl)) {
    imgEl.src = nativeIconCache.get(iconUrl) || iconUrl;
    return;
  }
  readFileAsDataUrl(iconUrl).then(function (dataUrl) {
    nativeIconCache.set(iconUrl, dataUrl || '');
    imgEl.src = dataUrl || iconUrl;
  }).catch(function () {
    imgEl.src = iconUrl;
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
  if (!isNative || nativeIconCache.has(iconUrl) ||
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

export function humanizeAppId(id) {
  if (!id) return 'App';

  let label = id;
  if (label.indexOf('com.') === 0) {
    const parts = label.split('.');
    if (parts.length >= 3) {
      label = parts.slice(2).join(' ');
    }
  }

  return label
    .replace(/[._-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .map(function (word) {
      if (!word) return '';
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(' ');
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

function applyBuiltinOverrides(record) {
  const builtinIcon = getBuiltinAppIcon(record.id);
  const builtinTitle = getBuiltinAppTitle(record.id);

  return {
    id: record.id,
    launchId: record.launchId || record.id,
    title: builtinTitle || record.title,
    icon: builtinIcon || record.icon
  };
}

export function normalizeAppRecord(raw, fallbackId) {
  const app = (raw && raw.appInfo) || raw || {};
  const id = app.id || (raw && raw.appId) || fallbackId || '';

  return applyBuiltinOverrides({
    id: id,
    title: app.title || app.displayName || humanizeAppId(id),
    icon: resolveAppIcon(app)
  });
}

export async function loadAppCatalog() {
  const catalog = {};

  try {
    const res = await listApps();
    (res.apps || []).forEach(function (entry) {
      const normalized = normalizeAppRecord(entry, entry && entry.id);
      if (!normalized.id || isCompanionVoiceApp(normalized.id)) return;
      catalog[normalized.id] = normalized;
    });
  } catch (err) {
    // listApps is best-effort; pinned apps can still be resolved individually.
  }

  return catalog;
}

export async function resolvePinnedApp(id, catalog) {
  const cached = catalog && catalog[id];
  const candidates = getAppIdCandidates(id);

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
  return applyBuiltinOverrides({id: id, title: humanizeAppId(id), icon: ''});
}