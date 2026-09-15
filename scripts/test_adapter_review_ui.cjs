// Optional browser acceptance test. Use an isolated server without real credentials.
// NODE_PATH must resolve Playwright; args: loopback URL, isolated data directory.
const { chromium } = require('playwright');
const { readFileSync, mkdirSync } = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const [base, dataDir] = process.argv.slice(2);
  assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
  assert(dataDir, 'An isolated test data directory is required');
  const tokens = JSON.parse(readFileSync(path.join(dataDir, 'tokens.json'), 'utf8'));
  const token = actor => tokens.find(entry => entry.actor === actor).token;
  const request = async (url, body, actor = 'owner', method = 'POST') => {
    const response = await fetch(base + '/api' + url, {
      method, headers: { Authorization: `Bearer ${token(actor)}`, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    assert(response.ok, `API ${url}: ${response.status}`);
    return response.json();
  };
  await request('/demo');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const errors = [];
    const login = async actor => {
      const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base);
      await page.getByLabel('アクセストークン', { exact: true }).fill(token(actor));
      await page.getByRole('button', { name: 'ワークスペースに接続' }).click();
      await page.locator('#auth-dialog').waitFor({ state: 'hidden' });
      await page.locator('a[data-view="adapters"]').click();
      return page;
    };
    const operator = await login('operator');
    await operator.getByRole('button', { name: '＋ 変換定義を作成' }).click();
    const id = `ui-review-${Date.now()}`;
    await request('/sources', { id, service_id: 'demo-checkout', name: id, kind: 'push' });
    await request(`/sources/${id}/ingest`, { events: [{ external_id: id, payload: JSON.stringify({ message: 'Synthetic failure' }) }] });
    const draft = {
      id, name: id, source_id: id, target_instance_id: 'demo-checkout-v1',
      target_version: 1, version: 1, fields: { message: 'message' },
    };
    await operator.getByLabel('宣言的な変換定義（JSON）').fill(JSON.stringify(draft));
    await operator.getByLabel('監視目的（利用者が決めた目的）').fill('決済障害を把握する');
    await operator.getByLabel('変換によって把握したいこと', { exact: true }).fill('処理失敗\n障害の状況');
    await operator.getByRole('button', { name: '承認待ちとして保存' }).click();
    await operator.locator('#action-dialog').waitFor({ state: 'hidden' });
    const open = async page => {
      await page.locator('article').filter({ has: page.getByRole('heading', { name: id, exact: true }) }).getByRole('button', { name: '定義を確認', exact: true }).click();
    };
    await open(operator);
    assert(await operator.getByRole('button', { name: 'この定義を承認', exact: true }).isDisabled());
    await operator.getByRole('button', { name: '監視目的・説明を編集', exact: true }).click();
    await operator.getByLabel('監視目的（利用者が決めた目的）').fill('');
    await operator.getByLabel('確認事項', { exact: true }).fill('<img src=x onerror=alert(1)>');
    await operator.getByRole('button', { name: '説明を保存して再確認' }).click();
    await operator.getByRole('heading', { name: '利用者の監視目的', exact: true }).waitFor();
    assert.match(await operator.locator('.adapter-explanation').innerText(), /未記録/);
    assert.equal(await operator.locator('.adapter-explanation img').count(), 0);
    assert.equal(await operator.locator('#action-dialog input[type=checkbox]').isChecked(), false);
    const reviewer = await login('reviewer');
    await open(reviewer);
    assert.equal(await reviewer.getByRole('button', { name: '監視目的・説明を編集', exact: true }).count(), 0);
    const approval = reviewer.getByRole('button', { name: 'この定義を承認', exact: true });
    assert(await approval.isDisabled());
    await reviewer.locator('#action-dialog input[type=checkbox]').check();
    const current = await request(`/adapters/${id}`, undefined, 'owner', 'GET');
    await request(`/adapters/${id}/explanation`, { digest: current.digest, explanation: { monitoring_purpose: '別画面で更新した目的' } }, 'owner', 'PUT');
    await approval.click();
    await reviewer.getByRole('alert').filter({ hasText: '承認対象が変更されたか' }).waitFor();
    assert(await approval.isDisabled());
    await reviewer.getByRole('button', { name: '最新の定義を読み直す' }).click();
    await reviewer.locator('.adapter-explanation').getByText('別画面で更新した目的', { exact: true }).waitFor();
    assert.equal(await reviewer.locator('#action-dialog input[type=checkbox]').isChecked(), false);
    // Editing from the operator's stale dialog must retain the unsaved input on conflict.
    await operator.getByRole('button', { name: '監視目的・説明を編集', exact: true }).click();
    await operator.getByLabel('監視目的（利用者が決めた目的）').fill('未保存の入力');
    await operator.getByRole('button', { name: '説明を保存して再確認' }).click();
    await operator.getByRole('alert').filter({ hasText: '入力は保存していません' }).waitFor();
    assert.equal(await operator.getByLabel('監視目的（利用者が決めた目的）').inputValue(), '未保存の入力');
    await reviewer.locator('#action-dialog input[type=checkbox]').check();
    await approval.click();
    await reviewer.locator('#action-dialog').waitFor({ state: 'hidden' });
    assert.equal((await request(`/adapters/${id}`, undefined, 'owner', 'GET')).status, 'APPROVED');
    await open(reviewer);
    await reviewer.getByRole('button', { name: '原本を再解析', exact: true }).waitFor();
    await reviewer.setViewportSize({ width: 390, height: 844 });
    mkdirSync(path.join(dataDir, 'screenshots'), { recursive: true });
    await reviewer.screenshot({ path: path.join(dataDir, 'screenshots', 'review-mobile.png') });
    assert(await reviewer.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await reviewer.setViewportSize({ width: 1280, height: 900 });
    await reviewer.screenshot({ path: path.join(dataDir, 'screenshots', 'review-desktop.png') });
    const viewer = await login('viewer');
    await viewer.route(`**/api/adapters/${id}`, async route => {
      const response = await route.fetch(); const record = await response.json();
      record.explanation = { human: null, recorded_by: null, agent: {
        task_id: 'synthetic-task', suggested_use: 'Agentの用途案',
        rationale: [{ text: '合成ログに結果のキーがある', evidence_ids: ['synthetic-raw'] }],
        unknowns: ['業務上の意味は未確認'],
      } };
      await route.fulfill({ response, json: record });
    });
    await open(viewer);
    await viewer.locator('.adapter-explanation').getByText('Agentの用途案', { exact: true }).waitFor();
    assert.match(await viewer.locator('.adapter-explanation .detail-section').first().innerText(), /未記録/);
    assert.match(await viewer.locator('.adapter-explanation').innerText(), /synthetic-raw/);
    assert.equal(await viewer.getByRole('button', { name: '監視目的・説明を編集', exact: true }).count(), 0);
    await viewer.unroute(`**/api/adapters/${id}`);
    await viewer.route(`**/api/adapters/${id}`, async route => {
      const response = await route.fetch(); const record = await response.json();
      delete record.explanation; delete record.explanation_revision; delete record.explanation_editors;
      await route.fulfill({ response, json: record });
    });
    await viewer.getByRole('button', { name: '最新の定義を読み直す' }).click();
    await viewer.locator('.adapter-explanation').getByText('Agentの用途案', { exact: true }).waitFor({ state: 'hidden' });
    await viewer.getByRole('heading', { name: '利用者の監視目的', exact: true }).waitFor();
    assert.equal(await viewer.locator('.adapter-explanation').getByText('未記録', { exact: true }).count(), 7);
    assert.deepEqual(errors, []);
    console.log('PASS: create, edit, missing purpose, text escaping, role checks, stale approval/edit, re-review, approval, mobile layout');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
