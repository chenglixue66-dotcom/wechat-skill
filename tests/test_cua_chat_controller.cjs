'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {CuaChatController, parseState, profileId, inspectComposer, selectResult} =
  require('../scripts/cua_chat_controller.cjs');

const target = {recipient_id: 'wxid_alice', alias: 'alice_id', display_name: 'Alice',
  nickname: 'Alice', kind: 'person', account_id: 'wxid_self', search_text: 'Alice'};
const identity = (current, draft) => ({account_id: current.account_id,
  recipient_id: current.recipient_id, binding_id: 'synthetic-process-window-composer', draft});

// Fake App advances every index at every read. An old index causes an error,
// so passing these scenarios requires choosing each action from fresh state.
class FakeApp {
  constructor(options = {}) {
    this.options = options;
    this.mode = options.article ? 'article' : 'chat';
    this.query = '';
    this.draft = options.draft || '';
    this.revision = 0;
    this.indices = new Map();
    this.calls = [];
    this.sent = [];
  }
  async getAXState(options) {
    assert.deepEqual(options, {emit: false, disableDiffing: true});
    this.calls.push('read');
    if (this.failRead) { this.failRead = false; throw new Error('private UI error'); }
    this.indices.clear();
    let next = ++this.revision * 100;
    const line = (indent, body, action) => {
      const id = next++;
      if (action) this.indices.set(id, action);
      return ' '.repeat(indent) + id + ' ' + body;
    };
    const lines = ['Window: "' + (this.mode === 'article' ? 'WeChat (Window)' : 'Weixin') + '", App: 微信.'];
    if (this.mode === 'article') {
      lines.push(line(0, '标准窗口 WeChat (Window)'), line(0, 'menu bar'), line(2, 'Window', 'window_menu'));
    } else if (this.mode === 'window_menu') {
      lines.push(line(0, 'Window, Secondary Actions: Cancel, Pick'), line(2, '菜单'),
        line(4, 'WeChat (Window), ID: makeKeyAndOrderFront:', 'article'),
        line(4, 'Weixin, ID: makeKeyAndOrderFront:', 'main_window'));
    } else if (this.mode === 'search') {
      lines.push(line(0, '标准窗口 Weixin'), line(2, 'TextField Search Value: ' + this.query + ', ID: searchField', 'search'),
        line(2, 'list, ID: search_results'), line(4, 'row', 'result'), line(6, 'text Value: Alice'));
      if (this.options.duplicate) lines.push(line(4, 'row', 'result_duplicate'), line(6, 'text Value: Alice'));
    } else if (this.mode === 'profile') {
      lines.push(line(0, 'group, ID: contact_profile'), line(2, 'text Value: Weixin ID:'),
        line(2, 'text Value: ' + (this.options.profile || target.alias)), line(2, 'Button Close', 'close'));
    } else {
      lines.push(line(0, '标准窗口 Weixin'), line(2, 'group, ID: chat_header'),
        line(4, 'Button Alice, ID: chat_title', 'header'),
        line(2, 'TextArea Value: ' + this.draft + ', ID: chat_editor', 'composer'),
        line(2, (this.draft ? '' : '(disabled) ') + 'Button Send, ID: send_button', 'send'),
        line(2, 'TextField Search Value: ' + this.query + ', ID: searchField', 'search'));
    }
    return lines.join('\n');
  }
  async click(index) {
    const action = this.indices.get(index);
    assert.ok(action, 'click must use current AX index');
    this.calls.push(action);
    if (action === 'window_menu') this.mode = 'window_menu';
    else if (action === 'main_window') this.mode = 'chat';
    else if (action === 'result') this.mode = 'chat';
    else if (action === 'header') this.mode = 'profile';
    else if (action === 'close') this.mode = 'chat';
    else if (action === 'send') {
      this.sent.push(this.draft); this.draft = '';
      if (this.options.sendReadFailure) this.failRead = true;
    } else throw new Error('unexpected fake click');
  }
  async setValue(index, text) {
    const action = this.indices.get(index);
    assert.ok(action, 'write must use current AX index');
    this.calls.push('set_' + action);
    if (action === 'search') { this.query = text; this.mode = 'search'; }
    else if (action === 'composer') this.draft = text;
    else throw new Error('unexpected fake write');
  }
}

