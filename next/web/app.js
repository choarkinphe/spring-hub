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
  settingsRevision: 0,
  validating: false,
  queueing: false,
  validation: null,
  posting: false,
  browserRequest: 0,
  scanRequest: 0,
  scanning: false,
  jobsLoading: false,
  jobsLoaded: false,
  detailRequest: 0,
  systemLoading: false,
  resources: null,
  resourcesLoading: false,
  resourcesAt: 0,
  decoderInventory: null,
  canceling: false,
  runtimeSettings: null,
  taskTemplates: [],
  jobActionsPending: new Set(),
  namingRequest: 0,
  generatedName: "",
  settingsSaving: false,
  templateEditing: null,
  defaultsApplied: false,
  jobCounts: {},
  queueFilter: "all",
  queueSearch: "",
  checkedJobs: new Set(),
  batchPending: false,
  queueFeedback: "",
};

const JOB_STATUS_LABELS = {
  waiting: "待启动",
  paused: "已暂停",
  queued: "排队中",
  probing: "扫描中",
  running: "转码中",
  finalizing: "整理输出",
  succeeded: "已完成",
  failed: "失败",
  canceled: "已取消",
  interrupted: "已中断",
};

function syncDialogLock() {
  document.body.classList.toggle("dialog-open", Boolean(document.querySelector("dialog[open]")));
}

function openSystemDrawer() {
  $("system-drawer").showModal();
  syncDialogLock();
  renderSystemStatus();
  if (state.resources) renderResources(state.resources);
  if (state.systemError) $("system-message").textContent = state.systemError;
  if (state.resourceError) $("resource-message").textContent = state.resourceError;
  $("btn-close-system").focus();
  refreshResources();
}

function closeSystemDrawer() { $("system-drawer").close(); }

function openJobDrawer(id) {
  state.selectedJob = id;
  state.detailJob = null;
  state.detailMeta = state.detailControls = null;
  $("detail-source").textContent = $("detail-status").textContent = "—";
  $("detail-actions").replaceChildren();
  $("detail-error").hidden = true;
  $("detail-message").textContent = "";
  $("job-detail").replaceChildren();
  $("job-log").textContent = $("progress-label").textContent = "—";
  $("progress-bar").style.width = "0%";
  $("btn-cancel").disabled = true;
  if (!$("job-drawer").open) $("job-drawer").showModal();
  syncDialogLock();
  $("btn-close-job").focus();
  loadJobDetail(id);
}

function closeJobDrawer() {
  state.detailRequest++;
  $("job-drawer").close();
}

function openCreateTask() {
  if (!state.caps || state.queueing || state.validating) return;
  $("create-message").textContent = "";
  if (!state.defaultsApplied && !state.selectedFile && !$("output-name").value && state.runtimeSettings) {
    state.defaultsApplied = true;
    setChoiceValue($("output-root-select"), state.runtimeSettings.default_output_root);
    if (state.runtimeSettings.default_task_template_id) applyTaskTemplate(state.runtimeSettings.default_task_template_id);
  }
  $("create-task-dialog").showModal();
  syncDialogLock();
  $("btn-select-file").focus();
}

function closeCreateTask() {
  if (state.posting) return;
  state.settingsRevision++;
  if (state.validation) showValidation("stale", "创建窗口已关闭，请重新预校验。", null);
  $("create-task-dialog").close();
}

function resetTaskSource() {
  state.selectedFile = null;
  state.scan = null;
  state.scanRequest++;
  state.scanning = false;
  state.validation = null;
  state.settingsRevision++;
  $("output-name").value = "";
  state.generatedName = "";
  state.defaultsApplied = false;
  state.namingRequest++;
  $("source-summary").replaceChildren(el("p", {class: "placeholder", text: "尚未选择源文件。点击“选择文件”；也可先调整参数并预校验。"}));
  $("spec-validation").hidden = true;
  $("validation-json").textContent = "";
  $("create-message").textContent = "";
  updateActionButtons();
}

function openFilePicker() {
  if (!$("create-task-dialog").open || state.posting) return;
  $("file-picker-dialog").showModal();
  syncDialogLock();
  browse(state.currentPath);
}

