const timeline = document.getElementById("timeline");
const empty = document.getElementById("empty");
const statusBar = document.getElementById("statusBar");
const navBack = document.getElementById("navBack");

const PAGE = 240;
let cursor = null;
let loading = false;
let sentinelEl = null;

const DAY_MS = 86400000;
const dayCache = new Map(); // dayKey -> {section, grid}

const WEEK_CN = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

// 时间轴里的日期标签（排序仍使用 dataset.key 的原始 YYYY-MM-DD）
function fmtDayLabel(key) {
  const [y, m, d] = key.split("-").map(Number);
  const md = `${m}月${d}日`;
  const week = WEEK_CN[new Date(y, m - 1, d).getDay()];
  return (y === new Date().getFullYear() ? md : `${y}年${md}`) + " · " + week;
}

function dayOf(takenAt) {
  const d = new Date(takenAt * 1000);
  const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return key;
}

function daySection(key) {
  let entry = dayCache.get(key);
  if (!entry) {
    const section = document.createElement("section");
    section.className = "day";
    const h = document.createElement("h2");
    const box = document.createElement("span");
    box.className = "day-sel";
    box.title = "选中这一天";
    box.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleDay(key);
    });
    const label = document.createElement("span");
    label.className = "day-key";
    label.dataset.key = key;
    label.textContent = fmtDayLabel(key);
    h.append(box, label);
    const grid = document.createElement("div");
    grid.className = "grid";
    section.append(h, grid);
    // Items arrive newest-first, so appending sections in order keeps the
    // timeline descending (newest at top).
    if (sentinelEl) timeline.insertBefore(section, sentinelEl);
    else timeline.append(section);
    entry = { section, grid };
    dayCache.set(key, entry);
  }
  return entry.grid;
}

function fmtBytes(n) {
  if (!n) return "";
  return n > 1048576 ? (n / 1048576).toFixed(1) + "MB" : Math.round(n / 1024) + "KB";
}

function fmtDur(sec) {
  if (!sec && sec !== 0) return "";
  const s = Math.round(sec);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(ss).padStart(2, "0");
}

function isGif(item) {
  return (item.ext || "").toLowerCase() === ".gif";
}

// ▶ + duration for videos, 动 for gifs — same overlays on tiles and cards
function addMediaBadges(el, item) {
  if (item.kind === "video") {
    const pb = document.createElement("span");
    pb.className = "play-badge";
    pb.textContent = "▶";
    el.append(pb);
    if (item.duration) {
      const db = document.createElement("span");
      db.className = "dur-badge";
      db.textContent = fmtDur(item.duration);
      el.append(db);
    }
  } else if (isGif(item)) {
    const gb = document.createElement("span");
    gb.className = "gif-badge";
    gb.textContent = "动";
    gb.title = "GIF 动图（预览栏中播放原图动画）";
    el.append(gb);
  }
}

function makeTile(item, container) {
  const tile = document.createElement("figure");
  tile.className = "tile" + (item.status === "broken" ? " broken" : "");
  tile.dataset.path = item.path;
  tile.dataset.id = item.id;
  tile.title = `${fmtBytes(item.bytes)}  ${item.width || "?"}x${item.height || "?"}`
    + (item.duration ? `  ${fmtDur(item.duration)}` : "");
  tile.addEventListener("click", () => {
    if (dragGuard) { dragGuard = false; return; }   // 长按连选后的收尾点击
    if (selectMode) toggleSelect(tile, item.id);
    else openDetail(item.id);
  });
  if (item.note) {
    const nb = document.createElement("span");
    nb.className = "note-badge";
    nb.textContent = "注";
    nb.title = "有备注：" + item.note;
    tile.append(nb);
  }
  if (item.caption) {
    const cb = document.createElement("span");
    cb.className = "note-badge cap-badge";
    cb.textContent = "述";
    cb.title = "云端描述：" + item.caption;
    tile.append(cb);
  }
  if (item.status === "broken") {
    tile.textContent = "无法读取";
  } else {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.draggable = false;
    img.src = `/thumb/${item.id}.webp?size=512`;
    img.alt = "";
    tile.append(img);
    addMediaBadges(tile, item);
  }
  const box = document.createElement("span");
  box.className = "sel-box";
  box.title = "多选";
  box.addEventListener("click", (e) => {
    e.stopPropagation();
    selectMode || enterSelectMode();
    toggleSelect(tile, item.id);
  });
  tile.append(box);
  if (selectMode && selectedIds.has(item.id)) tile.classList.add("sel");
  (container || daySection(dayOf(item.taken_at))).append(tile);
}

function ensureSentinel() {
  if (sentinelEl) return;
  sentinelEl = document.createElement("div");
  sentinelEl.className = "sentinel";
  timeline.append(sentinelEl);
  new IntersectionObserver(entries => {
    if (entries.some(e => e.isIntersecting)) loadMore();
  }, { rootMargin: "600px" }).observe(sentinelEl);
}

