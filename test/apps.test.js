import {beforeEach, describe, expect, it, vi} from 'vitest';

vi.mock('../src/js/luna.js', () => ({
  listApps: vi.fn(),
  getAppInfo: vi.fn(),
  launchApp: vi.fn(async () => 'ok'),
  launchAppViaRoot: vi.fn(async () => 'ok'),
  readFileAsDataUrl: vi.fn(async () => '')
}));

import {launchApp, listApps, getAppInfo} from '../src/js/luna.js';
import {createAppGrid} from '../src/js/apps.js';
import {createAllApps} from '../src/js/all-apps.js';

function makeConfig(pinnedApps, numberKeyLaunch) {
  return function () {
    return {
      launcher: {
        pinnedApps: pinnedApps,
        customApps: [],
        iconSize: 'medium',
        allAppsTile: true,
        numberKeyLaunch: numberKeyLaunch
      }
    };
  };
}

function mockInstalled(ids) {
  listApps.mockResolvedValue({
    apps: ids.map((id) => ({id: id, title: id}))
  });
  getAppInfo.mockImplementation(async (id) => ({id: id, title: id}));
}

function buildGrid(pinnedApps, numberKeyLaunch) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const grid = createAppGrid(container, makeConfig(pinnedApps, numberKeyLaunch), {});
  return {container, grid};
}

beforeEach(() => {
  vi.mocked(launchApp).mockClear();
  document.body.innerHTML = '';
});

describe('home row numbering and badges', () => {
  it('numbers every tile in reading order and shows a badge', async () => {
    mockInstalled(['netflix', 'youtube.leanback.v4']);
    const {container, grid} = buildGrid(['netflix', 'youtube.leanback.v4']);
    await grid.refresh();

    const tiles = container.querySelectorAll('.app-tile');
    // netflix, youtube, All apps, TV Settings
    expect(tiles.length).toBe(4);
    tiles.forEach((tile, i) => {
      expect(tile.dataset.appKey).toBe(String(i + 1));
      const badge = tile.querySelector('.app-key');
      expect(badge).not.toBeNull();
      expect(badge.textContent).toBe(String(i + 1));
    });
  });

  it('numbers only the first nine tiles', async () => {
    const ids = [];
    for (let i = 0; i < 12; i += 1) ids.push('app' + i);
    mockInstalled(ids);
    const {container, grid} = buildGrid(ids);
    await grid.refresh();

    expect(container.querySelectorAll('.app-tile[data-app-key]').length).toBe(9);
    expect(container.querySelectorAll('.app-key').length).toBe(9);
  });
});

describe('launchByNumber', () => {
  it('opens the tile at that position', async () => {
    mockInstalled(['netflix', 'youtube.leanback.v4']);
    const {grid} = buildGrid(['netflix', 'youtube.leanback.v4']);
    await grid.refresh();

    expect(grid.launchByNumber(2)).toBe(true);
    expect(launchApp).toHaveBeenCalledWith('youtube.leanback.v4');
  });

  it('returns false for 0, out of range and missing tiles', async () => {
    mockInstalled(['netflix']);
    const {grid} = buildGrid(['netflix']);
    await grid.refresh();

    expect(grid.launchByNumber(0)).toBe(false);
    expect(grid.launchByNumber(9)).toBe(false);
    expect(launchApp).not.toHaveBeenCalled();
  });
});

describe('All apps overlay', () => {
  it('shows no number badges on its tiles', async () => {
    mockInstalled(['netflix', 'youtube.leanback.v4']);
    const mount = document.createElement('div');
    document.body.appendChild(mount);
    const allApps = createAllApps({
      mount: mount,
      focus: {focusWithin: function () {}},
      launch: function () {}
    });

    await allApps.show();

    expect(mount.querySelectorAll('.app-tile').length).toBeGreaterThan(0);
    expect(mount.querySelectorAll('.app-key').length).toBe(0);
    expect(mount.querySelectorAll('[data-app-key]').length).toBe(0);
  });
});

describe('numberKeyLaunch off', () => {
  it('numbers no tiles and does not launch by number', async () => {
    mockInstalled(['netflix', 'youtube.leanback.v4']);
    const {container, grid} = buildGrid(['netflix', 'youtube.leanback.v4'], false);
    await grid.refresh();

    expect(container.querySelectorAll('.app-tile[data-app-key]').length).toBe(0);
    expect(container.querySelectorAll('.app-key').length).toBe(0);
    expect(grid.launchByNumber(1)).toBe(false);
    expect(launchApp).not.toHaveBeenCalled();
  });
});

describe('settingsTile', () => {
  it('leaves the TV Settings tile off the home row when switched off', async () => {
    mockInstalled(['netflix']);
    const container = document.createElement('div');
    document.body.appendChild(container);
    const config = makeConfig(['netflix'], true)();
    config.launcher.settingsTile = false;
    const grid = createAppGrid(container, () => config, {});
    await grid.refresh();

    expect(container.querySelector('.settings-tile')).toBeNull();
    // netflix, All apps
    expect(container.querySelectorAll('.app-tile').length).toBe(2);
  });

  it('shows the TV Settings tile when the setting is missing', async () => {
    mockInstalled(['netflix']);
    const {container, grid} = buildGrid(['netflix'], true);
    await grid.refresh();

    expect(container.querySelector('.settings-tile')).not.toBeNull();
  });
});
