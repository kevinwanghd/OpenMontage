/**
 * UI-11 · L1 review console (P1a).
 *
 * Static, dependency-free module. It consumes only the frozen M0 surface
 * (§10): review, episode/progress, library and the LocalMediaLocator media
 * routes. No external API, no CDN, no analytics.
 *
 * The API enforces local-only, same-origin CSRF (UI-02), so the page must be
 * served from the API origin. The API base therefore defaults to
 * ``window.location.origin``; ``?api=`` is available for same-origin mounts.
 */

// --- API surface (§10) ----------------------------------------------------

const ENDPOINTS = {
  episodes: "/api/v1/episodes",
  episode: (id) => `/api/v1/episode/${encodeURIComponent(id)}`,
  reviewPolicy: (id) => `/api/v1/episode/${encodeURIComponent(id)}/review_policy`,
  stats: (id) => `/api/v1/progress/${encodeURIComponent(id)}/stats`,
  pending: "/api/v1/review/pending",
  decision: (id) => `/api/v1/review/${encodeURIComponent(id)}/decision`,
  undo: (id) => `/api/v1/review/${encodeURIComponent(id)}/undo`,
  library: (kind) => `/api/v1/library/${encodeURIComponent(kind)}`,
};

const ACTIONS = new Set(["accept", "reject", "pick_variant", "edit_prompt", "send_back", "skip"]);

const VIDEO_EXTENSIONS = new Set(["mp4", "webm", "mov", "m4v", "ogv"]);

// --- media URLs (LocalMediaLocator surface) -------------------------------

function mediaSegments(relPath) {
  const parts = String(relPath ?? "")
    .replace(/\\/g, "/")
    .split("/")
    .filter((part) => part && part !== ".");
  if (!parts.length) return null;
  // The server rejects traversal/absolute paths (UI-02); never emit them.
  if (parts.some((part) => part === ".." || part.includes(":"))) return null;
  return parts.map((part) => encodeURIComponent(part)).join("/");
}

export function episodeMediaUrl(episode, relPath) {
  const path = mediaSegments(relPath);
  return path ? `/media/${encodeURIComponent(episode)}/${path}` : null;
}

export function episodeThumbUrl(episode, relPath) {
  const path = mediaSegments(relPath);
  return path ? `/thumb/${encodeURIComponent(episode)}/${path}` : null;
}

export function libraryMediaUrl(kind, assetId, relPath) {
  const path = mediaSegments(relPath);
  if (!path) return null;
  return `/media/library/${encodeURIComponent(kind)}/${encodeURIComponent(assetId)}/${path}`;
}

