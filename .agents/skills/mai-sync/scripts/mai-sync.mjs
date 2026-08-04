#!/usr/bin/env node
import { execFileSync } from 'node:child_process';
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { basename, join, relative, resolve } from 'node:path';
import { request as httpRequest } from 'node:http';

const DEFAULT_API = 'http://127.0.0.1:3002/api/events';
const DEFAULT_BASE_URL = 'http://127.0.0.1:3002';
const MAX = { title: 140, detail: 900, label: 160, file: 240, evidence: 300 };
const MODEL_COLLECTIONS = new Set([
  'modules', 'workItems', 'phases', 'milestones', 'decisions', 'risks', 'interfaces',
  'architectureNodes', 'architectureEdges', 'sourceDocuments',
]);

function parseArgs() {
  const raw = process.argv.slice(2);
  const command = raw[0] && !raw[0].startsWith('--') ? raw.shift() : 'scan';
  const values = {};
  for (let index = 0; index < raw.length; index += 1) {
    if (!raw[index].startsWith('--')) continue;
    const key = raw[index].slice(2);
    values[key] = raw[index + 1] && !raw[index + 1].startsWith('--') ? raw[++index] : 'true';
  }
  return {
    command,
    root: resolve(values['project-root'] || '.'),
    api: values.api || process.env.MAI_API_URL || DEFAULT_API,
    baseUrl: values['base-url'] || process.env.MAI_BASE_URL || DEFAULT_BASE_URL,
    values,
  };
}

