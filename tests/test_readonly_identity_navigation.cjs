'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {selectAppMenu, selectSettingsMenu, selectMyAccountButton, openAccount} =
  require('../scripts/readonly_identity_navigation.cjs');

const chinese = 'Window: "Weixin", App: 微信.\n 2 按钮 Weixin, ID: tabbar_mainframe\n 12 按钮 Search\n 30 微信\n 31 File\n 32 Edit\n 34 Window\n 35 Help';

test('Chinese app menu is 30, never the sidebar button 2', () => {
  assert.equal(selectAppMenu(chinese), 30);
  assert.equal(selectSettingsMenu(' 2 按钮 Settings\n 42 Settings…'), 42);
  assert.equal(selectMyAccountButton(' 7 静态文本 My Account\n 53 按钮 My Account, ID: account_button'), 53);
});

test('English plain app menus and exact supported Settings labels', () => {
  assert.equal(selectAppMenu(' 2 Button Weixin, ID: sidebar\n 30 Weixin'), 30);
  assert.equal(selectAppMenu(' 3 button WeChat\n 31 WeChat'), 31);
  for (const label of ['Settings', '设置', '偏好设置']) {
    assert.equal(selectSettingsMenu(' 42 ' + label), 42);
  }
  assert.equal(selectMyAccountButton(' 53 Button My Account'), 53);
  assert.equal(selectSettingsMenu(' 42 Settings, ID: preferencesMenuItem'), 42);
  assert.equal(selectMyAccountButton(' 53 按钮 My Account, Secondary Actions: Raise'), 53);
});

test('missing or ambiguous selectors stop rather than guess', () => {
  assert.throws(() => selectAppMenu(' 2 按钮 Weixin, ID: tabbar_mainframe'), {code: 'MISSING_SELECTOR'});
  assert.throws(() => selectAppMenu(' 30 微信\n 31 Weixin'), {code: 'AMBIGUOUS_SELECTOR'});
  assert.throws(() => selectSettingsMenu(' 41 Settings\n 42 设置'), {code: 'AMBIGUOUS_SELECTOR'});
  assert.throws(() => selectMyAccountButton(' 51 按钮 My Account\n 52 Button My Account'), {code: 'AMBIGUOUS_SELECTOR'});
  assert.throws(() => selectMyAccountButton(' 51 按钮 My Account Details'), {code: 'MISSING_SELECTOR'});
});

test('expired deadline or insufficient reserve makes zero app calls', async () => {
  const calls = [];
  const app = {click: async () => calls.push('click'), getAXState: async () => calls.push('state')};
  assert.equal((await openAccount(app, Date.now() - 1)).status, 'deadline');
  assert.equal((await openAccount(app, Date.now() + 7990)).status, 'reserve');
  assert.deepEqual(calls, []);
});

test('each numeric click derives from the fresh preceding state', async () => {
  const states = [chinese, ' 41 Settings, ID: preferencesMenuItem',
    ' 72 按钮 My Account, Secondary Actions: Raise', ' 90 静态文本 Account'];
  const calls = [];
  const app = {
    async getAXState(options) {
      assert.deepEqual(options, {emit: false, disableDiffing: true});
      calls.push('state');
      return states.shift();
    },
    async click(index) {
      assert.equal(typeof index, 'number');
      calls.push(index);
    },
  };
  const result = await openAccount(app, Date.now() + 90000);
  assert.equal(result.status, 'completed');
  assert.equal(result.stage, 'account_state');
  assert.deepEqual(calls, ['state', 30, 'state', 41, 'state', 72, 'state']);
  assert.deepEqual(Object.keys(result).sort(), ['elapsed_ms', 'stage', 'status']);
});

test('a click exception stops without retry and never exposes error UI content', async () => {
  const states = [chinese, ' 41 Settings'];
  const calls = [];
  const app = {
    async getAXState() { calls.push('state'); return states.shift(); },
    async click(index) {
      calls.push(index);
      if (index === 41) throw new Error('synthetic private UI content');
    },
  };
  const result = await openAccount(app, Date.now() + 90000);
  assert.deepEqual(calls, ['state', 30, 'state', 41]);
  assert.equal(result.status, 'error');
  assert.equal(result.stage, 'settings_click');
  assert.doesNotMatch(JSON.stringify(result), /synthetic|private|content/u);
});
