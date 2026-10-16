#!/usr/bin/env node
'use strict';

// Assemble and verify an unpublished npm tarball from an exact Core commit.
const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { verifyRuntime, prepareRuntime } = require('../lib/runtime.cjs');
const { verifyReleaseSources } = require('./verify-release-sources.cjs');

function command(program, args, cwd) {
  const run = spawnSync(program, args, { cwd, encoding: 'utf8',
    timeout: 300000, maxBuffer: 8 * 1024 * 1024 });
  if (run.error || run.status !== 0) {
    throw new Error(program + ' failed: ' + (run.stderr || run.error?.message || run.status));
  }
  return run.stdout.trim();
}

async function main(args) {
  if (![2, 3].includes(args.length) || !args.every((s) => path.isAbsolute(s))) {
    throw new Error('Usage: node npm/scripts/package.cjs /absolute/payload /absolute/output [/absolute/release-sources]');
  }
  if (process.platform !== 'darwin' || process.arch !== 'arm64') {
    throw new Error('Build the npm payload on Apple Silicon macOS.');
  }
  const [payload, output] = args.map((s) => path.resolve(s));
  const source = path.resolve(__dirname, '..');
  const manifest = JSON.parse(await fsp.readFile(path.join(payload, 'manifest.json'), 'utf8'));
  const head = command('git', ['rev-parse', 'HEAD'], path.join(source, '..'));
  if (!/^[0-9a-f]{40}$/.test(head) || manifest.core?.commit !== head) {
    throw new Error('Payload source commit differs from checkout HEAD.');
  }
  const scratch = await fsp.mkdtemp(path.join(os.tmpdir(), 'om-npm-package-'));
  try {
    const container = path.join(scratch, 'payload');
    const stage = path.join(scratch, 'package');
    await fsp.mkdir(container);
    await fsp.mkdir(stage);
    for (const name of ['package.json', 'README.md', 'THIRD_PARTY.md', 'release-sources.json', 'bin', 'lib',
      'scripts/assert-payload.cjs', 'scripts/verify-release-sources.cjs']) {
      fs.cpSync(path.join(source, name), path.join(stage, name), {
        recursive: true, verbatimSymlinks: true
      });
    }
    fs.copyFileSync(path.join(source, '..', 'LICENSE'), path.join(stage, 'LICENSE'));
    const fullRelease = args.length === 3;
    const expectedSources = JSON.parse(await fsp.readFile(path.join(source, 'release-sources.json'), 'utf8'));
    if (fullRelease) {
      if (manifest.git?.version !== '2.53.0' || manifest.git?.build !== 'v2.53.0-3/f49d009' ||
          manifest.python?.build !== '20261003') {
        throw new Error('Runtime Git/Python source does not match pinned release sources.');
      }
      fs.cpSync(path.resolve(args[2]), path.join(stage, 'third_party_sources'),
        { recursive: true, verbatimSymlinks: true });
      await verifyReleaseSources(stage, expectedSources);
    }
    fs.cpSync(payload, path.join(container, 'runtime'), {
      recursive: true, verbatimSymlinks: true
    });
    const digest = crypto.createHash('sha256').update(
      await fsp.readFile(path.join(container, 'runtime/manifest.json'))).digest('hex');
    await fsp.writeFile(path.join(container, 'runtime.sha256'), digest + '\n');
    await verifyRuntime(container);
    const archive = path.join(stage, 'runtime.tar.gz');
    command('/usr/bin/tar', ['-czf', archive, '-C', container, 'runtime'], scratch);
    const archiveDigest = crypto.createHash('sha256').update(
      await fsp.readFile(archive)).digest('hex');
    await fsp.writeFile(path.join(stage, 'runtime.sha256'), digest + '\n');
    await fsp.writeFile(path.join(stage, 'runtime.archive.sha256'), archiveDigest + '\n');
    const prepared = await prepareRuntime(stage, path.join(scratch, 'first-check'));
    if (prepared.manifest.core.commit !== head) throw new Error('Core identity mismatch.');
    await fsp.mkdir(output, { recursive: true });
    const name = 'openmachine-' + require('../package.json').version + '.tgz';
    if (fs.existsSync(path.join(output, name))) throw new Error('Refusing to overwrite an existing npm package.');
    const packed = JSON.parse(command('npm', [
      'pack', '--ignore-scripts', '--json', '--pack-destination', output
    ], stage));
    if (!Array.isArray(packed) || packed.length !== 1 || packed[0].filename !== name) {
      throw new Error('npm did not pack the expected package.');
    }
    const unpack = path.join(scratch, 'unpacked');
    await fsp.mkdir(unpack);
    command('/usr/bin/tar', ['-xzf', path.join(output, name), '-C', unpack], scratch);
    const finalPackage = path.join(unpack, 'package');
    const verified = await prepareRuntime(finalPackage, path.join(scratch, 'final-check'));
    if (fullRelease) await verifyReleaseSources(finalPackage, expectedSources);
    if (verified.sha256 !== digest || verified.manifest.core.commit !== head) {
      throw new Error('Final npm package integrity or source identity mismatch.');
    }
    process.stdout.write(JSON.stringify({ package: path.join(output, name),
      core_version: manifest.core.version, core_commit: head, manifest_sha256: digest,
      archive_sha256: archiveDigest, source_archives: fullRelease ? expectedSources.archives.length : 0 }, null, 2) + '\n');
  } finally {
    await fsp.rm(scratch, { recursive: true, force: true });
  }
}

main(process.argv.slice(2)).catch((error) => {
  process.stderr.write('npm packaging failed: ' + error.message + '\n');
  process.exitCode = 1;
});