function protect(value, max) {
  const text = String(value || '')
    .replace(/((?:api[_-]?key|token|password|secret)\s*[:=]\s*)\S+/gi, '$1[redacted]')
    .replace(/\s+/g, ' ')
    .trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function splitList(value, max) {
  return String(value || '').split(',').map((item) => protect(item, max)).filter(Boolean);
}

function parseJson(value, label) {
  try {
    const parsed = JSON.parse(String(value));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error();
    return parsed;
  } catch {
    throw new Error(`${label} must be a JSON object`);
  }
}

function deterministicId(root, event) {
  return `mai_${createHash('sha256').update([root, event.type, event.title, event.module, event.evidence?.join('|')].join('|')).digest('hex').slice(0, 24)}`;
}

function createEvent(root, input, deterministic = false) {
  const event = {
    v: 1,
    id: input.id || randomUUID(),
    type: protect(input.type || 'project_scanned', 60),
    title: protect(input.title, MAX.title),
    detail: protect(input.detail, MAX.detail),
    status: protect(input.status || 'completed', 40),
    module: protect(input.module, MAX.label),
    feature: protect(input.feature, MAX.label),
    files: (input.files || []).slice(0, 24).map((item) => protect(item, MAX.file)),
    evidence: (input.evidence || []).slice(0, 12).map((item) => protect(item, MAX.evidence)),
    tags: (input.tags || []).slice(0, 12).map((item) => protect(item, 80)),
    timestamp: input.timestamp || new Date().toISOString(),
  };
  if (deterministic) event.id = deterministicId(root, event);
  return event;
}

function runGit(root, args) {
  try {
    return execFileSync('git', args, { cwd: root, encoding: 'utf8', timeout: 8000, stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {
    return '';
  }
}

function listDirectory(target) {
  try {
    return readdirSync(target, { withFileTypes: true });
  } catch {
    return [];
  }
}

function scanModules(root) {
  const candidates = [];
  for (const container of ['src', 'app', 'packages', 'services', 'modules', 'lib']) {
    const absolute = join(root, container);
    if (!existsSync(absolute)) continue;
    const children = listDirectory(absolute).filter((entry) => entry.isDirectory() && !entry.name.startsWith('.'));
    if (children.length) children.forEach((entry) => candidates.push(join(container, entry.name)));
    else candidates.push(container);
  }

  return candidates.slice(0, 30).map((modulePath) => {
    const entries = listDirectory(join(root, modulePath));
    const names = entries.slice(0, 8).map((entry) => entry.name);
    return createEvent(root, {
      type: 'module_discovered',
      title: `识别模块：${basename(modulePath)}`,
      detail: `模块位于 ${modulePath}，当前包含 ${entries.length} 个直接子项。主要入口包括 ${names.join('、') || '暂无可见入口'}，Mai 将它作为独立责任区持续跟踪。`,
      status: 'completed',
      module: modulePath,
      files: names.map((name) => join(modulePath, name)),
      evidence: [`filesystem:${modulePath}`],
      tags: ['scan', 'module'],
    }, true);
  });
}

function scanGit(root) {
  const output = runGit(root, ['log', '-n', '8', '--pretty=format:%H%x09%aI%x09%s']);
  if (!output) return [];
  return output.split(/\r?\n/).flatMap((line) => {
    const [sha, timestamp, ...messageParts] = line.split('\t');
    const message = messageParts.join(' ').trim();
    if (!sha || !message) return [];
    return [createEvent(root, {
      type: /merge/i.test(message) ? 'git_milestone' : 'commit',
      title: message,
      detail: `提交 ${sha.slice(0, 8)} 已进入当前项目历史，提交主题为“${message}”。这条记录用于关联功能交付与后续复盘，不包含源码内容。`,
      status: 'completed',
      evidence: [`git:commit:${sha}`],
      timestamp,
      tags: ['git'],
    }, true)];
  });
}

function scanTests(root) {
  const evidence = [];
  for (const candidate of ['tests', 'test', '__tests__', 'playwright.config.ts', 'vitest.config.ts', 'pytest.ini', 'test-results/.last-run.json']) {
    if (existsSync(join(root, candidate))) evidence.push(candidate);
  }
  if (!evidence.length) return [];

  let status = 'completed';
  const lastRunPath = join(root, 'test-results', '.last-run.json');
  if (existsSync(lastRunPath)) {
    try {
      const parsed = JSON.parse(readFileSync(lastRunPath, 'utf8'));
      status = parsed.status === 'failed' ? 'failed' : parsed.status || status;
    } catch {
      status = 'unknown';
    }
  }
  return [createEvent(root, {
    type: status === 'failed' ? 'tests_failed' : 'tests_detected',
    title: status === 'failed' ? '最近测试存在失败' : '已识别项目测试链路',
    detail: `扫描识别到 ${evidence.length} 项测试证据：${evidence.join('、')}。当前记录状态为 ${status}，后续 Agent 完成测试后应通过 emit 模式写入具体结果。`,
    status,
    files: evidence,
    evidence: evidence.map((item) => `exists:${item}`),
    tags: ['scan', 'test'],
  }, true)];
}

function scanDocumentProgress(root) {
  const results = [];
  for (const relativePath of ['README.md', 'docs/HANDOFF.md']) {
    const absolute = join(root, relativePath);
    if (!existsSync(absolute)) continue;
    const lines = readFileSync(absolute, 'utf8').split(/\r?\n/);
    for (const line of lines) {
      const match = line.match(/^\s*[-*]\s*\[x\]\s*(.+)$/i);
      if (!match) continue;
      const title = protect(match[1], MAX.title);
      results.push(createEvent(root, {
        type: 'feature_completed',
        title,
        detail: `已有项目文档 ${relativePath} 将“${title}”标记为完成。该记录来自二次开发前的基线扫描，后续可由新的开发事件补充实现文件和验证证据。`,
        status: 'completed',
        feature: title,
        files: [relativePath],
        evidence: [`document-checkbox:${relativePath}`],
        tags: ['baseline', 'documentation'],
      }, true));
      if (results.length >= 20) return results;
    }
  }
  return results;
}

function scanProject(root) {
  const modules = scanModules(root);
  const gitEvents = scanGit(root);
  const tests = scanTests(root);
  const progress = scanDocumentProgress(root);
  const summary = createEvent(root, {
    type: 'project_scanned',
    title: `完成项目基线扫描：${basename(root)}`,
    detail: `扫描识别到 ${modules.length} 个模块、${gitEvents.length} 条近期 Git 记录、${tests.length} 组测试证据和 ${progress.length} 项已有进度。该基线适用于已有项目的二次开发，后续只需追加发生变化的里程碑事件。`,
    status: 'completed',
    module: basename(root),
    evidence: ['filesystem', 'git-log', 'project-docs', 'test-artifacts'],
    tags: ['scan', 'baseline'],
  }, true);
  return [summary, ...modules, ...gitEvents, ...tests, ...progress];
}

function buildManualEvent(root, values) {
  if (!values.title || !values.detail) {
    throw new Error('emit requires --title and --detail');
  }
  return createEvent(root, {
    id: values.id,
    type: values.type || 'feature_completed',
    title: values.title,
    detail: values.detail,
    status: values.status || 'completed',
    module: values.module,
    feature: values.feature,
    files: splitList(values.files, MAX.file),
    evidence: splitList(values.evidence, MAX.evidence),
    tags: splitList(values.tags, 80),
  });
}

function postEvents(url, root, events) {
  return new Promise((done) => {
    const body = JSON.stringify({ projectPath: root, events });
    const target = new URL(url);
    const req = httpRequest({ hostname: target.hostname, port: target.port || 80, path: target.pathname, method: 'POST', headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) }, timeout: 3500 }, (res) => {
      res.resume();
      res.on('end', () => done(Boolean(res.statusCode && res.statusCode >= 200 && res.statusCode < 300)));
    });
    req.on('error', () => done(false));
    req.on('timeout', () => { req.destroy(); done(false); });
    req.end(body);
  });
}

function requestJson(url, method, body) {
  return new Promise((resolveRequest, rejectRequest) => {
    const target = new URL(url);
    const payload = body === undefined ? '' : JSON.stringify(body);
    const req = httpRequest({
      hostname: target.hostname,
      port: target.port || 80,
      path: `${target.pathname}${target.search}`,
      method,
      headers: payload ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } : {},
      timeout: 5000,
    }, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { raw += chunk; });
      res.on('end', () => {
        let parsed = {};
        try { parsed = raw ? JSON.parse(raw) : {}; } catch { parsed = { raw }; }
        if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
          resolveRequest(parsed);
        } else {
          const err = new Error(parsed.error || `Mai API returned ${res.statusCode}`);
          err.statusCode = res.statusCode;
          rejectRequest(err);
        }
      });
    });
    req.on('error', rejectRequest);
    req.on('timeout', () => req.destroy(new Error('Mai API request timed out')));
    if (payload) req.write(payload);
    req.end();
  });
}

