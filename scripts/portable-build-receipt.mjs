#!/usr/bin/env node
import { createPrivateKey, sign } from 'node:crypto';
import { inflateRawSync } from 'node:zlib';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const API_VERSION = '2022-11-28';
const FETCH_TIMEOUT_MS = 30_000;
const SUPPORTED_SOURCE_EVENTS = new Set(['push', 'schedule', 'workflow_dispatch', 'workflow_run']);
const MAX_JSON_BYTES = 2 * 1024 * 1024;
const MAX_ZIP_BYTES = 32 * 1024 * 1024;
const MAX_LOG_BYTES = 64 * 1024 * 1024;
const MAX_LOG_MEMBERS = 256;
const MAX_MEMBER_BYTES = 16 * 1024 * 1024;
const SHA_RE = /^[a-f0-9]{40}$/i;
const DIGEST_RE = /^sha256:[a-f0-9]{64}$/i;
const UUID_RE = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;
const MARKER_RE = /^QB_BUILD_RECEIPT_V1 source_sha=([a-f0-9]{40}) run_id=([1-9][0-9]*) run_attempt=([1-9][0-9]*) digest=(sha256:[a-f0-9]{64})$/i;
const ANSI_RE = /\x1b\[[0-?]*[ -/]*[@-~]/g;
const DEFAULT_FETCH = globalThis.fetch;

export class ReceiptEvidenceError extends Error {}

function fail(message) {
  throw new ReceiptEvidenceError(message);
}

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requiredString(value, name, max = 512) {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim() || value.length > max) {
    fail(`${name} must be a non-empty, trimmed string.`);
  }
  return value;
}

function privateKeyPem(value) {
  if (typeof value !== 'string' || !value.trim() || value.length > 8192) fail('QB_BUILD_SIGNING_KEY must contain a PEM private key.');
  return value;
}

function positiveInteger(value, name) {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN;
  if (!Number.isSafeInteger(parsed) || parsed <= 0) fail(`${name} must be a positive safe integer.`);
  return parsed;
}

function repositoryParts(value) {
  const repository = requiredString(value, 'GITHUB_REPOSITORY', 250);
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) fail('GITHUB_REPOSITORY must be an owner/repository slug.');
  return { repository, owner: repository.split('/')[0], name: repository.split('/')[1] };
}

function workflowPath(value) {
  const path = requiredString(value, 'QB_TRUSTED_WORKFLOW_PATH', 300);
  if (!path.startsWith('.github/workflows/') || path.includes('..') || path.includes('\\') || path.startsWith('/')) {
    fail('QB_TRUSTED_WORKFLOW_PATH must be a repository-relative workflow path.');
  }
  return path;
}

function apiRoot(value) {
  const root = new URL(requiredString(value, 'GITHUB_API_URL', 2048));
  if (root.protocol !== 'https:' && root.hostname !== 'localhost' && root.hostname !== '127.0.0.1') fail('GITHUB_API_URL must use HTTPS.');
  if (root.username || root.password || root.search || root.hash) fail('GITHUB_API_URL must not include credentials, a query, or a fragment.');
  if (!root.pathname.endsWith('/')) root.pathname += '/';
  return root;
}

function apiEndpoint(root, path) {
  const endpoint = new URL(path.replace(/^\/+/, ''), root);
  if (endpoint.origin !== root.origin || !endpoint.pathname.startsWith(root.pathname)) fail('GitHub API endpoint escaped the configured API origin.');
  return endpoint;
}

function safeNextUrl(input, root) {
  const next = new URL(input, root);
  if (next.origin !== root.origin || !next.pathname.startsWith(root.pathname) || next.username || next.password || next.hash) {
    fail('GitHub pagination URL escaped the configured API origin.');
  }
  return next;
}

async function readLimited(response, maximum, label) {
  if (!response.body?.getReader) {
    const bytes = Buffer.from(await response.arrayBuffer());
    if (bytes.length > maximum) fail(`GitHub ${label} response exceeded the size limit.`);
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > maximum) {
        await reader.cancel().catch(() => {});
        fail(`GitHub ${label} response exceeded the size limit.`);
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks, length);
}

