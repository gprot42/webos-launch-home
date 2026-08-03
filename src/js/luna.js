function hasWebOS() {
  return typeof window !== 'undefined' && window.webOS && window.webOS.service;
}

/**
 * Reject if the wrapped promise doesn't settle within `ms`. Some Luna calls
 * (root exec, subscriptions) can hang indefinitely after a failed app launch;
 * without a timeout that would stall the whole UI refresh.
 */
function withTimeout(promise, ms) {
  return new Promise(function (resolve, reject) {
    const timer = setTimeout(function () {
      reject(new Error('Luna call timed out'));
    }, ms);
    promise.then(function (value) {
      clearTimeout(timer);
      resolve(value);
    }, function (err) {
      clearTimeout(timer);
      reject(err);
    });
  });
}

export function lunaRequest(uri, options) {
  const method = options.method;
  const parameters = options.parameters || {};

  return new Promise(function (resolve, reject) {
    if (!hasWebOS()) {
      reject(new Error('webOS Luna bus unavailable'));
      return;
    }

    window.webOS.service.request(uri, {
      method: method,
      parameters: parameters,
      onSuccess: function (res) {
        if (res && res.returnValue === false) {
          reject(new Error(res.errorText || 'Luna call failed'));
          return;
        }
        resolve(res || {});
      },
      onFailure: function (err) {
        reject(err || new Error('Luna call failed'));
      }
    });
  });
}

function tryLunaRequest(uri, options) {
  return lunaRequest(uri, options).catch(function () {
    return null;
  });
}

/**
 * Some applicationManager list methods only return data through a subscription
 * on retail webOS. Open a subscribe request, resolve on the first payload, then
 * cancel so we behave like a one-shot call.
 */
function lunaSubscribeOnce(uri, options) {
  const method = options.method;
  const parameters = Object.assign({subscribe: true}, options.parameters || {});

  return new Promise(function (resolve, reject) {
    if (!hasWebOS()) {
      reject(new Error('webOS Luna bus unavailable'));
      return;
    }

    let settled = false;
    let handle = null;

    function finish(fn, value) {
      if (settled) return;
      settled = true;
      if (handle && typeof handle.cancel === 'function') {
        try { handle.cancel(); } catch (err) { /* ignore */ }
      }
      fn(value);
    }

    handle = window.webOS.service.request(uri, {
      method: method,
      parameters: parameters,
      onSuccess: function (res) {
        if (res && res.returnValue === false) {
          finish(reject, new Error(res.errorText || 'Luna call failed'));
          return;
        }
        finish(resolve, res || {});
      },
      onFailure: function (err) {
        finish(reject, err || new Error('Luna call failed'));
      }
    });
  });
}

export function launchApp(id) {
  return lunaRequest('luna://com.webos.applicationManager', {
    method: 'launch',
    parameters: {id: id, params: {}}
  });
}

export function getAppInfo(id) {
  return lunaRequest('luna://com.webos.applicationManager', {
    method: 'getAppInfo',
    parameters: {id: id}
  });
}

/**
 * Run a shell command through the Homebrew Channel root service. On a rooted TV
 * with the service elevated this executes as root; otherwise it fails or runs
 * unprivileged. Resolves with the raw exec response.
 */
export function execRoot(command) {
  return lunaRequest('luna://org.webosbrew.hbchannel.service', {
    method: 'exec',
    parameters: {command: command}
  });
}

/**
 * Report the effective user privileged calls run as (e.g. "root"). Uses the
 * Homebrew Channel root exec service. Resolves to the username string, or null
 * when unavailable (not rooted, no Homebrew Channel, or desktop preview).
 */
export function whoAmI() {
  return execRoot('id -un').then(function (res) {
    let out = (res && res.stdoutString ? String(res.stdoutString) : '').trim();
    if (!out && res && res.stdoutBytes) {
      try { out = String(atob(res.stdoutBytes)).trim(); } catch (err) { /* ignore */ }
    }
    return out || null;
  }).catch(function () {
    return null;
  });
}

