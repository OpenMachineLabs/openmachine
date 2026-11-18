#!/usr/bin/env node
// Verifies an export of Open Machine logs, or OCSF objects that carry its lines as `raw_data`: each stream's
// chain and each machine record's signature. Node 24, no dependency.
//   node scripts/verify-logs.mjs <file>

const encoder = new TextEncoder();
const hex = async text => [...new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(text)))].map(byte => byte.toString(16).padStart(2, '0')).join('');
const decode = text => Uint8Array.from(atob(text.replaceAll('-', '+').replaceAll('_', '/')), character => character.charCodeAt(0));

/**
 * The report of a file's lines: `OK <n> records, <g> gaps`, then each stream's first `prev` and last line's hash, to
 * match against another copy. Throws the first failure with its line number. An export is filtered, so a step of
 * more than one in `seq` is a gap, counted, not a failure.
 */
export async function verify(text) {
  const streams = new Map();
  let records = 0, gaps = 0, number = 0;
  for (let line of text.split('\n')) {
    number++;
    if (!line.trim()) continue;
    const fail = why => { throw new Error(`line ${number}: ${why}`); };
    let record;
    try {
      record = JSON.parse(line);
      if (typeof record.raw_data === 'string') record = JSON.parse(line = record.raw_data);
    } catch { fail('not a record'); }
    if (typeof record?.stream !== 'string' || !Number.isSafeInteger(record.seq) || typeof record.prev !== 'string') fail('not a record');
    const last = streams.get(record.stream);
    if (last && record.seq <= last.seq) fail('seq does not rise');
    if (last && record.seq === last.seq + 1 && record.prev !== last.hash) fail('prev is not the hash of the line before');
    if (last && record.seq > last.seq + 1) gaps++;
    if (record.signed !== undefined || record.sig !== undefined) {
      // A machine's record: Ed25519 over its signed fields, in their order.
      const message = 'om-log-v1\n' + Object.entries(record.signed ?? {}).map(([name, value]) => `${name}:${encoder.encode(String(value)).length}:${value}\n`).join('');
      const valid = await (async () => crypto.subtle.verify('Ed25519', await crypto.subtle.importKey('raw', decode(record.key), 'Ed25519', false, ['verify']),
        decode(record.sig), encoder.encode(message)))().catch(() => false);
      if (!valid) fail('signature does not verify');
    }
    streams.set(record.stream, { first: last?.first ?? record.prev, seq: record.seq, hash: await hex(line) });
    records++;
  }
  return [`OK ${records} records, ${gaps} gaps`, ...[...streams].map(([stream, { first, hash }]) => `${stream}: first prev ${first}, last ${hash}`)];
}

// Run as the script, not imported; `import.meta.main` is missing before Node 24.2 and would verify nothing.
if (globalThis.process?.argv[1]?.endsWith('verify-logs.mjs')) {
  const { readFileSync } = await import('node:fs');
  try {
    console.log((await verify(readFileSync(process.argv[2], 'utf8'))).join('\n'));
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
