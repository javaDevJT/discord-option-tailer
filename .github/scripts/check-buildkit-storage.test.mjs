import test from 'node:test';
import assert from 'node:assert/strict';
import {checkStorage} from './check-buildkit-storage.mjs';

test('only valid underutilization becomes advisory; telemetry and capacity failures remain fatal', () => {
  const report = {valid: true, requested_bytes: 100, total_bytes: 100, current_used_bytes: 10, peak_used_bytes: 20, samples: 2};
  const message = peak => `Storage efficiency check failed: BuildKit peak usage is at or below 80% of requested capacity (${peak} of 100 bytes)`;
  assert.match(checkStorage(1, message(20), report), /Low BuildKit/);
  assert.match(checkStorage(1, message(80), {...report, peak_used_bytes: 80}), /Low BuildKit/);
  assert.equal(checkStorage(0, '', {...report, peak_used_bytes: 99}), undefined);
  for (const invalid of [{valid: false}, {samples: 0}, {total_bytes: 99}, {current_used_bytes: 21}, {peak_used_bytes: 101}, {error: 'collector failed'}, {requested_bytes: null}]) {
    assert.throws(() => checkStorage(1, message(20), {...report, ...invalid}));
  }
  for (const [code, output] of [[2, message(20)], [1, ''], [1, 'unknown collector failure'], [1, `${message(20)}\nStorage efficiency check failed: unknown`]]) {
    assert.throws(() => checkStorage(code, output, report));
  }
  assert.throws(() => checkStorage(1, message(81), {...report, peak_used_bytes: 81}));
});
