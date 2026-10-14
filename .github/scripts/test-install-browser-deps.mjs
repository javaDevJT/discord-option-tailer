import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  buildInstallPlan,
  detectHostPlatform,
  installBrowserDependencies,
  parseDependencyDryRun,
} from './install-browser-deps.mjs';

// Representative inputs for deterministic tests; production asks the installed Python Playwright CLI.
const PLAYWRIGHT_DEPS = Object.fromEntries(['ubuntu24.04-x64', 'debian13-arm64'].map((host) => [host, {
  chromium: ['libatk1.0-0t64', 'fonts-liberation', 'fonts-noto-color-emoji'],
  tools: ['xvfb'],
}]));

test('parses only bounded CLI package plans and rejects unsupported fallback hosts', () => {
  assert.deepEqual(parseDependencyDryRun("sudo -- sh -c 'apt-get update && apt-get install -y libatk1.0-0t64 xvfb fonts-liberation'"),
    ['fonts-liberation', 'libatk1.0-0t64']);
  for (const output of ["apt-get install -y --unsafe", 'no package plan',
    'BEWARE: unsupported platform\napt-get install -y libc6',
    'apt-get install -y libatk1.0-0t64\napt-get install -y libc6']) {
    assert.throws(() => parseDependencyDryRun(output));
  }
  assert.equal(detectHostPlatform('ID=ubuntu\nVERSION_ID="24.04"\n', 'x64'), 'ubuntu24.04-x64');
  assert.equal(detectHostPlatform('ID=debian\nVERSION_ID="13"\n', 'arm64'), 'debian13-arm64');
  assert.throws(() => detectHostPlatform('ID=unknown\nVERSION_ID=1', 'x64'));
});

function makeRunnerTemp(t) {
  const path = mkdtempSync(join(realpathSync(tmpdir()), 'discord-browser-deps-test-'));
  t.after(() => rmSync(path, { recursive: true, force: true }));
  const githubEnv = join(path, 'github-env');
  writeFileSync(githubEnv, 'EXISTING=value\n');
  return { path, githubEnv };
}

function fakeExecutor(calls, { failCommand, failWhen, contents } = {}) {
  return (command, args, context) => {
    calls.push({ command, args, context });
    if (command === failCommand || failWhen?.(command, args)) throw new Error(`simulated ${command} failure`);
    if (command === 'apt-get' && args.includes('install')) {
      writeFileSync(join(context.paths.archives, 'libatk1.0-0_1.0_amd64.deb'), 'fake deb');
    }
    if (command === 'dpkg-deb' && args[0] === '--contents') return contents ?? '-rw-r--r-- root/root 123 ./usr/lib/x86_64-linux-gnu/libatk-1.0.so.0\n';
    if (command === 'dpkg-deb' && args[0] === '--extract') {
      const extractedFile = join(args[2], 'usr/lib/x86_64-linux-gnu/libatk-1.0.so.0');
      mkdirSync(join(args[2], 'usr/lib/x86_64-linux-gnu'), { recursive: true });
      writeFileSync(extractedFile, 'fake library');
    }
    return '';
  };
}

test('builds plans from supplied Playwright Chromium metadata without Xvfb', () => {
  const ubuntu = buildInstallPlan({
    platform: 'linux',
    hostPlatform: 'ubuntu24.04-x64',
    officiallySupported: true,
    dependencies: PLAYWRIGHT_DEPS,
  });
  assert.ok(ubuntu.packages.includes('libatk1.0-0t64'));
  assert.ok(ubuntu.packages.includes('fonts-liberation'));
  assert.ok(ubuntu.packages.includes('fonts-noto-color-emoji'));
  assert.ok(!ubuntu.packages.includes('xvfb'));
  assert.equal(new Set(ubuntu.packages).size, ubuntu.packages.length);

  const debianArm = buildInstallPlan({
    platform: 'linux',
    hostPlatform: 'debian13-arm64',
    officiallySupported: true,
    dependencies: PLAYWRIGHT_DEPS,
  });
  assert.ok(debianArm.packages.includes('libatk1.0-0t64'));
  assert.equal(debianArm.libraryArchitecture, 'aarch64-linux-gnu');
});

