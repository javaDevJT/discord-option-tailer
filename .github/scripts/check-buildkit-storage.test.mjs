import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {checkStorage} from './check-buildkit-storage.mjs';

const report = overrides => ({
  valid: true,
  requested_bytes: 100,
  total_bytes: 100,
  current_used_bytes: 10,
  peak_used_bytes: 20,
  samples: 2,
  ...overrides,
});

const lowUsageFailure = peak => `Storage efficiency check failed: BuildKit peak usage is at or below 80% of requested capacity (${peak} of 100 bytes)`;

test('underutilization at or below 80% remains fatal', () => {
  for (const peak of [20, 79, 80]) {
    assert.throws(
      () => checkStorage(1, lowUsageFailure(peak), report({peak_used_bytes: peak})),
      /peak usage is at or below 80%/,
    );
  }
});

test('capacity above 80% passes only when the collector succeeds', () => {
  assert.equal(checkStorage(0, '', report({peak_used_bytes: 81})), undefined);
  assert.throws(
    () => checkStorage(1, lowUsageFailure(81), report({peak_used_bytes: 81})),
    /BuildKit storage collector failed/,
  );
});

test('missing, invalid, or inconsistent telemetry remains fatal', () => {
  const failures = [
    {valid: false},
    {samples: 0},
    {total_bytes: 99},
    {current_used_bytes: 21},
    {peak_used_bytes: 101},
    {error: 'collector failed'},
    {requested_bytes: null},
    {requested_bytes: undefined},
  ];
  for (const invalid of failures) {
    assert.throws(() => checkStorage(1, lowUsageFailure(20), report(invalid)));
  }

  for (const field of ['requested_bytes', 'total_bytes', 'current_used_bytes', 'peak_used_bytes', 'samples']) {
    const missing = report();
    delete missing[field];
    assert.throws(() => checkStorage(1, lowUsageFailure(20), missing));
  }
});

test('unexpected collector failures remain fatal', () => {
  const failure = lowUsageFailure(20);
  for (const [code, output] of [
    [2, failure],
    [1, ''],
    [1, 'unknown collector failure'],
    [1, `${failure}\nStorage efficiency check failed: unknown`],
  ]) {
    assert.throws(() => checkStorage(code, output, report()));
  }
});

test('the command exits nonzero for the exact 80% failure', () => {
  const directory = mkdtempSync(join(tmpdir(), 'check-buildkit-storage-'));
  const logPath = join(directory, 'finish.log');
  const reportPath = join(directory, 'usage.json');
  const scriptPath = fileURLToPath(new URL('./check-buildkit-storage.mjs', import.meta.url));
  try {
    writeFileSync(logPath, lowUsageFailure(80));
    writeFileSync(reportPath, JSON.stringify(report({peak_used_bytes: 80})));
    const result = spawnSync(process.execPath, [scriptPath, '1', logPath, reportPath], {encoding: 'utf8'});
    assert.equal(result.status, 1);
    assert.match(result.stderr, /::error::Storage efficiency check failed:/);
    assert.doesNotMatch(result.stdout, /::warning::/);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});
