/* global marked, hljs, echarts */

"use strict";

// ---------------------------------------------------------------- 全局状态
const state = {
  config: null,
  stocks: [],
  reports: {},
  jobs: [],
  categoryFilter: "全部",
  detail: null,
  detailOptions: { interval: "1d", period: "", showNX: true, showMA: true },
  currentChart: null,
  pendingZoom: null,
  pollTimer: null,
  editingSymbol: null,
  activeSymbols: new Set(),
  latestReports: {},
  theme: document.documentElement.dataset.theme || "dark",
  settings: { max_concurrency: 5, analysis: {} },
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const fmtPrice = (v) => (v == null ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const fmtNum = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const fmtVol = (v) => {
  if (v == null) return "—";
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(v);
};

function updateActiveSymbols(jobs) {
  const set = new Set();
  for (const j of jobs || []) {
    if (j.status === "running" || j.status === "queued") set.add(j.symbol.toUpperCase());
  }
  state.activeSymbols = set;
}

function renderWatchlistPills() {
  if (parseHash().view !== "watchlist") return;
  $$("#watchlist .stock-card").forEach((card) => {
    const sym = card.dataset.symbol;
    const active = state.activeSymbols.has(sym);
    let pill = card.querySelector(".analyzing-pill");
    if (active && !pill) {
      const wrap = card.querySelector(".card-metrics");
      if (wrap) {
        const el = document.createElement("span");
        el.className = "analyzing-pill";
        el.innerHTML = '<span class="mini-spinner"></span>分析中';
        wrap.appendChild(el);
      }
    } else if (!active && pill) {
      pill.remove();
    }
  });
}

// 日/夜间主题：切换后重绘图表配色，偏好存入 localStorage
function applyTheme(theme) {
  state.theme = theme;
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("sd-theme", theme);
  } catch (_) {}
  const btn = $("#btn-theme");
  if (btn) btn.textContent = theme === "dark" ? "🌛" : "☀️";
  if (state.detail && state.currentChart) renderChart(state.detail);
}
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
const arrow = (v) => (v > 0 ? "▲" : v < 0 ? "▼" : "―");
const RES_NODE_LABEL = { "30m": "30", "1h": "1", "2h": "2", "3h": "3", "4h": "4" };

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

// A股/港股（.SS/.SZ/.SH/.HK）主标题用公司名，代码做副标题；其余保持代码
function displayFor(symbol, name) {
  const isCnHk = /\.(SS|SZ|SH|HK)$/i.test(symbol);
  const nm = name || symbol;
  return {
    main: isCnHk ? nm : symbol,
    sub: isCnHk ? symbol : (nm !== symbol ? nm : ""),
    isCnHk,
  };
}

function toast(msg, type = "info", ms = 3200) {
  const wrap = $("#toast-wrap");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return resp.json();
}

// ---------------------------------------------------------------- 渲染工具
function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderMarkdown(md) {
  if (!md) return "<div class='empty'>暂无内容</div>";
  return marked.parse(md, { gfm: true, breaks: true });
}

function highlightBlocks(root) {
  if (!root || !window.hljs) return;
  root.querySelectorAll("pre code").forEach((el) => {
    try {
      hljs.highlightElement(el);
    } catch (_) {}
  });
}

function smoothScrollTo(target, duration = 450) {
  if (!target) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    target.scrollIntoView({ block: "start" });
    return;
  }
  const startY = window.scrollY;
  const endY = Math.max(0, target.getBoundingClientRect().top + startY - 16);
  const dist = endY - startY;
  if (Math.abs(dist) < 2) return;
  const start = performance.now();
  const easeInOutCubic = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    window.scrollTo(0, startY + dist * easeInOutCubic(t));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function sparkline(values, width = 120, height = 44) {
  if (!values || values.length < 2) return "<div class='empty' style='padding:0;font-size:11px'>—</div>";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pad = 3;
  const step = (width - pad * 2) / (values.length - 1);
  const pts = values.map((v, i) => [pad + i * step, pad + (1 - (v - min) / range) * (height - pad * 2)]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const up = values[values.length - 1] >= values[0];
  const color = up ? "#ef4444" : "#22c55e";
  const area = `${pts[0][0].toFixed(1)},${height} ${line} ${pts[pts.length - 1][0].toFixed(1)},${height}`;
  const gid = "g" + Math.random().toString(36).slice(2, 8);
  return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    <polygon points="${area}" fill="url(#${gid})"/>
    <polyline points="${line}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

// ---------------------------------------------------------------- 路由
function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const parts = h.split("/").filter(Boolean);
  if (parts[0] === "detail") return { view: "detail", symbol: decodeURIComponent(parts[1] || "") };
  if (parts[0] === "report") return { view: "report", ticker: decodeURIComponent(parts[1] || ""), id: decodeURIComponent(parts[2] || "") };
  if (parts[0] === "jobs") return { view: "jobs" };
  if (parts[0] === "reports") return { view: "reports" };
  if (parts[0] === "decisions") return { view: "decisions" };
  if (parts[0] === "review") return { view: "review" };
  return { view: "watchlist" };
}

function navigate(hash) {
  location.hash = hash;
}

function switchView(name) {
  $$(".view").forEach((v) => v.classList.add("hidden"));
  const el = $(`#view-${name}`);
  if (el) el.classList.remove("hidden");
  $$(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === name));
  window.scrollTo(0, 0);
  if (state.currentChart) {
    state.currentChart.dispose();
    state.currentChart = null;
  }
}

async function router() {
  const route = parseHash();
  switch (route.view) {
    case "detail":
      switchView("detail");
      await renderDetail(route.symbol);
      break;
    case "report":
      switchView("report");
      await renderReport(route.ticker, route.id);
      break;
    case "reports":
      switchView("reports");
      await renderReports();
      break;
    case "decisions":
      switchView("decisions");
      await renderDecisions();
      break;
    case "review":
      switchView("review");
      await renderReview();
      break;
    case "jobs":
      switchView("jobs");
      await renderJobs();
      break;
    default:
      switchView("watchlist");
      await renderWatchlist();
  }
}

// ---------------------------------------------------------------- 数据加载
async function loadConfig() {
  if (state.config) return state.config;
  state.config = await api("/api/config");
  return state.config;
}

async function loadSettings() {
  try {
    state.settings = await api("/api/settings");
  } catch (_) {}
  return state.settings;
}

let autoScanTimer = null;
function setupAutoScanPolling() {
  const enabled = !!(state.settings.auto_scan || {}).enabled;
  if (enabled && !autoScanTimer) {
    autoScanTimer = setInterval(async () => {
      try {
        state.settings = await api("/api/settings");
      } catch (_) {}
      if (!(state.settings.auto_scan || {}).enabled) {
        clearInterval(autoScanTimer);
        autoScanTimer = null;
        return;
      }
      if (parseHash().view === "watchlist") {
        try {
          await loadStocks(true);
          renderWatchlistBody();
        } catch (_) {}
      }
    }, 90000);
  } else if (!enabled && autoScanTimer) {
    clearInterval(autoScanTimer);
    autoScanTimer = null;
  }
}

// 模型下拉：按供应商填充可选模型；不在列表里的当前值自动落到「自定义模型 ID」
function fillModelSelect(select, customInput, provider, currentValue) {
  if (!select) return;
  const models = ((state.config || {}).models || {})[provider] || [];
  const known = models.filter((m) => m !== "custom" && m !== "__custom__");
  const isKnown = currentValue && known.includes(currentValue);
  select.innerHTML =
    known.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("") +
    `<option value="__custom__">✏️ 自定义模型 ID…</option>`;
  select.onchange = () => {
    const isCustom = select.value === "__custom__";
    if (customInput) customInput.classList.toggle("hidden", !isCustom);
  };
  if (isKnown) {
    select.value = currentValue;
    if (customInput) customInput.classList.add("hidden");
  } else {
    select.value = currentValue ? "__custom__" : (known[0] || "__custom__");
    if (customInput) {
      customInput.value = currentValue || "";
      customInput.classList.toggle("hidden", select.value !== "__custom__");
    }
  }
}

function modelValue(select, customInput) {
  if (!select) return "";
  return select.value === "__custom__"
    ? (customInput ? customInput.value.trim() : "")
    : select.value;
}

function syncModelSelects(scope) {
  const provider =
    scope === "settings"
      ? $("#set-provider").value
      : $("#analyze-form").provider.value;
  const a = (state.settings.analysis || {});
  if (scope === "settings") {
    fillModelSelect($("#set-deep-model"), $("#set-deep-model-custom"), provider, a.deep_model);
    fillModelSelect($("#set-quick-model"), $("#set-quick-model-custom"), provider, a.quick_model);
  } else {
    fillModelSelect($("#an-deep-model"), $("#an-deep-model-custom"), provider, a.deep_model);
    fillModelSelect($("#an-quick-model"), $("#an-quick-model-custom"), provider, a.quick_model);
  }
}

async function loadStocks(refresh = false, noSnapshot = false) {
  const params = new URLSearchParams();
  if (refresh) params.set("refresh", "1");
  if (noSnapshot) params.set("no_snapshot", "1");
  const qs = params.toString();
  const data = await api(`/api/stocks${qs ? "?" + qs : ""}`);
  state.stocks = data.stocks || [];
  return state.stocks;
}

// ---------------------------------------------------------------- 收藏列表
function groupStocks() {
  const groups = new Map();
  for (const s of state.stocks) {
    const cat = s.category || "未分类";
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(s);
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0], "zh"));
}

function renderCategoryFilter() {
  const bar = $("#category-filter");
  const counts = new Map();
  for (const s of state.stocks) {
    const cat = s.category || "未分类";
    counts.set(cat, (counts.get(cat) || 0) + 1);
  }
  const cats = [["全部", state.stocks.length], ...counts.entries()];
  bar.innerHTML = cats
    .map(
      ([c, n]) =>
        `<button class="filter-chip ${state.categoryFilter === c ? "active" : ""}" data-cat="${escapeHtml(c)}">${escapeHtml(c)} <span class="cnt">${n}</span></button>`
    )
    .join("");
  $$("#category-filter .filter-chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      state.categoryFilter = chip.dataset.cat;
      renderCategoryFilter();
      renderWatchlistBody();
    })
  );
}