function extensionOf(path) {
  const clean = String(path ?? "").split(/[?#]/)[0];
  const dot = clean.lastIndexOf(".");
  return dot === -1 ? "" : clean.slice(dot + 1).toLowerCase();
}

function isVideo(path) {
  return VIDEO_EXTENSIONS.has(extensionOf(path));
}

// --- state ---------------------------------------------------------------

const params = new URLSearchParams(window.location.search);
// UI-02: the API only accepts same-origin calls, so the default is the serving
// origin. ``?api=`` exists for same-origin sub-path mounts, never for a
// different host (the server rejects cross-origin requests).
const apiBase = params.get("api") || window.location.origin;

const state = {
  apiBase,
  episodes: [],
  episode: null,
  manifest: null,
  stats: null,
  items: [],
  index: 0,
  mode: "review",
  selectedCandidate: null,
  selectedReason: null,
  lastDecision: null,
  flagged: new Set(),
  budget: 10,
  spent: 0,
  policy: {
    mode: "sampled",
    detail_ratio: 0.35,
    face_sim_threshold: 0.75,
    sampling_salt: "default",
  },
};

const $ = (id) => document.getElementById(id);

const els = {
  episodeSelect: $("episode-select"),
  reload: $("reload-button"),
  queueCount: $("queue-count"),
  modeBadge: $("mode-badge"),
  errorBanner: $("error-banner"),
  staleBanner: $("stale-banner"),
  wallGrid: $("wall-grid"),
  wallSummary: $("wall-summary"),
  wallEmpty: $("wall-empty"),
  partialNote: $("partial-note"),
  itemTitle: $("item-title"),
  itemStatus: $("item-status"),
  metaStage: $("meta-stage"),
  metaSlot: $("meta-slot"),
  metaPriority: $("meta-priority"),
  metaFace: $("meta-face"),
  metaVersion: $("meta-version"),
  criteriaList: $("criteria-list"),
  playerImage: $("player-image"),
  playerVideo: $("player-video"),
  playerEmpty: $("player-empty"),
  openOriginal: $("open-original"),
  playerCaption: $("player-caption"),
  ratioSlider: $("ratio-slider"),
  ratioValue: $("ratio-value"),
  itemCost: $("item-cost"),
  spent: $("spent"),
  budgetInput: $("budget-input"),
  budgetBar: $("budget-bar"),
  pendingCost: $("pending-cost"),
  budgetStatus: $("budget-status"),
  rejectPanel: $("reject-panel"),
  reasonList: $("reason-code-list"),
  confirmReject: $("confirm-reject"),
  noteInput: $("note-input"),
  promptPatch: $("prompt-patch"),
  submitPrompt: $("submit-prompt"),
  undoButton: $("undo-button"),
  libraryKind: $("library-kind"),
  libraryRefresh: $("library-refresh"),
  libraryGrid: $("library-grid"),
  libraryEmpty: $("library-empty"),
  connection: $("connection-status"),
  live: $("status-live"),
  helpToggle: $("help-toggle"),
  helpDialog: $("help-dialog"),
  helpRows: $("help-rows"),
};

// --- JSON contract blocks (tested by tests/contracts/test_shortdrama_ui.py)

function readJsonBlock(id) {
  const node = $(id);
  if (!node) return null;
  try {
    return JSON.parse(node.textContent);
  // risk:swallowed-exception reason:"静态契约 JSON 块由契约测试保证格式，解析失败回退空映射不阻塞页面渲染" owner:@kevin reviewed:2026-11-11
  } catch {
    return null;
  }
}

const KEYBOARD_MAP = readJsonBlock("keyboard-map") || {};
const STAGE_REASON_CODES = readJsonBlock("reason-codes") || {};
const REASON_CODES = [...new Set(Object.values(STAGE_REASON_CODES).flat())].sort();

// --- small helpers -------------------------------------------------------

class ApiFailure extends Error {
  constructor(envelope) {
    super(envelope.what || envelope.code || "API error");
    this.code = envelope.code || "UNKNOWN_ERROR";
    this.what = envelope.what || this.message;
    this.fix = envelope.fix || "重试或查看服务端日志";
    this.recovery = envelope.recovery || "ask-human";
    this.sideEffects = envelope.side_effects || "unknown";
  }
}

function announce(message) {
  els.live.textContent = message;
}

function setConnection(message, kind = "info") {
  els.connection.textContent = message;
  els.connection.dataset.kind = kind;
}

function showError(error) {
  const failure = error instanceof ApiFailure ? error : new ApiFailure({ what: String(error) });
  els.errorBanner.hidden = false;
  els.errorBanner.textContent = `[${failure.code}] ${failure.what} → ${failure.fix}`;
  setConnection(failure.what, "error");
  announce(failure.what);
}

function clearError() {
  els.errorBanner.hidden = true;
  els.errorBanner.textContent = "";
}

async function request(path, { method = "GET", body, headers = {} } = {}) {
  const init = {
    method,
    headers: { ...headers },
    credentials: "same-origin",
  };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`${state.apiBase}${path}`, init);
  } catch (error) {
    throw new ApiFailure({
      code: "NETWORK_ERROR",
      what: `无法连接 API（${error.message}）`,
      fix: "确认 127.0.0.1 上的 API 已启动，并从 API 同源打开本控制台",
      recovery: "fix-then-retry",
    });
  }
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (contentType.includes("application/json")) {
    // risk:swallowed-exception reason:"错误响应体可能不是合法 JSON，解析失败保留 null 并交由状态码生成 ApiFailure" owner:@kevin reviewed:2026-11-11
    payload = await response.json().catch(() => null);
  }
  if (!response.ok) {
    throw new ApiFailure(payload || { code: `HTTP_${response.status}`, what: response.statusText });
  }
  return payload;
}

function idempotencyKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** Observe an async UI action; failures were already surfaced by showError. */
function safely(promise) {
  promise.catch((error) => {
    if (!(error instanceof ApiFailure)) showError(error);
  });
}

// --- persistence (budget + session spend) --------------------------------

function budgetKey() {
  return "om.review-console.budget";
}

function spentKey() {
  return `om.review-console.spent.${state.episode || "unknown"}`;
}

function readNumber(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    const value = raw === null ? NaN : Number(raw);
    return Number.isFinite(value) ? value : fallback;
  // risk:swallowed-exception reason:"隐私模式或受限环境下 localStorage 不可用，读取失败回退默认预算以保持审核可用" owner:@kevin reviewed:2026-11-11
  } catch {
    return fallback;
  }
}

function writeNumber(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
  // risk:swallowed-exception reason:"隐私模式或受限环境下 localStorage 写入失败仅影响会话持久化，不影响决策提交与成本展示" owner:@kevin reviewed:2026-11-11
  } catch {
    /* spend stays in-memory for this session */
  }
}