async function loadMore() {
  if (loading) return;
  loading = true;
  try {
    const params = new URLSearchParams({ limit: String(PAGE) });
    if (cursor) {
      params.set("taken_at", String(cursor.taken_at));
      params.set("id_cursor", cursor.id);
    }
    const res = await fetch(`/api/photos?${params}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (data.items.length) {
      empty.style.display = "none";
      for (const item of data.items) makeTile(item);
      cursor = data.next_cursor;
      for (const e of dayCache.values()) syncDayCheck(e.section);
    }
    if (!data.has_more) {
      if (sentinelEl) { sentinelEl.remove(); sentinelEl = null; }
    } else {
      ensureSentinel();
    }
  } catch (err) {
    console.error(err);
  } finally {
    loading = false;
  }
}

let pollTimer = null;
async function pollStatus() {
  try {
    const res = await fetch("/api/status");
    const { progress, db, ocr, phash, watch, semantic } = await res.json();
    const parts = [];
    if (progress.running) {
      parts.push(
        `自动索引 ${progress.seen}（入库 ${progress.indexed} · 重复 ${progress.duplicated}` +
        (progress.broken ? ` · 损坏 ${progress.broken}` : "") +
        `）· ${progress.rate_per_sec ?? "?"} 张/秒`);
      if (!pollTimer) pollTimer = setInterval(refreshIfIdle, 2000);
    } else {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      parts.push(`库内 ${db.total - (db.missing || 0)} 张` +
        (db.missing ? ` · 丢失 ${db.missing}` : ""));
    }
    if (watch && watch.roots && watch.roots.length) {
      let w = `监听 ${watch.roots.length} 个目录`;
      if (watch.new || watch.reconciled || watch.missing) {
        w += ` · 新 ${watch.new} · 移 ${watch.reconciled} · 失 ${watch.missing}`;
      }
      parts.push(w);
    }
    if (ocr && ocr.running) {
      const pct = ocr.pending_at_start
        ? Math.round(100 * ocr.done / ocr.pending_at_start) : 0;
      parts.push(`OCR ${ocr.done}/${ocr.pending_at_start}（${pct}% · ${ocr.rate_per_sec ?? "?"} 张/秒 · 剩 ${ocr.pending}）`);
      if (!pollTimer) pollTimer = setInterval(refreshIfIdle, 2000);
    } else if (ocr && ocr.done && !progress.running) {
      parts.push(`OCR 完成 ${ocr.done}（空文 ${ocr.empty}）`);
    }
    if (phash && phash.running) {
      parts.push(`重复检测 ${phash.backfilled}/${phash.total}`);
      if (!pollTimer) pollTimer = setInterval(refreshIfIdle, 2000);
    }
    if (semantic && semantic.model_present && (semantic.running || (db && db.embedded < db.total - db.broken))) {
      if (semantic.running) {
        parts.push(`语义向量 ${db.embedded + semantic.done}/${db.total - db.broken}`);
        if (!pollTimer) pollTimer = setInterval(refreshIfIdle, 2000);
      } else {
        parts.push(`语义向量 ${db.embedded}/${db.total - db.broken}`);
      }
    }
    statusBar.textContent = parts.join(" · ");
  } catch (e) {
    statusBar.textContent = "内核未响应";
  }
}

// Reload tiles whenever new content may have arrived and user is near bottom.
async function refreshIfIdle() {
  await pollStatus();
  const scrollable = document.documentElement.scrollHeight - window.scrollY - innerHeight;
  if (scrollable < 1200) await loadMore();
}

window.addEventListener("scroll", () => {
  if (document.getElementById("searchResults").hidden
      && document.getElementById("dupsResults").hidden
      && document.getElementById("levelGrid").hidden) {
    const nearBottom = innerHeight + window.scrollY > document.documentElement.scrollHeight - 1500;
    if (nearBottom) loadMore();
  }
});

// ---- search：单一混合搜索框（文字 + 语义 + 时间一次搜完） ----
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");
const timelineEl = document.getElementById("timeline");
const resultsEl = document.getElementById("searchResults");
const resultsGrid = document.getElementById("resultsGrid");
const resultsCount = document.getElementById("resultsCount");

const SOURCE_TOKEN = {
  ocr: "文字", note: "备注", caption: "描述", semantic: "语义",
  file: "文件名", time: "时间",
};
function sourceLabel(src) {
  return (src || "").split("+").map(t => SOURCE_TOKEN[t] || t).join("+");
}

function showTimeline() {
  resultsEl.hidden = true;
  dupsEl.hidden = true;
  levelGridEl.hidden = true;              // 先收起层级网格，避免和新画面叠在一起
  timelineEl.hidden = false;
  updateNavBack();
  curTab = "photos";
  photoLevel = "mine";
  backTo = null;
  levelGridEl.hidden = true;
  setSeg("mine");
  showPages();
  history.replaceState(null, "", location.pathname);
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function cardFor(item) {
  const card = document.createElement("figure");
  card.className = "result-card";
  card.dataset.id = item.id;
  card.addEventListener("click", () => {
    if (selectMode) toggleSelect(card, item.id);
    else openDetail(item.id);
  });
  if (item.status === "broken") {
    const noimg = document.createElement("div");
    noimg.className = "noimg";
    noimg.textContent = "无法读取";
    card.append(noimg);
  } else {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = `/thumb/${item.id}.webp?size=512`;
    img.alt = "";
    card.append(img);
    addMediaBadges(card, item);
    if (item.score != null) {
      const sc = document.createElement("span");
      sc.className = "score-chip";
      sc.textContent = Math.round(item.score * 100) + "%";
      sc.title = "语义相似度";
      card.append(sc);
    }
  }
  const sbox = document.createElement("span");
  sbox.className = "sel-box";
  sbox.title = "多选";
  sbox.addEventListener("click", (e) => {
    e.stopPropagation();
    selectMode || enterSelectMode();
    toggleSelect(card, item.id);
  });
  card.append(sbox);
  if (selectMode && selectedIds.has(item.id)) card.classList.add("sel");
  const cap = document.createElement("figcaption");
  cap.className = "cap";
  const date = document.createElement("span");
  date.className = "date";
  date.textContent = fmtDate(item.taken_at)
    + (item.source ? " · " + sourceLabel(item.source) : "");
  cap.append(date);
  if (item.ocr_snippet) {
    cap.append(document.createTextNode(item.ocr_snippet));
  }
  if (item.caption) {
    const cl = document.createElement("span");
    cl.className = "cap-line";
    cl.textContent = "描述：" + item.caption;
    cap.append(cl);
  }
  if (item.note) {
    const nl = document.createElement("span");
    nl.className = "note-line";
    nl.textContent = "备注：" + item.note;
    cap.append(nl);
  }
  card.append(cap);
  return card;
}

// Shared "photo grid" browse view (search hits, day tiles, albums, reviews).
let curGridIds = [];    // 当前结果视图的全部 id（全选用）
function showGrid(title, items, opts) {
  opts = opts || {};
  enterBrowse();
  dupsEl.hidden = true;
  resultsEl.hidden = false;
  backTo = opts.onBack || showPages;
  updateNavBack();
  resultsGrid.innerHTML = "";
  resultsGrid.classList.toggle("tiles", !!opts.tiles);
  resultsCount.textContent = title;
  curGridIds = items.map(i => i.id);
  for (const item of items) {
    if (opts.tiles) makeTile(item, resultsGrid);
    else resultsGrid.append(cardFor(item));
  }
  window.scrollTo(0, 0);
}

// ---- 本地语义模型：缺模型时询问一次，同意后后台静默下载 ----
const modelDlg = document.getElementById("modelDialog");
const modelState = document.getElementById("modelState");
const modelBar = document.getElementById("modelBar");
const modelBarFill = document.getElementById("modelBarFill");
const modelOk = document.getElementById("modelOk");
const modelCancel = document.getElementById("modelCancel");
let modelAsk = null;            // 等待用户决定的 resolve
let modelPhase = "ask";         // ask | running | fail | done
let modelPrompted = false;      // 本次页面会话内只问一次
let modelWaiters = [];          // 等在安装完成上的调用方

function fmtMB(n) { return Math.round((n || 0) / 1048576) + " MB"; }

async function modelStatus() {
  try { return await (await fetch("/api/models/status")).json(); }
  catch (e) { return { present: false, running: false, error: "内核未响应" }; }
}

function askModelInstall() {
  modelPhase = "ask";
  modelState.textContent = "准备中…";
  modelBar.hidden = true;
  modelBarFill.style.width = "0%";
  modelOk.disabled = false;
  modelOk.textContent = "开始安装";
  modelCancel.textContent = "以后再说";
  if (!modelDlg.open) modelDlg.showModal();
  return new Promise((resolve) => { modelAsk = resolve; });
}

function settleAsk(v) {
  if (!modelAsk) return false;
  const r = modelAsk;
  modelAsk = null;
  r(v);
  return true;
}

modelCancel.addEventListener("click", () => { settleAsk(false); modelDlg.close(); });
modelDlg.addEventListener("cancel", (e) => { e.preventDefault(); settleAsk(false); modelDlg.close(); });
modelOk.addEventListener("click", () => {
  if (modelPhase === "ask") { modelPhase = "running"; settleAsk(true); }
  else if (modelPhase === "fail") { modelPhase = "running"; startModelInstall(); }
  else if (modelPhase === "done") modelDlg.close();
});

function startModelInstall() {
  modelBar.hidden = false;
  modelOk.disabled = true;
  modelOk.textContent = "安装中…";
  modelCancel.textContent = "后台安装";
  modelState.textContent = "正在准备下载…";
  fetch("/api/models/install", { method: "POST" }).catch(() => {});
  const tick = async () => {
    const st = await modelStatus();
    if (st.present) {
      modelPhase = "done";
      modelBarFill.style.width = "100%";
      modelState.textContent = "安装完成，语义检索和智能场景现在可以使用。";
      modelOk.disabled = false;
      modelOk.textContent = "完成";
      modelCancel.textContent = "关闭";
      if (pollTimer === null) pollTimer = setInterval(refreshIfIdle, 2000);
      const ws = modelWaiters;
      modelWaiters = [];
      ws.forEach((w) => w(true));
      return;
    }
    if (st.error) {
      modelPhase = "fail";
      modelState.textContent = "安装失败：" + st.error;
      modelOk.disabled = false;
      modelOk.textContent = "重试";
      const ws = modelWaiters;
      modelWaiters = [];
      ws.forEach((w) => w(false));
      return;
    }
    if (!st.running) {
      modelPhase = "fail";
      modelState.textContent = "下载已中断，请重试。";
      modelOk.disabled = false;
      modelOk.textContent = "重试";
      return;
    }
    const pct = st.total ? Math.min(99, Math.round(100 * st.downloaded / st.total)) : 0;
    modelBarFill.style.width = pct + "%";
    modelState.textContent = `正在下载 ${st.file || "模型文件"} · ` +
      `${fmtMB(st.downloaded)} / ${fmtMB(st.total)}（${pct}%）`;
    setTimeout(tick, 800);
  };
  tick();
}

// 等安装落地；返回 true 表示模型可用
function waitModelReady() {
  return new Promise((resolve) => {
    modelWaiters.push(resolve);
    // 兜底轮询，避免后台安装（用户点了「后台安装」关掉弹窗）时无人推进
    const probe = setInterval(async () => {
      const st = await modelStatus();
      if (st.present) {
        clearInterval(probe);
        const ws = modelWaiters.filter((w) => w !== resolve);
        modelWaiters = ws;
        if (ws.length === 0) {
          modelPhase = "done";
          toast("语义模型安装完成");
        }
        resolve(true);
      } else if (st.error) {
        clearInterval(probe);
        modelWaiters = modelWaiters.filter((w) => w !== resolve);
        toast("语义模型安装失败：" + st.error);
        resolve(false);
      }
    }, 1500);
  });
}

// 语义检索入口统一先调这里：已装好直接 true；缺失则询问一次
async function ensureSemanticModel() {
  const st = await modelStatus();
  if (st.present) return true;
  if (st.running) {                       // 上次已经在后台下载
    if (!modelDlg.open) {
      modelPhase = "running";
      modelBar.hidden = false;
      modelOk.disabled = true;
      modelOk.textContent = "安装中…";
      modelCancel.textContent = "后台安装";
      modelDlg.showModal();
      startModelInstall();
    }
    return waitModelReady();
  }
  if (modelPrompted) return false;        // 本次会话已拒绝过，不再打扰
  modelPrompted = true;
  const agreed = await askModelInstall();
  if (!agreed) return false;
  startModelInstall();
  return waitModelReady();
}

async function runSearch(q) {
  const res = await fetch("/api/search?q=" + encodeURIComponent(q)
    + "&limit=200&engine=all");
  const data = await res.json();
  showGrid(
    data.items.length
      ? `“${q}” 命中 ${data.items.length} 张`
      : `“${q}” 没有匹配（文字需 OCR 完成，备注/描述需填写，语义需向量回填完成）`,
    data.items, { onBack: showTimeline });
}

searchForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = searchInput.value.trim();
  if (!q) return showTimeline();
  history.replaceState(null, "", `?q=${encodeURIComponent(q)}`);
  await ensureSemanticModel();
  runSearch(q);
});

// ---- multi-select（全选 / 类选 + 索引级操作） ----
const selectBar = document.getElementById("selectBar");
const selCount = document.getElementById("selCount");
let selectMode = false;
const selectedIds = new Set();

function visibleCards() {
  return [...document.querySelectorAll(".tile, .result-card")];
}

function enterSelectMode() {
  selectMode = true;
  document.body.classList.add("selecting");
  selectBar.hidden = false;
  closePane();
}

function exitSelectMode() {
  cancelActiveOp();
  selectMode = false;
  selectedIds.clear();
  opSnapshot = null;
  activeOp = null;
  clearActiveOpButtons();
  cpSelected.clear();
  cpSnapshot = null;
  cpBtnSync();
  document.body.classList.remove("selecting");
  selectBar.hidden = true;
  visibleCards().forEach(c => {
    c.classList.remove("sel");
    const b = c.querySelector(".sel-box");
    if (b) b.textContent = "";
  });
  for (const e of dayCache.values()) syncDayCheck(e.section);
}

// ---- 操作占用态：点一次进入操作、再点一次取消；关闭/执行后回到上一次选择 ----
const OP_BUTTONS = {
  all: "selAll", album: "selAddAlbum",
  move: "selMove", share: "selShare",
};
let activeOp = null;
let opSnapshot = null;     // 进入当前操作前的选择
let opCloser = null;       // 关闭当前操作弹窗的方式

function clearActiveOpButtons() {
  for (const id of Object.values(OP_BUTTONS))
    document.getElementById(id).classList.remove("active");
}
function setActiveOp(op) {
  clearActiveOpButtons();
  document.getElementById(OP_BUTTONS[op]).classList.add("active");
}
function beginOp(op, closer) {
  if (activeOp === op) {          // 再点一次 = 取消这个操作
    cancelActiveOp();
    return false;
  }
  if (activeOp) cancelActiveOp();
  opSnapshot = new Set(selectedIds);
  activeOp = op;
  opCloser = closer || null;
  setActiveOp(op);
  return true;
}
function opClosed(op, restore = true) {
  if (activeOp !== op) return;
  const snap = opSnapshot;
  activeOp = null;
  opCloser = null;
  opSnapshot = null;
  clearActiveOpButtons();
  if (restore && snap) applySelection([...snap]);   // 回到上一次的选择，仍在多选状态
}
function cancelActiveOp() {
  const op = activeOp;
  if (!op) return;
  const closer = opCloser;
  opCloser = null;
  activeOp = null;                 // 先清掉，避免 close 事件重复回退
  const snap = opSnapshot;
  opSnapshot = null;
  clearActiveOpButtons();
  if (closer) closer();
  if (snap) applySelection([...snap]);
}
// 删除成功后：保持多选，选择回退到上一次状态里仍然存在的图片
function keepExistingSelection(before, removed) {
  const gone = new Set(removed);
  const remain = [...before].filter(id => !gone.has(id));
  applySelection(remain);
  if (!remain.length) exitSelectMode();
}

function toggleSelect(card, id) {
  const on = !selectedIds.has(id);
  if (on) selectedIds.add(id); else selectedIds.delete(id);
  card.classList.toggle("sel", on);
  const b = card.querySelector(".sel-box");
  if (b) b.textContent = on ? "✓" : "";
  selCount.textContent = `已选 ${selectedIds.size} 张`;
  const sec = card.closest(".day");
  if (sec) syncDayCheck(sec);
  if (!selectedIds.size) exitSelectMode();   // 取消到最后一张 = 自动退出多选
}

// 时间轴每一天表头里的勾选框：整组选中 / 整组取消
function syncDayCheck(section) {
  const box = section.querySelector(".day-sel");
  if (!box) return;
  const ids = [...section.querySelectorAll(".tile")].map(t => t.dataset.id);
  const on = ids.filter(id => selectedIds.has(id)).length;
  box.textContent = on === 0 ? "" : (on === ids.length ? "✓" : "−");
  box.classList.toggle("on", on > 0);
  box.classList.toggle("all", on > 0 && on === ids.length);
}

function toggleDay(key) {
  const entry = dayCache.get(key);
  if (!entry) return;
  const tiles = [...entry.grid.querySelectorAll(".tile")];
  if (!tiles.length) { pruneEmptyDays(); return; }
  selectMode || enterSelectMode();
  const allOn = tiles.every(t => selectedIds.has(t.dataset.id));
  for (const t of tiles) {
    const on = !allOn;
    if (on) selectedIds.add(t.dataset.id); else selectedIds.delete(t.dataset.id);
    t.classList.toggle("sel", on);
    const b = t.querySelector(".sel-box");
    if (b) b.textContent = on ? "✓" : "";
  }
  selCount.textContent = `已选 ${selectedIds.size} 张`;
  syncDayCheck(entry.section);
  if (!selectedIds.size) exitSelectMode();
}

// 删除/移走后清理掉空的时间节点，保证不出现空日期
function pruneEmptyDays() {
  for (const [key, e] of [...dayCache]) {
    if (!e.grid.querySelector(".tile")) {
      e.section.remove();
      dayCache.delete(key);
    }
  }
  const secs = [...timeline.querySelectorAll(".day")];
  const keyOf = s => (s.querySelector(".day-key") || {}).dataset?.key || "";
  secs.sort((a, b) => keyOf(b).localeCompare(keyOf(a)));
  for (const s of secs) timeline.insertBefore(s, sentinelEl);
}

// 用一整批 id 替换当前选择（类选 / 全选共用，二者天然互斥）
function applySelection(ids) {
  selectedIds.clear();
  ids.forEach(i => selectedIds.add(i));
  for (const c of visibleCards()) {
    const on = selectedIds.has(c.dataset.id);
    c.classList.toggle("sel", on);
    const b = c.querySelector(".sel-box");
    if (b) b.textContent = on ? "✓" : "";
  }
  selCount.textContent = `已选 ${selectedIds.size} 张`;
  for (const e of dayCache.values()) syncDayCheck(e.section);
}

async function fetchIds(params) {
  const qs = new URLSearchParams({ ...params, limit: "50000" });
  const data = await (await fetch("/api/photo_ids?" + qs)).json();
  return data.ids || [];
}

function removeCardsByIds(ids) {
  const set = new Set(ids);
  for (const c of visibleCards()) {
    if (set.has(c.dataset.id)) c.remove();
  }
  pruneEmptyDays();
}

// ---- 长按鼠标左键：连续框选多张（长按即进入连选，移动扫过即入选，无需拖拽） ----
const LONG_PRESS_MS = 120;
let rubberOn = false;      // 长按连选已激活
let dragGuard = false;     // 长按收尾的那次 click 丢弃，避免把选中又取消
let pressTimer = null;
let pressX = 0, pressY = 0;
let rubberMove = null;

function enterRubber(tile) {
  rubberOn = true;
  dragGuard = true;
  selectMode || enterSelectMode();
  if (!selectedIds.has(tile.dataset.id)) toggleSelect(tile, tile.dataset.id);
  if (!rubberMove) {
    rubberMove = (ev) => {
      const el = document.elementFromPoint(ev.clientX, ev.clientY);
      const t = el && el.closest ? el.closest(".tile") : null;
      if (t && t.dataset.id && !selectedIds.has(t.dataset.id)) {
        selectMode || enterSelectMode();
        toggleSelect(t, t.dataset.id);
        ev.preventDefault();
      }
    };
  }
  document.addEventListener("pointermove", rubberMove, { passive: false });
}

function exitRubber() {
  rubberOn = false;
  dragGuard = false;
  if (rubberMove) document.removeEventListener("pointermove", rubberMove);
}

document.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  const target = e.target;
  if (!target || !target.closest) return;
  const tile = target.closest(".tile");
  if (!tile || target.closest(".sel-box")) return;
  pressX = e.clientX; pressY = e.clientY;
  clearTimeout(pressTimer);
  pressTimer = setTimeout(() => enterRubber(tile), LONG_PRESS_MS);
});

document.addEventListener("pointerup", (e) => {
  clearTimeout(pressTimer);
  // 明显位移后松开不会产生 click：把收尾 guard 消费掉，避免下次单击被误当收尾
  if (rubberOn && (Math.abs(e.clientX - pressX) > 8 || Math.abs(e.clientY - pressY) > 8))
    dragGuard = false;
});
document.addEventListener("pointercancel", () => clearTimeout(pressTimer));

// 连选激活后：收尾 click 吞掉并保持连选；之后的下一次单击才结束连选。
// 点图 → 结束并吞掉（不打开详情/不翻转选中）；点按钮/空白 → 只结束连选，按钮照常响应。
document.addEventListener("click", (e) => {
  if (!rubberOn) return;
  if (dragGuard) {
    dragGuard = false;
    if (e.target && e.target.closest && e.target.closest(".tile")) {
      e.stopPropagation(); e.preventDefault();
    }
    return;
  }
  exitRubber();
  if (e.target && e.target.closest && e.target.closest(".tile, .day-sel")) {
    e.stopPropagation(); e.preventDefault();
  }
}, true);

document.addEventListener("dragstart", (e) => { if (rubberOn) e.preventDefault(); });

// ---- 全选：选中当前视图里的全部图片（不是只选一屏） ----
document.getElementById("selAll").addEventListener("click", async () => {
  if (!beginOp("all")) return;
  let ids = [];
  if (!resultsEl.hidden) {
    ids = curGridIds.slice();
  } else if (!levelGridEl.hidden) {
    const key = lvMonth
      ? `${lvYear}-${String(lvMonth).padStart(2, "0")}`
      : String(lvYear);
    ids = await fetchIds({ level_key: key });
  } else {
    ids = await fetchIds({});
  }
  cpSelected.clear();          // 全选替换选择，清掉类选占用态
  cpSnapshot = null;
  cpBtnSync();
  applySelection(ids);
});

// ---- 类选：时间轴 / 种类 / 来源 / 主题（类别可多选，选中显示占用态） ----
const cpDlg = document.getElementById("classPickDialog");
const cpList = document.getElementById("cpList");
const cpTabs = document.getElementById("cpTabs");
const cpState = document.getElementById("cpState");
const cpBtn = document.getElementById("selClass");
const cpSelected = new Map();   // `dim:key` -> {dim, key, name, params}
let cpSnapshot = null;          // 进入类选前的选择；全部取消类别时回退
let cpDim = "time";

// 操作栏「类选…」与类别页签的占用态：有选中的类别就保持高亮
function cpBtnSync() {
  cpBtn.classList.toggle("active", cpSelected.size > 0);
  for (const b of cpTabs.querySelectorAll("button"))
    b.classList.toggle("has",
      [...cpSelected.values()].some(s => s.dim === b.dataset.dim));
}

cpBtn.addEventListener("click", async () => {
  if (!cpSelected.size) cpSnapshot = new Set(selectedIds);
  selectMode || enterSelectMode();
  cpDlg.showModal();
  await renderClassList();
  cpBtnSync();
});
cpDlg.addEventListener("close", () => {
  cpBtnSync();
  if (!cpSelected.size) {
    cpSnapshot = null;
    if (!selectedIds.size) exitSelectMode();
  }
});
cpTabs.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-dim]");
  if (!b) return;
  cpDim = b.dataset.dim;
  for (const x of cpTabs.querySelectorAll("button"))
    x.classList.toggle("on", x === b);
  cpState.textContent = "";
  renderClassList();
});
document.getElementById("cpClose").addEventListener("click", () => cpDlg.close());
// 取消选择：清空已选类别并回到进入类选前的选择，但留在弹窗里
document.getElementById("cpClear").addEventListener("click", async () => {
  cpSelected.clear();
  cpBtnSync();
  applySelection(cpSnapshot ? [...cpSnapshot] : []);
  cpState.textContent = "已取消所选类别";
  await renderClassList();
});

async function classBuckets(dim) {
  if (dim === "time") {
    const d = await (await fetch("/api/grouped?level=year")).json();
    return (d.groups || []).map(g => ({
      key: String(g.key), name: g.key + "年", count: g.count,
      params: { level_key: String(g.key) },
    }));
  }
  if (dim === "kind") {
    let smart = {};
    try {
      const d = await (await fetch("/api/albums")).json();
      for (const a of d.smart) smart[a.key] = a;
    } catch (e) { /* counts optional */ }
    return [
      { key: "photo", name: "照片", count: smart.camera && smart.camera.count, params: { kind: "photo" } },
      { key: "screenshot", name: "截屏录屏", count: smart.screenshot && smart.screenshot.count, params: { kind: "screenshot" } },
      { key: "video", name: "视频", count: smart.video && smart.video.count, params: { kind: "video" } },
      { key: "gif", name: "GIF 动图", count: smart.gif && smart.gif.count, params: { ext: ".gif" } },
    ];
  }
  if (dim === "source") {
    const d = await (await fetch("/api/sources")).json();
    return (d.sources || []).map(s => ({
      key: s.key, name: s.name, count: s.count, params: { source: s.key },
    }));
  }
  const d = await (await fetch("/api/scenes")).json();
  return (d.scenes || []).map(s => ({
    key: s.scene, name: s.scene, count: s.count,
    params: { scene_in: (s.keys || [s.scene]).join(",") },
  }));
}

// 所有选中类别的并集 -> 当前选择；全部取消则退回进入类选前的选择
async function applyClassSelection() {
  if (!cpSelected.size) {
    cpState.textContent = "未选择类别";
    applySelection(cpSnapshot ? [...cpSnapshot] : []);
    return;
  }
  cpState.textContent = "正在选取…";
  const lists = await Promise.all(
    [...cpSelected.values()].map(s => fetchIds(s.params)));
  const ids = [...new Set(lists.flat())];
  applySelection(ids);
  cpState.textContent = `已选 ${cpSelected.size} 个类别 · 共 ${ids.length} 张`;
}

async function renderClassList() {
  cpList.innerHTML = `<p class="ap-empty">加载中…</p>`;
  if (!cpDim) {
    cpList.innerHTML = `<p class="ap-empty">先选择一种类别。</p>`;
    return;
  }
  let buckets = [];
  try { buckets = await classBuckets(cpDim); } catch (e) { /* offline */ }
  cpList.innerHTML = "";
  if (!buckets.length) {
    cpList.innerHTML = `<p class="ap-empty">${
      cpDim === "scene" ? "还没有主题分类结果；先完成语义向量回填，再到图集页「主题」扫描。"
                        : "这个类别下还没有可选的现成分组。"}</p>`;
    return;
  }
  for (const b of buckets) {
    const row = document.createElement("button");
    row.type = "button";
    const key = `${cpDim}:${b.key}`;
    row.className = "ap-row" + (cpSelected.has(key) ? " on" : "");
    row.append(document.createTextNode(b.name));
    if (b.count != null) {
      const cnt = document.createElement("span");
      cnt.className = "ap-count";
      cnt.textContent = `${b.count} 张`;
      row.append(cnt);
    }
    row.addEventListener("click", async () => {
      if (cpSelected.has(key)) cpSelected.delete(key);
      else cpSelected.set(key, { dim: cpDim, key: b.key, name: b.name, params: b.params });
      row.classList.toggle("on", cpSelected.has(key));
      cpBtnSync();
      await applyClassSelection();
    });
    cpList.append(row);
  }
  if (!cpState.textContent) cpState.textContent = "类别可多选，再次点击已选类别可取消";
}

document.getElementById("selExit").addEventListener("click", exitSelectMode);
document.getElementById("selAddAlbum").addEventListener("click", () => {
  if (!selectedIds.size) return;
  if (!beginOp("album", () => apDlg.close())) return;
  openAlbumPicker({ ids: [...selectedIds], title: `加入图集 · ${selectedIds.size} 张` });
});

// ---- 删除 = 原文件直接移进系统回收站，索引同时移除 ----
document.getElementById("selTrash").addEventListener("click", async () => {
  if (!selectedIds.size) return;
  const before = new Set(selectedIds);
  const ids = [...selectedIds];
  if (!confirm(`删除这 ${ids.length} 张？原文件会被移进系统回收站（可从回收站恢复）。`))
    return;
  const res = await fetch("/api/photos/purge", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!res.ok) { toast("删除失败"); return; }
  const data = await res.json();
  removeCardsByIds(ids);
  keepExistingSelection(before, ids);
  toast(`已删除 ${data.deleted || 0} 张（原文件已进回收站）`
    + (data.failed ? ` · 失败 ${data.failed}` : ""));
  pollStatus();
});

// ---- 移动：仿资源管理器的文件夹窗口；单击选中目标，双击进入文件夹 ----
const fpDlg = document.getElementById("folderPickDialog");
const fpList = document.getElementById("fpList");
const fpCur = document.getElementById("fpCur");
const fpState = document.getElementById("fpState");
const fpQuick = document.getElementById("fpQuick");
const fpSelInfo = document.getElementById("fpSelInfo");
let fpPath = null;      // 当前所在文件夹
let fpParent = null;
let fpSel = null;       // 单击选中的目标文件夹

document.getElementById("selMove").addEventListener("click", async () => {
  if (!selectedIds.size) return;
  if (!beginOp("move", () => fpDlg.close())) return;
  fpPath = null;
  fpSel = null;
  fpState.textContent = "";
  fpDlg.showModal();
  await loadFolders();
});
fpDlg.addEventListener("close", () => opClosed("move", true));
document.getElementById("fpCancel").addEventListener("click", () => fpDlg.close());
document.getElementById("fpCloseX").addEventListener("click", () => fpDlg.close());
document.getElementById("fpHome").addEventListener("click", () => loadFolders());

function renderQuick(quick) {
  fpQuick.innerHTML = "";
  for (const q of quick || []) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "📁 " + q.name;
    b.title = q.path;
    b.addEventListener("click", () => loadFolders(q.path));
    fpQuick.append(b);
  }
}

// 目标文件夹的选中态 + 底部状态条
function fpSyncSel() {
  for (const row of fpList.querySelectorAll(".fp-row"))
    row.classList.toggle("on", row.dataset.path === fpSel);
  fpSelInfo.textContent = `已选 ${selectedIds.size} 张`;
  if (fpSel) fpSelInfo.textContent += ` · 目标：${fpSel}`;
}

async function loadFolders(path) {
  fpList.innerHTML = `<p class="fp-empty">加载中…</p>`;
  let data;
  try {
    data = await (await fetch("/api/fs/list" + (path ? "?path=" + encodeURIComponent(path) : ""))).json();
  } catch (e) {
    fpList.innerHTML = `<p class="fp-empty">无法读取目录</p>`;
    return;
  }
  if (data.detail) {
    fpList.innerHTML = `<p class="fp-empty">${escHtml(data.detail)}</p>`;
    return;
  }
  fpPath = data.path;
  fpParent = data.parent;
  fpSel = data.path;        // 进入文件夹后默认选中它
  fpCur.textContent = data.path + (data.is_default ? "（默认文件夹）" : "");
  renderQuick(data.quick);
  fpList.innerHTML = "";
  for (const d of data.dirs) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "fp-row";
    row.dataset.path = d.path;
    const ico = document.createElement("span");
    ico.className = "ico";
    ico.textContent = "📁";
    const nm = document.createElement("span");
    nm.textContent = d.name;
    row.append(ico, nm);
    row.addEventListener("click", () => { fpSel = d.path; fpSyncSel(); });
    row.addEventListener("dblclick", () => loadFolders(d.path));
    fpList.append(row);
  }
  if (!data.dirs.length) {
    fpList.innerHTML = `<p class="fp-empty">这个文件夹下没有子文件夹</p>`;
  }
  fpSyncSel();
}

document.getElementById("fpUp").addEventListener("click", () => {
  if (fpParent) loadFolders(fpParent);
});

document.getElementById("fpOk").addEventListener("click", async () => {
  const target = fpSel || fpPath;
  if (!selectedIds.size || !target) return;
  const ids = [...selectedIds];
  fpState.textContent = "移动中…";
  try {
    const res = await fetch("/api/photos/move", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, target }),
    });
    const data = await res.json();
    if (!res.ok) { fpState.textContent = data.detail || "移动失败"; return; }
    opClosed("move", true);      // 回到上一次选择，仍留在多选
    fpDlg.close();
    toast(`已移动 ${data.moved} 张` + (data.skipped ? ` · 跳过 ${data.skipped} 张` : "")
      + (data.failed ? ` · 失败 ${data.failed} 张` : ""));
  } catch (e) {
    fpState.textContent = "移动失败";
  }
});

// ---- 分享：先选导出位置并确认，再生成含原图的 zip ----
const shareDlg = document.getElementById("shareDialog");
const shareResult = document.getElementById("shareResult");
const shList = document.getElementById("shList");
const shCur = document.getElementById("shCur");
const shQuick = document.getElementById("shQuick");
let shPath = null;      // 当前所在文件夹
let shParent = null;
let shSel = null;       // 单击选中的导出位置

document.getElementById("selShare").addEventListener("click", async () => {
  if (!selectedIds.size) return;
  if (!beginOp("share", () => shareDlg.close())) return;
  shareResult.textContent = `准备导出 ${selectedIds.size} 张图片，请选择导出位置。`;
  shPath = null;
  shSel = null;
  shareDlg.showModal();
  await loadShareFolders();
});
shareDlg.addEventListener("close", () => opClosed("share", true));
document.getElementById("shareClose").addEventListener("click", () => shareDlg.close());
document.getElementById("shCancel").addEventListener("click", () => shareDlg.close());
document.getElementById("shHome").addEventListener("click", () => loadShareFolders());
document.getElementById("shUp").addEventListener("click", () => {
  if (shParent) loadShareFolders(shParent);
});

function shSyncSel() {
  for (const row of shList.querySelectorAll(".fp-row"))
    row.classList.toggle("on", row.dataset.path === shSel);
  if (shSel) shareResult.textContent = `将导出到：${shSel}`;
}

async function loadShareFolders(path) {
  shList.innerHTML = `<p class="fp-empty">加载中…</p>`;
  let data;
  try {
    data = await (await fetch("/api/fs/list" + (path ? "?path=" + encodeURIComponent(path) : ""))).json();
  } catch (e) {
    shList.innerHTML = `<p class="fp-empty">无法读取目录</p>`;
    return;
  }
  if (data.detail) {
    shList.innerHTML = `<p class="fp-empty">${escHtml(data.detail)}</p>`;
    return;
  }
  shPath = data.path;
  shParent = data.parent;
  shSel = data.path;        // 进入文件夹后默认选中它
  shCur.textContent = data.path + (data.is_default ? "（默认文件夹）" : "");
  shQuick.innerHTML = "";
  for (const q of data.quick || []) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "📁 " + q.name;
    b.title = q.path;
    b.addEventListener("click", () => loadShareFolders(q.path));
    shQuick.append(b);
  }
  shList.innerHTML = "";
  for (const d of data.dirs) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "fp-row";
    row.dataset.path = d.path;
    const ico = document.createElement("span");
    ico.className = "ico";
    ico.textContent = "📁";
    const nm = document.createElement("span");
    nm.textContent = d.name;
    row.append(ico, nm);
    row.addEventListener("click", () => { shSel = d.path; shSyncSel(); });
    row.addEventListener("dblclick", () => loadShareFolders(d.path));
    shList.append(row);
  }
  if (!data.dirs.length) {
    shList.innerHTML = `<p class="fp-empty">这个文件夹下没有子文件夹</p>`;
  }
  shSyncSel();
}

document.getElementById("shOk").addEventListener("click", async () => {
  const dest = shSel || shPath;
  if (!selectedIds.size || !dest) return;
  const ids = [...selectedIds];
  shareResult.textContent = "打包中…";
  try {
    const res = await fetch("/api/share/zip", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, title: `分享 · ${ids.length} 张`, dest }),
    });
    const data = await res.json();
    if (!res.ok) { shareResult.textContent = "失败：" + (data.detail || "未知"); return; }
    const mb = (data.bytes / 1048576).toFixed(1);
    shareResult.textContent = `已导出 ${data.count} 张 · ${mb}MB → ${data.path}`
      + (data.skipped ? `（跳过 ${data.skipped} 张）` : "");
  } catch (e) {
    shareResult.textContent = "打包失败";
  }
});

// ---- album picker dialog (加入图集 / 移出图集) ----
const apDlg = document.getElementById("albumPickDialog");
const apList = document.getElementById("apList");
const apTitle = document.getElementById("apTitle");
const apNewName = document.getElementById("apNewName");
let apCtx = null;  // {ids:Set, membership:Set<albumId>|null, single:bool}

// 图集页「我的图集」里已有的二级类别（自建图集）以卡片形式列出：
// 点一次加入（卡片显示占用态），再点一次移出。
async function openAlbumPicker({ ids, title, single = false }) {
  apCtx = {
    ids: new Set(ids), single,
    membership: single ? new Set(
      (await (await fetch(`/api/albums_of/${ids[0]}`)).json()
      ).albums.map(a => a.id)) : null,
  };
  apTitle.textContent = title || "加入图集";
  apNewName.value = "";
  apList.innerHTML = "";
  const data = await (await fetch("/api/albums")).json();
  const albums = data.user || [];
  if (!albums.length) {
    apList.innerHTML = `<p class="ap-empty">还没有图集，先在下方填名称新建一个</p>`;
  }
  for (const a of albums) {
    const card = albumCard(a.name, `${a.count} 张`, a.cover);
    if (!a.cover) card.classList.add("dim");
    if (apCtx.membership && apCtx.membership.has(a.id)) card.classList.add("on");
    card.title = `${a.name} · ${a.count} 张`;
    card.addEventListener("click", () => toggleAlbumCard(card, a.id));
    apList.append(card);
  }
  apDlg.showModal();
}

async function toggleAlbumCard(card, albumId) {
  const on = !card.classList.contains("on");
  if (!await applyPick(albumId, on)) return;
  card.classList.toggle("on", on);
}

async function applyPick(albumId, on) {
  const ids = [...apCtx.ids];
  const res = await fetch(on ? "/api/albums/add" : "/api/albums/remove", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: albumId, ids }),
  });
  if (!res.ok) { toast(on ? "加入失败" : "移出失败"); return false; }
  if (apCtx.membership) {
    on ? apCtx.membership.add(albumId) : apCtx.membership.delete(albumId);
  }
  return true;
}

async function createAlbum(name) {
  const res = await fetch("/api/albums", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    toast(err.detail === "name taken" ? "已有同名图集" : "创建失败");
    return null;
  }
  return (await res.json()).id;
}

// 「完成」= 若填写了新名称则新建图集并加入，然后回到操作选择界面
document.getElementById("apDone").addEventListener("click", async () => {
  const name = apNewName.value.trim();
  if (name && apCtx) {
    const aid = await createAlbum(name);
    if (!aid) return;               // 创建失败时留在弹窗里修正名称
    apNewName.value = "";
    await applyPick(aid, true);
    if (apCtx.single) refreshDetailAlbumState();
    await renderAlbums();           // 新建的图集追加到「我的图集」行尾
  }
  apDlg.close();
});
apDlg.addEventListener("close", () => {
  opClosed("album", true);      // 关闭后回到上一次选择，仍留在多选
  if (apCtx && apCtx.single) refreshDetailAlbumState();
});
document.getElementById("apClose").addEventListener("click", () => apDlg.close());

function toast(msg) {
  let t = document.getElementById("appToast");
  if (!t) {
    t = document.createElement("div");
    t.id = "appToast";
    t.className = "app-toast";
    document.body.append(t);
  }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove("show"), 1800);
}

// ---- 用户图集卡 ----
const userAlbumGrid = document.getElementById("userAlbumGrid");
const amDlg = document.getElementById("albumMenuDialog");
let amTarget = null;  // {id, name}

function userAlbumCard(a) {
  const card = albumCard(a.name, a.count, a.cover);
  if (!a.count) card.classList.add("dim");
  card.addEventListener("click", () =>
    openAlbum("album=" + a.id, a.name, { album: a.id }));
  const menu = document.createElement("button");
  menu.type = "button";
  menu.className = "al-menu";
  menu.textContent = "⋯";
  menu.title = "图集选项";
  menu.addEventListener("click", (e) => {
    e.stopPropagation();
    amTarget = { id: a.id, name: a.name };
    document.getElementById("amName").textContent = a.name;
    amDlg.showModal();
  });
  card.append(menu);
  return card;
}

document.getElementById("amClose").addEventListener("click", () => amDlg.close());
document.getElementById("amRename").addEventListener("click", async () => {
  const name = prompt("新的图集名称：", amTarget.name);
  if (!name || !name.trim()) return;
  const res = await fetch("/api/albums/rename", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: amTarget.id, name: name.trim() }),
  });
  if (!res.ok) toast(res.status === 400 ? "已有同名图集" : "重命名失败");
  amDlg.close();
  renderAlbums();
});
document.getElementById("amDelete").addEventListener("click", async () => {
  if (!confirm(`删除图集「${amTarget.name}」？照片文件不受影响。`)) return;
  await fetch("/api/albums/" + amTarget.id, { method: "DELETE" });
  amDlg.close();
  renderAlbums();
});

// 详情预览栏：加入图集
const ddAlbumState = document.getElementById("ddAlbumState");
let ddAlbumNames = "";
function refreshDetailAlbumState() {
  ddAlbumState.textContent = ddAlbumNames ? "已在：" + ddAlbumNames : "";
}
document.getElementById("ddAlbum").addEventListener("click", () => {
  if (!detailId) return;
  openAlbumPicker({ ids: [detailId], title: "加入/移出图集", single: true });
});

function escHtml(t) {
  const d = document.createElement("div");
  d.textContent = t;
  return d.innerHTML;
}

// ---- near-duplicates view ----
const dupsEl = document.getElementById("dupsResults");
const dupsGrid = document.getElementById("dupsGrid");
const dupsSummary = document.getElementById("dupsSummary");

function dupCard(item, isLeader) {
  const card = document.createElement("figure");
  card.className = "result-card";
  card.dataset.id = item.id;
  card.addEventListener("click", () => {
    if (selectMode) toggleSelect(card, item.id);
    else openDetail(item.id);
  });
  if (item.status === "broken") {
    const noimg = document.createElement("div");
    noimg.className = "noimg";
    noimg.textContent = "无法读取";
    card.append(noimg);
  } else {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = `/thumb/${item.id}.webp?size=512`;
    img.alt = "";
    card.append(img);
  }
  const sbox = document.createElement("span");
  sbox.className = "sel-box";
  sbox.title = "多选";
  sbox.addEventListener("click", (e) => {
    e.stopPropagation();
    selectMode || enterSelectMode();
    toggleSelect(card, item.id);
  });
  card.append(sbox);
  if (selectMode && selectedIds.has(item.id)) card.classList.add("sel");
  const cap = document.createElement("figcaption");
  cap.className = "cap";
  const date = document.createElement("span");
  date.className = "date";
  date.textContent = fmtDate(item.taken_at) + (isLeader ? " · 组内最早" : "");
  cap.append(date, document.createTextNode(item.path));
  card.append(cap);
  return card;
}

async function runDups() {
  const data = await (await fetch("/api/dups")).json();
  timelineEl.hidden = true;
  resultsEl.hidden = true;
  dupsEl.hidden = false;
  enterBrowse();
  backTo = null;
  updateNavBack();
  dupsGrid.innerHTML = "";
  if (!data.groups.length) {
    dupsSummary.textContent = "没有发现近似重复的图片";
  } else {
    dupsSummary.textContent =
      `发现 ${data.groups.length} 组近似重复，共 ${data.dupes} 张（哈希距离 ≤ 8/64）`;
    data.groups.forEach((g, idx) => {
      const block = document.createElement("div");
      block.className = "dup-group";
      const h = document.createElement("h3");
      h.textContent = `组 ${idx + 1} · ${g.items.length} 张`;
      const grid = document.createElement("div");
      grid.className = "results-grid";
      g.items.forEach((it) => grid.append(dupCard(it, it.id === g.group_id)));
      block.append(h, grid);
      dupsGrid.append(block);
    });
  }
  window.scrollTo(0, 0);
}

document.getElementById("dupsBack").addEventListener("click", goBack);

// ---- on this day (memories from previous years) ----
const otdBanner = document.getElementById("otdBanner");

function otdTitle(date) {
  const [mm, dd] = date.split("-");
  return `那年今日 · ${+mm}月${+dd}日`;
}

async function fetchOnThisDay(date) {
  try {
    const res = await fetch("/api/onthisday" + (date ? "?date=" + date : ""));
    if (!res.ok) return null;
    return await res.json();
  } catch (e) { return null; }
}

function renderOnThisDay(data) {
  enterBrowse();
  timelineEl.hidden = true;
  dupsEl.hidden = true;
  resultsEl.hidden = false;
  backTo = showTimeline;
  updateNavBack();
  resultsGrid.innerHTML = "";
  curGridIds = data.years.flatMap(g => g.items).map(i => i.id);
  resultsCount.textContent =
    `${otdTitle(data.date)} — 来自 ${data.years.length} 个往年 · 共 ${data.count} 张`
    + (data.year_span ? `（${data.year_span}，不含今年）` : "");
  for (const grp of data.years) {
    const block = document.createElement("div");
    block.className = "otd-group";
    const h = document.createElement("h3");
    h.textContent = `${grp.year} 年的今天 · ${grp.count} 张`;
    const grid = document.createElement("div");
    grid.className = "results-grid";
    grp.items.forEach((it) => grid.append(dupCard(it, false)));
    block.append(h, grid);
    resultsGrid.append(block);
  }
  window.scrollTo(0, 0);
}

let otdData = null;
fetchOnThisDay().then((data) => {
  otdData = data;
  if (!data || !data.count) return;
  otdBanner.textContent =
    `${otdTitle(data.date)} · 来自 ${data.years.length} 个往年 · ${data.count} 张，点击查看`;
  otdBanner.hidden = false;
  otdBanner.addEventListener("click", () => {
    if (!otdData) return;
    history.replaceState(null, "", `?view=onthisday&date=${otdData.date}`);
    renderOnThisDay(otdData);
  });
});

// ---- detail card + user notes ----
const detailDlg = document.getElementById("detailDialog");
const ddImage = document.getElementById("ddImage");
const ddVideo = document.getElementById("ddVideo");
const ddOpenSrc = document.getElementById("ddOpenSrc");
const ddBroken = document.getElementById("ddBroken");
const ddMeta = document.getElementById("ddMeta");
const ddOcr = document.getElementById("ddOcr");
const ddOcrText = document.getElementById("ddOcrText");
const ddDups = document.getElementById("ddDups");
const ddDupsText = document.getElementById("ddDupsText");
const ddNote = document.getElementById("ddNote");
const ddSave = document.getElementById("ddSave");
const ddSaveState = document.getElementById("ddSaveState");
let detailId = null;

const KIND_LABEL = { screenshot: "截图", photo: "照片", video: "视频", other: "其他" };
const INLINE_EXT = new Set([".mp4", ".m4v", ".webm", ".mov"]);

async function openDetail(id) {
  let d;
  try {
    const res = await fetch("/api/photo/" + id);
    if (!res.ok) return;
    d = await res.json();
  } catch (e) { return; }
  detailId = id;
  ddMeta.innerHTML = "";
  const state = d.status === "broken" ? "无法读取"
    : (d.status === "missing" || !d.exists) ? "文件不在原位" : "正常";
  const rows = [
    ["时间", fmtDate(d.taken_at)],
    ["类型", KIND_LABEL[d.kind] || d.kind],
    ["尺寸", d.width && d.height ? d.width + "×" + d.height : "?"],
    ["大小", fmtBytes(d.bytes) || "?"],
  ];
  if (d.duration) rows.push(["时长", fmtDur(d.duration)]);
  if (d.place) rows.push(["地点", d.place]);
  rows.push(["状态", state], ["位置", d.path]);
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    ddMeta.append(dt, dd);
  }
  const broken = d.status !== "ok" || !d.exists;
  const ext = (d.ext || "").toLowerCase();
  const isVid = d.kind === "video";
  // reset both media elements first: the pane is reused across items
  ddVideo.pause();
  ddVideo.removeAttribute("src");
  ddVideo.load();
  ddVideo.hidden = true;
  ddOpenSrc.hidden = true;
  ddImage.hidden = broken;
  ddBroken.hidden = !broken;
  if (!broken) {
    if (isVid) {
      ddImage.hidden = true;
      if (INLINE_EXT.has(ext)) {          // stream the original in-pane
        ddVideo.hidden = false;
        ddVideo.poster = `/thumb/${d.id}.webp?size=512`;
        ddVideo.src = `/media/${d.id}`;
      } else {                             // exotic container: poster + escape hatch
        ddImage.src = `/thumb/${d.id}.webp?size=512`;
      }
      ddOpenSrc.hidden = false;
      ddOpenSrc.href = `/media/${d.id}`;
    } else if (ext === ".gif") {
      ddImage.src = `/media/${d.id}`;      // animated original, not the static webp
    } else {
      ddImage.src = `/thumb/${d.id}.webp?size=512`;
    }
  }
  ddOcr.hidden = !d.ocr_raw;
  if (d.ocr_raw) ddOcrText.textContent = d.ocr_raw;
  ddDups.hidden = !(d.duplicates && d.duplicates.length);
  if (d.duplicates && d.duplicates.length) {
    ddDupsText.textContent = `另有 ${d.duplicates.length} 张近似重复，可在「重复」视图中对比处理`;
  }
  ddNote.value = d.note || "";
  ddSaveState.textContent = "";
  ddSimilarBox.hidden = true;
  ddSimilarGrid.innerHTML = "";
  ddSimilarState.textContent = "";
  ddAlbumNames = "";
  refreshDetailAlbumState();
  fetch("/api/albums_of/" + d.id).then(r => r.json()).then(j => {
    if (detailId === d.id) {
      ddAlbumNames = (j.albums || []).map(a => a.name).join("、");
      refreshDetailAlbumState();
    }
  }).catch(() => {});
  openPane();
}

function openPane() {
  detailDlg.hidden = false;
  document.body.classList.add("pane-open");
  updateNavBack();
}
function closePane() {
  detailDlg.hidden = true;
  document.body.classList.remove("pane-open");
  ddVideo.pause();
  ddVideo.removeAttribute("src");
  ddVideo.load();
  detailId = null;
  updateNavBack();
}
document.getElementById("ddClose").addEventListener("click", () => {
  closePane();
  history.replaceState(null, "", location.pathname);
});

// 点击预览栏以外的空白处也能退出查看（点在缩略图/卡片上则交给各自的点击逻辑）
document.addEventListener("click", (e) => {
  if (!detailId) return;
  if (e.target.closest(".detail-pane")) return;
  if (e.target.closest(".tile, .result-card, .album-card, .level-card, .info-card,"
    + " .otd-banner, .topbar, .tabbar, .seg-bar, .select-bar, dialog,"
    + " .app-toast")) return;
  closePane();
  history.replaceState(null, "", location.pathname);
});

// ---- 预览栏宽度拖拽（拖动左边缘改变宽度，从而实现图片缩放） ----
const paneResizer = document.getElementById("paneResizer");
(function restorePaneWidth() {
  const saved = parseInt(localStorage.getItem("paneW") || "", 10);
  if (saved >= 300) document.documentElement.style.setProperty("--pane-w", saved + "px");
})();
let rzDrag = false;
paneResizer.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  rzDrag = true;
  document.body.classList.add("pane-resizing");
  try { paneResizer.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
  e.preventDefault();
});
document.addEventListener("pointermove", (e) => {
  if (!rzDrag) return;
  const min = 300;
  const max = Math.max(min, Math.min(window.innerWidth - 360, 1200));
  const w = Math.round(Math.min(Math.max(window.innerWidth - e.clientX, min), max));
  document.documentElement.style.setProperty("--pane-w", w + "px");
  localStorage.setItem("paneW", String(w));
});
const endPaneResize = () => {
  if (!rzDrag) return;
  rzDrag = false;
  document.body.classList.remove("pane-resizing");
};
document.addEventListener("pointerup", endPaneResize);
document.addEventListener("pointercancel", endPaneResize);
paneResizer.addEventListener("dblclick", () => {
  document.documentElement.style.setProperty("--pane-w", "420px");
  localStorage.setItem("paneW", "420");
});

async function saveNote() {
  if (!detailId) return;
  ddSave.disabled = true;
  try {
    const res = await fetch("/api/photo/note", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: detailId, note: ddNote.value }),
    });
    if (!res.ok) { ddSaveState.textContent = "保存失败"; return; }
    const data = await res.json();
    ddSaveState.textContent = data.note ? "已保存" : "已清空备注";
    // reflect on the timeline tile without a full reload
    const tile = document.querySelector(`.tile[data-id="${detailId}"]`);
    if (tile) {
      const old = tile.querySelector(".note-badge");
      if (old) old.remove();
      if (data.note) {
        const nb = document.createElement("span");
        nb.className = "note-badge";
        nb.textContent = "注";
        nb.title = "有备注：" + data.note;
        tile.append(nb);
      }
    }
  } catch (e) {
    ddSaveState.textContent = "保存失败";
  } finally {
    ddSave.disabled = false;
  }
}

ddSave.addEventListener("click", saveNote);
ddNote.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") saveNote();
});

// "find similar" inside the detail card
const ddSimilar = document.getElementById("ddSimilar");
const ddSimilarState = document.getElementById("ddSimilarState");
const ddSimilarBox = document.getElementById("ddSimilarBox");
const ddSimilarGrid = document.getElementById("ddSimilarGrid");

ddSimilar.addEventListener("click", async () => {
  if (!detailId) return;
  ddSimilar.disabled = true;
  ddSimilarState.textContent = "编码并检索中…";
  try {
    let res = await fetch(`/api/similar/${detailId}?limit=12`);
    if (res.status === 409) {
      ddSimilarState.textContent = "语义模型未安装";
      const ready = await ensureSemanticModel();
      if (!ready) return;
      ddSimilarState.textContent = "编码并检索中…";
      res = await fetch(`/api/similar/${detailId}?limit=12`);
    }
    if (!res.ok) {
      ddSimilarState.textContent = "检索失败";
      return;
    }
    const data = await res.json();
    ddSimilarGrid.innerHTML = "";
    for (const it of data.items) {
      const fig = document.createElement("figure");
      fig.title = `${it.path}\n相似度 ${Math.round(it.score * 100)}%`;
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = `/thumb/${it.id}.webp?size=512`;
      const sc = document.createElement("span");
      sc.className = "score-chip";
      sc.textContent = Math.round(it.score * 100) + "%";
      fig.append(img, sc);
      fig.addEventListener("click", () => openDetail(it.id));
      ddSimilarGrid.append(fig);
    }
    ddSimilarBox.hidden = false;
    ddSimilarState.textContent = data.count ? `找到 ${data.count} 张` : "没有相似结果";
  } catch (e) {
    ddSimilarState.textContent = "检索失败";
  } finally {
    ddSimilar.disabled = false;
  }
});

// ---- gallery shell: tabs / 年月日 browse / albums ----
const tabbar = document.getElementById("tabbar");
const pagesWrap = document.getElementById("pagesWrap");
const pages = {
  photos: document.getElementById("pagePhotos"),
  albums: document.getElementById("pageAlbums"),
};
const levelGridEl = document.getElementById("levelGrid");
const photoSeg = document.getElementById("photoSeg");
const smartGrid = document.getElementById("smartGrid");
const folderGrid = document.getElementById("folderGrid");
const gifGrid = document.getElementById("gifGrid");
const sceneGrid = document.getElementById("sceneGrid");

let curTab = "photos";
let photoLevel = "mine";
let lvYear = null;   // drill-down context for 月/日 levels
let lvMonth = null;
let backTo = null;   // "返回" action set by the current browse view

function showPages() {
  resultsEl.hidden = true;
  dupsEl.hidden = true;
  for (const k of Object.keys(pages)) pages[k].hidden = k !== curTab;
  updateNavBack();
}

function enterBrowse() {
  for (const k of Object.keys(pages)) pages[k].hidden = true;
  updateNavBack();
}

function goBack() {
  if (selectMode) exitSelectMode();   // 离开当前页面时清空勾选
  const f = navBackTarget();
  if (f) { f(); return; }
  showPages();
}

// 各页面内置返回：详情栏 -> 关闭；结果/重复 -> 上一级；图集 -> 照片页；
// 日 -> 月 -> 年 -> 我的照片；我的照片是根页面，没有返回。
function navBackTarget() {
  if (detailId) return closePane;
  if (!resultsEl.hidden) return backTo || showTimeline;
  if (!dupsEl.hidden) return showTimeline;
  if (!levelGridEl.hidden) {
    if (photoLevel === "year") return showTimeline;
    if (photoLevel === "month") return () => showLevel("year");
    return () => showLevel("month");
  }
  if (curTab !== "photos") return () => switchTab("photos");
  return null;
}

function updateNavBack() {
  navBack.hidden = !navBackTarget();
  photoSeg.hidden = !(curTab === "photos" && resultsEl.hidden && dupsEl.hidden);
}

navBack.addEventListener("click", goBack);

function setSeg(level) {
  for (const b of photoSeg.querySelectorAll("button"))
    b.classList.toggle("on", b.dataset.level === level);
}

// 页面/结果视图切换：直接切换，不做滑入动画
function switchTab(name) {
  if (selectMode) exitSelectMode();   // 切换页面：取消全部勾选并退出多选
  curTab = name;
  backTo = null;
  for (const b of tabbar.querySelectorAll("button"))
    b.classList.toggle("on", b.dataset.tab === name);
  showPages();
  if (name === "albums") renderAlbums();
  window.scrollTo(0, 0);
}

tabbar.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) switchTab(b.dataset.tab);
});

photoSeg.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-level]");
  if (!b) return;
  if (b.dataset.level === "mine") showTimeline();
  else showLevel(b.dataset.level);
});

function levelLabel(key, level) {
  const curYear = new Date().getFullYear();
  const p = key.split("-").map(Number);
  if (level === "year") return key + "年";
  if (level === "month") return (p[0] !== curYear ? p[0] + "年" : "") + p[1] + "月";
  return (p[0] !== curYear ? p[0] + "年" : "") + p[1] + "月" + p[2] + "日";
}

function levelCard(g, level) {
  const card = document.createElement("figure");
  card.className = "level-card lv-" + level;
  card.title = `${g.key} · ${g.count} 张`;
  if (g.cover) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = `/thumb/${g.cover.id}.webp?size=512`;
    img.alt = "";
    card.append(img);
  }
  const lab = document.createElement("figcaption");
  lab.className = "lc-label";
  const nm = document.createElement("span");
  nm.className = "lc-name";
  nm.textContent = levelLabel(g.key, level);
  const ct = document.createElement("span");
  ct.className = "lc-count";
  ct.textContent = g.count;
  lab.append(nm, ct);
  card.append(lab);
  card.addEventListener("click", () => {
    if (level === "year") { lvYear = +g.key; lvMonth = null; showLevel("month"); }
    else if (level === "month") { lvMonth = +g.key.split("-")[1]; showLevel("day"); }
    else openDay(g.key);
  });
  return card;
}

async function showLevel(level) {
  photoLevel = level;
  curTab = "photos";
  setSeg(level);
  for (const b of tabbar.querySelectorAll("button"))
    b.classList.toggle("on", b.dataset.tab === "photos");
  showPages();
  timelineEl.hidden = true;               // 先收起时间轴，避免和新画面叠在一起
  levelGridEl.hidden = false;
  levelGridEl.className = "level-grid lv-" + level;
  backTo = null;
  levelGridEl.innerHTML = "";
  const params = new URLSearchParams({ level });
  if (lvYear && (level === "month" || level === "day")) params.set("year", lvYear);
  if (lvMonth && level === "day") params.set("month", lvMonth);
  let data;
  try {
    data = await (await fetch("/api/grouped?" + params)).json();
  } catch (e) { return; }
  if (!data.groups || !data.groups.length) {
    const p = document.createElement("div");
    p.className = "empty";
    p.textContent = "这个范围内还没有照片。";
    levelGridEl.append(p);
    return;
  }
  for (const g of data.groups) {
    if (!g.count) continue;      // 空的时间节点不显示
    levelGridEl.append(levelCard(g, level));
  }
  window.scrollTo(0, 0);
}

async function openDay(key) {
  const data = await (await fetch(
    "/api/photos?level_key=" + key + "&limit=1000")).json();
  showGrid(`${levelLabel(key, "day")} · ${data.items.length} 张`, data.items,
    { onBack: () => showLevel("day"), tiles: true });
}

// ---- albums page (smart + folder albums, all derived from paths) ----
function albumCard(name, countText, coverId) {
  const card = document.createElement("figure");
  card.className = "album-card";
  if (coverId) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = `/thumb/${coverId}.webp?size=512`;
    img.alt = "";
    card.append(img);
  } else {
    const ph = document.createElement("div");
    ph.className = "al-empty";
    ph.textContent = "空";
    card.append(ph);
  }
  const lab = document.createElement("figcaption");
  lab.className = "al-label";
  const nm = document.createElement("span");
  nm.className = "al-name";
  nm.textContent = name;
  const ct = document.createElement("span");
  ct.className = "al-count";
  ct.textContent = countText;
  lab.append(nm, ct);
  card.append(lab);
  return card;
}

async function openAlbum(qs, name, opts = {}) {
  const data = await (await fetch("/api/photos?limit=1000&" + qs)).json();
  showGrid(`${name} · ${data.items.length} 张`, data.items,
    { onBack: opts.onBack });
}

async function renderAlbums() {
  smartGrid.textContent = "加载中…";
  folderGrid.innerHTML = "";
  userAlbumGrid.innerHTML = "";
  gifGrid.innerHTML = "";
  sceneGrid.innerHTML = "";
  let data;
  try {
    data = await (await fetch("/api/albums")).json();
  } catch (e) { smartGrid.textContent = "加载失败"; return; }
  smartGrid.innerHTML = "";
  const qs = {
    phone: "bucket=phone",
    photo: "bucket=photo",
    screenshot: "bucket=screenshot",
    video: "bucket=video",
    record: "bucket=record",
  };
  // 顺序：性质（手机相片/图片/截屏/视频/录屏）→ 来源 → 主题 → 动图 → 我的图集
  for (const a of data.smart) {
    const card = albumCard(a.name, a.count, a.cover);
    if (a.count) {
      card.addEventListener("click", () =>
        openAlbum(qs[a.key], a.name, { onBack: () => switchTab("albums") }));
    } else {
      card.classList.add("dim");
    }
    smartGrid.append(card);
  }
  if (data.gif) {
    const card = albumCard(data.gif.name, data.gif.count, data.gif.cover);
    if (data.gif.count) {
      card.addEventListener("click", () =>
        openAlbum("ext=.gif", data.gif.name,
          { onBack: () => switchTab("albums") }));
    } else {
      card.classList.add("dim");
    }
    gifGrid.append(card);
  }
  for (const a of (data.user || [])) userAlbumGrid.append(userAlbumCard(a));
  if (!userAlbumGrid.children.length) {
    const p = document.createElement("div");
    p.className = "pg-tip";
    p.textContent = "选中图片后点「加入图集…」，填个名称就能新建图集。";
    userAlbumGrid.append(p);
  }
  for (const s of (data.sources || [])) {
    const card = albumCard(s.name, s.count, s.cover);
    if (!s.count) card.classList.add("dim");
    card.title = s.name;
    card.addEventListener("click", () =>
      openAlbum("source=" + encodeURIComponent(s.key), s.name,
        { onBack: () => switchTab("albums") }));
    folderGrid.append(card);
  }
  if (!folderGrid.children.length) {
    const p = document.createElement("div");
    p.className = "pg-tip";
    p.textContent = "索引更多目录后，这里会按图片来源排序。";
    folderGrid.append(p);
  }
  renderScenes();
}

// ---- 智能场景（图集页「主题」分区） ----
function infoCard(name, tip) {
  const card = document.createElement("div");
  card.className = "info-card";
  const h = document.createElement("b");
  h.textContent = name;
  const p = document.createElement("p");
  p.textContent = tip;
  card.append(h, p);
  return card;
}

// 智能场景 = 本地零样本主题分类（风景/建筑/截图/文档/证书/动物/植物/交通工具…）
let scenesTimer = null;
async function renderScenes() {
  if (scenesTimer) { clearTimeout(scenesTimer); scenesTimer = null; }
  let data = null;
  try { data = await (await fetch("/api/scenes")).json(); } catch (e) { return; }
  sceneGrid.innerHTML = "";
  const pr = data.progress || {};
  // 主题不重复性质/来源已有的类别（截屏、文档、其他）
  const DUP_THEMES = new Set(["截图", "文档", "其他"]);
  const list = (data.scenes || [])
    .filter(s => s.count > 0 && !DUP_THEMES.has(s.scene));
  for (const s of list) {
    const card = albumCard(s.scene, `${s.count} 张`, s.cover);
    card.addEventListener("click", () =>
      openAlbum("scene_in=" + encodeURIComponent((s.keys || [s.scene]).join(",")),
        s.scene, { onBack: () => switchTab("albums") }));
    sceneGrid.append(card);
  }
  if (!list.length) {
    const card = infoCard("智能场景",
      pr.running ? "正在分析图片主题…"
        : !data.model_present ? "主题识别需要本地语义模型（未安装），照片不会被上传。"
        : data.pending ? `${data.pending} 张待分类，点击开始`
        : "还没有可分组的照片主题。");
    if (!data.model_present) {
      card.addEventListener("click", async () => {
        const ready = await ensureSemanticModel();
        if (ready) renderScenes();
      });
    } else if (data.pending && !pr.running) {
      card.addEventListener("click", async () => {
        await fetch("/api/scenes/scan", { method: "POST" });
        renderScenes();
      });
    }
    sceneGrid.append(card);
  }
  if (pr.running) scenesTimer = setTimeout(renderScenes, 4000);
}

pollStatus();
loadMore();

// deep link: /?q=... searches; /?view=dups opens dup view; /?id=... opens detail card
const params = new URLSearchParams(location.search);
const initialQ = params.get("q");
if (initialQ) {
  searchInput.value = initialQ;
  runSearch(initialQ);
} else if (params.get("view") === "dups") {
  runDups();
} else if (params.get("view") === "onthisday") {
  fetchOnThisDay(params.get("date")).then((d) => {
    if (d && d.count) renderOnThisDay(d);
  });
} else if (params.get("level")) {
  if (params.get("year")) lvYear = +params.get("year");
  if (params.get("month")) lvMonth = +params.get("month");
  showLevel(params.get("level"));
} else if (params.get("tab") === "albums") {
  switchTab("albums");
} else if (params.get("id")) {
  openDetail(params.get("id")).then(() => {
    if (params.get("similar")) ddSimilar.click();
  });
}