test('queries installed Python Playwright and stages dependencies without root', (t) => {
  const { path, githubEnv } = makeRunnerTemp(t);
  const calls = [];
  const executor = (command, args, context) => {
    calls.push({ command, args, context });
    if (command === 'python') {
      return 'sudo apt-get update && sudo apt-get install -y --no-install-recommends libatk1.0-0 xvfb';
    }
    if (command === 'apt-get' && args.includes('install')) {
      writeFileSync(join(context.paths.archives, 'libatk1.0-0_1.0_amd64.deb'), 'fake deb');
    }
    if (command === 'dpkg-deb' && args[0] === '--contents') {
      return '-rw-r--r-- root/root 123 ./usr/lib/x86_64-linux-gnu/libatk-1.0.so.0\\n';
    }
    if (command === 'dpkg-deb' && args[0] === '--extract') {
      const extractedFile = join(args[2], 'usr/lib/x86_64-linux-gnu/libatk-1.0.so.0');
      mkdirSync(join(args[2], 'usr/lib/x86_64-linux-gnu'), { recursive: true });
      writeFileSync(extractedFile, 'fake library');
    }
    return '';
  };
  const result = installBrowserDependencies({
    env: { RUNNER_TEMP: path, GITHUB_ENV: githubEnv, LD_LIBRARY_PATH: '/existing/lib' },
    platform: 'linux',
    hostPlatform: 'ubuntu24.04-x64',
    executor,
    uid: 1000,
    effectiveUid: 1000,
  });
  const probe = calls.find(({ command }) => command === 'python');
  assert.deepEqual(probe.args, ['-m', 'playwright', 'install-deps', '--dry-run', 'chromium']);
  assert.equal(probe.context.captureOutput, true);
  assert.ok(calls.every(({ command }) => command !== 'sudo'));
  assert.ok(result.packages.includes('fonts-liberation'));
  assert.ok(!result.packages.includes('xvfb'));
  assert.match(readFileSync(githubEnv, 'utf8'), /LD_LIBRARY_PATH=.*discord-playwright-deps-/);
  assert.match(readFileSync(githubEnv, 'utf8'), /FONTCONFIG_FILE=.*fonts\.conf/);
});

test('rejects unsupported hosts before running package-manager commands', () => {
  assert.throws(() => buildInstallPlan({
    platform: 'darwin',
    hostPlatform: 'mac15-arm64',
    officiallySupported: true,
    dependencies: PLAYWRIGHT_DEPS,
  }), /Linux/);
  assert.throws(() => buildInstallPlan({
    platform: 'linux',
    hostPlatform: 'ubuntu24.04-x64',
    officiallySupported: false,
    dependencies: PLAYWRIGHT_DEPS,
  }), /officially support/);
});

test('refuses root and unknown privilege state before running commands', () => {
  const calls = [];
  const common = {
    env: {},
    platform: 'linux',
    hostPlatform: 'ubuntu24.04-x64',
    officiallySupported: true,
    dependencies: PLAYWRIGHT_DEPS,
    executor: fakeExecutor(calls),
  };
  assert.throws(() => installBrowserDependencies({ ...common, uid: 0, effectiveUid: 0 }), /non-root/);
  assert.throws(() => installBrowserDependencies({ ...common, uid: null, effectiveUid: null }), /non-root/);
  assert.equal(calls.length, 0);
});