function closeFilePicker() {
  state.browserRequest++;
  $("file-picker-dialog").close();
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

/* ---------- runtime settings and task templates ---------- */
async function loadRuntimeSettings() {
  const report = await api("/settings");
  state.runtimeSettings = report.values;
  state.taskTemplates = (await api("/task-templates")).templates;
  renderTemplateChoices();
}

function renderTemplateChoices() {
  const options = [{value:"",label:"不使用模板"}, ...state.taskTemplates.map(t=>({value:t.id,label:t.name}))];
  renderChoice($("task-template-select"), options);
  renderChoice($("setting-default-template"), options, {value:state.runtimeSettings?.default_task_template_id || ""});
}

async function openSettingsDrawer() {
  $("settings-drawer").showModal(); syncDialogLock();
  $("settings-message").textContent = "";
  try {
    await loadRuntimeSettings();
    const settings=state.runtimeSettings;
    $("setting-concurrency").value=settings.max_concurrent_jobs;
    $("setting-auto-start").checked=settings.auto_start;
    $("setting-timeout").value=settings.job_timeout_seconds;
    $("setting-name-template").value=settings.output_name_template;
    renderChoice($("setting-output-root"),state.roots.filter(r=>!r.read_only).map(r=>({value:r.id,label:r.label})),{value:settings.default_output_root});
    renderSettingsTemplates();
  } catch(err) {$("settings-message").textContent="无法读取设置："+err.message;}
  $("btn-close-settings").focus();
}

async function saveRuntimeSettings() {
  if(state.settingsSaving)return;
  state.settingsSaving=true; $("btn-save-settings").disabled=true;
  try {
    const values={max_concurrent_jobs:Number($("setting-concurrency").value),auto_start:$("setting-auto-start").checked,
      job_timeout_seconds:Number($("setting-timeout").value),default_output_root:choiceValue($("setting-output-root")),
      output_name_template:$("setting-name-template").value,default_task_template_id:choiceValue($("setting-default-template")) || null};
    state.runtimeSettings=(await api("/settings",{method:"POST",body:JSON.stringify(values)})).values;
    $("settings-message").textContent="设置已保存，并发立即生效；输出默认值用于新任务。";
  }catch(err){$("settings-message").textContent="保存失败："+err.message;}
  finally{state.settingsSaving=false;$("btn-save-settings").disabled=false;}
}

async function previewOutputName() {
  try {
    const data=await api("/output-name",{method:"POST",body:JSON.stringify({template:$("setting-name-template").value,source:"sample.mp4",encoder:"x264",container:"mp4"})});
    $("name-preview").textContent=data.path;
  }catch(err){$("name-preview").textContent=err.message;}
}

async function generateOutputName() {
  if(!state.selectedFile || !state.runtimeSettings)return;
  const current=$("output-name").value;
  if(current && current!==state.generatedName)return;
  const request=++state.namingRequest;
  const source=state.selectedFile.path;
  const revision=state.settingsRevision;
  try{
    const data=await api("/output-name",{method:"POST",body:JSON.stringify({source,encoder:choiceValue($("video-encoder")),preset:state.presetSelection.preset?.name,container:choiceValue($("container-select"))})});
    if(request!==state.namingRequest || revision!==state.settingsRevision || source!==state.selectedFile?.path || $("output-name").value!==current)return;
    $("output-name").value=state.generatedName=data.path;
  }catch(err){$("create-message").textContent="命名失败："+err.message;}
}

function restoreForm(spec) {
  const choices={"container-select":["container"],"video-encoder":["video","encoder"],"video-quality-type":["video","quality_type"],
    "video-preset":["video","preset"],"video-tune":["video","tune"],"video-profile":["video","profile"],"video-level":["video","level"],"video-framerate":["video","framerate"],
    "dim-crop-mode":["dimensions","crop_mode"],"dim-anamorphic":["dimensions","anamorphic"],"dim-modulus":["dimensions","modulus"],
    "flt-deinterlace":["filters","deinterlace"],"flt-denoise":["filters","denoise"],"flt-detelecine":["filters","detelecine"],"flt-rotate":["filters","rotate"],
    "sub-behavior":["subtitles","behavior"],"chap-mode":["chapters","mode"]};
  const values={"dim-width":["dimensions","width"],"dim-height":["dimensions","height"],"video-quality":["video","quality"],"video-bitrate":["video","bitrate_kbps"],
    "sub-burn":["subtitles","burn_track"],"sub-default":["subtitles","default_track"],"sub-srt":["subtitles","srt_file"],"chap-file":["chapters","marker_file"]};
  for(const side of ["top","bottom","left","right"])values["crop-"+side]=["dimensions","crop_"+side];
  const checks={"video-two-pass":["video","two_pass"],"video-turbo":["video","turbo"],"flt-grayscale":["filters","grayscale"],"flt-hflip":["filters","hflip"],
    "flt-chroma":["filters","chroma_smooth"],"flt-lapsharp":["filters","lapsharp"],"flt-unsharp":["filters","unsharp"],"sub-forced":["subtitles","forced_only"],"sub-srt-burn":["subtitles","srt_burn"],"sub-srt-default":["subtitles","srt_default"]};
  const get=keys=>keys.reduce((v,k)=>v?.[k],spec);
  for(const [id,keys]of Object.entries(choices))setChoiceValue($(id),get(keys)??"");
  for(const [id,keys]of Object.entries(values))$(id).value=get(keys)??"";
  for(const [id,keys]of Object.entries(checks))$(id).checked=Boolean(get(keys));
  $("audio-tracks").replaceChildren();
  for(const track of spec.audio?.tracks||[]){
    addAudioTrack(); const node=$("audio-tracks").lastElementChild;
    setChoiceValue(node.querySelector(".a-encoder"),track.encoder);
    setChoiceValue(node.querySelector(".a-mixdown"),track.mixdown||"auto");setChoiceValue(node.querySelector(".a-bitrate"),track.bitrate||"auto");
    node.querySelector(".a-source").value=track.source==="auto"?"":track.source||"";
  }
}

function applyTaskTemplate(identity) {
  const template=state.taskTemplates.find(t=>t.id===identity);
  if(!template)return;
  const preset=template.spec.preset ? {id:template.preset_id,name:template.spec.preset} : null;
  state.presetSelection={source:preset?"imported":"custom",preset};
  restoreForm(template.form_spec);
  state.presetBaseline=template.baseline;
  state.templateEditing=template.id;
  $("template-name").value=template.name;$("template-description").value=template.description||"";
  settingsChanged();updatePresetSummary();generateOutputName();
}

async function saveTaskTemplate() {
  $("btn-save-template").disabled=true;
  try {
    const snapshot=captureSettings();
    const payload={name:$("template-name").value.trim(),description:$("template-description").value,spec:snapshot.spec,preset_id:snapshot.preset_id,
      form_spec:buildSpec(true),baseline:state.presetBaseline||null,version:1};
    const saved=await api("/task-templates"+(state.templateEditing?"/"+state.templateEditing:""),{method:"POST",body:JSON.stringify(payload)});
    state.templateEditing=saved.id;
    await loadRuntimeSettings();
    $("template-message").textContent="模板已保存，不包含源文件与输出路径。";
  }catch(err){$("template-message").textContent="保存失败："+err.message;}
  finally{$("btn-save-template").disabled=false;}
}

function renderSettingsTemplates() {
  $("settings-template-list").replaceChildren();
  for(const template of state.taskTemplates){
    const row=el("div",{class:"template-row"},[el("strong",{text:template.name}),el("p",{class:"hint",text:template.description||""})]);
    row.appendChild(el("button",{class:"btn btn-small",type:"button",text:"选用 / 编辑",onclick:()=>{
      $("settings-drawer").close();openCreateTask();applyTaskTemplate(template.id);
    }}));
    row.appendChild(el("button",{class:"btn btn-small btn-danger",type:"button",text:"删除",onclick:async()=>{
      if(!confirm("删除此编码任务模板？已创建任务不会改变。"))return;
      try{await api("/task-templates/"+template.id,{method:"DELETE"});await loadRuntimeSettings();renderSettingsTemplates();}
      catch(err){$("settings-message").textContent=err.message;}
    }}));
    $("settings-template-list").appendChild(row);
  }
}

/* ---------- bootstrap ---------- */
async function boot() {
  try {
    const caps = await api("/capabilities");
    state.caps = caps;
    state.encoderCatalog = caps.encoder_catalog;
    state.decoderInventory = caps.decoder_inventory;
    $("version-tag").textContent = "v" + caps.version;
    renderEngineStatus(caps.engine);
    renderSpecOptions(caps.spec_options);
    renderFeatureMatrix(caps.features);
    await loadRoots();
    await loadRuntimeSettings();
  } catch (err) {
    state.systemError = "无法读取系统状态：" + err.message;
    $("system-message").textContent = state.systemError;
    showBanner("error", "无法连接后端，请检查服务或鉴权。详见系统详情。");
  }
  updateActionButtons();
  await refreshResources();
  await refreshJobs();
  startPolling();
}

function renderSystemStatus() {
  renderSystemSummary();
  if (!$("system-drawer").open) return;
  const engine = state.caps?.engine;
  if (!engine) {
    $("system-message").textContent = state.systemError || "引擎状态尚未读取。";
    return;
  }
  $("system-info").replaceChildren();
  const rows = [
    ["转码引擎", engine.available ? `HandBrake ${engine.version || "版本未知"}` : "未检测到 HandBrakeCLI"],
    ["编码器检测", engine.video_encoders_known && engine.audio_encoders_known ? "检测结果已知（不是任意素材兼容保证）" : "检测不完整或未知"],
  ];
  for (const [label, value] of rows) $("system-info").appendChild(el("div", {}, [el("dt", {text: label}), el("dd", {text: value})]));
  $("storage-status").replaceChildren();
  for (const root of state.roots) {
    const ready = root.available && (!root.mount_marker || root.marker_present);
    $("storage-status").appendChild(el("div", {class: "storage-row"}, [
      el("strong", {text: root.label}), el("span", {text: ready ? "可访问" : "不可用 / 挂载标记缺失"}),
      el("span", {text: root.read_only ? "只读" : "可读写"}), el("span", {class: "storage-path", text: root.path}),
    ]));
  }
  if (!state.roots.length) $("storage-status").appendChild(el("p", {class: "hint", text: "未配置存储根，无法选择源文件。"}));
  $("system-message").textContent = [...(engine.notes || []), ...(engine.probe_notes || []), "状态为最近读取结果；存储可访问不代表已验证输出写入。"].join(" ");
  $("btn-create-task").disabled = state.queueing || state.validating;
  renderCodecStatus();
}

function renderSystemSummary() {
  const data = state.resources;
  const cpu = data?.cpu?.utilization_percent;
  const memory = data?.memory;
  const devices = data?.gpu?.devices || [];
  const writable = new Set(state.roots.filter(root => !root.read_only).map(root => root.path));
  const disks = (data?.disks?.items || []).filter(disk => disk.label === "服务数据" || writable.has(disk.path));
  const known = disks.filter(disk => disk.status === "ok" && disk.available_bytes != null);
  const capacities = [...new Map(known.map(disk => [disk.filesystem_id || disk.path, disk.available_bytes])).values()];
  const partial = !disks.length || disks.some(disk => disk.status !== "ok" || disk.available_bytes == null);
  const diskValue = capacities.length ? `${metricBytes(Math.min(...capacities))}${partial ? " · 部分未知" : ""}` : "未知";
  const rows = [
    ["CPU", cpu == null ? "未知" : metricPercent(cpu)],
    ["内存", memory?.used_bytes != null && memory.total_bytes != null ? `${metricBytes(memory.used_bytes)} / ${metricBytes(memory.total_bytes)}` : "未知"],
    ["GPU", devices.length ? `${devices.length} 张可见` : "检测未知"],
    ["可用空间（最少）", diskValue],
  ];
  $("system-summary").replaceChildren(...rows.map(([label, text]) => el("div", {}, [el("dt", {text: label}), el("dd", {text})])));
  const time = data ? new Date(data.sampled_at).toLocaleTimeString() : "等待采样";
  const notice = state.resourceError || state.systemError ? " · 更新失败，查看详情" : data && (cpu == null || !memory?.total_bytes || !devices.length || partial) ? " · 部分数据未知" : "";
  $("summary-message").textContent = `${data?.scope || "服务环境"} · ${time}${notice}`;
}

function metricBytes(value) {
  if (value == null) return "无法读取";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let n = value, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function metricPercent(value) { return value == null ? "无法读取" : `${value.toFixed(1)}%`; }

async function refreshResources() {
  if (state.resourcesLoading) return;
  state.resourcesLoading = true;
  try {
    const data = await api("/system/status");
    state.resources = data;
    state.resourcesAt = Date.now();
    state.resourceError = "";
    renderResources(data);
  } catch (err) {
    state.resourceError = (state.resources ? "刷新失败，以下为上次采样：" : "无法读取资源：") + err.message;
    $("resource-message").textContent = state.resourceError;
    renderSystemSummary();
  } finally { state.resourcesLoading = false; }
}

function renderResources(data) {
  renderSystemSummary();
  if (!$("system-drawer").open) return;
  $("resource-message").textContent = `${data.scope} · 采样 ${new Date(data.sampled_at).toLocaleTimeString()} · ${data.note}`;
  const cpu = data.cpu, memory = data.memory;
  const rows = [
    ["CPU", cpu.model ? `${cpu.model}，${cpu.logical_cpus} 逻辑 CPU` : cpu.note || "无法读取"],
    ["CPU 使用率", metricPercent(cpu.utilization_percent) + (cpu.utilization_percent == null ? "（首次采样或数据不可用）" : "（可见环境）")],
    ["可用 CPU / 容器配额", `${cpu.affinity_cpus ?? "未知"} / ${cpu.quota_cpus ?? "未报告限制"}`],
    ["内存", `${metricBytes(memory.used_bytes)} 已用 / ${metricBytes(memory.total_bytes)} 总量；可用 ${metricBytes(memory.available_bytes)}`],
  ];
  if (memory.container_limit_bytes != null) rows.push(["容器内存", `${metricBytes(memory.container_used_bytes)} / ${metricBytes(memory.container_limit_bytes)}`]);
  for (const gpu of data.gpu.devices || []) {
    rows.push(["GPU", `${gpu.name}，驱动 ${gpu.driver || "未知"}`]);
    rows.push(["GPU 利用率 / 显存", `${metricPercent(gpu.utilization_percent)}；${metricBytes(gpu.memory_used_bytes)} / ${metricBytes(gpu.memory_total_bytes)}（${gpu.source}）`]);
  }
  if (!data.gpu.devices?.length) rows.push(["GPU", data.gpu.note || "未能枚举 GPU；不代表没有物理设备。"]);
  if (data.gpu.devices?.length) $("resource-message").textContent += ` GPU 采样 ${data.gpu.sampled_at ? new Date(data.gpu.sampled_at).toLocaleTimeString() : "时间未知"}；${data.gpu.note || "检测到设备不代表可硬件编码。"}`;
  $("resource-info").replaceChildren(...rows.map(([label, text]) => el("div", {}, [el("dt", {text: label}), el("dd", {text})])));
  $("disk-status").replaceChildren(el("h3", {text: "磁盘容量"}));
  const groups = new Map();
  for (const disk of data.disks.items || []) {
    const key = disk.status === "ok" ? disk.filesystem_id : disk.path;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(disk);
  }
  for (const disks of groups.values()) {
    const disk = disks[0];
    const paths = el("details", {class: "disk-paths"}, [el("summary", {text: disks.length > 1 ? "同盘共享容量，查看路径（不相加）" : "查看路径"})]);
    for (const item of disks) paths.appendChild(el("p", {class: "hint", text: `${item.label}：${item.path}`}));
    $("disk-status").appendChild(el("div", {class: "storage-row"}, [
      el("strong", {text: disks.map(d => d.label).join(" / ")}),
      el("span", {text: disk.status === "ok" ? `${metricBytes(disk.used_bytes)} 已用 / ${metricBytes(disk.total_bytes)} 总量；可用 ${metricBytes(disk.available_bytes)}` : disk.note}), paths,
    ]));
  }
  if (!data.disks.items) $("disk-status").appendChild(el("p", {class: "hint", text: data.disks.note || "无法读取磁盘容量"}));
}

function renderCodecStatus() {
  if (!$("system-drawer").open) return;
  const box = $("codec-status");
  box.replaceChildren();
  for (const kind of ["video", "audio"]) {
    box.appendChild(el("h3", {text: kind === "video" ? "视频编码器" : "音频编码器"}));
    for (const item of state.encoderCatalog?.[kind] || []) box.appendChild(el("div", {class: "codec-row"}, [
      el("strong", {text: item.name}), el("span", {text: encoderInstalledLabel(item)}),
      el("span", {text: ENCODER_STATUS_LABELS[item.status] || item.status}), el("p", {class: "hint", text: item.reason}),
    ]));
  }
  box.appendChild(el("h3", {text: "HandBrake 解码后端"}));
  box.appendChild(el("p", {class: "hint", text: "内置软件解码器没有完整枚举 CLI，逐格式支持以实际源扫描为准。后端诊断不等于解码实测。"}));
  const labels = {not_compiled: "未编译到引擎", unavailable: "当前不可用", reported: "引擎报告支持", unknown: "未报告 / 未知"};
  for (const name of ["nvdec", "qsv", "videotoolbox"]) {
    const item = state.caps?.engine.decoder_backends?.[name];
    box.appendChild(el("p", {text: `${name.toUpperCase()}：${labels[item?.status || "unknown"]} ${item?.reason || ""}`}));
  }
  const inventory = state.decoderInventory;
  box.appendChild(el("h3", {text: "独立 FFmpeg 解码器"}));
  box.appendChild(el("p", {class: "hint", text: (inventory?.status === "missing" ? "未检测到 FFmpeg。" : inventory?.status === "reported" ? "已安装，以下为构建提供的清单。" : "检测未知。") + (inventory?.note || "不代表 HandBrake 内置库。" )}));
  for (const item of inventory?.items || []) box.appendChild(el("p", {class: "hint", text: `${item.kind} / ${item.name}：${item.description}`}));
}

async function refreshCodecs() {
  $("btn-refresh-codecs").disabled = true;
  $("codec-message").textContent = "正在重新检测，可能需要数秒…";
  try {
    const data = await api("/encoders?refresh=1");
    state.encoderCatalog = data.encoder_catalog;
    state.decoderInventory = data.decoder_inventory;
    state.caps.engine = data.engine;
    renderEngineStatus(data.engine);
    renderSystemStatus();
    document.querySelectorAll(".encoder-control").forEach(renderEncoderControl);
    settingsChanged();
    $("codec-message").textContent = "检测完成；内置不等于任意素材可编码。";
  } catch (err) { $("codec-message").textContent = "检测失败，保留上次结果：" + err.message; }
  finally { $("btn-refresh-codecs").disabled = false; }
}

async function refreshSystem() {
  if (state.systemLoading) return;
  state.systemLoading = true;
  $("btn-refresh-system").disabled = true;
  try {
    const caps = await api("/capabilities");
    const first = !state.caps;
    state.caps = caps;
    state.encoderCatalog = caps.encoder_catalog;
    state.decoderInventory = caps.decoder_inventory;
    renderEngineStatus(caps.engine);
    renderFeatureMatrix(caps.features);
    $("version-tag").textContent = "v" + caps.version;
    if (first) renderSpecOptions(caps.spec_options);
    else document.querySelectorAll(".encoder-control").forEach(renderEncoderControl);
    await loadRoots();
    state.systemError = "";
    settingsChanged();
    renderSystemSummary();
  } catch (err) {
    state.systemError = "刷新失败，保留上次结果：" + err.message;
    $("system-message").textContent = state.systemError;
    renderSystemSummary();
  } finally {
    state.systemLoading = false;
    await refreshResources();
    $("btn-refresh-system").disabled = false;
  }
}

function renderEngineStatus(engine) {
  const pill = $("engine-status");
  $("engine-banner").classList.add("hidden");
  if (engine.available) {
    pill.className = "pill pill-ok";
    pill.textContent = `HandBrake ${engine.version || "?"}`;
    pill.title = engine.version_string || "";
    // Detailed detection notes belong in the system drawer.
  } else {
    pill.className = "pill pill-bad";
    pill.textContent = "未检测到 HandBrakeCLI";
    pill.title = (engine.notes || []).join(" ");
    showBanner("error", "未找到转码引擎，无法加入队列。请在系统详情中查看检测信息。");
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

  const rules = opts.ui_constraints;
  if (rules) {
    renderChoice($("dim-anamorphic"), rules.anamorphic, { value: "auto", labels: {auto: "自动", none: "关闭", loose: "宽松"} });
    renderChoice($("flt-deinterlace"), rules.deinterlace, { value: "off", labels: {off: "关闭", "skip-spatial": "Yadif（跳过空间检查）", default: "Yadif（默认）", bob: "Bob（双倍帧率）"} });
    renderChoice($("flt-denoise"), rules.denoise, { value: "off", labels: {off: "关闭", nlmeans: "NLMeans", hqdn3d: "HQDN3D"} });
    renderChoice($("flt-detelecine"), rules.detelecine, { value: "off" });
  }
  renderChoice($("sub-behavior"), opts.subtitle_behaviors, {
    value: "none", labels: { ...CHOICE_LABELS, none: "不添加", default: "默认字幕" },
  });

  if (!state.audioOptions) {
    state.audioOptions = opts;
    addAudioTrack();
  }
  syncControls();
  updateActionButtons();
}

/* ---------- storage roots & browser ---------- */
async function loadRoots() {
  const data = await api("/storage-roots");
  state.roots = data.roots;
  const options = data.roots.map((r) => ({ value: r.id, label: r.label + (r.read_only ? " (只读)" : "") }));
  renderChoice($("root-select"), options, { tags: true });
  renderChoice($("output-root-select"), options, { tags: true });
  const writable = data.roots.find((r) => !r.read_only);
  if (!state.currentRoot && writable) setChoiceValue($("output-root-select"), writable.id);
  if (!data.roots.some(r => r.id === state.currentRoot)) {
    state.currentRoot = data.roots[0]?.id || null;
    state.currentPath = "";
  }
  if (state.currentRoot) setChoiceValue($("root-select"), state.currentRoot);
  renderSystemStatus();
}

async function browse(path) {
  const root = choiceValue($("root-select")) || state.currentRoot;
  const request = ++state.browserRequest;
  state.currentRoot = root;
  state.currentPath = path;
  $("browser").replaceChildren();
  $("browser-message").textContent = root ? "正在读取目录…" : "未配置存储根。";
  if (!root) return;
  try {
    const data = await api(`/storage-roots/${encodeURIComponent(root)}/entries?path=${encodeURIComponent(path)}`);
    if (request !== state.browserRequest || !$("file-picker-dialog").open) return;
    $("current-path").textContent = "/" + (data.path || "");
    $("browser-message").textContent = data.entries.length ? "选择文件后返回任务设置。" : "此目录为空，可返回上级或切换存储根。";
    const browser = $("browser");
    browser.innerHTML = "";
    for (const entry of data.entries) {
      const node = el("button", {
        type: "button", class: "entry" + (entry.is_dir ? " dir" : ""),
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
    if (request === state.browserRequest && $("file-picker-dialog").open) $("browser-message").textContent = "读取目录失败：" + err.message;
  }
}

function selectFile(root, path, name) {
  state.selectedFile = { root, path, name };
  state.scan = null;
  state.scanRequest++;
  state.scanning = false;
  $("create-message").textContent = "";
  $("source-summary").innerHTML = "";
  $("source-summary").appendChild(el("div", { class: "meta-grid" }, [
    el("div", {}, [el("b", { text: "文件" }), document.createTextNode(name)]),
    el("div", {}, [el("b", { text: "根" }), document.createTextNode(root)]),
    el("div", {}, [el("b", { text: "路径" }), document.createTextNode(path)]),
  ]));
  $("btn-scan").disabled = false;
  settingsChanged();
  if (!$("output-name").value) {
    const base = name.replace(/\.[^.]+$/, "");
    const ext = (choiceValue($("container-select")) === "mkv") ? "mkv" : "mp4";
    if (!state.runtimeSettings) $("output-name").value = `converted/${base}.${ext}`;
  }
  generateOutputName();
  if ($("file-picker-dialog").open) closeFilePicker();
}

/* ---------- scan ---------- */
async function scanSource() {
  if (!state.selectedFile || state.scanning) return;
  const source = {...state.selectedFile};
  const request = ++state.scanRequest;
  state.scanning = true;
  updateActionButtons();
  $("create-message").textContent = "";
  try {
    const data = await api("/probe", {
      method: "POST", body: JSON.stringify({root: source.root, path: source.path}),
    });
    if (request !== state.scanRequest) return;
    state.scan = data.scan;
    renderScan(data.scan);
  } catch (err) {
    if (request === state.scanRequest) $("create-message").textContent = "扫描失败：" + err.message;
  } finally {
    if (request === state.scanRequest) {
      state.scanning = false;
      updateActionButtons();
    }
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
  $("preset-hint").textContent = preset ? `${preset.description ? preset.description + " " : ""}以预设为基底；仅选择后修改的参数会覆盖预设，未修改的控件值不会提交。` : "使用下方各标签中的参数。";
}

function selectPreset(source, preset) {
  state.presetSelection = { source, preset };
  state.presetBaseline = source === "custom" ? null : buildSpec(true);
  settingsChanged();
  updatePresetSummary();
  generateOutputName();
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
  syncDialogLock();
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
      const selected = state.presetSelection.source === source && (source === "custom" || (state.presetSelection.preset?.id || state.presetSelection.preset?.name) === (preset.id || preset.name));
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
  if (encoder.status === "no_hardware") return encoder.installed === true ? "已内置，但硬件不可用" : "安装情况未确认；硬件不可用";
  if (encoder.status === "unsupported") return "当前平台不支持此编码器";
  if (encoder.status === "not_installed") return "当前引擎不含（换引擎构建即可）";
  if (encoder.status === "passthrough_only" || encoder.device === "passthrough") return "不适用（直通或不编码）";
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
  syncDialogLock();
  $("encoder-search").focus();
  if (!state.encoderCatalog) refreshEncoders();
}

function restoreEncoderFocus() {
  syncDialogLock();
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
    state.decoderInventory = data.decoder_inventory;
    state.caps.engine = data.engine;
    renderEngineStatus(data.engine);
    renderSystemStatus();
    document.querySelectorAll(".encoder-control").forEach(renderEncoderControl);
  } catch (err) {
    state.encodersError = "重新检测失败：" + err.message + "。下方保留上次结果，不代表当前状态。";
  } finally {
    state.encodersLoading = false;
    renderEncoderCards();
    settingsChanged();
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
  const source = el("input", { class: "a-source", type: "number", min: "1", max: "64", step: "1", placeholder: "自动" });

  track.appendChild(el("div", { class: "grid-2" }, [
    choiceField("编码器", encoder), choiceField("混音", mixdown),
  ]));
  track.appendChild(el("div", { class: "grid-2" }, [
    choiceField("码率（kbps）", bitrate),
    el("label", { class: "field" }, [el("span", { text: "源音轨" }), source]),
  ]));
  initializeEncoderControl(encoder, "audio", "aac");
  renderChoice(mixdown, opts.audio_mixdowns, { value: "auto" });
  renderChoice(bitrate, ["auto", "96", "128", "160", "192", "256", "320"]);
  track.appendChild(el("button", {
    class: "btn btn-ghost btn-small", type: "button", text: "移除",
    onclick: () => { track.remove(); renumberAudio(); settingsChanged(); },
  }));
  container.appendChild(track);
  settingsChanged();
}

function renumberAudio() {
  [...$("audio-tracks").children].forEach((node, i) => {
    node.querySelector("h4").textContent = `音轨 ${i + 1}`;
  });
}

/* ---------- capability relationships & prevalidation ---------- */
function disableControl(control, disabled) {
  if (!control) return;
  control.disabled = disabled;
  control.querySelectorAll("input, select, button").forEach((input) => { input.disabled = disabled; });
  control.setAttribute("aria-disabled", String(disabled));
}

function syncControls(target) {
  const rules = state.caps?.spec_options?.ui_constraints;
  const mode = choiceValue($("video-quality-type"));
  const bitrate = (rules?.bitrate_quality_types || ["abr", "vbr"]).includes(mode);
  $("video-quality").disabled = bitrate || mode === "lossless";
  $("video-bitrate").disabled = !bitrate;
  $("video-two-pass").disabled = !bitrate;
  if (!bitrate) $("video-two-pass").checked = false;
  $("video-turbo").disabled = !bitrate || !$("video-two-pass").checked;
  if ($("video-turbo").disabled) $("video-turbo").checked = false;

  const inherited = state.presetSelection.preset && state.presetBaseline &&
    choiceValue($("video-encoder")) === state.presetBaseline.video.encoder;
  const lossless = inherited || (rules?.lossless_encoders || ["x264", "x264_10bit", "x265", "x265_10bit", "x265_12bit"]).includes(choiceValue($("video-encoder")));
  $("video-quality-type").querySelectorAll("input, option").forEach((input) => {
    if (input.value === "lossless") input.disabled = !lossless;
  });
  if (!lossless && mode === "lossless") {
    setChoiceValue($("video-quality-type"), "rf");
    $("video-quality").disabled = false;
  }
  const crop = choiceValue($("dim-crop-mode")) === "custom";
  for (const side of ["top", "bottom", "left", "right"]) $("crop-" + side).disabled = !crop;
  if (target === $("flt-lapsharp") && target.checked) $("flt-unsharp").checked = false;
  if (target === $("flt-unsharp") && target.checked) $("flt-lapsharp").checked = false;
  for (const track of $("audio-tracks").children) {
    const copy = ["copy", "none"].includes(choiceValue(track.querySelector(".a-encoder")));
    disableControl(track.querySelector(".a-mixdown"), copy);
    disableControl(track.querySelector(".a-bitrate"), copy);
  }
  const behavior = choiceValue($("sub-behavior"));
  const source = behavior !== "none";
  const srt = Boolean($("sub-srt").value.trim());
  if (target === $("sub-srt-burn") && target.checked) {
    $("sub-burn").value = "";
    if (behavior === "burn") setChoiceValue($("sub-behavior"), "add-first");
  }
  if (target === $("sub-srt-default") && target.checked) {
    $("sub-default").value = "";
    if (behavior === "default") setChoiceValue($("sub-behavior"), "add-first");
  }
  const activeBehavior = choiceValue($("sub-behavior"));
  if (activeBehavior === "burn" || (source && $("sub-burn").value)) $("sub-srt-burn").checked = false;
  if (activeBehavior === "default" || (source && $("sub-default").value)) $("sub-srt-default").checked = false;
  $("sub-burn").disabled = !source || activeBehavior === "burn" || $("sub-srt-burn").checked;
  $("sub-default").disabled = !source || activeBehavior === "default" || $("sub-srt-default").checked;
  $("sub-forced").disabled = !source;
  $("sub-burn").max = $("sub-default").max = String(rules?.source_subtitle_limit || 1);
  $("sub-srt-burn").disabled = $("sub-srt-default").disabled = !srt;
  $("chap-file").disabled = choiceValue($("chap-mode")) !== "markers";
}

function updateActionButtons() {
  const busy = state.validating || state.queueing;
  $("btn-validate").disabled = busy || !state.caps;
  $("btn-queue").disabled = busy || !state.selectedFile || !state.caps?.engine.available;
  $("btn-create-task").disabled = busy || !state.caps;
  $("btn-close-create").disabled = state.posting;
  $("btn-select-file").disabled = state.posting;
  $("btn-scan").disabled = state.scanning || !state.selectedFile || state.posting;
  $("btn-scan").textContent = state.scanning ? "扫描中…" : "扫描源文件";
}

function settingsChanged(event) {
  syncControls(event?.target);
  state.settingsRevision++;
  if (state.validation) showValidation("stale", "设置已修改，请重新预校验。", null);
  updateActionButtons();
  if (event?.target && ["container-select", "video-encoder"].some(id => $(id) === event.target || $(id).contains(event.target))) generateOutputName();
}

function showValidation(status, message, result) {
  state.validation = { status, result };
  const box = $("spec-validation");
  box.hidden = false;
  box.dataset.status = status;
  $("validation-message").textContent = message;
  $("validation-details").hidden = !result;
  $("validation-json").textContent = result ? JSON.stringify({
    preset_id: result.preset_id, args_scope: result.args_scope,
    submitted_spec: result.submitted_spec, args: result.args,
  }, null, 2) : "";
}

function captureSettings() {
  for (const input of document.querySelectorAll('[data-page="encode"] input')) {
    if (!input.disabled && !input.checkValidity()) throw new Error("请检查数值范围或输入格式：" + (input.closest("label")?.textContent.trim() || input.id));
  }
  const spec = buildSpec();
  return {
    spec, preset_id: state.presetSelection.preset?.id || spec.preset || "custom",
    input: state.selectedFile ? { root: state.selectedFile.root, path: state.selectedFile.path } : null,
    output: { root: choiceValue($("output-root-select")), path: $("output-name").value.trim() },
  };
}

async function prevalidate(snapshot, revision) {
  showValidation("pending", "正在校验参数…", null);
  const result = await api("/spec/validate", {
    method: "POST", body: JSON.stringify({ spec: snapshot.spec, preset_id: snapshot.preset_id }),
  });
  if (revision !== state.settingsRevision || JSON.stringify(snapshot) !== JSON.stringify(captureSettings())) {
    showValidation("stale", "设置已修改，本次校验结果已过期；请重新校验。", null);
    return false;
  }
  if (!result.valid) throw new Error("后端未确认参数有效。");
  showValidation("passed", result.args_scope === "overrides"
    ? "预校验通过：已解析预设，详情仅展示显式覆盖参数。"
    : "预校验通过：自定义参数结构与 CLI 映射有效。", { ...result, submitted_spec: snapshot.spec });
  return true;
}

async function validateSettings() {
  if (state.validating || state.queueing) return;
  const revision = state.settingsRevision;
  state.validating = true;
  updateActionButtons();
  try {
    await prevalidate(captureSettings(), revision);
  } catch (err) {
    showValidation(revision === state.settingsRevision ? "failed" : "stale",
      revision === state.settingsRevision ? "预校验失败：" + err.message : "设置已修改，请重新预校验。", null);
  } finally {
    state.validating = false;
    updateActionButtons();
  }
}

/* ---------- build spec ---------- */
function numberOrNull(id) {
  const control = $(id);
  if (control.disabled) return null;
  const raw = (control.classList.contains("choice-control") ? choiceValue(control) : control.value).trim();
  if (raw === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) throw new Error(`无效数值：${id}`);
  return n;
}

function buildSpec(full = false) {
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
      tracks: [...$("audio-tracks").children].map((node) => {
        const encoder = choiceValue(node.querySelector(".a-encoder"));
        return {
          encoder, source: node.querySelector(".a-source").value || "auto",
          ...(["copy", "none"].includes(encoder) ? {} : {
            mixdown: choiceValue(node.querySelector(".a-mixdown")),
            bitrate: choiceValue(node.querySelector(".a-bitrate")),
          }),
        };
      }),
    },
    subtitles: {
      behavior: choiceValue($("sub-behavior")),
      burn_track: numberOrNull("sub-burn"),
      default_track: numberOrNull("sub-default"),
      forced_only: $("sub-forced").checked,
      srt_file: $("sub-srt").value.trim() || null,
      srt_burn: $("sub-srt-burn").checked,
      srt_default: $("sub-srt-default").checked,
    },
    chapters: {
      mode: choiceValue($("chap-mode")),
      marker_file: $("chap-file").value.trim() || null,
    },
  };
  const bitrateMode = ["abr", "vbr"].includes(spec.video.quality_type);
  if (bitrateMode || spec.video.quality_type === "lossless") delete spec.video.quality;
  if (!bitrateMode) {
    delete spec.video.bitrate_kbps;
    spec.video.two_pass = spec.video.turbo = false;
  } else if (!spec.video.two_pass) spec.video.turbo = false;
  if (spec.dimensions.crop_mode !== "custom") {
    for (const side of ["top", "bottom", "left", "right"]) delete spec.dimensions["crop_" + side];
  }
  const subs = spec.subtitles;
  if (subs.behavior === "none") {
    delete subs.burn_track;
    delete subs.default_track;
    subs.forced_only = false;
  }
  if (subs.behavior === "burn" || subs.srt_burn) delete subs.burn_track;
  if (subs.behavior === "default" || subs.srt_default) delete subs.default_track;
  if (!subs.srt_file) subs.srt_burn = subs.srt_default = false;
  if (spec.chapters.mode !== "markers") delete spec.chapters.marker_file;
  if (presetSource !== "custom" && preset) {
    spec.preset = preset.name;
  }
  // Strip null/empty to keep the payload tight; the server also accepts nulls.
  for (const section of ["dimensions", "filters", "video", "subtitles", "chapters"]) {
    for (const [key, value] of Object.entries(spec[section])) {
      if (value === null || value === "") delete spec[section][key];
    }
  }
  if (!full && spec.preset && state.presetBaseline) {
    const baseline = state.presetBaseline;
    const sparse = { version: 1, preset: spec.preset, title: 1 };
    if (spec.container !== baseline.container) sparse.container = spec.container;
    for (const section of ["dimensions", "filters", "video", "audio", "subtitles", "chapters"]) {
      const changed = [...new Set([...Object.keys(spec[section]), ...Object.keys(baseline[section])])].filter((key) => JSON.stringify(spec[section][key]) !== JSON.stringify(baseline[section][key]));
      if (!changed.length) continue;
      sparse[section] = Object.fromEntries(changed.map((key) => [key, spec[section][key] ?? null]));
      // Coupled controls form one explicit override, not incomplete fragments.
      if (section === "video" && changed.some((key) => ["quality_type", "quality", "bitrate_kbps", "two_pass", "turbo"].includes(key))) {
        for (const key of ["quality_type", "quality", "bitrate_kbps", "two_pass", "turbo"]) {
          if (key in spec.video) sparse.video[key] = spec.video[key];
        }
      }
      const groups = {
        dimensions: [["crop_mode", "crop_top", "crop_bottom", "crop_left", "crop_right"]],
        subtitles: [["behavior", "burn_track", "default_track", "forced_only"], ["srt_file", "srt_burn", "srt_default"]],
        chapters: [["mode", "marker_file"]],
        filters: [["lapsharp", "unsharp"]],
      };
      for (const group of groups[section] || []) {
        if (changed.some((key) => group.includes(key))) {
          for (const key of group) {
            if (key in spec[section]) sparse[section][key] = spec[section][key];
          }
        }
      }
      if (section === "filters" && changed.some((key) => ["rotate", "hflip"].includes(key))) {
        sparse.filters.rotate = spec.filters.rotate;
        sparse.filters.hflip = spec.filters.hflip;
      }
    }
    return sparse;
  }
  return spec;
}

/* ---------- jobs ---------- */
async function queueJob() {
  if (state.validating || state.queueing || !state.selectedFile || !state.caps?.engine.available) return;
  if (!$("output-name").value.trim()) { $("create-message").textContent = "请填写输出文件名。"; return; }
  const revision = state.settingsRevision;
  state.queueing = true;
  updateActionButtons();
  let validated = false;
  try {
    const snapshot = captureSettings();
    validated = await prevalidate(snapshot, revision);
    if (!validated) return;
    state.posting = true;
    updateActionButtons();
    $("create-message").textContent = "正在加入队列…";
    const job = await api("/jobs", {
      method: "POST", body: JSON.stringify(snapshot),
    });
    state.posting = false;
    state.selectedJob = job.id;
    resetTaskSource();
    $("create-task-dialog").close();
    await refreshJobs();
    $("queue-message").textContent = "任务已加入队列，点击任务查看详情。";
  } catch (err) {
    if (!validated) showValidation(revision === state.settingsRevision ? "failed" : "stale",
      revision === state.settingsRevision ? "预校验失败，未加入队列：" + err.message : "设置已修改，请重新加入队列。", null);
    else $("create-message").textContent = "加入队列失败：" + err.message;
  } finally {
    state.posting = false;
    state.queueing = false;
    updateActionButtons();
    if (!$("create-task-dialog").open) $("btn-create-task").focus();
  }
}

const QUEUE_FILTERS = [
  ["all", "全部", null],
  ["pending", "待处理", ["waiting", "queued"]],
  ["active", "执行中", ["probing", "running", "finalizing"]],
  ["paused", "已暂停", ["paused"]],
  ["completed", "已完成", ["succeeded"]],
  ["issues", "失败 / 中断", ["failed", "interrupted"]],
  ["canceled", "已取消", ["canceled"]],
];

function filteredJobs() {
  const statuses = QUEUE_FILTERS.find(([key]) => key === state.queueFilter)?.[2];
  const query = state.queueSearch.trim().toLocaleLowerCase();
  return state.jobs.filter(job => (!statuses || statuses.includes(job.status)) &&
    [job.input.root, job.input.path, job.output.root, job.output.path, job.preset_name, job.preset_id, job.container]
      .filter(Boolean).join(" ").toLocaleLowerCase().includes(query));
}

function jobPercent(job) {
  const value = Number(job.progress);
  return Math.round((Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0) * 100);
}

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds)) || Number(seconds) < 0) return "—";
  const value = Math.round(Number(seconds));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
  return `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分`;
}

function jobFormat(job) {
  const container = {av_mp4: "MP4", av_mkv: "MKV", av_webm: "WebM", mp4: "MP4", mkv: "MKV", webm: "WebM"}[job.container];
  return container || job.output.path.match(/\.([a-z0-9]{1,8})$/i)?.[1].toUpperCase() || "自动";
}

function jobStatusLabel(job) {
  if (job.error === "cancel requested") return "取消中";
  if (job.pause_requested && job.status !== "paused") return "暂停中";
  return JOB_STATUS_LABELS[job.status] || job.status;
}

function eligibleJobs(action) {
  return state.jobs.filter(job => state.checkedJobs.has(job.id) && job.actions?.includes(action) && !state.jobActionsPending.has(job.id));
}

function renderQueueSelection() {
  const visible = filteredJobs();
  const selected = visible.filter(job => state.checkedJobs.has(job.id)).length;
  const check = $("queue-select-all");
  check.checked = visible.length > 0 && selected === visible.length;
  check.indeterminate = selected > 0 && selected < visible.length;
  check.disabled = !visible.length || state.batchPending;
  const total = state.checkedJobs.size;
  $("queue-selection-note").textContent = state.batchPending ? "正在逐项处理所选任务…" : total
    ? `已选 ${total} 项${total > selected ? `（当前结果 ${selected} 项）` : ""}` : "勾选任务后可批量操作";
  for (const action of ["start", "pause", "cancel", "delete"]) {
    const button = $("btn-batch-" + action);
    const count = eligibleJobs(action).length;
    button.disabled = state.batchPending || !count;
    button.title = `仅操作所选任务中允许此操作的 ${count} 项`;
  }
}

function renderQueue() {
  const visible = filteredJobs();
  const filtersSignature = JSON.stringify([state.jobs.map(job => job.status), state.queueFilter]);
  if (filtersSignature !== state.queueFiltersSignature) {
    state.queueFiltersSignature = filtersSignature;
    const focusedFilter = document.activeElement?.dataset.queueFilter;
    $("queue-filters").replaceChildren(...QUEUE_FILTERS.map(([key, label, statuses]) => {
    const count = state.jobs.filter(job => !statuses || statuses.includes(job.status)).length;
    return el("button", {type: "button", class: "queue-filter" + (state.queueFilter === key ? " active" : ""),
      "aria-pressed": String(state.queueFilter === key), "data-queue-filter": key, onclick: () => {state.queueFilter = key; renderQueue();}}, [
      el("span", {text: label}), el("span", {class: "queue-filter-count", text: String(count)}),
    ]);
    }));
    if (focusedFilter) [...$("queue-filters").querySelectorAll("button")].find(button => button.dataset.queueFilter === focusedFilter)?.focus();
  }
  const total = Object.values(state.jobCounts).reduce((sum, n) => sum + n, 0);
  $("job-list-note").textContent = `当前 ${visible.length} / 最近 ${state.jobs.length} 项 · 全库 ${total} 项；筛选与批量操作仅限最近 100 项`;
  $("queue-empty").hidden = visible.length !== 0;
  $("queue-empty-title").textContent = state.jobs.length ? "没有匹配的任务" : "还没有转换任务";
  $("queue-empty-description").textContent = state.jobs.length ? "尝试其他状态或搜索关键词。" : "点击“创建任务”，选择源文件并加入队列。";
  $("btn-clear-queue-filters").hidden = !state.jobs.length;
  const list = $("job-list");
  const signature = JSON.stringify([visible, state.selectedJob, [...state.checkedJobs], [...state.jobActionsPending], state.batchPending]);
  if (signature !== state.jobsSignature) {
    state.jobsSignature = signature;
    const focus = document.activeElement;
    const focusId = focus?.dataset.jobId;
    const focusKind = focus?.dataset.focusKind;
    list.replaceChildren(...visible.map(renderQueueJob));
    if (focusId) [...list.querySelectorAll("button, input")].find(node => node.dataset.jobId === focusId && node.dataset.focusKind === focusKind)?.focus();
  }
  renderQueueSelection();
}

function renderQueueJob(job) {
  const selected = state.checkedJobs.has(job.id);
  const status = Object.hasOwn(JOB_STATUS_LABELS, job.status) ? job.status : "unknown";
  const item = el("li", {class: `job-row queue-job status-row-${status}${selected ? " checked" : ""}`, "data-queue-job": job.id});
  const check = el("input", {type: "checkbox", "aria-label": `选择任务：${job.input.path}`,
    "data-job-id": job.id, "data-focus-kind": "check", onchange: () => {
      if (check.checked) state.checkedJobs.add(job.id); else state.checkedJobs.delete(job.id);
      renderQueue();
    }});
  check.checked = selected;
  check.disabled = state.batchPending;
  item.appendChild(el("label", {class: "job-select"}, [check]));
  const format = jobFormat(job);
  const file = el("button", {type: "button", class: "queue-file" + (job.id === state.selectedJob ? " active" : ""),
    "data-job-id": job.id, "data-focus-kind": "detail", "aria-haspopup": "dialog", "aria-controls": "job-drawer",
    "aria-label": `查看任务详情：${job.input.path}`, onclick: () => openJobDrawer(job.id)}, [
      el("span", {class: "job-format-icon", "aria-hidden": "true", text: format}),
      el("span", {class: "job-file-copy"}, [el("strong", {text: job.input.path.split("/").pop(), title: job.input.path}),
        el("span", {class: "job-source-path", text: `${job.input.root} / ${job.input.path}`, title: `${job.input.root} / ${job.input.path}`}),
        el("span", {class: "job-output-path", text: `输出：${job.output.root} / ${job.output.path}`, title: `${job.output.root} / ${job.output.path}`})]),
    ]);
  item.appendChild(file);
  const created = new Date(job.created_at);
  item.appendChild(el("div", {class: "job-conversion"}, [el("strong", {text: format}),
    el("span", {text: job.preset_name || (job.preset_id === "custom" ? "自定义参数" : job.preset_id) || "自定义参数"}),
    el("small", {text: Number.isNaN(created.getTime()) ? "创建时间未知" : created.toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}), title: job.created_at || ""}),
  ]));
  const pct = jobPercent(job);
  const symbols = {succeeded: "✓", failed: "!", interrupted: "!", paused: "Ⅱ", running: "▶", probing: "◌", finalizing: "◌", canceled: "×", waiting: "◷", queued: "◷"};
  const progress = el("div", {class: "queue-job-progress"}, [el("div", {class: "queue-progress-heading"}, [
    el("span", {class: `status-tag status-${status}`, text: `${symbols[status] || "·"} ${jobStatusLabel(job)}`}),
    el("strong", {text: `${pct}%`}),
  ])]);
  const fill = el("div", {class: "queue-progress-fill"});
  fill.style.width = pct + "%";
  progress.appendChild(el("div", {class: "queue-progress-track", role: "progressbar", "aria-label": `${job.input.path} 转换进度`, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct)}, [fill]));
  const active = ["probing", "running", "finalizing"].includes(job.status);
  const timing = active ? `${job.speed != null ? `${job.speed} fps` : "速度待报告"} · ${job.eta_seconds != null ? `剩余 ${formatDuration(job.eta_seconds)}` : "剩余时间未知"}`
    : job.status === "succeeded" ? "转换完成，输出已保存" : job.status === "paused"
      ? (["probing", "running", "finalizing"].includes(job.paused_from) ? "原地暂停，继续后恢复处理" : "尚未执行，继续后进入队列")
    : job.status === "waiting" ? "等待手动启动" : job.status === "queued" ? "等待可用并发名额" : "任务已停止";
  progress.appendChild(el("span", {class: "queue-job-timing", text: timing}));
  if (job.error && job.error !== "cancel requested") progress.appendChild(el("span", {class: "queue-job-error", text: job.error, title: job.error}));
  item.appendChild(progress);
  item.appendChild(jobActionButtons(job));
  return item;
}

