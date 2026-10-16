'use strict';
const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');

async function hashOf(file) {
  const hash = crypto.createHash('sha256');
  const handle = await fsp.open(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    for await (const chunk of handle.createReadStream({ autoClose: false })) hash.update(chunk);
  } finally {
    await handle.close();
  }
  return hash.digest('hex');
}

async function verifyReleaseSources(packageRoot, expected = null) {
  const metadata = JSON.parse(await fsp.readFile(
    path.join(packageRoot, 'release-sources.json'), 'utf8'));
  if (expected && JSON.stringify(metadata) !== JSON.stringify(expected)) {
    throw new Error('Release source manifest differs from pinned producer.');
  }
  if (metadata.format !== 1 || metadata.target !== 'macos-arm64' ||
      !Array.isArray(metadata.archives) || metadata.archives.length !== 5 ||
      !/^[a-f0-9]{40}$/.test(metadata.git_commit) ||
      !/^[a-f0-9]{40}$/.test(metadata.dugite_commit)) {
    throw new Error('Unsupported release source manifest.');
  }
  const directory = path.join(packageRoot, 'third_party_sources');
  const folder = await fsp.lstat(directory);
  if (!folder.isDirectory() || (folder.mode & 0o022)) {
    throw new Error('Unsafe release source directory.');
  }
  const filenames = metadata.archives.map((entry) => entry.filename);
  if (new Set(filenames).size !== filenames.length ||
      filenames.some((name) => !/^[a-z0-9][a-z0-9._-]{1,100}\.tar\.(?:xz|gz)$/.test(name))) {
    throw new Error('Invalid release source filenames.');
  }
  const actual = await fsp.readdir(directory);
  if (actual.length !== filenames.length || actual.some((name) => !filenames.includes(name))) {
    throw new Error('Missing or unexpected release source archives.');
  }
  for (const entry of metadata.archives) {
    if (!/^[a-f0-9]{64}$/.test(entry.sha256) ||
        typeof entry.url !== 'string' || !entry.url.startsWith('https://')) {
      throw new Error('Invalid release source pin.');
    }
    const file = path.join(directory, entry.filename);
    const info = await fsp.lstat(file);
    if (!info.isFile() || (info.mode & 0o022) || info.size === 0 ||
        info.size > 100 * 1024 * 1024 || await hashOf(file) !== entry.sha256) {
      throw new Error('Release source hash or permissions mismatch: ' + entry.filename);
    }
  }
  return { count: filenames.length, git_commit: metadata.git_commit };
}

module.exports = { verifyReleaseSources };
