import {getAllInputStatus, getAllInputStatusViaRoot, switchInput} from './luna.js';

const TV_INPUT_IDS = ['TV', 'LIVE_TV', 'TUNER'];

export function createInputRow(container, getConfig, options) {
  let devices = [];
  let currentInputId = '';

  function labelFor(device) {
    const config = getConfig();
    const labels = (config.launcher && config.launcher.inputLabels) || {};
    if (labels[device.id]) return labels[device.id];

    if (device.label) return device.label;
    return device.id.replace(/_/g, ' ');
  }

  function isConfigured(device) {
    const config = getConfig();
    // Explicit empty array means "hide all inputs". Only fall back to "show all"
    // when the key is missing (undefined/null) — never when the user cleared every
    // checkbox.
    if (!config.launcher || !Object.prototype.hasOwnProperty.call(config.launcher, 'inputs')) {
      return true;
    }
    const allowed = config.launcher.inputs;
    if (!Array.isArray(allowed) || !allowed.length) return false;

    if (allowed.indexOf(device.id) >= 0) return true;

    if (device.id.indexOf('HDMI') === 0) {
      return false;
    }

    return allowed.some(function (id) {
      return TV_INPUT_IDS.indexOf(id) >= 0;
    }) && (device.appId === 'com.webos.app.livetv' || device.id === 'TV');
  }

  function render() {
    container.innerHTML = '';

    // Live TV joins the inputs once the TV turns out to have channels.
    const all = devices.slice();
    const channels = options.channels;
    if (channels && channels.hasChannels() &&
        !all.some(function (d) { return TV_INPUT_IDS.indexOf(d.id) >= 0; })) {
      all.push(Object.assign({}, LIVE_TV_INPUT));
    }
    const visible = all.filter(isConfigured);
    visible.forEach(function (device, index) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'input-chip focusable';
      button.dataset.focusIndex = String(100 + index);
      button.dataset.inputId = device.id;
      button.textContent = labelFor(device);

      if (device.chosen || device.id === currentInputId) {
        button.classList.add('active');
      }

      button.addEventListener('click', function () {
        selectInput(device);
      });

      container.appendChild(button);
    });

    // Live TV channels (channels.js): only when the TV has channels tuned.
    if (channels && channels.available()) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'input-chip input-chip-channels focusable';
      button.dataset.focusIndex = String(100 + visible.length);
      button.dataset.channelsChip = '1';
      button.textContent = 'Channels';
      button.setAttribute('aria-expanded', channels.isOpen() ? 'true' : 'false');
      if (channels.isOpen()) button.classList.add('active');
      button.addEventListener('click', function () {
        channels.toggle();
      });
      container.appendChild(button);
    }
  }

  async function selectInput(device) {
    try {
      if (options.onBeforeLaunch) options.onBeforeLaunch();
      await switchInput(device.id, device);
      currentInputId = device.id;
      render();
    } catch (err) {
      if (options.onToast) options.onToast('Could not switch to ' + labelFor(device));
    }
  }

  async function refresh() {
    // Live TV is added in render(), once the channel list is known.
    const launcher = getConfig().launcher || {};
    devices = await fetchInputDevices(false, launcher.addedInputs || []);
    const chosen = devices.find(function (d) {
      return d.chosen || d.activate;
    });
    if (chosen) currentInputId = chosen.id;
    render();
  }

  return {
    refresh: refresh,
    // Redraw the chips only (e.g. the Channels chip appeared or opened).
    render: render,
    getDevices: function () {
      return devices.slice();
    }
  };
}

const LIVE_TV_INPUT = {id: 'TV', label: 'Live TV', appId: 'com.webos.app.livetv'};
// Offered when the TV's input list can't be read.
const FALLBACK_INPUTS = ['HDMI_1', 'HDMI_2', 'HDMI_3'];

export function isTvInputId(id) {
  return TV_INPUT_IDS.indexOf(id) >= 0;
}

/** An input from its id alone (HDMI_n or Live TV), or null. */
export function inputFromId(id) {
  const m = /^HDMI_(\d+)$/.exec(id || '');
  if (m) return {id: id, label: 'HDMI ' + m[1], appId: 'com.webos.app.hdmi' + m[1]};
  if (isTvInputId(id)) return Object.assign({}, LIVE_TV_INPUT);
  return null;
}

/**
 * Every input the TV has: the input service's list, plus any HDMI port it
 * left out (up to the TV's maxHdmiCount: some TVs list fewer to an app than
 * they have), inputs added by hand in Settings (`added`), and Live TV when
 * the TV may have channels (`hasChannels`).
 */
export async function fetchInputDevices(hasChannels, added) {
  let res = null;
  try {
    res = await getAllInputStatus().catch(function () {
      return getAllInputStatusViaRoot();
    });
  } catch (err) {
    res = null;
  }
  return completeInputList(res, hasChannels, added);
}

/**
 * fetchInputDevices' list from the input service's reply `res` (null if
 * none). Live TV is also in it whenever `res` is null, as before 0.0.112:
 * without the TV's list we can't tell.
 */
export function completeInputList(res, hasChannels, added) {
  const devices = res
    ? ((res && res.devices) || []).slice()
    : FALLBACK_INPUTS.map(inputFromId);
  function listed(id) {
    return devices.some(function (d) {
      return d && (d.id === id || (isTvInputId(id) && isTvInputId(d.id)));
    });
  }
  function addMissing(id) {
    const device = !listed(id) && inputFromId(id);
    if (device) devices.push(device);
  }
  const maxHdmi = Number(res && res.maxHdmiCount) || 0;
  for (let n = 1; n <= maxHdmi; n += 1) addMissing('HDMI_' + n);
  (added || []).forEach(addMissing);
  if (hasChannels || !res) addMissing('TV');
  // HDMI ports in port order, other inputs after them as the TV listed them.
  return devices.map(function (d, i) {
    const m = /^HDMI_(\d+)$/.exec((d && d.id) || '');
    return {d: d, key: m ? Number(m[1]) : 100 + i};
  }).sort(function (a, b) { return a.key - b.key; }).map(function (x) { return x.d; });
}