async function requestJson(url, token, fetchImpl, label) {
  let response;
  try {
    response = await fetchImpl(url, {
      method: 'GET',
      headers: {
        accept: 'application/vnd.github+json',
        authorization: `Bearer ${token}`,
        'x-github-api-version': API_VERSION,
        'user-agent': 'Quarterback-portable-build-receipt-producer',
      },
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      redirect: 'manual',
    });
  } catch {
    fail(`GitHub ${label} request failed.`);
  }
  if (response.status !== 200) {
    await readLimited(response, MAX_JSON_BYTES, label);
    fail(`GitHub ${label} request returned HTTP ${response.status}.`);
  }
  const bytes = await readLimited(response, MAX_JSON_BYTES, label);
  let body;
  try {
    body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } catch {
    fail(`GitHub ${label} response was not valid JSON.`);
  }
  return { body, response };
}

async function readPagedItems(root, token, fetchImpl, path, field, label) {
  let next = apiEndpoint(root, path);
  let expectedCount;
  const values = [];
  for (let page = 0; next && page < 100; page++) {
    next = safeNextUrl(next.href, root);
    const { body, response } = await requestJson(next, token, fetchImpl, label);
    if (!isRecord(body) || !Array.isArray(body[field]) || !Number.isSafeInteger(body.total_count) || body.total_count < 0) {
      fail(`GitHub ${label} response was incomplete.`);
    }
    if (expectedCount === undefined) expectedCount = body.total_count;
    else if (body.total_count !== expectedCount) fail(`GitHub ${label} result count changed during pagination.`);
    values.push(...body[field]);
    const link = response.headers.get('link');
    next = undefined;
    if (link) {
      for (const entry of link.split(',')) {
        const match = /^\s*<([^>]+)>\s*;\s*rel="next"\s*$/.exec(entry);
        if (match?.[1]) {
          next = safeNextUrl(match[1], root);
          break;
        }
      }
    }
  }
  if (next) fail(`GitHub ${label} pagination exceeded the page limit.`);
  if (expectedCount !== values.length) fail(`GitHub ${label} pagination returned an incomplete result.`);
  return values;
}

function acceptedWorkflowPath(value, expectedPath, branch) {
  return value === expectedPath || value === `${expectedPath}@${branch}` || value === `${expectedPath}@refs/heads/${branch}`;
}

function checkRunIdFromJob(job, root, repository, runId, runAttempt, sourceCommit) {
  if (!isRecord(job) || job.run_id !== runId || job.status !== 'completed' || job.conclusion !== 'success' ||
      typeof job.head_sha !== 'string' || job.head_sha.toLowerCase() !== sourceCommit.toLowerCase() || typeof job.check_run_url !== 'string') return undefined;
  if (job.run_attempt !== undefined && job.run_attempt !== null && job.run_attempt !== runAttempt) return undefined;
  let checkUrl;
  try { checkUrl = new URL(job.check_run_url); }
  catch { return undefined; }
  if (checkUrl.origin !== root.origin || !checkUrl.pathname.startsWith(root.pathname) || checkUrl.username || checkUrl.password || checkUrl.search || checkUrl.hash) return undefined;
  const [owner, name] = repository.split('/');
  const prefix = `${root.pathname.replace(/\/$/, '')}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/check-runs/`.toLowerCase();
  if (!checkUrl.pathname.toLowerCase().startsWith(prefix)) return undefined;
  const id = checkUrl.pathname.slice(prefix.length);
  return /^\d+$/.test(id) && Number.isSafeInteger(Number(id)) && Number(id) > 0 ? Number(id) : undefined;
}