async function resolveMaiProject(baseUrl, root) {
  return requestJson(`${baseUrl.replace(/\/$/, '')}/api/projects`, 'POST', { path: root });
}

const emptyModel = () => ({
  version: 1,
  revision: 0,
  updatedAt: new Date(0).toISOString(),
  modules: [],
  workItems: [],
  phases: [],
  milestones: [],
  decisions: [],
  risks: [],
  interfaces: [],
  architecture: { nodes: [], edges: [] },
  sourceDocuments: [],
});

function modelCollection(model, collection) {
  if (collection === 'architectureNodes') return model.architecture.nodes;
  if (collection === 'architectureEdges') return model.architecture.edges;
  return model[collection];
}

function recordKey(item) {
  if (item && typeof item.id === 'string' && item.id) return item.id;
  if (item && typeof item.path === 'string' && item.path) return item.path;
  return undefined;
}

const REQUIRED_FIELDS = {
  modules: ['id', 'name'],
  workItems: ['id', 'title', 'status', 'priority', 'progress'],
  phases: ['id', 'name'],
  milestones: ['id', 'title', 'status'],
  decisions: ['id', 'title'],
  risks: ['id', 'title'],
  interfaces: ['id', 'name', 'type'],
  architectureNodes: ['id', 'label', 'type'],
  architectureEdges: ['id', 'source', 'target'],
  sourceDocuments: ['path'],
};

const ENUM_FIELDS = {
  workItems: { status: ['todo', 'in_progress', 'review', 'done', 'blocked'], priority: ['low', 'medium', 'high', 'critical'] },
  phases: { status: ['todo', 'in_progress', 'review', 'done', 'blocked'] },
  milestones: { status: ['todo', 'in_progress', 'review', 'done', 'blocked'] },
  decisions: { status: ['proposed', 'accepted', 'superseded', 'rejected'] },
  risks: { status: ['open', 'mitigating', 'closed', 'accepted'], probability: ['low', 'medium', 'high'], impact: ['low', 'medium', 'high', 'critical'] },
  interfaces: { type: ['http', 'event', 'function', 'database', 'file', 'other'] },
  architectureNodes: { type: ['service', 'database', 'api', 'client', 'queue', 'cache', 'external'], status: ['active', 'inactive', 'planned'] },
  architectureEdges: { type: ['sync', 'async', 'data', 'auth'] },
};

