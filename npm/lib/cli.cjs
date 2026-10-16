'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const readline = require('node:readline/promises');
const { prepareRuntime, inside } = require('./runtime.cjs');

const PACKAGE_ROOT = path.resolve(__dirname, '..');
const PKG = require('../package.json');
const HELP = 'Open Machine npm installer (Apple Silicon macOS)\n' +
  '\nUsage:\n  openmachine                  Set up once, or show service status\n' +
  '  openmachine setup [--workspace PATH] [--probe FILE]\n' +
  '                    [--enable-commands] [--enable-workspace-writes]\n' +
  '  openmachine status | start | stop | doctor\n' +
  '  openmachine core <arguments>  Run the underlying Python Core CLI\n' +
  '  openmachine --version | --help\n' +
  '\nInstallation leaves machine commands and workspace writes OFF unless approved.\n';

function optionsOf(argv) {
  const options = { workspace: null, probe: null, commands: false, writes: false };
  for (let index = 0; index < argv.length; index++) {
    const argument = argv[index];
    if ((argument === '--workspace' || argument === '--probe') && argv[index + 1]) {
      options[argument === '--workspace' ? 'workspace' : 'probe'] = argv[++index];
    } else if (argument === '--enable-commands') {
      options.commands = true;
    } else if (argument === '--enable-workspace-writes') {
      options.writes = true;
    } else {
      throw new Error('Unrecognized setup argument: ' + argument);
    }
  }
  return options;
}

function management(runtime, config, state, action, args = []) {
  const argv = ['-I', '-B', '-m', 'openmachine.cli', '--config', config,
    '--state-dir', state, 'manage', '--contract', '1', action, ...args];
  const result = spawnSync(path.join(runtime.root, 'python/bin/python3'), argv, {
    encoding: 'utf8', timeout: 120000, maxBuffer: 1024 * 1024,
    env: { ...process.env, PYTHONNOUSERSITE: '1',
      PATH: path.join(runtime.root, 'bin') + path.delimiter + (process.env.PATH || '') }
  });
  if (result.error) throw new Error('Core management failed: ' + result.error.message);
  let response;
  try { response = JSON.parse(result.stdout); }
  catch {
    try { response = JSON.parse(result.stderr); }
    catch { throw new Error('Core management did not return valid JSON (' + action + ').'); }
  }
  if (response.error) throw new Error(response.error.code + ': ' + response.error.message);
  if (response.management_contract !== 1) {
    throw new Error('Unsupported Core management contract.');
  }
  return response;
}

async function workspacePath(input, home) {
  if (!input) throw new Error('Choose an existing workspace: openmachine setup --workspace /absolute/project');
  const resolved = await fsp.realpath(path.resolve(input.startsWith('~/') ? path.join(home, input.slice(2)) : input));
  const info = await fsp.stat(resolved);
  const canonicalHome = await fsp.realpath(home);
  if (!info.isDirectory() || resolved === '/' || resolved === canonicalHome) {
    throw new Error('Choose a project directory, not the home directory or filesystem root.');
  }
  return resolved;
}