function make(options = {}, controllerOptions = {}) {
  const app = new FakeApp(options);
  return {app, controller: new CuaChatController(app, {
    verifyIdentity: async ({target: current}) => identity(current, app.draft), ...controllerOptions})};
}
async function prepared(controller, payload = 'See you soon', operationId = 'synthetic-op') {
  const result = await controller.prepare(target, payload, {operationId});
  assert.equal(result.status, 'prepared', JSON.stringify(result));
  return result.token;
}

test('switches from article window to real main window and uses fresh indices throughout', async () => {
  const {app, controller} = make({article: true});
  await prepared(controller);
  assert.equal(app.draft, 'See you soon');
  assert.equal(app.calls.filter(call => call === 'main_window').length, 1);
  assert.equal(app.calls.filter(call => call === 'set_composer').length, 1);
  assert.deepEqual(app.sent, []);
});

test('profile parser uses semantic label and adjacent value with arbitrary child count', () => {
  const tree = parseState('1 group, ID: profile\n 2 text Value: Another label\n 3 text Value: Weixin ID:\n 4 text Value: alice_id\n 5 Button Close');
  assert.equal(profileId(tree), 'alice_id');
  assert.equal(profileId(parseState('1 text Value: 微信号：alice_id')), 'alice_id');
  assert.throws(() => profileId(parseState('1 group\n 2 text Value: Weixin ID:\n 3 text Value: alice_id \n 4 text Value: 微信号：other')), {code: 'PROFILE_ID_AMBIGUOUS'});
});

test('unknown draft is not treated as empty and whitespace draft is preserved', () => {
  assert.throws(() => inspectComposer(parseState('1 TextArea, ID: composer\n2 Button Send')), {code: 'DRAFT_UNREADABLE'});
  assert.equal(inspectComposer(parseState('1 TextArea Value:  , ID: composer\n2 Button Send')).draft, ' ');
  assert.equal(inspectComposer(parseState('1 TextArea Value:\n2 Button Send')).draft, '');
});

test('ambiguous search result and wrong profile never write a draft', async () => {
  for (const [options, code] of [[{duplicate: true}, 'AMBIGUOUS_CONTROL'], [{profile: 'wrong_alias'}, 'RECIPIENT_MISMATCH']]) {
    const {app, controller} = make(options);
    const result = await controller.prepare(target, 'Message', {operationId: 'one'});
    assert.equal(result.code, code);
    assert.equal(app.calls.includes('set_composer'), false);
    assert.deepEqual(app.sent, []);
  }
});

test('display-name duplicates can be disambiguated only by an exact ID in the result row', () => {
  const tree = parseState('1 list, ID: search_results\n 2 row\n  3 text Value: Alice\n  4 text Value: other_id\n 5 row\n  6 text Value: Alice\n  7 text Value: 微信号：alice_id');
  assert.equal(selectResult(tree, target, -1).index, 5);
});

test('existing drafts and wrong login identity are never overwritten', async () => {
  const first = make({draft: 'Keep my draft'});
  assert.equal((await first.controller.prepare(target, 'Message', {operationId: 'draft'})).code, 'EXISTING_DRAFT');
  assert.equal(first.app.draft, 'Keep my draft');
  const second = make({}, {verifyIdentity: async () => ({account_id: 'other', recipient_id: target.recipient_id, binding_id: 'x'})});
  assert.equal((await second.controller.prepare(target, 'Message', {operationId: 'identity'})).code, 'IDENTITY_UNVERIFIED');
  assert.equal(second.app.draft, '');
});

