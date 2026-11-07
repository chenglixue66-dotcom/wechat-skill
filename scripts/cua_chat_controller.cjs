'use strict';

// No application is discovered and no UI action runs when this file is loaded.
// The caller supplies an already selected CUA App and durable receipt hooks.
// This module never takes screenshots, guesses coordinates, or logs AX/body data.
const READ_OPTIONS = Object.freeze({emit: false, disableDiffing: true});
const ROLE_WORDS = ['搜索文本框', '标准窗口', '静态文本', '文本区域', '文本框',
  '输入框', '关闭按钮', '菜单项目', '菜单项', '对话框', '复选框', '表单',
  '按钮', '文本', '列表', '表格', '行', '组', '窗口', '菜单',
  'AXTextArea', 'AXTextField', 'AXSearchField', 'AXStaticText', 'AXButton',
  'AXWindow', 'AXDialog', 'AXRow', 'AXCell', 'AXMenuItem', 'AXGroup',
  'text area', 'text field', 'static text', 'menu item', 'menu bar',
  'Button', 'button', 'TextArea', 'TextField', 'Window', 'Dialog', 'Row',
  'row', 'cell', 'table', 'list', 'group', 'container', 'text'];
const EDIT_ROLES = new Set(['搜索文本框', '文本区域', '文本框', '输入框',
  'AXTextArea', 'AXTextField', 'AXSearchField', 'text area', 'text field',
  'TextArea', 'TextField']);
const BUTTON_ROLES = new Set(['按钮', 'AXButton', 'Button', 'button']);
const ROW_ROLES = new Set(['行', 'AXRow', 'AXCell', 'Row', 'row', 'cell']);
const TEXT_ROLES = new Set(['文本', '静态文本', 'AXStaticText', 'static text', 'text']);
const WINDOW_NAMES = new Set(['微信', 'Weixin', 'WeChat']);
const PLAIN_MENU_NAMES = new Set(['微信', 'Weixin', 'WeChat', 'Chats', '聊天',
  'Window', '窗口', 'File', '文件', 'Edit', '编辑', 'Show', '显示', 'Help', '帮助']);
const PROFILE_LABEL = /^(?:Weixin ID|WeChat ID|微信号)[:：]?$/u;
const DRAFT_HASH_KEYS = ['draft_hash', 'payload_hash'];

class ControllerError extends Error {
  constructor(code, stage, effect = 'none') {
    super(code);
    this.name = 'ControllerError';
    this.code = code;
    this.stage = stage;
    this.effect = effect;
  }
}

function fail(code, stage, effect) { throw new ControllerError(code, stage, effect); }
function one(nodes, stage) {
  if (nodes.length !== 1) fail(nodes.length ? 'AMBIGUOUS_CONTROL' : 'CONTROL_NOT_FOUND', stage);
  return nodes[0];
}
function publicError(error, stage) {
  return {status: 'blocked', code: error instanceof ControllerError ? error.code : 'CUA_ACTION_FAILED',
    stage: error instanceof ControllerError ? error.stage : stage,
    effect: error instanceof ControllerError ? error.effect : 'unknown'};
}

function splitMetadata(body) {
  // Metadata is introduced by the documented CUA attribute delimiters. Do not
  // trim Value: contents: an unknown draft must never become an empty draft.
  const marker = /(?:, (ID|Value|值|Help|Description|描述|Secondary Actions):(?: |$))|(?: (Value|值|Help|Description|描述|Secondary Actions):(?: |$))/gu;
  const matches = [...body.matchAll(marker)];
  if (!matches.length) return {head: body.trim(), attrs: {}};
  const attrs = {};
  for (let i = 0; i < matches.length; ++i) {
    const start = matches[i].index + matches[i][0].length;
    const end = i + 1 < matches.length ? matches[i + 1].index : body.length;
    attrs[matches[i][1] || matches[i][2]] = body.slice(start, end);
  }
  return {head: body.slice(0, matches[0].index).trim(), attrs};
}

function isAXNodeBody(body) {
  const {head, attrs} = splitMetadata(body.replace(/^\(disabled\) /u, ''));
  if (ROLE_WORDS.some(word => head === word || head.startsWith(word + ' '))) return true;
  // Native CUA menus expose some entries without an explicit role. Restrict
  // this exception to known navigation labels or the window-list selector.
  return PLAIN_MENU_NAMES.has(head) ||
    (typeof attrs.ID === 'string' && /^makeKeyAndOrderFront:/u.test(attrs.ID));
}

