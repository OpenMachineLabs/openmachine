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
const { verifyReleaseSources } = require('../../npm/scripts/verify-release-sources.cjs');

const VERSION = require('../../npm/package.json').version;
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
    core: { version: VERSION, commit, wheel: 'wheels/openmachine.whl' },
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
  assert.equal(first.manifest.core.version, VERSION);
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

test('startup sets only the explicit login preference and verifies the native result', async () => {
  const calls = [];
  const inject = { platform: 'darwin', arch: 'arm64', output: { write() {} },
    verify: async () => ({}),
    manager: async (_runtime, _config, _state, action, args) => {
      calls.push([action, ...args]);
      return { status: 'stopped', startup: { enabled: args[1] === 'true' } };
    } };
  await run(['startup', 'on'], inject);
  await run(['startup', 'off'], inject);
  assert.deepEqual(calls, [['startup', '--enabled', 'true'], ['startup', '--enabled', 'false']]);
  await assert.rejects(run(['startup', 'on'], { ...inject,
    manager: async () => ({ status: 'unknown', reason: 'startup_denied' }) }), /startup_denied/);
  await assert.rejects(run(['startup', 'off'], { ...inject,
    manager: async () => ({ status: 'ready', startup: { enabled: true } }) }), /not verified/);
  for (const args of [['startup'], ['startup', 'yes'], ['startup', 'on', 'extra'], ['update', '--force']]) {
    await assert.rejects(run(args, { verify: async () => assert.fail('Invalid arguments reached Core') }),
      /Usage:|Unexpected arguments/);
  }
});

test('update uses the bundled payload and retains the original ID across a lost reply', async () => {
  const calls = [];
  const runtime = { root: '/fixture/runtime', sha256: 'a'.repeat(64) };
  let operationId;
  let lost = true;
  const completed = () => ({ status: 'completed', operation: {
    operation_id: operationId, phase: 'completed',
    request: { payload_sha256: runtime.sha256 }, prepared: { identity: 'b'.repeat(64) }
  } });
  const inject = { platform: 'darwin', arch: 'arm64', output: { write() {} },
    verify: async () => runtime, sleep: async () => {},
    manager: async (_runtime, _config, _state, action, args) => {
      calls.push([action, ...(args || [])]);
      if (action === 'update') {
        if (operationId) assert.equal(args[1], operationId);
        operationId = args[1];
        assert.deepEqual(args.slice(2), ['--payload', runtime.root, '--sha256', runtime.sha256]);
        if (lost) { lost = false; throw new Error('Lost reply'); }
        return { status: 'busy', operation: { operation_id: operationId } };
      }
      if (action === 'operation') {
        assert.deepEqual(args, ['--operation-id', operationId]);
        return completed();
      }
      assert.equal(action, 'status');
      return { status: 'stopped', current: 'b'.repeat(64) };
    } };
  await assert.rejects(run(['update'], inject), /Lost reply/);
  assert.equal((await run(['update'], inject)).status, 'stopped');
  assert.match(operationId, /^[0-9a-f]{32}$/);
  assert.deepEqual(calls.map((call) => call[0]), ['update', 'update', 'operation', 'status']);
  const firstId = operationId;
  await run(['update'], { ...inject, verify: async () => ({ ...runtime, sha256: 'c'.repeat(64) }),
    manager: async (_runtime, _config, _state, action, args) => {
      if (action === 'update') {
        assert.notEqual(args[1], firstId);
        return { ...completed(), operation: { ...completed().operation, operation_id: args[1],
          request: { payload_sha256: 'c'.repeat(64) } } };
      }
      return { status: 'ready', current: 'b'.repeat(64) };
    } });
});

test('update reports deferred, failed and unrelated busy outcomes without retrying or starting', async () => {
  for (const status of ['continuation_required', 'recovery_required', 'rejected', 'restored', 'busy', 'unknown']) {
    const calls = [];
    await assert.rejects(run(['update'], { platform: 'darwin', arch: 'arm64', output: { write() {} },
      verify: async () => ({ root: '/fixture/runtime', sha256: 'a'.repeat(64) }),
      sleep: async () => assert.fail('Unrelated busy state must not be polled'),
      manager: async (_runtime, _config, _state, action) => {
        calls.push(action);
        return { status, reason: 'fixture_reason' };
      } }), /Update needs review/);
    assert.deepEqual(calls, ['update']);
  }
});