test('literal multiline drafts are preserved and newline-leading drafts are not empty', async () => {
  const first = make({draft: '\nKeep my draft'});
  assert.equal((await first.controller.prepare(target, 'Message', {operationId: 'multiline-existing'})).code, 'EXISTING_DRAFT');
  assert.equal(first.app.draft, '\nKeep my draft');
  const second = make();
  const token = await prepared(second.controller, 'First line\nSecond line', 'multiline-body');
  assert.equal(second.app.draft, 'First line\nSecond line');
  assert.equal((await second.controller.cleanup(token)).status, 'cleaned');
});

test('numeric draft continuation is text and forged AX-role continuation cannot authorize a write', async () => {
  const first = make({draft: '\n2026 年待办正文'});
  const plain = await first.controller.prepare(target, 'Message', {operationId: 'numeric-draft'});
  assert.equal(plain.code, 'EXISTING_DRAFT');
  assert.equal(first.app.draft, '\n2026 年待办正文');
  assert.equal(first.app.calls.includes('set_composer'), false);

  const second = make({draft: '\n2026 Button Existing draft'});
  const forged = await second.controller.prepare(target, 'Message', {operationId: 'forged-node-draft'});
  assert.equal(forged.code, 'DRAFT_EVIDENCE_MISMATCH');
  assert.equal(second.app.draft, '\n2026 Button Existing draft');
  assert.equal(second.app.calls.includes('set_composer'), false);
});

test('identity-only legacy callbacks cannot authorize an empty draft write', async () => {
  const {app, controller} = make({}, {verifyIdentity: async ({target: current}) => ({
    account_id: current.account_id, recipient_id: current.recipient_id, binding_id: 'legacy-binding'})});
  assert.equal((await controller.prepare(target, 'Message', {operationId: 'legacy-callback'})).code, 'DRAFT_EVIDENCE_REQUIRED');
  assert.equal(app.draft, '');
  assert.equal(app.calls.includes('set_composer'), false);
});

test('independent observed draft hashes are accepted, malformed or contradictory evidence is rejected', async () => {
  const crypto = require('node:crypto');
  const hashed = make();
  hashed.controller.verifyIdentity = async ({target: current}) => {
    const evidence = identity(current, hashed.app.draft);
    delete evidence.draft;
    evidence.payload_hash = crypto.createHash('sha256').update(hashed.app.draft, 'utf8').digest('hex');
    return evidence;
  };
  const token = await prepared(hashed.controller, 'Hashed draft ✓', 'hash-evidence');
  assert.equal((await hashed.controller.cleanup(token)).status, 'cleaned');

  const bad = make();
  bad.controller.verifyIdentity = async ({target: current}) => ({...identity(current, bad.app.draft), draft_hash: 'bad hash'});
  assert.equal((await bad.controller.prepare(target, 'Message', {operationId: 'bad-hash'})).code, 'DRAFT_EVIDENCE_INVALID');
  assert.equal(bad.app.calls.includes('set_composer'), false);

  const conflict = make();
  conflict.controller.verifyIdentity = async ({target: current}) => ({...identity(current, conflict.app.draft), draft_hash: 'a'.repeat(64)});
  assert.equal((await conflict.controller.prepare(target, 'Message', {operationId: 'contradictory-hash'})).code, 'DRAFT_EVIDENCE_MISMATCH');
  assert.equal(conflict.app.calls.includes('set_composer'), false);
});

test('cleanup and commit check native raw draft even when the text snapshot is forged empty', async () => {
  const cleanup = make();
  const cleanupToken = await prepared(cleanup.controller, 'Owned draft', 'raw-cleanup');
  cleanup.app.draft = 'Owned draft';
  cleanup.controller.verifyIdentity = async ({target: current}) => identity(current, 'User replaced it');
  assert.equal((await cleanup.controller.cleanup(cleanupToken)).code, 'DRAFT_EVIDENCE_MISMATCH');
  assert.equal(cleanup.app.draft, 'Owned draft');

  let journalCalls = 0;
  const commit = make({}, {allowCommit: true, beforeCommit: async () => { journalCalls++; }});
  const commitToken = await prepared(commit.controller, 'Owned draft', 'raw-commit');
  commit.controller.verifyIdentity = async ({target: current}) => identity(current, 'User replaced it');
  assert.equal((await commit.controller.commit(commitToken)).code, 'DRAFT_EVIDENCE_MISMATCH');
  assert.equal(journalCalls, 0);
  assert.deepEqual(commit.app.sent, []);
});

