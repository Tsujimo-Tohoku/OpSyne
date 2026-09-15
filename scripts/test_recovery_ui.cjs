// Browser acceptance test against a fresh, isolated local server without real credentials.
// NODE_PATH must resolve Playwright; args: loopback URL, isolated data directory.
const { chromium, expect } = require('playwright/test');
const { readFileSync, mkdirSync } = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');

(async () => {
  const [base, dataDir] = process.argv.slice(2);
  assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
  assert(dataDir, 'A fresh, isolated data directory is required');
  const tokens = JSON.parse(readFileSync(path.join(dataDir, 'tokens.json'), 'utf8'));
  const token = actor => tokens.find(entry => entry.actor === actor).token;
  const request = async (url, body, actor = 'owner') => {
    const response = await fetch(`${base}/api${url}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { Authorization: `Bearer ${token(actor)}`, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    assert(response.ok, `API ${url}: ${response.status}`);
    return response.json();
  };
  await request('/demo', {});
  const incident = (await request('/overview')).cases.find(item => item.kind === 'operation_failure');
  assert.equal((await request(`/cases/${incident.id}`)).executions.length, 0, 'Use a fresh data directory');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [];
  const passed = [];
  const done = name => { passed.push(name); console.log(`PASS: ${name}`); };
  try {
    const prepare = async (actor = 'owner', proposer = 'operator') => {
      const reason = `Recovery browser acceptance ${randomUUID()}`;
      const plan = await request(`/cases/${incident.id}/plans`, { capability_id: 'demo-restore', reason }, proposer);
      const page = await browser.newPage({ viewport: { width: 1280, height: 960 } });
      page.on('pageerror', error => errors.push(error.message));
      const writes = [];
      page.on('request', req => { if (req.method() === 'POST') writes.push(new URL(req.url()).pathname); });
      await page.goto(`${base}/#services/demo-checkout/incidents`);
      await page.getByLabel('アクセストークン', { exact: true }).fill(token(actor));
      await page.getByRole('button', { name: 'ワークスペースに接続' }).click();
      await expect(page.locator('#auth-dialog')).not.toBeVisible();
      const open = async (execute = false) => {
        if (await page.locator('#action-dialog').isVisible()) await page.locator('#dialog-close').click();
        await page.locator('a[data-view="incidents"]').click();
        await page.getByRole('button', { name: 'サンプル: 注文処理を復旧してください', exact: true }).click();
        const card = page.locator('article.plan-card').filter({ has: page.getByText(reason, { exact: true }) });
        await card.getByRole('button', { name: execute ? '復旧処理を開始' : '固定計画を確認', exact: true }).click();
      };
      await open();
      const confirm = () => page.locator('#action-dialog input[type=checkbox]').check();
      const submit = page.getByRole('button', { name: '承認して復旧を実行', exact: true });
      const feedback = page.locator('#dialog-content [role=status]');
      const executionRequests = () => writes.filter(url => url.endsWith('/execute'));
      return { page, plan, writes, open, confirm, submit, feedback, executionRequests };
    };
    const mockApproval = async test => {
      await test.page.route(`**/api/plans/${test.plan.id}/approve`, route => route.fulfill({ json: { ...test.plan, status: 'APPROVED' } }));
    };
    const mockExecution = async (test, status) => {
      await mockApproval(test);
      await test.page.route(`**/api/plans/${test.plan.id}/execute`, route => route.fulfill({ json: { id: `synthetic-${test.plan.id}`, plan_id: test.plan.id, status } }));
    };
    const start = async test => { await test.confirm(); await test.submit.click(); };

    for (const [actor, proposer] of [['owner', 'owner'], ['operator', 'operator'], ['viewer', 'operator']]) {
      const test = await prepare(actor, proposer);
      await test.confirm();
      await expect(test.page.getByRole('button', { name: actor === 'owner' ? '承認して復旧を実行' : 'この計画を承認', exact: true })).toBeDisabled();
      assert.equal(test.writes.length, 0);
      await test.page.close();
    }
    done('Self approval and unauthorized roles cannot start recovery');

    {
      const test = await prepare('reviewer');
      await test.confirm();
      await test.page.getByRole('button', { name: 'この計画を承認', exact: true }).click();
      await expect(test.feedback).toContainText('実行権限のある担当者');
      assert.equal((await request(`/cases/${incident.id}`)).plans.find(plan => plan.id === test.plan.id).status, 'APPROVED');
      assert.equal(test.executionRequests().length, 0);
      await test.page.close();
    done('Approver-only session saves approval and waits for an operator');
    }
    {
      const test = await prepare('operator');
      await request(`/plans/${test.plan.id}/approve`, { digest: test.plan.digest }, 'reviewer');
      await test.open(true);
      await test.page.route(`**/api/plans/${test.plan.id}/execute`, route => route.fulfill({ json: { id: 'synthetic-approved', plan_id: test.plan.id, status: 'SUCCEEDED' } }));
      await test.page.route('**/api/executions/synthetic-approved/verify', route => route.fulfill({ json: { status: 'PASS' } }));
      await test.confirm();
      await test.page.getByRole('button', { name: '復旧を実行して確認', exact: true }).click();
      await expect(test.feedback).toContainText('サービスが登録された正常性の条件を満たす');
      assert.deepEqual(test.writes.map(url => url.split('/').at(-1)), ['execute', 'verify']);
      await test.page.close();
      done('Operator can execute an already approved plan and verify without another approval');
    }
    {
      const test = await prepare();
      await test.page.route('**/api/capabilities', route => route.fulfill({ json: [] }));
      await test.open(); await test.confirm();
      await expect(test.page.getByText('計画に記録された版の操作能力を取得できません。承認前に登録内容を確認してください。')).toBeVisible();
      await expect(test.submit).toBeDisabled();
      assert.equal(test.writes.length, 0);
      await test.page.close();
      done('Unavailable registered operation prevents approval');
    }
    {
      const test = await prepare();
      await test.page.route(`**/api/plans/${test.plan.id}/approve`, route => route.fulfill({ status: 409, json: { detail: '対象の版が変更されました' } }));
      await start(test);
      await expect(test.feedback).toContainText('復旧処理は要求していません');
      await expect(test.submit).toBeDisabled();
      assert.equal(test.executionRequests().length, 0);
      await test.page.close();
      done('Rejected approval cannot execute or be resubmitted from the same review');
    }
    {
      const test = await prepare();
      await test.page.route(`**/api/plans/${test.plan.id}/approve`, async route => {
        await route.fetch(); // Commit approval but lose its response.
        await route.abort('failed');
      });
      await start(test);
      await expect(test.feedback).toContainText('復旧処理は要求していません');
      assert.equal((await request(`/cases/${incident.id}`)).plans.find(plan => plan.id === test.plan.id).status, 'APPROVED');
      assert.equal(test.executionRequests().length, 0);
      await test.page.close();
      done('Lost approval response does not dispatch an execution');
    }
    for (const status of ['FAILED', 'UNKNOWN', 'IN_FLIGHT']) {
      const test = await prepare(); await mockExecution(test, status);
      await start(test);
      await expect(test.feedback).toContainText(status === 'FAILED' ? '復旧処理が失敗' : '完了を確認できません');
      await expect(test.submit).toBeDisabled();
      assert.equal(test.executionRequests().length, 1);
      assert.equal(test.writes.filter(url => url.endsWith('/verify')).length, 0);
      await test.page.close();
    }
    done('Failed, unknown and unfinished executions never claim recovery or auto-retry');
    {
      const test = await prepare(); await mockApproval(test);
      await test.page.route(`**/api/plans/${test.plan.id}/execute`, route => route.abort('failed'));
      await start(test);
      await expect(test.feedback).toContainText('再実行せず、実行記録と監査ログ');
      await expect(test.submit).toBeDisabled();
      assert.equal(test.executionRequests().length, 1);
      assert.equal(test.writes.filter(url => url.endsWith('/verify')).length, 0);
      await test.page.close();
      done('Lost execution response stops without retry');
    }
    for (const status of ['FAIL', 'UNKNOWN', 'network-error']) {
      const test = await prepare(); await mockExecution(test, 'SUCCEEDED');
      await test.page.route(`**/api/executions/synthetic-${test.plan.id}/verify`, route => status === 'network-error' ? route.abort('failed') : route.fulfill({ json: { status } }));
      await start(test);
      await expect(test.feedback).toContainText(status === 'FAIL' ? '正常性の条件を満たしていません' : status === 'UNKNOWN' ? '復旧を確認できません' : '復旧確認の結果を取得できません');
      assert.equal(test.executionRequests().length, 1);
      await expect(test.submit).toBeDisabled();
      await test.page.close();
    }
    done('Failed or unknown verification remains unresolved without repeating recovery');
    {
      const test = await prepare();
      let release;
      const gate = new Promise(resolve => { release = resolve; });
      await test.page.route(`**/api/plans/${test.plan.id}/approve`, async route => {
        await gate;
        await route.fulfill({ json: { ...test.plan, status: 'APPROVED' } });
      });
      await start(test);
      await expect(test.page.locator('.recovery-progress')).toContainText('承認：処理中');
      await test.page.locator('#dialog-close').click();
      const response = test.page.waitForResponse(`**/api/plans/${test.plan.id}/approve`);
      release(); await response;
      // Synchronize with the response handler instead of relying on an arbitrary delay.
      await expect(test.page.getByRole('button', { name: '承認して復旧を実行', exact: true, includeHidden: true })).not.toHaveAttribute('aria-busy', 'true');
      assert.equal(test.executionRequests().length, 0);
      await test.page.close();
      done('Closing the review during approval stops later requests');
    }
    {
      const test = await prepare();
      for (const viewport of [{ width: 1280, height: 600 }, { width: 390, height: 600 }, { width: 700, height: 360 }]) {
        await test.page.setViewportSize(viewport);
        for (const scroll of [0, 100000]) {
          await test.page.locator('#dialog-content').evaluate((node, top) => { node.scrollTop = top; }, scroll);
          const box = await test.submit.boundingBox();
          assert(box && box.y >= 0 && box.y + box.height <= viewport.height, 'Approval button must remain inside the viewport');
          assert(box.x >= 0 && box.x + box.width <= viewport.width, 'Approval button must fit horizontally');
          assert(await test.submit.evaluate(node => {
            const box = node.getBoundingClientRect();
            return node.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
          }), 'Approval button must not be covered by another element');
        }
      }
      await test.page.setViewportSize({ width: 1280, height: 960 });
      await test.confirm();
      await expect(test.submit).toBeEnabled();
      mkdirSync(path.join(dataDir, 'screenshots'), { recursive: true });
      await test.submit.scrollIntoViewIfNeeded();
      await test.page.screenshot({ path: path.join(dataDir, 'screenshots', 'approval.png') });
      await test.submit.evaluate(button => { button.click(); button.click(); });
      await expect(test.feedback).toContainText('サービスが登録された正常性の条件を満たす');
      assert.deepEqual(test.writes.map(url => url.split('/').at(-1)), ['approve', 'execute', 'verify']);
      const detail = await request(`/cases/${incident.id}`);
      assert.equal(detail.case.status, 'RESOLVED');
      assert.equal(detail.executions.length, 1);
      assert.equal(detail.executions[0].status, 'SUCCEEDED');
      assert.equal(detail.executions[0].verification.status, 'PASS');
      assert.equal(detail.executions[0].verification.evidence.change_count, 1);
      await test.feedback.scrollIntoViewIfNeeded();
      await test.page.screenshot({ path: path.join(dataDir, 'screenshots', 'recovered.png') });
      await test.page.setViewportSize({ width: 390, height: 844 });
      assert(await test.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await test.feedback.scrollIntoViewIfNeeded();
      await test.page.screenshot({ path: path.join(dataDir, 'screenshots', 'recovered-mobile.png') });
      await test.page.getByRole('button', { name: '案件と実行記録を確認' }).click();
      await expect(test.page.locator('#dialog-content')).toContainText('復旧確認済み');
      await test.page.close();
      done('Real approval → exactly one target change → independent PASS → resolved; mobile layout');
    }
    assert.deepEqual(errors, []);
    console.log(`PASS: ${passed.length} scenario groups; no browser JavaScript errors`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