async function batchJobAction(action) {
  if (state.batchPending) return;
  const targets = eligibleJobs(action);
  if (!targets.length) return;
  if (["delete", "cancel"].includes(action) && !confirm(action === "delete"
    ? `删除所选任务中可删除的 ${targets.length} 项记录和日志？源文件与输出媒体不会删除。`
    : `取消所选任务中可取消的 ${targets.length} 项？运行中的任务将停止。`)) return;
  state.batchPending = true;
  targets.forEach(job => state.jobActionsPending.add(job.id));
  renderQueue();
  if (state.detailJob && $("job-drawer").open) renderJobDetail(state.detailJob);
  let succeeded = 0;
  const errors = [];
  try {
    for (const job of targets) {
      try {
        await sendJobAction(job.id, action);
        succeeded++;
        state.checkedJobs.delete(job.id);
        if (action === "delete" && state.selectedJob === job.id) {closeJobDrawer(); state.selectedJob = null;}
      } catch (err) {errors.push(`${job.input.path.split("/").pop()}：${err.message}`);}
    }
    state.queueFeedback = `批量操作完成：成功 ${succeeded} 项${errors.length ? `，失败 ${errors.length} 项。${errors.join("；")}` : "。"}`;
    $("queue-message").textContent = state.queueFeedback;
  } finally {
    targets.forEach(job => state.jobActionsPending.delete(job.id));
    state.batchPending = false;
    state.jobsSignature = null;
    await refreshJobs();
    renderQueue();
    if (state.selectedJob && $("job-drawer").open) await loadJobDetail(state.selectedJob);
  }
}