function renderWatchlistBody() {
  const wrap = $("#watchlist");
  if (!state.stocks.length) {
    wrap.innerHTML = `<div class="empty"><div class="big">⭐</div>还没有收藏股票<br/>点击右上角「添加股票」开始，之后可一键运行 TradingAgents 分析</div>`;
    return;
  }
  const groups = groupStocks().filter(([c]) => state.categoryFilter === "全部" || c === state.categoryFilter);
  if (!groups.length) {
    wrap.innerHTML = `<div class="empty">该分类下暂无股票</div>`;
    return;
  }
  wrap.innerHTML = groups
    .map(([cat, stocks]) => {
      const cards = stocks
        .map((s) => {
          const snap = s.snapshot || {};
          const disp = displayFor(s.symbol, s.name);
          const pct = snap.change_pct;
          const chgCls = cls(pct);
          const pctTxt = pct == null ? "—" : `${pct > 0 ? "+" : ""}${fmtNum(pct)}%`;
          const trendCls = pct > 0 ? "top" : pct < 0 ? "down" : "";
          const signals = Object.values(snap.signals || {});
          const signalPill = signals.length
            ? `<span class="signal-pill ${signals.some((x) => /超买|死叉|顶背离|减仓|清仓|止损|卖出/.test(x.description || "")) ? "bear" : "bull"}">⚡ ${signals.length} 个信号</span>`
            : "";
          const strong1234 = !!(snap.signals && snap.signals["1234"]);
          const rsi = snap.rsi;
          const rsiCls = rsi >= 70 ? "down" : rsi <= 30 ? "up" : "";
          const macd = snap.macd_hist;
          const analyzingPill = state.activeSymbols.has(s.symbol)
            ? `<span class="analyzing-pill"><span class="mini-spinner"></span>分析中</span>`
            : "";
          const lr = state.latestReports[s.symbol];
          const ratingMeta = RATING_META[String((lr || {}).action || "").toLowerCase()] || { cls: "d-unknown" };
          const resNodes = (snap.resonance && snap.resonance.fired_nodes) || [];
          const resChip = resNodes.length
            ? `<span class="res-chip" title="30m/1h/2h/3h/4h MRMC 抄底触发：${resNodes.join(", ")}">⚡ ${resNodes.map((n) => RES_NODE_LABEL[n] || n).join(",")}</span>`
            : "";
          return `<div class="stock-card ${trendCls}" data-symbol="${escapeHtml(s.symbol)}">
            <div class="card-head">
              <div class="card-title" data-act="detail" title="查看详情">
                <div>
                  <div class="card-symbol">${escapeHtml(disp.main)}</div>
                  <div class="card-name" title="${escapeHtml(s.name || s.symbol)}">${escapeHtml(disp.sub)}</div>
                </div>
              </div>
              ${resChip}
              <div class="card-actions">
                <button class="icon-btn" data-act="edit" title="编辑">✎</button>
                <button class="icon-btn danger" data-act="del" title="删除">✕</button>
              </div>
            </div>
            <div class="card-body">
              <div class="price-block">
                <div class="price ${chgCls}">${fmtPrice(snap.close)}</div>
                <div class="change ${chgCls}">${arrow(pct)} ${pctTxt}</div>
              </div>
              <div class="spark" data-act="detail" title="查看详情">${sparkline(snap.spark)}</div>
            </div>
            <div class="card-metrics">
              <span class="metric"><span class="k">RSI</span> <b class="${rsiCls}">${fmtNum(rsi, 1)}</b></span>
              <span class="metric"><span class="k">MACD</span> <b class="${cls(macd)}">${fmtNum(macd)}</b></span>
              <span class="metric"><span class="k">量</span> <b>${fmtVol(snap.volume)}</b></span>
              <span class="tag">${escapeHtml(s.strategy || "macd_rsi")}</span>
              <span class="tag">${escapeHtml(s.interval || "15m")}</span>
              ${signalPill}
              ${strong1234 ? `<span class="strong-pill">🔥 1234 强共振</span>` : ""}
              ${analyzingPill}
            </div>
            <div class="card-foot">
              <span class="metric" style="border:0;padding:0;background:transparent"><span class="k">${escapeHtml(s.category || "未分类")}</span></span>
              <div class="card-foot-right">
                ${lr && lr.rating_cn ? `<span class="d-badge ${ratingMeta.cls}">${escapeHtml(lr.rating_cn)}</span>` : ""}
                ${lr
                  ? `<button class="btn btn-sm btn-primary" data-act="report" data-ticker="${escapeHtml(s.symbol)}" data-id="${escapeHtml(lr.report_id)}">查看报告 · ${escapeHtml((lr.date || "").slice(5) || "—")}</button>`
                  : `<button class="btn btn-sm" disabled style="opacity:.5">暂无报告</button>`}
              </div>
            </div>
          </div>`;
        })
        .join("");
      return `<div class="stock-group">
        <div class="group-head"><span class="glyph">🗂</span>${escapeHtml(cat)}<span class="group-count">${stocks.length}</span></div>
        <div class="stock-grid">${cards}</div>
      </div>`;
    })
    .join("");

  $$("#watchlist .stock-card").forEach((card) => {
    const symbol = card.dataset.symbol;
    card.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      e.stopPropagation();
      const act = btn.dataset.act;
      if (act === "detail") navigate(`/detail/${encodeURIComponent(symbol)}`);
      else if (act === "report") navigate(`/report/${encodeURIComponent(btn.dataset.ticker)}/${encodeURIComponent(btn.dataset.id)}`);
      else if (act === "edit") openStockModal(symbol);
      else if (act === "del") deleteStock(symbol);
    });
  });
}

async function renderWatchlist() {
  await loadConfig();
  await loadStocks();
  try {
    const dec = await api("/api/decisions");
    state.latestReports = {};
    for (const d of dec.decisions || []) {
      if (!state.latestReports[d.ticker]) {
        const finalD = (d.decision || {}).final || {};
        state.latestReports[d.ticker] = {
          date: d.date,
          report_id: d.report_id,
          action: finalD.action,
          rating_cn: finalD.rating_cn,
        };
      }
    }
  } catch (_) {
    state.latestReports = state.latestReports || {};
  }
  renderCategoryFilter();
  renderWatchlistBody();
}

async function refreshWatchlist() {
  $("#btn-refresh-all").disabled = true;
  try {
    await loadStocks(true);
    renderCategoryFilter();
    renderWatchlistBody();
    toast("行情已刷新", "ok");
  } catch (e) {
    toast("刷新失败: " + e.message, "err");
  } finally {
    $("#btn-refresh-all").disabled = false;
  }
}

async function deleteStock(symbol) {
  if (!confirm(`确定删除收藏 ${symbol} 吗？（不影响已生成报告）`)) return;
  try {
    await api(`/api/stocks/${encodeURIComponent(symbol)}`, { method: "DELETE" });
    toast(`已删除 ${symbol}`, "ok");
    await renderWatchlist();
  } catch (e) {
    toast("删除失败: " + e.message, "err");
  }
}

// ---------------------------------------------------------------- 收藏弹窗
function fillFormSelects() {
  const cfg = state.config;
  if (!cfg) return;
  $("#strategy-select").innerHTML = cfg.strategies
    .map((s) => `<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)} - ${escapeHtml(s.description)}</option>`)
    .join("");
  $("#interval-select").innerHTML = cfg.intervals.map((i) => `<option value="${i}">${i}</option>`).join("");
  $("#category-options").innerHTML = cfg.categories.map((c) => `<option value="${escapeHtml(c)}">`).join("");
}

function openStockModal(symbol = null) {
  const form = $("#stock-form");
  form.reset();
  state.editingSymbol = symbol;
  $("#stock-modal-title").textContent = symbol ? `编辑 ${symbol}` : "添加股票";
  fillFormSelects();
  if (symbol) {
    const s = state.stocks.find((x) => x.symbol === symbol);
    if (s) {
      form.symbol.value = s.symbol;
      form.symbol.disabled = true;
      form.elements["name"].value = s.name || "";
      form.category.value = s.category || "";
      form.strategy.value = s.strategy || "macd_rsi";
      form.interval.value = s.interval || "15m";
      form.note.value = s.note || "";
    }
  } else {
    form.symbol.disabled = false;
    if (state.config && state.config.strategies.length) form.strategy.value = "macd_rsi";
  }
  $("#stock-modal").classList.remove("hidden");
}

function closeStockModal() {
  $("#stock-modal").classList.add("hidden");
  state.editingSymbol = null;
}

