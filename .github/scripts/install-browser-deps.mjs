#!/usr/bin/env node

import { appendFileSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const PYTHON_EXECUTABLE = 'python';
const STAGING_PREFIX = 'discord-playwright-deps-';
const PACKAGE_NAME = /^[a-z0-9][a-z0-9+.-]*$/;
const PROTECTED_SYSTEM_LIBRARY = /^(?:libc(?:-[0-9][^/]*)?\.so(?:\.[^/]*)?|ld(?:-linux)?[^/]*\.so(?:\.[^/]*)?|lib(?:pthread|dl|m|rt|resolv|mvec|thread_db)\.so(?:\.[^/]*)?|libnss_[^/]+\.so(?:\.[^/]*)?)$/;

export function parseDependencyDryRun(output) {
  if (typeof output !== 'string' || output.length > 1024 * 1024
      || /not officially supported|unsupported.*platform|BEWARE/i.test(output)) {
    throw new Error('Playwright did not return a supported, bounded dependency plan.');
  }
  const report = output.trim();
  if (report === 'All system dependencies installed.') return [];
  if (report.startsWith('Missing system dependencies')) {
    const match = /^Missing system dependencies \(([1-9][0-9]*)\):\r?\n((?:[ \t]+[a-z0-9][a-z0-9+.-]*\r?\n?)+)$/.exec(report);
    if (!match) throw new Error('Invalid Playwright missing-dependency report.');
    const packages = match[2].trim().split(/\s+/);
    if (packages.length !== Number(match[1]) || new Set(packages).size !== packages.length) {
      throw new Error('Playwright dependency count does not match its package report.');
    }
    return packages.filter((name) => name !== 'xvfb').sort();
  }
  const matches = [...output.matchAll(/apt-get\s+install\s+-y(?:\s+--no-install-recommends)?\s+([^'"\r\n;&|]+)/g)];
  if (matches.length !== 1) throw new Error('Expected one Playwright APT dry-run package plan.');
  const packages = matches[0][1].trim().split(/\s+/);
  if (!packages.length || packages.some((name) => !PACKAGE_NAME.test(name))) {
    throw new Error('Playwright returned invalid dependency package tokens.');
  }
  return [...new Set(packages.filter((name) => name !== 'xvfb'))].sort();
}

export function acceptsDependencyProbeExit(status, output) {
  if (status !== 1 || typeof output !== 'string'
    || !output.trim().startsWith('Missing system dependencies')) return false;
  try {
    parseDependencyDryRun(output);
    return true;
  } catch {
    return false;
  }
}

export function detectHostPlatform(osRelease, architecture) {
  if (!['x64', 'arm64'].includes(architecture)) throw new Error('Unsupported runner architecture.');
  const field = (name) => new RegExp(`^${name}=["']?([^"'\\r\\n]+)["']?$`, 'm').exec(osRelease)?.[1];
  const id = field('ID');
  const version = field('VERSION_ID');
  if (id === 'ubuntu' && /^\d{2}\.\d{2}$/.test(version ?? '')) return `ubuntu${version}-${architecture}`;
  if (id === 'debian' && /^\d+$/.test(version ?? '')) return `debian${version}-${architecture}`;
  throw new Error('Browser dependencies require an identified Ubuntu or Debian host.');
}

export function buildInstallPlan({
  platform,
  hostPlatform,
  officiallySupported,
  dependencies,
  dryRunOutput,
}) {
  if (platform !== 'linux') throw new Error('Browser dependency staging is supported only on Linux.');
  if (!officiallySupported) throw new Error(`Playwright does not officially support host platform ${hostPlatform}.`);

  const platformMatch = /^(?:ubuntu\d{2}\.\d{2}|debian\d+)-(x64|arm64)$/.exec(hostPlatform ?? '');
  if (!platformMatch) throw new Error(`Unsupported apt-based Playwright host platform: ${hostPlatform ?? '<unknown>'}.`);

  const platformDependencies = dependencies?.[hostPlatform];
  if (dependencies && (!platformDependencies || !Array.isArray(platformDependencies.chromium) || !Array.isArray(platformDependencies.tools))) {
    throw new Error(`Playwright has no Chromium dependency metadata for ${hostPlatform}.`);
  }

  const packages = dependencies ? [...new Set([
    ...platformDependencies.chromium,
    ...platformDependencies.tools.filter((name) => name !== 'xvfb'),
  ])].sort() : parseDependencyDryRun(dryRunOutput);
  if (packages.some((name) => !PACKAGE_NAME.test(name))) {
    throw new Error(`Playwright returned an invalid Chromium package plan for ${hostPlatform}.`);
  }

  return {
    hostPlatform,
    packages,
    libraryArchitecture: platformMatch[1] === 'x64' ? 'x86_64-linux-gnu' : 'aarch64-linux-gnu',
  };
}

export function buildAptConfiguration(paths) {
  const settings = {
    'Dir::State::status': '/var/lib/dpkg/status',
    'Dir::State::lists': paths.lists + sep,
    'Dir::State::extended_states': paths.extendedStates,
    'Dir::Cache::archives': paths.archives + sep,
    'Dir::Cache::pkgcache': paths.pkgCache,
    'Dir::Cache::srcpkgcache': paths.srcpkgCache,
    'Dir::Log': paths.logs + sep,
    'Debug::NoLocking': '1',
  };
  return Object.entries(settings).map(([key, value]) => `${key} ${JSON.stringify(value)};`).join('\n') + '\n';
}

export function buildFontConfiguration(prefix, cache) {
  const xml = (value) => value.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;');
  return '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
    + '<fontconfig>\n'
    + `  <dir>${xml(join(prefix, 'usr', 'share', 'fonts'))}</dir>\n`
    + '  <dir>/usr/share/fonts</dir>\n'
    + `  <cachedir>${xml(cache)}</cachedir>\n`
    + '  <alias><family>sans-serif</family><prefer><family>Liberation Sans</family></prefer></alias>\n'
    + '</fontconfig>\n';
}

export function buildAptArguments(operation, paths, packages = []) {
  const aptOptions = [
    '-o', `Dir::State::status=/var/lib/dpkg/status`,
    '-o', `Dir::State::lists=${paths.lists}${sep}`,
    '-o', `Dir::State::extended_states=${paths.extendedStates}`,
    '-o', `Dir::Cache::archives=${paths.archives}${sep}`,
    '-o', `Dir::Cache::pkgcache=${paths.pkgCache}`,
    '-o', `Dir::Cache::srcpkgcache=${paths.srcpkgCache}`,
    '-o', `Dir::Log=${paths.logs}${sep}`,
    '-o', 'Debug::NoLocking=1',
  ];

  if (operation === 'update') return [...aptOptions, 'update'];
  if (operation === 'download') {
    if (packages.length === 0 || packages.some((name) => !PACKAGE_NAME.test(name))) {
      throw new Error('Refusing an empty or invalid APT package plan.');
    }
    return [...aptOptions, '--download-only', '--reinstall', '--yes', '--no-install-recommends', 'install', ...packages];
  }
  throw new Error(`Unsupported APT operation: ${operation}.`);
}

function isWithin(base, target) {
  const pathFromBase = relative(base, target);
  return pathFromBase !== '' && pathFromBase !== '..' && !pathFromBase.startsWith(`..${sep}`) && !isAbsolute(pathFromBase);
}

function validateRunnerTemp(runnerTemp) {
  if (typeof runnerTemp !== 'string' || !isAbsolute(runnerTemp)) {
    throw new Error('RUNNER_TEMP must be an absolute RUNNER_TEMP path.');
  }
  if (/[\r\n:]/.test(runnerTemp)) throw new Error('RUNNER_TEMP contains a character unsafe for environment export.');
  const absolute = resolve(runnerTemp);
  const info = lstatSync(absolute);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('RUNNER_TEMP must be an existing, non-symlink directory.');
  const canonical = realpathSync(absolute);
  if (/[\r\n:]/.test(canonical)) throw new Error('RUNNER_TEMP contains a character unsafe for environment export.');
  if (canonical === resolve(canonical, '..')) throw new Error('Refusing to use the filesystem root as RUNNER_TEMP.');
  return canonical;
}

function validateGithubEnv(githubEnv, runnerTemp) {
  if (typeof githubEnv !== 'string' || !isAbsolute(githubEnv)) throw new Error('GITHUB_ENV must be an absolute path.');
  if (/[\r\n]/.test(githubEnv)) throw new Error('GITHUB_ENV contains a newline.');
  const absolute = resolve(githubEnv);
  const parent = realpathSync(dirname(absolute));
  const candidate = join(parent, absolute.slice(dirname(absolute).length + 1));
  if (!isWithin(runnerTemp, candidate)) throw new Error('GITHUB_ENV must be inside RUNNER_TEMP.');
  const info = lstatSync(absolute);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error('GITHUB_ENV must be an existing, regular file.');
  const canonical = realpathSync(absolute);
  if (!isWithin(runnerTemp, canonical)) throw new Error('GITHUB_ENV must be inside RUNNER_TEMP.');
  return canonical;
}

function makeStagingPaths(runnerTemp) {
  const root = mkdtempSync(join(runnerTemp, STAGING_PREFIX));
  const paths = {
    root,
    prefix: join(root, 'prefix'),
    temporary: join(root, 'tmp'),
    lists: join(root, 'apt', 'lists'),
    archives: join(root, 'apt', 'archives'),
    extendedStates: join(root, 'apt', 'extended_states'),
    pkgCache: join(root, 'apt', 'pkgcache.bin'),
    srcpkgCache: join(root, 'apt', 'srcpkgcache.bin'),
    logs: join(root, 'apt', 'logs'),
  };
  try {
    for (const directory of [paths.prefix, paths.temporary, join(paths.lists, 'partial'), join(paths.archives, 'partial'), paths.logs]) {
      mkdirSync(directory, { recursive: true });
    }
  } catch (error) {
    rmSync(root, { recursive: true, force: true });
    throw error;
  }
  return paths;
}

function runCommand(command, args, { cwd, captureOutput, paths }) {
  const result = spawnSync(command, args, {
    cwd,
    env: { ...process.env, TMPDIR: paths.temporary, TMP: paths.temporary, TEMP: paths.temporary,
      APT_CONFIG: join(paths.root, 'apt.conf') },
    encoding: 'utf8',
    stdio: captureOutput ? ['ignore', 'pipe', 'pipe'] : 'inherit',
  });
  if (result.error) throw result.error;
  const output = `${result.stdout ?? ''}${result.stderr ?? ''}`;
  const dependencyProbe = captureOutput && command === PYTHON_EXECUTABLE
    && args.length === 5 && args[0] === '-m' && args[1] === 'playwright'
    && args[2] === 'install-deps' && args[3] === '--dry-run' && args[4] === 'chromium';
  if (result.status !== 0 && !(dependencyProbe && acceptsDependencyProbeExit(result.status, output))) {
    const diagnostic = dependencyProbe ? output
      .replace(/https?:\/\/\S+/g, '[url]')
      .replace(/[A-Za-z0-9_=.-]{32,}/g, '[redacted]')
      .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '')
      .slice(0, 1200) : '';
    throw new Error(`${command} failed with exit code ${result.status ?? 'unknown'}.${diagnostic ? '\n' + diagnostic : ''}`);
  }
  return captureOutput ? output : '';
}

function invoke(executor, command, args, paths, captureOutput = false) {
  const result = executor(command, args, { cwd: paths.root, paths, captureOutput });
  if (captureOutput && typeof result !== 'string') throw new Error(`${command} did not return captured output.`);
  return result;
}

function packageArchives(archivesDirectory) {
  return readdirSync(archivesDirectory)
    .filter((name) => name.endsWith('.deb'))
    .map((name) => {
      const archive = join(archivesDirectory, name);
      const info = lstatSync(archive);
      if (!info.isFile() || info.isSymbolicLink()) throw new Error(`APT created a non-regular package archive: ${name}.`);
      return archive;
    })
    .sort();
}

function packageContainsProtectedLibrary(contents) {
  return contents.split(/\r?\n/).some((line) => {
    const entry = line.split(' -> ', 1)[0].trim().split(/\s+/).at(-1);
    if (!entry?.startsWith('./')) return false;
    const basename = entry.replace(/\/$/, '').split('/').at(-1);
    return PROTECTED_SYSTEM_LIBRARY.test(basename);
  });
}

export function buildLibraryPath(prefix, libraryArchitecture, existing = '') {
  if (/\r|\n/.test(existing)) throw new Error('LD_LIBRARY_PATH contains a newline and cannot be exported safely.');
  const stagedDirectories = [
    join(prefix, 'usr', 'lib', libraryArchitecture),
    join(prefix, 'lib', libraryArchitecture),
    join(prefix, 'usr', 'lib'),
    join(prefix, 'lib'),
  ];
  return `${stagedDirectories.join(':')}${existing ? `:${existing}` : ''}`;
}

export function installBrowserDependencies({
  env = process.env,
  platform = process.platform,
  hostPlatform,
  officiallySupported = true,
  dependencies,
  executor = runCommand,
  uid = process.getuid?.(),
  effectiveUid = process.geteuid?.(),
} = {}) {
  if (platform !== 'linux') throw new Error('Browser dependency staging is supported only on Linux.');
  if (!Number.isInteger(uid) || !Number.isInteger(effectiveUid) || uid === 0 || effectiveUid === 0) {
    throw new Error('Chromium dependencies must be staged by a confirmed non-root user.');
  }
  const runnerTemp = validateRunnerTemp(env.RUNNER_TEMP);
  const githubEnv = validateGithubEnv(env.GITHUB_ENV, runnerTemp);
  const paths = makeStagingPaths(runnerTemp);

  try {
    hostPlatform ??= detectHostPlatform(readFileSync('/etc/os-release', 'utf8'), process.arch);
    writeFileSync(join(paths.root, 'apt.conf'), buildAptConfiguration(paths), { flag: 'wx', mode: 0o600 });
    let metadataReady = false;
    let dryRunOutput;
    if (!dependencies) {
      invoke(executor, 'apt-get', buildAptArguments('update', paths), paths);
      metadataReady = true;
      dryRunOutput = invoke(executor, PYTHON_EXECUTABLE, ['-m', 'playwright', 'install-deps', '--dry-run', 'chromium'], paths, true);
    }
    const plan = buildInstallPlan({ platform, hostPlatform, officiallySupported, dependencies, dryRunOutput });
    const packages = [...new Set([...plan.packages, 'fonts-liberation'])].sort();
    const libraryPath = buildLibraryPath(paths.prefix, plan.libraryArchitecture, env.LD_LIBRARY_PATH ?? '');
    if (packages.length) {
      if (!metadataReady) invoke(executor, 'apt-get', buildAptArguments('update', paths), paths);
      invoke(executor, 'apt-get', buildAptArguments('download', paths, packages), paths);
    }

    const archives = packageArchives(paths.archives);
    const inspected = archives.map((archive) => ({
      archive,
      contents: invoke(executor, 'dpkg-deb', ['--contents', archive], paths, true),
    }));
    const unsafeArchive = inspected.find(({ contents }) => packageContainsProtectedLibrary(contents));
    if (unsafeArchive) throw new Error(`Refusing to stage a protected system library from ${unsafeArchive.archive}.`);

    for (const { archive } of inspected) {
      invoke(executor, 'dpkg-deb', ['--extract', archive, paths.prefix], paths);
    }

    const fontCache = join(paths.root, 'font-cache');
    mkdirSync(fontCache);
    const fontConfiguration = join(paths.root, 'fonts.conf');
    writeFileSync(fontConfiguration, buildFontConfiguration(paths.prefix, fontCache), { flag: 'wx', mode: 0o600 });
    appendFileSync(githubEnv, `LD_LIBRARY_PATH=${libraryPath}\nFONTCONFIG_FILE=${fontConfiguration}\nFONTCONFIG_PATH=${paths.root}\n`, { encoding: 'utf8' });
    return { stagingRoot: paths.root, prefix: paths.prefix, libraryPath, fontConfiguration, packages };
  } catch (error) {
    try {
      rmSync(paths.root, { recursive: true, force: true });
    } catch {
      // Preserve the original failure; the staging root is still confined to RUNNER_TEMP.
    }
    throw error;
  }
}

function isMainModule() {
  return process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}

if (isMainModule()) {
  try {
    const result = installBrowserDependencies();
    process.stdout.write(`Staged Chromium libraries under ${result.prefix}.\n`);
  } catch (error) {
    process.stderr.write(`Chromium dependency staging failed: ${error.message}\n`);
    process.exitCode = 1;
  }
}