export async function verifyGitHubRun(options) {
  const token = requiredString(options.githubToken, 'GITHUB_TOKEN', 4096);
  const { repository, owner, name } = repositoryParts(options.repository);
  const githubRepositoryId = positiveInteger(options.githubRepositoryId, 'GITHUB_REPOSITORY_ID');
  const runId = positiveInteger(options.sourceRunId, 'QB_SOURCE_RUN_ID');
  const runAttempt = positiveInteger(options.sourceRunAttempt, 'QB_SOURCE_RUN_ATTEMPT');
  const trustedWorkflowId = positiveInteger(options.trustedWorkflowId, 'QB_TRUSTED_WORKFLOW_ID');
  const trustedWorkflowPath = workflowPath(options.trustedWorkflowPath);
  const root = apiRoot(options.githubApiUrl);
  const fetchImpl = options.fetchImpl ?? DEFAULT_FETCH;
  const repoPath = `repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;

  const { body: repo } = await requestJson(apiEndpoint(root, repoPath), token, fetchImpl, 'repository');
  if (!isRecord(repo) || repo.id !== githubRepositoryId || typeof repo.full_name !== 'string' ||
      repo.full_name.toLowerCase() !== repository.toLowerCase() || typeof repo.default_branch !== 'string' || !repo.default_branch) {
    fail('GitHub repository ID, full name, or default branch did not match the configured repository.');
  }
  const branch = repo.default_branch;
  const runPath = `${repoPath}/actions/runs/${runId}/attempts/${runAttempt}`;
  const { body: run } = await requestJson(apiEndpoint(root, runPath), token, fetchImpl, 'workflow attempt');
  const event = isRecord(run) && typeof run.event === 'string' ? run.event : '';
  if (!SUPPORTED_SOURCE_EVENTS.has(event)) fail('GitHub workflow attempt uses an unsupported source event.');
  if (!isRecord(run) || run.id !== runId || run.run_attempt !== runAttempt || run.workflow_id !== trustedWorkflowId ||
      run.status !== 'completed' || run.conclusion !== 'success' ||
      run.head_branch !== branch || typeof run.head_sha !== 'string' || !SHA_RE.test(run.head_sha) ||
      !isRecord(run.repository) || run.repository.id !== githubRepositoryId ||
      !isRecord(run.head_repository) || run.head_repository.id !== githubRepositoryId ||
      !acceptedWorkflowPath(run.path, trustedWorkflowPath, branch)) {
    fail('GitHub workflow attempt is not a successful trusted default-branch run for this repository.');
  }
  const sourceCommit = run.head_sha;

  const { body: workflow } = await requestJson(apiEndpoint(root, `${repoPath}/actions/workflows/${trustedWorkflowId}`), token, fetchImpl, 'workflow identity');
  if (!isRecord(workflow) || workflow.id !== trustedWorkflowId || workflow.path !== trustedWorkflowPath || workflow.state !== 'active') {
    fail('GitHub workflow ID and path do not match the configured trusted workflow.');
  }

  const checkSuiteId = positiveInteger(run.check_suite_id, 'workflow attempt check_suite_id');
  const { body: suite } = await requestJson(apiEndpoint(root, `${repoPath}/check-suites/${checkSuiteId}`), token, fetchImpl, 'check suite');
  if (!isRecord(suite) || suite.id !== checkSuiteId || typeof suite.head_sha !== 'string' || suite.head_sha.toLowerCase() !== sourceCommit.toLowerCase()) {
    fail('GitHub check suite does not match the verified source commit.');
  }

  const jobs = await readPagedItems(root, token, fetchImpl, `${repoPath}/actions/runs/${runId}/attempts/${runAttempt}/jobs?per_page=100`, 'jobs', 'workflow job');
  for (const job of jobs) {
    if (!isRecord(job) || job.run_id !== runId || (job.run_attempt !== undefined && job.run_attempt !== null && job.run_attempt !== runAttempt)) {
      fail('GitHub returned a job outside the verified workflow attempt.');
    }
    if (job.status !== 'completed' || !['success', 'skipped'].includes(job.conclusion)) {
      fail('The verified workflow attempt contains a job check that did not pass.');
    }
  }
  const successJobs = jobs.filter((job) => job.conclusion === 'success');
  const jobCheckIds = new Set();
  for (const job of successJobs) {
    const id = checkRunIdFromJob(job, root, repository, runId, runAttempt, sourceCommit);
    if (!id) fail('A successful source job has no check run bound to the verified repository and commit.');
    jobCheckIds.add(id);
  }
  if (jobCheckIds.size === 0 || jobCheckIds.size !== successJobs.length) fail('The trusted workflow attempt has no distinct successful job check run for every source job.');

  const rawChecks = await readPagedItems(root, token, fetchImpl, `${repoPath}/check-suites/${checkSuiteId}/check-runs?per_page=100`, 'check_runs', 'check run');
  const checks = [];
  for (const item of rawChecks) {
    if (!isRecord(item) || !jobCheckIds.has(item.id)) continue;
    if (item.status !== 'completed' || item.conclusion !== 'success' || !isRecord(item.check_suite) || item.check_suite.id !== checkSuiteId ||
        typeof item.head_sha !== 'string' || item.head_sha.toLowerCase() !== sourceCommit.toLowerCase() || !isRecord(item.app) ||
        !Number.isSafeInteger(item.app.id) || item.app.id <= 0 || typeof item.name !== 'string' || !item.name.trim()) {
      fail('A source job check did not pass or did not match the verified source commit.');
    }
    checks.push({ appId: item.app.id, name: item.name, headSha: item.head_sha, conclusion: 'success' });
    jobCheckIds.delete(item.id);
  }
  if (jobCheckIds.size > 0 || checks.length === 0) fail('GitHub check runs do not cover every successful source job.');
  checks.sort((left, right) => left.name.localeCompare(right.name) || left.appId - right.appId);

  return {
    repositoryFullName: repo.full_name,
    defaultBranch: branch,
    sourceCommit,
    workflowId: trustedWorkflowId,
    workflowPath: trustedWorkflowPath,
    workflowRef: `refs/heads/${branch}`,
    runId,
    runAttempt,
    checks,
    jobs: successJobs,
    githubApiRoot: root,
    githubToken: token,
    fetchImpl,
  };
}

export function canonicalJSON(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('Receipt contains a non-JSON number.');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(',')}]`;
  if (!isRecord(value)) fail('Receipt contains a non-JSON value.');
  const keys = Object.keys(value).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJSON(value[key])}`).join(',')}}`;
}

