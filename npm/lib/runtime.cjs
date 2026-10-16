'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');

function inside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative !== '' && relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative);
}

function safeName(name) {
  return typeof name === 'string' && name.length > 0 &&
    !name.startsWith('/') && !name.includes('\\') && !name.includes('\0') &&
    name.split('/').every((part) => part && part !== '.' && part !== '..') &&
    path.posix.normalize(name) === name;
}

async function fileHash(file) {
  const hash = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

async function verifyRuntime(packageRoot, digestOverride = null) {
  const runtimePath = path.join(packageRoot, 'runtime');
  const reference = digestOverride || (await fsp.readFile(path.join(packageRoot, 'runtime.sha256'), 'utf8')).trim();
  if (!/^[a-f0-9]{64}$/.test(reference)) throw new Error('Missing trusted runtime digest (not a release package).');
  const rootStat = await fsp.lstat(runtimePath);
  if (!rootStat.isDirectory() || (rootStat.mode & 0o022)) throw new Error('Unsafe runtime directory.');
  const root = await fsp.realpath(runtimePath);
  const manifestPath = path.join(root, 'manifest.json');
  const manifestStat = await fsp.lstat(manifestPath);
  if (!manifestStat.isFile() || (manifestStat.mode & 0o022)) throw new Error('Unsafe runtime manifest.');
  const bytes = await fsp.readFile(manifestPath);
  if (bytes.length > 4 * 1024 * 1024 ||
      crypto.createHash('sha256').update(bytes).digest('hex') !== reference) {
    throw new Error('Runtime manifest digest mismatch.');
  }
  let manifest;
  try { manifest = JSON.parse(bytes.toString('utf8')); }
  catch { throw new Error('Invalid runtime manifest.'); }
  if (manifest.format !== 1 || !manifest.target || manifest.target.os !== 'macos' ||
      manifest.target.arch !== 'arm64' || manifest.target.minimum_os !== '13.0' ||
      !manifest.core || typeof manifest.core.version !== 'string' ||
      !manifest.files || typeof manifest.files !== 'object' || Array.isArray(manifest.files)) {
    throw new Error('Unsupported runtime payload.');
  }
  const expected = Object.keys(manifest.files);
  if (expected.length > 20000 || expected.some((name) => !safeName(name) || name === 'manifest.json')) {
    throw new Error('Unsafe runtime inventory.');
  }
  const encountered = new Set();
  let totalBytes = 0;
  async function visit(relativeDirectory) {
    const dir = path.join(root, relativeDirectory);
    for (const child of await fsp.readdir(dir, { withFileTypes: true })) {
      const name = relativeDirectory ? relativeDirectory + '/' + child.name : child.name;
      if (name === 'manifest.json') continue;
      if (!safeName(name) || encountered.has(name) || !Object.hasOwn(manifest.files, name)) {
        throw new Error('Unexpected runtime file: ' + name);
      }
      encountered.add(name);
      const fullPath = path.join(root, name);
      const stat = await fsp.lstat(fullPath);
      const declaration = manifest.files[name];
      if (!declaration || typeof declaration !== 'object' || (!stat.isSymbolicLink() && (stat.mode & 0o022))) {
        throw new Error('Unsafe runtime entry: ' + name);
      }
      if (stat.isDirectory()) {
        if (declaration.directory !== true || Object.keys(declaration).length !== 1) {
          throw new Error('Runtime directory mismatch: ' + name);
        }
        await visit(name);
      } else if (stat.isSymbolicLink()) {
        const target = await fsp.readlink(fullPath);
        if (declaration.link !== target || path.isAbsolute(target) || target.includes('\\')) {
          throw new Error('Runtime link mismatch: ' + name);
        }
        const resolved = await fsp.realpath(fullPath);
        if (!inside(root, resolved)) throw new Error('Runtime link escapes package: ' + name);
      } else if (stat.isFile()) {
        totalBytes += stat.size;
        if (totalBytes > 2 * 1024 * 1024 * 1024 ||
            declaration.bytes !== stat.size ||
            declaration.executable !== Boolean(stat.mode & 0o111) ||
            declaration.sha256 !== await fileHash(fullPath)) {
          throw new Error('Runtime file mismatch: ' + name);
        }
      } else {
        throw new Error('Unsupported runtime entry: ' + name);
      }
    }
  }
  await visit('');
  if (encountered.size !== expected.length) throw new Error('Missing runtime entries.');
  for (const required of ['python/bin/python3', 'bin/git']) {
    if (!Object.hasOwn(manifest.files, required)) throw new Error('Incomplete runtime package.');
  }
  return { root, sha256: reference, manifest };
}

function tar(args) {
  const result = spawnSync('/usr/bin/tar', args, {
    encoding: 'utf8', timeout: 120000, maxBuffer: 4 * 1024 * 1024
  });
  if (result.error || result.status !== 0) {
    throw new Error('Bundled runtime archive could not be extracted.');
  }
  return result.stdout;
}

async function prepareRuntime(packageRoot, home = os.homedir()) {
  const archive = path.join(packageRoot, 'runtime.tar.gz');
  const archiveHash = (await fsp.readFile(path.join(packageRoot, 'runtime.archive.sha256'), 'utf8')).trim();
  const manifestHash = (await fsp.readFile(path.join(packageRoot, 'runtime.sha256'), 'utf8')).trim();
  if (!/^[a-f0-9]{64}$/.test(archiveHash) || !/^[a-f0-9]{64}$/.test(manifestHash) ||
      await fileHash(archive) !== archiveHash) {
    throw new Error('Bundled runtime archive digest mismatch.');
  }
  const cache = path.join(home, '.local/share/openmachine/npm-payloads');
  await fsp.mkdir(cache, { recursive: true, mode: 0o700 });
  const info = await fsp.lstat(cache);
  if (!info.isDirectory() || (info.mode & 0o077)) throw new Error('Unsafe runtime cache.');
  const destination = path.join(cache, archiveHash);
  if (fs.existsSync(destination)) return verifyRuntime(destination, manifestHash);
  const staging = await fsp.mkdtemp(path.join(cache, '.install-'));
  try {
    const listing = tar(['-tzf', archive]);
    for (const entry of listing.split('\n').filter(Boolean)) {
      if (!(entry === 'runtime/' || entry.startsWith('runtime/')) ||
          entry.startsWith('/') || entry.split('/').includes('..') ||
          entry.includes('\\') || entry.includes('\0')) {
        throw new Error('Unsafe runtime archive member.');
      }
    }
    tar(['-xzf', archive, '-C', staging]);
    const verified = await verifyRuntime(staging, manifestHash);
    // Retained payload lives outside npm's replaceable global package.
    await fsp.rename(staging, destination);
    return { ...verified, root: await fsp.realpath(path.join(destination, 'runtime')) };
  } catch (error) {
    await fsp.rm(staging, { recursive: true, force: true });
    throw error;
  }
}

module.exports = { verifyRuntime, prepareRuntime, inside, safeName };