test('downloads signed APT packages and extracts them only into the private prefix', (t) => {
  const { path, githubEnv } = makeRunnerTemp(t);
  const calls = [];
  const result = installBrowserDependencies({
    env: { RUNNER_TEMP: path, GITHUB_ENV: githubEnv, LD_LIBRARY_PATH: '/existing/lib' },
    platform: 'linux',
    hostPlatform: 'ubuntu24.04-x64',
    officiallySupported: true,
    dependencies: PLAYWRIGHT_DEPS,
    executor: fakeExecutor(calls),
  });

  const aptCalls = calls.filter((call) => call.command === 'apt-get');
  assert.equal(aptCalls.length, 2);
  assert.ok(aptCalls[0].args.includes('update'));
  assert.ok(aptCalls[1].args.includes('install'));
  assert.ok(aptCalls[1].args.includes('--download-only'));
  assert.ok(aptCalls.every(({ args }) => args.includes('Debug::NoLocking=1')));
  assert.ok(aptCalls.every(({ args }) => args.includes('Dir::State::status=/var/lib/dpkg/status')));
  const temporaryAptPaths = aptCalls.flatMap(({ args }) => args.filter((arg) => /^(?:Dir::State::lists|Dir::State::extended_states|Dir::Cache::archives|Dir::Cache::pkgcache|Dir::Cache::srcpkgcache|Dir::Log)=/.test(arg)));
  assert.ok(temporaryAptPaths.length >= 12);
  assert.ok(temporaryAptPaths.every((arg) => arg.slice(arg.indexOf('=') + 1).replace(/\/$/, '').startsWith(`${aptCalls[0].context.paths.root}/`)));
  assert.ok(aptCalls.every(({ args }) => !args.some((arg) => /unauthenticated/i.test(arg))));
  assert.ok(aptCalls.every(({ args }) => !args.includes('--allow-unauthenticated')));
  assert.ok(aptCalls[1].args.includes('--no-install-recommends'));
  assert.ok(!calls.some(({ command, args }) => command === 'sudo' || (command === 'dpkg' && args.includes('--install'))));
  assert.ok(calls.filter(({ command }) => command === 'dpkg-deb').every(({ args }) => args[0] === '--contents' || args[0] === '--extract'));
  assert.ok(calls.filter(({ command }) => command === 'dpkg-deb').every(({ context }) => context.paths.prefix.startsWith(join(path, 'discord-playwright-deps-'))));

  const envFile = readFileSync(githubEnv, 'utf8');
  assert.ok(envFile.startsWith('EXISTING=value\n'));
  assert.ok(envFile.includes(`LD_LIBRARY_PATH=${result.libraryPath}\n`));
  assert.ok(envFile.split('\n').find((line) => line.startsWith('LD_LIBRARY_PATH=')).endsWith('/existing/lib'));
  assert.ok(envFile.includes(`FONTCONFIG_FILE=${result.fontConfiguration}\n`));
  assert.ok(readdirSync(path).includes(result.stagingRoot.split('/').at(-1)));
});

test('APT failure stops before extraction and leaves GITHUB_ENV untouched', (t) => {
  const { path, githubEnv } = makeRunnerTemp(t);
  const before = readFileSync(githubEnv, 'utf8');
  const calls = [];
  assert.throws(() => installBrowserDependencies({
    env: { RUNNER_TEMP: path, GITHUB_ENV: githubEnv },
    platform: 'linux',
    hostPlatform: 'ubuntu24.04-x64',
    officiallySupported: true,
    dependencies: PLAYWRIGHT_DEPS,
    executor: fakeExecutor(calls, { failWhen: (command, args) => command === 'apt-get' && args.includes('install') }),
  }), /simulated apt-get failure/);
  assert.equal(readFileSync(githubEnv, 'utf8'), before);
  assert.equal(calls.filter(({ command }) => command === 'apt-get').length, 2);
  assert.ok(!calls.some(({ command }) => command === 'dpkg-deb'));
  assert.deepEqual(readdirSync(path), ['github-env']);
});

test('refuses packages containing the system libc loader before extracting anything', (t) => {
  const { path, githubEnv } = makeRunnerTemp(t);
  const before = readFileSync(githubEnv, 'utf8');
  const calls = [];
  assert.throws(() => installBrowserDependencies({
    env: { RUNNER_TEMP: path, GITHUB_ENV: githubEnv },
    platform: 'linux',
    hostPlatform: 'ubuntu24.04-x64',
    officiallySupported: true,
    dependencies: PLAYWRIGHT_DEPS,
    executor: fakeExecutor(calls, {
      contents: 'lrwxrwxrwx root/root 0 ./lib/x86_64-linux-gnu/libc.so.6 -> libc-2.39.so\n',
    }),
  }), /protected system library/);
  assert.equal(readFileSync(githubEnv, 'utf8'), before);
  assert.ok(!calls.some(({ command, args }) => command === 'dpkg-deb' && args[0] === '--extract'));
});

test('requires absolute runner paths and keeps GITHUB_ENV inside RUNNER_TEMP', (t) => {
  const { path, githubEnv } = makeRunnerTemp(t);
  const calls = [];
  const common = {
    platform: 'linux',
    hostPlatform: 'ubuntu24.04-x64',
    officiallySupported: true,
    dependencies: PLAYWRIGHT_DEPS,
    executor: fakeExecutor(calls),
  };
  assert.throws(() => installBrowserDependencies({
    ...common,
    env: { RUNNER_TEMP: 'relative', GITHUB_ENV: githubEnv },
  }), /RUNNER_TEMP.*absolute/);
  assert.throws(() => installBrowserDependencies({
    ...common,
    env: { RUNNER_TEMP: path, GITHUB_ENV: join(tmpdir(), 'outside-github-env') },
  }), /inside RUNNER_TEMP/);
  assert.equal(calls.length, 0);
});

