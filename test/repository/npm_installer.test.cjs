'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { prepareRuntime, verifyRuntime } = require('../../npm/lib/runtime.cjs');
const { run, workspacePath, probePath, optionsOf } = require('../../npm/lib/cli.cjs');

const sha = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const temp = () => fsp.mkdtemp(path.join(os.tmpdir(), 'om-npm-test-'));

async function fixturePackage(root, commit = 'c'.repeat(40)) {
  const pkg = path.join(root, 'package');
  const runtime = path.join(pkg, 'runtime');
  await fsp.mkdir(path.join(runtime, 'python/bin'), { recursive: true });
  await fsp.mkdir(path.join(runtime, 'bin'), { recursive: true });
  await fsp.writeFile(path.join(runtime, 'python/bin/python3'), '#!python\n', { mode: 0o755 });
  await fsp.writeFile(path.join(runtime, 'bin/git'), '#!git\n', { mode: 0o755 });
  await fsp.symlink('python3', path.join(runtime, 'python/bin/python-link'));
  const manifest = { format: 1, target: { os: 'macos', arch: 'arm64', minimum_os: '13.0' },
    core: { version: '0.2.21', commit, wheel: 'wheels/openmachine.whl' },
    files: {
      python: { directory: true }, 'python/bin': { directory: true },
      'python/bin/python3': { bytes: 9, sha256: sha('#!python\n'), executable: true },
      'python/bin/python-link': { link: 'python3' },
      bin: { directory: true }, 'bin/git': { bytes: 6, sha256: sha('#!git\n'), executable: true }
    } };
  await fsp.writeFile(path.join(runtime, 'manifest.json'), JSON.stringify(manifest));
  await fsp.writeFile(path.join(pkg, 'runtime.sha256'),
    sha(await fsp.readFile(path.join(runtime, 'manifest.json'))) + '\n');
  assert.equal(spawnSync('/usr/bin/tar',
    ['-czf', path.join(pkg, 'runtime.tar.gz'), '-C', pkg, 'runtime']).status, 0);
  await fsp.writeFile(path.join(pkg, 'runtime.archive.sha256'),
    sha(await fsp.readFile(path.join(pkg, 'runtime.tar.gz'))) + '\n');
  await fsp.rm(runtime, { recursive: true, force: true });
  return pkg;
}

test('runtime archive is checked, extracted privately and verified on reuse', async (t) => {
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const pkg = await fixturePackage(root);
  const home = path.join(root, 'home');
  const first = await prepareRuntime(pkg, home);
  assert.equal(first.manifest.core.version, '0.2.21');
  assert.equal(await fsp.readlink(path.join(first.root, 'python/bin/python-link')), 'python3');
  assert.equal((await prepareRuntime(pkg, home)).root, first.root);
  await fsp.writeFile(path.join(first.root, 'bin/git'), 'tampered');
  await assert.rejects(prepareRuntime(pkg, home), /mismatch/);
});

test('modified archive is refused before it is extracted', async (t) => {
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const pkg = await fixturePackage(root);
  await fsp.appendFile(path.join(pkg, 'runtime.tar.gz'), 'tampered');
  await assert.rejects(prepareRuntime(pkg, path.join(root, 'home')), /digest mismatch/);
  assert.equal(fs.existsSync(path.join(root, 'home')), false);
});

test('payload verifier refuses changed symbolic links', async (t) => {
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const pkg = await fixturePackage(root);
  const unpacked = await prepareRuntime(pkg, path.join(root, 'home'));
  await fsp.rm(path.join(unpacked.root, 'python/bin/python-link'));
  await fsp.symlink('/etc/passwd', path.join(unpacked.root, 'python/bin/python-link'));
  await assert.rejects(verifyRuntime(path.dirname(unpacked.root), unpacked.sha256), /link mismatch/);
});

test('workspace path and health probe are restricted', async (t) => {
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const project = path.join(root, 'project');
  await fsp.mkdir(project);
  await fsp.writeFile(path.join(project, 'README.md'), 'health\n');
  assert.equal(await workspacePath(project, root), await fsp.realpath(project));
  assert.equal(await probePath(project), 'README.md');
  await assert.rejects(workspacePath(root, root), /home directory/);
  await assert.rejects(probePath(project, '../secret'), /Probe must be/);
  await assert.rejects(probePath(project, 'missing'), /No safe/);
  assert.deepEqual(optionsOf(['--enable-commands', '--workspace', project]),
    { workspace: project, probe: null, commands: true, writes: false });
});

