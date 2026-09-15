"use strict";

// API records are evidence, never markup. All server values use textContent.
const $ = (selector) => document.querySelector(selector);
const storeKey = "opsyne.access-token";
const state = { token: "", session: null, data: null, view: "overview", refreshBusy: false, caseId: null, query: "", filter: "all", modalEpoch: 0, caseLive: null, renderedData: "" };
try { state.token = sessionStorage.getItem(storeKey) || ""; } catch { /* In-memory sessions work when browser storage is unavailable. */ }
const headings = {
  overview: ["概要", "OPERATIONS OVERVIEW", "運用の現在地", "観測の状態と対応の進捗を、一つの場所で。"],
  cases: ["案件", "CASE MANAGEMENT", "根拠から、次の判断へ。", "調査・提案・承認・実行・結果確認を案件ごとに追跡します。"],
  sources: ["観測源", "OBSERVABILITY", "観測をつなぐ", "登録された対象と、収集の完全性を確認します。"],
  adapters: ["変換定義", "NORMALIZATION", "原本に、意味を結び付ける。", "適用範囲と版を固定した変換定義を管理します。"],
  audit: ["監査ログ", "AUDIT TRAIL", "判断と操作の記録", "誰が、何に、どのような操作を行ったかを確認します。"],
};
const statuses = {
  OPEN: ["未着手", "blue"], INVESTIGATING: ["調査中", "blue"], ANALYZED: ["調査済み", "blue"],
  PROPOSED: ["提案済み", "purple"], AWAITING_APPROVAL: ["承認待ち", "amber"], DRAFT: ["承認待ち", "amber"],
  APPROVED: ["承認済み", "teal"], REJECTED: ["却下", "red"], REVOKED: ["失効", "red"], EXPIRED: ["期限切れ", "amber"],
  EXECUTED: ["実行済み", "blue"], EXECUTING: ["実行中", "blue"], VERIFYING: ["結果確認待ち", "amber"], PENDING: ["待機中", "amber"], IN_FLIGHT: ["実行中", "blue"],
  SUCCEEDED: ["操作成功", "blue"], FAILED: ["失敗", "red"], UNKNOWN: ["確認不能", "amber"],
  RESOLVED: ["復旧確認済み", "teal"], CLOSED: ["終了", "teal"], VERIFIED: ["結果確認済み", "teal"],
  PASS: ["条件充足を確認", "teal"], FAIL: ["条件未達", "red"], ACTIVE: ["有効", "teal"],
  healthy: ["受信継続", "teal"], stale: ["受信遅延", "amber"], missing: ["未受信", "amber"], disabled: ["停止中", "outline"],
  KNOWN: ["解析済み", "teal"], PARTIAL: ["部分解析", "amber"], INVALID: ["解析失敗", "red"],
  RUNNING: ["進行中", "blue"], COMPLETED: ["完了", "teal"], DONE: ["完了", "teal"], BLOCKED: ["保留", "amber"],
  COLLECTING: ["標本を収集中", "blue"], QUEUED: ["提案待ち", "amber"], COVERED: ["承認済み定義で対応", "teal"], RETRY_WAIT: ["再試行待ち", "amber"],
};
const roleNames = { admin: "管理者", operator: "運用担当", approver: "承認者", viewer: "閲覧者", agent: "Agent" };
const taskRoleNames = { operator: "運用調査", sre: "信頼性調査", security: "セキュリティ調査", adapter: "変換定義の提案", periodic: "定期調査" };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function append(parent, ...children) { for (const child of children.flat()) if (child) parent.append(child); return parent; }
function button(text, handler, kind = "button", allowed = true) {
  const node = el("button", kind, text);
  node.type = "button";
  node.disabled = !allowed;
  if (!allowed) node.title = "現在の役割ではこの操作を行えません";
  node.addEventListener("click", handler);
  return node;
}
function badge(status) { const [label, color] = statuses[status] || [status || "未確認", "outline"]; return el("span", `badge ${color}`, label); }
function severity(value) {
  const levels = { CRITICAL: "緊急", ERROR: "高", HIGH: "高", WARNING: "中", MEDIUM: "中", LOW: "低", INFO: "情報", DEBUG: "詳細", UNKNOWN: "不明" };
  const level = String(value || "UNKNOWN").toUpperCase();
  return el("span", `severity ${level === "ERROR" ? "high" : level.toLowerCase()}`, levels[level] || level);
}
function message(text, type = "info") { return el("div", `message ${type}`, text); }
function jsonDetails(title, value, open = false) {
  const node = el("details"); node.open = open;
  append(node, el("summary", "", title), el("pre", "", JSON.stringify(value, null, 2)));
  return node;
}
function date(value, full = false) {
  if (value === null || value === undefined || value === "") return "未記録";
  const parsed = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(parsed.getTime())) return "日時不明";
  return new Intl.DateTimeFormat("ja-JP", full
    ? { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(parsed);
}
function relative(value) {
  if (value === null || value === undefined) return "未受信";
  const milliseconds = typeof value === "number" ? value * 1000 : Date.parse(value);
  if (!Number.isFinite(milliseconds)) return "日時不明";
  const seconds = Math.max(0, Math.floor((Date.now() - milliseconds) / 1000));
  if (seconds < 60) return "1分以内";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}時間前`;
  return `${Math.floor(seconds / 86400)}日前`;
}
function relativeNode(value, className = "") {
  const node = el("span", className, relative(value));
  if (value !== null && value !== undefined) node.dataset.relativeTimestamp = typeof value === "number" ? String(value) : String(Date.parse(value) / 1000);
  return node;
}
function dataSignature() {
  if (!state.data) return "";
  const { worker, ...records } = state.data;
  return JSON.stringify(records);
}
function rows(key) { return Array.isArray(state.data?.[key]) ? state.data[key] : []; }
function serviceName(id) { return rows("services").find((item) => item.id === id)?.name || id || "対象未登録"; }
function sourceName(id) { return rows("sources").find((item) => item.id === id)?.name || id || "観測源不明"; }
function can(operation) {
  const role = state.session?.role;
  const rules = { configure: ["admin"], operate: ["admin", "operator"], approve: ["admin", "approver"] };
  return (rules[operation] || []).includes(role);
}
function selfProposed(record) { return [record.proposer, record.proposed_by, record.created_by].includes(state.session?.actor); }
function safeId(value) { return encodeURIComponent(String(value)); }
function planObject(record) { return record.plan || record; }
function adapterObject(record) { return record.definition || record; }
function errorText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => `${(item.loc || []).filter((part) => part !== "body").join(".")}: ${item.msg || JSON.stringify(item)}`).join(" / ");
  return detail ? JSON.stringify(detail) : "応答に詳細がありません。監査ログとサーバーの状態を確認してください。";
}
async function api(path, body, token = state.token, method = body === undefined ? "GET" : "POST") {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 70000);
  try {
    const response = await fetch(`/api${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: controller.signal, cache: "no-store", credentials: "same-origin",
    });
    let result;
    try { result = await response.json(); } catch { result = null; }
    if (!response.ok) {
      if (response.status === 401 && state.session) disconnect("認証の有効性を確認できません。トークンを確認して再接続してください。");
      const failure = new Error(`${response.status === 403 ? "この操作の権限がありません。" : ""}${errorText(result?.detail || result?.error || (response.status === 401 ? "トークンが正しいか確認してください。" : null))}`);
      failure.status = response.status; throw failure;
    }
    return result;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("応答がタイムアウトしました。操作結果は不明です。状態を更新し、実行台帳を照合してください。");
    if (error instanceof TypeError) throw new Error("サーバーに接続できません。起動状態を確認し、更新してください。操作済みの場合は結果を確認するまで再送しないでください。");
    throw error;
  } finally { clearTimeout(timeout); }
}
let toastTimer;
function toast(text, error = false) {
  clearTimeout(toastTimer);
  $("#toast").textContent = text;
  $("#toast").className = `toast${error ? " error" : ""}`;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, error ? 10000 : 5500);
}
async function busy(node, action) {
  if (node.disabled) return;
  node.disabled = true; node.setAttribute("aria-busy", "true");
  try { await action(); } catch (error) { toast(error.message, true); }
  finally { node.disabled = false; node.removeAttribute("aria-busy"); node.dispatchEvent(new Event("opsyne:idle")); }
}
function setConnection(connected) {
  const target = $("#connection-state");
  target.className = `connection-indicator ${connected ? "connected" : "failed"}`;
  target.replaceChildren(el("i"), document.createTextNode(connected ? "接続中 · 10秒ごとに更新" : "接続を確認してください"));
}
function disconnect(reason) {
  state.token = ""; state.session = null; state.data = null;
  try { sessionStorage.removeItem(storeKey); } catch { /* Session memory was cleared above. */ }
  $("#identity").replaceChildren(document.createTextNode("未接続"), el("small", "", "アクセストークンで接続"));
  $("#page-content").replaceChildren(empty("接続が必要です", "アクセストークンでワークスペースに接続してください。", "◎"));
  $("#heading-actions").replaceChildren();
  $("#case-count").textContent = "";
  $("#global-error").hidden = true;
  $("#action-dialog").close();
  state.modalEpoch += 1;
  $("#auth-error").textContent = reason || ""; $("#auth-error").hidden = !reason;
  if (!$("#auth-dialog").open) $("#auth-dialog").showModal();
  $("#auth-token").focus();
  setConnection(false);
}
async function connect(token) {
  const session = await api("/session", undefined, token);
  if (!session?.actor || !Object.hasOwn(roleNames, session.role)) throw new Error("サーバーから有効な認証情報を取得できませんでした。");
  state.token = token; state.session = session;
  try { sessionStorage.setItem(storeKey, token); } catch { /* Continue with an in-memory token. */ }
  $("#identity").replaceChildren(document.createTextNode(session.actor), el("small", "", roleNames[session.role] || session.role));
  $("#auth-dialog").close(); $("#auth-token").value = "";
  await refresh(true);
}
async function refresh(force = false) {
  if (!state.session || state.refreshBusy) return;
  state.refreshBusy = true;
  try {
    const overview = await api("/overview");
    if (!overview || ["services", "sources", "cases", "plans", "executions", "adapters", "audit", "coverage", "tasks"].some((key) => !Array.isArray(overview[key]))) throw new Error("状態データの形式を確認できません。未確認のデータを正常として表示しないため、最後に取得できた状態を保持しています。");
    state.data = overview;
    $("#global-error").hidden = true;
    setConnection(true);
    $("#last-updated").textContent = `最終取得 ${date(Date.now() / 1000, true)}`;
    $("#case-count").textContent = rows("cases").filter((item) => !["RESOLVED", "CLOSED"].includes(item.status)).length || "";
    const focused = document.activeElement;
    const editing = focused && ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "SUMMARY", "A"].includes(focused.tagName) && $("#page-content").contains(focused);
    if (force || (!editing && !$("#action-dialog").open && dataSignature() !== state.renderedData)) render();
    document.querySelectorAll("[data-relative-timestamp]").forEach((node) => { node.textContent = relative(Number(node.dataset.relativeTimestamp)); });
    if (state.caseLive && $("#action-dialog").open) await refreshCaseSignals();
  } catch (error) {
    setConnection(false);
    $("#global-error").textContent = error.message;
    $("#global-error").hidden = false;
    if (!state.data && state.session) $("#page-content").replaceChildren(empty("データを取得できませんでした", "サーバーの状態を確認し、右上の「更新」を押してください。", "↻"));
  } finally { state.refreshBusy = false; }
}
function navigate() {
  const requested = location.hash.slice(1) || "overview";
  state.view = headings[requested] ? requested : "overview";
  state.query = ""; state.filter = "all";
  if (state.data) render();
  updateHeadings();
}
function updateHeadings() {
  const [breadcrumb, eyebrow, title, description] = headings[state.view];
  $("#breadcrumb-title").textContent = breadcrumb;
  $("#page-eyebrow").textContent = eyebrow; $("#page-title").textContent = title; $("#page-description").textContent = description;
  document.title = `${breadcrumb} — OpSyne`;
  document.querySelectorAll("[data-view]").forEach((link) => {
    const active = link.dataset.view === state.view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
}
function render() {
  state.renderedData = dataSignature();
  updateHeadings();
  const actions = $("#heading-actions"); actions.replaceChildren();
  if (state.view === "overview" || state.view === "sources") {
    append(actions, button("↻  収集・解析", (event) => busy(event.currentTarget, async () => {
      const result = await api("/poll", {}); toast("収集・解析の処理が完了しました。"); await refresh(true);
      if (result?.errors?.length) toast(errorText(result.errors), true);
    }), "button", can("operate")), button("＋ 観測源を登録", sourceForm, "button primary", can("configure")));
  }
  if (state.view === "adapters") append(actions, button("＋ 変換定義を作成", adapterForm, "button primary", can("configure") || can("operate")));
  if (state.view === "cases") append(actions, button("↻  最新の状態を取得", () => refresh(true)));
  const views = { overview: renderOverview, cases: renderCases, sources: renderSources, adapters: renderAdapters, audit: renderAudit };
  $("#page-content").replaceChildren(views[state.view]());
}
function empty(title, description, symbol = "◫", action = null) {
  return append(el("div", "empty-state"), el("span", "empty-icon", symbol), el("h3", "", title), el("p", "", description), action);
}
function panel(title, subtitle = "", count = null, action = null) {
  const root = el("section", "panel");
  const heading = el("div", "panel-heading", title);
  if (count !== null) heading.append(el("span", "count", count));
  const headingWrap = append(el("div"), heading, subtitle ? el("p", "panel-subtitle", subtitle) : null);
  root.append(append(el("div", "panel-header"), headingWrap, action));
  return root;
}
function info(items) {
  const grid = el("dl", "info-grid");
  for (const [key, value] of items) append(grid, append(el("div", "info-item"), el("dt", "", key), value instanceof Node ? append(el("dd"), value) : el("dd", "", value ?? "未設定")));
  return grid;
}
function metrics() {
  const active = rows("cases").filter((item) => !["RESOLVED", "CLOSED"].includes(item.status));
  const pending = rows("plans").filter((item) => item.status === "DRAFT");
  const uncertain = rows("sources").filter((source) => !["healthy", "disabled"].includes(rows("coverage").find((item) => item.source_id === source.id)?.status));
  const unknown = rows("executions").filter((item) => item.status === "UNKNOWN");
  const cards = [
    ["進行中の案件", active.length, "▤", "調査から結果確認まで", ""],
    ["承認待ちの計画", pending.length, "◇", "固定された計画への判断が必要", "warn"],
    ["観測状態の要確認", uncertain.length, "◎", `${rows("sources").length} 観測源を登録済み`, "warn"],
    ["操作結果不明", unknown.length, "⇄", "照合が完了するまで再送を保留", "danger"],
  ];
  const grid = el("div", "metrics-grid");
  for (const [label, value, icon, description, color] of cards) {
    append(grid, append(el("div", "metric-card"), append(el("div", "metric-top"), el("span", "", label), el("span", "metric-icon", icon)), el("div", `metric-number ${value ? color : ""}`, value), el("div", "metric-description", description)));
  }
  return grid;
}
function caseTable(cases) {
  if (!cases.length) return empty("該当する案件はありません", "観測データから検知された候補がここに表示されます。案件がないことは、対象の正常性を保証しません。", "▤");
  const table = el("table", "case-table data-table");
  const head = el("tr");
  for (const [label, className] of [["案件 / 対象", ""], ["重要度", ""], ["状態", ""], ["更新", "hide-compact"], ["", ""]]) {
    const th = el("th", className, label); th.scope = "col"; head.append(th);
  }
  table.append(append(el("thead"), head));
  const body = el("tbody");
  for (const item of cases) {
    const title = button(item.title || item.id, () => openCase(item.id), "case-title");
    const meta = append(el("div", "case-meta"), el("span", "", serviceName(item.service_id)), el("span", "", "·"), el("span", "", item.kind || "案件"));
    const link = button("詳細 →", () => openCase(item.id), "inline-link"); link.setAttribute("aria-label", `${item.title || item.id} の詳細`);
    append(body, append(el("tr"), append(el("td"), title, meta), append(el("td"), severity(item.severity)), append(el("td"), badge(item.status)), append(el("td", "hide-compact muted"), relativeNode(item.updated_at || item.created_at)), append(el("td"), link)));
  }
  table.append(body); return append(el("div", "table-scroll"), table);
}
function renderCoverage() {
  const root = panel("観測の完全性", "対象の正常性とは独立して評価", null, button("観測源へ →", () => { location.hash = "sources"; }, "inline-link"));
  const body = el("div", "panel-body");
  const healthy = rows("coverage").filter((item) => item.status === "healthy").length;
  const count = rows("sources").length;
  const summary = append(el("div", "coverage-summary"), el("span", "ring", "◎"), append(el("div"), el("strong", "", count ? `${healthy} / ${count} 観測源が受信継続` : "観測源は未登録です"), el("p", "", "最終受信・解析失敗・欠落を個別に確認")));
  body.append(summary);
  for (const source of rows("sources").slice(0, 4)) {
    const coverage = rows("coverage").find((item) => item.source_id === source.id);
    const row = el("div", "coverage-row");
    append(row, append(el("div", "coverage-row-head"), el("span", "coverage-row-name", source.name), badge(coverage?.status || "missing")), append(el("div", "coverage-row-meta"), el("span", "", source.kind === "file" ? "FILE COLLECTOR" : "PUSH INGESTION"), relativeNode(coverage?.last_received)));
    body.append(row);
  }
  append(root, body, el("div", "section-note", "未受信や解析不能のデータを正常として扱いません。"));
  return root;
}
function renderOverview() {
  const root = el("div");
  const recovery = state.data?.recovery;
  if (can("configure") && (recovery?.quarantined || recovery?.holds?.length)) {
    const notice = panel("復元・実行保留の確認", recovery.quarantined ? "復元後の照合が完了するまで、新しい実行許可を保留しています" : "記録された実行予約と台帳の状態を確認できます", null, button("管理者として確認", recoveryDetails, "button small"));
    notice.append(el("div", "section-note", `復元後の確認待ち: ${recovery.quarantined ? "あり" : "なし"} · 実行予約: ${recovery.holds?.length || 0} 件`)); root.append(notice);
  }
  root.append(metrics());
  const primary = el("div", "overview-primary");
  const recent = [...rows("cases")].sort((a, b) => (b.updated_at || b.created_at) - (a.updated_at || a.created_at)).slice(0, 6);
  const cases = panel("対応する案件", "根拠を確認し、次のアクションへ", rows("cases").length, button("すべて見る →", () => { location.hash = "cases"; }, "inline-link"));
  cases.append(caseTable(recent)); primary.append(cases);
  const activity = panel("最近のアクティビティ", "判断と操作は監査ログに記録されます", null, button("監査ログへ →", () => { location.hash = "audit"; }, "inline-link"));
  activity.append(activityList(rows("audit").slice(0, 4))); primary.append(activity);
  const secondary = el("div", "overview-secondary"); secondary.append(renderCoverage());
  const workflow = el("section", "workflow-card");
  append(workflow, el("div", "eyebrow", "HUMAN GOVERNED OPERATIONS"), el("h3", "", "判断の根拠を、最後まで。"), el("p", "", "提案と認可、操作と結果確認を分離。承認した計画を現在の条件で照合し、実行後の状態を独立して確かめます。"));
  const steps = el("div", "workflow-steps");
  ["観測", "調査", "承認", "実行", "確認"].forEach((label, index) => { if (index) steps.append(el("b", "", "→")); steps.append(el("span", "", label)); });
    workflow.append(steps); secondary.append(workflow);
  const llm = state.data?.llm;
  if (llm) {
    const llmPanel = panel("調査・変換提案モデル", "収集処理はモデルの稼働に依存しません");
    const llmBody = el("div", "panel-body");
    append(llmBody, info([["モデル", llm.model || "未設定"], ["日次呼び出し上限", llm.daily_call_limit ?? "未設定"]]), llm.configured ? badge("ACTIVE") : el("span", "badge outline", "未設定"));
    llmBody.append(info([["未知形式の自動提案", autoAdapterStatus()]]));
    llmBody.append(el("p", "field-hint", "未知形式をまとめて変換案を作成します。有効化には内容の確認と承認が必要です。"));
    if (!llm.configured) llmBody.append(el("p", "field-hint", "API キーが未設定です。サーバー環境に OPENAI_API_KEY を設定すると、調査・変換案の生成を利用できます。"));
    llmPanel.append(llmBody); secondary.append(llmPanel);
  }
  if (!rows("services").length) {
    const start = panel("はじめの一歩", "対象を登録して、観測を開始");
    const body = el("div", "panel-body");
    append(body, button("＋ サービスを登録", serviceForm, "button full-width", can("configure")), button("合成データで動作を確認", demoForm, "button subtle full-width", can("configure")), el("p", "field-hint", "デモは明示的に追加する合成データです。"));
    start.append(body); secondary.append(start);
  }
  root.append(append(el("div", "overview-grid"), primary, secondary)); return root;
}
function toolbar(placeholder, filters, onChange) {
  const bar = el("div", "toolbar");
  const search = el("input", "search-input"); search.type = "search"; search.placeholder = placeholder; search.value = state.query; search.setAttribute("aria-label", placeholder);
  search.addEventListener("input", () => { state.query = search.value; onChange(); });
  bar.append(search);
  if (filters) {
    const select = el("select", "filter-select"); select.setAttribute("aria-label", "状態で絞り込み");
    filters.forEach(([value, text]) => { const option = el("option", "", text); option.value = value; select.append(option); });
    select.value = state.filter; select.addEventListener("change", () => { state.filter = select.value; onChange(); }); bar.append(select);
  }
  return bar;
}
function renderCases() {
  const root = el("div"); const target = el("div");
  const update = () => {
    const query = state.query.toLocaleLowerCase();
    const matches = rows("cases").filter((item) => `${item.title} ${item.id} ${serviceName(item.service_id)}`.toLocaleLowerCase().includes(query) && (state.filter === "all" || (state.filter === "open" ? !["RESOLVED", "CLOSED"].includes(item.status) : ["RESOLVED", "CLOSED"].includes(item.status))));
    const list = panel("案件一覧", "原本・調査結果・固定計画・実行台帳を確認", matches.length); list.append(caseTable(matches)); target.replaceChildren(list);
  };
  append(root, toolbar("案件名・サービス・IDで検索", [["all", "すべての状態"], ["open", "進行中"], ["resolved", "解決・終了"]], update), target); update(); return root;
}
function renderSources() {
  const root = el("div");
  const heading = append(el("div", "list-section-heading"), el("h2", "", `サービス (${rows("services").length})`), button("＋ サービスを登録", serviceForm, "button small", can("configure")));
  root.append(heading);
  if (!rows("services").length) root.append(append(el("div", "panel"), empty("サービスを登録してください", "監視対象の実体・版・所有者を先に登録します。その後、観測源を関連付けます。", "◎")));
  else {
    const grid = el("div", "source-grid");
    for (const service of rows("services")) {
      const card = el("article", "source-card");
      append(card, append(el("div", "source-card-head"), el("div", "source-icon", "▧"), append(el("div"), el("h3", "", service.name), el("p", "", service.id)), badge(service.enabled ? "ACTIVE" : "disabled")), info([["対象実体", service.instance_id], ["対象版", `v${service.version}`], ["所有者", service.owner], ["重要度", service.criticality]]), append(el("div", "card-actions"), button("操作能力・独立確認の設定", () => integrationDetails(service), "button small")));
      grid.append(card);
    }
    root.append(grid);
  }
  root.append(append(el("div", "list-section-heading"), el("h2", "", `観測源 (${rows("sources").length})`)));
  if (!rows("sources").length) root.append(append(el("div", "panel"), empty("まだ観測源がありません", "ログファイルまたは Push 取込の観測源を登録してください。", "◎")));
  else {
    const grid = el("div", "source-grid");
    for (const source of rows("sources")) {
      const coverage = rows("coverage").find((item) => item.source_id === source.id);
      const card = el("article", "source-card");
      append(card, append(el("div", "source-card-head"), el("div", "source-icon", source.kind === "file" ? "▤" : "⇣"), append(el("div"), el("h3", "", source.name), el("p", "", serviceName(source.service_id))), badge(coverage?.status || "missing")), info([["取込方式", source.kind === "file" ? "ログファイル" : "Push / API"], ["最終受信", relativeNode(coverage?.last_received)], ["解析失敗", coverage?.parse_failures ?? "未確認"], ["未知形式", coverage?.unknown_count ?? "未確認"], ["未処理", coverage?.pending_count ?? "未確認"], ["欠落", coverage?.gap_count ?? "未確認"]]));
      if (coverage?.last_error) card.append(message(coverage.last_error, "warning"));
      const actions = el("div", "card-actions");
      if (source.kind === "push") actions.append(button("イベントを取り込む", () => ingestForm(source), "button small", can("operate")));
      actions.append(button("観測詳細", () => {
        const body = openDialog(source.name, "OBSERVATION DETAILS");
        append(body, jsonDetails("観測源の設定", source, true), jsonDetails("収集の完全性", coverage || { status: "missing" }, true));
      }, "button small"));
      card.append(actions); grid.append(card);
    }
    root.append(grid);
  }
  const demo = append(el("div", "panel"), append(el("div", "panel-header"), append(el("div"), el("h3", "", "合成データで確認"), el("p", "panel-subtitle", "隔離されたデモ対象と観測イベントを追加します。")), button("デモを準備", demoForm, "button small", can("configure"))));
  root.append(demo); return root;
}
function renderAdapters() {
  const root = el("div");
  root.append(message("変換定義は原本の解釈に使います。定義の承認・再解析から、対象の操作が実行されることはありません。"));
  const discoveries = rows("adapter_discoveries");
  const automatic = panel("未知形式からの変換提案", "同じ構造のログをまとめ、標本と競合の検証後に承認待ちへ追加します", discoveries.length);
  const automaticBody = el("div", "panel-body");
  const llm = state.data?.llm;
  append(automaticBody, info([["自動提案", autoAdapterStatus()], ["標本の収集時間", secondsLabel(llm?.adapter_coalesce_seconds)], ["再試行の最短間隔", secondsLabel(llm?.adapter_retry_seconds)], ["形式ごとの自動試行上限", llm?.adapter_max_attempts ?? "未取得"]]));
  if (!llm?.configured) automaticBody.append(message("API キーが未設定のため、LLMによる変換案の生成は行われません。未知形式の収集状態は引き続き確認できます。", "warning"));
  if (!discoveries.length) automaticBody.append(empty("未知形式のグループはありません", "新しく検知した未知形式がここに表示されます。自動提案が有効なら、標本をまとめて生成を開始します。", "⇄"));
  else for (const discovery of discoveries) automaticBody.append(discoveryCard(discovery, true));
  automatic.append(automaticBody); root.append(automatic);
  root.append(append(el("div", "list-section-heading"), el("h2", "", `変換定義 (${rows("adapters").length})`)));
  if (!rows("adapters").length) root.append(append(el("div", "panel"), empty("変換定義は未登録です", "JSON の項目対応と値の意味を定義します。適用範囲と版を確認し、承認すると有効になります。", "⇄")));
  const grid = el("div", "source-grid");
  for (const record of rows("adapters")) {
    const definition = adapterObject(record);
    const card = el("article", "source-card");
    append(card, append(el("div", "source-card-head"), el("div", "source-icon", "⇄"), append(el("div"), el("h3", "", definition.name), el("p", "", definition.id)), badge(record.status || (record.active ? "ACTIVE" : "DRAFT"))), info([["定義版", `v${definition.version}`], ["観測源", sourceName(definition.source_id)], ["対象実体", definition.target_instance_id], ["対象版", `v${definition.target_version}`]]));
    card.append(append(el("div", "card-actions"), button("定義を確認", () => openAdapter(record), "button small"))); grid.append(card);
  }
  root.append(grid); return root;
}
function secondsLabel(value) {
  if (!Number.isFinite(value)) return "未取得";
  return value >= 60 && value % 60 === 0 ? `${value / 60}分` : `${value}秒`;
}
function autoAdapterStatus() {
  const llm = state.data?.llm;
  if (!llm?.configured) return "API キー未設定";
  if (llm.auto_adapter_proposals === true) return "有効 · 有効化には承認が必要";
  if (llm.auto_adapter_proposals === false) return "停止中";
  return "未取得";
}
function discoveryCard(discovery, showCase = false) {
  const card = el("article", "plan-card");
  append(card, append(el("div", "plan-card-header"), el("h4", "", sourceName(discovery.source_id)), badge(discovery.status)),
    info([["まとめたログ", `${discovery.event_count ?? 0}件`], ["生成の試行", `${discovery.attempts ?? 0}回`], ["参照する標本", `${discovery.evidence_ids?.length ?? 0}件`], ["次の試行予定", ["COLLECTING", "RETRY_WAIT"].includes(discovery.status) && discovery.next_attempt_at ? date(discovery.next_attempt_at, true) : "—"]]));
  if (discovery.detail) card.append(el("p", "analysis-text", discovery.detail));
  if (discovery.status === "RETRY_WAIT") card.append(el("p", "field-hint", "自動再試行は、待機時間が過ぎて新しい標本が届いた後に行います。案件から手動で再提案することもできます。"));
  if (discovery.status === "AWAITING_APPROVAL") card.append(el("p", "field-hint", "変換案を保存しました。項目・値の意味・適用範囲を確認して承認すると有効になります。"));
  const actions = el("div", "card-actions");
  if (showCase && discovery.case_id) actions.append(button("案件と標本を確認", () => openCase(discovery.case_id), "button small"));
  const draft = rows("adapters").find((record) => adapterObject(record).id === discovery.adapter_id);
  if (draft) actions.append(button(draft.status === "DRAFT" ? "変換案を確認・承認" : "変換定義を確認", () => openAdapter(draft), "button small"));
  if (actions.childElementCount) card.append(actions);
  card.append(jsonDetails("形式グループの詳細", discovery));
  return card;
}
function configureProposalButton(node, discovery, caseId) {
  const active = ["QUEUED", "RUNNING"].includes(discovery?.status) || rows("tasks").some((task) => task.case_id === caseId && task.role === "adapter" && ["PENDING", "RUNNING"].includes(task.status));
  node.textContent = active ? "変換案の生成を待機中" : discovery?.attempts > 0 ? "変換案を再提案" : "変換案を手動生成";
  node.disabled = !can("operate") || !state.data?.llm?.configured || active || node.getAttribute("aria-busy") === "true";
  node.title = !can("operate") ? "運用担当または管理者の権限が必要です" : !state.data?.llm?.configured ? "API キーが未設定です" : active ? "受付済みのタスクが完了するまでお待ちください" : "現在の標本から変換案を生成します。手動生成は自動再試行の待機時間・回数上限に関係なく要求できます。日次呼び出し上限は適用されます。";
}
function auditAction(entry) { return entry.action || entry.kind || entry.event || "操作記録"; }
function activityList(entries) {
  if (!entries.length) return empty("まだアクティビティはありません", "登録・承認・実行などの操作履歴をここに表示します。", "≡");
  const list = el("div", "activity-list");
  for (const entry of entries) append(list, append(el("div", "activity-item"), el("span", "activity-dot", "↗"), append(el("div", "activity-text"), el("div", "", auditAction(entry)), el("div", "activity-meta", `${entry.actor || "system"} · ${entry.target_id || entry.object_id || entry.resource_id || "workspace"}`)), relativeNode(entry.created_at || entry.at || entry.timestamp, "activity-time")));
  return list;
}
function renderAudit() {
  const root = el("div"); const target = el("div");
  const update = () => {
    const query = state.query.toLocaleLowerCase();
    const entries = rows("audit").filter((item) => JSON.stringify(item).toLocaleLowerCase().includes(query));
    const box = panel("監査ログ", "時刻はブラウザーのタイムゾーンで表示", entries.length);
    if (!entries.length) box.append(empty("該当する記録はありません", "登録・調査・承認・実行の履歴がここに表示されます。", "≡"));
    else {
      const table = el("table", "data-table"); const head = el("tr");
      ["日時", "実行主体", "操作", "対象", "詳細"].forEach((name) => { const th = el("th", "", name); th.scope = "col"; head.append(th); });
      table.append(append(el("thead"), head)); const body = el("tbody");
      for (const entry of entries) append(body, append(el("tr"), el("td", "muted", date(entry.created_at || entry.at || entry.timestamp)), el("td", "", entry.actor || "system"), el("td", "", auditAction(entry)), el("td", "audit-object", entry.target_id || entry.object_id || entry.resource_id || "—"), append(el("td"), button("詳細", () => {
        const content = openDialog("監査記録", "AUDIT RECORD"); content.append(jsonDetails("記録された内容", entry, true));
      }, "inline-link"))));
      table.append(body); box.append(append(el("div", "table-scroll"), table));
    }
    target.replaceChildren(box);
  };
  append(root, toolbar("主体・操作・対象で検索", null, update), target); update();
  if (rows("tasks").length) {
    const tasks = panel("調査タスク", "有限の処理単位として保持された調査の記録", rows("tasks").length);
    const body = el("div", "panel-body");
    for (const task of rows("tasks").slice(0, 20)) body.append(jsonDetails(`${taskRoleNames[task.role] || task.role} · ${task.id} · ${task.status || "未確認"}`, task));
    tasks.append(body); root.append(tasks);
  }
  return root;
}
function openDialog(title, eyebrow = "WORKSPACE", wide = false) {
  state.modalEpoch += 1;
  state.caseLive = null;
  const dialog = $("#action-dialog"); dialog.classList.toggle("wide", wide);
  dialog.classList.remove("recovery-review"); dialog.querySelector(".recovery-footer")?.remove();
  $("#dialog-title").textContent = title; $("#dialog-eyebrow").textContent = eyebrow;
  const content = $("#dialog-content"); content.replaceChildren();
  if (!dialog.open) dialog.showModal();
  return content;
}
function formField(form, label, name, { value = "", type = "text", options = null, hint = "", full = false, required = true, min = null, max = null, pattern = null } = {}) {
  const wrapper = el("div", `form-field${full ? " full" : ""}`);
  const id = `field-${name}`; const labelNode = el("label", "", label); labelNode.htmlFor = id;
  let input;
  if (options) {
    input = el("select"); for (const [optionValue, optionText] of options) { const option = el("option", "", optionText); option.value = optionValue; input.append(option); }
  } else input = el(type === "textarea" ? "textarea" : "input");
  if (input.tagName === "INPUT") input.type = type;
  input.id = id; input.name = name;
  if (value !== "" || !options) input.value = value;
  input.required = required;
  if (min !== null) input.min = min;
  if (max !== null) input.max = max;
  if (pattern) input.pattern = pattern;
  append(wrapper, labelNode, input);
  if (hint) { const help = el("p", "field-hint", hint); help.id = `${id}-hint`; input.setAttribute("aria-describedby", help.id); wrapper.append(help); }
  form.append(wrapper); return input;
}
function createForm(body, submitText, handler) {
  const form = el("form"); const fields = el("div", "form-grid"); const error = message("", "error"); error.hidden = true; error.classList.add("form-error"); error.setAttribute("role", "alert");
  const submit = el("button", "button primary", submitText); submit.type = "submit";
  const actions = append(el("div", "form-actions"), button("キャンセル", () => $("#action-dialog").close()), submit);
  append(form, fields, error, actions); body.append(form);
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); if (submit.disabled) return; submit.disabled = true; submit.setAttribute("aria-busy", "true"); error.hidden = true;
    try { await handler(new FormData(form)); }
    catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { submit.disabled = false; submit.removeAttribute("aria-busy"); submit.dispatchEvent(new Event("opsyne:idle")); }
  });
  return { fields, form, submit };
}
function identifierOptions() { return { pattern: "[A-Za-z0-9_.:\\-]+", hint: "英数字・_・.・:・- を使用できます。" }; }
function serviceForm() {
  const body = openDialog("サービスを登録", "REGISTER SERVICE");
  const { fields } = createForm(body, "サービスを登録", async (form) => {
    const payload = Object.fromEntries(form); payload.version = Number(payload.version); payload.enabled = true;
    await api("/services", payload); $("#action-dialog").close(); toast("サービスを登録しました。"); await refresh(true);
  });
  formField(fields, "サービス ID", "id", identifierOptions()); formField(fields, "サービス名", "name");
  formField(fields, "対象実体 ID", "instance_id", identifierOptions()); formField(fields, "所有者", "owner", { value: state.session.actor });
  formField(fields, "対象版", "version", { type: "number", min: 1, value: 1 });
  formField(fields, "重要度", "criticality", { options: [["low", "低"], ["medium", "中"], ["high", "高"], ["critical", "緊急"]], value: "medium" });
}
function sourceForm() {
  if (!rows("services").length) { serviceForm(); toast("先に観測対象のサービスを登録してください。"); return; }
  const body = openDialog("観測源を登録", "REGISTER OBSERVATION SOURCE");
  const { fields } = createForm(body, "観測源を登録", async (form) => {
    const payload = Object.fromEntries(form); payload.stale_after_seconds = Number(payload.stale_after_seconds); payload.enabled = true;
    if (payload.kind === "push") delete payload.path;
    await api("/sources", payload); $("#action-dialog").close(); toast("観測源を登録しました。"); await refresh(true);
  });
  formField(fields, "観測源 ID", "id", identifierOptions()); formField(fields, "観測源名", "name");
  formField(fields, "対象サービス", "service_id", { options: rows("services").map((item) => [item.id, item.name]) });
  const kind = formField(fields, "取込方式", "kind", { options: [["push", "Push / API"], ["file", "ログファイル"]], value: "push" });
  const path = formField(fields, "ログファイルの絶対パス", "path", { full: true, required: false, hint: "OpSyne が動くサーバー上の、読み取り可能なファイルを指定します。" }); path.parentElement.hidden = true;
  const finder = logDiscovery(fields, (candidate) => {
    path.value = candidate.path;
    const name = fields.querySelector('[name="name"]');
    if (!name.value) name.value = candidate.path.split(/[\\/]/).pop();
    path.focus();
  });
  finder.hidden = true;
  kind.addEventListener("change", () => {
    path.parentElement.hidden = kind.value !== "file"; path.required = kind.value === "file";
    finder.hidden = kind.value !== "file";
  });
  formField(fields, "受信遅延と判定する秒数", "stale_after_seconds", { value: 300, type: "number", min: 1, max: 604800, full: true });
}
function logDiscovery(fields, choose) {
  const wrapper = el("section", "form-field full log-discovery");
  const title = el("h3", "", "ログの場所がわからない場合");
  const label = el("label", "", "アプリのフォルダー"); label.htmlFor = "log-search-root";
  const root = el("input"); root.id = "log-search-root"; root.placeholder = "/opt/my-app";
  root.setAttribute("aria-describedby", "log-search-help");
  const help = el("p", "field-hint", "OpSyne が動くサーバー上のフォルダーを指定してください。配下と、設定に書かれた別フォルダーのログ出力先を探します。"); help.id = "log-search-help";
  const results = el("div", "discovery-results"); results.setAttribute("aria-live", "polite");
  const epoch = state.modalEpoch;
  const reasons = {
    excluded: "依存フォルダー・秘密ファイルなどは探索対象外です",
    permission_denied: "読み取り権限がありません", missing: "指定先が見つかりません", not_directory: "フォルダーではありません",
    linked_path: "リンク先の探索は対象外です", special_file: "通常のファイルではありません", read_error: "読み取れませんでした",
    changed_file: "探索中にファイルが変わりました", binary_file: "テキストのログではありません",
    config_truncated: "設定が大きいため一部のみ確認しました", unresolved_reference: "変数などを含む出力先は手動確認が必要です",
    unsupported_reference: "対応していない形式の出力先です", unsupported_path: "ローカルのパスではありません",
    non_file_destination: "標準出力・journal などの出力先があります。転送方法の設定が必要です",
    config_depth: "設定の入れ子が深いため一部のみ確認しました", read_budget: "読み取り量の上限に達しました",
    sample_no_complete_line: "サンプル内に行全体が収まらないため分類できませんでした",
  };
  const signals = { http_requests: "HTTPアクセス", errors: "エラーの記録", lifecycle: "起動・停止の記録" };
  const search = button("ログを探す", async () => {
    if (!root.value.trim()) { results.replaceChildren(message("アプリのフォルダーを入力してください。", "error")); root.focus(); return; }
    await busy(search, async () => {
      results.replaceChildren(el("p", "muted", "ログを探しています…"));
      try {
        const result = await api("/log-discovery", { roots: [root.value.trim()] });
        if (state.modalEpoch !== epoch || !wrapper.isConnected || !$("#action-dialog").open) return;
        const summary = result.status === "partial" ? "一部を探索できませんでした。" : "指定範囲の探索が完了しました。";
        results.replaceChildren(message(`${summary} ${result.candidates.length} 件の候補が見つかりました。`));
        results.append(el("p", "field-hint", "選ぶと登録欄にパスが入ります。対象サービスのログか確認して登録してください。分類は少量のサンプルからの推定で、サービスの正常性を示すものではありません。"));
        for (const candidate of result.candidates) {
          const card = el("article", "discovery-candidate");
          const evidence = candidate.signals.map((signal) => signals[signal] || signal).join("・") || (candidate.format === "empty" ? "空のファイル：内容は未確認" : candidate.format === "unreadable" ? "本文の読み取り不可" : "ログの種類は未判定");
          append(card, el("strong", "", candidate.path), el("p", "field-hint", `${evidence} ／ 更新: ${date(candidate.modified_at)}`));
          for (const reference of candidate.references.slice(0, 3)) card.append(el("p", "field-hint", `設定の出力先: ${reference.config_path}${reference.line ? `:${reference.line}` : ""}`));
          card.append(button("このログを選ぶ", () => { choose(candidate); toast("ログのパスを入力しました。登録内容を確認してください。"); }, "button", candidate.format !== "unreadable"));
          results.append(card);
        }
        if (!result.candidates.length) results.append(el("p", "field-hint", "ログが存在しないとは限りません。フォルダーを変えて探すか、ログのパスを直接入力してください。"));
        if (result.limits_reached.length) results.append(message("探索上限に達しました。範囲を狭めて再度探してください。"));
        const notices = result.notices;
        if (notices.length) {
          const details = el("details"); details.append(el("summary", "", "探索できなかった場所・確認が必要な設定"));
          for (const notice of notices) details.append(el("p", "field-hint", `${notice.path}: ${reasons[notice.reason] || "確認が必要です"}`));
          results.append(details);
        }
      } catch (error) {
        if (state.modalEpoch === epoch && wrapper.isConnected) results.replaceChildren(message(error.message, "error"));
      }
    });
  });
  root.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); search.click(); } });
  append(wrapper, title, label, root, help, search, results); fields.append(wrapper);
  return wrapper;
}
function ingestForm(source) {
  const body = openDialog("イベントを取り込む", source.name);
  body.append(message("実際の原本を入力してください。外部イベント ID は同じイベントの再送で固定し、異なるイベントには別の ID を付けます。"));
  const { fields } = createForm(body, "受領・解析する", async (form) => {
    const result = await api(`/sources/${safeId(source.id)}/ingest`, { events: [{ external_id: form.get("external_id"), payload: form.get("payload") }] });
    $("#action-dialog").close(); toast("イベントの取込を受け付けました。"); await refresh(true);
    if (result?.errors?.length) toast(errorText(result.errors), true);
  });
  formField(fields, "外部イベント ID", "external_id", { full: true });
  formField(fields, "原本（JSON またはログ本文）", "payload", { type: "textarea", full: true, hint: "この本文はデータとして保存されます。本文に含まれる命令が操作を認可することはありません。" });
}
function demoForm() {
  const body = openDialog("合成データで動作を確認", "SYNTHETIC DEMO");
  append(body, message("隔離されたデモサービス、観測イベント、変換定義と操作能力を準備します。対象はすべて合成データです。実サービスへの接続は行いません。"), el("p", "analysis-text", "案件から調査・計画作成へ進み、別の主体で承認後に実行と独立確認を試せます。"));
  createForm(body, "合成データを追加", async () => {
    await api("/demo", {}); $("#action-dialog").close(); toast("合成データのデモを準備しました。"); await refresh(true);
  });
}
async function integrationDetails(service) {
  const body = openDialog(`${service.name} の接続設定`, "SERVICE CAPABILITIES");
  const epoch = state.modalEpoch;
  append(body, message("実行する HTTP 操作と、独立した読取確認を別々に登録します。対象と操作は設定に固定され、計画の承認・認可を経て実行されます。"));
  try {
    const [capabilityResponse, checkResponse] = await Promise.all([api("/capabilities"), api("/checks")]);
    if (state.modalEpoch !== epoch) return;
    const capabilities = (Array.isArray(capabilityResponse) ? capabilityResponse : capabilityResponse.capabilities || []).filter((item) => item.service_id === service.id);
    const check = Array.isArray(checkResponse) ? checkResponse.find((item) => item.service_id === service.id) : (checkResponse[service.id] || checkResponse.checks?.[service.id]);
    const capabilitySection = section("登録された操作能力");
    if (!capabilities.length) capabilitySection.append(el("p", "analysis-text", "登録された操作能力はありません。"));
    for (const capability of capabilities) capabilitySection.append(jsonDetails(`${capability.name} · v${capability.version}`, capability));
    capabilitySection.append(append(el("div", "card-actions"), button("操作能力を登録", () => capabilityForm(service), "button", can("configure"))));
    const checkSection = section("独立した結果確認");
    if (check) checkSection.append(jsonDetails("登録された読取確認", check, true));
    else checkSection.append(el("p", "analysis-text", "独立確認は未設定です。未設定・確認不能は復旧と判断されません。"));
    checkSection.append(append(el("div", "card-actions"), button("独立確認を設定", () => checkForm(service, check), "button", can("configure"))));
    append(body, capabilitySection, checkSection);
  } catch (error) { if (state.modalEpoch === epoch) body.append(message(error.message, "error")); }
}
function capabilityForm(service) {
  const body = openDialog("HTTP 操作能力を登録", service.name);
  body.append(message("登録された URL・HTTP メソッド・本文を持つ操作を作成します。認証が必要な場合はサーバーの環境変数名を指定します。秘密の値を本文に書かないでください。"));
  const { fields } = createForm(body, "操作能力を登録", async (form) => {
    const payload = Object.fromEntries(form);
    payload.service_id = service.id; payload.kind = "http.request"; payload.version = Number(payload.version);
    if (!payload.auth_env) delete payload.auth_env;
    try { payload.body = JSON.parse(payload.body); } catch { throw new Error("HTTP 本文には有効な JSON オブジェクトを入力してください。"); }
    if (!payload.body || typeof payload.body !== "object" || Array.isArray(payload.body)) throw new Error("HTTP 本文は JSON オブジェクトで指定してください。");
    await api("/capabilities", payload); toast("HTTP 操作能力を登録しました。"); await refresh(); await integrationDetails(service);
  });
  formField(fields, "操作能力 ID", "id", identifierOptions()); formField(fields, "表示名", "name");
  formField(fields, "操作先 URL", "endpoint", { type: "url", full: true });
  formField(fields, "HTTP メソッド", "method", { options: [["POST", "POST"], ["PUT", "PUT"], ["PATCH", "PATCH"], ["DELETE", "DELETE"]], value: "POST" });
  formField(fields, "操作能力の版", "version", { type: "number", min: 1, value: 1 });
  formField(fields, "認証トークンの環境変数名", "auth_env", { full: true, required: false, pattern: "[A-Za-z_][A-Za-z0-9_]*", hint: "例: SERVICE_OPERATION_TOKEN。トークンの値は入力しません。" });
  const bodyInput = formField(fields, "固定された HTTP 本文（JSON オブジェクト）", "body", { type: "textarea", value: "{}", full: true }); bodyInput.spellcheck = false;
}
function checkForm(service, existing) {
  const configured = existing?.check || existing?.config || existing;
  const body = openDialog("独立した結果確認を設定", service.name);
  body.append(message("GET リクエストで実行後の実状態を観測します。操作 API の成功応答から独立した URL と条件を設定してください。"));
  const { fields } = createForm(body, "独立確認を保存", async (form) => {
    const payload = Object.fromEntries(form); payload.kind = "http"; payload.expected_status = Number(payload.expected_status);
    if (!payload.auth_env) delete payload.auth_env;
    if (!payload.body_contains) delete payload.body_contains;
    await api(`/services/${safeId(service.id)}/check`, payload); toast("独立した結果確認を設定しました。"); await refresh(); await integrationDetails(service);
  });
  formField(fields, "読取確認の URL", "endpoint", { type: "url", value: configured?.endpoint || "", full: true });
  formField(fields, "期待する HTTP ステータス", "expected_status", { type: "number", min: 100, max: 599, value: configured?.expected_status || 200, full: true });
  formField(fields, "応答本文に必要な文字列", "body_contains", { value: configured?.body_contains || "", required: false, full: true, hint: "未指定の場合、HTTP ステータスだけを確認します。業務条件を判定できる内容を指定してください。" });
  formField(fields, "読取認証トークンの環境変数名", "auth_env", { value: configured?.auth_env || "", required: false, full: true, pattern: "[A-Za-z_][A-Za-z0-9_]*", hint: "操作用の認証情報とは分離した読取専用の資格情報を使用します。" });
}
async function recoveryDetails() {
  const body = openDialog("復元・実行保留の確認", "RECOVERY REVIEW", true);
  const epoch = state.modalEpoch;
  body.append(message("対象サービス側の操作履歴と、保存された台帳を照合してください。通常の正常性チェックだけでは、未送信やバックアップの整合性を確認できません。", "warning"));
  try {
    const overview = await api("/overview");
    if (state.modalEpoch !== epoch) return;
    const recovery = overview.recovery;
    if (!recovery || !can("configure")) throw new Error("管理者用の復元状態を取得できません。");
    const quarantine = section("復元後の実行制限");
    if (recovery.quarantined) {
      append(quarantine, message("バックアップ後に対象側で行われた操作を照合するまで、新しい実行許可は発行されません。照合完了を記録しても、古い承認が再び有効になることはありません。", "warning"), button("復元前後の照合結果を記録", () => recoveryForm(null), "button"));
    } else quarantine.append(el("p", "analysis-text", "復元後の照合による実行制限は記録されていません。"));
    body.append(quarantine);
    const holds = section(`実行予約 (${recovery.holds?.length || 0})`);
    if (!recovery.holds?.length) holds.append(el("p", "analysis-text", "保留中の実行予約はありません。"));
    for (const hold of recovery.holds || []) {
      const card = el("article", "plan-card");
      append(card, info([["対象サービス", serviceName(hold.service_id)], ["計画", hold.plan_id], ["許可", hold.permit_id], ["実行台帳", hold.has_execution ? "記録あり" : "記録なし"]]));
      if (hold.has_execution) {
        card.append(message("実行台帳に記録がある予約はここで解除できません。案件の実行台帳から操作結果を照合してください。"));
        const plan = rows("plans").find((item) => planObject(item).id === hold.plan_id);
        if (plan) card.append(button("案件の実行台帳を開く", () => openCase(planObject(plan).case_id), "button small"));
      } else card.append(append(el("div", "card-actions"), button("未送信の照合結果を記録", () => recoveryForm(hold), "button small")));
      holds.append(card);
    }
    body.append(holds);
  } catch (error) { if (state.modalEpoch === epoch) body.append(message(error.message, "error")); }
}
function recoveryForm(hold) {
  const body = openDialog(hold ? "未送信の実行予約を解消" : "復元前後の照合完了を記録", "ADMINISTRATOR RECOVERY REVIEW");
  body.append(message(hold
    ? "対象サービスの履歴を確認し、この計画の操作が送信されていない根拠を記録します。操作済み・結果不明・照合不能の場合は解除しないでください。解除した計画は却下され、新しい計画と承認が必要です。"
    : "バックアップ以降の対象側の操作履歴、原本・承認・実行台帳の整合性を確認し、根拠を記録してください。確認不能な範囲が残る場合は実行制限を維持してください。", "warning"));
  if (hold) body.append(jsonDetails("対象の実行予約", hold, true));
  const { fields, submit } = createForm(body, hold ? "未送信の予約を解除" : "照合完了を記録", async (form) => {
    const reason = String(form.get("reason") || "").trim();
    if (reason.length < 20) throw new Error("照合した操作履歴と確認根拠を20文字以上で記録してください。");
    await api(hold ? `/recovery/unsent/${safeId(hold.plan_id)}` : "/recovery/acknowledge", { reason });
    toast(hold ? "未送信の予約を解消しました。計画は却下されました。" : "復元前後の照合完了を記録しました。古い承認は失効したままです。");
    await refresh(); await recoveryDetails();
  });
  const reason = formField(fields, "照合した履歴・時刻・結果とその根拠", "reason", { type: "textarea", full: true, hint: "20〜2000文字。正常性チェックの結果だけを根拠にしないでください。" }); reason.minLength = 20; reason.maxLength = 2000;
  const confirmation = el("label", "check-line form-field full"); const checkbox = el("input"); checkbox.type = "checkbox";
  append(confirmation, checkbox, document.createTextNode(hold ? "対象側の操作履歴を照合し、未送信であることを確認しました。" : "復元前後の操作履歴を照合し、新しい計画の認可を再開できることを確認しました。")); fields.append(confirmation);
  submit.disabled = true;
  const update = () => { submit.disabled = submit.getAttribute("aria-busy") === "true" || !can("configure") || !checkbox.checked || reason.value.trim().length < 20; };
  checkbox.addEventListener("change", update); reason.addEventListener("input", update); submit.addEventListener("opsyne:idle", update);
}
function adapterForm() {
  const source = rows("sources")[0]; const service = rows("services").find((item) => item.id === source?.service_id);
  const definition = {
    id: "", name: "", source_id: source?.id || "", target_instance_id: service?.instance_id || "", target_version: service?.version || 1, version: 1,
    fields: { message: "message", outcome: "status", severity: "level" }, conditions: {},
    outcome_map: { ok: "SUCCESS", error: "FAILURE" }, severity_map: { info: "INFO", warn: "WARNING", error: "ERROR" },
  };
  const body = openDialog("変換定義を作成", "DECLARATIVE ADAPTER");
  body.append(message("項目は JSON のキーへの対応です。値の意味・対象実体・対象版・適用条件を確認してください。未知の値は正常に変換されません。"));
  const { fields } = createForm(body, "承認待ちとして保存", async (form) => {
    let payload;
    try { payload = JSON.parse(form.get("definition")); } catch { throw new Error("JSON の構文に誤りがあります。括弧・カンマ・引用符を確認してください。"); }
    payload.explanation = readExplanation(form);
    await api("/adapters", payload); $("#action-dialog").close(); toast("変換定義を保存しました。別の主体による確認と承認が必要です。"); await refresh(true);
  });
  const editor = formField(fields, "宣言的な変換定義（JSON）", "definition", { type: "textarea", value: JSON.stringify(definition, null, 2), full: true }); editor.classList.add("json-input"); editor.spellcheck = false;
  explanationFields(fields);
}
function explanationFields(fields, human = {}) {
  const purpose = formField(fields, "監視目的（利用者が決めた目的）", "monitoring_purpose", { type: "textarea", value: human?.monitoring_purpose || "", full: true, required: false, hint: "不明なら空欄のまま保存します。Agentの用途案からは補完しません。" });
  purpose.maxLength = 2000;
  for (const [key, label, limit] of [["expected_insights", "変換によって把握したいこと", 30], ["rationale", "利用者の根拠", 30], ["questions", "確認事項", 50]]) {
    formField(fields, label, key, { type: "textarea", value: (human?.[key] || []).join("\n"), full: true, required: false, hint: `1行に1項目。最大${limit}件、各2000文字。` });
  }
}
function readExplanation(form) {
  const value = { monitoring_purpose: String(form.get("monitoring_purpose") || "").trim() || null };
  for (const [key, limit] of [["expected_insights", 30], ["rationale", 30], ["questions", 50]]) {
    value[key] = String(form.get(key) || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (value[key].length > limit || value[key].some((line) => line.length > 2000)) throw new Error(`項目数は${limit}件以内、各項目は2000文字以内にしてください。`);
  }
  return value;
}
function explanationList(title, values) {
  const list = el("ul", "detail-list");
  for (const value of values || []) list.append(el("li", "analysis-text", value));
  return section(title, list.children.length ? list : el("p", "analysis-text", "未記録"));
}
function renderAdapterExplanation(record) {
  const human = record.explanation?.human; const agent = record.explanation?.agent;
  const root = el("div", "adapter-explanation");
  root.append(section("利用者の監視目的", el("p", "analysis-text", human?.monitoring_purpose || "未記録"), el("p", "field-hint", `記録者: ${record.explanation?.recorded_by || "未記録"}`)));
  root.append(explanationList("変換によって把握したいこと", human?.expected_insights), explanationList("利用者の根拠", human?.rationale), explanationList("利用者の確認事項", human?.questions));
  root.append(section("Agentが提案した用途", el("p", "analysis-text", agent?.suggested_use || "未記録"), el("p", "field-hint", "Agentの用途案は、利用者が決めた監視目的とは別の情報です。")));
  const claims = el("div");
  for (const claim of agent?.rationale || []) claims.append(append(el("div", "evidence-item"), el("p", "analysis-text", claim.text), el("p", "field-hint", `原本参照: ${(claim.evidence_ids || []).join(" / ") || "未記録"}`)));
  root.append(section("Agentの提案根拠", claims.children.length ? claims : el("p", "analysis-text", "未記録")), explanationList("Agentが判断できないこと・確認事項", agent?.unknowns));
  if (agent?.task_id) root.append(el("p", "field-hint", `生成タスク: ${agent.task_id}`));
  return root;
}
function editAdapterExplanation(record) {
  const body = openDialog("監視目的・説明を編集", "REVIEW EXPLANATION");
  body.append(message("保存すると承認対象の説明が更新されます。保存後の内容を確認し、別の承認主体が承認してください。"));
  body.append(button("最新の定義を読み直す", (event) => busy(event.currentTarget, async () => {
    await openAdapter(record);
  })));
  const { fields } = createForm(body, "説明を保存して再確認", async (form) => {
    try {
      const updated = await api(`/adapters/${safeId(record.id)}/explanation`, { digest: record.digest, explanation: readExplanation(form) }, state.token, "PUT");
      await openAdapter(updated); toast("説明を保存しました。更新後の内容を再確認してください。"); await refresh(true);
    } catch (error) {
      if (error.status === 409) throw new Error("他の画面で変更または承認されています。入力は保存していません。「最新の定義を読み直す」で内容を確認してください。");
      throw error;
    }
  });
  explanationFields(fields, record.explanation?.human);
}
async function openAdapter(record) {
  const body = openDialog("変換定義を取得中", "REVIEW ADAPTER");
  const epoch = state.modalEpoch;
  body.append(el("p", "analysis-text", "最新の定義と説明を取得しています…"));
  try {
    const latest = await api(`/adapters/${safeId(adapterObject(record).id)}`);
    if (state.modalEpoch === epoch && $("#action-dialog").open) renderAdapterReview(latest);
  } catch (error) {
    if (state.modalEpoch === epoch) body.replaceChildren(message(error.message, "error"));
  }
}
function renderAdapterReview(record) {
  const definition = adapterObject(record); const status = record.status || (record.active ? "ACTIVE" : "DRAFT");
  const body = openDialog(definition.name || "変換定義", "REVIEW ADAPTER");
  append(body, badge(status), renderAdapterExplanation(record), jsonDetails("固定された定義の全体", definition), el("code", "digest", `SHA-256 ${record.digest || definition.digest || "未記録"}`));
  body.append(button("最新の定義を読み直す", (event) => busy(event.currentTarget, async () => {
    await openAdapter(record);
  })));
  if (status === "DRAFT" && (can("configure") || can("operate"))) body.append(button("監視目的・説明を編集", () => editAdapterExplanation(record)));
  const preview = el("div");
  body.append(append(el("div", "card-actions"), button("標本で検証", (event) => busy(event.currentTarget, async () => {
    const result = await api(`/adapters/${safeId(definition.id)}/validate`, {});
    if (!Number.isInteger(result?.sample_count) || !Number.isInteger(result?.supported_count) || !Array.isArray(result?.samples)) throw new Error("標本検証の結果を確認できません。");
    preview.replaceChildren(
      section("保存された原本による標本検証", info([["検証した標本", result.sample_count], ["KNOWN / PARTIAL", result.supported_count]]),
        message(result.supported_count > 0 ? "適用可能な標本が見つかりました。値の意味と適用範囲は、承認前に内容を確認してください。" : "適用可能な標本はありません。承認には対応可能な原本が必要です。", result.supported_count > 0 ? "info" : "warning"),
        el("code", "digest", `検証対象 SHA-256 ${result.digest}`), jsonDetails("標本ごとの解釈結果", result.samples, true)),
    );
  }), "button")));
  body.append(preview);
  const actions = el("div", "form-actions");
  if (status === "DRAFT" || status === "PENDING") {
    let stale = false;
    const contributed = selfProposed(record) || (record.explanation_editors || []).includes(state.session?.actor);
    const approveAllowed = can("approve") && !contributed && Boolean(record.digest || definition.digest);
    const confirm = el("label", "check-line"); const checkbox = el("input"); checkbox.type = "checkbox";
    append(confirm, checkbox, document.createTextNode("対象・版・項目と値の意味・適用条件・監視目的・提案理由・確認事項（未記録の項目を含む）を確認し、この定義の有効化を承認します。")); body.append(confirm);
    const approve = button("この定義を承認", (event) => busy(event.currentTarget, async () => {
      try { await api(`/adapters/${safeId(definition.id)}/approve`, { digest: record.digest || definition.digest }); }
      catch (error) {
        if (error.status === 409) {
          stale = true; checkbox.checked = false;
          const notice = message("承認対象が変更されたか、現在の条件で承認できません。「最新の定義を読み直す」で説明と定義を再確認してください。", "warning");
          notice.setAttribute("role", "alert"); body.prepend(notice);
        }
        throw error;
      }
      $("#action-dialog").close(); toast("確認した変換定義を承認しました。"); await refresh(true);
    }), "button primary", false);
    approve.title = approveAllowed ? "内容を確認してチェックを入れてください" : "承認権限と、提案者・説明編集者とは異なる主体が必要です";
    const update = () => { approve.disabled = approve.getAttribute("aria-busy") === "true" || stale || !approveAllowed || !checkbox.checked; };
    checkbox.addEventListener("change", update); approve.addEventListener("opsyne:idle", update); actions.append(approve);
    if (!approveAllowed) body.append(el("p", "role-note", contributed ? "提案者・説明編集者は自己承認できません。別の承認主体で確認してください。" : "承認権限を持つ主体と、記録済みの digest が必要です。"));
  }
  if (["ACTIVE", "APPROVED"].includes(status)) {
    actions.append(button("原本を再解析", (event) => busy(event.currentTarget, async () => {
      await api(`/adapters/${safeId(definition.id)}/reprocess`, {}); $("#action-dialog").close(); toast("再解析を実施しました。対象操作は実行されません。"); await refresh(true);
    }), "button", can("operate")));
    actions.append(button("この定義を失効", (event) => busy(event.currentTarget, async () => {
      await api(`/adapters/${safeId(definition.id)}/revoke`, {}); $("#action-dialog").close(); toast("変換定義を失効しました。"); await refresh(true);
    }), "button danger", can("approve")));
  }
  body.append(actions);
}
async function openCase(id) {
  state.caseId = id;
  const body = openDialog("案件を読み込み中…", "CASE WORKSPACE", true); const epoch = state.modalEpoch;
  body.append(append(el("div", "loading-card"), el("span", "spinner"), el("p", "", "根拠と対応状況を取得しています…")));
  try {
    const detail = await api(`/cases/${safeId(id)}`);
    if (epoch !== state.modalEpoch || !$("#action-dialog").open) return;
    renderCaseDetail(body, detail);
  } catch (error) { if (epoch === state.modalEpoch) body.replaceChildren(message(error.message, "error")); }
}
function section(title, ...children) { return append(el("section", "detail-section"), el("h3", "", title), ...children); }
function renderCaseDetail(body, detail) {
  const item = detail.case; $("#dialog-title").textContent = item.title || item.id; body.replaceChildren();
  for (const job of detail.recoveries || []) body.append(message(`${job.status}：${job.detail}`));
  if (detail.recovery_proposal) body.append(message(detail.recovery_proposal.detail));
  const statusNode = badge(item.status);
  append(body, append(el("div", "detail-topline"), severity(item.severity), statusNode, el("code", "detail-id", item.id)), info([["対象サービス", serviceName(item.service_id)], ["観測源", sourceName(item.source_id)], ["作成日時", date(item.created_at, true)], ["更新日時", date(item.updated_at, true)]]));
  const proposalButton = button("変換案を手動生成", (event) => busy(event.currentTarget, async () => {
    await api(`/cases/${safeId(item.id)}/adapter-proposal`, {}); toast("変換案の生成を受け付けました。標本と競合の検証後、承認待ちの定義として追加されます。"); await refresh(); await openCase(item.id);
  }), "button", can("operate"));
  configureProposalButton(proposalButton, detail.adapter_discovery, item.id);
  proposalButton.addEventListener("opsyne:idle", () => configureProposalButton(proposalButton, state.caseLive?.discoveryValue ? JSON.parse(state.caseLive.discoveryValue) : detail.adapter_discovery, item.id));
  const actions = append(el("div", "detail-actions"), button("調査を実行", (event) => busy(event.currentTarget, async () => {
    await api(`/cases/${safeId(item.id)}/investigate`, {}); toast("調査タスクを受け付けました。完了後に調査結果へ反映されます。"); await refresh(); await openCase(item.id);
  }), "button primary", can("operate")), proposalButton, button("対応計画を作成", () => planForm(item), "button", can("operate")), button("↻ 更新", () => openCase(item.id), "button"));
  body.append(section("対応を進める", actions));
  const liveDiscovery = el("div");
  if (detail.adapter_discovery) liveDiscovery.append(section("未知形式の変換提案", discoveryCard(detail.adapter_discovery)));
  body.append(liveDiscovery);
  const liveTasks = el("div"); append(liveTasks, latestTask(item.id)); body.append(liveTasks);
  const analysis = detail.analysis || item.analysis;
  const liveAnalysis = section("調査結果", analysis ? renderAnalysis(analysis) : message("調査結果はまだ得られていません。根拠のある事実・仮説・不足情報を分けて記録します。")); body.append(liveAnalysis);
  const liveAdapterAnalysis = el("div");
  if (detail.adapter_analysis && detail.adapter_analysis.task_id !== analysis?.task_id) liveAdapterAnalysis.append(section("変換提案の根拠・不明点", renderAnalysis(detail.adapter_analysis)));
  body.append(liveAdapterAnalysis);
  const evidence = Array.isArray(detail.evidence) ? detail.evidence : [];
  const evidenceSection = section(`根拠と原本 (${evidence.length})`);
  if (!evidence.length) evidenceSection.append(message("参照できる原本はありません。観測停止の案件などでは、収集状態が根拠になります。"));
  for (const entry of evidence) {
    const raw = entry.raw || entry; const node = el("div", "evidence-item");
    const parseStatus = entry.interpretation?.parse_status;
    append(node, append(el("div", "evidence-meta"), el("code", "", raw.id || entry.raw_ref || "原本"), el("span", "", date(raw.received_at || raw.observed_at)), parseStatus === "UNKNOWN" ? el("span", "badge amber", "未知形式") : parseStatus ? badge(parseStatus) : null));
    if (raw.payload !== undefined) node.append(el("pre", "", raw.payload));
    node.append(jsonDetails("原本参照・派生解釈・版を確認", entry)); evidenceSection.append(node);
  }
  body.append(evidenceSection);
  const plans = Array.isArray(detail.plans) ? detail.plans : [];
  const planSection = section(`対応計画 (${plans.length})`);
  if (!plans.length) planSection.append(el("p", "analysis-text", "対応計画はまだ作成されていません。登録済みの操作能力から計画を作成できます。"));
  for (const record of plans) planSection.append(planCard(record, item)); body.append(planSection);
  const executions = Array.isArray(detail.executions) ? detail.executions : [];
  const executionSection = section(`実行台帳と独立確認 (${executions.length})`);
  if (!executions.length) executionSection.append(el("p", "analysis-text", "実行記録はありません。操作の前に意図を永続化し、結果不明は照合まで保留します。"));
  for (const execution of executions) executionSection.append(executionCard(execution, item)); body.append(executionSection);
  state.caseLive = { id: item.id, epoch: state.modalEpoch, analysis: liveAnalysis, analysisValue: JSON.stringify(analysis), adapterAnalysis: liveAdapterAnalysis, adapterAnalysisValue: JSON.stringify([detail.adapter_analysis, analysis?.task_id]), tasks: liveTasks, tasksValue: JSON.stringify(rows("tasks")), status: statusNode, discovery: liveDiscovery, discoveryValue: JSON.stringify(detail.adapter_discovery), proposalButton };
}
function latestTask(caseId) {
  const tasks = rows("tasks").filter((task) => task.case_id === caseId).sort((left, right) => right.created_at - left.created_at);
  if (!tasks.length) return null;
  const latest = tasks[0];
  return section(`最新のタスク · ${taskRoleNames[latest.role] || latest.role}`, badge(latest.status === "SUCCEEDED" ? "COMPLETED" : latest.status), el("p", "analysis-text", latest.detail || (["RUNNING", "PENDING"].includes(latest.status) ? "処理を受け付けています。状態と調査結果は10秒ごとに取得します。" : "タスクの状態を記録しました。")), el("p", "field-hint", `${latest.id} · ${date(latest.created_at, true)}`));
}
async function refreshCaseSignals() {
  const live = state.caseLive;
  const detail = await api(`/cases/${safeId(live.id)}`);
  if (live !== state.caseLive || live.epoch !== state.modalEpoch) return;
  const status = badge(detail.case.status); live.status.className = status.className; live.status.textContent = status.textContent;
  const analysis = detail.analysis || detail.case.analysis;
  const serialized = JSON.stringify(analysis);
  // Leave plan-review inputs, expanded evidence, and focused controls in place.
  if (serialized !== live.analysisValue && !live.analysis.contains(document.activeElement)) {
    live.analysis.replaceChildren(el("h3", "", "調査結果"), analysis ? renderAnalysis(analysis) : message("調査結果はまだ記録されていません。"));
    live.analysisValue = serialized;
  }
  const serializedAdapterAnalysis = JSON.stringify([detail.adapter_analysis, analysis?.task_id]);
  if (serializedAdapterAnalysis !== live.adapterAnalysisValue && !live.adapterAnalysis.contains(document.activeElement)) {
    live.adapterAnalysis.replaceChildren();
    if (detail.adapter_analysis && detail.adapter_analysis.task_id !== analysis?.task_id) live.adapterAnalysis.append(section("変換提案の根拠・不明点", renderAnalysis(detail.adapter_analysis)));
    live.adapterAnalysisValue = serializedAdapterAnalysis;
  }
  const serializedTasks = JSON.stringify(rows("tasks"));
  if (serializedTasks !== live.tasksValue) {
    live.tasks.replaceChildren(); append(live.tasks, latestTask(live.id)); live.tasksValue = serializedTasks;
  }
  const serializedDiscovery = JSON.stringify(detail.adapter_discovery);
  if (serializedDiscovery !== live.discoveryValue && !live.discovery.contains(document.activeElement)) {
    live.discovery.replaceChildren();
    if (detail.adapter_discovery) live.discovery.append(section("未知形式の変換提案", discoveryCard(detail.adapter_discovery)));
    live.discoveryValue = serializedDiscovery;
  }
  configureProposalButton(live.proposalButton, detail.adapter_discovery, live.id);
}
function renderAnalysis(analysis) {
  const wrapper = el("div");
  if (analysis.status) wrapper.append(badge(analysis.status));
  const content = analysis.output || analysis.result || analysis;
  if (content.summary) wrapper.append(el("p", "analysis-text", content.summary));
  for (const [field, title] of [["facts", "事実"], ["hypotheses", "仮説"], ["unknowns", "不明点"], ["recommendations", "提案"]]) {
    if (!content[field]?.length) continue;
    wrapper.append(el("h3", "", title)); const list = el("ul", "detail-list");
    for (const entry of content[field]) {
      const claim = el("li", "", typeof entry === "string" ? entry : entry.text || JSON.stringify(entry));
      if (entry.evidence_ids?.length) claim.append(el("div", "field-hint", `根拠: ${entry.evidence_ids.join(", ")}`));
      list.append(claim);
    }
    wrapper.append(list);
  }
  wrapper.append(jsonDetails("調査の全体・根拠・実行状態", analysis)); return wrapper;
}
function planCard(record, item) {
  const plan = planObject(record); const card = el("article", "plan-card");
  append(card, append(el("div", "plan-card-header"), el("h4", "", plan.capability_id || plan.id), badge(record.status || "DRAFT")), info([["対象 / 版", `${plan.target_instance_id} / v${plan.target_version}`], ["有効期限", date(plan.expires_at, true)], ["提案者", plan.proposer || record.proposed_by], ["提案理由", plan.reason]]));
  const actions = el("div", "card-actions");
  actions.append(button("固定計画を確認", () => reviewPlan(record, item), "button small"));
  if (record.status === "APPROVED") {
    card.append(message("承認済みです。実行記録を確認し、未実行の場合は実行権限のある担当者が復旧処理を開始してください。"));
    actions.append(button("復旧処理を開始", () => reviewPlan(record, item, true), "button primary small", can("operate")));
  }
  card.append(actions); return card;
}
async function planForm(item) {
  const body = openDialog("対応計画を作成", "PROPOSE A PLAN"); const epoch = state.modalEpoch;
  body.append(message("登録済みの復旧操作を選び、実行する内容を計画として保存します。提案者とは別の管理者が承認すると、復旧処理と正常性の確認まで進みます。承認専用の担当者が承認した場合は、実行権限のある担当者が開始します。"));
  try {
    const response = await api("/capabilities");
    if (state.modalEpoch !== epoch) return;
    const capabilities = (Array.isArray(response) ? response : response.capabilities || []).filter((capability) => capability.service_id === item.service_id);
    if (!capabilities.length) { body.append(message("このサービスには操作能力が登録されていません。管理者がサーバー設定に許可された操作と独立確認方法を登録してください。", "warning")); return; }
    const { fields } = createForm(body, "計画を保存", async (form) => {
      await api(`/cases/${safeId(item.id)}/plans`, Object.fromEntries(form)); toast("固定された対応計画を作成しました。"); await refresh(); await openCase(item.id);
    });
    formField(fields, "操作能力", "capability_id", { full: true, options: capabilities.map((capability) => [capability.id, `${capability.name} (${capability.id} / v${capability.version})`]) });
    formField(fields, "対応する理由", "reason", { full: true, type: "textarea", hint: "根拠と、対応が必要と判断した理由を記載してください。" });
    body.append(jsonDetails("登録された操作能力", capabilities));
  } catch (error) { if (state.modalEpoch === epoch) body.append(message(error.message, "error")); }
}
function reviewPlan(record, item, execute = false) {
  const plan = planObject(record); const status = record.status || "DRAFT";
  const runRecovery = execute || can("approve");
  const body = openDialog(execute ? "復旧処理を開始" : "復旧計画を確認", "PLAN REVIEW", true);
  $("#action-dialog").classList.add("recovery-review");
  const epoch = state.modalEpoch;
  const session = state.session; const token = state.token;
  const isCurrent = () => state.modalEpoch === epoch && $("#action-dialog").open && state.session === session && state.token === token;
  let operationReady = false;
  let updateApprovalState = () => {};
  const planStatus = append(el("div"), badge(status));
  append(body, planStatus, info([["計画 ID", plan.id], ["提案者", plan.proposer], ["対象サービス", serviceName(plan.service_id)], ["対象実体 / 版", `${plan.target_instance_id} / v${plan.target_version}`], ["操作能力 / 版", `${plan.capability_id} / v${plan.capability_version}`], ["有効期限", date(plan.expires_at, true)]]));
  for (const [key, title] of [["reason", "対応理由"], ["impact", "想定される影響"], ["success_condition", "独立して確認する成功条件"], ["abort_condition", "中止条件"]]) body.append(section(title, el("p", "analysis-text", plan[key] || "未記録")));
  body.append(section("証拠参照", el("code", "", (plan.evidence_ids || []).join("\n") || "参照なし")));
  const operationSection = section("操作の内容", el("p", "analysis-text", "登録された操作能力の内容を取得しています…"));
  body.append(operationSection);
  api("/capabilities").then((response) => {
    if (state.modalEpoch !== epoch) return;
    const operation = (Array.isArray(response) ? response : response.capabilities || []).find((capability) => capability.id === plan.capability_id && capability.version === plan.capability_version);
    operationSection.replaceChildren(el("h3", "", "操作の内容"));
    if (operation) {
      operationSection.append(jsonDetails(`単一操作: ${operation.name} / v${operation.version}`, operation, true));
      operationReady = true; updateApprovalState();
    }
    else operationSection.append(message("計画に記録された版の操作能力を取得できません。承認前に登録内容を確認してください。", "warning"));
  }).catch((error) => { if (state.modalEpoch === epoch) operationSection.replaceChildren(message(error.message, "error")); });
  body.append(jsonDetails("確認時の固定計画（すべてのフィールド）", plan));
  body.append(el("code", "digest", `SHA-256 ${plan.digest || "未記録"}`));
  const expired = Number(plan.expires_at) * 1000 <= Date.now();
  if (expired) body.append(message("この計画は期限切れです。新しい計画を作成して、改めて確認・承認してください。", "warning"));
  const isSelf = selfProposed(plan);
  if (status === "DRAFT" && isSelf) body.append(el("p", "role-note", "提案者は自己承認できません。別の承認主体で内容を確認してください。"));
  if (execute) body.append(message("復旧処理の結果を受け取った後、この画面から正常性確認を要求します。完了まで画面を開いておいてください。"));
  else if (runRecovery) body.append(message("承認後はサーバーが復旧処理と正常性確認を進めます。受付後は画面を閉じても処理を継続します。対象変更や期限切れの場合は停止します。"));
  if (status === "DRAFT" || (execute && status === "APPROVED")) {
    const footer = el("div", "recovery-footer");
    const confirm = el("label", "check-line"); const checkbox = el("input"); checkbox.type = "checkbox";
    append(confirm, checkbox, document.createTextNode(execute ? "対象・操作・影響・確認条件を確認しました。復旧処理を開始します。" : runRecovery ? "対象・操作・影響・成功条件・中止条件・期限を確認しました。この計画を承認し、復旧処理を開始します。" : "対象・版・根拠・影響・成功条件・中止条件・期限を確認しました。この固定計画を承認します。")); footer.append(confirm);
    const allowed = !expired && Boolean(plan.digest) && (execute ? can("operate") : can("approve") && !isSelf);
    const progress = el("ol", "detail-list recovery-progress"); progress.hidden = true;
    const steps = [el("li", "", execute ? "承認：承認済み" : "承認：待機中"), el("li", "", "復旧処理：待機中"), el("li", "", "復旧確認：待機中")];
    append(progress, ...steps); progress.setAttribute("aria-label", "復旧の進捗"); body.append(progress);
    const feedback = message(""); feedback.hidden = true; feedback.setAttribute("role", "status"); feedback.setAttribute("aria-live", "polite"); body.append(feedback);
    const actions = el("div", "form-actions");
    let attempted = false;
    const reject = !execute ? button("却下する", () => rejectForm(plan, item), "button danger", can("approve")) : null;
    if (reject) actions.append(reject);
    const showFeedback = (text, type = "info") => { feedback.textContent = text; feedback.className = `message ${type}`; feedback.hidden = false; feedback.scrollIntoView({ block: "nearest" }); };
    const records = button("案件と実行記録を確認", (event) => busy(event.currentTarget, async () => { await refresh(); if (isCurrent()) await openCase(item.id); }));
    records.hidden = true;
    const submit = button(execute ? "復旧を実行して確認" : runRecovery ? "承認して復旧を実行" : "この計画を承認", (event) => busy(event.currentTarget, async () => {
      if (attempted || !isCurrent()) return;
      attempted = true; checkbox.disabled = true; confirm.hidden = true; if (reject) { reject.disabled = true; reject.hidden = true; }
      progress.hidden = false; progress.scrollIntoView({ block: "nearest" });
      let phase = execute ? 1 : 0;
      try {
        if (!execute) {
          steps[0].textContent = "承認：処理中";
          let job = await api(`/plans/${safeId(plan.id)}/approve-and-execute`, { digest: plan.digest }, token);
          if (!isCurrent()) return;
          phase = 1;
          steps[0].textContent = "承認：完了";
          planStatus.replaceChildren(badge("APPROVED"));
          while (isCurrent()) {
            steps[1].textContent = `復旧処理：${job.status === "QUEUED" ? "実行待ち" : job.status === "EXECUTING" ? "実行中" : job.status === "VERIFYING" ? "操作成功" : statuses[job.status]?.[0] || job.status}`;
            steps[2].textContent = `復旧確認：${job.status === "VERIFYING" ? "確認中" : job.status === "COMPLETED" ? "正常性を確認" : "未完了"}`;
            showFeedback(job.detail + " 受付後は画面を閉じても処理を継続します。", ["BLOCKED", "FAILED", "UNKNOWN", "CHECK_FAILED"].includes(job.status) ? "warning" : "info");
            if (!["QUEUED", "EXECUTING", "VERIFYING"].includes(job.status)) break;
            await new Promise(resolve => setTimeout(resolve, 1000));
            if (!isCurrent()) return;
            job = await api(`/plans/${safeId(plan.id)}/recovery`, undefined, token);
          }
          return;
        }
        phase = 1; steps[1].textContent = "復旧処理：実行中";
        const execution = await api(`/plans/${safeId(plan.id)}/execute`, {}, token);
        if (!isCurrent()) return;
        if (!execution?.id || execution.plan_id !== plan.id) throw new Error("この計画の実行記録を取得できませんでした。");
        steps[1].textContent = `復旧処理：${statuses[execution.status]?.[0] || "結果不明"}`;
        if (["SUCCEEDED", "FAILED"].includes(execution.status)) planStatus.replaceChildren(badge("EXECUTED"));
        if (execution.status !== "SUCCEEDED") {
          steps[2].textContent = "復旧確認：未実施";
          showFeedback(execution.status === "FAILED" ? "復旧処理が失敗しました。実行記録で原因を確認してください。自動で再実行しません。" : "復旧処理の完了を確認できません。再実行せず、実行記録から操作結果を照合してください。", "warning");
          return;
        }
        phase = 2; steps[2].textContent = "復旧確認：確認中";
        const verification = await api(`/executions/${safeId(execution.id)}/verify`, {}, token);
        if (!isCurrent()) return;
        const verified = verification?.status === "PASS";
        steps[2].textContent = `復旧確認：${verified ? "正常性を確認" : verification?.status === "FAIL" ? "復旧未完了" : "確認不能"}`;
        showFeedback(verified ? "復旧処理が成功し、サービスが登録された正常性の条件を満たすことを確認しました。" : verification?.status === "FAIL" ? "操作は成功しましたが、サービスは正常性の条件を満たしていません。実行記録から原因を確認してください。" : "操作は成功しましたが、復旧を確認できません。実行記録から復旧確認をやり直してください。", verified ? "info" : "warning");
      } catch (error) {
        if (!isCurrent()) return;
        steps[phase].textContent = `${["承認", "復旧処理", "復旧確認"][phase]}：結果を確認できません`;
        if (phase < 2) steps[phase + 1].textContent = `${["承認", "復旧処理", "復旧確認"][phase + 1]}：${execute ? "未実施" : "状態不明"}`;
        const guidance = !execute ? "受付または進捗を確認できません。サーバーで処理中の可能性があります。再送せず案件と実行記録を確認してください。" : ["", "復旧処理の結果を確認できません。再実行せず、実行記録と監査ログを確認してください。", "復旧処理は成功しましたが、復旧確認の結果を取得できません。実行記録から復旧確認をやり直してください。"][phase];
        showFeedback(`${guidance}\n${error.message}`, "error");
      } finally {
        if (isCurrent()) { records.hidden = false; await refresh(); }
      }
    }), "button primary", false);
    updateApprovalState = () => {
      submit.disabled = attempted || submit.getAttribute("aria-busy") === "true" || !allowed || !checkbox.checked || !operationReady || Number(plan.expires_at) * 1000 <= Date.now();
      submit.title = attempted ? "送信済みです。案件と実行記録を確認してください" : !allowed ? "役割・提案者・有効期限を確認してください" : !operationReady ? "操作能力の内容を取得しています" : !checkbox.checked ? "内容を確認してチェックを入れてください" : "";
    };
    updateApprovalState();
    checkbox.addEventListener("change", updateApprovalState); submit.addEventListener("opsyne:idle", updateApprovalState); append(actions, submit, records); footer.append(actions); $("#action-dialog").append(footer);
  }
}
function rejectForm(plan, item) {
  const body = openDialog("計画を却下", "REJECT PLAN");
  const { fields } = createForm(body, "理由を記録して却下", async (form) => {
    await api(`/plans/${safeId(plan.id)}/reject`, { reason: form.get("reason") }); toast("計画を却下しました。"); await refresh(); await openCase(item.id);
  });
  formField(fields, "却下理由", "reason", { type: "textarea", full: true });
}
function executionCard(execution, item) {
  const card = el("article", "plan-card");
  append(card, append(el("div", "plan-card-header"), el("h4", "", execution.id), badge(execution.status)), info([["計画", execution.plan_id], ["最終更新", date(execution.updated_at, true)]]));
  if (execution.result?.detail) card.append(el("p", "analysis-text", execution.result.detail));
  if (execution.status === "UNKNOWN") card.append(message("操作結果は不明です。独立確認で状態が改善していても、この操作の照合が必要です。無条件の再送は行いません。", "warning"));
  const check = el("div", "verification-card");
  append(check, el("h3", "", "独立した結果確認"), badge(execution.verification?.status), el("p", "", execution.verification?.detail || "まだ確認されていません。操作の成功応答だけでは復旧と判断しません。")); card.append(check);
  const actions = el("div", "card-actions");
  if (["UNKNOWN", "IN_FLIGHT", "PENDING"].includes(execution.status)) actions.append(button("操作結果を照合", (event) => busy(event.currentTarget, async () => {
    await api(`/executions/${safeId(execution.id)}/reconcile`, {}); toast("照合結果を更新しました。"); await refresh(); await openCase(item.id);
  }), "button small", can("operate")));
  actions.append(button("独立確認を実行", (event) => busy(event.currentTarget, async () => {
    await api(`/executions/${safeId(execution.id)}/verify`, {}); toast("独立確認の結果を記録しました。"); await refresh(); await openCase(item.id);
  }), "button small", can("operate")));
  append(card, actions, jsonDetails("実行台帳の全体", execution)); return card;
}

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const submit = $("#auth-form button"); if (submit.disabled) return; submit.disabled = true;
  $("#auth-error").hidden = true;
  try { await connect($("#auth-token").value.trim()); }
  catch (error) { $("#auth-error").textContent = error.message; $("#auth-error").hidden = false; }
  finally { submit.disabled = false; }
});
$("#auth-dialog").addEventListener("cancel", (event) => { if (!state.session) event.preventDefault(); });
$("#session-button").addEventListener("click", () => {
  if (!state.session) { $("#auth-dialog").showModal(); return; }
  const body = openDialog("接続中のセッション", "SESSION");
  append(body, info([["主体", state.session.actor], ["役割", roleNames[state.session.role] || state.session.role]]), message("承認には提案者と異なる主体が必要です。接続を切り替えると現在のトークンはこのタブから削除されます。"), append(el("div", "form-actions"), button("切断して別の主体で接続", () => disconnect(), "button")));
});
$("#refresh-button").addEventListener("click", (event) => busy(event.currentTarget, () => refresh(true)));
$("#dialog-close").addEventListener("click", () => $("#action-dialog").close());
$("#action-dialog").addEventListener("close", () => { state.modalEpoch += 1; });
window.addEventListener("hashchange", navigate);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 10000);
navigate();
if (state.token) connect(state.token).catch(() => disconnect("保存したトークンでは接続できませんでした。再接続してください。"));
else disconnect();
