#!/usr/bin/env node
//
// Fail if an IPK's control/data tarballs stamp files at the Unix epoch.
// @webosose/ares-cli on Node.js 22+ does that, and some TVs (webOS 5)
// refuse to install the package. @webos-tools/cli is not affected.

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const {readVersion} = require('./read-version');

const EPOCH_CUTOFF = Date.UTC(2000, 0, 1) / 1000;

function parseAr(buf) {
  if (buf.subarray(0, 8).toString('ascii') !== '!<arch>\n') {
    throw new Error('Not a Debian/IPK ar archive');
  }

  const members = [];
  let offset = 8;

  while (offset + 60 <= buf.length) {
    const header = buf.subarray(offset, offset + 60);
    const name = header.subarray(0, 16).toString('latin1').trim().replace(/\/$/, '');
    const size = parseInt(header.subarray(48, 58).toString('ascii').trim(), 10);

    if (!Number.isFinite(size) || size < 0) {
      throw new Error('Invalid ar member size at offset ' + offset);
    }

    offset += 60;
    members.push({name: name, data: buf.subarray(offset, offset + size)});
    offset += size + (size % 2);
  }

  return members;
}

function parseTar(buf) {
  const members = [];
  let offset = 0;

  while (offset + 512 <= buf.length) {
    const block = buf.subarray(offset, offset + 512);
    if (block.every(function (b) { return b === 0; })) {
      break;
    }

    const name = block.subarray(0, 100).toString('utf8').replace(/\0.*$/, '');
    const sizeOctal = block.subarray(124, 136).toString('ascii').replace(/\0.*$/, '').trim();
    const mtimeOctal = block.subarray(136, 148).toString('ascii').replace(/\0.*$/, '').trim();
    const size = parseInt(sizeOctal || '0', 8);
    const mtime = parseInt(mtimeOctal || '0', 8);

    if (name) {
      members.push({name: name, mtime: mtime});
    }

    offset += 512 + Math.ceil(size / 512) * 512;
  }

  return members;
}

function gunzipTar(data, label) {
  try {
    return parseTar(zlib.gunzipSync(data));
  } catch (err) {
    throw new Error('Failed to read ' + label + ': ' + err.message);
  }
}

function verifyIpk(ipkPath) {
  const members = parseAr(fs.readFileSync(ipkPath));
  const stale = [];

  for (let i = 0; i < members.length; i += 1) {
    const member = members[i];
    if (!/^((control|data)\.tar(\.gz)?)$/.test(member.name)) {
      continue;
    }

    const files = member.name.endsWith('.gz')
      ? gunzipTar(member.data, member.name)
      : parseTar(member.data);

    for (let j = 0; j < files.length; j += 1) {
      const file = files[j];
      if (file.mtime < EPOCH_CUTOFF) {
        stale.push({
          archive: member.name,
          name: file.name,
          mtime: file.mtime
        });
      }
    }
  }

  if (stale.length) {
    const sample = stale.slice(0, 8)
      .map(function (f) {
        return '  ' + f.archive + ': ' + f.name + ' (mtime=' + f.mtime + ')';
      })
      .join('\n');
    throw new Error(
      path.basename(ipkPath) + ' stamps ' + stale.length +
      ' file(s) before 2000-01-01 (usually 1970-01-01 from @webosose/ares-cli on Node.js 22+).\n' +
      'Package with @webos-tools/cli instead (npm run pack).\n' + sample
    );
  }

  return ipkPath;
}

function defaultIpkPath() {
  const root = path.resolve(__dirname, '..');
  const appinfo = JSON.parse(fs.readFileSync(path.join(root, 'appinfo.json'), 'utf8'));
  const version = readVersion();
  return path.join(root, 'dist', appinfo.id + '_' + version + '_all.ipk');
}

if (require.main === module) {
  const ipkPath = process.argv[2] || defaultIpkPath();

  if (!fs.existsSync(ipkPath)) {
    console.error('Missing IPK: ' + ipkPath);
    process.exit(1);
  }

  try {
    verifyIpk(ipkPath);
    console.log('verify-ipk: ' + path.basename(ipkPath) + ' file dates look valid');
  } catch (err) {
    console.error(err.message);
    process.exit(1);
  }
}

module.exports = {verifyIpk: verifyIpk, defaultIpkPath: defaultIpkPath};