function validateImageTargets(value, sourceRepository) {
  let targets = value;
  if (typeof value === 'string') {
    if (value.length > 8192) fail('QB_IMAGE_TARGETS_JSON exceeds the size limit.');
    try { targets = JSON.parse(value); }
    catch { fail('QB_IMAGE_TARGETS_JSON is not valid JSON.'); }
  }
  if (!Array.isArray(targets) || targets.length < 1 || targets.length > 8) fail('QB_IMAGE_TARGETS_JSON must contain between one and eight image targets.');
  const sourceOwner = repositoryParts(sourceRepository).owner.toLowerCase();
  const imageRepositories = new Set();
  const publisherJobNames = new Set();
  return targets.map((target) => {
    if (!isRecord(target) || Object.keys(target).sort().join(',') !== 'imageRepository,publisherJobName') {
      fail('Each QB image target must contain only imageRepository and publisherJobName.');
    }
    const imageRepository = requiredString(target.imageRepository, 'imageRepository', 250);
    const [imageOwner, imageName, ...extra] = imageRepository.split('/');
    if (imageOwner !== sourceOwner || extra.length || !/^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(imageName ?? '')) {
      fail('imageRepository must be a hostless lowercase GHCR repository owned by GITHUB_REPOSITORY.');
    }
    const publisherJobName = requiredString(target.publisherJobName, 'publisherJobName', 200);
    if (imageRepositories.has(imageRepository) || publisherJobNames.has(publisherJobName)) {
      fail('Each image repository and publisher job name must be unique.');
    }
    imageRepositories.add(imageRepository);
    publisherJobNames.add(publisherJobName);
    return { imageRepository, publisherJobName };
  });
}

function markerFromText(text, found, sourceCommit, runId, runAttempt) {
  for (const line of text.replace(ANSI_RE, '').split(/\r?\n/)) {
    const markerAt = line.indexOf('QB_BUILD_RECEIPT_V1');
    if (markerAt < 0) continue;
    const candidate = line.slice(markerAt).trim();
    const match = MARKER_RE.exec(candidate);
    if (!match) fail('Publisher job log contains a malformed build receipt marker.');
    if (match[1].toLowerCase() !== sourceCommit.toLowerCase() || Number(match[2]) !== runId || Number(match[3]) !== runAttempt) {
      fail('Publisher job log marker does not match the verified source run identity.');
    }
    if (!DIGEST_RE.test(match[4])) fail('Publisher job log marker contains an invalid image digest.');
    found.push(match[4].toLowerCase());
  }
}

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function safeZipPath(name) {
  if (!name || name.includes('\\') || name.startsWith('/') || /[\x00-\x1f\x7f]/.test(name)) return false;
  const parts = name.split('/');
  if (parts.some((part, index) => part === '..' || part === '.' || (!part && index !== parts.length - 1))) return false;
  return true;
}

