import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';

export function checkStorage(exitCode, output, report) {
  const fields = ['requested_bytes', 'total_bytes', 'current_used_bytes', 'peak_used_bytes', 'samples'];
  if (report.valid !== true || report.error || fields.some(key => !Number.isSafeInteger(report[key]) || report[key] < 0) ||
      report.requested_bytes === 0 || report.samples === 0 || report.total_bytes !== report.requested_bytes ||
      report.current_used_bytes > report.peak_used_bytes || report.peak_used_bytes > report.requested_bytes) {
    throw new Error('Invalid BuildKit storage measurement or capacity exceeded');
  }
  if (exitCode === 0) return;
  const failures = output.split('\n').filter(line => line.startsWith('Storage efficiency check failed:'));
  const expected = `Storage efficiency check failed: BuildKit peak usage is at or below 80% of requested capacity (${report.peak_used_bytes} of ${report.requested_bytes} bytes)`;
  if (exitCode !== 1 || failures.length !== 1 || failures[0] !== expected ||
      5n * BigInt(report.peak_used_bytes) > 4n * BigInt(report.requested_bytes)) {
    throw new Error('BuildKit storage collector failed');
  }
  throw new Error(failures[0]);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [, , code, log, report] = process.argv;
    const warning = checkStorage(Number(code), readFileSync(log, 'utf8'), JSON.parse(readFileSync(report, 'utf8')));
    if (warning) console.log(`::warning::${warning}`);
  } catch (error) {
    console.error(`::error::${error.message}`);
    process.exitCode = 1;
  }
}
