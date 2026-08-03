import {getAllInputStatus, switchInput} from './luna.js';

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

    const visible = devices.filter(isConfigured);
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
    try {
      const res = await getAllInputStatus();
      devices = (res && res.devices) || [];

      const chosen = devices.find(function (d) {
        return d.chosen || d.activate;
      });
      if (chosen) currentInputId = chosen.id;
    } catch (err) {
      devices = [
        {id: 'HDMI_1', label: 'HDMI 1', chosen: true},
        {id: 'HDMI_2', label: 'HDMI 2'},
        {id: 'HDMI_3', label: 'HDMI 3'},
        {id: 'TV', label: 'TV', appId: 'com.webos.app.livetv'}
      ];
    }

    render();
  }

  return {
    refresh: refresh,
    getDevices: function () {
      return devices.slice();
    }
  };
}

export async function fetchInputDevices() {
  try {
    const res = await getAllInputStatus();
    return (res && res.devices) || [];
  } catch (err) {
    return [
      {id: 'HDMI_1', label: 'HDMI 1'},
      {id: 'HDMI_2', label: 'HDMI 2'},
      {id: 'HDMI_3', label: 'HDMI 3'},
      {id: 'TV', label: 'TV', appId: 'com.webos.app.livetv'}
    ];
  }
}