async function probePath(workspace, requested) {
  workspace = await fsp.realpath(workspace);
  const candidates = requested ? [requested] :
    ['README.md', 'README', 'AGENTS.md', 'package.json', 'pyproject.toml', 'LICENSE'];
  for (const candidate of candidates) {
    if (path.isAbsolute(candidate) || candidate.includes('\\') ||
        candidate.split('/').some((part) => !part || part === '.' || part === '..')) {
      if (requested) throw new Error('Probe must be a relative file inside the workspace.');
      continue;
    }
    const selected = path.resolve(workspace, candidate);
    if (!inside(workspace, selected)) continue;
    try {
      if ((await fsp.lstat(selected)).isFile() &&
          inside(workspace, await fsp.realpath(selected))) return candidate;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  throw new Error('No safe readable probe file. Supply --probe README.md (an existing file).');
}

async function prompt(rl, question, initial = '') {
  const answer = (await rl.question(question)).trim();
  return answer || initial;
}

async function setup(args, environment) {
  const { home, config, state, input, output, manager, verify, sleep } = environment;
  const opts = optionsOf(args);
  const interactive = Boolean(input.isTTY && output.isTTY);
  const rl = interactive ? readline.createInterface({ input, output }) : null;
  try {
    if (!opts.workspace && !interactive) {
      throw new Error('Non-interactive setup requires --workspace /absolute/project');
    }
    const chosen = opts.workspace || await prompt(rl, 'Existing project directory: ');
    const workspace = await workspacePath(chosen, home);
    let probe;
    try {
      probe = await probePath(workspace, opts.probe);
    } catch (error) {
      if (!interactive || opts.probe || !error.message.startsWith('No safe readable probe')) throw error;
      probe = await probePath(workspace, await prompt(rl,
        'Existing safe file relative to that directory (e.g. README.md): '));
    }
    const commands = opts.commands || (interactive && (await prompt(rl,
      'Allow OM to execute commands on your machine? [y/N] ')).toLowerCase() === 'y');
    const writes = opts.writes || (interactive && (await prompt(rl,
      'Allow OM to edit configured workspace files? [y/N] ')).toLowerCase() === 'y');
    const runtime = await verify(PACKAGE_ROOT, home);
    let status = await manager(runtime, config, state, 'status');
    if (status.status !== 'not_installed') {
      if (['ready', 'stopped'].includes(status.status)) {
        output.write('OM already installed (' + status.status + ').\n');
        return status;
      }
      throw new Error('Existing OM state needs review: ' + status.status);
    }
    const initialize = ['--workspace', workspace];
    if (commands) initialize.push('--enable-commands');
    if (writes) initialize.push('--enable-workspace-writes');
    if (fs.existsSync(config)) initialize.push('--verify-existing');
    const configured = await manager(runtime, config, state, 'configure', initialize);
    if (configured.status !== 'configured') throw new Error('Configuration was not confirmed.');
    const marker = path.join(path.dirname(config), 'npm-install.json');
    const wanted = { format: 1, runtimeSha256: runtime.sha256, workspace, probe };
    await fsp.mkdir(path.dirname(config), { recursive: true, mode: 0o700 });
    let operationId;
    try {
      const current = JSON.parse(await fsp.readFile(marker, 'utf8'));
      if (current.format !== 1 || current.runtimeSha256 !== wanted.runtimeSha256 ||
          current.workspace !== workspace || current.probe !== probe ||
          !/^[a-f0-9]{32}$/.test(current.operationId)) {
        throw new Error('Earlier setup differs. Inspect ' + marker + ' before proceeding.');
      }
      operationId = current.operationId;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      operationId = crypto.randomBytes(16).toString('hex');
      const handle = await fsp.open(marker, 'wx', 0o600);
      try {
        await handle.writeFile(JSON.stringify({ ...wanted, operationId }) + '\n');
        await handle.sync();
      } finally {
        await handle.close();
      }
    }
    // Re-submitting the SAME ID returns its original receipt; never allocate a
    // replacement after a lost acknowledgement.
    let result = await manager(runtime, config, state, 'install', [
      '--operation-id', operationId, '--payload', runtime.root,
      '--sha256', runtime.sha256, '--probe-workspace', 'project', '--probe-path', probe
    ]);
    const deadline = Date.now() + 8 * 60 * 1000;
    while (result.status === 'accepted' || result.status === 'busy') {
      if (Date.now() >= deadline) {
        throw new Error('Install still running. Re-run setup to read operation ' + operationId + '.');
      }
      await sleep(1500);
      result = await manager(runtime, config, state, 'operation', ['--operation-id', operationId]);
    }
    if (result.status !== 'completed' || result.operation?.phase !== 'completed') {
      throw new Error('Installation needs review (' + result.status + '), operation ' + operationId + '. No retry was started.');
    }
    status = await manager(runtime, config, state, 'start');
    if (status.status !== 'ready') {
      throw new Error('Core installed but not ready (' + status.status + '). Check openmachine status.');
    }
    output.write('Open Machine Core is ready. Commands and workspace writes: ' +
      (commands ? 'commands allowed' : 'commands disabled') + ', ' +
      (writes ? 'edits allowed' : 'edits disabled') + '.\n');
    output.write('ChatGPT connection is a separate step.\n');
    return status;
  } finally {
    rl?.close();
  }
}

async function run(argv, overrides = {}) {
  const output = overrides.output || process.stdout;
  const input = overrides.input || process.stdin;
  const home = overrides.home || os.homedir();
  const config = path.join(home, '.config/openmachine/config.toml');
  const state = path.join(home, '.local/state/openmachine');
  const command = argv[0] || '';
  if (command === '--help' || command === '-h' || command === 'help') {
    output.write(HELP);
    return;
  }
  if (command === '--version' || command === '-V') {
    output.write('Open Machine npm installer ' + PKG.version + '\n');
    return;
  }
  if (!['', 'setup', 'status', 'start', 'stop', 'doctor', 'core'].includes(command)) {
    throw new Error('Unknown command: ' + command + '\n' + HELP);
  }
  if (!['', 'setup', 'core'].includes(command) && argv.length !== 1) {
    throw new Error('Unexpected arguments for ' + command);
  }
  if ((overrides.platform || process.platform) !== 'darwin' ||
      (overrides.arch || process.arch) !== 'arm64') {
    throw new Error('This package supports Apple Silicon macOS only.');
  }
  const verify = overrides.verify || prepareRuntime;
  const manager = overrides.manager || management;
  const sleep = overrides.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const environment = { home, config, state, input, output, manager, verify, sleep };
  if (command === 'setup') return setup(argv.slice(1), environment);
  const runtime = await verify(PACKAGE_ROOT, home);
  if (command === '') {
    const result = await manager(runtime, config, state, 'status');
    if (result.status === 'not_installed') return setup([], environment);
    output.write('Open Machine: ' + result.status + '\n');
    return result;
  }
  if (command === 'core') {
    const result = spawnSync(path.join(runtime.root, 'python/bin/python3'),
      ['-I', '-B', '-m', 'openmachine.cli', ...argv.slice(1)],
      { stdio: 'inherit' });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error('Core exited with status ' + result.status);
    return;
  }
  const action = command === 'doctor' ? 'status' : command;
  const result = await manager(runtime, config, state, action);
  output.write(JSON.stringify(result, null, 2) + '\n');
  if (command === 'doctor' && result.status !== 'ready') {
    throw new Error('Core is not ready: ' + result.status);
  }
  return result;
}

module.exports = { run, setup, optionsOf, workspacePath, probePath, management, HELP };