function sendJobAction(id, action) {
  return api(`/jobs/${encodeURIComponent(id)}` + (action === "delete" ? "" : "/" + action),
    {method: action === "delete" ? "DELETE" : "POST", body: action === "delete" ? undefined : "{}"});
}

async function refreshJobs() {
  if (state.jobsLoading) return;
  state.jobsLoading = true;
  try {
    const data = await api("/jobs");
    state.jobs = data.jobs;
    state.jobsLoaded = true;
    $("queue-message").textContent = state.queueFeedback;
    state.jobCounts = data.counts || {};
    const knownIds = new Set(state.jobs.map(job => job.id));
    for (const id of state.checkedJobs) if (!knownIds.has(id)) state.checkedJobs.delete(id);
    const counts = state.jobCounts;
    const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
    const active = ["probing", "running", "finalizing"].reduce((sum, key) => sum + (counts[key] || 0), 0);
    $("queue-count").textContent = String(total);
    $("queue-summary").textContent = `待启动 ${counts.waiting || 0} · 排队 ${counts.queued || 0} · 执行 ${active} · 暂停 ${counts.paused || 0} · 完成 ${counts.succeeded || 0} · 失败/中断 ${(counts.failed || 0) + (counts.interrupted || 0)} · 已取消 ${counts.canceled || 0}（全库统计）`;
    $("btn-refresh-jobs").disabled = state.batchPending;
    if (total === 0 && state.selectedJob) {
      state.selectedJob = null;
      state.detailRequest++;
      $("job-detail").replaceChildren(el("p", {class: "hint", text: "选择任务查看进度与日志。"}));
      $("job-log").textContent = $("progress-label").textContent = "—";
      $("progress-bar").style.width = "0%";
      $("detail-message").textContent = "";
      $("btn-cancel").disabled = true;
    }
    renderQueue();
    const pending = active + (counts.queued || 0) + (counts.waiting || 0) + (counts.paused || 0);
    const queuePill = $("queue-status");
    if (pending) { queuePill.className = "pill pill-running"; queuePill.textContent = `队列 ${pending} 个待完成`; }
    else { queuePill.className = "pill pill-idle"; queuePill.textContent = "队列空闲"; }
  } catch (err) {
    $("queue-message").textContent = (state.jobsLoaded ? "刷新失败，保留上次队列：" : "无法读取任务队列：") + err.message;
    $("queue-status").className = "pill pill-warn";
    $("queue-status").textContent = "队列状态未知";
  } finally {
    state.jobsLoading = false;
  }
}