function extractZipLogs(zipBytes, sourceCommit, runId, runAttempt) {
  if (!zipBytes.length || zipBytes.length > MAX_ZIP_BYTES) fail('Publisher job log archive is empty or exceeds the size limit.');
  let eocd = -1;
  const minimum = Math.max(0, zipBytes.length - 22 - 65535);
  for (let index = zipBytes.length - 22; index >= minimum; index--) {
    if (zipBytes.readUInt32LE(index) === 0x06054b50 && index + 22 + zipBytes.readUInt16LE(index + 20) === zipBytes.length) {
      eocd = index;
      break;
    }
  }
  if (eocd < 0) fail('Publisher job log response was not a valid ZIP archive.');
  const disk = zipBytes.readUInt16LE(eocd + 4);
  const directoryDisk = zipBytes.readUInt16LE(eocd + 6);
  const diskEntries = zipBytes.readUInt16LE(eocd + 8);
  const entryCount = zipBytes.readUInt16LE(eocd + 10);
  const directorySize = zipBytes.readUInt32LE(eocd + 12);
  const directoryOffset = zipBytes.readUInt32LE(eocd + 16);
  if (disk !== 0 || directoryDisk !== 0 || diskEntries !== entryCount || entryCount === 0xffff ||
      directorySize === 0xffffffff || directoryOffset === 0xffffffff || entryCount > MAX_LOG_MEMBERS ||
      directoryOffset + directorySize !== eocd) fail('Publisher job log archive has unsupported or invalid ZIP metadata.');

  const markers = [];
  const names = new Set();
  let expandedSize = 0;
  let cursor = directoryOffset;
  for (let index = 0; index < entryCount; index++) {
    if (cursor + 46 > eocd || zipBytes.readUInt32LE(cursor) !== 0x02014b50) fail('Publisher job log archive has an invalid central directory.');
    const madeBy = zipBytes.readUInt16LE(cursor + 4) >>> 8;
    const flags = zipBytes.readUInt16LE(cursor + 8);
    const method = zipBytes.readUInt16LE(cursor + 10);
    const checksum = zipBytes.readUInt32LE(cursor + 16);
    const compressedSize = zipBytes.readUInt32LE(cursor + 20);
    const memberSize = zipBytes.readUInt32LE(cursor + 24);
    const nameLength = zipBytes.readUInt16LE(cursor + 28);
    const extraLength = zipBytes.readUInt16LE(cursor + 30);
    const commentLength = zipBytes.readUInt16LE(cursor + 32);
    const externalAttributes = zipBytes.readUInt32LE(cursor + 38);
    const localOffset = zipBytes.readUInt32LE(cursor + 42);
    const recordEnd = cursor + 46 + nameLength + extraLength + commentLength;
    if (recordEnd > eocd || compressedSize === 0xffffffff || memberSize === 0xffffffff || localOffset === 0xffffffff) {
      fail('Publisher job log archive contains an unsupported ZIP64 entry.');
    }
    let name;
    try { name = new TextDecoder('utf-8', { fatal: true }).decode(zipBytes.subarray(cursor + 46, cursor + 46 + nameLength)); }
    catch { fail('Publisher job log archive contains a non-UTF-8 path.'); }
    if (!safeZipPath(name) || names.has(name)) fail('Publisher job log archive contains an unsafe or duplicate path.');
    names.add(name);
    if (flags & (1 | 0x40)) fail('Publisher job log archive contains encrypted entries.');
    if (method !== 0 && method !== 8) fail('Publisher job log archive uses an unsupported compression method.');
    if (memberSize > MAX_MEMBER_BYTES) fail('Publisher job log archive entry exceeds the size limit.');
    expandedSize += memberSize;
    if (expandedSize > MAX_LOG_BYTES) fail('Publisher job log archive exceeds the expanded size limit.');
    const unixMode = madeBy === 3 ? externalAttributes >>> 16 : 0;
    const type = unixMode & 0o170000;
    if (type === 0o120000) fail('Publisher job log archive contains a symbolic link.');
    const isDirectory = name.endsWith('/');
    if ((isDirectory && (memberSize !== 0 || compressedSize !== 0 || (type && type !== 0o040000))) ||
        (!isDirectory && type && type !== 0o100000)) fail('Publisher job log archive contains an invalid non-regular entry.');
    if (localOffset + 30 > directoryOffset || zipBytes.readUInt32LE(localOffset) !== 0x04034b50) fail('Publisher job log archive has an invalid local header.');
    const localFlags = zipBytes.readUInt16LE(localOffset + 6);
    const localMethod = zipBytes.readUInt16LE(localOffset + 8);
    const localNameLength = zipBytes.readUInt16LE(localOffset + 26);
    const localExtraLength = zipBytes.readUInt16LE(localOffset + 28);
    const localNameStart = localOffset + 30;
    const localDataStart = localNameStart + localNameLength + localExtraLength;
    const localName = zipBytes.subarray(localNameStart, localNameStart + localNameLength);
    const centralName = zipBytes.subarray(cursor + 46, cursor + 46 + nameLength);
    if (localFlags !== flags || localMethod !== method || !localName.equals(centralName) || localDataStart + compressedSize > directoryOffset) {
      fail('Publisher job log archive local header does not match its central directory.');
    }
    if (isDirectory) {
      cursor = recordEnd;
      continue;
    }
    if (/\.(?:txt|log)$/i.test(name)) {
      const compressed = zipBytes.subarray(localDataStart, localDataStart + compressedSize);
      let raw;
      try {
        raw = method === 0 ? Buffer.from(compressed) : inflateRawSync(compressed, { maxOutputLength: MAX_MEMBER_BYTES + 1 });
      } catch {
        fail('Publisher job log archive entry could not be decompressed within its size limit.');
      }
      if (raw.length !== memberSize || raw.length > MAX_MEMBER_BYTES || crc32(raw) !== checksum) {
        fail('Publisher job log archive entry did not match its declared size or checksum.');
      }
      let text;
      try { text = new TextDecoder('utf-8', { fatal: true }).decode(raw); }
      catch { fail('Publisher job log archive contains a non-UTF-8 log entry.'); }
      markerFromText(text, markers, sourceCommit, runId, runAttempt);
    }
    cursor = recordEnd;
  }
  if (cursor !== directoryOffset + directorySize) fail('Publisher job log archive central directory length did not match its metadata.');
  return markers;
}

