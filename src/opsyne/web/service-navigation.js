"use strict";

// Service summaries and paged collections come from the service query API (#6).
// The global overview supplies only registries and explicitly shared settings.
const serviceTabs = { home: "ホーム", incidents: "インシデント", approvals: "承認待ち", history: "操作履歴", settings: "設定" };
Object.assign(state, { route: {}, routeEpoch: 0, requestId: 0, summaries: {}, workspace: {}, caseServices: new Map(), loadError: null });

function parseRoute(hash) {
  let parts;
  try { parts = hash.replace(/^#/, "").split("/").map(decodeURIComponent); }
  catch { return { view: "missing" }; }
  if ((!parts[0] || parts[0] === "services") && parts.length === 1) return { view: "services" };
  if (parts[0] === "history" && ["global", "unknown"].includes(parts[1]) && parts.length === 2) return { view: "shared-history", scope: parts[1] };
  if (parts[0] !== "services" || !parts[1]) return { view: "missing" };
  const serviceId = parts[1], view = parts[2] || "home";
  if (Object.hasOwn(serviceTabs, view) && parts.length <= 3) return { view, serviceId };
  if (view === "incidents" && parts[3] && parts.length === 4) return { view, serviceId, caseId: parts[3] };
  return { view: "missing", serviceId };
}
function serviceUrl(view = "home", serviceId = state.route.serviceId, caseId = null) {
  return serviceId ? `#services/${safeId(serviceId)}/${view}${caseId ? `/${safeId(caseId)}` : ""}` : "#services";
}
function link(text, href, className = "inline-link") { const node = el("a", className, text); node.href = href; return node; }
function allRows(key) { return Array.isArray(state.data?.[key]) ? state.data[key] : []; }
function rows(key) {
  const serviceId = state.route.serviceId;
  if (!serviceId) return [];
  if (key === "services") return allRows(key).filter(item => item.id === serviceId);
  if (key === "cases") return state.workspace.cases?.items || [];
  if (key === "executions") return state.workspace.executions?.items || [];
  if (key === "plans") return (state.workspace.approvals?.items || []).filter(item => item.kind === "plan").map(item => item.definition);
  if (key === "audit") return state.workspace.history?.items || [];
  if (key === "coverage") return state.summaries[serviceId]?.value?.coverage || [];
  if (key === "adapters" || key === "adapter_discoveries") {
    const sources = new Set(rows("sources").map(item => item.id));
    return allRows(key).filter(item => sources.has(adapterObject(item).source_id));
  }
  if (key === "tasks") return allRows(key).filter(item => state.caseServices.get(item.case_id) === serviceId);
  return allRows(key).filter(item => item.service_id === serviceId);
}
function navigate() {
  if (location.hash === "#main") { $("#main").focus(); return; }
  const next = parseRoute(location.hash);
  if (JSON.stringify(next) === JSON.stringify(state.route)) return;
  const switching = document.activeElement?.id === "service-switch";
  state.route = next; state.view = next.view; state.routeEpoch += 1; state.requestId += 1;
  state.workspace = {}; state.loadError = null; state.query = ""; state.filter = "all"; state.detailSection = "summary";
  state.caseLive = null; state.modalEpoch += 1; $("#action-dialog").close();
  clearTimeout(toastTimer); $("#toast").hidden = true; $("#global-error").hidden = true;
  render();
  if (switching) $("#service-switch")?.focus();
  if (state.session) refresh(true);
}
function updateHeadings() {
  const { view, serviceId } = state.route;
  const service = allRows("services").find(item => item.id === serviceId);
  const name = service?.name || serviceId;
  const title = serviceTabs[view] || ({ services: "サービス一覧", "shared-history": "共通・所属不明の操作履歴", missing: "ページが見つかりません" })[view];
  $("#breadcrumb-title").textContent = name ? `${name} / ${title}` : title;
  $("#page-title").textContent = title;
  $("#page-eyebrow").textContent = name || "OPSYNE";
  $("#page-description").textContent = name ? `${name} の状況と対応を確認します。` : "保守するサービスを選択してください。";
  document.title = `${name ? `${name} · ` : ""}${title} — OpSyne`;
  const context = $("#service-context"), nav = $(".nav-list");
  const summary = state.summaries[serviceId]?.value;
  const counts = { incidents: summary?.unresolved_incidents, approvals: summary ? summary.pending_plans + summary.pending_adapters : null };
  const signature = JSON.stringify([state.route, allRows("services").map(item => [item.id, item.name])]);
  // Polling must not replace a focused service selector or menu link.
  if (context.dataset.signature === signature) {
    for (const [tab, count] of Object.entries(counts)) {
      const node = nav.querySelector(`[data-view=${tab}] .nav-count`);
      if (node) node.textContent = count ?? "";
    }
    return;
  }
  context.dataset.signature = signature; context.replaceChildren(); nav.replaceChildren();
  context.append(link("← サービス一覧", "#services"));
  if (!serviceId) return;
  const label = el("label", "", "サービスを切り替え"); label.htmlFor = "service-switch";
  const select = el("select"); select.id = "service-switch";
  if (!service) { const option = el("option", "", `未確認: ${serviceId}`); option.value = serviceId; select.append(option); }
  for (const item of allRows("services")) { const option = el("option", "", item.name); option.value = item.id; select.append(option); }
  select.value = serviceId;
  select.addEventListener("change", () => { location.hash = serviceUrl(Object.hasOwn(serviceTabs, view) ? view : "home", select.value); });
  append(context, label, select);
  for (const [tab, text] of Object.entries(serviceTabs)) {
    const anchor = link(text, serviceUrl(tab), ""); anchor.dataset.view = tab;
    if (tab === view) { anchor.classList.add("active"); anchor.setAttribute("aria-current", "page"); }
    if (Object.hasOwn(counts, tab)) anchor.append(el("span", "nav-count", counts[tab] ?? ""));
    nav.append(anchor);
  }
}
function validSummary(value, serviceId) {
  if (value?.service?.id !== serviceId || !Array.isArray(value.coverage) || ![value.unresolved_incidents, value.pending_plans, value.pending_adapters].every(Number.isSafeInteger)) throw new Error("サービスの状態データを確認できません。");
  return value;
}
function validPage(value) {
  if (!value || !Array.isArray(value.items) || !Number.isSafeInteger(value.total) || typeof value.has_more !== "boolean" || !["empty", "complete", "partial"].includes(value.collection_state) || (value.has_more && typeof value.next_cursor !== "string")) throw new Error("一覧の形式を確認できません。0件とは扱いません。");
  return value;
}
function pagePath(collection) {
  return state.route.view === "shared-history" ? `/history?scope=${state.route.scope}&limit=50` : `/services/${safeId(state.route.serviceId)}/items/${collection}?limit=50`;
}
function pageCollections() {
  return ({ home: ["cases"], incidents: ["cases"], approvals: ["approvals"], history: ["executions", "history"], "shared-history": ["history"] })[state.route.view] || [];
}
function rememberCases(page, serviceId) {
  for (const item of page.items) {
    if (item.service_id !== serviceId) throw new Error("異なるサービスのインシデントが返りました。表示を中止しました。");
    state.caseServices.set(item.id, serviceId);
  }
}
async function refresh(force = false) {
  if (!state.session || (state.refreshBusy && !force)) return;
  const requestId = ++state.requestId, epoch = state.routeEpoch, token = state.token;
  const current = () => requestId === state.requestId && epoch === state.routeEpoch && token === state.token && state.session;
  const { serviceId, view } = state.route;
  state.refreshBusy = true;
  try {
    const overview = await api("/overview");
    if (!current()) return;
    if (!overview || ["services", "sources", "adapters", "coverage", "tasks"].some(key => !Array.isArray(overview[key]))) throw new Error("登録情報の形式を確認できません。");
    state.data = overview; state.loadError = null;
    if (view === "services") {
      const results = await Promise.allSettled(overview.services.map(item => api(`/services/${safeId(item.id)}/overview`).then(value => validSummary(value, item.id))));
      if (!current()) return;
      state.summaries = Object.fromEntries(overview.services.map((item, i) => [item.id, results[i].status === "fulfilled" ? { value: results[i].value } : { error: results[i].reason.message }]));
    } else if (serviceId && view !== "missing") {
      // Never fall back to another service or an unscoped global collection.
      const value = validSummary(await api(`/services/${safeId(serviceId)}/overview`), serviceId);
      if (!current()) return;
      state.summaries[serviceId] = { value };
    }
    const collections = pageCollections();
    // Keep already loaded continuation pages stable until the user explicitly refreshes.
    const needed = collections.filter(key => force || !state.workspace[key]?.loadedMore);
    const results = await Promise.allSettled(needed.map(async key => {
      const page = validPage(await api(pagePath(key)));
      if (key === "cases") rememberCases(page, serviceId);
      return page;
    }));
    if (!current()) return;
    needed.forEach((key, i) => { state.workspace[key] = results[i].status === "fulfilled" ? results[i].value : { error: results[i].reason.message }; });
    if (view === "settings") {
      const results = await Promise.allSettled([api("/capabilities"), api("/checks")]);
      if (!current()) return;
      ["capabilities", "checks"].forEach((key, i) => {
        state.workspace[key] = results[i].status === "fulfilled" && Array.isArray(results[i].value)
          ? { items: results[i].value.filter(item => item.service_id === serviceId) } : { error: "設定を取得できませんでした。" };
      });
    }
    $("#global-error").hidden = true; setConnection(true);
    $("#last-updated").textContent = `最終取得 ${date(Date.now() / 1000, true)}`;
    const editing = $("#page-content").contains(document.activeElement);
    if (force || (!editing && !$("#action-dialog").open)) render(); else updateHeadings();
    if (state.route.caseId && !$("#action-dialog").open) await openCase(state.route.caseId);
    else if (state.caseLive && $("#action-dialog").open) await refreshCaseSignals();
  } catch (error) {
    if (!current()) return;
    state.loadError = error; state.workspace = {};
    if (serviceId) state.summaries[serviceId] = { error: error.message };
    setConnection(false); render();
  } finally { if (requestId === state.requestId) state.refreshBusy = false; }
}
async function loadMore(collection, node) {
  const page = state.workspace[collection], epoch = state.routeEpoch;
  if (!page?.has_more || !page.next_cursor) return;
  await busy(node, async () => {
    try {
      const next = validPage(await api(`${pagePath(collection)}&cursor=${safeId(page.next_cursor)}`));
      if (epoch !== state.routeEpoch || state.workspace[collection] !== page) return;
      if (collection === "cases") rememberCases(next, state.route.serviceId);
      state.workspace[collection] = { ...next, items: [...page.items, ...next.items], loadedMore: true }; render();
    } catch (error) {
      if (epoch !== state.routeEpoch || state.workspace[collection] !== page) return;
      if (error.status === 409) {
        // A changed collection invalidates every accumulated page and its cursor.
        state.workspace[collection] = { error: "一覧が更新されました。先頭から再取得しています。" }; render();
        await refresh(true);
      } else { page.moreError = error.message; render(); }
    }
  });
}
function collectionPanel(collection, title, renderItems) {
  const page = state.workspace[collection], box = panel(title);
  const body = el("div", "panel-body"); box.append(body);
  if (!page) { body.append(message("取得中です…")); return box; }
  if (page.error) {
    append(body, message(`取得失敗: ${page.error}`, "error"), button("再取得", () => refresh(true))); return box;
  }
  const status = page.has_more ? `一部取得: ${page.items.length} / ${page.total}件` : `取得済み: ${page.items.length}件`;
  body.append(el("p", "collection-status", `${status} · 確認 ${date(page.observed_at, true)}`));
  if (page.items.length) body.append(renderItems(page.items));
  else body.append(message("取得済み0件です。記録がないことは、サービスの正常性を保証しません。"));
  if (page.moreError) body.append(message(`続きの取得に失敗しました: ${page.moreError}`, "error"));
  if (page.has_more) body.append(button("続きを取得", event => loadMore(collection, event.currentTarget)));
  return box;
}
function observation(summary) {
  if (!summary?.coverage?.length) return "ログの接続が未登録です";
  if (summary.coverage.some(item => !item.last_received)) return "ログ未受信の接続があります";
  if (summary.coverage.every(item => item.status === "healthy")) return "ログ受信を継続中";
  return "ログの受信状況を確認してください";
}
function renderServiceDirectory() {
  const root = el("div"), grid = el("div", "source-grid");
  if (!allRows("services").length) root.append(empty("サービスが未登録です", "サービスを追加して、ログを接続してください。GitHub連携は不要です。", "◎", button("サービスを追加", serviceForm, "button primary", can("configure"))));
  for (const service of allRows("services")) {
    const card = el("article", "source-card"), result = state.summaries[service.id];
    append(card, el("h2", "", service.name), el("code", "", service.id), message("機能の状態は未確認です"));
    if (result?.value) {
      const s = result.value;
      append(card, el("p", "", observation(s)), info([["未解決インシデント", s.unresolved_incidents], ["承認待ち", s.pending_plans + s.pending_adapters], ["確認時刻", date(s.observed_at, true)]]));
    } else card.append(message(result?.error ? `状態の取得失敗: ${result.error}` : "状態を取得中…", result?.error ? "error" : "info"));
    card.append(link("サービスを開く →", serviceUrl("home", service.id), "button")); grid.append(card);
  }
  append(root, grid, sharedHistoryLinks());
  if (can("configure")) root.append(button("全サービス共通: 復元・実行保留の管理", recoveryDetails, "button"));
  root.append(button("合成データで動作を確認", demoForm, "button", can("configure")));
  return root;
}
function sharedHistoryLinks() {
  return append(el("div", "shared-settings"), el("p", "field-hint", "全体共通・所属を確認できない操作はサービス別履歴に含めません。"), link("全体共通の操作履歴", "#history/global"), link("所属不明の操作履歴", "#history/unknown"));
}
function renderServiceHome() {
  const s = state.summaries[state.route.serviceId]?.value, root = el("div");
  if (!s) return message("状態を取得中です…");
  const metrics = el("div", "metrics-grid service-metrics");
  for (const [title, count, view] of [["未解決インシデント", s.unresolved_incidents, "incidents"], ["承認待ち", s.pending_plans + s.pending_adapters, "approvals"]]) {
    metrics.append(append(link("", serviceUrl(view), "metric-card"), el("div", "metric-top", title), el("div", "metric-number", count)));
  }
  append(root, metrics, message(`${observation(s)}。機能の状態は未確認です。インシデントと復旧確認の記録で判断してください。`));
  const next = panel("次にすること"), body = el("div", "panel-body");
  body.append(s.pending_plans + s.pending_adapters ? link("承認待ちの内容を確認", serviceUrl("approvals"), "button primary") : s.unresolved_incidents ? link("インシデントを確認", serviceUrl("incidents"), "button primary") : link("ログの接続・確認方法を確認", serviceUrl("settings"), "button"));
  next.append(body); root.append(next);
  root.append(collectionPanel("cases", "インシデント", caseTable));
  const preparation = panel("このサービスの準備状況");
  preparation.append(append(el("div", "panel-body"), info([["ログの接続", observation(s)], ["AI調査（全サービス共通）", state.data.llm?.configured ? "接続設定あり・実行品質は未確認" : "未設定"]]), link("ログ・解析ルール・復旧操作の設定", serviceUrl("settings"))));
  root.append(preparation); return root;
}
function renderApprovals() {
  return collectionPanel("approvals", "内容を確認し、実施を許可するもの", items => {
    const list = el("div");
    for (const entry of items) {
      const record = entry.definition;
      if (entry.kind === "plan" && record.service_id !== state.route.serviceId) { list.append(message("別サービスの対応案は表示できません。", "error")); continue; }
      if (entry.kind === "adapter" && !rows("sources").some(source => source.id === adapterObject(record).source_id)) { list.append(message("解析ルールの所属を確認できません。", "error")); continue; }
      const card = el("article", "plan-card");
      append(card, el("h3", "", entry.kind === "plan" ? "対応案" : "ログの解析ルール"), el("p", "analysis-text", record.reason || adapterObject(record).name || entry.id));
      card.append(el("p", "role-note", entry.review?.can_review ? "内容を確認できます。対象・期限などは承認時に照合します。" : entry.review?.reason === "self_authored" ? "自身が提案・編集したため、別の承認者を待っています。" : "承認権限のある担当者を待っています。"));
      if (entry.kind === "plan") {
        state.caseServices.set(record.case_id, state.route.serviceId);
        card.append(button("対応案を確認", () => reviewPlan(record, { id: record.case_id, service_id: record.service_id }), "button small"));
      } else card.append(button("解析ルールを確認", () => openAdapter(record), "button small"));
      list.append(card);
    }
    return list;
  });
}
function historyItems(items) {
  const list = el("div");
  for (const entry of items) {
    const card = el("article", "plan-card");
    append(card, el("h3", "", auditAction(entry)), info([["日時", date(entry.at, true)], ["主体", entry.actor], ["対象", entry.object_id]]), el("p", "analysis-text", typeof entry.detail === "string" ? entry.detail : JSON.stringify(entry.detail)));
    if (entry.current_object) card.append(jsonDetails("現在の対象（操作時点の状態とは別）", entry.current_object));
    if (entry.related_plan) card.append(jsonDetails("関連する固定計画と承認", entry.related_plan));
    card.append(jsonDetails("操作時点の記録", entry)); list.append(card);
  }
  return list;
}
function renderServiceHistory() {
  const root = el("div");
  root.append(collectionPanel("executions", "実行と復旧確認", items => {
    const list = el("div");
    for (const execution of items) {
      const card = el("article", "plan-card");
      append(card, el("h3", "", execution.id), info([["操作結果", badge(execution.status)], ["復旧確認", execution.verification ? badge(execution.verification.status) : "未確認"]]));
      if (execution.status === "UNKNOWN") card.append(message("操作結果は不明です。復旧確認がPASSでも成功とは扱いません。照合するまで再送しないでください。", "warning"));
      const actions = el("div", "card-actions");
      if (["UNKNOWN", "IN_FLIGHT", "PENDING"].includes(execution.status)) actions.append(button("操作結果を照合", event => busy(event.currentTarget, async () => {
        await api(`/executions/${safeId(execution.id)}/reconcile`, {}); await refresh(true);
      }), "button small", can("operate")));
      actions.append(button("復旧確認を実行", event => busy(event.currentTarget, async () => {
        await api(`/executions/${safeId(execution.id)}/verify`, {}); await refresh(true);
      }), "button small", can("operate")));
      card.append(actions);
      card.append(jsonDetails("実行台帳・確認記録", execution)); list.append(card);
    }
    return list;
  }));
  append(root, collectionPanel("history", "提案・承認・操作・復旧確認の記録", historyItems), sharedHistoryLinks()); return root;
}
function renderServiceSettings() {
  const root = el("div"), service = rows("services")[0];
  if (!service) return message("サービスの登録情報を取得できません。", "error");
  const connections = panel("ログの接続先・復旧操作・確認方法");
  const body = el("div", "panel-body");
  append(body, button("ログの接続先を追加", sourceForm, "button", can("configure")), button("復旧操作・確認方法を設定", () => integrationDetails(service), "button"));
  for (const key of ["capabilities", "checks"]) {
    const entry = state.workspace[key];
    if (entry?.error) body.append(message(entry.error, "error"));
    else if (entry) body.append(jsonDetails(key === "capabilities" ? "登録された復旧操作" : "登録された確認方法", entry.items));
  }
  connections.append(body); root.append(connections);
  root.append(renderSources(true));
  root.append(append(el("div", "list-section-heading"), el("h2", "", "ログの解析ルール"), button("解析ルールを作成", adapterForm, "button", can("operate"))));
  root.append(renderAdapters());
  root.append(message("自動提案の有効・無効、収集時間、再試行間隔と上限は全サービス共通の設定です。"));
  const shared = panel("全サービス共通の設定", "サービス選択によって適用範囲は変わりません");
  shared.append(append(el("div", "panel-body"), info([["AIモデル", state.data.llm?.model], ["AI接続", state.data.llm?.configured ? "設定あり" : "未設定"], ["日次呼出上限", state.data.llm?.daily_call_limit], ["認証主体", state.session.actor], ["権限", roleNames[state.session.role]]]), message("AI接続・利用上限・自動提案・認証は全サービス共通です。ログの接続と復旧操作はこのサービスの設定です。")));
  root.append(shared); return root;
}
function render() {
  updateHeadings();
  const actions = $("#heading-actions"); actions.replaceChildren();
  if (state.route.view === "services") actions.append(button("＋ サービスを追加", serviceForm, "button primary", can("configure")));
  const target = $("#page-content");
  if (!state.session) { target.replaceChildren(message("接続が必要です。")); return; }
  if (state.loadError) { target.replaceChildren(message(state.loadError.status === 404 ? "指定したサービスは見つかりません。URLを確認してください。" : `取得失敗: ${state.loadError.message}`, "error"), link("サービス一覧へ", "#services", "button")); return; }
  if (!state.data) { target.replaceChildren(message("登録情報を取得中です…")); return; }
  const views = { services: renderServiceDirectory, home: renderServiceHome, incidents: () => collectionPanel("cases", "調査・対応が必要な問題", caseTable), approvals: renderApprovals, history: renderServiceHistory, settings: renderServiceSettings, "shared-history": () => collectionPanel("history", state.route.scope === "global" ? "全サービス共通の操作" : "所属を確認できない操作", historyItems), missing: () => append(el("div"), message("指定したページは見つかりません。サービスや詳細IDを確認してください。", "warning"), link("サービス一覧へ", "#services")) };
  target.replaceChildren(views[state.route.view]());
}

// App primitives are loaded first; navigation owns startup and periodic refresh.
window.addEventListener("hashchange", navigate);
document.querySelector(".skip-link").addEventListener("click", event => { event.preventDefault(); $("#main").focus(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 10000);
navigate();
if (state.token) connect(state.token).catch(() => disconnect("保存したトークンでは接続できませんでした。再接続してください。"));
else disconnect();