function jobActionButtons(job) {
  const box=el("div",{class:"job-controls"});
  const labels={start:job.status==="paused"?"继续":job.status==="waiting"?"启动":"重试",pause:"暂停",cancel:"取消",delete:"删除"};
  for(const action of job.actions||[]){
    const button=el("button",{type:"button",class:"btn btn-small"+(action==="delete"?" btn-danger":""),"aria-label":`${labels[action]}任务：${job.input?.path || job.id}`,text:labels[action],"data-job-id":job.id,"data-focus-kind":action,onclick:()=>jobAction(job.id,action)});
    button.disabled=state.jobActionsPending.has(job.id);box.appendChild(button);
  }
  return box;
}

async function jobAction(id,action) {
  if(state.jobActionsPending.has(id))return;
  if(action==="delete"&&!confirm("删除此任务记录和日志？源文件与输出媒体不会删除。"))return;
  state.jobActionsPending.add(id);
  document.querySelectorAll(`[data-job-id="${id}"]`).forEach(row => row.closest(".job-row")?.querySelectorAll(".job-controls button").forEach(button=>{button.disabled=true;}));
  if(state.detailJob?.id===id && $("job-drawer").open)renderJobDetail(state.detailJob);
  try{
    await sendJobAction(id, action);
    state.queueFeedback = "";
    if (action === "delete") state.checkedJobs.delete(id);
    if(action==="delete"&&state.selectedJob===id){closeJobDrawer();state.selectedJob=null;}
    else if(state.selectedJob===id)await loadJobDetail(id);
    await refreshJobs();
  }catch(err){
    const box=state.selectedJob===id&&$("job-drawer").open?$("detail-message"):$("queue-message");box.textContent="操作失败："+err.message;
  }finally{
    state.jobActionsPending.delete(id);state.jobsSignature=null;
    renderQueue();
    if(state.detailJob?.id===id && $("job-drawer").open)renderJobDetail(state.detailJob);
  }
}

