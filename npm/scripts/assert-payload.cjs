'use strict';

const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { prepareRuntime } = require('../lib/runtime.cjs');
const { verifyReleaseSources } = require('./verify-release-sources.cjs');

(async () => {
  const scratch = await fs.mkdtemp(path.join(os.tmpdir(), 'om-npm-verify-'));
  try {
    await verifyReleaseSources(path.resolve(__dirname, '..'));
    const { manifest } = await prepareRuntime(path.resolve(__dirname, '..'), scratch);
    if (manifest.core?.version !== require('../package.json').version) {
      throw new Error('npm installer version does not match bundled Core version.');
    }
    process.stdout.write('Verified bundled Open Machine Core ' + manifest.core.version + '\n');
  } finally {
    await fs.rm(scratch, { recursive: true, force: true });
  }
})().catch((error) => {
  process.stderr.write('Refusing unverified release package: ' + error.message + '\n');
  process.exitCode = 1;
});
