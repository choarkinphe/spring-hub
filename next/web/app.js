/* Cute Cat — HandBrake workbench (native ES2020, no framework, no build step). */
"use strict";

const API = "/api/v1";
const state = {
  caps: null,
  roots: [],
  currentRoot: null,
  currentPath: "",
  selectedFile: null,   // { root, path, name }
  scan: null,
  jobs: [],
  selectedJob: null,
  pollTimer: null,
  token: "",
  presetSelection: { source: "custom", preset: null },
  presetSource: "official",
  presets: null,
  presetsLoading: false,
  presetsError: "",
  encoderCatalog: null,
  encoderTarget: null,
  encoderKind: "video",
  encoderFilter: "all",
  encodersLoading: false,
  encodersError: "",
};

const JOB_STATUS_LABELS = {
  queued: "排队中",
  probing: "扫描中",
  running: "转码中",
  finalizing: "整理输出",
  succeeded: "已完成",
  failed: "失败",
  canceled: "已取消",
  interrupted: "已中断",
};

function showWorkspace(name) {
  document.querySelectorAll("[data-page]").forEach((page) => {
    page.hidden = page.dataset.page !== name;
  });
  document.querySelectorAll("[data-workspace]").forEach((button) => {
    const active = button.dataset.workspace === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (name === "queue") refreshJobs();
}

function bindWorkspaceNavigation() {
  document.querySelectorAll("[data-workspace], [data-open-workspace]").forEach((button) => {
    button.addEventListener("click", () => showWorkspace(button.dataset.workspace || button.dataset.openWorkspace));
  });
}


const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
};
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  const res = await fetch(API + path, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) {
    const message = (data && (data.error || data.detail)) || res.statusText;
    throw new Error(`${res.status}: ${message}`);
  }
  return data;
}