async function loadJobDetail(id) {
  if (!$("job-drawer").open) return;
  const request = ++state.detailRequest;
  try {
    const job = await api(`/jobs/${id}`);
    if (request !== state.detailRequest || state.selectedJob !== id || !$("job-drawer").open) return;
    renderJobDetail(job);
    const logs = await api(`/jobs/${id}/logs`);
    if (request !== state.detailRequest || state.selectedJob !== id || !$("job-drawer").open) return;
    $("detail-message").textContent = "";
    $("btn-cancel").disabled = state.canceling || !["queued", "probing", "running", "finalizing"].includes(job.status);
    const text = logs.logs.map((l) => `[${l.ts}] ${l.level.toUpperCase()} ${l.message}`).join("\n") || "—";
    if ($("job-log").textContent !== text) {
      const scroll = $("job-log").scrollTop;
      $("job-log").textContent = text;
      $("job-log").scrollTop = scroll;
    }
  } catch (err) {
    if (request === state.detailRequest && state.selectedJob === id) $("detail-message").textContent = "详情刷新失败：" + err.message;
  }
}

function renderJobDetail(job) {
  state.detailJob = job;
  $("detail-source").textContent = job.input.path.split("/").pop();
  $("detail-status").textContent = jobStatusLabel(job);
  $("detail-status").className = "status-tag status-" + job.status;
  $("detail-error").hidden = !job.error;
  $("detail-error").textContent = job.error || "";
  const controls = JSON.stringify([job.actions, state.jobActionsPending.has(job.id)]);
  if (controls !== state.detailControls) { state.detailControls=controls; $("detail-actions").replaceChildren(jobActionButtons(job)); }
  const box = $("job-detail");
  const grid = el("div", { class: "meta-grid" });
  const rows = [
    ["状态", JOB_STATUS_LABELS[job.status] || job.status],
    ["预设", job.preset_name || job.preset_id],
    ["容器", job.container || "auto"],
    ["输入", job.input.path],
    ["输出", job.output.path],
    ["标题数", job.title_count != null ? String(job.title_count) : "—"],
    ["开始", job.started_at || "—"],
    ["结束", job.finished_at || "—"],
  ];
  if (job.status === "paused") rows.push(["暂停", job.paused_from && ["probing", "running", "finalizing"].includes(job.paused_from) ? "原地暂停，保留进程与并发名额" : "未开始执行，可启动"]);
  for (const [label, value] of rows) {
    grid.appendChild(el("div", {}, [el("b", { text: label }), document.createTextNode(String(value))]));
  }
  const meta = JSON.stringify(rows);
  if (meta !== state.detailMeta) { state.detailMeta=meta; box.replaceChildren(grid); }
  const specText=JSON.stringify({spec:job.spec,args:job.args},null,2);
  if($("detail-spec").textContent!==specText)$("detail-spec").textContent=specText;
  const pct = jobPercent(job);
  $("progress-bar").style.width = pct + "%";
  const speed = job.speed != null ? ` · ${job.speed} fps` : "";
  const eta = job.eta_seconds != null ? ` · 剩余 ${formatDuration(job.eta_seconds)}` : "";
  $("progress-label").textContent = `${pct}%${speed}${eta}`;
  $("btn-cancel").disabled = ["queued", "probing", "running", "finalizing"].includes(job.status) ? false : true;
  $("btn-cancel").dataset.jobId = job.id;
}