export function extractPublisherDigest(logBytes, sourceCommit, runId, runAttempt) {
  const bytes = Buffer.from(logBytes);
  if (!bytes.length || bytes.length > MAX_LOG_BYTES) fail('Publisher job log is empty or exceeds the size limit.');
  const markers = [];
  if (bytes.subarray(0, 4).equals(Buffer.from([0x50, 0x4b, 0x03, 0x04])) || bytes.subarray(0, 4).equals(Buffer.from([0x50, 0x4b, 0x05, 0x06])) || bytes.subarray(0, 4).equals(Buffer.from([0x50, 0x4b, 0x07, 0x08]))) {
    markers.push(...extractZipLogs(bytes, sourceCommit, runId, runAttempt));
  } else {
    let text;
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch { fail('Publisher job log response was not valid UTF-8 text.'); }
    markerFromText(text, markers, sourceCommit, runId, runAttempt);
  }
  if (markers.length !== 1) fail('Publisher job log must contain exactly one build receipt marker.');
  return markers[0];
}

async function publisherJobLog(evidence, repository, job, imageTarget) {
  const checkUrl = apiEndpoint(evidence.githubApiRoot, `repos/${repository.split('/').map(encodeURIComponent).join('/')}/actions/jobs/${job.id}/logs`);
  let response;
  try {
    response = await evidence.fetchImpl(checkUrl, {
      method: 'GET',
      headers: {
        accept: 'application/vnd.github+json',
        authorization: `Bearer ${evidence.githubToken}`,
        'x-github-api-version': API_VERSION,
        'user-agent': 'Quarterback-portable-build-receipt-producer',
      },
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      redirect: 'manual',
    });
  } catch {
    fail(`GitHub publisher job log request failed for ${imageTarget.imageRepository}.`);
  }
  let bytes;
  if (response.status === 200) {
    bytes = await readLimited(response, MAX_LOG_BYTES, 'publisher job logs');
  } else if ([301, 302, 303, 307, 308].includes(response.status)) {
    await readLimited(response, 4096, 'publisher job log redirect');
    const location = response.headers.get('location');
    if (!location || location.length > 8192 || location.includes(evidence.githubToken)) fail('GitHub publisher job log redirect is missing or invalid.');
    let target;
    try { target = new URL(location, checkUrl); }
    catch { fail('GitHub publisher job log redirect is invalid.'); }
    if (target.protocol !== 'https:' || !target.hostname || target.username || target.password || target.hash) fail('GitHub publisher job log redirect must be HTTPS and contain no credentials or fragment.');
    let redirected;
    try { redirected = await evidence.fetchImpl(target, { method: 'GET', headers: {}, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS), redirect: 'manual' }); }
    catch { fail('Signed GitHub publisher job log download failed.'); }
    if (redirected.status !== 200) {
      await readLimited(redirected, MAX_LOG_BYTES, 'redirected publisher job logs');
      fail('Signed GitHub publisher job log download failed.');
    }
    bytes = await readLimited(redirected, MAX_LOG_BYTES, 'redirected publisher job logs');
  } else {
    await readLimited(response, MAX_LOG_BYTES, 'publisher job logs');
    fail(`GitHub publisher job log request returned HTTP ${response.status}.`);
  }
  return extractPublisherDigest(bytes, evidence.sourceCommit, evidence.runId, evidence.runAttempt);
}