async function saveStock(e) {
  e.preventDefault();
  const form = e.target;
  const payload = {
    symbol: form.symbol.value.trim().toUpperCase(),
    name: form.elements["name"].value.trim(),
    category: form.category.value.trim() || "未分类",
    strategy: form.strategy.value,
    interval: form.interval.value,
    note: form.note.value.trim(),
  };
  try {
    if (state.editingSymbol) {
      await api(`/api/stocks/${encodeURIComponent(state.editingSymbol)}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      toast("已更新", "ok");
    } else {
      await api("/api/stocks", { method: "POST", body: JSON.stringify(payload) });
      toast(`已收藏 ${payload.symbol}`, "ok");
    }
    closeStockModal();
    await renderWatchlist();
  } catch (err) {
    toast("保存失败: " + err.message, "err");
  }
}

// ---------------------------------------------------------------- 一键分析
function openAnalyzeModal(symbol) {
  fillFormSelects();
  $("#analyze-summary").innerHTML = `即将分析 <b>${escapeHtml(symbol)}</b>：TradingAgents 多智能体（市场 / 新闻 / 情绪 / 基本面 + 研究辩论 + 风控 + 组合决策）`;
  $("#analyze-form").trade_date.value = new Date().toISOString().slice(0, 10);
  // 预填公共设置里的默认参数（弹窗内可临时覆盖）
  const a = state.settings.analysis || {};
  const form = $("#analyze-form");
  if (a.provider) form.provider.value = a.provider;
  syncModelSelects("analyze");
  if (a.research_depth) form.research_depth.value = String(a.research_depth);
  if (a.language) form.language.value = a.language;
  $$("#analyst-checks input").forEach((c) => {
    c.checked = !a.analysts || a.analysts.includes(c.value);
  });
  const s = state.stocks.find((x) => x.symbol === symbol);
  $("#analyze-form").dataset.symbol = symbol;
  $("#analyze-modal").classList.remove("hidden");
}

function closeAnalyzeModal() {
  $("#analyze-modal").classList.add("hidden");
}

async function submitAnalyze(e) {
  e.preventDefault();
  const form = e.target;
  const symbol = form.dataset.symbol;
  const analysts = $$("#analyst-checks input:checked").map((i) => i.value);
  if (!analysts.length) {
    toast("至少选择一个分析师团队", "err");
    return;
  }
  const payload = {
    symbol,
    trade_date: form.trade_date.value,
    provider: form.provider.value,
    deep_model: modelValue(form.deep_model, $("#an-deep-model-custom")),
    quick_model: modelValue(form.quick_model, $("#an-quick-model-custom")),
    research_depth: Number(form.research_depth.value),
    language: form.language.value,
    analysts,
  };
  try {
    await api("/api/analyze", { method: "POST", body: JSON.stringify(payload) });
    closeAnalyzeModal();
    toast(`已启动 ${symbol} 的分析任务，可在「运行记录」查看进度`, "ok", 5000);
    navigate("/jobs");
    await renderJobs();
  } catch (err) {
    toast("启动失败: " + err.message, "err");
  }
}

function startAnalyze(symbol) {
  // 一键：直接按公共设置执行，无需弹窗
  const a = state.settings.analysis || {};
  const payload = {
    symbol,
    trade_date: new Date().toISOString().slice(0, 10),
    provider: a.provider,
    deep_model: a.deep_model,
    quick_model: a.quick_model,
    research_depth: a.research_depth,
    language: a.language,
    analysts: a.analysts,
    temperature: a.temperature,
  };
  api("/api/analyze", { method: "POST", body: JSON.stringify(payload) })
    .then(() => {
      toast(`已启动 ${symbol} 的分析任务`, "ok", 4000);
      navigate("/jobs");
      renderJobs();
    })
    .catch((err) => toast("启动失败: " + err.message, "err"));
}

async function batchAnalyze() {
  if (!state.stocks.length) {
    toast("收藏列表为空", "info");
    return;
  }
  await openBatchModal();
}

async function openBatchModal() {
  try {
    const settings = await loadSettings();
    $("#batch-date").value = new Date().toISOString().slice(0, 10);
    $("#batch-concurrency").value = settings.max_concurrency || 5;
    const list = $("#batch-list");
    list.innerHTML = state.stocks
      .map((s) => {
        const disp = displayFor(s.symbol, s.name);
        return `<label class="batch-item">
          <input type="checkbox" class="batch-check" value="${escapeHtml(s.symbol)}" checked />
          <span class="batch-sym">${escapeHtml(disp.main)}</span>
          <span class="batch-name">${escapeHtml(disp.sub)}</span>
          <span class="tag">${escapeHtml(s.category || "未分类")}</span>
        </label>`;
      })
      .join("");
    $("#batch-modal").classList.remove("hidden");
  } catch (err) {
    toast("打开批量分析失败: " + err.message, "err");
  }
}

// ---------------------------------------------------------------- 分析设置
function openSettingsModal() {
  const s = state.settings || {};
  const a = s.analysis || {};
  const as = s.auto_scan || {};
  $("#set-provider").value = a.provider || "deepseek";
  syncModelSelects("settings");
  $("#set-depth").value = String(a.research_depth || 1);
  $("#set-language").value = a.language || "中文";
  $("#set-temperature").value = a.temperature == null ? "" : a.temperature;
  $("#set-concurrency").value = s.max_concurrency || 5;
  $("#set-auto-scan").checked = !!as.enabled;
  const statusEl = $("#set-auto-scan-status");
  if (statusEl) {
    const next = (as.next_slots || []).slice(0, 3).join(" ｜ ");
    statusEl.textContent =
      `${as.enabled ? "已开启" : "已关闭"}${as.last_scan_at ? ` · 上次扫描 ${fmtTime(as.last_scan_at)}` : ""}${next ? ` · 下次节点 ${next}` : ""}`;
  }
  const analysts = a.analysts || ["market", "social", "news", "fundamentals"];
  $$("#set-analysts input").forEach((c) => {
    c.checked = analysts.includes(c.value);
  });
  $("#settings-modal").classList.remove("hidden");
}

async function saveSettings() {
  const analysts = $$("#set-analysts input:checked").map((i) => i.value);
  const tempRaw = $("#set-temperature").value.trim();
  const payload = {
    max_concurrency: Math.max(1, Math.min(200, Number($("#set-concurrency").value) || 5)),
    auto_scan_enabled: $("#set-auto-scan").checked,
    analysis: {
      provider: $("#set-provider").value,
      deep_model: modelValue($("#set-deep-model"), $("#set-deep-model-custom")),
      quick_model: modelValue($("#set-quick-model"), $("#set-quick-model-custom")),
      research_depth: Number($("#set-depth").value),
      language: $("#set-language").value,
      analysts,
      temperature: tempRaw === "" ? null : Number(tempRaw),
    },
  };
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
    state.settings = await api("/api/settings");
    $("#settings-modal").classList.add("hidden");
    toast("分析设置已保存，后续分析默认按此参数执行", "ok", 4000);
    setupAutoScanPolling();
  } catch (err) {
    toast("保存失败: " + err.message, "err");
  }
}

function closeBatchModal() {
  $("#batch-modal").classList.add("hidden");
}

async function submitBatch() {
  const checks = $$("#batch-list .batch-check:checked");
  if (!checks.length) {
    toast("请至少勾选一只股票", "err");
    return;
  }
  const concurrency = Math.max(1, Math.min(200, Number($("#batch-concurrency").value) || 5));
  const tradeDate = $("#batch-date").value || new Date().toISOString().slice(0, 10);
  const symbols = checks.map((c) => c.value);
  const a = (state.settings.analysis || {});
  const btn = $("#batch-start");
  btn.disabled = true;
  btn.textContent = "创建中…";
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ max_concurrency: concurrency }),
    });
    const resp = await api("/api/analyze/batch", {
      method: "POST",
      body: JSON.stringify({
        symbols,
        trade_date: tradeDate,
        provider: a.provider,
        deep_model: a.deep_model,
        quick_model: a.quick_model,
        research_depth: a.research_depth,
        language: a.language,
        analysts: a.analysts,
        temperature: a.temperature,
      }),
    });
    closeBatchModal();
    toast(`已创建 ${resp.created.length} 个分析任务（并发 ${concurrency}）`, "ok", 5000);
    navigate("/jobs");
    renderJobs();
  } catch (err) {
    toast("批量分析启动失败: " + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "开始分析";
  }
}

// ---------------------------------------------------------------- 1234 扫描
function openScanModal() {
  $("#scan-symbols").value = state.stocks.map((s) => s.symbol).join(", ");
  $("#scan-results").innerHTML = "";
  $("#scan-modal").classList.remove("hidden");
}

async function runScan1234() {
  const scope = $("#scan-scope").value;
  const windowH = Math.max(1, Math.min(168, Number($("#scan-window").value) || 24));
  let symbols = [];
  if (scope === "favorites") {
    symbols = state.stocks.map((s) => s.symbol);
  } else {
    symbols = $("#scan-symbols")
      .value.split(/[,，\s]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
  }
  if (!symbols.length) {
    toast("没有可扫描的股票", "err");
    return;
  }
  const btn = $("#scan-start");
  const box = $("#scan-results");
  btn.disabled = true;
  btn.textContent = "扫描中…";
  box.innerHTML = `<div class="loading"><div class="spinner"></div> 正在扫描 ${symbols.length} 只股票（30m/1h/2h/3h/4h MRMC 共振，最近 ${windowH} 小时）…</div>`;
  try {
    const scanOnce = () =>
      api("/api/scan/1234", {
        method: "POST",
        body: JSON.stringify({ symbols, window_hours: windowH }),
      });
    let data = await scanOnce();
    // 异步指标引擎：后台计算中的股票轮询补齐（上限 90s）
    const deadline = Date.now() + 90000;
    while (data.computing && data.computing.length && Date.now() < deadline) {
      box.innerHTML = `<div class="loading"><div class="spinner"></div> 正在异步计算 ${data.computing.length} 只股票的指标（${data.results.length}/${symbols.length} 已完成）…</div>`;
      await new Promise((r) => setTimeout(r, 1200));
      data = await scanOnce();
    }
    const rows = data.results || [];
    const tierMeta = {
      full: { cn: "4/4 全共振", cls: "d-buy" },
      major: { cn: "3/4 强共振", cls: "d-over" },
      none: { cn: "未共振", cls: "d-unknown" },
    };
    box.innerHTML = rows.length
      ? `<div class="scan-table-wrap"><table class="scan-table">
          <thead><tr><th>股票</th><th>共振</th><th>周期明细</th><th></th></tr></thead>
          <tbody>
            ${rows
              .map((r) => {
                const m = tierMeta[r.tier] || tierMeta.none;
                const tf = r.timeframes || {};
                const detail = ["1h", "2h", "3h", "4h"]
                  .map((k) => `${k}:${(tf[k] || {}).fired ? "✓" : "—"}`)
                  .join(" ");
                const dsp = displayFor(r.symbol, r.name);
                return `<tr data-symbol="${escapeHtml(r.symbol)}" class="${r.active ? "scan-active" : ""}">
                  <td><b>${escapeHtml(dsp.main)}</b>${dsp.sub ? `<span class="rating-cn">${escapeHtml(dsp.sub)}</span>` : ""}</td>
                  <td><span class="d-badge ${m.cls}">${m.cn}</span></td>
                  <td class="scan-detail" title="${escapeHtml(detail)}">${escapeHtml(detail)}</td>
                  <td><button class="btn btn-sm" data-open-detail>详情</button></td>
                </tr>`;
              })
              .join("")}
          </tbody></table></div>
          <p class="hint">30m/1h/2h/3h/4h 五个节点，✓ 表示该周期 ${data.window_hours || 24}h 窗口内出现过 MRMC 抄底；🔥 4/4 最强、⚡ 3/4 强共振（按 1h-4h 计）。${data.computing && data.computing.length ? `（${data.computing.length} 只仍在计算中）` : ""}</p>`
      : `<div class="empty">扫描结果为空</div>`;
    $$("#scan-results [data-open-detail]").forEach((b) =>
      b.addEventListener("click", () => {
        navigate(`/detail/${encodeURIComponent(b.closest("tr").dataset.symbol)}`);
      })
    );
  } catch (err) {
    box.innerHTML = `<div class="empty">扫描失败: ${escapeHtml(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "开始扫描";
  }
}

// ---------------------------------------------------------------- 详情页
async function renderDetail(symbol) {
  const title = $("#detail-title");
  const sub = $("#detail-sub");
  try {
    await loadConfig();
    await loadStocks(false, true);
  } catch (_) {}
  const s = state.stocks.find((x) => x.symbol === symbol);
  title.textContent = s ? `${s.name || ""} ${symbol}` : symbol;
  sub.textContent = s
    ? `分类：${s.category || "未分类"} ｜ 盯盘策略：${s.strategy || "macd_rsi"} ｜ ${s.interval || "15m"}`
    : "未收藏（仍可查看行情与指标）";
  $("#detail-body").innerHTML = `<div class="loading"><div class="spinner"></div> 加载行情与指标…</div>`;

  try {
    const data = await api(`/api/stocks/${encodeURIComponent(symbol)}/data?interval=${state.detailOptions.interval}&period=${encodeURIComponent(state.detailOptions.period)}`);
    if (data.error) throw new Error(data.error);
    state.detail = data;
    renderDetailBody(data);
  } catch (err) {
    $("#detail-body").innerHTML = `<div class="empty"><div class="big">😵</div>${escapeHtml(err.message)}</div>`;
  }
}

function renderDetailBody(data) {
  // 布局只渲染一次，之后切换分时/K线长度只异步填充数据，整页不刷新
  $("#detail-body").innerHTML = `
    <div id="strong-alert"></div>
    <div class="detail-stats" id="detail-stats"></div>
    <div class="chart-panel">
      <div class="chart-toolbar">
        <div class="seg" id="interval-seg">
          ${["1m", "15m", "30m"].map((i) => `<button data-interval="${i}" class="${state.detailOptions.interval === i ? "active" : ""}">${i.replace("m", "")}</button>`).join("")}
          <span class="seg-divider"></span>
          ${["1h", "2h", "3h", "4h"].map((i) => `<button data-interval="${i}" class="${state.detailOptions.interval === i ? "active" : ""}">${i === "1h" ? "1h" : i}</button>`).join("")}
          <span class="seg-divider"></span>
          <button data-interval="1d" class="${state.detailOptions.interval === "1d" ? "active" : ""}">日</button>
          <button data-interval="1wk" class="${state.detailOptions.interval === "1wk" ? "active" : ""}">周</button>
        </div>
        <div class="more-wrap">
          <button class="seg-more" id="interval-more" title="更多周期 / 时间范围">…</button>
          <div class="interval-menu hidden" id="interval-menu"></div>
        </div>
        <div class="seg" id="period-seg">
          ${[["30d", "1月"], ["180d", "6月"], ["1y", "1年"], ["3y", "3年"], ["", "全部"]].map(([p, label]) => `<button data-period="${p}" class="${state.detailOptions.period === p ? "active" : ""}">${label}</button>`).join("")}
        </div>
        <span class="spacer"></span>
        <label class="seg" style="align-items:center;gap:6px;padding:2px 10px;cursor:pointer">
          <input type="checkbox" id="toggle-ma" ${state.detailOptions.showMA ? "checked" : ""} style="accent-color:#4f8cff"> MA
        </label>
        <label class="seg" style="align-items:center;gap:6px;padding:2px 10px;cursor:pointer">
          <input type="checkbox" id="toggle-nx" ${state.detailOptions.showNX ? "checked" : ""} style="accent-color:#7c5cff"> NX通道
        </label>
      </div>
      <div id="main-chart"></div>
      <div class="chart-loading hidden" id="chart-loading"><div class="spinner"></div></div>
    </div>
    <div class="detail-cols">
      <div class="info-panel">
        <h3>⚡ 最新信号 <span class="badge" id="signal-badge"></span></h3>
        <div class="signal-list" id="signal-list"></div>
      </div>
      <div class="info-panel">
        <h3>📊 回测摘要（full 策略） <span class="badge" id="bt-badge"></span></h3>
        <div id="bt-body"></div>
      </div>
    </div>
  `;

  fillDetailData(data);

  // 工具栏事件：切换分时/K线长度 -> 只异步刷新数据区域
  $$("#interval-seg button").forEach((b) =>
    b.addEventListener("click", () => {
      state.detailOptions.interval = b.dataset.interval;
      // 切换周期：保持当前柱状图尺寸；时间范围选择失效，自动取消选中
      capturePendingZoom();
      state.detailOptions.period = null;
      refreshDetailChart();
    })
  );
  $$("#period-seg button").forEach((b) =>
    b.addEventListener("click", () => {
      state.detailOptions.period = b.dataset.period;
      refreshDetailChart();
    })
  );
  $("#toggle-ma").addEventListener("change", (e) => {
    state.detailOptions.showMA = e.target.checked;
    renderChart(state.detail);
  });
  $("#toggle-nx").addEventListener("change", (e) => {
    state.detailOptions.showNX = e.target.checked;
    renderChart(state.detail);
  });

  // “…” 更多周期 / 时间范围菜单
  const intervalMore = $("#interval-more");
  const intervalMenu = $("#interval-menu");
  const renderIntervalMenu = () => {
    if (!intervalMenu) return;
    const moreInt = [["2m", "2m"], ["5m", "5m"], ["90m", "90m"]];
    const periods = [["7d", "7天"], ["60d", "2月"], ["90d", "3月"], ["5y", "5年"], ["", "全部"]];
    intervalMenu.innerHTML =
      `<div class="menu-group"><div class="menu-title">更多周期</div>` +
      moreInt
        .map(
          ([v, label]) =>
            `<button class="menu-item" data-kind="interval" data-value="${v}">${label}</button>`
        )
        .join("") +
      `</div><div class="menu-group"><div class="menu-title">时间范围</div>` +
      periods
        .map(
          ([v, label]) =>
            `<button class="menu-item" data-kind="period" data-value="${v}">${label}</button>`
        )
        .join("") +
      `</div>`;
  };
  if (intervalMore) {
    intervalMore.addEventListener("click", (e) => {
      e.stopPropagation();
      renderIntervalMenu();
      intervalMenu.classList.toggle("hidden");
    });
  }
  if (intervalMenu) {
    intervalMenu.addEventListener("click", (e) => {
      const item = e.target.closest(".menu-item");
      if (!item) return;
      if (item.dataset.kind === "interval") {
        state.detailOptions.interval = item.dataset.value;
        capturePendingZoom();
        state.detailOptions.period = null;
      } else {
        state.detailOptions.period = item.dataset.value;
      }
      intervalMenu.classList.add("hidden");
      refreshDetailChart();
    });
  }
}

function fillDetailData(data) {
  state.detail = data;
  renderStrongAlert(data.indicator_1234);
  if (data.indicator_1234_pending) scheduleResonancePoll(data.symbol);
  const stats = data.stats || {};
  const statCard = (k, v, extra = "") =>
    `<div class="stat-card"><div class="k">${k}</div><div class="v ${extra}">${v}</div></div>`;
  const pct = stats.change_pct;
  const chgCls = cls(pct);
  const pctTxt = pct == null ? "—" : `${pct > 0 ? "+" : ""}${fmtNum(pct)}%`;
  const rsi = stats.rsi;
  const rsiCls = rsi >= 70 ? "down" : rsi <= 30 ? "up" : "";
  const macdCls = cls(stats.macd_hist);
  const bt = data.backtest || {};
  renderSignalList(data.signals || {});

  $("#detail-stats").innerHTML = [
    statCard("最新价", fmtPrice(stats.close)),
    statCard("涨跌幅", pctTxt, chgCls),
    statCard("开盘 / 最高 / 最低", `${fmtPrice(stats.open)} / ${fmtPrice(stats.day_high)} / ${fmtPrice(stats.day_low)}`, "small"),
    statCard("成交量", fmtVol(stats.volume)),
    statCard("52周高 / 低", `${fmtPrice(stats.high_52w)} / ${fmtPrice(stats.low_52w)}`, "small"),
    statCard("52周位置", stats.range_pos_52w == null ? "—" : `${fmtNum(stats.range_pos_52w)}%`),
    statCard("RSI(14)", fmtNum(rsi, 1), rsiCls),
    statCard("MACD 柱", fmtNum(stats.macd_hist), macdCls),
  ].join("");

  $("#signal-badge").textContent = `${data.rows} 根K线 · ${data.interval}`;
  $("#bt-badge").textContent = bt.strategy || "";
  $("#bt-body").innerHTML = bt.error
    ? `<div class="empty" style="padding:20px">${escapeHtml(bt.error)}</div>`
    : `<table class="bt-table"><tbody>
        <tr><td>总K线数</td><td><b>${bt.total_bars ?? "—"}</b></td></tr>
        <tr><td>信号触发次数</td><td><b>${bt.total_signals ?? "—"}</b></td></tr>
        <tr><td>信号触发率</td><td><b>${bt.signal_rate ?? "—"}</b></td></tr>
        <tr><td>启用信号</td><td>${(bt.enabled_signals || []).map((x) => `<span class="tag">${x}</span>`).join(" ")}</td></tr>
        <tr><td>策略说明</td><td>${escapeHtml(bt.description || "")}</td></tr>
      </tbody></table>`;

  renderChart(data);
}

// 1234 强共振横幅（可被异步共振结果重复调用）
function renderStrongAlert(s1234) {
  const alertEl = $("#strong-alert");
  if (!alertEl) return;
  s1234 = s1234 || {};
  if (s1234.active) {
    const tf = s1234.timeframes || {};
    const fmtT = (ms) => (ms ? new Date(ms).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "—");
    const isFull = s1234.tier === "full";
    alertEl.innerHTML = `
      <div class="strong-alert ${isFull ? "" : "major"}">
        <span class="sa-fire">🔥</span>
        <div class="sa-main">
          <div class="sa-title">${isFull ? "1234 全周期共振（最强）" : "1234 强共振买入信号"}</div>
          <div class="sa-sub">${isFull ? "1h / 2h / 3h / 4h 四个周期全部触发 MRMC 抄底" : `1h / 2h / 3h / 4h 中 ${s1234.fired_count || 3}/4 周期在 ${s1234.window_hours || 24} 小时内触发 MRMC 抄底，多周期资金共振`}</div>
        </div>
        <div class="sa-tfs">
          ${["30m", "1h", "2h", "3h", "4h"]
            .map(
              (k) =>
                `<span class="sa-tf"><b>${k}</b> ${fmtT(tf[k] && tf[k].last_ts)}</span>`
            )
            .join("")}
        </div>
      </div>`;
  } else {
    alertEl.innerHTML = "";
  }
}

// 信号列表（可被异步共振结果重复调用）
function renderSignalList(signals) {
  const list = $("#signal-list");
  if (!list) return;
  const signalItems = Object.entries(signals)
    .map(([key, v]) => {
      const labels = {
        full: "标准策略（MACD+RSI+EMA）",
        nx_scheme1: "NX 方案1（右侧突破）",
        nx_scheme2: "NX 方案2（左侧狙击）",
        mrmc_buy: "MRMC 抄底（MACD底背离）",
        mrmc_sell: "MRMC 卖出（MACD顶背离）",
        "1234": "1234 强共振（1/2/3/4小时）",
      };
      const st = v.triggered ? `<span class="status-badge done">触发</span>` : `<span class="status-badge canceled">未触发</span>`;
      return `<div class="signal-item"><div class="head"><span>${labels[key] || key} ${st}</span></div><div class="desc">${escapeHtml(v.description || "")}</div></div>`;
    })
    .join("");
  list.innerHTML = signalItems || `<div class="empty" style="padding:20px">暂无信号数据</div>`;
}

let resonanceTimer = null;
// 共振指标异步就绪后轮询补齐（只更新横幅 + 信号列表，不重绘图表）
function scheduleResonancePoll(symbol) {
  if (resonanceTimer) clearInterval(resonanceTimer);
  let tries = 0;
  resonanceTimer = setInterval(async () => {
    tries++;
    try {
      const r = await api(`/api/stocks/${encodeURIComponent(symbol)}/resonance`);
      if (!r.pending && r.indicator_1234) {
        clearInterval(resonanceTimer);
        resonanceTimer = null;
        renderStrongAlert(r.indicator_1234);
        const signals = Object.assign({}, (state.detail.signals || {}));
        if (r.indicator_1234.active) {
          signals["1234"] = {
            triggered: true,
            description: `股票: ${symbol}\n指标: 1234 强共振\n信号: 多周期 MRMC 抄底共振，建议结合 NX 通道与大盘确认后重点关注`,
          };
        }
        renderSignalList(signals);
      }
    } catch (_) {}
    if (tries >= 15) {
      clearInterval(resonanceTimer);
      resonanceTimer = null;
    }
  }, 2000);
}

async function refreshDetailChart() {
  const sym = symbol();
  const loading = $("#chart-loading");
  if (!sym || !loading) return;
  // 同步工具栏高亮
  $$("#interval-seg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.interval === state.detailOptions.interval)
  );
  syncPeriodButtons();
  $$("#interval-menu .menu-item[data-kind='interval']").forEach((b) =>
    b.classList.toggle("active", b.dataset.value === state.detailOptions.interval)
  );
  $$("#interval-menu .menu-item[data-kind='period']").forEach((b) =>
    b.classList.toggle("active", b.dataset.value === state.detailOptions.period)
  );
  const menu = $("#interval-menu");
  if (menu) menu.classList.add("hidden");
  loading.classList.remove("hidden");
  try {
    const data = await api(
      `/api/stocks/${encodeURIComponent(sym)}/data?interval=${state.detailOptions.interval}&period=${encodeURIComponent(state.detailOptions.period || "")}`
    );
    if (data.error) throw new Error(data.error);
    fillDetailData(data);
  } catch (err) {
    toast("数据刷新失败: " + err.message, "err");
  } finally {
    loading.classList.add("hidden");
  }
}

function syncPeriodButtons() {
  $$("#period-seg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.period === state.detailOptions.period)
  );
}

// 切换周期前记录当前可见 K 线数量，新周期加载后保持同样的柱状图尺寸
function capturePendingZoom() {
  const chart = state.currentChart;
  const rows = (state.detail || {}).rows || 0;
  if (!chart || !rows) return;
  try {
    const opts = chart.getOption();
    const dz = (Array.isArray(opts.dataZoom) ? opts.dataZoom : [opts.dataZoom]).filter(Boolean);
    const cur = dz[dz.length - 1] || { start: 60, end: 100 };
    const start = typeof cur.start === "number" ? cur.start : 60;
    const end = typeof cur.end === "number" ? cur.end : 100;
    state.pendingZoom = {
      visibleBars: Math.max(5, Math.round(((end - start) / 100) * rows)),
    };
  } catch (_) {}
}

function symbol() {
  return parseHash().symbol || (state.detail && state.detail.symbol) || "";
}

// ---------------------------------------------------------------- ECharts
const CHART_THEMES = {
  dark: {
    axisLine: "#2a3446",
    axisLabel: "#66748c",
    splitLine: "#1d2534",
    tooltipBg: "rgba(16,21,32,.95)",
    tooltipBorder: "#2a3446",
    tooltipText: "#e7ecf3",
    sliderBorder: "#232c3d",
    sliderBg: "#0d121d",
    sliderFiller: "rgba(79,140,255,.12)",
    sliderHandle: "#4f8cff",
    sliderText: "#66748c",
  },
  light: {
    axisLine: "#d7dee8",
    axisLabel: "#64748b",
    splitLine: "#e8edf4",
    tooltipBg: "rgba(255,255,255,.97)",
    tooltipBorder: "#d7dee8",
    tooltipText: "#1d2636",
    sliderBorder: "#d5dde8",
    sliderBg: "#f3f5fa",
    sliderFiller: "rgba(37,99,235,.12)",
    sliderHandle: "#2563eb",
    sliderText: "#64748b",
  },
};

function renderChart(data) {
  const el = $("#main-chart");
  if (!el) return;
  if (state.currentChart) {
    state.currentChart.dispose();
    state.currentChart = null;
  }
  const chart = echarts.init(el, null, { renderer: "canvas" });
  state.currentChart = chart;
  window.__stockChart = chart; // 调试/测试出口
  const pal = CHART_THEMES[state.theme] || CHART_THEMES.dark;
  // 用户缩放/滑条调整后，右侧固定时间范围不再准确，自动取消选中
  chart.on("datazoom", () => {
    state.detailOptions.period = null;
    syncPeriodButtons();
  });

  // Ctrl/Cmd + 滚轮 = 以鼠标位置为锚点缩放；普通滚轮留给页面滚动
  if (el.__wheelZoom) el.removeEventListener("wheel", el.__wheelZoom);
  const wheelZoom = (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const opts = chart.getOption();
    const dz = (Array.isArray(opts.dataZoom) ? opts.dataZoom : [opts.dataZoom]).filter(Boolean);
    const cur = dz[dz.length - 1] || { start: 60, end: 100 };
    const start = typeof cur.start === "number" ? cur.start : 60;
    const end = typeof cur.end === "number" ? cur.end : 100;
    const span = Math.max(5, end - start);
    const factor = e.deltaY > 0 ? 1.08 : 1 / 1.08;
    const newSpan = Math.min(100, Math.max(5, span * factor));
    // 锚点 = 鼠标在图表数据区的位置对应的数据百分比，缩放后该点仍停留在鼠标下方
    const rect = el.getBoundingClientRect();
    const usableX = Math.max(1, rect.width - 76);
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left - 58) / usableX));
    const anchor = start + frac * span;
    let newStart = anchor - frac * newSpan;
    newStart = Math.max(0, Math.min(100 - newSpan, newStart));
    chart.dispatchAction({
      type: "dataZoom",
      dataZoomIndex: 0,
      start: newStart,
      end: newStart + newSpan,
    });
  };
  el.addEventListener("wheel", wheelZoom, { passive: false });
  el.__wheelZoom = wheelZoom;

  // 左键拖拽平移时间轴，松手带惯性滑行（滚轮仍留给页面；底部滑条、顶部图例区域不拦截）
  if (el.__panCleanup) el.__panCleanup();
  let dragPan = null;
  let inertiaId = null;
  const getZoom = () => {
    const opts = chart.getOption();
    const dz = (Array.isArray(opts.dataZoom) ? opts.dataZoom : [opts.dataZoom]).filter(Boolean);
    const cur = dz[dz.length - 1] || { start: 60, end: 100 };
    return {
      start: typeof cur.start === "number" ? cur.start : 60,
      end: typeof cur.end === "number" ? cur.end : 100,
    };
  };
  const panStart = (e) => {
    if (e.button !== 0) return;
    if (inertiaId) {
      cancelAnimationFrame(inertiaId);
      inertiaId = null;
    }
    const rect = el.getBoundingClientRect();
    if (e.clientY < rect.top + 30 || e.clientY > rect.bottom - 26) return;
    const z = getZoom();
    dragPan = {
      x: e.clientX,
      start: z.start,
      end: z.end,
      usable: Math.max(1, rect.width - 76),
      lastX: e.clientX,
      lastT: performance.now(),
      vx: 0,
    };
    el.style.cursor = "grabbing";
    e.preventDefault();
  };
  const panMove = (e) => {
    if (!dragPan) return;
    const span = dragPan.end - dragPan.start;
    const dxPct = ((e.clientX - dragPan.x) / dragPan.usable) * span;
    let ns = dragPan.start - dxPct;
    ns = Math.max(0, Math.min(100 - span, ns));
    chart.dispatchAction({ type: "dataZoom", dataZoomIndex: 0, start: ns, end: ns + span });
    // 估算滑行速度（px/ms），松手后用于惯性
    const now = performance.now();
    const dt = now - dragPan.lastT;
    if (dt > 0) {
      dragPan.vx = 0.6 * dragPan.vx + 0.4 * ((e.clientX - dragPan.lastX) / dt);
    }
    dragPan.lastX = e.clientX;
    dragPan.lastT = now;
  };
  const panEnd = () => {
    if (!dragPan) return;
    const { vx, usable, start, end } = dragPan;
    dragPan = null;
    el.style.cursor = "";
    if (Math.abs(vx) > 0.12) {
      const span = end - start;
      let curStart = start;
      let v = vx;
      const step = () => {
        if (chart.isDisposed()) return;
        const dxPct = ((v * 16) / usable) * span; // 每帧约 16ms
        const maxStart = 100 - span;
        const ns = Math.max(0, Math.min(maxStart, curStart - dxPct));
        if (ns !== curStart) {
          chart.dispatchAction({ type: "dataZoom", dataZoomIndex: 0, start: ns, end: ns + span });
        }
        curStart = ns;
        v *= 0.94;
        const hitBound =
          (curStart <= 0 && dxPct > 0) || (curStart >= maxStart && dxPct < 0);
        if (Math.abs(v) > 0.08 && !hitBound) {
          inertiaId = requestAnimationFrame(step);
        } else {
          inertiaId = null;
        }
      };
      inertiaId = requestAnimationFrame(step);
    }
  };
  el.addEventListener("mousedown", panStart);
  window.addEventListener("mousemove", panMove);
  window.addEventListener("mouseup", panEnd);
  el.__panCleanup = () => {
    if (inertiaId) {
      cancelAnimationFrame(inertiaId);
      inertiaId = null;
    }
    el.removeEventListener("mousedown", panStart);
    window.removeEventListener("mousemove", panMove);
    window.removeEventListener("mouseup", panEnd);
  };

  const ohlcv = data.ohlcv || [];
  const ind = data.indicators || {};
  const times = ohlcv.map((r) => new Date(r.t));
  const candlesticks = ohlcv.map((r) => [r.o, r.c, r.l, r.h]);
  const volumes = ohlcv.map((r) => ({
    value: r.v,
    itemStyle: { color: r.c >= r.o ? "rgba(239,68,68,.55)" : "rgba(34,197,94,.55)" },
  }));

  const upColor = "#ef4444";
  const downColor = "#22c55e";

  // NX 蓝黄梯子：忠实还原原公式 STICKLINE(C>A 或 C<B, A, B)——
  // 只有收盘价在通道外侧时，才在通道上下沿之间画一根竖线
  function nxStickSeries(name, upper, lower, closeArr, color, width) {
    return {
      name,
      type: "custom",
      // data 用完整的收盘价序列（无 null、在价格区间内）：
      // 1) 避免 0..N 索引把 y 轴范围撑大导致 K 线变扁
      // 2) 避免 null 值导致 dataIndex 错位、竖线丢失
      data: closeArr.slice(),
      clip: true,
      silent: true,
      tooltip: { show: false },
      xAxisIndex: 0,
      yAxisIndex: 0,
      z: 3,
      renderItem: (params, api) => {
        const i = params.dataIndex;
        const u = upper[i];
        const l = lower[i];
        const c = closeArr[i];
        if (u == null || l == null || c == null) return null;
        if (!(c > u || c < l)) return null;
        const x = api.coord([i, 0])[0];
        return {
          type: "line",
          shape: {
            x1: x,
            y1: api.coord([i, u])[1],
            x2: x,
            y2: api.coord([i, l])[1],
          },
          style: api.style({ stroke: color, lineWidth: width, fill: "none" }),
        };
      },
    };
  }

  const series = [
    {
      name: "K线",
      type: "candlestick",
      data: candlesticks,
      xAxisIndex: 0,
      yAxisIndex: 0,
      itemStyle: { color: upColor, color0: downColor, borderColor: upColor, borderColor0: downColor },
    },
  ];
  if (state.detailOptions.showMA) {
    series.push(
      { name: "EMA9", type: "line", data: ind.ema9, smooth: true, showSymbol: false, lineStyle: { width: 1.4, color: "#f5b83d" }, xAxisIndex: 0, yAxisIndex: 0 },
      { name: "EMA21", type: "line", data: ind.ema21, smooth: true, showSymbol: false, lineStyle: { width: 1.4, color: "#38bdf8" }, xAxisIndex: 0, yAxisIndex: 0 }
    );
  }
  if (state.detailOptions.showNX) {
    const closeArr = ohlcv.map((r) => r.c);
    // 梯子竖线（STICKLINE）：蓝 = 短中线通道，黄 = 长线通道
    series.push(
      nxStickSeries("NX长通道", ind.nx_a1, ind.nx_b1, closeArr, "rgba(234,179,8,.85)", 1.5),
      nxStickSeries("NX短通道", ind.nx_a, ind.nx_b, closeArr, "rgba(59,130,246,.85)", 2)
    );
    // 通道边界线（A/B/A1/B1）
    series.push(
      { name: "长线上沿A1", type: "line", data: ind.nx_a1, showSymbol: false, lineStyle: { width: 1, color: "#eab308", opacity: .9 }, xAxisIndex: 0, yAxisIndex: 0, z: 3 },
      { name: "长线下沿B1", type: "line", data: ind.nx_b1, showSymbol: false, lineStyle: { width: 1, color: "#eab308", opacity: .9 }, xAxisIndex: 0, yAxisIndex: 0, z: 3 },
      { name: "NX上沿A", type: "line", data: ind.nx_a, showSymbol: false, lineStyle: { width: 1.2, color: "#3b82f6", opacity: .95 }, xAxisIndex: 0, yAxisIndex: 0, z: 3 },
      { name: "NX下沿B", type: "line", data: ind.nx_b, showSymbol: false, lineStyle: { width: 1.2, color: "#3b82f6", opacity: .95 }, xAxisIndex: 0, yAxisIndex: 0, z: 3 }
    );
  }
  const bullPts = ohlcv.map((r, i) => (ind.div_bull && ind.div_bull[i] ? [i, r.l] : null)).filter(Boolean);
  const bearPts = ohlcv.map((r, i) => (ind.div_bear && ind.div_bear[i] ? [i, r.h] : null)).filter(Boolean);
  series.push(
    {
      name: "底背离",
      type: "scatter",
      data: bullPts,
      xAxisIndex: 0,
      yAxisIndex: 0,
      symbol: "triangle",
      symbolSize: 11,
      itemStyle: { color: upColor, opacity: .9 },
      z: 5,
    },
    {
      name: "顶背离",
      type: "scatter",
      data: bearPts,
      xAxisIndex: 0,
      yAxisIndex: 0,
      symbol: "triangle",
      symbolRotate: 180,
      symbolSize: 11,
      itemStyle: { color: downColor, opacity: .9 },
      z: 5,
    },
    { name: "成交量", type: "bar", data: volumes, xAxisIndex: 1, yAxisIndex: 1 },
    {
      name: "MACD柱",
      type: "bar",
      data: (ind.macd_hist || []).map((v) => ({
        value: v,
        itemStyle: { color: v >= 0 ? "rgba(239,68,68,.75)" : "rgba(34,197,94,.75)" },
      })),
      xAxisIndex: 2,
      yAxisIndex: 2,
    },
    { name: "MACD", type: "line", data: ind.macd, showSymbol: false, lineStyle: { width: 1.2, color: "#4f8cff" }, xAxisIndex: 2, yAxisIndex: 2 },
    { name: "MACD信号", type: "line", data: ind.macd_signal, showSymbol: false, lineStyle: { width: 1.2, color: "#f5b83d" }, xAxisIndex: 2, yAxisIndex: 2 },
    {
      name: "MRMC抄底",
      type: "scatter",
      data: ohlcv
        .map((r, i) => (ind.mrmc_buy && ind.mrmc_buy[i] ? [i, (ind.macd && ind.macd[i] != null ? ind.macd[i] : 0) / 0.81] : null))
        .filter(Boolean),
      xAxisIndex: 2,
      yAxisIndex: 2,
      symbol: "roundRect",
      symbolSize: [42, 20],
      itemStyle: { color: "rgba(239,68,68,.88)", borderColor: "#ff9b9b", borderWidth: 1 },
      label: { show: true, formatter: "抄底", color: "#fff", fontSize: 10, fontWeight: "bold" },
      z: 6,
    },
    {
      name: "MRMC卖出",
      type: "scatter",
      data: ohlcv
        .map((r, i) => (ind.mrmc_sell && ind.mrmc_sell[i] ? [i, (ind.macd && ind.macd[i] != null ? ind.macd[i] : 0) * 1.21] : null))
        .filter(Boolean),
      xAxisIndex: 2,
      yAxisIndex: 2,
      symbol: "roundRect",
      symbolSize: [42, 20],
      itemStyle: { color: "rgba(34,197,94,.88)", borderColor: "#8af0b5", borderWidth: 1 },
      label: { show: true, formatter: "卖出", color: "#fff", fontSize: 10, fontWeight: "bold" },
      z: 6,
    },
    {
      name: "RSI",
      type: "line",
      data: ind.rsi,
      showSymbol: false,
      lineStyle: { width: 1.5, color: "#c084fc" },
      markLine: {
        silent: true,
        symbol: "none",
        lineStyle: { color: "rgba(139,151,171,.5)", type: "dashed" },
        label: { show: false },
        data: [{ yAxis: 70 }, { yAxis: 30 }],
      },
      xAxisIndex: 3,
      yAxisIndex: 3,
    }
  );

  const axisCommon = (i) => ({
    type: "category",
    gridIndex: i,
    data: times,
    boundaryGap: true,
    axisLine: { lineStyle: { color: pal.axisLine } },
    axisLabel: {
      color: pal.axisLabel,
      fontSize: 10,
      formatter: (v) => new Date(v).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" }),
    },
    splitLine: { show: false },
  });

  // 切换周期后保持可见 K 线数量（柱状图尺寸不变），否则用默认 60%
  let zoomStart = data.rows > 120 ? 60 : 0;
  if (state.pendingZoom && data.rows > 0) {
    const visible = Math.min(state.pendingZoom.visibleBars, data.rows);
    zoomStart = Math.max(0, 100 - (visible / data.rows) * 100);
    state.pendingZoom = null;
  }

  chart.setOption({
    animation: false,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      backgroundColor: pal.tooltipBg,
      borderColor: pal.tooltipBorder,
      textStyle: { color: pal.tooltipText, fontSize: 11.5 },
      formatter: (params) => {
        const idx = params[0].dataIndex;
        const r = ohlcv[idx];
        if (!r) return "";
        const lines = [`<b>${new Date(r.t).toLocaleString("zh-CN", { hour12: false })}</b>`];
        lines.push(`开 ${fmtPrice(r.o)}　高 ${fmtPrice(r.h)}　低 ${fmtPrice(r.l)}　收 ${fmtPrice(r.c)}`);
        lines.push(`量 ${fmtVol(r.v)}　涨跌 ${fmtPrice(r.c - r.o)} (${(((r.c - r.o) / (r.o || 1)) * 100).toFixed(2)}%)`);
        if (ind.rsi && ind.rsi[idx] != null) lines.push(`RSI ${fmtNum(ind.rsi[idx], 1)}`);
        if (ind.macd_hist && ind.macd_hist[idx] != null) lines.push(`MACD ${fmtNum(ind.macd[idx])} / 信号 ${fmtNum(ind.macd_signal[idx])} / 柱 ${fmtNum(ind.macd_hist[idx])}`);
        if (ind.div_bull && ind.div_bull[idx]) lines.push("<span style='color:#ef4444'>▲ 底背离</span>");
        if (ind.div_bear && ind.div_bear[idx]) lines.push("<span style='color:#22c55e'>▼ 顶背离</span>");
        return lines.join("<br/>");
      },
    },
    legend: {
      top: 0,
      textStyle: { color: "#8b97ab", fontSize: 10.5 },
      itemWidth: 14,
      itemHeight: 8,
      data: ["K线", "EMA9", "EMA21", "NX短通道", "NX长通道", "NX上沿A", "NX下沿B", "长线上沿A1", "长线下沿B1", "底背离", "顶背离", "MRMC抄底", "MRMC卖出"],
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 58, right: 18, top: 28, height: "44%" },
      { left: 58, right: 18, top: "53%", height: "11%" },
      { left: 58, right: 18, top: "67%", height: "16%" },
      { left: 58, right: 18, top: "86%", height: "12%" },
    ],
    xAxis: [axisCommon(0), axisCommon(1), axisCommon(2), axisCommon(3)],
    yAxis: [
      { gridIndex: 0, scale: true, axisLabel: { color: pal.axisLabel, fontSize: 10 }, splitLine: { lineStyle: { color: pal.splitLine } } },
      { gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
      { gridIndex: 2, axisLabel: { color: pal.axisLabel, fontSize: 10 }, splitLine: { lineStyle: { color: pal.splitLine } } },
      { gridIndex: 3, min: 0, max: 100, axisLabel: { color: pal.axisLabel, fontSize: 10 }, splitLine: { lineStyle: { color: pal.splitLine } } },
    ],
    dataZoom: [
      {
        type: "slider",
        xAxisIndex: [0, 1, 2, 3],
        bottom: 2,
        height: 16,
        start: zoomStart,
        end: 100,
        borderColor: pal.sliderBorder,
        backgroundColor: pal.sliderBg,
        fillerColor: pal.sliderFiller,
        handleStyle: { color: pal.sliderHandle },
        textStyle: { color: pal.sliderText, fontSize: 9 },
      },
    ],
    series,
  });
}