test('cleanup clears only this prepared exact draft and rejects a changed draft', async () => {
  const first = make(); const token = await prepared(first.controller);
  first.app.draft = 'User replaced it';
  assert.equal((await first.controller.cleanup(token)).code, 'DRAFT_EVIDENCE_MISMATCH');
  assert.equal(first.app.draft, 'User replaced it');
  const second = make(); const token2 = await prepared(second.controller);
  assert.equal((await second.controller.cleanup(token2)).status, 'cleaned');
  assert.equal(second.app.draft, '');
  assert.deepEqual(second.app.sent, []);
});

test('commit is disabled by default and requires durable receipt hook', async () => {
  const first = make(); const token = await prepared(first.controller);
  assert.equal((await first.controller.commit(token)).code, 'COMMIT_DISABLED');
  assert.deepEqual(first.app.sent, []);
  const second = make({}, {allowCommit: true}); const token2 = await prepared(second.controller);
  assert.equal((await second.controller.commit(token2)).code, 'COMMIT_JOURNAL_REQUIRED');
  assert.deepEqual(second.app.sent, []);
});

test('real submission is once only after durable mark-submit and remains unverified', async () => {
  let recorded = 0;
  const {app, controller} = make({}, {allowCommit: true,
    beforeCommit: async ({operation_id}) => ({status: 'submit_recorded', operation_id,
      committed_at: Date.now() / 1000 + ++recorded * 0.001})});
  const token = await prepared(controller);
  const result = await controller.commit(token);
  assert.equal(result.status, 'submitted_unverified');
  assert.equal(recorded, 1);
  assert.deepEqual(app.sent, ['See you soon']);
  assert.equal((await controller.commit(token)).code, 'COMMIT_ALREADY_ATTEMPTED');
  assert.deepEqual(app.sent, ['See you soon']);
});

test('post-send read failure and durable-hook exception never allow re-submission', async () => {
  const first = make({sendReadFailure: true}, {allowCommit: true,
    beforeCommit: async ({operation_id}) => ({status: 'submit_recorded', operation_id, committed_at: Date.now() / 1000})});
  const token = await prepared(first.controller);
  assert.equal((await first.controller.commit(token)).status, 'blocked');
  assert.equal((await first.controller.commit(token)).code, 'COMMIT_ALREADY_ATTEMPTED');
  assert.deepEqual(first.app.sent, ['See you soon']);
  let called = 0;
  const second = make({}, {allowCommit: true, beforeCommit: async () => { called++; throw new Error('private local path'); }});
  const token2 = await prepared(second.controller);
  const result = await second.controller.commit(token2);
  assert.doesNotMatch(JSON.stringify(result), /private local path/u);
  assert.equal((await second.controller.commit(token2)).code, 'COMMIT_ALREADY_ATTEMPTED');
  assert.equal(called, 1);
  assert.deepEqual(second.app.sent, []);
});

test('concurrent commits cannot navigate or submit twice', async () => {
  let recorded = 0;
  const {app, controller} = make({}, {allowCommit: true,
    beforeCommit: async ({operation_id}) => ({status: 'submit_recorded', operation_id, committed_at: Date.now() / 1000 + ++recorded * 0.001})});
  const token = await prepared(controller);
  const results = await Promise.all([controller.commit(token), controller.commit(token)]);
  assert.equal(results.filter(result => result.status === 'submitted_unverified').length, 1);
  assert.equal(recorded, 1);
  assert.deepEqual(app.sent, ['See you soon']);
});
