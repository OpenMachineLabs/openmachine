'use strict';

const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { prepareRuntime } = require('../lib/runtime.cjs');

(async () => {
  const scratch = await fs.mkdtemp(path.join(os.tmpdir(), 'om-npm-verify-'));
  try {
    const { manifest } = await prepareRuntime(path.resolve(__dirname, '..'), scratch);
    process.stdout.write('Verified bundled Open Machine Core ' + manifest.core.version + '\n');
  } finally {
    await fs.rm(scratch, { recursive: true, force: true });
  }
})().catch((error) => {
  process.stderr.write('Refusing unverified release package: ' + error.message + '\n');
  process.exitCode = 1;
});
