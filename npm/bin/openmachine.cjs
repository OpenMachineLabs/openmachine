#!/usr/bin/env node
'use strict';

require('../lib/cli.cjs').run(process.argv.slice(2)).catch((error) => {
  process.stderr.write('openmachine: ' + error.message + '\n');
  process.exitCode = 1;
});