test('update stops waiting at its deadline without cancelling or replacing the operation', async (t) => {
  let now = 0;
  t.mock.method(Date, 'now', () => now);
  const calls = [];
  await assert.rejects(run(['update'], { platform: 'darwin', arch: 'arm64', output: { write() {} },
    verify: async () => ({ root: '/fixture/runtime', sha256: 'a'.repeat(64) }),
    sleep: async () => { now = 8 * 60 * 1000; },
    manager: async (_runtime, _config, _state, action, args) => {
      calls.push(action);
      return { status: 'busy', operation: { operation_id: args[1] } };
    } }), /Update still running/);
  assert.deepEqual(calls, ['update', 'operation']);
});

test('a completed update only succeeds when its exact installation is ready or stopped', async () => {
  for (const [status, current, succeeds] of [
    ['ready', 'b'.repeat(64), true], ['stopped', 'b'.repeat(64), true],
    ['ready', 'c'.repeat(64), false], ['unready', 'b'.repeat(64), false]
  ]) {
    const request = run(['update'], { platform: 'darwin', arch: 'arm64', output: { write() {} },
      verify: async () => ({ root: '/fixture/runtime', sha256: 'a'.repeat(64) }),
      manager: async (_runtime, _config, _state, action, args) => action === 'update'
        ? { status: 'completed', operation: { phase: 'completed', operation_id: args[1],
          request: { payload_sha256: 'a'.repeat(64) }, prepared: { identity: 'b'.repeat(64) } } }
        : { status, current } });
    if (succeeds) assert.equal((await request).status, status);
    else await assert.rejects(request, /not verified/);
  }
});

for (const [command, status, exitCode] of [
  ['start', 'ready', 0],
  ['start', 'unready', 1],
  ['doctor', 'unready', 1],
  ['status', 'unready', 0]
]) {
  test(`CLI ${command} with ${status} preserves diagnostics and exits ${exitCode}`, () => {
    const result = spawnSync(process.execPath, ['-e', `
      const cli = require(${JSON.stringify(require.resolve('../../npm/lib/cli.cjs'))});
      const run = cli.run;
      cli.run = (args) => run(args, {
        platform: 'darwin', arch: 'arm64',
        verify: async () => ({}),
        manager: async () => ({ status: ${JSON.stringify(status)} })
      });
      process.argv = [process.execPath, 'openmachine', ${JSON.stringify(command)}];
      require(${JSON.stringify(require.resolve('../../npm/bin/openmachine.cjs'))});
    `], { encoding: 'utf8', timeout: 5000 });
    assert.equal(result.status, exitCode, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), { status });
    assert.equal(result.stderr,
      exitCode ? `openmachine: Core is not ready: ${status}\n` : '');
  });
}

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
  assert.deepEqual(files, ['openmachine-' + require('../../npm/package.json').version + '.tgz']);
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