// ---------------------------------------------------------------- 报告
const RATING_META = {
  buy: { cn: "买入", cls: "d-buy" },
  overweight: { cn: "增持", cls: "d-over" },
  hold: { cn: "持有", cls: "d-hold" },
  underweight: { cn: "减持", cls: "d-under" },
  sell: { cn: "卖出", cls: "d-sell" },
};

function decisionBadge(action) {
  const key = String(action || "").toLowerCase();
  const meta = RATING_META[key] || { cn: "未知", cls: "d-unknown" };
  return `<span class="d-badge ${meta.cls}">${meta.cn}</span>`;
}

function finalSide(action) {
  const key = String(action || "").toLowerCase();
  if (key === "buy" || key === "overweight") return "buy";
  if (key === "sell" || key === "underweight") return "sell";
  return "hold";
}

async function renderDecisions(useCache = false) {
  const filter = $("#decision-filter");
  const summary = $("#decision-summary");
  const wrap = $("#decisions-table");
  filter.innerHTML = `<div class="loading" style="padding:20px"><div class="spinner"></div> 加载决策…</div>`;
  summary.innerHTML = "";
  wrap.innerHTML = "";
  try {
    if (!useCache || !state.decisions) {
      const data = await api("/api/decisions");
      state.decisions = data.decisions || [];
    }
    const tickers = [...new Set(state.decisions.map((d) => d.ticker))].sort();
    const nameOf = (t) => {
      const hit = state.decisions.find((d) => d.ticker === t);
      return hit ? hit.name : "";
    };
    filter.innerHTML =
      `<button class="filter-chip ${!state.decisionFilter ? "active" : ""}" data-t="">全部 <span class="cnt">${state.decisions.length}</span></button>` +
      tickers
        .map(
          (t) => {
            const dsp = displayFor(t, nameOf(t));
            return `<button class="filter-chip ${state.decisionFilter === t ? "active" : ""}" data-t="${escapeHtml(t)}">${escapeHtml(dsp.main)}${dsp.sub ? ` <span class="head-sub">${escapeHtml(dsp.sub)}</span>` : ""} <span class="cnt">${state.decisions.filter((d) => d.ticker === t).length}</span></button>`;
          }
        )
        .join("");
    $$("#decision-filter .filter-chip").forEach((chip) =>
      chip.addEventListener("click", () => {
        state.decisionFilter = chip.dataset.t;
        renderDecisions(true);
      })
    );

    const rows = state.decisionFilter
      ? state.decisions.filter((d) => d.ticker === state.decisionFilter)
      : state.decisions;

    // 顶部小结：当前筛选下综合决议的方向统计（增持计入买入侧，减持计入卖出侧）
    const counts = { buy: 0, hold: 0, sell: 0 };
    rows.forEach((r) => {
      const a = finalSide(((r.decision || {}).final || {}).action);
      if (a in counts) counts[a] += 1;
    });
    summary.innerHTML = Object.entries(counts)
      .map(([k, n]) => {
        const sideMeta = { buy: { cn: "买入方向", cls: "d-buy" }, hold: { cn: "持有", cls: "d-hold" }, sell: { cn: "卖出方向", cls: "d-sell" } }[k];
        return `<span class="d-stat"><span class="dot ${sideMeta.cls}"></span>${sideMeta.cn} ${n} 次</span>`;
      })
      .join("");

    if (!rows.length) {
      wrap.innerHTML = `<div class="empty">暂无报告数据</div>`;
      return;
    }
    wrap.innerHTML = `<table class="decision-table">
      <thead>
        <tr>
          <th>日期</th><th>股票</th><th>综合决议</th><th>目标价</th><th>时间跨度</th>
          <th>交易提案</th><th>分仓策略</th><th>策略摘要</th><th></th>
        </tr>
      </thead>
      <tbody>
        ${rows
          .map((r) => {
            const d = r.decision || {};
            const finalD = d.final || {};
            const traderD = d.trader || {};
            const rd = displayFor(r.ticker, r.name);
            const proposal =
              [traderD.action_cn, traderD.entry_price ? "@ " + traderD.entry_price : "", traderD.stop_loss ? "止损 " + traderD.stop_loss : ""]
                .filter(Boolean)
                .join(" / ") || "—";
            return `<tr data-ticker="${escapeHtml(r.ticker)}" data-id="${escapeHtml(r.report_id)}">
              <td class="nowrap">${escapeHtml(r.date || "—")}</td>
              <td><b>${escapeHtml(rd.main)}</b>${rd.sub ? `<span class="rating-cn">${escapeHtml(rd.sub)}</span>` : ""}</td>
              <td class="nowrap">${decisionBadge(finalD.action)}${finalD.rating_cn && finalD.rating_cn !== RATING_META[String(finalD.action || "").toLowerCase()]?.cn ? `<span class="rating-cn">${escapeHtml(finalD.rating_cn)}</span>` : ""}</td>
              <td class="num">${escapeHtml(finalD.price_target || "—")}</td>
              <td class="time-cell" title="${escapeHtml(finalD.time_horizon || "")}">${escapeHtml(finalD.time_horizon || "—")}</td>
              <td class="pos-cell" title="${escapeHtml(proposal)}">${escapeHtml(proposal)}</td>
              <td class="pos-cell" title="${escapeHtml(traderD.position_sizing || "")}">${escapeHtml(traderD.position_sizing || "—")}</td>
              <td class="pos-cell" title="${escapeHtml(finalD.executive_summary || "")}">${escapeHtml((finalD.executive_summary || "—").slice(0, 90))}${(finalD.executive_summary || "").length > 90 ? "…" : ""}</td>
              <td><button class="btn btn-sm" data-open-report>阅读</button></td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`;
    $$("#decisions-table [data-open-report]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const tr = btn.closest("tr");
        navigate(`/report/${encodeURIComponent(tr.dataset.ticker)}/${encodeURIComponent(tr.dataset.id)}`);
      })
    );
  } catch (err) {
    wrap.innerHTML = `<div class="empty">加载失败: ${escapeHtml(err.message)}</div>`;
    filter.innerHTML = "";
  }
}

// ---------------------------------------------------------------- 决策胜率复盘
const REVIEW_RATING_CN = {
  Buy: "买入", Overweight: "增持", Hold: "持有", Underweight: "减持", Sell: "卖出",
};
const REVIEW_RATING_ACTION = {
  Buy: "buy", Overweight: "overweight", Hold: "hold", Underweight: "underweight", Sell: "sell",
};
const REVIEW_BUCKET_CN = { short: "短", medium: "中", long: "长" };

async function renderReview() {
  const summaryBox = $("#review-summary");
  const tableBox = $("#review-table");
  summaryBox.innerHTML = `<div class="loading"><div class="spinner"></div> 加载胜率复盘…</div>`;
  tableBox.innerHTML = "";
  try {
    const data = await api("/api/review");
    state.reviewData = data;
    const s = data.summary || {};
    const bucketMeta = [
      ["short", "短期 ≤30天"],
      ["medium", "中期 1-6月"],
      ["long", "长期 >6月"],
    ];
    const fmtPct = (v, sign = false) =>
      v == null ? "—" : (sign && v > 0 ? "+" : "") + v + "%";
    const bucketCards = bucketMeta
      .map(([k, label]) => {
        const b = (s.by_bucket || {})[k] || {};
        return `<div class="rb-item">
          <div class="rb-name">${label}</div>
          <div class="rb-rate">${fmtPct(b.rate)}</div>
          <div class="rb-sub">${b.wins || 0} 胜 / ${b.total || 0} 评 · α ${fmtPct(b.avg_alpha, true)}</div>
          ${b.hold_total ? `<div class="rb-sub">持有 ${fmtPct(b.hold_rate)}（${b.hold_wins}/${b.hold_total}）</div>` : ""}
        </div>`;
      })
      .join("");
    const ratingItems = Object.entries(s.by_rating || {})
      .map(
        ([r, v]) => `<div class="rr-item">
          <div class="rr-top"><span class="d-badge ${RATING_META[REVIEW_RATING_ACTION[r] || ""]?.cls || "d-unknown"}">${escapeHtml(REVIEW_RATING_CN[r] || r)}</span><b>${fmtPct(v.rate)}</b></div>
          <div class="rr-sub">${v.wins}/${v.total} 胜 · α ${fmtPct(v.avg_alpha, true)}</div>
        </div>`
      )
      .join("");
    summaryBox.innerHTML = `
      <div class="review-overview">
        <div class="ro-card main">
          <div class="ro-label">方向胜率</div>
          <div class="ro-value">${fmtPct(s.overall_rate)}</div>
          <div class="ro-sub">${s.wins || 0} 胜 / ${s.evaluated || 0} 评 · ${s.pending || 0} 未到期</div>
        </div>
        <div class="ro-card">
          <div class="ro-label">平均超额 α（vs 基准）</div>
          <div class="ro-value ${cls(s.avg_alpha)}">${fmtPct(s.avg_alpha, true)}</div>
          <div class="ro-sub">平均收益 ${fmtPct(s.avg_return, true)}</div>
        </div>
        <div class="ro-card">
          <div class="ro-label">目标价触达率</div>
          <div class="ro-value">${fmtPct(s.target_rate)}</div>
          <div class="ro-sub">${s.target_hit || 0} / ${s.target_total || 0} 周期内触达</div>
        </div>
      </div>
      <div class="review-buckets">${bucketCards}</div>
      ${s.hold && s.hold.total ? `<div class="review-hold">持有（±5% 带内）${fmtPct(s.hold.rate)} · ${s.hold.wins}/${s.hold.total} 评</div>` : ""}
      <div class="review-ratings">${ratingItems || `<span class="review-empty">暂无已评估样本</span>`}</div>
      ${s.sample_warning ? `<div class="review-warn">⚠ 已评估样本不足 10 条，胜率仅供参考</div>` : ""}
      <p class="hint">口径：方向胜率 = 看多到期价高于入场价 / 看空低于入场价（持有单独按 ±5% 带内计）；超额 α = 同期相对基准（美股 SPY / 港股恒指 / A股沪深300）的收益；目标价触达 = 周期内价格触碰目标价；未到期不计入。</p>`;

    renderReviewFilterAndGroups();
  } catch (err) {
    summaryBox.innerHTML = `<div class="empty">加载失败: ${escapeHtml(err.message)}</div>`;
  }
}

// 只刷新筛选器 + 分组表格（异步、不动摘要，数据来自缓存）
function renderReviewFilterAndGroups() {
  const data = state.reviewData || { decisions: [], summary: {} };
  const rows = data.decisions || [];
  const tableBox = $("#review-table");
  if (!tableBox) return;
  // 股票筛选器（默认全部）
  const tickers = [...new Set(rows.map((r) => r.ticker))].sort((a, b) => a.localeCompare(b));
  const nameOf = (t) => {
    const hit = rows.find((r) => r.ticker === t);
    return hit ? hit.name : "";
  };
  const filterEl = $("#review-filter");
  if (filterEl) {
    filterEl.innerHTML =
      `<button class="filter-chip ${!state.reviewFilter ? "active" : ""}" data-t="">全部 <span class="cnt">${rows.length}</span></button>` +
      tickers
        .map((t) => {
          const dsp = displayFor(t, nameOf(t));
          return `<button class="filter-chip ${state.reviewFilter === t ? "active" : ""}" data-t="${escapeHtml(t)}">${escapeHtml(dsp.main)}${dsp.sub ? ` <span class="head-sub">${escapeHtml(dsp.sub)}</span>` : ""} <span class="cnt">${rows.filter((r) => r.ticker === t).length}</span></button>`;
        })
        .join("");
    $$("#review-filter .filter-chip").forEach((chip) =>
      chip.addEventListener("click", () => {
        state.reviewFilter = chip.dataset.t;
        renderReviewFilterAndGroups();
      })
    );
  }
  const filtered = state.reviewFilter ? rows.filter((r) => r.ticker === state.reviewFilter) : rows;
  if (!rows.length) {
    tableBox.innerHTML = `<div class="empty">暂无复盘数据，点击右上角「立即复盘」生成</div>`;
    return;
  }
  const groups = {};
  for (const r of filtered) (groups[r.ticker] = groups[r.ticker] || []).push(r);
  if (!Object.keys(groups).length) {
    tableBox.innerHTML = `<div class="empty">该股票暂无复盘数据</div>`;
    return;
  }
  const rowHtml = (r) => {
        const rd = displayFor(r.ticker, r.name);
        const st = r.status === "evaluated" ? (r.win ? "win" : "loss") : r.status === "pending" ? "pending" : "error";
        const stCn = { win: "胜", loss: "亏", pending: "未到期", error: "无法评估" }[st] || r.status;
        const stCls = { win: "status-badge done", loss: "status-badge failed", pending: "status-badge queued", error: "status-badge canceled" }[st];
        const targetHit = r.target_hit == null ? "—" : r.target_hit ? "✓" : "✗";
        const priceTip = `入场 ${r.entry_date ? r.entry_date + " " : ""}${r.entry_price ?? "—"} → 到期 ${r.exit_price ?? "—"}（基准 ${r.benchmark || "—"} ${r.bench_ret_pct != null ? r.bench_ret_pct + "%" : "—"}）`;
        const bucketCls = { short: "tag tag-short", medium: "tag tag-medium", long: "tag tag-long" }[r.bucket] || "tag";
        return `<tr>
          <td class="nowrap">${escapeHtml(r.date || "—")}</td>
          <td><b>${escapeHtml(rd.main)}</b>${rd.sub ? `<span class="head-sub">${escapeHtml(rd.sub)}</span>` : ""}</td>
          <td>${decisionBadge(REVIEW_RATING_ACTION[r.rating] || "")}</td>
          <td class="nowrap" title="${escapeHtml(r.horizon || "")}"><span class="${bucketCls}">${REVIEW_BUCKET_CN[r.bucket] || "—"}${r.horizon_days ? ` · ${r.horizon_days}天` : ""}</span></td>
          <td class="num" title="分析日 ${escapeHtml(r.date || "")}">${fmtPrice(r.entry_price)}<div class="cell-sub">${escapeHtml((r.entry_date || "").slice(5))}</div></td>
          <td class="num ${cls(r.return_pct)}" title="${escapeHtml(priceTip)}">${r.return_pct != null ? (r.return_pct > 0 ? "+" : "") + r.return_pct + "%" : "—"}</td>
          <td class="num ${cls(r.alpha_pct)}" title="${escapeHtml(priceTip)}">${r.alpha_pct != null ? (r.alpha_pct > 0 ? "+" : "") + r.alpha_pct + "%" : "—"}</td>
          <td class="${r.target_hit === true ? "num up" : r.target_hit === false ? "num down" : ""}">${escapeHtml(targetHit)}</td>
          <td><span class="${stCls}">${stCn}</span></td>
          <td><a class="btn btn-sm" href="#/report/${encodeURIComponent(r.ticker)}/${encodeURIComponent(r.report_id)}">报告</a></td>
        </tr>`;
  };
  tableBox.innerHTML = Object.entries(groups)
    .sort((a, b) => (b[1][0].date || "").localeCompare(a[1][0].date || ""))
    .map(([ticker, items]) => {
      const rd = displayFor(ticker, items[0].name);
      const ev = items.filter((x) => x.status === "evaluated");
      const wins = ev.filter((x) => x.win).length;
      const rate = ev.length ? Math.round((wins / ev.length) * 100) : null;
      return `<div class="review-group">
            <div class="review-group-head">
              <span class="rg-title">${escapeHtml(rd.main)}</span>
              ${rd.sub ? `<span class="head-sub">${escapeHtml(rd.sub)}</span>` : ""}
              <span class="rg-stat">${ev.length ? `${wins} 胜 / ${ev.length} 评 · ${rate}%` : `${items.length} 条未到期`}</span>
            </div>
            <div class="decisions-table-wrap"><table class="decision-table">
              <thead><tr><th>日期</th><th>股票</th><th>决议</th><th>有效期</th><th>入场价</th><th>涨跌幅</th><th>超额α</th><th>目标触达</th><th>结果</th><th></th></tr></thead>
              <tbody>${items.map(rowHtml).join("")}</tbody>
          </table></div>
        </div>`;
    })
    .join("");
}

async function renderReports() {
  const wrap = $("#reports-list");
  wrap.innerHTML = `<div class="loading"><div class="spinner"></div> 加载报告…</div>`;
  try {
    const data = await api("/api/reports");
    // 展平并按日期倒序，默认每页 10 条
    const flat = [];
    for (const t of Object.keys(data)) {
      for (const r of data[t]) flat.push(r);
    }
    flat.sort(
      (a, b) =>
        (b.date || "").localeCompare(a.date || "") ||
        (b.created || "").localeCompare(a.created || "")
    );
    if (!flat.length) {
      wrap.innerHTML = `<div class="empty"><div class="big">📄</div>暂无分析报告<br/>在收藏列表或详情页点击「⚡ 一键分析」生成</div>`;
      return;
    }
    const pageSize = 10;
    const pages = Math.max(1, Math.ceil(flat.length / pageSize));
    if (!state.reportPage || state.reportPage > pages) state.reportPage = 1;
    const pageItems = flat.slice((state.reportPage - 1) * pageSize, state.reportPage * pageSize);
    wrap.innerHTML =
      pageItems
        .map((r) => {
          const rd = displayFor(r.ticker, r.name);
          return `<div class="report-card" data-ticker="${escapeHtml(r.ticker)}" data-id="${escapeHtml(r.id)}">
            <div class="report-ico">🗒</div>
            <div class="report-info">
              <div class="t">${escapeHtml(rd.main)}${rd.sub ? ` <span class="head-sub">${escapeHtml(rd.sub)}</span>` : ""} · ${escapeHtml(r.date || "—")}</div>
              <div class="s">生成于 ${escapeHtml(fmtTime(r.created))} ｜ ${r.sections.length} 个章节</div>
            </div>
            <div class="report-badges">
              ${r.decision && r.decision.final && r.decision.final.action_cn ? decisionBadge(r.decision.final.action) : ""}
              <span class="status-badge ${escapeHtml(r.status)}">${escapeHtml(r.status === "done" ? "已完成" : r.status)}</span>
              <span class="btn btn-sm">阅读</span>
            </div>
          </div>`;
        })
        .join("") +
      `<div class="pagination">
        <button class="btn btn-sm" data-page="${state.reportPage - 1}" ${state.reportPage <= 1 ? "disabled" : ""}>‹ 上一页</button>
        <span class="page-info">第 ${state.reportPage} / ${pages} 页 · 共 ${flat.length} 条</span>
        <button class="btn btn-sm" data-page="${state.reportPage + 1}" ${state.reportPage >= pages ? "disabled" : ""}>下一页 ›</button>
      </div>`;
    $$(".report-card").forEach((card) =>
      card.addEventListener("click", () => navigate(`/report/${encodeURIComponent(card.dataset.ticker)}/${encodeURIComponent(card.dataset.id)}`))
    );
    $$(".pagination [data-page]").forEach((btn) =>
      btn.addEventListener("click", () => {
        if (btn.disabled) return;
        state.reportPage = Number(btn.dataset.page);
        renderReports();
      })
    );
  } catch (err) {
    wrap.innerHTML = `<div class="empty">加载失败: ${escapeHtml(err.message)}</div>`;
  }
}

async function renderReport(ticker, id) {
  const title = $("#report-title");
  const sub = $("#report-sub");
  title.textContent = `${ticker} 分析报告`;
  sub.textContent = "加载中…";
  $("#report-content").innerHTML = `<div class="loading"><div class="spinner"></div> 加载报告…</div>`;
  $("#report-toc").innerHTML = "";
  try {
    const data = await api(`/api/reports/${encodeURIComponent(ticker)}/${encodeURIComponent(id)}`);
    const meta = data.meta || {};
    sub.textContent = `${data.ticker} · 分析日期 ${meta.date || "—"} · 完成于 ${fmtTime(meta.completed_at)}`;

    // 综合决议卡片（投资组合经理最终裁决 + 交易员提案参考）
    const fd = (data.decision || {}).final || {};
    const td = (data.decision || {}).trader || {};
    const decisionCard = fd.rating_cn
      ? `<div class="report-decision-card">
          <div class="rd-main">
            <span class="rd-label">综合决议</span>
            ${decisionBadge(fd.action)}
            <span class="rd-rating">${escapeHtml(fd.rating_cn || "")}</span>
            ${fd.price_target ? `<span class="rd-chip">目标价 <b>${escapeHtml(fd.price_target)}</b></span>` : ""}
            ${fd.time_horizon ? `<span class="rd-chip">时间跨度 ${escapeHtml(fd.time_horizon)}</span>` : ""}
          </div>
          ${
            td.action_cn || td.entry_price || td.position_sizing
              ? `<div class="rd-sub">交易提案（参考）：${escapeHtml(
                  [td.action_cn, td.entry_price ? "@ " + td.entry_price : "", td.stop_loss ? "止损 " + td.stop_loss : ""].filter(Boolean).join(" / ") || "—"
                )}${td.position_sizing ? " ｜ " + escapeHtml(td.position_sizing.slice(0, 120)) : ""}</div>`
              : ""
          }
        </div>`
      : "";

    $("#report-toc").innerHTML =
      `<div class="toc-title">目录</div>` +
      data.sections
        .map((s, i) => `<button class="toc-item" data-i="${i}">${escapeHtml(s.title)}</button>`)
        .join("");

    $("#report-content").innerHTML =
      decisionCard +
      data.sections
        .map((s, i) => `<div id="sec-${i}" style="scroll-margin-top:18px"><h1 style="font-size:20px">${escapeHtml(s.title)}</h1>${renderMarkdown(s.markdown)}</div>`)
        .join("");
    highlightBlocks($("#report-content"));

    // 目录点击：平滑滚动到对应章节（不修改 hash，避免触发路由跳回主页）
    const tocItems = $$("#report-toc .toc-item");
    tocItems.forEach((btn) =>
      btn.addEventListener("click", () => {
        tocItems.forEach((x) => x.classList.remove("active"));
        btn.classList.add("active");
        const target = document.getElementById(`sec-${btn.dataset.i}`);
        if (target) smoothScrollTo(target);
      })
    );
    window._reportRaw = data.raw || data.sections.map((s) => s.markdown).join("\n\n");

    $("#btn-copy-md").onclick = async () => {
      try {
        await navigator.clipboard.writeText(window._reportRaw);
        toast("Markdown 已复制", "ok");
      } catch (_) {
        toast("复制失败，请手动下载", "err");
      }
    };
    $("#btn-download-md").href = `/api/reports/${encodeURIComponent(ticker)}/${encodeURIComponent(id)}/raw`;
  } catch (err) {
    $("#report-content").innerHTML = `<div class="empty">加载失败: ${escapeHtml(err.message)}</div>`;
  }
}

// ---------------------------------------------------------------- 运行记录
async function renderJobs() {
  const wrap = $("#jobs-list");
  wrap.innerHTML = `<div class="loading"><div class="spinner"></div> 加载任务…</div>`;
  try {
    const data = await api("/api/jobs");
    state.jobs = data.jobs || [];
    updateActiveSymbols(state.jobs);
    if (!state.jobs.length) {
      wrap.innerHTML = `<div class="empty"><div class="big">🛠</div>暂无运行记录<br/>点击收藏卡片上的「⚡ 一键分析」开始</div>`;
      return;
    }
    wrap.innerHTML = state.jobs
      .map((j) => {
        const st = j.status;
        const stMap = { queued: "排队中", running: "分析中", done: "已完成", failed: "失败", canceled: "已取消", interrupted: "中断" };
        const jd = displayFor(j.symbol, j.name);
        const progress =
          st === "running" || st === "queued"
            ? `<div class="job-progress-bar"><div class="fill"></div></div>`
            : "";
        const liveBadge =
          st === "running"
            ? `<span class="status-badge running"><span class="mini-spinner"></span>分析中</span>`
            : st === "queued"
              ? `<span class="status-badge queued">排队中</span>`
              : `<span class="status-badge ${st}">${stMap[st] || st}</span>`;
        const actions = [];
        if (st === "running" || st === "queued") {
          actions.push(`<button class="btn btn-sm btn-danger" data-act="cancel" data-id="${j.id}">取消</button>`);
        }
        if (st === "interrupted" || st === "failed") {
          actions.push(`<button class="btn btn-sm" data-act="resume" data-id="${j.id}">续跑</button>`);
        }
        if (st === "done" && j.report_id) {
          actions.push(`<button class="btn btn-sm btn-primary" data-act="open" data-ticker="${encodeURIComponent(j.symbol)}" data-id="${encodeURIComponent(j.report_id)}">查看报告</button>`);
        }
        const errBox = j.error ? `<div class="job-log"><span class="dim">错误：</span>${escapeHtml(j.error)}</div>` : "";
        return `<div class="job-card ${st === "running" ? "running" : st === "queued" ? "queued" : ""}" data-id="${j.id}">
          <div class="job-head">
            <div>
              <div class="job-title">${escapeHtml(jd.main)}${jd.sub ? ` <span class="head-sub">${escapeHtml(jd.sub)}</span>` : ""} ${liveBadge}</div>
              <div class="job-meta">${escapeHtml(j.trade_date)} ｜ ${escapeHtml(j.provider)} ｜ ${escapeHtml(j.deep_model)} ｜ 深度 ${j.research_depth} ｜ 创建于 ${escapeHtml(fmtTime(j.created_at))}${j.finished_at ? " ｜ 结束于 " + escapeHtml(fmtTime(j.finished_at)) : ""}</div>
            </div>
            <div class="job-actions">${actions.join("")}</div>
          </div>
          ${progress}
          <div class="job-log" id="log-${j.id}">${escapeHtml((j.log_tail || "等待输出…").slice(-6000))}</div>
          ${errBox}
        </div>`;
      })
      .join("");

    $$("#jobs-list [data-act='cancel']").forEach((b) =>
      b.addEventListener("click", async () => {
        await api(`/api/jobs/${b.dataset.id}/cancel`, { method: "POST" });
        toast("已发送取消请求", "info");
        renderJobs();
      })
    );
    $$("#jobs-list [data-act='resume']").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          const job = await api(`/api/jobs/${b.dataset.id}/resume`, { method: "POST" });
          toast(`已重新排队续跑 ${job.symbol}`, "ok");
          renderJobs();
        } catch (err) {
          toast("续跑失败: " + err.message, "err");
        }
      })
    );
    $$("#jobs-list [data-act='open']").forEach((b) =>
      b.addEventListener("click", () => navigate(`/report/${b.dataset.ticker}/${b.dataset.id}`))
    );
    startJobPolling();
  } catch (err) {
    wrap.innerHTML = `<div class="empty">加载失败: ${escapeHtml(err.message)}</div>`;
  }
}

function startJobPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  const hasActive = state.jobs.some((j) => j.status === "running" || j.status === "queued");
  if (!hasActive) return;
  state.pollTimer = setInterval(async () => {
    try {
      const data = await api("/api/jobs");
      state.jobs = data.jobs || [];
      updateActiveSymbols(state.jobs);
      renderWatchlistPills();
      const anyActive = state.jobs.some((j) => j.status === "running" || j.status === "queued");
      if (parseHash().view === "jobs") {
        const wrap = $("#jobs-list");
        // 已从「当前任务」消失的卡片（完成/取消/超过3天）直接移除
        const visibleIds = new Set(state.jobs.map((j) => j.id));
        wrap.querySelectorAll(".job-card").forEach((card) => {
          if (!visibleIds.has(card.dataset.id)) card.remove();
        });
        state.jobs.forEach((j) => {
          const card = wrap.querySelector(`.job-card[data-id="${j.id}"]`);
          if (!card) return;
          const badge = card.querySelector(".status-badge");
          const stMap = { queued: "排队中", running: "分析中", done: "已完成", failed: "失败", canceled: "已取消", interrupted: "中断" };
          badge.innerHTML =
            j.status === "running"
              ? '<span class="mini-spinner"></span>分析中'
              : stMap[j.status] || j.status;
          badge.className = `status-badge ${j.status}`;
          card.className =
            `job-card ${j.status === "running" ? "running" : j.status === "queued" ? "queued" : ""}`;
          const log = card.querySelector(".job-log");
          if (log && j.log_tail) log.textContent = j.log_tail.slice(-6000);
          if (j.status === "done" && !card.querySelector("[data-act='open']")) {
            const actions = card.querySelector(".job-actions");
            if (actions && j.report_id) {
              const btn = document.createElement("button");
              btn.className = "btn btn-sm btn-primary";
              btn.dataset.act = "open";
              btn.dataset.ticker = encodeURIComponent(j.symbol);
              btn.dataset.id = encodeURIComponent(j.report_id);
              btn.textContent = "查看报告";
              btn.addEventListener("click", () => navigate(`/report/${btn.dataset.ticker}/${btn.dataset.id}`));
              actions.appendChild(btn);
            }
            if (actions) {
              const cancel = actions.querySelector("[data-act='cancel']");
              if (cancel) cancel.remove();
            }
          }
        });
      }
      if (!anyActive && state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
    } catch (_) {}
  }, 2500);
}

// ---------------------------------------------------------------- 初始化
function bindGlobalEvents() {
  window.addEventListener("hashchange", router);
  document.addEventListener("click", (e) => {
    const menu = $("#interval-menu");
    if (
      menu &&
      !menu.classList.contains("hidden") &&
      !e.target.closest("#interval-menu") &&
      !e.target.closest("#interval-more")
    ) {
      menu.classList.add("hidden");
    }
  });
  $$("[data-back]").forEach((b) => b.addEventListener("click", () => history.back()));
  $$("[data-close-modal]").forEach((b) =>
    b.addEventListener("click", () => {
      $("#stock-modal").classList.add("hidden");
      $("#analyze-modal").classList.add("hidden");
      $("#batch-modal").classList.add("hidden");
      $("#scan-modal").classList.add("hidden");
      $("#settings-modal").classList.add("hidden");
    })
  );
  $("#btn-add-stock").addEventListener("click", () => openStockModal());
  $("#btn-scan-1234").addEventListener("click", openScanModal);
  $("#scan-start").addEventListener("click", runScan1234);
  $("#btn-review-run").addEventListener("click", async () => {
    const btn = $("#btn-review-run");
    btn.disabled = true;
    btn.textContent = "复盘中…";
    try {
      const before = (await api("/api/review")).summary.updated_at;
      await api("/api/review/run", { method: "POST" });
      const deadline = Date.now() + 30000;
      let done = false;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2000));
        const cur = (await api("/api/review")).summary.updated_at;
        if (cur !== before) {
          done = true;
          break;
        }
      }
      toast(done ? "复盘完成，胜率已更新" : "复盘仍在进行，稍后刷新查看", done ? "ok" : "info");
      renderReview();
    } catch (err) {
      toast("复盘失败: " + err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "⟳ 立即复盘";
    }
  });
  $("#scan-scope").addEventListener("change", (e) => {
    $("#scan-symbols-field").style.display = e.target.value === "custom" ? "" : "none";
  });
  $("#btn-analyze-all").addEventListener("click", batchAnalyze);
  $("#batch-start").addEventListener("click", submitBatch);
  $("#batch-check-all").addEventListener("click", () => {
    $$("#batch-list .batch-check").forEach((c) => (c.checked = true));
  });
  $("#batch-check-none").addEventListener("click", () => {
    $$("#batch-list .batch-check").forEach((c) => (c.checked = false));
  });
  $("#btn-refresh-all").addEventListener("click", refreshWatchlist);
  $("#btn-theme").addEventListener("click", () => {
    applyTheme(state.theme === "dark" ? "light" : "dark");
  });
  $("#btn-settings").addEventListener("click", openSettingsModal);
  $("#settings-save").addEventListener("click", saveSettings);
  $("#set-provider").addEventListener("change", () => syncModelSelects("settings"));
  $("#analyze-form").provider.addEventListener("change", () => syncModelSelects("analyze"));
  $("#stock-form").addEventListener("submit", saveStock);
  $("#analyze-form").addEventListener("submit", submitAnalyze);
  $("#btn-detail-analyze").addEventListener("click", () => {
    const sym = symbol();
    if (sym) openAnalyzeModal(sym);
  });
  $("#stock-form").elements["symbol"].addEventListener("blur", async (e) => {
    const v = e.target.value.trim();
    if (!v || state.editingSymbol) return;
    try {
      const info = await api(`/api/stocks/lookup?symbol=${encodeURIComponent(v)}`);
      if (info.name && !$("#stock-form").elements["name"].value) {
        $("#stock-form").elements["name"].value = info.name;
      }
      if (info.sector && !$("#stock-form").category.value) $("#stock-form").category.value = info.sector;
    } catch (_) {}
  });
}

async function init() {
  if (window.__sdInited) return; // 防止重复初始化导致监听器双绑
  window.__sdInited = true;
  bindGlobalEvents();
  applyTheme(state.theme);
  try {
    await loadConfig();
    await loadSettings();
    setupAutoScanPolling();
    $("#server-state").textContent = "已连接";
    $("#server-state").classList.add("ok");
  } catch (_) {
    $("#server-state").textContent = "服务异常";
    $("#server-state").classList.add("err");
  }
  await router();
}

document.addEventListener("DOMContentLoaded", init);
