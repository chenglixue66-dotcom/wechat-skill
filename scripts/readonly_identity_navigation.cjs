'use strict';

// Offline-loadable helpers only. Nothing runs when this module is imported.
// openAccount requires a freshly authorized app object supplied by the caller;
// it never discovers/activates apps, enters text, sends, presses keys, takes
// screenshots, uses coordinates, logs AX state, retries, or bypasses deadlines.
const RESERVE_MS = 8000;
const READ_OPTIONS = Object.freeze({emit: false, disableDiffing: true});

class SelectionError extends Error {
  constructor(code, status, stage) {
    super(code);
    this.name = 'SelectionError';
    this.code = code;
    this.status = status;
    this.stage = stage;
  }
}

function selectExactlyOne(state, pattern, stage) {
  if (typeof state !== 'string') {
    throw new SelectionError('STATE_UNAVAILABLE', 'unavailable', stage);
  }
  const matches = [];
  for (const line of state.split(/\r?\n/u)) {
    const match = pattern.exec(line);
    if (match) {
      const index = Number(match[1]);
      if (!Number.isSafeInteger(index) || index < 0) {
        throw new SelectionError('INVALID_AX_INDEX', 'unavailable', stage);
      }
      matches.push(index);
    }
  }
  if (matches.length !== 1) {
    throw new SelectionError(matches.length ? 'AMBIGUOUS_SELECTOR' : 'MISSING_SELECTOR',
      matches.length ? 'ambiguous' : 'missing', stage);
  }
  return matches[0];
}

function selectAppMenu(state) {
  // A plain menubar item, never the sidebar's "按钮 Weixin, ID: ...".
  return selectExactlyOne(state, /^\s*(\d+)\s+(?:微信|Weixin|WeChat)\s*$/u, 'app_menu');
}

function selectSettingsMenu(state) {
  return selectExactlyOne(state,
    /^\s*(\d+)\s+(?:(?:菜单项|菜单项目|MenuItem|AXMenuItem|menu item)\s+)?(?:Settings|设置|偏好设置)(?:…|\.\.\.)?(?:, (?:ID|Secondary Actions): [^\r\n]+)?\s*$/u,
    'settings_menu');
}

function selectMyAccountButton(state) {
  return selectExactlyOne(state,
    /^\s*(\d+)\s+(?:按钮|Button|button|AXButton)\s+My Account(?:, (?:ID|Secondary Actions): [^\r\n]+)?\s*$/u,
    'my_account_button');
}

async function openAccount(app, deadlineEpochMs) {
  const started = Date.now();
  let stage = 'preflight';
  function result(status, publicStage) {
    return {status, stage: publicStage, elapsed_ms: Math.max(0, Date.now() - started)};
  }
  function budget(nextStage) {
    stage = nextStage;
    if (typeof deadlineEpochMs !== 'number' || !Number.isFinite(deadlineEpochMs)) {
      throw new SelectionError('INVALID_DEADLINE', 'invalid_deadline', stage);
    }
    const remaining = deadlineEpochMs - Date.now();
    if (remaining <= 0) throw new SelectionError('DEADLINE_EXPIRED', 'deadline', stage);
    if (remaining < RESERVE_MS) throw new SelectionError('DEADLINE_RESERVE', 'reserve', stage);
  }
  async function freshState(nextStage) {
    budget(nextStage);
    const state = await app.getAXState({...READ_OPTIONS});
    budget(nextStage);
    if (typeof state !== 'string') throw new SelectionError('STATE_UNAVAILABLE', 'unavailable', stage);
    return state;
  }
  async function click(index, nextStage) {
    budget(nextStage);
    await app.click(index); // Native CUA contract: a single number argument.
    budget(nextStage);
  }
  try {
    budget('preflight');
    if (!app || typeof app.click !== 'function' || typeof app.getAXState !== 'function') {
      throw new SelectionError('APP_INTERFACE_UNAVAILABLE', 'unavailable', stage);
    }
    const initial = await freshState('initial_state');
    budget('app_menu');
    const appMenu = selectAppMenu(initial);
    await click(appMenu, 'app_menu_click');

    // Never reuse indices from an earlier tree after any click.
    const menuState = await freshState('app_menu_state');
    budget('settings_menu');
    const settings = selectSettingsMenu(menuState);
    await click(settings, 'settings_click');

    const settingsState = await freshState('settings_state');
    budget('my_account_button');
    const account = selectMyAccountButton(settingsState);
    await click(account, 'my_account_click');
    await freshState('account_state');
    // Navigation completed; account/recipient identity is NOT verified here.
    return result('completed', 'account_state');
  } catch (error) {
    if (error instanceof SelectionError) return result(error.status, error.stage);
    // Error payloads may contain raw UI content; expose no exception text.
    return result('error', stage);
  }
}

module.exports = {RESERVE_MS, SelectionError, selectAppMenu, selectSettingsMenu,
  selectMyAccountButton, openAccount};