function guessImageMime(path) {
  const p = String(path).toLowerCase();
  if (p.endsWith('.jpg') || p.endsWith('.jpeg')) return 'image/jpeg';
  if (p.endsWith('.gif')) return 'image/gif';
  if (p.endsWith('.webp')) return 'image/webp';
  if (p.endsWith('.svg')) return 'image/svg+xml';
  if (p.endsWith('.bmp')) return 'image/bmp';
  return 'image/png';
}

/**
 * Read an image file from the TV filesystem as a base64 data URI using the
 * Homebrew Channel root service. Native app icons live outside the launcher's
 * sandbox, so WAM blocks `file://` <img> loads; reading the bytes as root and
 * inlining them is the only reliable way to display them. Resolves to a
 * `data:` URL, or null when the file can't be read.
 */
export function readFileAsDataUrl(path) {
  if (!path) return Promise.resolve(null);
  const clean = String(path).replace(/^file:\/\//, '');
  const command = 'cat -- "' + clean.replace(/(["$`\\])/g, '\\$1') + '"';
  return execRoot(command).then(function (res) {
    const b64 = res && res.stdoutBytes ? String(res.stdoutBytes).replace(/\s+/g, '') : '';
    if (!b64) return null;
    return 'data:' + guessImageMime(clean) + ';base64,' + b64;
  }).catch(function () {
    return null;
  });
}

function normalizeListedApps(res) {
  if (!res) return {apps: []};

  if (Array.isArray(res.apps) && res.apps.length) {
    return {apps: res.apps};
  }

  if (Array.isArray(res.launchPoints)) {
    return {
      apps: res.launchPoints.map(function (point) {
        return {
          id: point.id,
          appInfo: point
        };
      })
    };
  }

  return {apps: []};
}

/**
 * Build a root `luna-send` command line for this TV's 2011-era binary.
 *
 * That build requires `-i` (interactive) or it prints nothing; `-n 1` exits
 * after one reply; `-w` is a hard timeout. Newer TVs accept the same flags.
 */
export function lunaSendRootCommand(uri, payloadJson, waitMs) {
  const wait = waitMs || 4000;
  const payload = payloadJson == null ? '{}' : String(payloadJson);
  // Single-quoted payload; escape any embedded single quotes for the shell.
  const safePayload = payload.replace(/'/g, "'\\''");
  return "luna-send -i -n 1 -w " + wait + " -f '" + uri + "' '" + safePayload + "'";
}

function readExecStdout(res) {
  let out = res && res.stdoutString ? String(res.stdoutString) : '';
  if (!out && res && res.stdoutBytes) {
    try { out = String(atob(res.stdoutBytes)); } catch (err) { /* ignore */ }
  }
  return out.trim();
}

/**
 * List every installed app via the Homebrew Channel root service.
 *
 * A sandboxed web app cannot call the privileged
 * `com.webos.applicationManager/listApps` method directly, so on retail webOS
 * the in-app subscription only returns a handful (or zero) apps. Running
 * `luna-send` through the elevated Homebrew Channel exec service performs the
 * call as root and returns the full, unfiltered list. Rejects when the service
 * is unavailable or the output can't be parsed, so callers can fall back.
 */
function listAppsViaRoot() {
  const command = lunaSendRootCommand(
    'luna://com.webos.applicationManager/listApps',
    '{}',
    5000
  );
  return withTimeout(execRoot(command), 6000).then(function (res) {
    const out = readExecStdout(res);
    if (!out) throw new Error('empty listApps output');

    const data = JSON.parse(out);
    if (data && data.returnValue === false) {
      throw new Error(data.errorText || 'listApps failed');
    }
    const normalized = normalizeListedApps(data);
    if (!normalized.apps.length) throw new Error('no apps returned');
    return normalized;
  });
}

export function listApps() {
  return listAppsViaRoot().catch(function () {
    return withTimeout(lunaSubscribeOnce('luna://com.webos.applicationManager', {
      method: 'listLaunchPoints',
      parameters: {}
    }), 5000).then(normalizeListedApps).then(function (result) {
      if (result.apps && result.apps.length) return result;
      return lunaRequest('luna://com.webos.applicationManager', {
        method: 'listApps',
        parameters: {}
      }).then(normalizeListedApps);
    }).catch(function () {
      return lunaRequest('luna://com.webos.applicationManager', {
        method: 'listApps',
        parameters: {}
      }).then(normalizeListedApps).catch(function () {
        return {apps: []};
      });
    });
  });
}

function pickForegroundId(res) {
  if (!res) return '';
  if (res.appId) return res.appId;
  if (Array.isArray(res.foregroundAppInfo) && res.foregroundAppInfo.length) {
    return res.foregroundAppInfo[0].appId || '';
  }
  return '';
}

function getForegroundViaRoot() {
  // Same privilege wall as listApps: a sandboxed web app is not allowed to
  // query applicationManager for the foreground app, so the direct/subscription
  // call is denied and never resolves. Running luna-send as root returns it.
  // Must use -i on this TV's luna-send or stdout is empty (see lunaSendRootCommand).
  const command = lunaSendRootCommand(
    'luna://com.webos.applicationManager/getForegroundAppInfo',
    '{"subscribe":true}',
    4000
  );
  return withTimeout(execRoot(command), 5000).then(function (res) {
    const out = readExecStdout(res);
    if (!out) throw new Error('empty getForegroundAppInfo output');
    const data = JSON.parse(out);
    if (data && data.returnValue === false) {
      throw new Error(data.errorText || 'getForegroundAppInfo failed');
    }
    return {appId: pickForegroundId(data)};
  });
}

/**
 * Launch an app via root luna-send. More reliable from backgrounded contexts
 * where the sandboxed applicationManager/launch call may be denied or ignored.
 */
export function launchAppViaRoot(id) {
  const command = lunaSendRootCommand(
    'luna://com.webos.applicationManager/launch',
    JSON.stringify({id: id}),
    4000
  );
  return withTimeout(execRoot(command), 5000).then(function (res) {
    const out = readExecStdout(res);
    if (!out) return {returnValue: true};
    const data = JSON.parse(out);
    if (data && data.returnValue === false) {
      throw new Error(data.errorText || 'launch failed');
    }
    return data;
  });
}

const LOUNGE_APP_ID = 'org.webosbrew.lounge.launcher';
const HOME_WATCHER_APP_DIR =
  '/media/developer/apps/usr/palm/applications/' + LOUNGE_APP_ID;
const HOME_WATCHER_APP_PATH = HOME_WATCHER_APP_DIR + '/home-watcher.sh';
const HOME_WATCHER_ENABLE = HOME_WATCHER_APP_DIR + '/enable-home-watcher.sh';
const HOME_WATCHER_DISABLE = HOME_WATCHER_APP_DIR + '/disable-home-watcher.sh';
const HOME_WATCHER_PIDFILE = '/tmp/launch-home-watcher.pid';
const BOOT_LAUNCH_SCRIPT = HOME_WATCHER_APP_DIR + '/boot-launch.sh';
const BOOT_LAUNCH_ENABLE = HOME_WATCHER_APP_DIR + '/enable-boot-launch.sh';
const BOOT_LAUNCH_DISABLE = HOME_WATCHER_APP_DIR + '/disable-boot-launch.sh';

/**
 * Install + start the root Home-button watcher so Home opens Launch Home even when
 * this web app is suspended.
 *
 * Runs a packaged enable script (not an inline command string). The previous
 * inline path joined steps with ";" after a background `&`, which busybox ash
 * rejects as `unexpected ";"` — enable always failed with "Home watcher failed".
 */
export function enableHomeWatcher() {
  const cmd =
    'chmod 755 "' + HOME_WATCHER_ENABLE + '" "' + HOME_WATCHER_APP_PATH +
    '" 2>/dev/null; sh "' + HOME_WATCHER_ENABLE + '"';
  return withTimeout(execRoot(cmd), 20000).then(function (res) {
    const out = readExecStdout(res);
    if (out.indexOf('missing_watcher') >= 0) {
      throw new Error('home-watcher.sh not installed on TV');
    }
    if (out.indexOf('enabled') < 0) {
      throw new Error(out || 'home watcher failed to start');
    }
    return true;
  });
}

/**
 * Stop and remove the root Home-button watcher.
 */
export function disableHomeWatcher() {
  const cmd =
    'chmod 755 "' + HOME_WATCHER_DISABLE + '" 2>/dev/null; sh "' +
    HOME_WATCHER_DISABLE + '"';
  return withTimeout(execRoot(cmd), 8000).then(function () {
    return true;
  }).catch(function () {
    return false;
  });
}

/** True when the detached root watcher process is alive. */
export function isHomeWatcherRunning() {
  const cmd =
    'if [ -f "' + HOME_WATCHER_PIDFILE + '" ] && kill -0 $(cat "' +
    HOME_WATCHER_PIDFILE + '") 2>/dev/null; then echo running; else echo stopped; fi';
  return withTimeout(execRoot(cmd), 5000).then(function (res) {
    return readExecStdout(res).indexOf('running') >= 0;
  }).catch(function () {
    return false;
  });
}

/**
 * Install Homebrew init.d hook so Launch Home launches after TV power-on.
 * Requires rooted TV + Homebrew Channel startup (webosbrew init.d).
 */
export function enableBootLaunch() {
  const cmd =
    'chmod 755 "' + BOOT_LAUNCH_ENABLE + '" "' + BOOT_LAUNCH_SCRIPT +
    '" 2>/dev/null; sh "' + BOOT_LAUNCH_ENABLE + '"';
  return withTimeout(execRoot(cmd), 12000).then(function (res) {
    const out = readExecStdout(res);
    if (out.indexOf('missing_boot_script') >= 0) {
      throw new Error('boot-launch.sh not installed on TV');
    }
    if (out.indexOf('enabled') < 0) {
      throw new Error(out || 'boot launch failed to install');
    }
    return true;
  });
}

/**
 * Remove the boot-on-start init.d hook.
 */
export function disableBootLaunch() {
  const cmd =
    'chmod 755 "' + BOOT_LAUNCH_DISABLE + '" 2>/dev/null; sh "' +
    BOOT_LAUNCH_DISABLE + '"';
  return withTimeout(execRoot(cmd), 8000).then(function () {
    return true;
  }).catch(function () {
    return false;
  });
}

export function getForegroundApp() {
  // Prefer the root path (the only one that works on retail webOS for a
  // sandboxed app); fall back to in-app subscription / one-shot for previews.
  return getForegroundViaRoot().catch(function () {
    return withTimeout(lunaSubscribeOnce('luna://com.webos.applicationManager', {
      method: 'getForegroundAppInfo',
      parameters: {}
    }), 4000).then(function (res) {
      return {appId: pickForegroundId(res)};
    });
  }).catch(function () {
    return withTimeout(lunaRequest('luna://com.webos.applicationManager', {
      method: 'getForegroundApp',
      parameters: {}
    }), 4000).then(function (res) {
      return {appId: pickForegroundId(res)};
    });
  });
}

/**
 * Bring an already-running app to the foreground (or launch it). Used to
 * reclaim input focus when another app has grabbed it without covering us.
 */
export function closeApp(id) {
  return lunaRequest('luna://com.webos.applicationManager', {
    method: 'closeByAppId',
    parameters: {id: id}
  });
}

export function getAllInputStatus() {
  return lunaRequest('luna://com.webos.service.eim', {
    method: 'getAllInputStatus',
    parameters: {subscribe: false}
  });
}

export function getInputStatus(inputId) {
  return lunaRequest('luna://com.webos.service.eim', {
    method: 'getInputStatus',
    parameters: {id: inputId}
  });
}

function switchInputViaApiAdapter(inputId) {
  return lunaRequest('luna://com.webos.service.apiadapter/tv', {
    method: 'switchInput',
    parameters: {inputId: inputId}
  });
}

function switchInputViaEim(inputId) {
  return getInputStatus(inputId).then(function (status) {
    if (status && status.appId) {
      return launchApp(status.appId);
    }
    throw new Error('Input has no launchable app');
  });
}

function switchInputViaExtinputs(inputId) {
  const hdmiMatch = inputId.match(/^HDMI_(\d+)$/i);
  if (!hdmiMatch) {
    return Promise.reject(new Error('extinputs only supports HDMI inputs'));
  }

  const mediaId = hdmiMatch[1];

  return lunaRequest('luna://com.webos.service.utp.extinputs', {
    method: 'open',
    parameters: {
      inputSourceType: 'HDMI',
      options: {mediaId: mediaId}
    }
  }).catch(function () {
    return lunaRequest('luna://com.webos.service.utp.extinputs', {
      method: 'open',
      parameters: {
        inputSourceType: 'HDMI',
        inputSource: 'HDMI_' + mediaId
      }
    });
  });
}

export function switchInput(inputId, device) {
  if (device && device.appId) {
    return launchApp(device.appId).catch(function () {
      return switchInputViaApiAdapter(inputId)
        .catch(function () { return switchInputViaEim(inputId); })
        .catch(function () { return switchInputViaExtinputs(inputId); });
    });
  }

  return switchInputViaApiAdapter(inputId)
    .catch(function () { return switchInputViaEim(inputId); })
    .catch(function () { return switchInputViaExtinputs(inputId); });
}

export function getStorageDevices() {
  return lunaRequest('luna://com.webos.service.pdm', {
    method: 'getAttachedStorageDeviceList',
    parameters: {subscribe: false}
  }).catch(function () {
    return tryLunaRequest('luna://com.webos.service.pdm', {
      method: 'getDeviceList',
      parameters: {subscribe: false}
    });
  });
}

/**
 * Set the LG webOS screensaver idle timeout (minutes), or disable it.
 *
 * On rooted TVs we write /var/luna/preferences/option *and* keep the sibling
 * .md5 in sync (SettingsService ignores or reverts prefs when the checksum
 * is stale). We also align screenOffTime — energy-saving "turn screen off"
 * is a separate timer that often blanks the panel long before the gallery
 * screensaver (e.g. screenOffTime=5 while screenSaverTimer=15).
 *
 * @param {number|string} minutes  minutes (1–120), or 0 / 'off' to disable
 */
export function setScreensaverTimeout(minutes) {
  let value = '15';
  if (minutes === 0 || minutes === '0' || minutes === 'off' || minutes === false) {
    value = 'off';
  } else {
    let n = Math.round(Number(minutes));
    if (isNaN(n) || n < 1) n = 15;
    if (n > 120) n = 120;
    value = String(n);
  }

  // Single python block: option + md5 + general + best-effort Luna notify.
  const py =
    'python3 -c \'' +
    'import json,hashlib,subprocess,os\n' +
    'def wp(path,fn):\n' +
    ' d=json.load(open(path))\n' +
    ' fn(d)\n' +
    ' o=json.dumps(d,separators=(",",":"),ensure_ascii=False)\n' +
    ' open(path,"w").write(o)\n' +
    ' h=hashlib.md5(o.encode()).hexdigest()\n' +
    ' open(path+".md5","w").write("%s  %s\\n"%(h,path))\n' +
    ' return d\n' +
    'v="' + value + '"\n' +
    'def uo(d):\n' +
    ' d["screenSaverTimer"]=v\n' +
    ' d["checkChangedScreenSaverTimerByUser"]="user"\n' +
    ' # Energy-saving blank must not fire sooner than the screensaver.\n' +
    ' d["screenOffTime"]=v\n' +
    ' d["screenOff"]="off"\n' +
    ' d["screenSaverArtEnabled"]="off"\n' +
    ' d["artisticDisplayTimer"]="off"\n' +
    'd=wp("/var/luna/preferences/option",uo)\n' +
    'def ug(d):\n' +
    ' d["screenSaverEnabled"]="off" if v=="off" else "on"\n' +
    'wp("/var/luna/preferences/general",ug)\n' +
    'payload=json.dumps({"category":"option","settings":{' +
    '"screenSaverTimer":v,"screenOffTime":v,' +
    '"checkChangedScreenSaverTimerByUser":"user"}})\n' +
    'try:\n' +
    ' subprocess.call(["luna-send","-n","1","luna://com.webos.settingsservice/setSystemSettings",payload],timeout=4)\n' +
    'except Exception:\n' +
    ' pass\n' +
    'print("ok",d.get("screenSaverTimer"),d.get("screenOffTime"))\n' +
    '\'';

  return withTimeout(execRoot(py), 10000).then(function (res) {
    const out = readExecStdout(res);
    if (out.indexOf('ok') < 0) {
      return {returnValue: false, value: value, out: out};
    }
    return {returnValue: true, value: value, out: out};
  }).catch(function () {
    return {returnValue: false, value: value};
  });
}

/**
 * Set the TV system volume (0–100). Best-effort across webOS audio services.
 * Used when entering Launch Home vs launching an external app.
 */
export function setSystemVolume(level) {
  let n = Math.round(Number(level));
  if (isNaN(n)) n = 0;
  if (n < 0) n = 0;
  if (n > 100) n = 100;

  if (!hasWebOS()) {
    return Promise.resolve({returnValue: false, skipped: true});
  }

  const params = {volume: n};
  const uris = [
    'luna://com.webos.audio',
    'luna://com.webos.service.audio',
    'luna://com.webos.service.apiadapter/audio'
  ];

  function tryAt(i) {
    if (i >= uris.length) {
      return Promise.reject(new Error('setVolume unavailable'));
    }
    return lunaRequest(uris[i], {method: 'setVolume', parameters: params}).catch(function () {
      return tryAt(i + 1);
    });
  }

  return tryAt(0).catch(function () {
    // Last resort: shell via root (Homebrew Channel) for stubborn firmware.
    return execRoot(
      'luna-send -n 1 -f luna://com.webos.audio/setVolume \'{"volume":' + n + '}\' 2>/dev/null; ' +
      'luna-send -n 1 -f luna://com.webos.service.audio/setVolume \'{"volume":' + n + '}\' 2>/dev/null; true'
    ).then(function () {
      return {returnValue: true, via: 'root'};
    }).catch(function () {
      return {returnValue: false};
    });
  });
}

/**
 * Subscribe to TV system volume changes. On webOS the physical remote volume
 * keys are handled by the platform, so we listen for volume updates and mirror
 * them on the on-screen bar. Returns an unsubscribe function.
 */
export function subscribeVolume(onChange) {
  if (!hasWebOS() || typeof onChange !== 'function') {
    return function () {};
  }

  function extractVolume(res) {
    if (!res) return null;
    if (res.volume && typeof res.volume.volume === 'number') return res.volume.volume;
    if (res.volumeStatus && typeof res.volumeStatus.volume === 'number') return res.volumeStatus.volume;
    if (typeof res.volume === 'number') return res.volume;
    return null;
  }

  function extractMuted(res) {
    if (!res) return false;
    if (res.volume && typeof res.volume.muted === 'boolean') return res.volume.muted;
    if (res.volumeStatus && typeof res.volumeStatus.muteStatus === 'boolean') return res.volumeStatus.muteStatus;
    if (typeof res.muted === 'boolean') return res.muted;
    return false;
  }

  function requestOn(uri) {
    try {
      return window.webOS.service.request(uri, {
        method: 'getVolume',
        parameters: {subscribe: true},
        onSuccess: function (res) {
          const volume = extractVolume(res);
          if (volume !== null) onChange(volume, extractMuted(res));
        },
        onFailure: function () {}
      });
    } catch (err) {
      return null;
    }
  }

  const handle = requestOn('luna://com.webos.audio') || requestOn('luna://com.webos.service.audio');

  return function () {
    if (handle && typeof handle.cancel === 'function') {
      handle.cancel();
    }
  };
}