function validateLocalEntity(collection, item) {
  for (const field of REQUIRED_FIELDS[collection] || []) {
    const value = item[field];
    if (value === undefined || value === null || (typeof value === 'string' && !value.trim())) {
      throw new Error(`Invalid ${collection}: ${field} is required`);
    }
  }
  for (const [field, allowed] of Object.entries(ENUM_FIELDS[collection] || {})) {
    if (item[field] !== undefined && !allowed.includes(item[field])) {
      throw new Error(`Invalid ${collection}: ${field} must be one of ${allowed.join(', ')}`);
    }
  }
  if (item.progress !== undefined && (!Number.isInteger(item.progress) || item.progress < 0 || item.progress > 100)) {
    throw new Error(`Invalid ${collection}: progress must be an integer from 0 through 100`);
  }
  for (const [field, value] of Object.entries(item)) {
    if (['dependsOn', 'acceptanceCriteria', 'evidence', 'technologies', 'dependencies', 'alternatives'].includes(field) && !Array.isArray(value)) {
      throw new Error(`Invalid ${collection}: ${field} must be an array`);
    }
    if (['startDate', 'dueDate', 'endDate', 'date', 'updatedAt', 'lastReadAt'].includes(field) && value !== undefined && (typeof value !== 'string' || Number.isNaN(Date.parse(value)))) {
      throw new Error(`Invalid ${collection}: ${field} must use ISO 8601`);
    }
  }
}

function mutateLocalModel(root, operation, collection, item, id) {
  const directory = join(root, '.mai');
  const target = join(directory, 'project.json');
  mkdirSync(directory, { recursive: true });
  let model = emptyModel();
  if (existsSync(target)) model = { ...model, ...JSON.parse(readFileSync(target, 'utf8')) };
  model.architecture = { nodes: [], edges: [], ...(model.architecture || {}) };
  const records = modelCollection(model, collection);
  if (!Array.isArray(records)) throw new Error(`Unsupported model collection: ${collection}`);
  const key = operation === 'upsert' ? recordKey(item) : id;
  const index = records.findIndex((record) => recordKey(record) === key);
  if (operation === 'remove') {
    if (index >= 0) records.splice(index, 1);
  } else if (index >= 0) {
    const merged = { ...records[index], ...item };
    validateLocalEntity(collection, merged);
    records[index] = merged;
  } else {
    validateLocalEntity(collection, item);
    records.push(item);
  }
  model.version = 1;
  model.revision = Number(model.revision || 0) + 1;
  model.updatedAt = new Date().toISOString();
  const temporary = `${target}.${randomUUID()}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(model, null, 2)}\n`, 'utf8');
  renameSync(temporary, target);
  return { target, model };
}

function buildModelItem(collection, values) {
  const data = values.data ? parseJson(values.data, '--data') : {};
  if (collection === 'sourceDocuments') {
    const docPath = protect(values.path || data.path, MAX.file);
    if (!docPath) throw new Error('upsert sourceDocuments requires --path or a path in --data');
    const item = { path: docPath };
    if (values.hash || data.hash) item.hash = protect(values.hash || data.hash, 120);
    if (values['updated-at'] || data.updatedAt) item.updatedAt = protect(values['updated-at'] || data.updatedAt, 32);
    if (values['last-read-at'] || data.lastReadAt) item.lastReadAt = protect(values['last-read-at'] || data.lastReadAt, 32);
    return item;
  }
  const id = protect(values.id || data.id, 120);
  if (!id) throw new Error('upsert requires --id or an id in --data');
  const item = { ...data, id };
  if (values.title) item.title = protect(values.title, MAX.title);
  if (values.name) item.name = protect(values.name, MAX.title);
  if (values.description) item.description = protect(values.description, MAX.detail);
  if (values.status) item.status = protect(values.status, 40);
  if (values.priority) item.priority = protect(values.priority, 20);
  if (values.progress !== undefined) item.progress = Math.max(0, Math.min(100, Number(values.progress)));
  if (values.module) item.moduleId = protect(values.module, MAX.label);
  if (values.phase) item.phaseId = protect(values.phase, MAX.label);
  if (values.milestone) item.milestoneId = protect(values.milestone, MAX.label);
  if (values['start-date']) item.startDate = protect(values['start-date'], 32);
  if (values['due-date']) item.dueDate = protect(values['due-date'], 32);
  if (values['end-date']) item.endDate = protect(values['end-date'], 32);
  if (values.dependencies) item.dependsOn = splitList(values.dependencies, MAX.label);
  if (values.acceptance) item.acceptanceCriteria = splitList(values.acceptance, MAX.evidence);
  if (values.evidence) item.evidence = splitList(values.evidence, MAX.evidence);
  return item;
}