test('release source archives are mandatory, immutable and unlinked', async (t) => {
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const dir = path.join(root, 'third_party_sources');
  await fsp.mkdir(dir);
  const archives = Array.from({ length: 5 }, (_, i) => ({
    filename: 'source-' + i + '.tar.gz',
    sha256: sha('source-payload-' + i),
    url: 'https://example.invalid/source'
  }));
  const pins = { format: 1, target: 'macos-arm64', git_commit: 'a'.repeat(40),
    dugite_commit: 'b'.repeat(40), archives };
  await fsp.writeFile(path.join(root, 'release-sources.json'), JSON.stringify(pins));
  for (let i = 0; i < 5; i++) {
    await fsp.writeFile(path.join(dir, archives[i].filename), 'source-payload-' + i);
  }
  assert.equal((await verifyReleaseSources(root, pins)).count, 5);
  await fsp.appendFile(path.join(dir, archives[0].filename), 'tampered');
  await assert.rejects(verifyReleaseSources(root, pins), /hash or permissions mismatch/);
  await fsp.writeFile(path.join(dir, archives[0].filename), 'source-payload-0');
  await fsp.writeFile(path.join(dir, 'unexpected.txt'), 'bad');
  await assert.rejects(verifyReleaseSources(root, pins), /unexpected/);
  await fsp.rm(path.join(dir, 'unexpected.txt'));
  await fsp.rm(path.join(dir, archives[0].filename));
  await fsp.symlink('/etc/passwd', path.join(dir, archives[0].filename));
  await assert.rejects(verifyReleaseSources(root, pins), /hash or permissions mismatch/);
  await fsp.rm(path.join(dir, archives[0].filename));
  await assert.rejects(verifyReleaseSources(root, pins), /Missing or unexpected/);
});

test('release validation fails without attached corresponding sources', async (t) => {
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  await fsp.writeFile(path.join(root, 'release-sources.json'),
    await fsp.readFile(path.resolve('npm/release-sources.json')));
  await assert.rejects(verifyReleaseSources(root), /ENOENT/);
});


test('npm version matches Core and mismatched payload cannot be released', async (t) => {
  const metadata = require('../../npm/package.json');
  const core = fs.readFileSync(path.resolve('openmachine/__init__.py'), 'utf8');
  const version = core.match(/^__version__ = "([^"]+)"$/m);
  assert.ok(version, 'Core must declare a release version');
  assert.equal(metadata.version, version[1]);
  if (process.platform !== 'darwin' || process.arch !== 'arm64') return;
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const revision = spawnSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' });
  assert.equal(revision.status, 0);
  const pkg = await fixturePackage(root, revision.stdout.trim());
  assert.equal(spawnSync('/usr/bin/tar',
    ['-xzf', path.join(pkg, 'runtime.tar.gz'), '-C', root]).status, 0);
  const payload = path.join(root, 'runtime');
  const manifestPath = path.join(payload, 'manifest.json');
  const manifest = JSON.parse(await fsp.readFile(manifestPath, 'utf8'));
  manifest.core.version = '0.2.20';
  await fsp.writeFile(manifestPath, JSON.stringify(manifest));
  const output = path.join(root, 'dist');
  const result = spawnSync(process.execPath,
    ['npm/scripts/package.cjs', payload, output], { encoding: 'utf8', timeout: 120000 });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /installer version does not match bundled Core version/);
  assert.equal(fs.existsSync(output), false);
});


test('only the historical tag may override the packager HEAD', async (t) => {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') return;
  const root = await temp();
  t.after(() => fsp.rm(root, { recursive: true, force: true }));
  const fixture = await fixturePackage(root, 'f'.repeat(40));
  assert.equal(spawnSync('/usr/bin/tar',
    ['-xzf', path.join(fixture, 'runtime.tar.gz'), '-C', root]).status, 0);
  const payload = path.join(root, 'runtime');
  const result = spawnSync(process.execPath, ['npm/scripts/package.cjs',
    payload, path.join(root, 'dist'), path.join(root, 'sources'), 'v0.2.21'],
  { encoding: 'utf8', timeout: 30000 });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Payload source commit differs from checkout HEAD/);
  assert.equal(fs.existsSync(path.join(root, 'dist')), false);
});

test('future npm publication requires the exact published Release tag', () => {
  const wf = fs.readFileSync('.github/workflows/npm-release.yml', 'utf8');
  assert.match(wf, /types: \[published\]/);
  assert.match(wf, /ref: \$\{\{ github\.event\.release\.tag_name \|\| inputs\.tag \}\}/);
  assert.match(wf, /tagged\/openmachine\/__init__\.py/);
  assert.match(wf, /test "\$GITHUB_EVENT_NAME" = workflow_dispatch/);
  assert.match(wf, /if: github\.event_name == 'release'/);
  assert.match(wf, /npm publish.*VERSION/);
});