function verifyReceiptConfiguration(options) {
  const repositoryId = requiredString(options.repositoryId, 'QB_REPOSITORY_ID', 128);
  if (!UUID_RE.test(repositoryId)) fail('QB_REPOSITORY_ID must be Quarterback’s internal repository UUID.');
  const publisherIdentity = requiredString(options.publisherIdentity, 'QB_PUBLISHER_IDENTITY', 200);
  const instanceUrl = new URL(requiredString(options.instanceUrl, 'QB_INSTANCE_URL', 2048));
  if (instanceUrl.protocol !== 'https:' && instanceUrl.hostname !== 'localhost' && instanceUrl.hostname !== '127.0.0.1') fail('QB_INSTANCE_URL must use HTTPS.');
  if (instanceUrl.username || instanceUrl.password || instanceUrl.search || instanceUrl.hash || instanceUrl.pathname !== '/') {
    fail('QB_INSTANCE_URL must not include credentials, a path, a query, or a fragment.');
  }
  const endpoint = new URL('/api/v1/build-receipts', instanceUrl);
  const receiptToken = requiredString(options.receiptToken, 'QB_BUILD_RECEIPT_TOKEN', 8192);
  let key;
  try { key = createPrivateKey(privateKeyPem(options.signingKeyPem)); }
  catch { fail('QB_BUILD_SIGNING_KEY is not a valid private key.'); }
  if (key.asymmetricKeyType !== 'ed25519') fail('QB_BUILD_SIGNING_KEY must be Ed25519.');
  return { repositoryId, publisherIdentity, endpoint, receiptToken, key };
}

function signedReceipt(unsigned, privateKey) {
  const canonical = canonicalJSON(unsigned);
  const signature = sign(null, Buffer.from(canonical, 'utf8'), privateKey).toString('base64');
  return {
    ...unsigned,
    attestation: {
      digest: unsigned.imageDigest,
      issuer: 'quarterback-ci',
      subject: unsigned.publisherIdentity,
      signature,
      payload: JSON.parse(canonical),
    },
  };
}

async function postReceipt(endpoint, token, receipt, fetchImpl, imageRepository) {
  let response;
  try {
    response = await fetchImpl(endpoint, {
      method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify(receipt),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      redirect: 'manual',
    });
  } catch {
    fail(`Quarterback receipt submission failed for ${imageRepository}.`);
  }
  if (response.status !== 200 && response.status !== 201) {
    await readLimited(response, MAX_JSON_BYTES, 'Quarterback receipt submission');
    fail(`Quarterback receipt submission returned HTTP ${response.status} for ${imageRepository}.`);
  }
  await readLimited(response, MAX_JSON_BYTES, 'Quarterback receipt submission');
}

function securityResults(checks, names, sourceCommit) {
  const result = [];
  for (const scannerName of names) {
    const matches = checks.filter((check) => check.name === scannerName);
    if (matches.length !== 1) fail(`Configured security check must map to exactly one successful job check: ${scannerName}.`);
    result.push({ scanner: scannerName, status: 'passed', commit: sourceCommit });
  }
  return result;
}

