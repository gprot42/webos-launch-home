import {launchApp, launchAppViaRoot, listApps} from './luna.js';
import {loadAppCatalog, normalizeAppRecord, resolvePinnedApp, setIconSrc} from './app-catalog.js';
import {getAppIdCandidates, getBuiltinAppIcon, isCompanionVoiceApp} from './app-icons.js';

/**
 * Launch an app by trying every candidate id, sandboxed then root.
 * Prime Video is `amazon.html` on older TVs and native `amazon` on current
 * OLEDs — the first id often 404s or hangs, so we must fall through.
 */
export async function launchAppCandidates(ids) {
  const unique = [];
  (ids || []).forEach(function (id) {
    if (id && unique.indexOf(id) < 0) unique.push(id);
  });

  for (let i = 0; i < unique.length; i += 1) {
    try {
      await launchApp(unique[i]);
      return unique[i];
    } catch (err) {
      // Missing id or sandboxed launch denied (native Prime Video).
    }
    try {
      await launchAppViaRoot(unique[i]);
      return unique[i];
    } catch (err2) {
      // Try the next candidate.
    }
  }
  return '';
}

const APP_ID = 'org.webosbrew.lounge.launcher';

export function createAppGrid(container, getConfig, options) {
  let catalog = {};

  function makeTile(app, index) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'app-tile focusable';
    button.dataset.focusIndex = String(index);
    button.dataset.appId = app.id;
    button.setAttribute('aria-label', app.title);

    const label = document.createElement('span');
    label.className = 'app-label';
    label.textContent = app.title;

    if (app.icon) {
      const img = document.createElement('img');
      img.className = 'app-icon';
      img.alt = '';
      img.addEventListener('error', function () {
        const fallbackIcon = getBuiltinAppIcon(app.id);
        if (fallbackIcon && img.src !== fallbackIcon) {
          img.src = fallbackIcon;
          return;
        }
        img.remove();
        const fallback = document.createElement('span');
        fallback.className = 'app-fallback';
        fallback.textContent = app.title.slice(0, 2).toUpperCase();
        button.insertBefore(fallback, label);
      });
      setIconSrc(img, app.icon);
      button.appendChild(img);
    } else {
      const fallback = document.createElement('span');
      fallback.className = 'app-fallback';
      fallback.textContent = app.title.slice(0, 2).toUpperCase();
      button.appendChild(fallback);
    }

    button.appendChild(label);

    button.addEventListener('click', function () {
      openApp(app);
    });

    return button;
  }

  function makeSettingsTile(index) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'app-tile settings-tile focusable';
    button.dataset.focusIndex = String(index);
    button.dataset.action = 'tv-settings';
    button.setAttribute('aria-label', 'TV Settings');

    const img = document.createElement('img');
    img.className = 'app-icon';
    img.src = 'assets/app-icons/tv-settings.png';
    img.alt = '';
    img.addEventListener('error', function () {
      img.remove();
      const fallback = document.createElement('span');
      fallback.className = 'app-fallback';
      fallback.textContent = '\u2699';
      button.insertBefore(fallback, label);
    });
    button.appendChild(img);

    const label = document.createElement('span');
    label.className = 'app-label';
    label.textContent = 'TV Settings';
    button.appendChild(label);

    button.addEventListener('click', function () {
      if (options.onOpenTvSettings) options.onOpenTvSettings();
    });

    return button;
  }

  async function openApp(app) {
    if (options.onBeforeLaunch) options.onBeforeLaunch();

    const ids = [];
    function addId(id) {
      if (id && ids.indexOf(id) < 0) ids.push(id);
    }
    if (app && Array.isArray(app.ids)) {
      app.ids.forEach(addId);
    }
    addId(app && app.launchId);
    getAppIdCandidates((app && app.id) || '').forEach(addId);
    getAppIdCandidates((app && app.launchId) || '').forEach(addId);

    const launched = await launchAppCandidates(ids);
    if (launched) return launched;

    const label = app && app.title ? app.title : (app && app.id) || 'app';
    if (options.onToast) options.onToast('Could not launch ' + label);
    return '';
  }

  async function refresh() {
    const config = getConfig();
    const pinned = (config.launcher && config.launcher.pinnedApps) || [];
    const customApps = (config.launcher && config.launcher.customApps) || [];
    const customById = {};
    customApps.forEach(function (entry) {
      if (entry && entry.id) customById[entry.id] = entry;
    });
    const scaleBySize = {small: 0.78, medium: 1, large: 1.28};
    const iconSize = (config.launcher && config.launcher.iconSize) || 'medium';
    container.style.setProperty('--tile-scale', String(scaleBySize[iconSize] || 1));
    const tiles = [];

    catalog = await loadAppCatalog();

    for (let i = 0; i < pinned.length; i += 1) {
      if (isCompanionVoiceApp(pinned[i])) continue;
      const custom = customById[pinned[i]];
      if (custom) {
        if (isCompanionVoiceApp(custom.launchId || custom.id)) continue;
        tiles.push(makeTile({
          id: custom.launchId || custom.id,
          launchId: custom.launchId || custom.id,
          title: custom.title || custom.launchId || custom.id,
          icon: custom.icon || ''
        }, i));
        continue;
      }
      const info = await resolvePinnedApp(pinned[i], catalog);
      tiles.push(makeTile(info, i));
    }

    tiles.push(makeSettingsTile(pinned.length));

    // Swap in the freshly built tiles atomically. Clearing the container up
    // front instead would leave the dock empty (and unselectable) for the whole
    // async catalog fetch above -- and if that fetch stalls after a failed app
    // launch, the dock would stay empty and focus would never be restored.
    const fragment = document.createDocumentFragment();
    for (const tile of tiles) {
      fragment.appendChild(tile);
    }
    container.innerHTML = '';
    container.appendChild(fragment);
  }

  return {
    refresh: refresh,
    isLoungeApp: function (id) {
      return id === APP_ID;
    },
    launchApp: openApp
  };
}

export async function listInstalledApps(options) {
  const includeHidden = !!(options && options.includeHidden);
  try {
    const res = await listApps();
    return (res.apps || [])
      .filter(function (app) {
        const id = (app && (app.id || app.appId)) || '';
        if (isCompanionVoiceApp(id)) return false;
        if (includeHidden) return true;
        const record = (app && app.appInfo) || app || {};
        return record.visible !== false;
      })
      .map(function (app) {
        return normalizeAppRecord(app, app && app.id);
      });
  } catch (err) {
    return [];
  }
}