async function mutateModel(baseUrl, root, operation, collection, values) {
  if (!MODEL_COLLECTIONS.has(collection)) {
    throw new Error(`Unsupported collection: ${collection}. Allowed: ${[...MODEL_COLLECTIONS].join(', ')}`);
  }
  const item = operation === 'upsert' ? buildModelItem(collection, values) : undefined;
  const id = operation === 'remove' ? protect(values.id || values.path, 120) : undefined;
  if (operation === 'remove' && !id) throw new Error('remove requires --id or --path');
  try {
    const project = await resolveMaiProject(baseUrl, root);
    const model = await requestJson(`${baseUrl.replace(/\/$/, '')}/api/projects/${encodeURIComponent(project.id)}/model`, 'PATCH', {
      operation,
      collection,
      item,
      id,
    });
    return { mode: 'api', projectId: project.id, model };
  } catch (error) {
    if (error.statusCode && error.statusCode >= 400 && error.statusCode < 500) {
      throw error;
    }
    const fallback = mutateLocalModel(root, operation, collection, item, id);
    return { mode: 'file', warning: error.message, target: fallback.target, model: fallback.model };
  }
}

function appendFallback(root, events) {
  const directory = join(root, '.mai');
  const target = join(directory, 'events.ndjson');
  mkdirSync(directory, { recursive: true });
  const existingIds = new Set();
  if (existsSync(target)) {
    readFileSync(target, 'utf8').split(/\r?\n/).filter(Boolean).forEach((line) => {
      try { existingIds.add(JSON.parse(line).id); } catch { /* Preserve unreadable external lines. */ }
    });
  }
  const fresh = events.filter((event) => !existingIds.has(event.id));
  if (fresh.length) appendFileSync(target, `${fresh.map((event) => JSON.stringify(event)).join('\n')}\n`, 'utf8');
  return { target, count: fresh.length };
}

async function scanViaApi(baseUrl, root, mode) {
  const project = await resolveMaiProject(baseUrl, root);
  const url = `${baseUrl.replace(/\/$/, '')}/api/projects/${encodeURIComponent(project.id)}/scan`;
  return requestJson(url, 'POST', { mode });
}

async function main() {
  const { command, root, api, baseUrl, values } = parseArgs();
  if (!existsSync(root) || !statSync(root).isDirectory()) throw new Error(`Project root not found: ${root}`);
  if (!['scan', 'emit', 'upsert', 'remove'].includes(command)) throw new Error('Command must be scan, emit, upsert or remove');
  if (command === 'scan') {
    const mode = values.mode === 'full' ? 'full' : 'incremental';
    try {
      const snapshot = await scanViaApi(baseUrl, root, mode);
      console.log(`[mai-sync] scan via Mai API (${mode})`);
      console.log(JSON.stringify(snapshot));
      return;
    } catch (error) {
      if (error.statusCode && error.statusCode >= 400 && error.statusCode < 500) throw error;
      console.warn(`[mai-sync] Mai scan API unavailable (${error.message}); falling back to local scan`);
    }
  }
  if (command === 'upsert' || command === 'remove') {
    const result = await mutateModel(baseUrl, root, command, values.collection, values);
    console.log(`[mai-sync] ${command} ${values.collection}/${values.id || values.path} via ${result.mode}`);
    if (result.warning) console.warn(`[mai-sync] Mai offline; updated local model (${result.warning})`);
    console.log(JSON.stringify({ revision: result.model.revision, updatedAt: result.model.updatedAt }));
    return;
  }
  const events = command === 'emit' ? [buildManualEvent(root, values)] : scanProject(root);
  const posted = await postEvents(api, root, events);
  if (posted) {
    console.log(`[mai-sync] synced ${events.length} event(s) to ${api}`);
  } else {
    const fallback = appendFallback(root, events);
    console.log(`[mai-sync] Mai offline; appended ${fallback.count} new event(s) to ${relative(root, fallback.target)}`);
  }
  events.forEach((event) => console.log(JSON.stringify(event)));
}

main().catch((error) => {
  console.error(`[mai-sync] ${error.message}`);
  process.exitCode = 1;
});
