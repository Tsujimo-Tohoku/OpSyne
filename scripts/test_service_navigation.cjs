// Browser acceptance against an isolated local server. No real LLM or service credentials.
// NODE_PATH resolves playwright/test. Arguments: loopback URL, fresh server data directory.
const { chromium, expect } = require('playwright/test');
const { readFileSync, mkdirSync, writeFileSync } = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const [base, dataDir] = process.argv.slice(2);
  assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
  assert(dataDir, 'Use an isolated server data directory');
  const tokens = JSON.parse(readFileSync(path.join(dataDir, 'tokens.json'), 'utf8'));
  const token = actor => tokens.find(item => item.actor === actor).token;
  const request = async (url, body, actor = 'owner') => {
    const response = await fetch(`${base}/api${url}`, { method: body === undefined ? 'GET' : 'POST', headers: { Authorization: `Bearer ${token(actor)}`, 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    assert(response.ok, `API ${url}: ${response.status}`); return response.json();
  };
  assert.equal((await request('/overview')).services.length, 0, 'Start with a fresh server');
  await request('/demo', {});
  const a = 'demo-checkout', b = 'members';
  await request('/services', { id: b, name: '会員API', instance_id: 'members-v1', owner: 'owner' });
  await request('/sources', { id: 'members-log', name: '会員ログ', service_id: b, kind: 'push' });
  await request('/sources/members-log/ingest', { events: [{ external_id: 'members-1', payload: '{"synthetic":"member-only"}' }] });
  const overview = await request('/overview');
  const incident = overview.cases.find(item => item.service_id === a && item.kind === 'operation_failure');
  const memberIncident = overview.cases.find(item => item.service_id === b);
  assert(incident && memberIncident);
  const plan = await request(`/cases/${incident.id}/plans`, { capability_id: 'demo-restore', reason: 'Service navigation acceptance' }, 'operator');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [], passed = [];
  const done = name => { passed.push(name); console.log(`PASS: ${name}`); };
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.on('pageerror', error => errors.push(error.message));
    const writes = [];
    page.on('request', req => { if (req.method() !== 'GET') writes.push(new URL(req.url()).pathname); });
    // Simulate the global view's truncation: scoped cases/approvals/history must remain visible.
    await page.route('**/api/overview', async route => {
      const response = await route.fetch(); const body = await response.json();
      await route.fulfill({ json: { ...body, cases: [], plans: [], executions: [], audit: [] } });
    });
    await page.goto(base);
    await page.getByLabel('アクセストークン', { exact: true }).fill(token('owner'));
    await page.getByRole('button', { name: 'ワークスペースに接続' }).click();
    await expect(page.locator('#auth-dialog')).not.toBeVisible();
    await expect(page.locator('#page-title')).toHaveText('サービス一覧');
    await expect(page.locator('.source-card')).toHaveCount(2);
    await expect(page.locator('#page-content')).toContainText('機能の状態は未確認');
    await expect(page.locator('#page-content')).toContainText('会員API');
    done('Directory uses service summaries despite truncated global lists');
    const go = async (service, view) => {
      await page.goto(`${base}/#services/${service}/${view}`);
      await expect(page.locator('#service-switch')).toHaveValue(service);
      await expect(page.locator('#page-title')).toHaveText(({ home: 'ホーム', incidents: 'インシデント', approvals: '承認待ち', history: '操作履歴', settings: '設定' })[view]);
    };
    await go(a, 'home');
    await expect(page.locator('.nav-list a')).toHaveCount(5);
    await page.locator('[data-view=approvals]').click();
    await expect(page.locator('#page-content')).toContainText('Service navigation acceptance');
    await page.locator('#service-switch').selectOption(b);
    await expect(page).toHaveURL(new RegExp(`#services/${b}/approvals$`));
    await expect(page.locator('#page-content')).toContainText('取得済み0件');
    await expect(page.locator('#page-content')).not.toContainText('Service navigation acceptance');
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`#services/${a}/approvals$`));
    await expect(page.locator('#page-content')).toContainText('Service navigation acceptance');
    await page.goForward();
    await expect(page.locator('#service-switch')).toHaveValue(b);
    await page.reload();
    await expect(page.locator('#service-switch')).toHaveValue(b);
    done('Five left menu entries, tab preservation, back/forward and reload');

    await go(b, 'incidents');
    await expect(page.locator('#page-content')).toContainText(memberIncident.title);
    await expect(page.locator('#page-content')).not.toContainText(incident.title);
    await page.getByRole('button', { name: memberIncident.title, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/incidents/${memberIncident.id}$`));
    await expect(page.locator('#dialog-title')).toHaveText(memberIncident.title);
    await page.getByRole('button', { name: 'ログと調査', exact: true }).click();
    await expect(page.locator('#dialog-content')).toContainText('member-only');
    await page.getByRole('button', { name: '対応履歴・復旧確認', exact: true }).click();
    await expect(page.getByText('実行記録はありません。', { exact: false })).toBeVisible();
    await page.reload();
    await expect(page.locator('#dialog-title')).toHaveText(memberIncident.title);
    // Changing the URL is possible while a modal is open (history or another script).
    await page.evaluate(id => { location.hash = `services/${id}/incidents`; }, a);
    await expect(page.locator('#action-dialog')).not.toBeVisible();
    await expect(page.locator('#page-content')).toContainText(incident.title);
    await page.goto(`${base}/#services/${b}/incidents/${incident.id}`);
    await expect(page.locator('#dialog-content')).toContainText('選択中のサービスに属していません');
    await expect(page.locator('#dialog-content button')).toHaveCount(0);
    await page.goto(`${base}/#services/nonexistent/home`);
    await expect(page.locator('#page-content')).toContainText('指定したサービスは見つかりません');
    await page.goto(`${base}/#services/%ZZ/home`);
    await expect(page.locator('#page-content')).toContainText('指定したページは見つかりません');
    done('Scoped details, direct URLs and missing/cross-service IDs');

    await go(b, 'settings');
    await expect(page.locator('#page-content')).toContainText('会員ログ');
    await expect(page.locator('#page-content')).not.toContainText('demo-restore');
    await expect(page.locator('#page-content')).toContainText('全サービス共通の設定');
    await page.getByRole('button', { name: 'ログの接続先を追加' }).click();
    await expect(page.locator('#field-service_id option')).toHaveCount(1);
    await expect(page.locator('#field-service_id')).toHaveValue(b);
    const logRoot = path.join(dataDir, 'synthetic-application'); mkdirSync(logRoot, { recursive: true });
    const logFile = path.join(logRoot, 'application.log');
    writeFileSync(logFile, 'Synthetic application error: request failed\n');
    await page.locator('#field-kind').selectOption('file');
    await page.locator('#log-search-root').fill(logRoot);
    await page.getByRole('button', { name: 'ログを探す', exact: true }).click();
    await expect(page.locator('.discovery-results')).toContainText('application.log');
    await page.getByRole('button', { name: 'このログを選ぶ', exact: true }).click();
    await expect(page.locator('#field-path')).toHaveValue(logFile);
    await expect(page.locator('#field-service_id')).toHaveValue(b);
    await page.locator('#dialog-close').click();
    await go(a, 'history');
    await expect(page.locator('#page-content')).toContainText('Service navigation acceptance');
    await page.locator('#service-switch').selectOption(b);
    await expect(page.locator('#page-content')).not.toContainText('Service navigation acceptance');
    await expect(page.locator('#page-content')).toContainText('会員');
    done('Settings, local log discovery and history stay scoped; shared settings are labelled');

    const casesPath = `**/api/services/${a}/items/cases?*`;
    const synthetic = Array.from({ length: 51 }, (_, index) => ({ ...incident, id: `page-${index}`, title: `Page incident ${index}` }));
    const pageBody = (items, more) => ({ items, total: 51, has_more: more, next_cursor: more ? 'opaque+/=' : null, collection_state: 'partial', observed_at: Date.now() / 1000 });
    let cursorSeen = false;
    await page.route(casesPath, route => {
      const cursor = new URL(route.request().url()).searchParams.get('cursor');
      if (cursor) { assert.equal(cursor, 'opaque+/='); cursorSeen = true; }
      return route.fulfill({ json: pageBody(cursor ? synthetic.slice(50) : synthetic.slice(0, 50), !cursor) });
    });
    await go(a, 'incidents');
    await expect(page.locator('#page-content')).toContainText('一部取得: 50 / 51件');
    await page.getByRole('button', { name: '続きを取得', exact: true }).click();
    await expect(page.locator('#page-content')).toContainText('取得済み: 51件');
    await expect(page.locator('.case-table tbody tr')).toHaveCount(51); assert(cursorSeen);
    await page.unroute(casesPath);
    let conflict = false;
    await page.route(casesPath, route => {
      if (new URL(route.request().url()).searchParams.has('cursor')) { conflict = true; return route.fulfill({ status: 409, json: { detail: 'changed' } }); }
      return route.fulfill({ json: conflict ? { ...pageBody([incident], false), total: 1, collection_state: 'complete' } : pageBody(synthetic.slice(0, 50), true) });
    });
    await page.locator('#refresh-button').click();
    await page.getByRole('button', { name: '続きを取得', exact: true }).click();
    await expect(page.locator('.case-table tbody tr')).toHaveCount(1);
    await expect(page.locator('#page-content')).not.toContainText('Page incident');
    await page.unroute(casesPath);
    await page.route(casesPath, route => route.fulfill({ status: 503, json: { detail: 'synthetic unavailable' } }));
    await page.locator('#refresh-button').click();
    await expect(page.locator('#page-content')).toContainText('取得失敗');
    await expect(page.locator('#page-content')).not.toContainText('取得済み0件');
    await page.unroute(casesPath);
    done('Pagination, opaque cursors, 409 resets and failure versus empty');

    let release, received;
    const reached = new Promise(resolve => { received = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    await page.route(`**/api/services/${a}/overview`, async route => { received(); await gate; await route.fulfill({ json: { ...(await request(`/services/${a}/overview`)), unresolved_incidents: 9999 } }); });
    await go(a, 'home'); await reached;
    await page.locator('#service-switch').selectOption(b);
    await expect(page.locator('#page-content')).toContainText(memberIncident.title);
    release(); await page.waitForTimeout(300);
    await expect(page.locator('#service-switch')).toHaveValue(b);
    await expect(page.locator('#page-content')).not.toContainText('9999');
    await page.unroute(`**/api/services/${a}/overview`);
    done('Late service responses cannot replace the new service');

    await go(a, 'approvals');
    await page.getByRole('button', { name: '対応案を確認', exact: true }).click();
    await expect(page.locator('#dialog-title')).toContainText('計画');
    const before = writes.length;
    await page.evaluate(id => { location.hash = `services/${id}/approvals`; }, b);
    await expect(page.locator('#action-dialog')).not.toBeVisible();
    assert.equal(writes.length, before);
    await go(a, 'approvals');
    await page.getByRole('button', { name: '対応案を確認', exact: true }).click();
    let finishApproval, approvalReceived;
    const gotApproval = new Promise(resolve => { approvalReceived = resolve; });
    const approvalGate = new Promise(resolve => { finishApproval = resolve; });
    await page.route(`**/api/plans/${plan.id}/approve`, async route => { approvalReceived(); await approvalGate; await route.fulfill({ json: { ...plan, status: 'APPROVED' } }); });
    await page.locator('#action-dialog input[type=checkbox]').check();
    await page.getByRole('button', { name: '承認して復旧を実行', exact: true }).click();
    await gotApproval;
    await page.evaluate(id => { location.hash = `services/${id}/approvals`; }, b);
    finishApproval(); await page.waitForTimeout(300);
    assert(!writes.some(url => url.endsWith('/execute')), 'No execution after changing service during approval');
    await expect(page.locator('#action-dialog')).not.toBeVisible();
    await page.unroute(`**/api/plans/${plan.id}/approve`);
    done('Service switch closes consent and cancels continuation after approval');

    const executionsPath = `**/api/services/${a}/items/executions?*`;
    await page.route(executionsPath, route => route.fulfill({ json: { items: [{ id: 'unknown-result', status: 'UNKNOWN', verification: { status: 'PASS' } }, { id: 'unverified-success', status: 'SUCCEEDED', verification: null }], total: 2, has_more: false, next_cursor: null, collection_state: 'complete', observed_at: Date.now() / 1000 } }));
    await go(a, 'history');
    await expect(page.locator('#page-content')).toContainText('操作結果は不明');
    await expect(page.locator('#page-content')).toContainText('未確認');
    await expect(page.locator('#page-content')).not.toContainText('復旧確認済み');
    await expect(page.getByRole('button', { name: '操作結果を照合', exact: true })).toBeEnabled();
    await expect(page.getByRole('button', { name: '復旧確認を実行', exact: true })).toHaveCount(2);
    await page.unroute(executionsPath);
    done('UNKNOWN/PASS and SUCCEEDED/unverified do not imply recovery');

    await page.goto(`${base}/#services`);
    await page.getByRole('button', { name: '＋ サービスを追加', exact: true }).click();
    await page.locator('#field-id').fill('created-in-ui');
    await page.locator('#field-name').fill('新しいサービス');
    await page.locator('#field-instance_id').fill('created-v1');
    await page.getByRole('button', { name: 'サービスを登録', exact: true }).click();
    await expect(page).toHaveURL(/#services\/created-in-ui\/home$/);
    await expect(page.locator('#page-content')).toContainText('ログの接続が未登録');
    done('Registration opens the new service home without GitHub');

    await page.setViewportSize({ width: 390, height: 844 });
    await go(a, 'home');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#service-switch').focus();
    await page.evaluate(() => refresh());
    await expect(page.locator('#service-switch')).toBeFocused();
    await page.keyboard.press('m'); await page.keyboard.press('Enter');
    // Explicit selection also exercises the native select on platforms with differing typeahead.
    await page.locator('#service-switch').selectOption(b);
    await page.locator('[data-view=approvals]').focus(); await page.keyboard.press('Enter');
    await expect(page).toHaveURL(new RegExp(`#services/${b}/approvals$`));
    for (const view of Object.keys({ home: 1, incidents: 1, approvals: 1, history: 1, settings: 1 })) {
      await page.locator(`[data-view=${view}]`).focus(); await page.keyboard.press('Enter');
      await expect(page).toHaveURL(new RegExp(`#services/${b}/${view}$`));
    }
    await page.locator('#service-context a').focus(); await page.keyboard.press('Enter');
    await expect(page.locator('#page-title')).toHaveText('サービス一覧');
    const artifacts = path.join(dataDir, 'browser-results'); mkdirSync(artifacts, { recursive: true });
    await page.screenshot({ path: path.join(artifacts, 'mobile.png'), fullPage: true });
    await go(b, 'settings');
    await expect(page.locator('#page-content')).toContainText('会員ログ');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(artifacts, 'mobile-settings.png'), fullPage: true });
    await page.setViewportSize({ width: 1280, height: 900 }); await go(a, 'home');
    await expect(page.locator('.case-table tbody tr')).toHaveCount(2);
    await page.screenshot({ path: path.join(artifacts, 'desktop.png'), fullPage: true });
    assert.equal(errors.length, 0, errors.join('\n'));
    done('390px layout and keyboard navigation have no browser errors');
    writeFileSync(path.join(artifacts, 'report.json'), JSON.stringify({ passed, errors }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
