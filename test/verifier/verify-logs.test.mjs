// scripts/verify-logs.mjs against the shared vectors: node --test test/verifier/verify-logs.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { verify } from '../../scripts/verify-logs.mjs';

const vectors = JSON.parse(readFileSync(new URL('../vectors/logs.json', import.meta.url), 'utf8'));
// A machine's record as an export holds it: its fields, in their order, and its signature.
const stored = (line, seq = 1, prev = '0'.repeat(64)) => {
  const { sig, ...signed } = JSON.parse(line);
  return { stream: 'activity', seq, prev, key: vectors.key, signed, sig };
};

test("Cloud's chain verifies; a changed line, a reordered one or a gap is found", async () => {
  const [first, second, third] = vectors.chain;
  assert.equal((await verify(vectors.chain.join('\n')))[0], 'OK 3 records, 0 gaps');
  await assert.rejects(verify([first, second.replace('Singapore', 'Singapura'), third].join('\n')),
    /line 3: prev is not the hash of the line before/);
  await assert.rejects(verify([second, first].join('\n')), /line 2: seq does not rise/);
  assert.equal((await verify([first, third].join('\n')))[0], 'OK 2 records, 1 gaps');
  assert.equal((await verify(JSON.stringify({ raw_data: first })))[0], 'OK 1 records, 0 gaps');
  await assert.rejects(verify('{}'), /line 1: not a record/);
});

test("every machine record of the vectors verifies, and one changed field breaks its signature", async () => {
  for (const { line } of vectors.records) {
    assert.equal((await verify(JSON.stringify(stored(line))))[0], 'OK 1 records, 0 gaps');
    const changed = stored(line);
    changed.signed.at = '2000-01-01T00:00:00.000Z';
    await assert.rejects(verify(JSON.stringify(changed)), /line 1: signature does not verify/);
  }
});