function money(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

// --- status language (§11.2) ---------------------------------------------

const STATUS_LABELS = {
  PASS: "✓ PASS",
  FAIL: "✕ FAIL",
  UNKNOWN: "? UNKNOWN",
  "N/A": "— N/A",
  BLOCKED: "⊘ BLOCKED",
  STALE: "⟳ STALE",
};

function statusNode(status) {
  const span = document.createElement("span");
  span.className = "status-token";
  span.dataset.status = status;
  span.title = {
    PASS: "机器与人检均通过",
    FAIL: "阈值未达标或人检驳回",
    UNKNOWN: "机器无法判定，需人工确认",
    "N/A": "该阶段不适用此检查",
    BLOCKED: "预算或其他门禁阻塞，需先处理",
    STALE: "候选版本已被取代，需重审",
  }[status];
  const icon = document.createElement("span");
  icon.className = "status-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = STATUS_LABELS[status].slice(0, STATUS_LABELS[status].indexOf(" "));
  const text = document.createElement("span");
  text.className = "status-text";
  text.textContent = status;
  span.append(icon, text);
  return span;
}

function itemStatus(item) {
  if (item.__stale) return "STALE";
  const needsFace = new Set(["shot_gen", "keyframe", "character_cast"]);
  if (item.face_similarity === null || item.face_similarity === undefined) {
    return needsFace.has(item.stage) ? "UNKNOWN" : "N/A";
  }
  const threshold = Number(state.policy.face_sim_threshold ?? 0.75);
  return Number(item.face_similarity) >= threshold ? "PASS" : "FAIL";
}

// --- loading -------------------------------------------------------------

async function loadEpisodes() {
  const data = await request(ENDPOINTS.episodes);
  state.episodes = data.episodes || [];
  els.episodeSelect.innerHTML = "";
  for (const episode of state.episodes) {
    const option = document.createElement("option");
    option.value = episode.episode_id;
    option.textContent = `${episode.project_id} / ${episode.episode_id}`;
    els.episodeSelect.appendChild(option);
  }
  if (!state.episodes.length) {
    els.wallEmpty.hidden = false;
    els.wallEmpty.textContent = "未发现剧集；先运行 om demo --mock 种一集 mock 候选。";
    els.wallGrid.innerHTML = "";
    els.queueCount.textContent = "0 待审";
    setConnection("未发现剧集");
    return false;
  }
  const requested = params.get("episode");
  const selected = state.episodes.find((episode) => episode.episode_id === requested);
  state.episode = (selected || state.episodes[0]).episode_id;
  els.episodeSelect.value = state.episode;
  return true;
}

async function loadEpisode(episodeId) {
  clearError();
  state.episode = episodeId;
  state.index = 0;
  state.selectedCandidate = null;
  state.selectedReason = null;
  state.lastDecision = null;
  state.spent = readNumber(spentKey(), 0);
  state.budget = readNumber(budgetKey(), 10);
  els.budgetInput.value = state.budget.toFixed(2);
  const [manifest, stats] = await Promise.all([
    request(ENDPOINTS.episode(episodeId)),
    // risk:swallowed-exception reason:"进度统计是可选展示信息，统计接口失败时待审队列仍必须可用" owner:@kevin reviewed:2026-11-11
    request(ENDPOINTS.stats(episodeId)).catch(() => null),
  ]);
  state.manifest = manifest;
  state.stats = stats;
  state.policy = { ...state.policy, ...(manifest.review_policy || {}) };
  syncSamplingControls();
  await loadPending();
}

async function loadPending() {
  const query = new URLSearchParams({
    episode: state.episode,
    mode: state.policy.mode,
    ratio: String(state.policy.detail_ratio),
  });
  const data = await request(`${ENDPOINTS.pending}?${query.toString()}`);
  state.items = (data.items || []).map((item) => ({ ...item }));
  state.policy = { ...state.policy, ...(data.policy || {}) };
  state.index = Math.min(state.index, Math.max(state.items.length - 1, 0));
  state.selectedCandidate = null;
  renderWall();
  renderReviewPanel();
  renderCost();
  renderPartialState();
  setConnection(`已加载 ${state.items.length} 个待审项`);
}

function syncSamplingControls() {
  const radio = document.querySelector(`input[name="sampling-mode"][value="${state.policy.mode}"]`);
  if (radio) radio.checked = true;
  els.ratioSlider.value = String(state.policy.detail_ratio);
  els.ratioValue.textContent = `${Math.round(state.policy.detail_ratio * 100)}%`;
}

function renderPartialState() {
  if (!state.stats) {
    els.partialNote.hidden = true;
    return;
  }
  const pendingGeneration = Object.entries(state.stats.shot_states || {}).some(([shotState, count]) =>
    count > 0 && ["planned", "keyframing", "generating"].includes(shotState)
  );
  els.partialNote.hidden = !pendingGeneration;
  if (pendingGeneration) {
    els.partialNote.textContent =
      "部分镜头未生成（not-generated）· 仅已生成候选可审，其余等待生成";
  }
}

// --- candidate wall (L1 contact sheet, VIS-03 / D-10) ---------------------

function ariaLabelFor(item) {
  const face =
    item.face_similarity === null || item.face_similarity === undefined
      ? "face unknown"
      : `face ${Number(item.face_similarity).toFixed(2)}${
          item.face_similarity >= state.policy.face_sim_threshold ? " meets threshold" : " below threshold"
        }`;
  const priority = ["must-view", "recommended", "skippable"][item.priority] || `priority ${item.priority}`;
  const criteria = (item.criteria || []).join(", ");
  return `shot ${item.slot}, stage ${item.stage}, ${face}, ${priority}${
    criteria ? `, ${criteria}` : ""
  }, cost ${money(item.cost_if_approved_usd)}, candidates ${item.candidates?.length || 0}`;
}

function currentItem() {
  return state.items[state.index] || null;
}

function renderWall() {
  els.wallGrid.innerHTML = "";
  els.wallEmpty.hidden = state.items.length > 0;
  els.queueCount.textContent = `${state.items.length} 待审`;
  els.wallSummary.textContent = state.items.length
    ? `${state.items.length} 项 · ${state.policy.mode} @ ${Math.round(state.policy.detail_ratio * 100)}%`
    : "队列为空";

  state.items.forEach((item, index) => {
    els.wallGrid.appendChild(renderRow(item, index));
  });

  if (!state.items.length) {
    els.wallEmpty.textContent = "24 镜已审完—查看决策";
    if (state.stats) {
      const decided = state.stats.review?.decided ?? 0;
      els.wallEmpty.textContent = `${decided} 镜已审完—查看决策`;
    }
    return;
  }
  els.wallEmpty.hidden = true;
}

function renderRow(item, index) {
  const row = document.createElement("div");
  row.className = "candidate-row";
  row.setAttribute("role", "row");
  row.dataset.reviewId = item.review_id;
  row.dataset.index = String(index);
  row.setAttribute("aria-selected", String(index === state.index));
  row.setAttribute("aria-label", ariaLabelFor(item));
  row.tabIndex = index === state.index ? 0 : -1;
  if (state.flagged.has(item.review_id)) row.classList.add("flagged");

  const head = document.createElement("div");
  head.className = "row-head";
  head.setAttribute("role", "rowheader");

  const title = document.createElement("span");
  title.className = "row-title";
  title.textContent = `${item.slot} · ${item.stage}`;
  head.appendChild(title);

  const badges = document.createElement("span");
  badges.className = "row-badges";
  const priority = document.createElement("span");
  priority.className = "badge";
  priority.textContent = ["必看 must-view", "建议 recommended", "可跳 skippable"][item.priority] || `P${item.priority}`;
  badges.appendChild(priority);
  if (state.flagged.has(item.review_id)) {
    const flagged = document.createElement("span");
    flagged.className = "badge";
    flagged.textContent = "标疑 ?";
    badges.appendChild(flagged);
  }
  badges.appendChild(statusNode(itemStatus(item)));
  head.appendChild(badges);
  row.appendChild(head);

  const cells = document.createElement("div");
  cells.className = "candidate-cells";
  (item.candidates || []).forEach((candidate, candidateIndex) => {
    cells.appendChild(renderCandidateCell(item, candidate, candidateIndex));
  });
  if (!item.candidates?.length) {
    const missing = document.createElement("p");
    missing.className = "hint";
    missing.textContent = "无候选（not-generated）";
    cells.appendChild(missing);
  }
  row.appendChild(cells);

  row.addEventListener("click", (event) => {
    if (event.target.closest(".candidate-cell")) return;
    selectItem(index);
  });
  return row;
}

function renderCandidateCell(item, candidate, candidateIndex) {
  const cell = document.createElement("button");
  cell.type = "button";
  cell.className = "candidate-cell";
  cell.setAttribute("role", "gridcell");
  cell.dataset.candidate = candidate;
  cell.setAttribute("aria-selected", String(candidate === state.selectedCandidate));
  cell.setAttribute("aria-label", `candidate ${candidateIndex + 1}: ${candidate}`);

  const label = document.createElement("span");
  label.className = "cell-label";
  label.textContent = `${candidateIndex + 1}. ${candidate}`;
  cell.appendChild(label);

  const media = createThumbMedia(item.episode || state.episode, candidate);
  if (media) cell.insertBefore(media, label);

  cell.addEventListener("click", (event) => {
    event.stopPropagation();
    selectItem(state.items.findIndex((row) => row.review_id === item.review_id));
    selectCandidate(candidate);
  });
  return cell;
}

function createThumbMedia(episode, candidate) {
  const mediaUrl = episodeMediaUrl(episode, candidate);
  const thumbUrl = episodeThumbUrl(episode, candidate);
  if (!mediaUrl) return null;

  if (isVideo(candidate)) {
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "metadata";
    video.playsInline = true;
    video.src = mediaUrl;
    video.addEventListener("error", () => replaceWithMissing(video, episode, candidate), { once: true });
    return video;
  }

  const image = document.createElement("img");
  image.loading = "lazy";
  image.alt = "";
  image.src = thumbUrl || mediaUrl;
  let fellBack = false;
  image.addEventListener("error", () => {
    if (!fellBack && thumbUrl) {
      fellBack = true;
      image.src = mediaUrl;
      return;
    }
    replaceWithMissing(image, episode, candidate);
  });
  return image;
}

function replaceWithMissing(node, episode, candidate) {
  const host = node.parentElement;
  node.remove();
  if (!host || host.querySelector(".media-missing")) return;
  const missing = document.createElement("div");
  missing.className = "media-missing";
  missing.textContent = "媒体缺失";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "重试";
  retry.addEventListener("click", (event) => {
    event.stopPropagation();
    missing.replaceWith(createThumbMedia(episode, candidate));
  });
  missing.appendChild(retry);
  host.insertBefore(missing, host.firstChild);
}

function selectItem(index) {
  if (index < 0 || index >= state.items.length) return;
  state.index = index;
  const item = currentItem();
  state.selectedCandidate = item?.candidates?.[0] || null;
  markSelectedRow();
  renderReviewPanel();
  renderCost();
  if (state.selectedCandidate) loadPlayer(item, state.selectedCandidate);
}

function markSelectedRow() {
  for (const row of els.wallGrid.querySelectorAll(".candidate-row")) {
    const selected = Number(row.dataset.index) === state.index;
    row.setAttribute("aria-selected", String(selected));
    row.tabIndex = selected ? 0 : -1;
  }
}

function selectCandidate(candidate) {
  const item = currentItem();
  if (!item) return;
  state.selectedCandidate = candidate;
  for (const cell of els.wallGrid.querySelectorAll(".candidate-cell")) {
    cell.setAttribute("aria-selected", String(cell.dataset.candidate === candidate));
  }
  loadPlayer(item, candidate);
  announce(`已选择候选 ${candidate}`);
}

// --- player --------------------------------------------------------------

function loadPlayer(item, candidate) {
  const url = episodeMediaUrl(item.episode || state.episode, candidate);
  els.playerImage.hidden = true;
  els.playerVideo.hidden = true;
  els.playerEmpty.hidden = Boolean(url);
  els.playerCaption.textContent = candidate || "";

  if (!url) {
    els.playerEmpty.hidden = false;
    els.playerEmpty.textContent = "候选路径无效";
    els.openOriginal.hidden = true;
    return;
  }

  els.openOriginal.hidden = false;
  els.openOriginal.href = url;

  if (isVideo(candidate)) {
    els.playerVideo.src = url;
    els.playerVideo.hidden = false;
  } else {
    els.playerImage.src = url;
    els.playerImage.alt = `${item.slot} 候选 ${candidate}`;
    els.playerImage.hidden = false;
  }
}

function resetPlayer() {
  els.playerVideo.pause?.();
  els.playerVideo.removeAttribute("src");
  els.playerVideo.hidden = true;
  els.playerImage.removeAttribute("src");
  els.playerImage.hidden = true;
  els.openOriginal.hidden = true;
  els.playerEmpty.hidden = false;
  els.playerEmpty.textContent = "选择候选后在此播放";
  els.playerCaption.textContent = "";
}

function renderReviewPanel() {
  const item = currentItem();
  if (!item) {
    els.itemTitle.textContent = "选择一项开始审核";
    els.itemStatus.innerHTML = "";
    for (const node of [els.metaStage, els.metaSlot, els.metaPriority, els.metaFace, els.metaVersion]) {
      node.textContent = "—";
    }
    els.criteriaList.innerHTML = "";
    resetPlayer();
    return;
  }

  els.itemTitle.textContent = `${item.slot} · ${item.stage}`;
  els.itemStatus.innerHTML = "";
  els.itemStatus.appendChild(statusNode(itemStatus(item)));
  els.metaStage.textContent = item.stage;
  els.metaSlot.textContent = item.slot;
  els.metaPriority.textContent =
    ["必看", "建议", "可跳"][item.priority] || String(item.priority);
  els.metaFace.textContent =
    item.face_similarity === null || item.face_similarity === undefined
      ? "未测量"
      : `${Number(item.face_similarity).toFixed(2)} / ${Number(state.policy.face_sim_threshold ?? 0.75).toFixed(2)}`;
  els.metaVersion.textContent = `${item.candidate_version} · review ${item.review_version}`;
  els.criteriaList.innerHTML = "";
  for (const criterion of item.criteria || []) {
    const li = document.createElement("li");
    li.textContent = criterion;
    els.criteriaList.appendChild(li);
  }
  if (state.selectedCandidate) {
    loadPlayer(item, state.selectedCandidate);
  } else if (item.candidates?.length) {
    selectCandidate(item.candidates[0]);
  } else {
    resetPlayer();
  }
}

// --- cost (X-14: per-item cost + spent/budget) ----------------------------

function renderCost() {
  const item = currentItem();
  const pending = state.items.reduce((sum, row) => sum + Number(row.cost_if_approved_usd || 0), 0);
  els.itemCost.textContent = money(item?.cost_if_approved_usd || 0);
  els.spent.textContent = money(state.spent);
  els.pendingCost.textContent = money(pending);
  els.budgetBar.max = Math.max(state.budget, 0.01);
  els.budgetBar.value = Math.min(state.spent, state.budget);

  const over = state.spent + pending > state.budget;
  els.budgetStatus.innerHTML = "";
  if (over) {
    els.budgetStatus.appendChild(statusNode("BLOCKED"));
    els.budgetStatus.title = "已花 + 待核准超出预算；先调高预算或拒绝部分候选";
  }
}

function addSpend(item) {
  state.spent = Number((state.spent + Number(item.cost_if_approved_usd || 0)).toFixed(4));
  writeNumber(spentKey(), state.spent);
}

// --- decision flow -------------------------------------------------------

async function submitDecision(action) {
  if (!ACTIONS.has(action)) throw new Error(`unknown action '${action}'`);
  const item = currentItem();
  if (!item) {
    announce("没有待审项");
    return;
  }

  if (action === "reject" && !state.selectedReason) {
    enterRejectMode();
    announce("驳回必须先选择原因码");
    return;
  }

  const payload = {
    action,
    chosen_candidate:
      action === "accept" || action === "pick_variant" ? state.selectedCandidate || item.candidates?.[0] || null : null,
    reason_code: action === "reject" ? state.selectedReason : null,
    note: els.noteInput.value.trim(),
    prompt_patch: action === "edit_prompt" ? els.promptPatch.value.trim() || null : null,
    candidate_version: item.candidate_version ?? null,
    override: false,
    evidence: "",
    decided_by: "owner",
  };

  try {
    setConnection(`提交 ${action}…`);
    const result = await request(ENDPOINTS.decision(item.review_id), {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: payload,
    });
    if (action === "accept" || action === "pick_variant") addSpend(item);
    state.lastDecision = { review_id: item.review_id, action, item };
    removeCurrentItem(index_of_item(item.review_id));
    exitRejectMode();
    setConnection(`已${actionLabel(action)} ${item.slot}`);
    announce(`已${actionLabel(action)} ${item.slot}，下一项 ${currentItem()?.slot || "无"}`);
    for (const key of ["noteInput", "promptPatch"]) els[key].value = "";
    focusCurrentRow();
    return result;
  } catch (error) {
    showError(error);
    if (error instanceof ApiFailure && error.code === "STALE_REVIEW") {
      els.staleBanner.hidden = false;
      await loadPending();
    }
    throw error;
  }
}

function index_of_item(reviewId) {
  return state.items.findIndex((item) => item.review_id === reviewId);
}

function removeCurrentItem(index) {
  if (index < 0) return;
  state.items.splice(index, 1);
  state.index = Math.min(state.index, Math.max(state.items.length - 1, 0));
  renderWall();
  renderReviewPanel();
  renderCost();
}

function actionLabel(action) {
  return {
    accept: "通过",
    reject: "驳回",
    pick_variant: "选择变体",
    edit_prompt: "提交提示词补丁",
    send_back: "打回",
    skip: "跳过",
  }[action] || action;
}

async function undoLast() {
  if (!state.lastDecision) {
    setConnection("没有可撤销的决策");
    return;
  }
  try {
    await request(ENDPOINTS.undo(state.lastDecision.review_id), {
      method: "POST",
      body: { reason: "console undo (U)" },
    });
    const undone = state.lastDecision;
    if (undone.action === "accept" || undone.action === "pick_variant") {
      state.spent = Math.max(0, Number((state.spent - Number(undone.item.cost_if_approved_usd || 0)).toFixed(4)));
      writeNumber(spentKey(), state.spent);
    }
    state.lastDecision = null;
    setConnection(`已撤销 ${undone.review_id}`);
    announce(`已撤销 ${undone.review_id}`);
    await loadPending();
  } catch (error) {
    showError(error);
  }
}

// --- reject mode (§11.3: same key map, visible mode badge) ---------------

function enterRejectMode() {
  const item = currentItem();
  if (!item) return;
  state.mode = "reject";
  state.selectedReason = null;
  document.body.dataset.mode = "reject";
  els.modeBadge.dataset.mode = "reject";
  els.modeBadge.textContent = "REJECT 模式 · 选择原因码";
  renderReasonCodes(item);
  els.rejectPanel.hidden = false;
  els.confirmReject.disabled = true;
  const first = els.reasonList.querySelector("button");
  first?.focus();
  announce("REJECT 模式，1/2/3 选择原因码，Esc 退出");
}

function exitRejectMode() {
  state.mode = "review";
  state.selectedReason = null;
  document.body.dataset.mode = "review";
  els.modeBadge.dataset.mode = "review";
  els.modeBadge.textContent = "REVIEW 模式";
  els.rejectPanel.hidden = true;
}

function renderReasonCodes(item) {
  const codes = STAGE_REASON_CODES[item.stage] || [];
  els.reasonList.innerHTML = "";
  codes.forEach((code, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.reasonCode = code;
    button.setAttribute("aria-pressed", "false");
    button.textContent = index < 3 ? `${index + 1}. ${code}` : code;
    button.addEventListener("click", () => chooseReason(code, button));
    els.reasonList.appendChild(button);
  });
}

function chooseReason(code, button) {
  state.selectedReason = code;
  for (const node of els.reasonList.querySelectorAll("button")) {
    node.setAttribute("aria-pressed", String(node.dataset.reasonCode === code));
  }
  if (button) button.focus();
  els.confirmReject.disabled = false;
  announce(`原因码 ${code}`);
}

function renderFlag(reviewId) {
  const row = els.wallGrid.querySelector(`.candidate-row[data-review-id="${CSS.escape(reviewId)}"]`);
  if (!row) return;
  row.classList.toggle("flagged", state.flagged.has(reviewId));
}

function toggleFlag() {
  const item = currentItem();
  if (!item) return;
  if (state.flagged.has(item.review_id)) state.flagged.delete(item.review_id);
  else state.flagged.add(item.review_id);
  renderFlag(item.review_id);
  announce(state.flagged.has(item.review_id) ? `已标疑 ${item.slot}` : `取消标疑 ${item.slot}`);
}

// --- editing / navigation ------------------------------------------------

function submitPromptPatch() {
  const item = currentItem();
  if (!item) return;
  if (!els.promptPatch.value.trim()) {
    els.promptPatch.focus();
    setConnection("输入提示词补丁后按 E 或点击「提交提示词补丁」");
    return;
  }
  submitDecision("edit_prompt");
}

function moveSelection(delta) {
  if (!state.items.length) return;
  selectItem(Math.min(Math.max(state.index + delta, 0), state.items.length - 1));
  focusCurrentRow();
}

function focusCurrentRow() {
  const row = els.wallGrid.querySelector(`.candidate-row[data-index="${state.index}"]`);
  row?.focus();
}

function selectCandidateByDigit(number) {
  const item = currentItem();
  const candidate = item?.candidates?.[number - 1];
  if (candidate) selectCandidate(candidate);
}

function selectReasonByDigit(number) {
  const buttons = [...els.reasonList.querySelectorAll("button")];
  const button = buttons[number - 1];
  if (button) chooseReason(button.dataset.reasonCode, button);
}

// --- sampling policy -----------------------------------------------------

let policyTimer = null;

function schedulePolicySave() {
  window.clearTimeout(policyTimer);
  policyTimer = window.setTimeout(savePolicy, 250);
}

async function savePolicy() {
  if (!state.episode) return;
  const mode = document.querySelector('input[name="sampling-mode"]:checked')?.value || "sampled";
  const detailRatio = Number(els.ratioSlider.value);
  state.policy = { ...state.policy, mode, detail_ratio: detailRatio };
  try {
    await request(ENDPOINTS.reviewPolicy(state.episode), {
      method: "PUT",
      body: {
        mode,
        detail_ratio: detailRatio,
        face_sim_threshold: state.policy.face_sim_threshold,
        sampling_salt: state.policy.sampling_salt,
      },
    });
    await loadPending();
    setConnection(`策略已更新：${mode} @ ${Math.round(detailRatio * 100)}%`);
  } catch (error) {
    showError(error);
  }
}

// --- library -------------------------------------------------------------

async function loadLibrary() {
  if (!state.episode) return;
  const kind = els.libraryKind.value;
  try {
    setConnection(`加载 ${kind} 素材…`);
    const data = await request(`${ENDPOINTS.library(kind)}?page=1&page_size=24`);
    els.libraryGrid.innerHTML = "";
    const items = data.items || [];
    els.libraryEmpty.hidden = items.length > 0;
    for (const asset of items) {
      const card = document.createElement("div");
      card.className = "library-card";
      card.setAttribute("role", "listitem");
      const url = asset.media_path
        ? libraryMediaUrl(kind, asset.asset_id, asset.media_path)
        : null;
      if (url) {
        const image = document.createElement("img");
        image.loading = "lazy";
        image.alt = asset.asset_id;
        image.src = url;
        card.appendChild(image);
      }
      const id = document.createElement("span");
      id.className = "library-id";
      id.textContent = asset.asset_id;
      id.title = asset.text || asset.asset_id;
      card.appendChild(id);
      els.libraryGrid.appendChild(card);
    }
    setConnection(`素材库 ${kind}: ${items.length} 项`);
  } catch (error) {
    showError(error);
  }
}

// --- keyboard map (§11.3) ------------------------------------------------

function handleKeydown(event) {
  const target = event.target;
  if (target instanceof HTMLElement && target.matches("input, textarea, select")) return;
  if (els.helpDialog?.open) return;

  const key = event.key;
  const lower = key.toLowerCase();

  if (key === "Escape") {
    if (state.mode === "reject") {
      exitRejectMode();
      event.preventDefault();
    }
    return;
  }

  if (key === "Enter" && state.mode === "reject" && state.selectedReason) {
    submitDecision("reject");
    event.preventDefault();
    return;
  }

  if (["1", "2", "3"].includes(key)) {
    if (state.mode === "reject") selectReasonByDigit(Number(key));
    else selectCandidateByDigit(Number(key));
    event.preventDefault();
    return;
  }

  if (lower === "j") moveSelection(1);
  else if (lower === "k") moveSelection(-1);
  else if (lower === "a") safely(submitDecision("accept"));
  else if (lower === "r") enterRejectMode();
  else if (lower === "e") submitPromptPatch();
  else if (lower === "u") undoLast();
  else if (lower === "s") safely(submitDecision("skip"));
  else if (lower === "b") toggleFlag();
  else return;

  event.preventDefault();
}

// --- wiring --------------------------------------------------------------

function buildHelp() {
  els.helpRows.innerHTML = "";
  for (const [key, entry] of Object.entries(KEYBOARD_MAP)) {
    const tr = document.createElement("tr");
    const keyCell = document.createElement("td");
    keyCell.textContent = key;
    const actionCell = document.createElement("td");
    actionCell.textContent = entry.label || entry.action;
    tr.append(keyCell, actionCell);
    els.helpRows.appendChild(tr);
  }
}

function bindEvents() {
  els.episodeSelect.addEventListener("change", () => loadEpisode(els.episodeSelect.value).catch(showError));
  els.reload.addEventListener("click", () => loadEpisode(state.episode).catch(showError));

  for (const button of document.querySelectorAll("[data-action]")) {
    button.addEventListener("click", () => {
      safely(handleAction(button.dataset.action));
    });
  }

  els.confirmReject.addEventListener("click", () => safely(submitDecision("reject")));
  els.submitPrompt.addEventListener("click", submitPromptPatch);
  els.undoButton.addEventListener("click", undoLast);
  els.libraryRefresh.addEventListener("click", loadLibrary);

  for (const radio of document.querySelectorAll('input[name="sampling-mode"]')) {
    radio.addEventListener("change", schedulePolicySave);
  }
  els.ratioSlider.addEventListener("input", () => {
    els.ratioValue.textContent = `${Math.round(Number(els.ratioSlider.value) * 100)}%`;
    schedulePolicySave();
  });

  els.budgetInput.addEventListener("change", () => {
    const value = Number(els.budgetInput.value);
    if (Number.isFinite(value) && value >= 0) {
      state.budget = value;
      writeNumber(budgetKey(), value);
      renderCost();
    }
  });

  document.addEventListener("keydown", handleKeydown);
  els.helpToggle.addEventListener("click", () => els.helpDialog.showModal());
  els.helpDialog.addEventListener("close", () => els.helpToggle.focus());

  els.playerImage.addEventListener("error", () => {
    els.playerImage.hidden = true;
    els.playerEmpty.hidden = false;
    els.playerEmpty.textContent = "媒体 404 或缺候选 · 重试";
  });
  els.playerVideo.addEventListener("error", () => {
    els.playerVideo.hidden = true;
    els.playerEmpty.hidden = false;
    els.playerEmpty.textContent = "媒体 404 或缺候选 · 重试";
  });
}

async function handleAction(action) {
  switch (action) {
    case "reject":
      enterRejectMode();
      return;
    case "edit_prompt":
      submitPromptPatch();
      return;
    case "pick_variant":
      if (!state.selectedCandidate) {
        setConnection("先选择候选再选变体");
        return;
      }
      await submitDecision("pick_variant");
      return;
    case "send_back":
      await submitDecision("send_back");
      return;
    case "skip":
      await submitDecision("skip");
      return;
    default:
      await submitDecision(action);
  }
}

async function boot() {
  buildHelp();
  bindEvents();
  if (window.location.protocol === "file:") {
    showError(new ApiFailure({
      code: "SAME_ORIGIN_REQUIRED",
      what: "控制台必须从 API 同源加载，file:// 无法通过 CSRF 检查",
      fix: "把 web/review-console 挂到 API 同源路径后打开，或用同源反向代理",
      recovery: "fix-then-retry",
    }));
    return;
  }
  setConnection("连接 API…");
  try {
    const hasEpisodes = await loadEpisodes();
    if (!hasEpisodes) return;
    await loadEpisode(state.episode);
  } catch (error) {
    showError(error);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}