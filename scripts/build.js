#!/usr/bin/env node

const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');
const {execSync} = require('child_process');

const {readVersion} = require('./read-version');
const {verifyIpk} = require('./verify-ipk');

const root = path.join(__dirname, '..');
const dist = path.join(root, 'dist');

// Dev / source-only assets that must not ship in the .ipk.
const PACK_EXCLUDE = new Set([
  'icon-source.png',
  'icon-source-clay.png',
  'icon-source-v6-backup.png',
  // Older icon generations — appinfo uses v8 only.
  'icon-60-v4.png',
  'icon-80-v4.png',
  'icon-130-v4.png',
  'icon-60-v5.png',
  'icon-80-v5.png',
  'icon-130-v5.png',
  'icon-60-v6.png',
  'icon-80-v6.png',
  'icon-130-v6.png',
  'icon-60-v7.png',
  'icon-80-v7.png',
  'icon-130-v7.png',
  'icon-source-doorway-backup.png'
]);

function copyRecursive(src, dest) {
  if (!fs.existsSync(src)) return;
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, {recursive: true});
    for (const entry of fs.readdirSync(src)) {
      if (PACK_EXCLUDE.has(entry)) continue;
      copyRecursive(path.join(src, entry), path.join(dest, entry));
    }
    return;
  }
  if (PACK_EXCLUDE.has(path.basename(src))) return;
  fs.mkdirSync(path.dirname(dest), {recursive: true});
  fs.copyFileSync(src, dest);
}

async function build() {
  const version = readVersion();

  fs.rmSync(dist, {recursive: true, force: true});
  fs.mkdirSync(dist, {recursive: true});

  await esbuild.build({
    entryPoints: [path.join(root, 'src/js/main.js')],
    outfile: path.join(dist, 'main.js'),
    bundle: true,
    format: 'iife',
    target: ['es2015'],
    minify: true,
    legalComments: 'none',
    define: {
      __LOUNGE_VERSION__: JSON.stringify(version)
    }
  });

  copyRecursive(path.join(root, 'src/index.html'), path.join(dist, 'index.html'));
  // Cache-bust asset URLs. webOS WAM caches web resources by URL and ignores
  // the app version bump, so without a changing query string a reinstall keeps
  // serving the previously cached main.js/CSS. Appending ?v=<version> forces a
  // fresh fetch on every version change.
  const indexPath = path.join(dist, 'index.html');
  let indexHtml = fs.readFileSync(indexPath, 'utf8');
  indexHtml = indexHtml
    .replace('href="styles/main.css"', 'href="styles/main.css?v=' + version + '"')
    .replace('src="webOS.js"', 'src="webOS.js?v=' + version + '"')
    .replace('src="main.js"', 'src="main.js?v=' + version + '"');
  fs.writeFileSync(indexPath, indexHtml);
  copyRecursive(path.join(root, 'src/styles'), path.join(dist, 'styles'));
  copyRecursive(path.join(root, 'assets'), path.join(dist, 'assets'));
  fs.copyFileSync(path.join(root, 'version.md'), path.join(dist, 'version.md'));

  // Root Home-button watcher + boot-on-start helpers (started via settings / init.d).
  [
    'home-watcher.sh',
    'enable-home-watcher.sh',
    'disable-home-watcher.sh',
    'boot-launch.sh',
    'enable-boot-launch.sh',
    'disable-boot-launch.sh'
  ].forEach(function (name) {
    const src = path.join(root, 'scripts', name);
    if (fs.existsSync(src)) {
      const dest = path.join(dist, name);
      fs.copyFileSync(src, dest);
      try { fs.chmodSync(dest, 0o755); } catch (err) { /* windows */ }
    }
  });

  const appinfo = JSON.parse(fs.readFileSync(path.join(root, 'appinfo.json'), 'utf8'));
  appinfo.version = version;
  fs.writeFileSync(path.join(dist, 'appinfo.json'), JSON.stringify(appinfo, null, 2) + '\n');

  const webosLib = path.join(root, 'node_modules/@procot/webostv/webOSTV/index.js');
  if (!fs.existsSync(webosLib)) {
    throw new Error('Missing @procot/webostv — run npm install');
  }
  fs.copyFileSync(webosLib, path.join(dist, 'webOS.js'));

  console.log('Built dist/');
}

async function main() {
  await build();

  if (process.argv.includes('--pack')) {
    // Use the project-local @webos-tools/cli packager. The older global
    // @webosose/ares-cli writes epoch-zero tar timestamps (1970-01-01) that
    // make pkgverifier fail on webOS 5.x with "-5: ipk verified failed".
    const aresPackage = path.join(root, 'node_modules', '.bin', 'ares-package');
    if (!fs.existsSync(aresPackage)) {
      throw new Error('Missing @webos-tools/cli — run npm install');
    }
    execSync(`"${aresPackage}" --no-minify .`, {cwd: dist, stdio: 'inherit'});

    const packed = JSON.parse(fs.readFileSync(path.join(dist, 'appinfo.json'), 'utf8'));
    const ipkPath = path.join(dist, packed.id + '_' + packed.version + '_all.ipk');
    if (!fs.existsSync(ipkPath)) {
      throw new Error('ares-package did not produce ' + ipkPath);
    }
    verifyIpk(ipkPath);
    console.log('Packaged IPK in dist/ (file dates verified)');
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});