test('first setup uses Core management without granting commands or writes', async (t) => {
  const home = await temp();
  t.after(() => fsp.rm(home, { recursive: true, force: true }));
  const project = path.join(home, 'project');
  await fsp.mkdir(project);
  await fsp.writeFile(path.join(project, 'README.md'), 'health\n');
  const calls = [];
  const output = { isTTY: false, write(s) { calls.push(['output', s]); } };
  let installed = false;
  const manager = async (_runtime, config, _state, action, args = []) => {
    calls.push([action, ...args]);
    if (action === 'status') return { status: installed ? 'ready' : 'not_installed' };
    if (action === 'configure') {
      await fsp.mkdir(path.dirname(config), { recursive: true });
      await fsp.writeFile(config, '# test\n');
      return { status: 'configured' };
    }
    if (action === 'install') {
      installed = true;
      return { status: 'completed', operation: { phase: 'completed' } };
    }
    if (action === 'start') return { status: 'ready' };
    throw new Error('Unexpected manager call');
  };
  const inject = { home, input: { isTTY: false }, output, platform: 'darwin',
    arch: 'arm64', verify: async () => ({ root: '/fixture/runtime', sha256: 'c'.repeat(64) }), manager };
  await run(['setup', '--workspace', project], inject);
  assert.ok(calls.some((x) => x[0] === 'configure' &&
    !x.includes('--enable-commands') && !x.includes('--enable-workspace-writes')));
  assert.ok(calls.some((x) => x[0] === 'install' && x.includes('--sha256')));
  assert.ok(calls.some((x) => x[0] === 'start'));
  const marker = JSON.parse(await fsp.readFile(path.join(home, '.config/openmachine/npm-install.json')));
  assert.match(marker.operationId, /^[0-9a-f]{32}$/);
  const before = calls.length;
  await run(['status'], inject);
  assert.equal(calls.length, before + 2);
});

test('npm pack preserves a bundled runtime archive on Apple Silicon', async (t) => {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') return t.skip('Requires Apple Silicon');
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const revision = spawnSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' });
  assert.equal(revision.status, 0);
  const pkg = await fixturePackage(root, revision.stdout.trim());
  const payload = path.join(root, 'payload');
  assert.equal(spawnSync('/usr/bin/tar',
    ['-xzf', path.join(pkg, 'runtime.tar.gz'), '-C', root]).status, 0);
  await fsp.rename(path.join(root, 'runtime'), payload);
  const output = path.join(root, 'dist');
  const packed = spawnSync(process.execPath,
    ['npm/scripts/package.cjs', payload, output], { encoding: 'utf8', timeout: 120000 });
  assert.equal(packed.status, 0, packed.stdout + packed.stderr);
  const files = await fsp.readdir(output);
  assert.deepEqual(files, ['openmachine-0.1.0.tgz']);
  const listing = spawnSync('/usr/bin/tar',
    ['-tzf', path.join(output, files[0])], { encoding: 'utf8' });
  assert.equal(listing.status, 0);
  assert.match(listing.stdout, /package\/runtime\.tar\.gz/);
  assert.doesNotMatch(listing.stdout, /package\/runtime\/python/);
});

test('non-interactive setup refuses missing workspace or blocked existing runtime', async (t) => {
  const home = await temp();
  t.after(() => fsp.rm(home, { recursive: true, force: true }));
  const project = path.join(home, 'project');
  await fsp.mkdir(project);
  await fsp.writeFile(path.join(project, 'README.md'), 'health\n');
  const common = { home, input: { isTTY: false }, output: { isTTY: false, write() {} },
    platform: 'darwin', arch: 'arm64', verify: async () => ({ root: '.', sha256: 'd'.repeat(64) }) };
  await assert.rejects(run(['setup'], common), /requires --workspace/);
  let checks = 0;
  await assert.rejects(run(['setup', '--workspace', project], { ...common,
    manager: async () => { checks++; return { status: 'blocked' }; }
  }), /needs review/);
  assert.equal(checks, 1);
});