async function sha256(value) {
  if (globalThis.crypto && globalThis.crypto.subtle && typeof TextEncoder !== 'undefined') {
    const result = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
    return [...new Uint8Array(result)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  }
  // The CommonJS offline runner has node:crypto. Loading in a CUA environment
  // never requires it if the evidence supplies the original draft string.
  if (typeof require === 'function') return require('node:crypto').createHash('sha256').update(value, 'utf8').digest('hex');
  fail('DRAFT_HASH_PROVIDER_UNAVAILABLE', 'draft_evidence');
}

function parseState(state) {
  if (typeof state !== 'string') fail('STATE_UNAVAILABLE', 'parse');
  const nodes = [];
  const stack = [];
  const records = [];
  let window = null;
  for (const raw of state.split(/\r?\n/u)) {
    const heading = /^Window: "(.*)", App: /u.exec(raw);
    if (heading) { window = heading[1]; continue; }
    const candidate = /^(\s*)(\d+)\s+(.+)$/u.exec(raw);
    const match = candidate && isAXNodeBody(candidate[3]) ? candidate : null;
    if (!match) {
      // CUA may emit a field's literal multiline value. Keep it intact instead
      // of turning a draft beginning with a newline into an empty draft.
      if (records.length && raw && !/^The focused UI element is /u.test(raw))
        records[records.length - 1].body += '\n' + raw;
      continue;
    }
    records.push({indentText: match[1], indexText: match[2], body: match[3]});
  }
  const seenIndices = new Set();
  for (const record of records) {
    const index = Number(record.indexText);
    if (!Number.isSafeInteger(index) || index < 0 || seenIndices.has(index))
      fail('INVALID_AX_INDEX', 'parse');
    seenIndices.add(index);
    const indent = record.indentText.replace(/\t/gu, '    ').length;
    const disabled = record.body.startsWith('(disabled) ');
    const {head, attrs} = splitMetadata(record.body.replace(/^\(disabled\) /u, ''));
    const role = ROLE_WORDS.find(word => head === word || head.startsWith(word + ' ')) || '';
    const label = role ? head.slice(role.length).trim() : head;
    while (stack.length && stack[stack.length - 1].indent >= indent) stack.pop();
    const node = {index, indent, role, label, attrs, disabled, raw_head: head,
      parent: stack.length ? stack[stack.length - 1] : null, children: []};
    if (node.parent) node.parent.children.push(node);
    nodes.push(node);
    stack.push(node);
  }
  if (!nodes.length) fail('UI_UNREADABLE', 'parse');
  return {window, nodes};
}

function valueOf(node) {
  if (Object.hasOwn(node.attrs, 'Value')) return node.attrs.Value;
  if (Object.hasOwn(node.attrs, '值')) return node.attrs['值'];
  return null;
}
function displayOf(node) { return valueOf(node) === null ? node.label : valueOf(node); }
function descendants(node) {
  const found = [];
  const pending = [...node.children];
  while (pending.length) { const next = pending.shift(); found.push(next); pending.push(...next.children); }
  return found;
}
function ancestry(node) {
  const found = [];
  for (let next = node.parent; next; next = next.parent) found.push(next);
  return found;
}
function identifier(node) { return node.attrs.ID || ''; }
function described(node) {
  return [identifier(node), node.label, node.attrs.Description || '', node.attrs['描述'] || '', node.attrs.Help || ''].join(' ');
}
function searchCandidates(tree) {
  return tree.nodes.filter(node => EDIT_ROLES.has(node.role) &&
    /search|搜索/iu.test(described(node)));
}
function searchLaunchers(tree) {
  return tree.nodes.filter(node => BUTTON_ROLES.has(node.role) && /^(?:Search|搜索)$/u.test(node.label));
}
function composerCandidates(tree) {
  return tree.nodes.filter(node => EDIT_ROLES.has(node.role) &&
    !searchCandidates({nodes: [node]}).length &&
    (/chat[_-]?input|message[_-]?(?:input|editor)|composer|聊天输入|消息输入/iu.test(described(node)) ||
     node.role === 'AXTextArea' || node.role === 'text area' || node.role === '文本区域' || node.role === 'TextArea'));
}
function targetNames(target) {
  return [...new Set([target.display_name, target.remark, target.nickname,
    ...(Array.isArray(target.names) ? target.names : [])].filter(value => typeof value === 'string' && value))];
}
function targetIds(target) {
  return [...new Set([target.recipient_id, target.alias].filter(value => typeof value === 'string' && value))];
}
function normalizeTarget(target) {
  if (!target || typeof target.recipient_id !== 'string' || !target.recipient_id ||
      typeof target.account_id !== 'string' || !target.account_id ||
      !['person', 'group'].includes(target.kind)) fail('INVALID_TARGET', 'target');
  if (target.kind !== 'person') fail('GROUP_PROFILE_ADAPTER_REQUIRED', 'target');
  const result = {...target};
  result.search_text = target.search_text || target.alias || target.display_name || target.recipient_id;
  if (typeof result.search_text !== 'string' || !result.search_text) fail('INVALID_SEARCH_TEXT', 'target');
  return result;
}

function selectWindowMenu(tree) {
  return one(tree.nodes.filter(node => /^(?:Window|窗口)$/u.test(node.raw_head)), 'window_menu');
}
function selectMainWindow(tree) {
  const chats = tree.nodes.filter(node => /^(?:Chats|聊天)$/u.test(node.label) &&
    (!node.role || node.role === '菜单项' || node.role === 'AXMenuItem'));
  if (chats.length) return one(chats, 'main_window_chats');
  return one(tree.nodes.filter(node => WINDOW_NAMES.has(node.label) &&
    /makeKeyAndOrderFront/u.test(identifier(node))), 'main_window_item');
}

function selectResult(tree, target, searchIndex) {
  const names = new Set([...targetNames(target), ...targetIds(target), target.search_text]);
  const candidates = new Map();
  for (const node of tree.nodes) {
    if (node.index === searchIndex || EDIT_ROLES.has(node.role) || !names.has(displayOf(node))) continue;
    if (ancestry(node).some(parent => EDIT_ROLES.has(parent.role))) continue;
    const row = [node, ...ancestry(node)].find(parent => ROW_ROLES.has(parent.role) || BUTTON_ROLES.has(parent.role));
    if (!row || row.disabled) continue;
    const inResults = /search|搜索|results|结果/iu.test(described(row)) ||
      ancestry(row).some(parent => /search|搜索|results|结果/iu.test(described(parent)));
    if (!inResults && !ROW_ROLES.has(row.role)) continue;
    candidates.set(row.index, row);
  }
  const rows = [...candidates.values()];
  const ids = new Set(targetIds(target));
  const withExactId = rows.filter(row => [row, ...descendants(row)].some(node => {
    const text = displayOf(node);
    if (ids.has(text)) return true;
    const inline = typeof text === 'string' && /^(?:Weixin ID|WeChat ID|微信号)[:：] ?(.+)$/u.exec(text);
    return !!inline && ids.has(inline[1]);
  }));
  return one(withExactId.length ? withExactId : rows, 'contact_result');
}

function selectHeader(tree, target) {
  const names = new Set(targetNames(target));
  const candidates = tree.nodes.filter(node => names.has(displayOf(node)) &&
    (BUTTON_ROLES.has(node.role) || TEXT_ROLES.has(node.role)) &&
    !ancestry(node).some(parent => ROW_ROLES.has(parent.role) || /message[_-]?list|history|sidebar|session[_-]?list/iu.test(described(parent))) &&
    /chat[_-]?(?:title|header|name)|peer[_-]?name|contact[_-]?(?:title|header)|聊天标题|header/iu.test(
      [described(node), ...ancestry(node).map(described)].join(' ')));
  // Prefer the single interactive header over its static child representation.
  const buttons = candidates.filter(node => BUTTON_ROLES.has(node.role));
  return one(buttons.length ? buttons : candidates, 'chat_header');
}

function profileId(tree) {
  const matches = [];
  for (const node of tree.nodes) {
    const text = displayOf(node);
    if (typeof text !== 'string') continue;
    const inline = /^(?:Weixin ID|WeChat ID|微信号)[:：] ?(.+)$/u.exec(text);
    if (inline) { matches.push(inline[1]); continue; }
    if (!PROFILE_LABEL.test(text) || !node.parent) continue;
    const siblings = node.parent.children;
    const next = siblings[siblings.indexOf(node) + 1];
    if (next && TEXT_ROLES.has(next.role)) {
      const value = displayOf(next);
      if (typeof value === 'string' && value) matches.push(value);
    }
  }
  const values = [...new Set(matches)];
  if (values.length !== 1) fail(values.length ? 'PROFILE_ID_AMBIGUOUS' : 'PROFILE_ID_UNAVAILABLE', 'profile');
  return values[0];
}

function inspectComposer(tree) {
  const composer = one(composerCandidates(tree), 'composer');
  const draft = valueOf(composer);
  if (draft === null) fail('DRAFT_UNREADABLE', 'composer');
  const sends = tree.nodes.filter(node => BUTTON_ROLES.has(node.role) &&
    (/^(?:Send|发送)$/u.test(node.label) || /^(?:send_button|chat_send_button)$/iu.test(identifier(node))));
  const send = one(sends, 'send');
  return {composer, send, draft};
}

class CuaChatController {
  constructor(app, {verifyIdentity, beforeCommit, allowCommit = false, deadlineEpochMs = Infinity} = {}) {
    if (!app || typeof app.getAXState !== 'function' || typeof app.click !== 'function' ||
        typeof app.setValue !== 'function') fail('APP_INTERFACE_UNAVAILABLE', 'constructor');
    this.app = app;
    this.verifyIdentity = verifyIdentity;
    this.beforeCommit = beforeCommit;
    this.allowCommit = allowCommit === true;
    this.deadlineEpochMs = deadlineEpochMs;
    this.operations = new Map();
    this.prepared = new WeakMap();
    this.stage = 'initial';
  }
  checkTime(stage) {
    this.stage = stage;
    if (typeof this.deadlineEpochMs !== 'number' || Number.isNaN(this.deadlineEpochMs) || Date.now() >= this.deadlineEpochMs)
      fail('DEADLINE_EXPIRED', stage);
  }
  async read(stage) {
    this.checkTime(stage);
    const state = await this.app.getAXState({...READ_OPTIONS});
    this.checkTime(stage);
    return {state, tree: parseState(state)};
  }
  async click(node, stage) {
    this.checkTime(stage);
    await this.app.click(node.index);
    return this.read(stage + '_after');
  }
  async set(node, text, stage) {
    this.checkTime(stage);
    await this.app.setValue(node.index, text);
    return this.read(stage + '_after');
  }
  async ensureMainWindow() {
    let current = await this.read('main_window');
    if (searchCandidates(current.tree).length || searchLaunchers(current.tree).length || composerCandidates(current.tree).length) return current;
    current = await this.click(selectWindowMenu(current.tree), 'window_menu');
    current = await this.click(selectMainWindow(current.tree), 'main_window_select');
    if (!searchCandidates(current.tree).length && !searchLaunchers(current.tree).length && !composerCandidates(current.tree).length)
      fail('MAIN_WINDOW_UNREADABLE', 'main_window');
    return current;
  }
  async closeProfile(current) {
    const close = current.tree.nodes.filter(node => /^(?:Close|关闭)$/u.test(node.label) && BUTTON_ROLES.has(node.role));
    if (close.length === 1) return this.click(close[0], 'profile_close');
    if (typeof this.app.pressKey !== 'function') fail('PROFILE_CLOSE_UNAVAILABLE', 'profile_close');
    // Escape is used only after a positively identified profile was opened.
    await this.app.pressKey('Escape');
    return this.read('profile_close_after');
  }
  async verifyCurrentProfile(current, target) {
    current = await this.click(selectHeader(current.tree, target), 'profile_open');
    const actualId = profileId(current.tree);
    current = await this.closeProfile(current);
    if (!targetIds(target).includes(actualId)) fail('RECIPIENT_MISMATCH', 'profile');
    selectHeader(current.tree, target);
    return current;
  }
  async openChat(targetInput) {
    try {
      const target = normalizeTarget(targetInput);
      let current = await this.ensureMainWindow();
      let searches = searchCandidates(current.tree);
      if (!searches.length) {
        current = await this.click(one(searchLaunchers(current.tree), 'search_launcher'), 'search_open');
        searches = searchCandidates(current.tree);
      }
      current = await this.set(one(searches, 'search'), target.search_text, 'search_write');
      const search = one(searchCandidates(current.tree), 'search_readback');
      if (valueOf(search) !== target.search_text) fail('SEARCH_READBACK_MISMATCH', 'search');
      current = await this.click(selectResult(current.tree, target, search.index), 'contact_open');
      current = await this.verifyCurrentProfile(current, target);
      inspectComposer(current.tree);
      return {status: 'opened', recipient_id: target.recipient_id};
    } catch (error) { return publicError(error, this.stage); }
  }
  async identity(current, target, phase, expectedDraft) {
    if (typeof this.verifyIdentity !== 'function') fail('IDENTITY_PROVIDER_REQUIRED', phase);
    const evidence = await this.verifyIdentity({state: current.state, target: {...target}, phase});
    if (!evidence || evidence.account_id !== target.account_id || evidence.recipient_id !== target.recipient_id ||
        typeof evidence.binding_id !== 'string' || !evidence.binding_id) fail('IDENTITY_UNVERIFIED', phase);
    // A text AX snapshot cannot unambiguously serialize arbitrary multiline
    // editor contents (a draft can itself look like an AX node). Only an
    // independent raw native Value/hash may authorize writing, deleting or
    // sending. These fields must describe the observed editor, never echo the
    // requested message/hash. A legacy identity-only provider fails closed.
    let draftKnown = false;
    if (Object.hasOwn(evidence, 'draft')) {
      if (typeof evidence.draft !== 'string') fail('DRAFT_EVIDENCE_INVALID', phase);
      draftKnown = true;
      if (evidence.draft !== expectedDraft) fail('DRAFT_EVIDENCE_MISMATCH', phase);
    }
    const hashKeys = DRAFT_HASH_KEYS.filter(key => Object.hasOwn(evidence, key));
    if (hashKeys.length) {
      const expectedHash = await sha256(expectedDraft);
      for (const key of hashKeys) {
        if (typeof evidence[key] !== 'string' || !/^[a-f0-9]{64}$/u.test(evidence[key]))
          fail('DRAFT_EVIDENCE_INVALID', phase);
        if (evidence[key] !== expectedHash) fail('DRAFT_EVIDENCE_MISMATCH', phase);
      }
      draftKnown = true;
    }
    if (!draftKnown) fail('DRAFT_EVIDENCE_REQUIRED', phase);
    return evidence.binding_id;
  }
  async prepare(targetInput, payload, {operationId} = {}) {
    let operation;
    try {
      const target = normalizeTarget(targetInput);
      if (typeof payload !== 'string' || !payload || typeof operationId !== 'string' || !operationId)
        fail('INVALID_MESSAGE', 'prepare');
      if (this.operations.has(operationId)) fail('OPERATION_ALREADY_USED', 'prepare');
      operation = {operationId, target, payload, phase: 'NAVIGATING'};
      this.operations.set(operationId, operation);
      const opened = await this.openChat(target);
      if (opened.status !== 'opened') fail(opened.code, opened.stage, opened.effect);
      let current = await this.read('prepare_read');
      selectHeader(current.tree, target);
      let controls = inspectComposer(current.tree);
      if (controls.draft !== '') fail('EXISTING_DRAFT', 'prepare');
      operation.binding = await this.identity(current, target, 'prepare_identity', '');
      operation.phase = 'WRITE_STARTED';
      current = await this.set(controls.composer, payload, 'draft_write');
      controls = inspectComposer(current.tree);
      if (controls.draft !== payload) fail('DRAFT_WRITE_UNCERTAIN', 'prepare', 'unknown');
      if (controls.send.disabled) fail('SEND_DISABLED', 'prepare', 'draft_written');
      const binding = await this.identity(current, target, 'prepare_readback', payload);
      if (binding !== operation.binding) fail('IDENTITY_DRIFT', 'prepare', 'draft_written');
      const token = Object.freeze({operation_id: operationId});
      operation.phase = 'PREPARED';
      operation.token = token;
      this.prepared.set(token, operation);
      return {status: 'prepared', operation_id: operationId, token};
    } catch (error) {
      if (operation && operation.phase !== 'PREPARED') operation.phase = operation.phase === 'WRITE_STARTED' ? 'WRITE_UNCERTAIN' : 'ABORTED';
      return publicError(error, this.stage);
    }
  }
  getPrepared(token) {
    const operation = token && this.prepared.get(token);
    if (!operation || operation.token !== token) fail('PREPARED_TOKEN_INVALID', 'prepared');
    return operation;
  }
  async cleanup(token) {
    let operation;
    try {
      operation = this.getPrepared(token);
      if (operation.phase !== 'PREPARED') fail('CLEANUP_NOT_ALLOWED', 'cleanup');
      operation.phase = 'CLEANUP_VALIDATING';
      let current = await this.read('cleanup_read');
      selectHeader(current.tree, operation.target);
      if (await this.identity(current, operation.target, 'cleanup_identity', operation.payload) !== operation.binding)
        fail('IDENTITY_DRIFT', 'cleanup');
      const controls = inspectComposer(current.tree);
      if (controls.draft !== operation.payload) fail('DRAFT_CHANGED', 'cleanup');
      operation.phase = 'CLEANUP_STARTED';
      current = await this.set(controls.composer, '', 'draft_cleanup');
      if (inspectComposer(current.tree).draft !== '') fail('CLEANUP_UNCERTAIN', 'cleanup', 'unknown');
      if (await this.identity(current, operation.target, 'cleanup_readback', '') !== operation.binding)
        fail('IDENTITY_DRIFT', 'cleanup', 'unknown');
      operation.phase = 'CLEANED';
      return {status: 'cleaned', operation_id: operation.operationId};
    } catch (error) {
      if (operation && operation.phase === 'CLEANUP_VALIDATING') operation.phase = 'PREPARED';
      return publicError(error, this.stage);
    }
  }
  async commit(token) {
    let operation;
    try {
      operation = this.getPrepared(token);
      if (!this.allowCommit) fail('COMMIT_DISABLED', 'commit');
      if (operation.phase !== 'PREPARED') fail('COMMIT_ALREADY_ATTEMPTED', 'commit');
      if (typeof this.beforeCommit !== 'function') fail('COMMIT_JOURNAL_REQUIRED', 'commit');
      operation.phase = 'COMMIT_VALIDATING';
      let current = await this.read('commit_read');
      current = await this.verifyCurrentProfile(current, operation.target);
      if (await this.identity(current, operation.target, 'commit_identity', operation.payload) !== operation.binding)
        fail('IDENTITY_DRIFT', 'commit');
      let controls = inspectComposer(current.tree);
      if (controls.draft !== operation.payload || controls.send.disabled) fail('DRAFT_OR_SEND_CHANGED', 'commit');
      // The hook must durably and atomically consume the receipt operation.
      operation.phase = 'COMMIT_RECORDING';
      const receipt = await this.beforeCommit({operation_id: operation.operationId,
        recipient_id: operation.target.recipient_id});
      if (!receipt || receipt.operation_id !== operation.operationId ||
          !(receipt.send_allowed === true || receipt.status === 'submit_recorded') ||
          typeof receipt.committed_at !== 'number' || !Number.isFinite(receipt.committed_at))
        fail('COMMIT_NOT_RECORDED', 'commit');
      operation.phase = 'COMMIT_STARTED';
      current = await this.read('commit_final_read');
      selectHeader(current.tree, operation.target);
      if (await this.identity(current, operation.target, 'commit_final_identity', operation.payload) !== operation.binding)
        fail('IDENTITY_DRIFT', 'commit', 'commit_recorded');
      controls = inspectComposer(current.tree);
      if (controls.draft !== operation.payload || controls.send.disabled)
        fail('DRAFT_OR_SEND_CHANGED', 'commit', 'commit_recorded');
      await this.click(controls.send, 'send_once');
      operation.phase = 'SUBMITTED_UNVERIFIED';
      return {status: 'submitted_unverified', operation_id: operation.operationId,
        committed_at: receipt.committed_at, retry_allowed: false};
    } catch (error) {
      if (operation && ['COMMIT_RECORDING', 'COMMIT_STARTED'].includes(operation.phase)) operation.phase = 'UNKNOWN';
      else if (operation && operation.phase === 'COMMIT_VALIDATING') operation.phase = 'PREPARED';
      return {...publicError(error, this.stage), retry_allowed: false};
    }
  }
}

const exported = {CuaChatController, ControllerError, READ_OPTIONS, parseState,
  valueOf, searchCandidates, composerCandidates, selectMainWindow, selectResult,
  selectHeader, profileId, inspectComposer};
if (typeof module !== 'undefined' && module.exports) module.exports = exported;
else globalThis.WechatCuaController = exported;