async function cancelJob() {
  const id = $("btn-cancel").dataset.jobId;
  if (!id || state.canceling) return;
  state.canceling = true;
  $("btn-cancel").disabled = true;
  try {
    await api(`/jobs/${id}/cancel`, { method: "POST", body: "{}" });
    if (state.selectedJob === id) await loadJobDetail(id);
    await refreshJobs();
  } catch (err) {
    if (state.selectedJob === id && $("job-drawer").open) $("detail-message").textContent = "取消失败：" + err.message;
  } finally {
    state.canceling = false;
    if (state.selectedJob === id && $("job-drawer").open && state.detailJob?.id === id) {
      $("btn-cancel").disabled = !["queued", "probing", "running", "finalizing"].includes(state.detailJob.status);
    }
  }
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
    if (!document.hidden && Date.now() - state.resourcesAt >= 5000) refreshResources();
    await refreshJobs();
    if (state.selectedJob && $("job-drawer").open) await loadJobDetail(state.selectedJob);
  }, 2000);
}

/* ---------- events ---------- */
function wireEvents() {
  $("queue-search").addEventListener("input", () => {state.queueSearch = $("queue-search").value; renderQueue();});
  $("queue-select-all").addEventListener("change", () => {
    const checked = $("queue-select-all").checked;
    for (const job of filteredJobs()) {
      if (checked) state.checkedJobs.add(job.id); else state.checkedJobs.delete(job.id);
    }
    renderQueue();
  });
  $("btn-clear-queue-filters").addEventListener("click", () => {
    state.queueFilter = "all"; state.queueSearch = ""; $("queue-search").value = ""; renderQueue(); $("queue-search").focus();
  });
  $("btn-refresh-jobs").addEventListener("click", refreshJobs);
  for (const action of ["start", "pause", "cancel", "delete"]) $("btn-batch-" + action).addEventListener("click", () => batchJobAction(action));
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      document.querySelector(`.tab-panel[data-panel="${tab.dataset.tab}"]`).classList.add("active");
    });
  });
  $("btn-open-settings").addEventListener("click", openSettingsDrawer);
  $("btn-close-settings").addEventListener("click", () => {if(!state.settingsSaving)$("settings-drawer").close();});
  $("settings-drawer").addEventListener("cancel",event=>{if(state.settingsSaving)event.preventDefault();});
  $("settings-drawer").addEventListener("close",()=>{syncDialogLock();$("btn-open-settings").focus();});
  $("btn-save-settings").addEventListener("click",saveRuntimeSettings);
  $("btn-name-preview").addEventListener("click",previewOutputName);
  $("btn-save-template").addEventListener("click",saveTaskTemplate);
  $("task-template-select").addEventListener("change",()=>{
    const identity=choiceValue($("task-template-select"));
    if(identity)applyTaskTemplate(identity);
    else {state.templateEditing=null;$("template-name").value="";$("template-description").value="";}
  });
  $("btn-new-template").addEventListener("click",()=>{
    $("settings-drawer").close();openCreateTask();state.templateEditing=null;$("template-name").value="";$("template-description").value="";
  });
  $("output-name").addEventListener("input",()=>{state.namingRequest++;});
  $("btn-open-system").addEventListener("click", openSystemDrawer);
  $("btn-close-system").addEventListener("click", closeSystemDrawer);
  $("system-drawer").addEventListener("close", () => { syncDialogLock(); $("btn-open-system").focus(); });
  $("system-drawer").addEventListener("click", event => {
    if (event.target !== $("system-drawer")) return;
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeSystemDrawer();
  });
  $("btn-close-job").addEventListener("click", closeJobDrawer);
  $("job-drawer").addEventListener("close", () => {
    state.detailRequest++;
    syncDialogLock();
    const button = [...$("job-list").querySelectorAll("button")].find(n => n.dataset.jobId === state.selectedJob);
    (button || $("btn-create-task")).focus();
  });
  $("job-drawer").addEventListener("click", event => {
    if (event.target !== $("job-drawer")) return;
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeJobDrawer();
  });
  $("btn-refresh-codecs").addEventListener("click", refreshCodecs);
  document.addEventListener("visibilitychange", () => {if (!document.hidden) refreshResources();});
  $("btn-create-task").addEventListener("click", openCreateTask);
  $("btn-close-create").addEventListener("click", closeCreateTask);
  $("create-task-dialog").addEventListener("cancel", event => { event.preventDefault(); closeCreateTask(); });
  $("create-task-dialog").addEventListener("close", () => {
    syncDialogLock();
    $("btn-create-task").focus();
  });
  $("btn-select-file").addEventListener("click", openFilePicker);
  $("btn-close-files").addEventListener("click", closeFilePicker);
  $("file-picker-dialog").addEventListener("cancel", event => { event.preventDefault(); closeFilePicker(); });
  $("file-picker-dialog").addEventListener("close", () => {
    state.browserRequest++;
    syncDialogLock();
    $("btn-select-file").focus();
  });
  $("btn-refresh-system").addEventListener("click", refreshSystem);
  const settings = document.querySelector('[data-page="encode"]');
  settings.addEventListener("input", settingsChanged);
  settings.addEventListener("change", settingsChanged);
  $("btn-validate").addEventListener("click", validateSettings);

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
    syncDialogLock();
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
    if (name && !state.runtimeSettings) {
      const base = name.replace(/\.[^.]+$/, "");
      const ext = choiceValue($("container-select")) === "mkv" ? "mkv" : "mp4";
      $("output-name").value = `${base}.${ext}`;
    }
  });
  $("link-matrix").addEventListener("click", (e) => { e.preventDefault(); $("matrix-dialog").showModal(); syncDialogLock(); });
  $("matrix-dialog").addEventListener("close", syncDialogLock);
  $("btn-close-matrix").addEventListener("click", () => $("matrix-dialog").close());
}

document.addEventListener("DOMContentLoaded", () => {
  initializeChoices();
  wireEvents();
  boot();
});
