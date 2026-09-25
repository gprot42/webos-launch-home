import {launchApp, launchAppViaRoot, listApps} from './luna.js';
import {
  isAppInstalled,
  isCatalogComplete,
  loadAppCatalog,
  normalizeAppRecord,
  prefersBundledIcons,
  resolvePinnedApp,
  setIconSrc
} from './app-catalog.js';
import {getAppIdCandidates, getBuiltinAppIcon} from './app-icons.js';

const BUNDLED_SETTINGS_ICON = 'assets/app-icons/tv-settings.png';

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

  // Each tile's app: a late app list updates tiles in place (see
  // retryNames), which leaves the remote's focus where it is.
  const tileApps = new WeakMap();

  function fallbackBadge(title) {
    const fallback = document.createElement('span');
    fallback.className = 'app-fallback';
    fallback.textContent = String(title || '').slice(0, 2).toUpperCase();
    return fallback;
  }

  function tileIcon(app) {
    if (!app.icon) return fallbackBadge(app.title);
    const img = document.createElement('img');
    img.className = 'app-icon';
    img.alt = '';
    img.addEventListener('error', function () {
      const fallbackIcon = getBuiltinAppIcon(app.id);
      if (fallbackIcon && img.dataset.iconUrl !== fallbackIcon) {
        setIconSrc(img, fallbackIcon);
        return;
      }
      if (img.parentNode) img.parentNode.replaceChild(fallbackBadge(app.title), img);
    });
    setIconSrc(img, app.icon, {keep: true});
    return img;
  }

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

    button.appendChild(tileIcon(app));
    button.appendChild(label);

    tileApps.set(button, app);
    button.addEventListener('click', function () {
      openApp(tileApps.get(button) || app);
    });

    return button;
  }

  function updateTile(button, app) {
    tileApps.set(button, app);
    button.setAttribute('aria-label', app.title);
    const label = button.querySelector('.app-label');
    if (label) label.textContent = app.title;
    const icon = tileIcon(app);
    const old = button.querySelector('.app-icon, .app-fallback');
    if (old) button.replaceChild(icon, old);
    else button.insertBefore(icon, label);
  }

  function makeSettingsTile(index, iconUrl) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'app-tile settings-tile focusable';
    button.dataset.focusIndex = String(index);
    button.dataset.action = 'tv-settings';
    button.setAttribute('aria-label', 'TV Settings');

    const img = document.createElement('img');
    img.className = 'app-icon';
    img.alt = '';
    img.addEventListener('error', function () {
      if (img.dataset.iconUrl !== BUNDLED_SETTINGS_ICON) {
        setIconSrc(img, BUNDLED_SETTINGS_ICON);
        return;
      }
      img.remove();
      const fallback = document.createElement('span');
      fallback.className = 'app-fallback';
      fallback.textContent = '\u2699';
      button.insertBefore(fallback, label);
    });
    setIconSrc(img, iconUrl || BUNDLED_SETTINGS_ICON);
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

  // What the current dock was built from; see refresh({reuse}).
  let builtSignature = '';
  let lastSignature = '';

  // Built without the TV's full app list (root not answering yet, Luna late
  // after power-on): ask again a few times and fill names and icons in place.
  const NAME_RETRY_MS = [20000, 60000, 180000];
  let nameRetry = null;
  let nameRetries = 0;

  function scheduleNameRetry(listed) {
    if (nameRetry) clearTimeout(nameRetry);
    nameRetry = null;
    if (listed) {
      nameRetries = 0;
      return;
    }
    if (nameRetries >= NAME_RETRY_MS.length) return;
    nameRetry = setTimeout(retryNames, NAME_RETRY_MS[nameRetries]);
    nameRetries += 1;
  }

  async function retryNames() {
    nameRetry = null;
    const fresh = await loadAppCatalog();
    const listed = isCatalogComplete(fresh);
    const listedAny = Object.keys(fresh).length > 0;
    const tiles = container.querySelectorAll('.app-tile[data-app-id]');
    for (let i = 0; i < tiles.length; i += 1) {
      const current = tileApps.get(tiles[i]);
      if (!current || current.custom) continue;
      const info = await resolvePinnedApp(tiles[i].dataset.appId, fresh);
      if (info.title !== current.title || info.icon !== current.icon) updateTile(tiles[i], info);
    }
    if (listedAny) {
      catalog = fresh;
      builtSignature = lastSignature;
    }
    scheduleNameRetry(listed);
  }

  /**
   * @param {{reuse?: boolean}} [opts] reuse: keep the current dock when its
   *   inputs are unchanged. Returning from an app used to redo every Luna
   *   lookup (app list + one getAppInfo per pinned app) and redraw the dock.
   */
  async function refresh(opts) {
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
    const signature = JSON.stringify([pinned, customApps, iconSize, prefersBundledIcons()]);
    if (opts && opts.reuse && signature === builtSignature && container.children.length) {
      return;
    }
    const tiles = [];

    catalog = await loadAppCatalog();

    for (let i = 0; i < pinned.length; i += 1) {
      const custom = customById[pinned[i]];
      if (custom) {
        tiles.push(makeTile({
          id: custom.launchId || custom.id,
          launchId: custom.launchId || custom.id,
          title: custom.title || custom.launchId || custom.id,
          icon: custom.icon || '',
          custom: true
        }, i));
        continue;
      }
      // Pinned but not on this TV (a default pin, or an app since removed):
      // no tile that can't open. Settings still lists it, marked.
      if (!isAppInstalled(catalog, pinned[i])) continue;
      const info = await resolvePinnedApp(pinned[i], catalog);
      tiles.push(makeTile(info, i));
    }

    let settingsIcon = '';
    if (!prefersBundledIcons()) {
      const settingsApp = catalog['com.webos.app.settings'] || catalog['com.palm.app.settings'];
      settingsIcon = (settingsApp && settingsApp.icon) || '';
    }
    tiles.push(makeSettingsTile(pinned.length, settingsIcon));

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
    // Only reuse a dock built from a real app list: at power-on the Luna
    // services can be late, and that first dock uses fallback titles/icons.
    builtSignature = Object.keys(catalog).length ? signature : '';
    lastSignature = signature;
    scheduleNameRetry(isCatalogComplete(catalog));
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