export async function produceBuildReceipts(options) {
  const config = verifyReceiptConfiguration(options);
  const targets = validateImageTargets(options.imageTargets, options.repository);
  const evidence = await verifyGitHubRun(options);
  const checksForSecurity = securityResults(evidence.checks, options.securityCheckNames ?? [], evidence.sourceCommit);
  const receipts = [];
  for (const target of targets) {
    const matches = evidence.jobs.filter((job) => job.name === target.publisherJobName && job.status === 'completed' && job.conclusion === 'success' &&
      job.head_sha?.toLowerCase() === evidence.sourceCommit.toLowerCase() && job.run_id === evidence.runId &&
      (job.run_attempt === undefined || job.run_attempt === null || job.run_attempt === evidence.runAttempt));
    if (matches.length !== 1) fail(`The verified workflow attempt must contain exactly one successful publisher job for ${target.imageRepository}.`);
    const imageDigest = await publisherJobLog(evidence, options.repository, matches[0], target);
    const unsigned = {
      repositoryId: config.repositoryId,
      repositoryFullName: evidence.repositoryFullName,
      sourceCommit: evidence.sourceCommit,
      sourceBranch: evidence.defaultBranch,
      workflowId: evidence.workflowId,
      workflowPath: evidence.workflowPath,
      workflowRef: evidence.workflowRef,
      runId: evidence.runId,
      runAttempt: evidence.runAttempt,
      runConclusion: 'success',
      publisherIdentity: config.publisherIdentity,
      imageRepository: target.imageRepository,
      imageDigest,
      checkRuns: evidence.checks,
      securityResults: checksForSecurity,
    };
    const receipt = signedReceipt(unsigned, config.key);
    await postReceipt(config.endpoint, config.receiptToken, receipt, evidence.fetchImpl, target.imageRepository);
    receipts.push(receipt);
  }
  return receipts;
}

function parseSecurityNames(value) {
  if (!value) return [];
  const names = value.split(',').map((item) => item.trim()).filter(Boolean);
  if (names.some((item) => item.length > 200)) fail('QB_SECURITY_CHECK_NAMES contains an invalid check name.');
  return [...new Set(names)];
}

export async function runProducer(env = process.env) {
  const sourceRunId = positiveInteger(env.QB_SOURCE_RUN_ID, 'QB_SOURCE_RUN_ID');
  const sourceRunAttempt = positiveInteger(env.QB_SOURCE_RUN_ATTEMPT, 'QB_SOURCE_RUN_ATTEMPT');
  const targets = requiredString(env.QB_IMAGE_TARGETS_JSON, 'QB_IMAGE_TARGETS_JSON', 8192);
  const signingKeyPem = privateKeyPem(env.QB_BUILD_SIGNING_KEY);
  await produceBuildReceipts({
    githubApiUrl: env.GITHUB_API_URL ?? 'https://api.github.com/',
    githubToken: requiredString(env.GITHUB_TOKEN, 'GITHUB_TOKEN', 4096),
    repository: requiredString(env.GITHUB_REPOSITORY, 'GITHUB_REPOSITORY', 250),
    githubRepositoryId: positiveInteger(env.GITHUB_REPOSITORY_ID, 'GITHUB_REPOSITORY_ID'),
    sourceRunId,
    sourceRunAttempt,
    trustedWorkflowId: positiveInteger(env.QB_TRUSTED_WORKFLOW_ID, 'QB_TRUSTED_WORKFLOW_ID'),
    trustedWorkflowPath: workflowPath(requiredString(env.QB_TRUSTED_WORKFLOW_PATH, 'QB_TRUSTED_WORKFLOW_PATH', 300)),
    repositoryId: requiredString(env.QB_REPOSITORY_ID, 'QB_REPOSITORY_ID', 128),
    publisherIdentity: requiredString(env.QB_PUBLISHER_IDENTITY, 'QB_PUBLISHER_IDENTITY', 200),
    imageTargets: targets,
    signingKeyPem,
    instanceUrl: requiredString(env.QB_INSTANCE_URL, 'QB_INSTANCE_URL', 2048),
    receiptToken: requiredString(env.QB_BUILD_RECEIPT_TOKEN, 'QB_BUILD_RECEIPT_TOKEN', 8192),
    securityCheckNames: parseSecurityNames(env.QB_SECURITY_CHECK_NAMES),
  });
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : '';
if (invokedPath && pathToFileURL(invokedPath).href === import.meta.url) {
  runProducer().catch((error) => {
    process.stderr.write(`portable build receipt producer: ${error instanceof ReceiptEvidenceError ? error.message : 'operation failed'}\n`);
    process.exitCode = 1;
  });
}