function humanSize(bytes) {
  if (bytes == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function fillSelect(select, items, { valueKey = "value", labelKey = "label", includeEmpty = false, emptyLabel = "—" } = {}) {
  select.innerHTML = "";
  if (includeEmpty) select.appendChild(el("option", { value: "", text: emptyLabel }));
  for (const item of items) {
    const value = typeof item === "string" ? item : item[valueKey];
    const label = typeof item === "string" ? item : item[labelKey];
    select.appendChild(el("option", { value, text: label }));
  }
}

const CHOICE_LABELS = {
  auto: "自动", off: "关闭", none: "无", default: "默认",
  av_mp4: "MP4（av_mp4）", av_mkv: "MKV（av_mkv）", mp4: "MP4", mkv: "MKV", webm: "WebM",
  rf: "恒定质量（RF）", crf: "恒定质量（CRF）", cqp: "恒定量化（CQP）",
  constant: "恒定质量（constant）", abr: "平均码率（ABR）", vbr: "可变码率（VBR）", lossless: "无损",
  fast: "快速", slow: "慢速", slower: "更慢", bob: "Bob（双倍帧率）",
  add: "添加字幕", "add-first": "添加首条", burn: "烧录", foreign: "外语搜索",
  mono: "单声道", stereo: "立体声", dpl1: "杜比环绕", dpl2: "杜比定向逻辑 II",
  "5point1": "5.1 声道", "6point1": "6.1 声道", "7point1": "7.1 声道",
};
let choiceSequence = 0;

function choiceValue(control) {
  if (control.classList.contains("encoder-control")) return control.dataset.encoderValue || "";
  const select = control.querySelector("select");
  if (select) return select.value;
  return control.querySelector("input:checked")?.value ?? "";
}

function setChoiceValue(control, value) {
  if (control.classList.contains("encoder-control")) {
    control.dataset.encoderValue = String(value);
    renderEncoderControl(control);
    return;
  }
  const select = control.querySelector("select");
  if (select) select.value = String(value);
  else control.querySelectorAll("input[type=radio]").forEach((input) => {
    input.checked = input.value === String(value);
  });
}

function renderChoice(control, items, { value, labels = CHOICE_LABELS, tags = false } = {}) {
  const previous = value !== undefined ? String(value) : choiceValue(control);
  const options = items.map((item) => typeof item === "string"
    ? { value: item, label: labels[item] || item }
    : { value: String(item.value), label: item.label });
  const selected = options.some((option) => option.value === previous) ? previous : options[0]?.value;
  const groupName = control.dataset.choiceName || `choice-${++choiceSequence}`;
  control.dataset.choiceName = groupName;
  control.replaceChildren();
  control.classList.remove("choice-empty", "choice-dropdown", "choice-tags", "choice-segment");
  control.classList.add("choice-control");
  if (!options.length) {
    control.classList.add("choice-empty");
    control.appendChild(el("span", { text: "无可选项" }));
    return;
  }
  if (options.length > 8) {
    control.classList.add("choice-dropdown");
    const select = el("select", { "aria-labelledby": control.dataset.labelId });
    fillSelect(select, options);
    select.value = selected;
    control.appendChild(select);
    return;
  }
  control.classList.add(tags || options.length > 4 ? "choice-tags" : "choice-segment");
  options.forEach((option, index) => {
    const input = el("input", { type: "radio", name: groupName, id: `${groupName}-${index}`, value: option.value });
    input.checked = option.value === selected;
    control.appendChild(el("label", { class: "choice-option", for: input.id }, [
      input, el("span", { text: option.label }),
    ]));
  });
}

function choiceField(label, control) {
  const labelId = `choice-label-${++choiceSequence}`;
  control.dataset.labelId = labelId;
  return el("fieldset", { class: "field choice-field" }, [
    el("legend", { id: labelId, text: label }), control,
  ]);
}

function initializeChoices() {
  document.querySelectorAll("select").forEach((select) => {
    const label = select.closest("label.field");
    const control = el("div", { id: select.id });
    const field = choiceField(label.querySelector("span").textContent, control);
    const items = [...select.options].map((option) => ({ value: option.value, label: option.textContent }));
    label.replaceWith(field);
    renderChoice(control, items, { value: select.value, tags: select.id.endsWith("root-select") });
  });
}

function dictToOptions(dict) {
  return Object.entries(dict).map(([value, label]) => ({ value, label: `${label} (${value})` }));
}

/* ---------- bootstrap ---------- */
async function boot() {
  try {
    const caps = await api("/capabilities");
    state.caps = caps;
    state.encoderCatalog = caps.encoder_catalog;
    $("version-tag").textContent = "v" + caps.version;
    renderEngineStatus(caps.engine);
    renderSpecOptions(caps.spec_options);
    renderFeatureMatrix(caps.features);
    await loadRoots();
    await refreshJobs();
    showWorkspace("encode");
    startPolling();
  } catch (err) {
    showBanner("error", "无法连接后端：" + err.message);
  }
}

function renderEngineStatus(engine) {
  const pill = $("engine-status");
  if (engine.available) {
    pill.className = "pill pill-ok";
    pill.textContent = `HandBrake ${engine.version || "?"}`;
    pill.title = engine.version_string || "";
    if (engine.notes && engine.notes.length) showBanner("warn", engine.notes.join(" "));
  } else {
    pill.className = "pill pill-bad";
    pill.textContent = "未检测到 HandBrakeCLI";
    pill.title = (engine.notes || []).join(" ");
    showBanner("error",
      "未找到 HandBrakeCLI，转码功能已禁用。请安装引擎或设置 engine.handbrake_bin。" +
      (engine.notes && engine.notes.length ? " (" + engine.notes.join(" ") + ")" : ""));
    $("btn-queue").disabled = true;
  }
}

function showBanner(kind, message) {
  const banner = $("engine-banner");
  banner.className = `banner banner-${kind === "error" ? "error" : "warn"}`;
  banner.textContent = message;
  banner.classList.remove("hidden");
}

function renderSpecOptions(opts) {
  renderChoice($("container-select"), opts.containers, { value: "auto" });

  initializeEncoderControl($("video-encoder"), "video", "x264");

  renderChoice($("video-quality-type"), opts.video_quality_types, { value: "rf" });
  renderChoice($("video-preset"), [{ value: "", label: "默认" }, ...opts.video_presets.map((p) => ({ value: p, label: p }))], { value: "medium" });
  renderChoice($("video-tune"), [{ value: "", label: "默认（无调优）" }, ...opts.video_tunes.map((t) => ({ value: t, label: t === "none" ? "无（none）" : t }))]);
  renderChoice($("video-profile"), [{ value: "", label: "默认（自动）" }, ...opts.video_profiles.map((p) => ({ value: p, label: p === "auto" ? "自动（auto）" : p }))]);
  renderChoice($("video-level"), [{ value: "", label: "默认（自动）" }, ...opts.video_levels.map((l) => ({ value: l, label: l === "auto" ? "自动（auto）" : l }))]);
  renderChoice($("video-framerate"), opts.framerates, { value: "auto" });

  renderChoice($("sub-behavior"), opts.subtitle_behaviors, {
    value: "none", labels: { ...CHOICE_LABELS, none: "不添加", default: "默认字幕" },
  });

  if (!state.audioOptions) {
    state.audioOptions = opts;
    addAudioTrack();
  }
}

/* ---------- storage roots & browser ---------- */
async function loadRoots() {
  const data = await api("/storage-roots");
  state.roots = data.roots;
  const options = data.roots.map((r) => ({ value: r.id, label: r.label + (r.read_only ? " (只读)" : "") }));
  renderChoice($("root-select"), options, { tags: true });
  renderChoice($("output-root-select"), options, { tags: true });
  const writable = data.roots.find((r) => !r.read_only);
  if (writable) setChoiceValue($("output-root-select"), writable.id);
  if (data.roots.length) {
    state.currentRoot = data.roots[0].id;
    await browse("");
  }
}

async function browse(path) {
  const root = choiceValue($("root-select")) || state.currentRoot;
  state.currentRoot = root;
  state.currentPath = path;
  try {
    const data = await api(`/storage-roots/${encodeURIComponent(root)}/entries?path=${encodeURIComponent(path)}`);
    $("current-path").textContent = "/" + (data.path || "");
    const browser = $("browser");
    browser.innerHTML = "";
    for (const entry of data.entries) {
      const node = el("div", {
        class: "entry" + (entry.is_dir ? " dir" : ""),
        onclick: () => {
          if (entry.is_dir) {
            const next = (data.path ? data.path + "/" : "") + entry.name;
            browse(next);
          } else {
            selectFile(root, (data.path ? data.path + "/" : "") + entry.name, entry.name);
          }
        },
      }, [
        el("span", { text: (entry.is_dir ? "📁 " : "🎬 ") + entry.name }),
        el("span", { class: "size", text: entry.is_dir ? "" : humanSize(entry.size) }),
      ]);
      browser.appendChild(node);
    }
  } catch (err) {
    showBanner("warn", "读取目录失败：" + err.message);
  }
}

function selectFile(root, path, name) {
  state.selectedFile = { root, path, name };
  state.scan = null;
  $("source-summary").innerHTML = "";
  $("source-summary").appendChild(el("div", { class: "meta-grid" }, [
    el("div", {}, [el("b", { text: "文件" }), document.createTextNode(name)]),
    el("div", {}, [el("b", { text: "根" }), document.createTextNode(root)]),
    el("div", {}, [el("b", { text: "路径" }), document.createTextNode(path)]),
  ]));
  $("btn-scan").disabled = false;
  $("btn-queue").disabled = !(state.caps && state.caps.engine.available);
  if (!$("output-name").value) {
    const base = name.replace(/\.[^.]+$/, "");
    const ext = (choiceValue($("container-select")) === "mkv") ? "mkv" : "mp4";
    $("output-name").value = `converted/${base}.${ext}`;
  }
}

/* ---------- scan ---------- */
async function scanSource() {
  if (!state.selectedFile) return;
  $("btn-scan").disabled = true;
  $("btn-scan").textContent = "扫描中…";
  try {
    const data = await api("/probe", {
      method: "POST",
      body: JSON.stringify({ root: state.selectedFile.root, path: state.selectedFile.path }),
    });
    state.scan = data.scan;
    renderScan(data.scan);
  } catch (err) {
    showBanner("warn", "扫描失败：" + err.message);
  } finally {
    $("btn-scan").disabled = false;
    $("btn-scan").textContent = "扫描源文件";
  }
}

function renderScan(scan) {
  const titles = scan.titles || [];
  const box = $("source-summary");
  box.innerHTML = "";
  const grid = el("div", { class: "meta-grid" });
  grid.appendChild(el("div", {}, [el("b", { text: "标题数" }), document.createTextNode(String(titles.length))]));
  if (scan.engine_version) grid.appendChild(el("div", {}, [el("b", { text: "引擎" }), document.createTextNode(scan.engine_version)]));
  const first = titles[0] || {};
  if (first.Format) grid.appendChild(el("div", {}, [el("b", { text: "格式" }), document.createTextNode(String(first.Format))]));
  if (first["VideoCodec"]) grid.appendChild(el("div", {}, [el("b", { text: "视频" }), document.createTextNode(String(first["VideoCodec"]))]));
  box.appendChild(grid);
  if (titles.length) {
    const list = el("div", { class: "hint" });
    list.textContent = "可用标题：" + titles.map((t, i) => `${i + 1}${t.Name ? " (" + t.Name + ")" : ""}`).join(", ");
    box.appendChild(list);
  }
}

/* ---------- presets ---------- */
const PRESET_GROUP_LABELS = {
  General: "通用", Web: "网络发布", Devices: "设备", Matroska: "Matroska",
  Hardware: "硬件加速", Professional: "专业制作", "CLI Defaults": "CLI 默认",
};
const PRESET_SOURCE_LABELS = { official: "官方预设", imported: "导入预设", custom: "自定义" };

function presetCategory(preset) {
  return preset.category || (preset.name.includes("/") ? preset.name.slice(0, preset.name.lastIndexOf("/")) : "未分类");
}

function presetGroupLabel(category) {
  return PRESET_GROUP_LABELS[category] || category;
}

function updatePresetSummary() {
  const { source, preset } = state.presetSelection;
  $("preset-current-source").textContent = PRESET_SOURCE_LABELS[source] + (preset ? ` · ${presetGroupLabel(presetCategory(preset))}` : "");
  $("preset-current-name").textContent = preset ? preset.name : "自定义参数";
  $("preset-hint").textContent = preset ? (preset.description || "已选择预设；下方参数仍按现有规则作为覆盖设置。") : "使用下方各标签中的参数。";
}

function selectPreset(source, preset) {
  state.presetSelection = { source, preset };
  updatePresetSummary();
  $("preset-drawer").close();
}

function setPresetSource(source) {
  state.presetSource = source;
  document.querySelectorAll("[data-preset-source]").forEach((button) => {
    const active = button.dataset.presetSource === source;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderPresets();
  if (source !== "custom" && !state.presets && !state.presetsLoading) loadPresets();
}

function openPresetDrawer() {
  $("preset-search").value = "";
  setPresetSource(state.presetSelection.source === "custom" ? "official" : state.presetSelection.source);
  $("preset-drawer").showModal();
  document.body.classList.add("preset-drawer-open");
  $("preset-search").focus();
}

async function loadPresets() {
  if (state.presetsLoading) return;
  state.presetsLoading = true;
  state.presetsError = "";
  renderPresets();
  try {
    state.presets = await api("/presets");
  } catch (err) {
    state.presetsError = "加载预设失败：" + err.message;
  } finally {
    state.presetsLoading = false;
    renderPresets();
  }
}

function renderPresets() {
  const results = $("preset-results");
  const nav = $("preset-group-nav");
  const count = $("preset-result-count");
  results.replaceChildren();
  nav.replaceChildren();
  results.scrollTop = 0;
  const source = state.presetSource;
  results.setAttribute("aria-busy", String(source !== "custom" && state.presetsLoading));
  const message = (text) => results.appendChild(el("p", { class: "preset-empty", text }));
  if (source !== "custom" && state.presetsLoading) {
    message("正在读取引擎预设…"); count.textContent = "加载中…"; return;
  }
  if (source !== "custom" && state.presetsError) {
    message(state.presetsError);
    results.appendChild(el("button", { class: "btn", type: "button", text: "重新加载", onclick: loadPresets }));
    count.textContent = "加载失败"; return;
  }
  const all = source === "custom" ? [{ name: "自定义参数", category: "自定义", description: "使用尺寸、滤镜、视频、音频、字幕与章节标签中的参数。" }] : (state.presets?.[source] || []);
  if (!all.length) {
    message(source === "official" ? (state.presets?.engine_note || "引擎未报告任何官方预设。") : "尚无导入预设。可在存储根放入预设 JSON 并导入。");
    count.textContent = "0 个预设"; return;
  }
  const query = $("preset-search").value.trim().toLocaleLowerCase();
  const filtered = all.filter((preset) => [preset.name, presetCategory(preset), presetGroupLabel(presetCategory(preset)), preset.description || ""].join(" ").toLocaleLowerCase().includes(query));
  count.textContent = `${filtered.length} / ${all.length} 个预设`;
  if (!filtered.length) { message("没有匹配的预设，请调整关键词或清空搜索。"); return; }
  const groups = new Map();
  for (const preset of filtered) {
    const category = presetCategory(preset);
    if (!groups.has(category)) groups.set(category, []);
    groups.get(category).push(preset);
  }
  for (const [category, presets] of groups) {
    const label = presetGroupLabel(category);
    const section = el("section", { class: "preset-group", "aria-label": label });
    const heading = el("h3", { text: label }, [el("span", { class: "preset-group-count", text: String(presets.length) })]);
    section.appendChild(heading);
    if (category === "Hardware") section.appendChild(el("p", { class: "preset-hardware-note", text: "需相应硬件与驱动支持；列出预设不代表当前设备可用。" }));
    const grid = el("div", { class: "preset-card-grid" });
    for (const preset of presets) {
      const selected = state.presetSelection.source === source && (source === "custom" || state.presetSelection.preset?.name === preset.name);
      const card = el("button", {
        class: "preset-card" + (selected ? " selected" : ""), type: "button",
        "aria-pressed": String(selected), "data-preset-name": preset.name,
        onclick: () => selectPreset(source, source === "custom" ? null : preset),
      });
      card.appendChild(el("span", { class: "preset-card-top" }, [
        el("strong", { text: preset.name.split("/").pop() }),
        el("span", { class: "preset-card-check", text: selected ? "已选用 ✓" : "选择" }),
      ]));
      card.appendChild(el("span", { class: "preset-card-description", text: preset.description || "此预设暂无说明。" }));
      if (preset.file) card.appendChild(el("span", { class: "preset-card-file", text: `文件：${preset.file}` }));
      grid.appendChild(card);
    }
    section.appendChild(grid);
    results.appendChild(section);
    nav.appendChild(el("button", {
      class: "preset-group-link", type: "button", text: `${label} ${presets.length}`,
      onclick: () => { results.scrollTop = section.offsetTop - results.offsetTop; },
    }));
  }
}

/* ---------- encoder drawer ---------- */
const ENCODER_STATUS_LABELS = {
  available: "引擎支持", unverified: "GPU 转码未实测",
  no_hardware: "缺少硬件支持", not_installed: "当前引擎不含",
  passthrough_only: "仅支持直通", unavailable: "当前不可用",
  unsupported: "当前引擎未提供", missing: "未检测到引擎",
  unknown: "检测结果未知",
};
const ENCODER_DEVICE_LABELS = { cpu: "CPU", gpu: "GPU / 专用硬件", passthrough: "直通 / 自动 / 不编码" };

function encoderInstalledLabel(encoder) {
  if (encoder.status === "no_hardware") return "不适用（缺少对应硬件）";
  if (encoder.status === "not_installed") return "当前引擎不含（换引擎构建即可）";
  if (encoder.status === "passthrough_only") return "不适用（无此编码器）";
  if (encoder.installed === true) return "已内置于当前 HandBrake";
  return encoder.installed === false ? "当前未提供" : "无法确认";
}

/* Copy-to-clipboard for the host-side commands shown on "not installed" cards.
   The page never runs these; the operator copies them and runs them on the
   host, which is why the whole acquisition block is guidance text only. */
async function copyCommand(text, button) {
  let ok = false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch { ok = false; }
  if (!ok) {
    const scratch = el("textarea", { class: "copy-scratch", readonly: "readonly" });
    scratch.value = text;
    document.body.appendChild(scratch);
    scratch.select();
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    scratch.remove();
  }
  const original = button.textContent;
  button.textContent = ok ? "已复制 ✓" : "请手动复制";
  button.disabled = true;
  setTimeout(() => {
    if (button.isConnected) { button.textContent = original; button.disabled = false; }
  }, 1600);
}

function renderEncoderAcquisition(acquisition) {
  const block = el("div", { class: "encoder-acquisition" });
  block.appendChild(el("p", { class: "encoder-acquisition-headline", text: acquisition.headline }));
  if (acquisition.note) block.appendChild(el("p", { class: "encoder-acquisition-note", text: acquisition.note }));
  const list = el("ol", { class: "encoder-acquisition-options" });
  for (const option of acquisition.options || []) {
    const item = el("li", {}, [
      el("span", { class: "encoder-acquisition-label", text: option.label }),
      el("span", { class: "encoder-acquisition-detail", text: option.detail }),
    ]);
    if (option.command) {
      const copy = el("button", {
        type: "button", class: "btn btn-ghost btn-small", text: "复制命令",
        "aria-label": `复制命令：${option.command}`,
        onclick: () => copyCommand(option.command, copy),
      });
      item.appendChild(el("span", { class: "encoder-acquisition-command" }, [
        el("code", { text: option.command }), copy,
      ]));
    }
    list.appendChild(item);
  }
  if (list.children.length) block.appendChild(list);
  if (acquisition.reference) block.appendChild(el("p", { class: "encoder-acquisition-ref", text: `参考：${acquisition.reference}` }));
  return block;
}

function initializeEncoderControl(control, kind, value) {
  control.classList.remove("choice-empty", "choice-dropdown", "choice-tags", "choice-segment");
  control.classList.add("encoder-control");
  control.dataset.encoderKind = kind;
  control.dataset.encoderValue = value;
  renderEncoderControl(control);
}

function renderEncoderControl(control) {
  const encoder = state.encoderCatalog?.[control.dataset.encoderKind]?.find((item) => item.id === choiceValue(control));
  control.replaceChildren();
  const button = el("button", {
    type: "button", class: "encoder-trigger", "aria-haspopup": "dialog", "aria-controls": "encoder-drawer",
    onclick: () => openEncoderDrawer(control),
  }, [
    el("span", { class: "encoder-trigger-name", text: encoder?.name || choiceValue(control) || "尚未选择编码器" }),
    el("span", { class: "encoder-trigger-status", text: encoder ? `${ENCODER_DEVICE_LABELS[encoder.device]} · ${ENCODER_STATUS_LABELS[encoder.status]}` : "检测结果未知" }),
    el("span", { class: "encoder-trigger-action", text: "选择编码器" }),
  ]);
  control.appendChild(button);
}

function openEncoderDrawer(control) {
  state.encoderTarget = control;
  state.encoderKind = control.dataset.encoderKind;
  state.encoderFilter = "all";
  $("encoder-search").value = "";
  $("encoder-drawer-title").textContent = state.encoderKind === "video" ? "选择视频编码器" : "选择音频编码器";
  renderEncoderCards();
  $("encoder-drawer").showModal();
  document.body.classList.add("encoder-drawer-open");
  $("encoder-search").focus();
  if (!state.encoderCatalog) refreshEncoders();
}

function restoreEncoderFocus() {
  document.body.classList.remove("encoder-drawer-open");
  if (state.encoderTarget?.isConnected) state.encoderTarget.querySelector("button").focus();
}

function closeEncoderDrawer() {
  $("encoder-drawer").close();
  restoreEncoderFocus();
}

function selectEncoder(encoder) {
  if (!encoder.selectable || state.encodersLoading || state.encodersError || !state.encoderTarget?.isConnected) return;
  setChoiceValue(state.encoderTarget, encoder.id);
  state.encoderTarget.dispatchEvent(new Event("change", { bubbles: true }));
  closeEncoderDrawer();
}

async function refreshEncoders() {
  if (state.encodersLoading) return;
  state.encodersLoading = true;
  state.encodersError = "";
  renderEncoderCards();
  try {
    // refresh=1 forces a real re-probe instead of the cached result.
    const data = await api("/encoders?refresh=1");
    state.encoderCatalog = data.encoder_catalog;
    state.caps.engine = data.engine;
    document.querySelectorAll(".encoder-control").forEach(renderEncoderControl);
  } catch (err) {
    state.encodersError = "重新检测失败：" + err.message + "。下方保留上次结果，不代表当前状态。";
  } finally {
    state.encodersLoading = false;
    renderEncoderCards();
  }
}

function renderEncoderCards() {
  const results = $("encoder-results");
  const filters = $("encoder-filters");
  results.replaceChildren();
  filters.replaceChildren();
  results.setAttribute("aria-busy", String(state.encodersLoading));
  $("btn-refresh-encoders").disabled = state.encodersLoading;
  $("encoder-message").textContent = state.encodersLoading ? "正在读取当前 HandBrake 的真实编码器清单…" : state.encodersError;
  const all = state.encoderCatalog?.[state.encoderKind] || [];
  const matchesFilter = (item, key) => key === "all" || item.device === key ||
    (key === "no_hardware" ? item.status === "no_hardware" : key === "not_installed" && item.status === "not_installed");
  for (const [key, label] of Object.entries({
    all: "全部", cpu: "CPU 软件", gpu: "GPU 硬件", passthrough: "直通与自动",
    no_hardware: "缺少硬件支持", not_installed: "当前引擎不含",
  })) {
    if (key !== "all" && !all.some((item) => matchesFilter(item, key))) continue;
    filters.appendChild(el("button", {
      type: "button", class: "encoder-filter" + (state.encoderFilter === key ? " active" : ""),
      text: label, "aria-pressed": String(state.encoderFilter === key),
      onclick: () => { state.encoderFilter = key; renderEncoderCards(); },
    }));
  }
  const query = $("encoder-search").value.trim().toLocaleLowerCase();
  const filtered = all.filter((item) => matchesFilter(item, state.encoderFilter) &&
    [item.id, item.name, item.description, item.backend, ENCODER_DEVICE_LABELS[item.device], ENCODER_STATUS_LABELS[item.status],
      encoderInstalledLabel(item), item.acquisition?.headline, ...(item.acquisition?.options || []).map((option) => option.label)]
      .filter(Boolean).join(" ").toLocaleLowerCase().includes(query));
  $("encoder-result-count").textContent = `${filtered.length} / ${all.length} 个编码器`;
  if (!filtered.length) {
    results.appendChild(el("p", { class: "preset-empty", text: all.length ? "没有匹配的编码器，请调整搜索或分类。" : "尚无编码器检测结果，请点击重新检测。" }));
    return;
  }
  const selectedValue = state.encoderTarget ? choiceValue(state.encoderTarget) : null;
  for (const device of ["cpu", "gpu", "passthrough"]) {
    const items = filtered.filter((item) => item.device === device);
    if (!items.length) continue;
    const section = el("section", { class: "preset-group", "aria-label": ENCODER_DEVICE_LABELS[device] }, [el("h3", { text: ENCODER_DEVICE_LABELS[device] })]);
    const grid = el("div", { class: "preset-card-grid" });
    for (const encoder of items) {
      const selected = selectedValue === encoder.id;
      const card = el("article", { class: "encoder-card" + (selected ? " selected" : ""), "data-encoder-id": encoder.id });
      const tags = [el("span", { class: "encoder-device", text: ENCODER_DEVICE_LABELS[encoder.device] })];
      const badges = { no_hardware: ["nohw", "缺少硬件支持"], not_installed: ["notinst", "引擎不含"] };
      const badge = badges[encoder.status];
      if (badge) tags.unshift(el("span", { class: `encoder-badge encoder-badge-${badge[0]}`, text: badge[1] }));
      const heading = el("div", { class: "encoder-card-heading" }, [
        el("strong", { text: encoder.name }),
        el("div", { class: "encoder-card-tags" }, tags),
      ]);
      card.appendChild(heading);
      card.appendChild(el("p", { class: "encoder-description", text: encoder.description }));
      card.appendChild(el("dl", { class: "encoder-facts" }, [
        el("dt", { text: encoder.status === "no_hardware" ? "安装情况" : "安装 / 引擎提供" }), el("dd", { text: encoderInstalledLabel(encoder) }),
        el("dt", { text: "运行状态" }), el("dd", { class: `encoder-status status-${encoder.status}`, text: ENCODER_STATUS_LABELS[encoder.status] }),
        el("dt", { text: "处理方式" }), el("dd", { text: encoder.backend }),
        el("dt", { text: "引擎标识" }), el("dd", { text: encoder.cli_name || encoder.id }),
      ]));
      card.appendChild(el("p", { class: "encoder-reason", text: encoder.reason }));
      if (encoder.acquisition) card.appendChild(renderEncoderAcquisition(encoder.acquisition));
      const button = el("button", {
        type: "button", class: "btn" + (selected ? " btn-primary" : ""), "aria-pressed": String(selected),
        text: selected ? "已选用 ✓" : encoder.selectable ? "选用此编码器" : "不可选用",
        onclick: () => selectEncoder(encoder),
      });
      button.disabled = !encoder.selectable || state.encodersLoading || Boolean(state.encodersError);
      card.appendChild(button);
      grid.appendChild(card);
    }
    section.appendChild(grid);
    results.appendChild(section);
  }
}

/* ---------- audio tracks ---------- */
function addAudioTrack() {
  const opts = state.audioOptions || { audio_encoders: ["aac"], audio_mixdowns: ["auto"] };
  const container = $("audio-tracks");
  const index = container.children.length + 1;
  const track = el("div", { class: "audio-track" }, [el("h4", { text: `音轨 ${index}` })]);
  const encoder = el("div", { class: "a-encoder" });
  const mixdown = el("div", { class: "a-mixdown" });
  const bitrate = el("div", { class: "a-bitrate" });
  const source = el("input", { class: "a-source", type: "number", min: "1", placeholder: "自动" });

  track.appendChild(el("div", { class: "grid-2" }, [
    choiceField("编码器", encoder), choiceField("混音", mixdown),
  ]));
  track.appendChild(el("div", { class: "grid-2" }, [
    choiceField("码率（kbps）", bitrate),
    el("label", { class: "field" }, [el("span", { text: "源音轨" }), source]),
  ]));
  initializeEncoderControl(encoder, "audio", "aac");
  renderChoice(mixdown, opts.audio_mixdowns);
  renderChoice(bitrate, ["auto", "96", "128", "160", "192", "256", "320"]);
  track.appendChild(el("button", {
    class: "btn btn-ghost btn-small", type: "button", text: "移除",
    onclick: () => { track.remove(); renumberAudio(); },
  }));
  container.appendChild(track);
}

function renumberAudio() {
  [...$("audio-tracks").children].forEach((node, i) => {
    node.querySelector("h4").textContent = `音轨 ${i + 1}`;
  });
}

/* ---------- build spec ---------- */
function numberOrNull(id) {
  const control = $(id);
  const raw = (control.classList.contains("choice-control") ? choiceValue(control) : control.value).trim();
  if (raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

function buildSpec() {
  const { source: presetSource, preset } = state.presetSelection;
  const spec = {
    version: 1,
    container: choiceValue($("container-select")),
    title: 1,
    dimensions: {
      width: numberOrNull("dim-width"),
      height: numberOrNull("dim-height"),
      crop_mode: choiceValue($("dim-crop-mode")),
      crop_top: numberOrNull("crop-top"),
      crop_bottom: numberOrNull("crop-bottom"),
      crop_left: numberOrNull("crop-left"),
      crop_right: numberOrNull("crop-right"),
      anamorphic: choiceValue($("dim-anamorphic")),
      modulus: numberOrNull("dim-modulus"),
    },
    filters: {
      deinterlace: choiceValue($("flt-deinterlace")),
      denoise: choiceValue($("flt-denoise")),
      detelecine: choiceValue($("flt-detelecine")),
      rotate: choiceValue($("flt-rotate")),
      grayscale: $("flt-grayscale").checked,
      hflip: $("flt-hflip").checked,
      chroma_smooth: $("flt-chroma").checked,
      lapsharp: $("flt-lapsharp").checked,
      unsharp: $("flt-unsharp").checked,
    },
    video: {
      encoder: choiceValue($("video-encoder")),
      quality_type: choiceValue($("video-quality-type")),
      quality: numberOrNull("video-quality"),
      bitrate_kbps: numberOrNull("video-bitrate"),
      preset: choiceValue($("video-preset")) || null,
      tune: choiceValue($("video-tune")) || null,
      profile: choiceValue($("video-profile")) || null,
      level: choiceValue($("video-level")) || null,
      framerate: choiceValue($("video-framerate")),
      two_pass: $("video-two-pass").checked,
      turbo: $("video-turbo").checked,
    },
    audio: {
      tracks: [...$("audio-tracks").children].map((node) => ({
        encoder: choiceValue(node.querySelector(".a-encoder")),
        mixdown: choiceValue(node.querySelector(".a-mixdown")),
        bitrate: choiceValue(node.querySelector(".a-bitrate")),
        source: node.querySelector(".a-source").value || "auto",
      })),
    },
    subtitles: {
      behavior: choiceValue($("sub-behavior")),
      burn_track: numberOrNull("sub-burn"),
      default_track: numberOrNull("sub-default"),
      forced_only: $("sub-forced").checked,
      srt_file: $("sub-srt").value.trim() || null,
    },
    chapters: {
      mode: choiceValue($("chap-mode")),
      marker_file: $("chap-file").value.trim() || null,
    },
  };
  if (presetSource !== "custom" && preset) {
    spec.preset = preset.name;
  }
  // Strip null/empty to keep the payload tight; the server also accepts nulls.
  for (const section of ["dimensions", "filters", "video", "subtitles", "chapters"]) {
    for (const [key, value] of Object.entries(spec[section])) {
      if (value === null || value === "") delete spec[section][key];
    }
  }
  return spec;
}

/* ---------- jobs ---------- */
async function queueJob() {
  if (!state.selectedFile) return;
  const outputRoot = choiceValue($("output-root-select"));
  const outputName = $("output-name").value.trim();
  if (!outputName) { showBanner("warn", "请填写输出文件名。"); return; }
  $("btn-queue").disabled = true;
  try {
    const spec = buildSpec();
    const job = await api("/jobs", {
      method: "POST",
      body: JSON.stringify({
        input: { root: state.selectedFile.root, path: state.selectedFile.path },
        output: { root: outputRoot, path: outputName },
        preset_id: spec.preset || "custom",
        spec,
      }),
    });
    state.selectedJob = job.id;
    await refreshJobs();
    await loadJobDetail(job.id);
  } catch (err) {
    showBanner("warn", "加入队列失败：" + err.message);
  } finally {
    $("btn-queue").disabled = !(state.caps && state.caps.engine.available);
  }
}

async function refreshJobs() {
  try {
    const data = await api("/jobs");
    state.jobs = data.jobs;
    const list = $("job-list");
    list.innerHTML = "";
    $("queue-count").textContent = String(data.jobs.length);
    for (const job of data.jobs) {
      const item = el("li", {
        class: job.id === state.selectedJob ? "active" : "",
        onclick: () => { state.selectedJob = job.id; loadJobDetail(job.id); refreshJobs(); },
      }, [
        el("div", { class: "job-title" }, [
          el("span", { text: job.preset_name || job.preset_id }),
          el("span", { class: `status-tag status-${job.status}`, text: JOB_STATUS_LABELS[job.status] || job.status }),
        ]),
        el("div", { class: "job-path", text: `${job.input.path} → ${job.output.path}` }),
      ]);
      list.appendChild(item);
    }
    const active = data.jobs.filter((j) => ["queued", "probing", "running", "finalizing"].includes(j.status)).length;
    const queuePill = $("queue-status");
    if (active) { queuePill.className = "pill pill-running"; queuePill.textContent = `队列 ${active} 个进行中`; }
    else { queuePill.className = "pill pill-idle"; queuePill.textContent = "队列空闲"; }
  } catch (err) { /* transient */ }
}

async function loadJobDetail(id) {
  try {
    const job = await api(`/jobs/${id}`);
    renderJobDetail(job);
    const logs = await api(`/jobs/${id}/logs`);
    $("job-log").textContent = logs.logs.map((l) => `[${l.ts}] ${l.level.toUpperCase()} ${l.message}`).join("\n") || "—";
  } catch (err) { /* transient */ }
}

function renderJobDetail(job) {
  const box = $("job-detail");
  box.innerHTML = "";
  const grid = el("div", { class: "meta-grid" });
  const rows = [
    ["状态", job.status],
    ["预设", job.preset_name || job.preset_id],
    ["容器", job.container || "auto"],
    ["输入", job.input.path],
    ["输出", job.output.path],
    ["标题数", job.title_count != null ? String(job.title_count) : "—"],
    ["开始", job.started_at || "—"],
    ["结束", job.finished_at || "—"],
  ];
  if (job.error) rows.push(["错误", job.error]);
  for (const [label, value] of rows) {
    grid.appendChild(el("div", {}, [el("b", { text: label }), document.createTextNode(String(value))]));
  }
  box.appendChild(grid);
  const pct = Math.round((job.progress || 0) * 100);
  $("progress-bar").style.width = pct + "%";
  const speed = job.speed ? ` · ${job.speed} fps` : "";
  const eta = job.eta_seconds ? ` · ETA ${job.eta_seconds}s` : "";
  $("progress-label").textContent = `${pct}%${speed}${eta}`;
  $("btn-cancel").disabled = ["queued", "probing", "running", "finalizing"].includes(job.status) ? false : true;
  $("btn-cancel").dataset.jobId = job.id;
}

async function cancelJob() {
  const id = $("btn-cancel").dataset.jobId;
  if (!id) return;
  try {
    await api(`/jobs/${id}/cancel`, { method: "POST", body: "{}" });
    await loadJobDetail(id);
    await refreshJobs();
  } catch (err) { showBanner("warn", "取消失败：" + err.message); }
}

/* ---------- feature matrix ---------- */
function renderFeatureMatrix(features) {
  const body = $("matrix-table").querySelector("tbody");
  body.innerHTML = "";
  for (const f of features) {
    body.appendChild(el("tr", {}, [
      el("td", { text: f.area }),
      el("td", { class: `feat-${f.status}`, text: f.status }),
      el("td", { text: f.note }),
    ]));
  }
}

/* ---------- polling ---------- */
function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    await refreshJobs();
    if (state.selectedJob) await loadJobDetail(state.selectedJob);
  }, 2000);
}

/* ---------- events ---------- */
function wireEvents() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      document.querySelector(`.tab-panel[data-panel="${tab.dataset.tab}"]`).classList.add("active");
    });
  });
  bindWorkspaceNavigation();

  $("root-select").addEventListener("change", () => browse(""));
  $("btn-up").addEventListener("click", () => {
    const parts = state.currentPath.split("/").filter(Boolean);
    parts.pop();
    browse(parts.join("/"));
  });
  $("btn-scan").addEventListener("click", scanSource);
  $("btn-queue").addEventListener("click", queueJob);
  $("btn-add-audio").addEventListener("click", addAudioTrack);
  $("btn-close-encoders").addEventListener("click", closeEncoderDrawer);
  $("encoder-drawer").addEventListener("close", () => {
    if (!$("encoder-drawer").open) restoreEncoderFocus();
  });
  $("encoder-drawer").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeEncoderDrawer();
  });
  $("encoder-drawer").addEventListener("click", (event) => {
    if (event.target !== $("encoder-drawer")) return;
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeEncoderDrawer();
  });
  $("encoder-search").addEventListener("input", renderEncoderCards);
  $("btn-clear-encoder-search").addEventListener("click", () => {
    $("encoder-search").value = "";
    renderEncoderCards();
    $("encoder-search").focus();
  });
  $("btn-refresh-encoders").addEventListener("click", refreshEncoders);
  $("btn-open-presets").addEventListener("click", openPresetDrawer);
  $("btn-close-presets").addEventListener("click", () => $("preset-drawer").close());
  $("preset-drawer").addEventListener("close", () => {
    document.body.classList.remove("preset-drawer-open");
    $("btn-open-presets").focus();
  });
  $("preset-drawer").addEventListener("click", (event) => {
    if (event.target !== $("preset-drawer")) return;
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close();
  });
  document.querySelectorAll("[data-preset-source]").forEach((button) => {
    button.addEventListener("click", () => setPresetSource(button.dataset.presetSource));
  });
  $("preset-search").addEventListener("input", renderPresets);
  $("btn-clear-preset-search").addEventListener("click", () => {
    $("preset-search").value = "";
    renderPresets();
    $("preset-search").focus();
  });
  $("btn-cancel").addEventListener("click", cancelJob);
  $("container-select").addEventListener("change", () => {
    const name = $("output-name").value;
    if (name) {
      const base = name.replace(/\.[^.]+$/, "");
      const ext = choiceValue($("container-select")) === "mkv" ? "mkv" : "mp4";
      $("output-name").value = `${base}.${ext}`;
    }
  });
  $("link-matrix").addEventListener("click", (e) => { e.preventDefault(); $("matrix-dialog").showModal(); });
  $("btn-close-matrix").addEventListener("click", () => $("matrix-dialog").close());
}

document.addEventListener("DOMContentLoaded", () => {
  initializeChoices();
  wireEvents();
  boot();
});
