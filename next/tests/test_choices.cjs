/* Choice rendering regression tests. Run with node --test tests/test_choices.cjs. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.className = "";
    this.listeners = {};
    this.checked = false;
    this._value = "";
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(" ").filter(Boolean), ...names])].join(" "); },
      remove: (...names) => { this.className = this.className.split(" ").filter(n => !names.includes(n)).join(" "); },
      contains: name => this.className.split(" ").includes(name),
      toggle: (name, force) => { if (force) this.classList.add(name); else this.classList.remove(name); },
    };
  }
  setAttribute(key, value) { this.attributes[key] = String(value); this[key] = String(value); }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.closed = true; this.open = false; this.dispatchEvent({type: "close"}); }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  dispatchEvent(event) {
    for (const callback of this.listeners[event.type] || []) callback(event);
    if (event.bubbles && this.parentNode) this.parentNode.dispatchEvent(event);
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  replaceChildren(...children) { this.children = []; children.forEach(c => this.appendChild(c)); }
  set innerHTML(value) { assert.equal(value, ""); this.replaceChildren(); }
  get options() { return this.children.filter(c => c.tagName === "option"); }
  get value() {
    if (this.tagName === "select") return this.options.find(o => o.value === this._value)?.value ?? this.options[0]?.value ?? "";
    return this._value;
  }
  set value(value) { this._value = String(value); }
  querySelectorAll(selector) {
    if (selector.includes(",")) return selector.split(",").flatMap(s => this.querySelectorAll(s.trim()));
    const matches = node => selector === "select" ? node.tagName === "select"
      : selector === "input:checked" ? node.tagName === "input" && node.checked
      : selector === "input[type=radio]" ? node.tagName === "input" && node.type === "radio"
      : selector.startsWith(".") ? node.classList.contains(selector.slice(1)) : node.tagName === selector;
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1]; }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
}

function harness() {
  const controls = new Map();
  const body = new Element("body");
  const context = vm.createContext({
    document: {
      body,
      createElement: tag => new Element(tag),
      createTextNode: text => { const node = new Element("#text"); node.textContent = text; return node; },
      addEventListener() {},
      querySelectorAll: () => [],
      querySelector: selector => selector === "dialog[open]" ? [...controls.values()].find(n => n.open) || null : null,
      getElementById: id => controls.get(id),
    },
    Event: class { constructor(type, init = {}) { this.type = type; this.bubbles = Boolean(init.bubbles); } },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/ui-text.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/settings.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8"), context);
  vm.runInContext('globalThis.realRefreshEngineAvailability=refreshEngineAvailability; state.engineAvailability=Object.keys(ENGINE_LABELS).map(id=>({id,selectable:true,status:"available"})); refreshEngineAvailability=async()=>{}',context);
  const control = (id = "control") => {
    const node = new Element("div");
    node.dataset.labelId = `${id}-label`;
    controls.set(id, node);
    return node;
  };
  return { context, control, controls };
}

test("encoder summaries retain independent values and literal names", () => {
  const { context, control } = harness();
  vm.runInContext('state.encoderCatalog = { audio: [{ id: "aac", name: "AAC <test>", device: "cpu", status: "available" }, { id: "opus", name: "Opus", device: "cpu", status: "available" }] }', context);
  const first = control("first"), second = control("second");
  first.className = "a-encoder";
  context.initializeEncoderControl(first, "audio", "aac");
  context.initializeEncoderControl(second, "audio", "aac");
  context.setChoiceValue(second, "opus");
  assert.equal(context.choiceValue(first), "aac");
  assert.equal(context.choiceValue(second), "opus");
  assert.equal(first.querySelector(".encoder-trigger-name").textContent, "AAC <test>");
  assert.ok(first.classList.contains("a-encoder"));
  assert.equal(first.querySelector("select"), null);
});

const items = count => Array.from({ length: count }, (_, i) => String(i));

test("an unselectable encoder never reaches the control", () => {
  // The CLI rejects (auto, dtshd) or silently reinterprets (dts) these values,
  // so the card explains them but must not be selectable.
  const { context, control } = harness();
  const node = control("target");
  node.className = "a-encoder";
  node.isConnected = true;
  const drawer = control("encoder-drawer");
  drawer.close = () => { drawer.closed = true; };
  context.initializeEncoderControl(node, "audio", "aac");

  const seen = vm.runInContext(`(() => {
    state.encoderCatalog = { audio: [
      { id: "aac", name: "AAC", device: "cpu", status: "available", selectable: true },
      { id: "auto", name: "AUTO", device: "passthrough", status: "unsupported", selectable: false },
      { id: "dts", name: "DTS", device: "passthrough", status: "passthrough_only", selectable: false },
    ] };
    state.encoderTarget = document.getElementById("target");
    state.encodersLoading = false;
    state.encodersError = "";
    const out = [];
    for (const id of ["auto", "dts", "aac"]) {
      selectEncoder(state.encoderCatalog.audio.find(e => e.id === id));
      out.push(choiceValue(state.encoderTarget));
    }
    return out;
  })()`, context);

  // The two unselectable cards leave the value alone; the selectable one lands.
  // (String compare: the array is built inside the VM realm.)
  assert.equal(seen.join(","), "aac,aac,aac");
});

test("catalog-only audio values are labelled, not offered", () => {
  const { context } = harness();
  const labels = vm.runInContext(`({
    passthrough: ENCODER_STATUS_LABELS.passthrough_only,
    unsupported: ENCODER_STATUS_LABELS.unsupported,
    installed: encoderInstalledLabel({ status: "passthrough_only", installed: false }),
  })`, context);
  assert.equal(labels.passthrough, "仅支持直通");
  assert.equal(labels.unsupported, "当前引擎未提供");
  assert.equal(labels.installed, "不适用（直通或不编码）");
});

test("2/4 use segments, 5/8 use tags, 9 use a real dropdown", () => {
  for (const [count, style] of [[2, "segment"], [4, "segment"], [5, "tags"], [8, "tags"], [9, "dropdown"]]) {
    const { context, control } = harness();
    const node = control();
    context.renderChoice(node, items(count));
    assert.ok(node.classList.contains(`choice-${style}`));
    assert.equal(node.querySelectorAll("input[type=radio]").length, count > 8 ? 0 : count);
    if (count > 8) assert.equal(node.querySelector("select").options.length, count);
  }
});

test("empty lists and single options do not invent choices", () => {
  const { context, control } = harness();
  const node = control();
  context.renderChoice(node, []);
  assert.ok(node.classList.contains("choice-empty"));
  assert.equal(context.choiceValue(node), "");
  context.renderChoice(node, ["auto"]);
  assert.equal(node.querySelectorAll("input[type=radio]").length, 1);
  assert.equal(context.choiceValue(node), "auto");
});

test("empty-string defaults remain distinguishable from explicit auto", () => {
  const { context, control } = harness();
  const node = control();
  context.renderChoice(node, [{ value: "", label: "默认（自动）" }, { value: "auto", label: "自动（auto）" }], { value: "" });
  assert.equal(context.choiceValue(node), "");
  context.setChoiceValue(node, "auto");
  assert.equal(context.choiceValue(node), "auto");
  assert.equal(node.querySelectorAll("input:checked").length, 1);
});

test("rerender preserves valid selection across radio/dropdown modes", () => {
  const { context, control } = harness();
  const node = control();
  node.className = "a-mixdown";
  context.renderChoice(node, items(4), { value: "3" });
  context.renderChoice(node, items(9));
  assert.equal(context.choiceValue(node), "3");
  context.renderChoice(node, items(5));
  assert.equal(context.choiceValue(node), "3");
  assert.ok(node.classList.contains("a-mixdown"));
  context.renderChoice(node, ["auto"]);
  assert.equal(context.choiceValue(node), "auto");
});

test("track groups use unique stable names and independent selected values", () => {
  const { context, control } = harness();
  const first = control("first"), second = control("second");
  context.renderChoice(first, items(8));
  context.renderChoice(second, items(8));
  const name = first.dataset.choiceName;
  assert.notEqual(name, second.dataset.choiceName);
  context.setChoiceValue(first, "4");
  context.setChoiceValue(second, "7");
  context.renderChoice(first, items(8));
  assert.equal(first.dataset.choiceName, name);
  assert.equal(context.choiceValue(first), "4");
  assert.equal(context.choiceValue(second), "7");
});

test("radio and select change events bubble to the stable control", () => {
  const { context, control } = harness();
  const node = control();
  let changes = 0;
  node.addEventListener("change", () => changes++);
  context.renderChoice(node, items(4));
  node.querySelector("input[type=radio]").dispatchEvent({ type: "change", bubbles: true });
  context.renderChoice(node, items(9));
  node.querySelector("select").dispatchEvent({ type: "change", bubbles: true });
  assert.equal(changes, 2);
});

test("storage labels remain literal and short roots use tags", () => {
  const { context, control } = harness();
  const node = control();
  context.renderChoice(node, [{ value: "auto", label: "NAS <archive>" }, { value: "out", label: "输出" }], { tags: true });
  assert.ok(node.classList.contains("choice-tags"));
  assert.equal(node.querySelectorAll("span")[0].textContent, "NAS <archive>");
});

test("preset payload inherits untouched defaults and keeps explicit rate-control changes", () => {
  const { context, control } = harness();
  const choices = {
    "container-select": "auto", "dim-crop-mode": "auto", "dim-anamorphic": "auto",
    "flt-deinterlace": "off", "flt-denoise": "off", "flt-detelecine": "off", "flt-rotate": "off",
    "video-encoder": "x264", "video-quality-type": "rf", "video-preset": "medium",
    "video-tune": "", "video-profile": "", "video-level": "", "video-framerate": "auto",
    "sub-behavior": "none", "chap-mode": "none",
  };
  for (const [id, value] of Object.entries(choices)) context.renderChoice(control(id), [value]);
  for (const id of ["dim-width", "dim-height", "crop-top", "crop-bottom", "crop-left", "crop-right", "dim-modulus",
                    "video-quality", "video-bitrate", "sub-burn", "sub-default", "sub-srt", "sub-srt-burn", "sub-srt-default", "chap-file",
                    "flt-grayscale", "flt-hflip", "flt-chroma", "flt-lapsharp", "flt-unsharp", "video-two-pass", "video-turbo", "sub-forced", "audio-tracks"]) control(id);
  control("video-quality").value = "22";
  vm.runInContext('state.presetSelection = {source: "imported", preset: {id: "uuid", name: "Same Name"}}; state.presetBaseline = buildSpec(true)', context);
  assert.deepEqual(JSON.parse(JSON.stringify(context.buildSpec())), {version: 1, preset: "Same Name", title: 1});
  // A changed quality explicitly overrides rate control, not the preset encoder.
  context.document.getElementById("video-quality").value = "20";
  const changed = JSON.parse(JSON.stringify(context.buildSpec()));
  assert.equal(changed.video.quality, 20);
  assert.equal(changed.video.quality_type, "rf");
  assert.equal(changed.video.two_pass, false);
  assert.equal(changed.video.encoder, undefined);
  // Removing a formerly non-empty control is an explicit reset, not omission.
  context.renderChoice(context.document.getElementById("video-preset"), ["medium", ""]);
  context.setChoiceValue(context.document.getElementById("video-preset"), "");
  assert.equal(JSON.parse(JSON.stringify(context.buildSpec())).video.preset, null);
  // Rotation and flip travel together; off/false cannot erase the other one.
  context.renderChoice(context.document.getElementById("flt-rotate"), ["off", "90"]);
  context.setChoiceValue(context.document.getElementById("flt-rotate"), "90");
  const rotated = JSON.parse(JSON.stringify(context.buildSpec()));
  assert.equal(rotated.filters.rotate, "90");
  assert.equal(rotated.filters.hflip, false);
  context.document.getElementById("sub-srt").value = "sub.srt";
  context.document.getElementById("sub-srt-burn").checked = true;
  assert.equal(JSON.parse(JSON.stringify(context.buildSpec())).subtitles.srt_burn, true);
});

test("queue submits the imported UUID instead of its display name", async () => {
  const { context, control } = harness();
  control("output-root-select");
  context.renderChoice(context.document.getElementById("output-root-select"), ["out"]);
  control("output-name").value = "out.mp4";
  control("btn-queue");
  control("btn-validate");
  for (const id of ["spec-validation", "validation-message", "validation-details", "validation-json", "create-message", "create-task-dialog", "btn-create-task", "btn-close-create", "btn-select-file", "btn-scan", "source-summary"]) control(id);
  vm.runInContext(`
    state.selectedFile = { root: "media", path: "movie.mp4" };
    state.caps = {engine: {available: true}};
    state.presetSelection = {source: "imported", preset: {id: "stable-uuid", name: "Same Name"}};
    buildSpec = () => ({preset: "Same Name", version: 1});
    api = async (path, options) => {
      globalThis.calls = [...(globalThis.calls || []), path];
      if (path === "/spec/validate") { globalThis.validatedBody = JSON.parse(options.body); return {valid: true, args: [], args_scope: "overrides"}; }
      globalThis.sentBody = JSON.parse(options.body); return {id: "job"};
    };
    refreshJobs = async () => {};
    loadJobDetail = async () => {};
  `, context);
  await context.queueJob();
  assert.equal(context.sentBody.preset_id, "stable-uuid");
  assert.equal(context.sentBody.spec.preset, "Same Name");
  assert.equal(context.validatedBody.preset_id, "stable-uuid");
  assert.equal(context.calls.join(","), "/spec/validate,/jobs");
});

function settingsHarness() {
  const h = harness();
  const choices = {
    "container-select": ["auto", "mkv"], "output-root-select": ["out"],
    "dim-crop-mode": ["auto", "none", "custom"], "dim-anamorphic": ["auto", "none", "loose"], "dim-modulus": ["", "2"],
    "flt-deinterlace": ["off", "bob"], "flt-denoise": ["off", "nlmeans"], "flt-detelecine": ["off", "default"], "flt-rotate": ["off", "90"],
    "video-encoder": ["x264", "av1"], "video-quality-type": ["rf", "abr", "lossless"], "video-preset": ["medium", ""],
    "video-tune": [""], "video-profile": [""], "video-level": [""], "video-framerate": ["auto"],
    "sub-behavior": ["none", "burn", "default", "add-first"], "chap-mode": ["auto", "none", "markers"],
  };
  for (const [id, options] of Object.entries(choices)) h.context.renderChoice(h.control(id), options);
  for (const id of ["dim-width", "dim-height", "crop-top", "crop-bottom", "crop-left", "crop-right", "video-quality", "video-bitrate",
                    "sub-burn", "sub-default", "sub-forced", "sub-srt", "sub-srt-burn", "sub-srt-default", "chap-file",
                    "flt-grayscale", "flt-hflip", "flt-chroma", "flt-lapsharp", "flt-unsharp", "video-two-pass", "video-turbo", "audio-tracks",
                    "btn-validate", "btn-queue", "spec-validation", "validation-message", "validation-details", "validation-json", "output-name"]) h.control(id);
  for (const id of ["create-message", "create-task-dialog", "file-picker-dialog", "btn-create-task", "btn-close-create", "btn-select-file", "btn-scan", "source-summary", "browser", "browser-message", "current-path", "root-select", "system-info", "system-message", "storage-status", "engine-status", "engine-banner", "queue-count", "queue-summary", "queue-message", "queue-empty", "job-list-note", "job-list", "detail-message", "btn-cancel", "job-log", "job-detail", "progress-label", "progress-bar", "job-drawer", "btn-close-job", "resource-message", "resource-info", "disk-status", "codec-status", "system-summary", "summary-message", "system-drawer", "btn-close-system", "btn-open-system", "detail-source", "detail-status", "detail-actions", "detail-error", "detail-spec", "task-template-select", "setting-default-template", "template-name", "template-description", "template-message", "btn-save-template", "settings-message", "btn-save-settings", "setting-concurrency", "setting-auto-start", "setting-timeout", "setting-output-root", "setting-name-template", "name-preview", "preset-current-source", "preset-current-name", "preset-hint"]) h.control(id);
  for (const id of ["queue-filters", "queue-search", "queue-select-all", "queue-selection-note", "queue-empty-title", "queue-empty-description", "btn-clear-queue-filters", "btn-refresh-jobs", "btn-batch-start", "btn-batch-pause", "btn-batch-cancel", "btn-batch-delete"]) h.control(id);
  h.controls.get("queue-empty").hidden = true;
  h.context.renderChoice(h.controls.get("root-select"), ["media", "out"]);
  h.controls.get("video-quality").value = "22";
  h.controls.get("output-name").value = "out.mkv";
  vm.runInContext('state.caps = {engine: {available: true}}', h.context);
  return h;
}

const plain = value => JSON.parse(JSON.stringify(value));

test("capability controls constrain quality, passes, crop and sharpening", () => {
  const {context: c, controls: nodes} = settingsHarness();
  c.syncControls();
  assert.equal(nodes.get("video-two-pass").disabled, true);
  assert.equal(nodes.get("video-bitrate").disabled, true);
  assert.equal(nodes.get("crop-top").disabled, true);
  c.setChoiceValue(nodes.get("video-quality-type"), "abr");
  nodes.get("video-two-pass").checked = true;
  c.syncControls();
  assert.equal(nodes.get("video-quality").disabled, true);
  assert.equal(nodes.get("video-turbo").disabled, false);
  c.setChoiceValue(nodes.get("video-quality-type"), "rf");
  c.syncControls();
  assert.equal(nodes.get("video-two-pass").checked, false);
  c.setChoiceValue(nodes.get("video-quality-type"), "lossless");
  c.setChoiceValue(nodes.get("video-encoder"), "av1");
  c.syncControls();
  assert.equal(c.choiceValue(nodes.get("video-quality-type")), "rf");
  nodes.get("flt-lapsharp").checked = nodes.get("flt-unsharp").checked = true;
  c.syncControls(nodes.get("flt-unsharp"));
  assert.equal(nodes.get("flt-lapsharp").checked, false);
  nodes.get("crop-top").value = "4";
  assert.equal(plain(c.buildSpec()).dimensions.crop_top, undefined);
});

test("audio defaults to auto and copy omits encoding controls", () => {
  const {context: c, controls: nodes} = settingsHarness();
  vm.runInContext('state.audioOptions = {audio_mixdowns: ["5point1", "auto", "stereo"]}', c);
  c.addAudioTrack();
  const track = nodes.get("audio-tracks").children[0];
  assert.equal(c.choiceValue(track.querySelector(".a-mixdown")), "auto");
  c.setChoiceValue(track.querySelector(".a-encoder"), "copy");
  c.syncControls();
  assert.equal(track.querySelector(".a-mixdown").disabled, true);
  assert.deepEqual(plain(c.buildSpec()).audio.tracks[0], {encoder: "copy", source: "auto"});
});

test("subtitle and chapter controls omit inactive values and resolve conflicts", () => {
  const {context: c, controls: n} = settingsHarness();
  n.get("sub-burn").value = "1";
  n.get("chap-file").value = "chapter.csv";
  c.syncControls();
  assert.equal(plain(c.buildSpec()).subtitles.burn_track, undefined);
  assert.equal(plain(c.buildSpec()).chapters.marker_file, undefined);
  c.setChoiceValue(n.get("sub-behavior"), "burn");
  n.get("sub-srt").value = "x.srt";
  n.get("sub-srt-burn").checked = true;
  c.syncControls(n.get("sub-srt-burn"));
  assert.equal(c.choiceValue(n.get("sub-behavior")), "add-first");
  assert.equal(n.get("sub-burn").value, "");
  assert.equal(plain(c.buildSpec()).subtitles.srt_burn, true);
  n.get("sub-srt").value = "";
  assert.equal(plain(c.buildSpec()).subtitles.srt_burn, false);
});

test("preset crop and SRT changes do not override unrelated defaults", () => {
  const {context: c, controls: n} = settingsHarness();
  vm.runInContext('state.presetSelection = {source: "imported", preset: {id: "uuid", name: "Same"}}; state.presetBaseline = buildSpec(true)', c);
  c.setChoiceValue(n.get("dim-crop-mode"), "custom");
  n.get("crop-top").value = "0";
  n.get("sub-srt").value = "x.srt";
  const spec = plain(c.buildSpec());
  assert.deepEqual(spec.dimensions, {crop_mode: "custom", crop_top: 0});
  assert.equal(spec.subtitles.behavior, undefined);
  assert.equal(spec.subtitles.forced_only, undefined);
  n.get("dim-width").value = "abc";
  assert.throws(() => c.buildSpec(), /无效数值/);
});

test("prevalidation works without source and forwards auth through api", async () => {
  const {context: c, controls: n} = settingsHarness();
  c.fetch = async (url, opts) => {
    c.request = {url, opts};
    return {ok: true, text: async () => JSON.stringify({valid: true, args: ["--title", "1"], args_scope: "custom"})};
  };
  vm.runInContext('state.token = "test-token"', c);
  await c.validateSettings();
  assert.equal(c.request.opts.headers.Authorization, "Bearer test-token");
  assert.equal(c.request.url, "/api/v1/spec/validate");
  assert.equal(n.get("spec-validation").dataset.status, "passed");
  assert.equal(n.get("btn-queue").disabled, true);
  c.settingsChanged();
  assert.equal(n.get("spec-validation").dataset.status, "stale");
});

test("validation failures prevent queue submission and restore buttons", async () => {
  for (const failure of ["400: bad parameters", "Network failure"]) {
    const {context: c, controls: n} = settingsHarness();
    vm.runInContext('state.selectedFile = {root: "media", path: "source.avi"}', c);
    const calls = [];
    c.api = async path => {calls.push(path); throw new Error(failure);};
    await c.queueJob();
    assert.deepEqual(calls, ["/spec/validate"]);
    assert.equal(n.get("spec-validation").dataset.status, "failed");
    assert.equal(n.get("btn-queue").disabled, false);
  }
});

test("pending validation ignores repeated clicks and stale results never enqueue", async () => {
  const {context: c, controls: n} = settingsHarness();
  vm.runInContext('state.selectedFile = {root: "media", path: "source.avi"}', c);
  let finish;
  const calls = [];
  c.api = path => { calls.push(path); return new Promise(resolve => { finish = resolve; }); };
  const pending = c.queueJob();
  await c.validateSettings();
  await c.queueJob();
  assert.deepEqual(calls, ["/spec/validate"]);
  n.get("video-quality").value = "20";
  c.settingsChanged();
  finish({valid: true, args: []});
  await pending;
  assert.equal(n.get("spec-validation").dataset.status, "stale");
  assert.deepEqual(calls, ["/spec/validate"]);
});

test("disabled stale numeric values do not block an unrelated validation", () => {
  const {context: c, controls: n} = settingsHarness();
  for (const id of ["video-bitrate", "crop-top", "sub-burn"]) n.get(id).value = "invalid";
  c.syncControls();
  assert.doesNotThrow(() => c.captureSettings());
  c.setChoiceValue(n.get("dim-crop-mode"), "custom");
  c.syncControls();
  assert.throws(() => c.captureSettings(), /无效数值/);
});

test("HTML invalid input blocks requests and engine refresh restores gating", async () => {
  const {context: c, controls: n} = settingsHarness();
  const input = n.get("dim-width");
  input.checkValidity = () => false;
  input.closest = () => null;
  c.document.querySelectorAll = () => [input];
  const calls = [];
  c.api = async path => { calls.push(path); return {engine: {available: false}, encoder_catalog: {}}; };
  await c.validateSettings();
  assert.deepEqual(calls, []);
  assert.equal(n.get("spec-validation").dataset.status, "failed");
  c.document.querySelectorAll = () => [];
  c.renderEncoderCards = () => {};
  vm.runInContext('state.selectedFile = {root: "media", path: "source.avi"}', c);
  await c.refreshEncoders();
  assert.equal(n.get("btn-queue").disabled, true);
  assert.equal(n.get("spec-validation").dataset.status, "stale");
});

test("preset width and chapter overrides preserve unrelated inherited settings", () => {
  const {context: c, controls: n} = settingsHarness();
  vm.runInContext('state.presetSelection = {source: "imported", preset: {id: "uuid", name: "Same"}}; state.presetBaseline = buildSpec(true)', c);
  n.get("dim-width").value = "320";
  c.setChoiceValue(n.get("chap-mode"), "markers");
  n.get("chap-file").value = "chapter.csv";
  assert.deepEqual(plain(c.buildSpec()).dimensions, {width: 320});
  assert.deepEqual(plain(c.buildSpec()).chapters, {mode: "markers", marker_file: "chapter.csv"});
  c.setChoiceValue(n.get("video-quality-type"), "abr");
  n.get("video-bitrate").value = "600";
  c.syncControls();
  assert.deepEqual(plain(c.buildSpec()).video, {quality_type: "abr", quality: null, bitrate_kbps: 600, two_pass: false, turbo: false});
});

test("creation is a modal draft, closing preserves settings and invalidates pending validation", async () => {
  const {context: c, controls: n} = settingsHarness();
  c.openCreateTask();
  assert.equal(n.get("create-task-dialog").open, true);
  assert.equal(c.document.body.classList.contains("dialog-open"), true);
  let finish;
  const calls = [];
  vm.runInContext('state.selectedFile = {root: "media", path: "clip.avi"}', c);
  c.api = path => {calls.push(path); return new Promise(resolve => {finish = resolve;});};
  const pending = c.queueJob();
  c.closeCreateTask();
  assert.equal(n.get("create-task-dialog").open, false);
  assert.equal(n.get("output-name").value, "out.mkv");
  finish({valid: true, args: []});
  await pending;
  assert.deepEqual(calls, ["/spec/validate"]);
  assert.equal(n.get("spec-validation").dataset.status, "stale");
  c.openCreateTask();
  assert.equal(n.get("output-name").value, "out.mkv");
});

test("posting prevents modal close, success clears source and selects the new job", async () => {
  const {context: c, controls: n} = settingsHarness();
  c.openCreateTask();
  vm.runInContext('state.selectedFile = {root: "media", path: "clip.avi"}', c);
  let finish, started;
  const posting = new Promise(resolve => {started = resolve;});
  c.api = async path => path === "/spec/validate" ? {valid: true, args: []} : new Promise(resolve => {finish = resolve; started();});
  c.refreshJobs = async () => {};
  c.loadJobDetail = async () => {};
  const pending = c.queueJob();
  await posting;
  c.closeCreateTask();
  assert.equal(n.get("create-task-dialog").open, true);
  assert.equal(n.get("btn-close-create").disabled, true);
  finish({id: "new-job"});
  await pending;
  assert.equal(n.get("create-task-dialog").open, false);
  assert.equal(vm.runInContext('state.selectedJob', c), "new-job");
  assert.equal(vm.runInContext('state.selectedFile', c), null);
  assert.equal(n.get("output-name").value, "");
  assert.equal(n.get("video-quality").value, "22");
});

test("creation failure stays in the modal with its draft and local error", async () => {
  const {context: c, controls: n} = settingsHarness();
  c.openCreateTask();
  vm.runInContext('state.selectedFile = {root: "media", path: "clip.avi"}', c);
  c.api = async path => { if (path === "/spec/validate") return {valid: true, args: []}; throw new Error("409 output exists"); };
  await c.queueJob();
  assert.equal(n.get("create-task-dialog").open, true);
  assert.match(n.get("create-message").textContent, /输出位置已被占用/);
  assert.equal(n.get("output-name").value, "out.mkv");
});

test("dialog lock remains while the parent is open", () => {
  const {context: c, controls: n} = settingsHarness();
  n.get("create-task-dialog").showModal();
  n.get("file-picker-dialog").showModal();
  c.closeFilePicker();
  c.syncDialogLock();
  assert.equal(c.document.body.classList.contains("dialog-open"), true);
  c.closeCreateTask();
  c.syncDialogLock();
  assert.equal(c.document.body.classList.contains("dialog-open"), false);
});

test("directory responses cannot replace a newer root or a closed picker", async () => {
  const {context: c, controls: n} = settingsHarness();
  n.get("file-picker-dialog").showModal();
  const finishes = [];
  c.api = () => new Promise(resolve => finishes.push(resolve));
  const first = c.browse("old");
  c.setChoiceValue(n.get("root-select"), "out");
  const second = c.browse("new");
  finishes[1]({path: "new", entries: [{name: "new.avi", is_dir: false}]});
  await second;
  finishes[0]({path: "old", entries: [{name: "old.avi", is_dir: false}]});
  await first;
  assert.equal(n.get("current-path").textContent, "/new");
  assert.equal(n.get("browser").children[0].tagName, "button");
  const third = c.browse("late");
  c.closeFilePicker();
  finishes[2]({path: "late", entries: []});
  await third;
  assert.equal(n.get("current-path").textContent, "/new");
});

test("scan responses for a replaced source are ignored", async () => {
  const {context: c, controls: n} = settingsHarness();
  vm.runInContext('state.selectedFile = {root: "media", path: "old.avi"}', c);
  let finish;
  c.api = () => new Promise(resolve => {finish = resolve;});
  let rendered = false;
  c.renderScan = () => {rendered = true;};
  const pending = c.scanSource();
  c.selectFile("media", "new.avi", "new.avi");
  finish({scan: {titles: []}});
  await pending;
  assert.equal(rendered, false);
  assert.equal(n.get("btn-scan").disabled, false);
});

test("header keeps accessible settings only and engine status belongs to system overview", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
  const header = html.match(/<header class="topbar">([\s\S]*?)<\/header>/)[1];
  assert.match(header, /id="btn-open-settings"[^>]*aria-label="系统设置"/);
  assert.match(header, /<svg[^>]*aria-hidden="true"/);
  assert.doesNotMatch(header, /id="engine-status"|id="queue-status"/);
  const overview = html.match(/<section class="panel system-overview"([\s\S]*?)<\/section>/)[1];
  assert.match(overview, /id="engine-status"/);
  assert.doesNotMatch(html, /id="queue-status"/);
  const {context: c, controls: n} = settingsHarness();
  c.renderEngineStatus({available: true, version: "1.11.0", version_string: "HandBrake 1.11.0"});
  assert.equal(n.get("engine-status").textContent, "HandBrake 1.11.0");
  c.renderEngineStatus({available: false, notes: ["missing"]});
  assert.equal(n.get("engine-status").textContent, "未检测到 HandBrakeCLI");
  assert.equal(n.get("btn-queue").disabled, true);
});

function expandedSettingsHarness() {
  const h = settingsHarness();
  for (const id of ["settings-drawer","btn-close-settings","settings-content","settings-fields","settings-dirty","btn-reset-settings-section","setting-refresh","setting-queue-filter","setting-density","setting-notify-success","setting-notify-failure","setting-notify-sound","setting-notify-desktop","setting-collision","setting-active-slots","setting-root-note","settings-template-list","settings-storage-list","notification-permission","template-transfer-message","cleanup-message","btn-confirm-cleanup","template-import-file"]) h.control(id);
  for (const [id, options] of Object.entries({"setting-refresh":["2","5","10","30"],"setting-queue-filter":["all","pending","active","paused","completed","issues","canceled"],"setting-density":["standard","compact"],"setting-collision":["reject","rename"],"cleanup-days":["7","30","90"]})) h.context.renderChoice(h.controls.get(id) || h.control(id),options);
  h.controls.get("settings-fields").checkValidity = () => true;
  const storage = new Map();
  h.context.localStorage = {getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)};
  h.context.window = {isSecureContext:false};
  h.context.confirm = () => false;
  h.context.startPolling = () => {};
  h.context.renderQueue = () => {};
  vm.runInContext('state.roots=[{id:"out",label:"Out",path:"/out",read_only:false,available:true}]',h.context);
  h.context.api = async route => route === "/settings" ? {values:{max_concurrent_jobs:1,auto_start:true,job_timeout_seconds:0,default_output_root:"out",output_name_template:"{source}.{ext}",default_task_template_id:null,output_collision_policy:"reject"},defaults:{max_concurrent_jobs:1,auto_start:true,job_timeout_seconds:0,default_output_root:"out",output_name_template:"{source}.{ext}",default_task_template_id:null,output_collision_policy:"reject"},revision:0,active_slots:0} : route === "/task-templates" ? {templates:[]} : {path:"movie.mp4"};
  return {...h,storage};
}

test("categorised settings load failure disables save and late response cannot reopen", async () => {
  const {context:c,controls:n}=expandedSettingsHarness();
  c.api=async()=>{throw new Error("offline");};
  await c.Settings.open();
  assert.equal(n.get("settings-fields").disabled,true);
  assert.equal(n.get("btn-save-settings").disabled,true);
  assert.match(n.get("settings-message").textContent,/无法连接服务/);
  let finish;
  c.api=()=>new Promise(resolve=>{finish=resolve;});
  const pending=c.Settings.open();
  c.Settings.close();
  finish({values:{}});
  await pending;
  assert.equal(n.get("settings-drawer").open,false);
  assert.equal(n.get("settings-fields").disabled,true);
});

test("settings dirty close, save conflict and scoped reset preserve drafts", async () => {
  const {context:c,controls:n}=expandedSettingsHarness();
  await c.Settings.open();
  assert.match(n.get("settings-message").textContent,/服务配置/);
  c.setChoiceValue(n.get("setting-refresh"),"10");
  assert.equal(c.Settings.dirty(),true);
  assert.equal(c.Settings.canClose(),false);
  c.api=async()=>{throw new Error("409 revision conflict");};
  await c.Settings.save();
  assert.equal(c.Settings.dirty(),true);
  assert.match(n.get("settings-message").textContent,/原始错误|冲突/);
  c.Settings.selectSection("general");
  c.Settings.resetSection();
  assert.equal(c.choiceValue(n.get("setting-refresh")),"2");
  assert.equal(c.Settings.dirty(),false);
  n.get("setting-timeout").value="99";
  c.Settings.selectSection("output");c.Settings.resetSection();
  assert.equal(n.get("setting-timeout").value,"99");
});

test("settings saves service revision before browser preferences", async () => {
  const {context:c,controls:n,storage}=expandedSettingsHarness();
  await c.Settings.open();
  c.setChoiceValue(n.get("setting-refresh"),"5");
  let posted;
  c.api=async(route,opts)=>{posted=JSON.parse(opts.body);return {values:posted,revision:1,defaults:{},active_slots:0};};
  await c.Settings.save();
  assert.equal(posted.expected_revision,0);
  assert.equal(c.Settings.refreshSeconds(),5);
  assert.equal(JSON.parse(storage.get("cute-cat.preferences.v1")).refresh,5);
  assert.equal(c.Settings.dirty(),false);
});

test("completion polling starts at latest, advances cursor and does not replay duplicates", async () => {
  const {context:c,controls:n}=expandedSettingsHarness();
  const notices=n.has("completion-notices")?n.get("completion-notices"):new Element("div");n.set("completion-notices",notices);
  c.location={origin:"http://localhost:18087"};c.navigator={};c.setTimeout=()=>1;
  let initial=true, cursorQueries=[];
  c.api=async route=>{
    cursorQueries.push(route);
    if(initial){initial=false;return {events:[],cursor:10,has_more:false};}
    return {events:[{id:11,job_id:"a",status:"succeeded",source:"safe.mp4"}],cursor:11,has_more:false};
  };
  await c.Settings.pollEvents();
  assert.equal(notices.children.length,0);
  await c.Settings.pollEvents();
  assert.equal(notices.children.length,1);
  await c.Settings.pollEvents();
  assert.equal(notices.children.length,1);
  assert.match(cursorQueries[1],/after=10/);
  c.api=async()=>{throw new Error("offline");};
  await c.Settings.pollEvents();
  assert.equal(notices.children.length,1);
});

test("browser settings reject invalid preferences and seven settings categories exist", () => {
  const {context:c} = settingsHarness();
  const prefs = c.Settings.validPrefs({refresh:1,filter:"arbitrary",density:"huge",sound:"yes",desktop:true});
  assert.equal(prefs.refresh,2);
  assert.equal(prefs.filter,"all");
  assert.equal(prefs.sound,false);
  assert.equal(prefs.desktop,true);
  const valid = c.Settings.validPrefs({refresh:10,filter:"completed",density:"compact",sound:true});
  assert.equal(valid.refresh,10);
  assert.equal(valid.filter,"completed");
  const html=fs.readFileSync(path.join(__dirname,"../web/index.html"),"utf8");
  assert.equal((html.match(/data-settings-panel=/g)||[]).length,7);
  assert.match(html,/id="settings-fields" disabled/);
  assert.match(html,/id="setting-collision"/);
  assert.match(html,/id="btn-confirm-cleanup"[^>]*disabled/);
  assert.match(html,/settings\.js/);
});

test("queue totals use full-store counts, not the limited list, with recoverable errors", async () => {
  const {context: c, controls: n} = settingsHarness();
  c.loadJobDetail = async () => {};
  c.api = async () => ({jobs: [{id: "old", status: "succeeded", input: {path:"a"}, output: {path:"b"}}, {id: "active", status: "running", input: {path:"a"}, output: {path:"b"}}], counts: {succeeded: 150, queued: 8, running: 1}});
  await c.refreshJobs();
  assert.equal(n.get("queue-count").textContent, "159");
  assert.match(n.get("queue-summary").textContent, /排队 8 · 执行 1/);
  assert.equal(vm.runInContext('state.selectedJob', c), null);
  assert.equal(n.get("job-drawer").open, undefined);
  c.api = async () => {throw new Error("offline");};
  await c.refreshJobs();
  assert.match(n.get("queue-message").textContent, /保留上次队列/);
  c.api = async () => ({jobs: [], counts: {}});
  await c.refreshJobs();
  assert.equal(n.get("queue-empty").hidden, false);
  assert.equal(n.get("queue-message").textContent, "");
});

test("late detail cannot overwrite the task selected afterwards", async () => {
  const {context: c, controls: n} = settingsHarness();
  let finish;
  c.api = () => new Promise(resolve => {finish = resolve;});
  let rendered = false;
  c.renderJobDetail = () => {rendered = true;};
  vm.runInContext('state.selectedJob = "old"', c);
  n.get("job-drawer").showModal();
  const pending = c.loadJobDetail("old");
  vm.runInContext('state.selectedJob = "new"', c);
  finish({id: "old"});
  await pending;
  assert.equal(rendered, false);
});

test("closed drawer never requests details and pending response cannot reopen it", async () => {
  const {context: c, controls: n} = settingsHarness();
  const calls = [];
  let finish;
  c.api = path => {calls.push(path); return new Promise(resolve => {finish = resolve;});};
  await c.loadJobDetail("id");
  assert.deepEqual(calls, []);
  c.openJobDrawer("id");
  assert.equal(n.get("job-drawer").open, true);
  c.closeJobDrawer();
  finish({id: "id"});
  await Promise.resolve();
  assert.equal(n.get("job-drawer").open, false);
  assert.deepEqual(calls, ["/jobs/id"]);
});

test("resource refresh is independent of parameters, handles zero/null and failure", async () => {
  const {context: c, controls: n} = settingsHarness();
  const revision = vm.runInContext('state.settingsRevision', c);
  c.api = async () => ({scope: "WSL2", sampled_at: new Date().toISOString(), note: "scope",
    cpu: {model: "CPU", logical_cpus: 12, utilization_percent: 0}, memory: {used_bytes:0,total_bytes:1024,available_bytes:1024},
    gpu: {devices:[{name:"GPU",driver:"1",utilization_percent:null,source:"query"}]},disks:{items:[]}});
  await c.refreshResources();
  assert.equal(vm.runInContext('state.settingsRevision', c), revision);
  assert.equal(c.metricPercent(0), "0.0%");
  assert.equal(c.metricPercent(null), "无法读取");
  assert.equal(c.metricBytes(1024), "1.0 KiB");
  c.api = async () => {throw new Error("offline");};
  await c.refreshResources();
  assert.match(n.get("resource-message").textContent, /上次采样/);
});

test("blocked built-in encoder is not called uninstalled", () => {
  const {context: c} = settingsHarness();
  assert.equal(c.encoderInstalledLabel({status:"no_hardware",installed:true}), "已内置，但硬件不可用");
});

test("cancel failure remains visible in drawer and duplicate clicks are ignored", async () => {
  const {context:c, controls:n} = settingsHarness();
  n.get("job-drawer").showModal();
  n.get("btn-cancel").dataset.jobId = "id";
  vm.runInContext('state.selectedJob = "id"; state.detailJob = {id:"id",status:"running"}',c);
  let fail;
  const calls=[];
  c.api=path=>{calls.push(path);return new Promise((resolve,reject)=>{fail=reject;});};
  const pending=c.cancelJob();
  await c.cancelJob();
  fail(new Error("409 cannot cancel"));
  await pending;
  assert.deepEqual(calls,["/jobs/id/cancel"]);
  assert.match(n.get("detail-message").textContent,/原始错误|冲突/);
  assert.equal(n.get("btn-cancel").disabled,false);
});

test("summary is compact, preserves zero and uses minimum known output capacity", () => {
  const {context:c, controls:n} = settingsHarness();
  vm.runInContext(`state.roots=[{path:"/out",read_only:false},{path:"/missing",read_only:false}];
    state.resources={scope:"WSL2",sampled_at:"2026-10-04T00:00:00Z",cpu:{utilization_percent:0},
      memory:{used_bytes:0,total_bytes:1024},gpu:{devices:[{name:"A"},{name:"B"}]},
      disks:{items:[{label:"服务数据",path:"/db",filesystem_id:"1",status:"ok",available_bytes:4096},
        {path:"/out",filesystem_id:"2",status:"ok",available_bytes:1024},
        {path:"/missing",status:"unknown"}]}}`,c);
  c.renderSystemSummary();
  const text=n.get("system-summary").children.map(row=>row.children[1].textContent);
  assert.deepEqual(text,["0.0%","0 B / 1.0 KiB","2 张可见","1.0 KiB · 部分未知"]);
  assert.match(n.get("summary-message").textContent,/部分数据未知/);
  assert.equal(n.get("resource-info").children.length,0);
});

test("system drawer presents cached detail on demand without changing parameters", async () => {
  const {context:c, controls:n} = settingsHarness();
  vm.runInContext(`state.resources={scope:"WSL2",sampled_at:"2026-10-04T00:00:00Z",note:"scope note",
    cpu:{model:"CPU Model",logical_cpus:12},memory:{},gpu:{devices:[]},disks:{items:[]}}`,c);
  const revision=vm.runInContext("state.settingsRevision",c);
  c.api=async()=>vm.runInContext("state.resources",c);
  c.renderSystemSummary();
  assert.equal(n.get("resource-info").children.length,0);
  c.openSystemDrawer();
  assert.equal(n.get("system-drawer").open,true);
  assert.ok(n.get("resource-info").children.length>0);
  assert.equal(c.document.body.classList.contains("dialog-open"),true);
  c.closeSystemDrawer();
  c.syncDialogLock();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(n.get("system-drawer").open,false);
  assert.equal(vm.runInContext("state.settingsRevision",c),revision);
});

test("task controls avoid duplicate requests and deletion needs confirmation", async () => {
  const {context:c,controls:n}=settingsHarness();
  let finish;const calls=[];
  c.api=path=>{calls.push(path);return new Promise(resolve=>{finish=resolve;});};
  c.refreshJobs=async()=>{};
  c.confirm=()=>false;
  await c.jobAction("id","delete");assert.deepEqual(calls,[]);
  const pending=c.jobAction("id","pause");await c.jobAction("id","pause");
  assert.deepEqual(calls,["/jobs/id/pause"]);finish({});await pending;
});

test("task template restores controlled fields and keeps sparse preset overrides", () => {
  const {context:c,controls:n}=settingsHarness();
  const form=plain(c.buildSpec(true));
  form.video.quality=19;
  const baseline=plain(c.buildSpec(true));
  vm.runInContext('state.taskTemplates='+JSON.stringify([{id:"template",name:"Daily",preset_id:"uuid",spec:{preset:"Same"},form_spec:form,baseline}]),c);
  c.applyTaskTemplate("template");
  assert.equal(n.get("video-quality").value,"19");
  assert.equal(plain(c.buildSpec()).video.quality,19);
  assert.equal(plain(c.buildSpec()).video.encoder,undefined);
});

test("generated output names do not overwrite manual edits or stale source responses", async () => {
  const {context:c,controls:n}=settingsHarness();
  vm.runInContext('state.runtimeSettings={};state.selectedFile={root:"media",path:"a.avi"}',c);
  n.get("output-name").value="manual.mp4";const calls=[];
  c.api=async path=>{calls.push(path);return {path:"generated.mp4"};};
  await c.generateOutputName();assert.deepEqual(calls,[]);
  n.get("output-name").value="";let finish;
  c.api=()=>new Promise(resolve=>{finish=resolve;});
  const pending=c.generateOutputName();n.get("output-name").value="manual.mp4";
  finish({path:"late.mp4"});await pending;assert.equal(n.get("output-name").value,"manual.mp4");
});

test("detail refresh is silent and keeps log scroll", async () => {
  const {context:c,controls:n}=settingsHarness();
  n.get("job-drawer").showModal();n.get("job-log").scrollTop=32;
  vm.runInContext('state.selectedJob="id"',c);
  c.renderJobDetail=()=>{};
  let finish;c.api=()=>new Promise(resolve=>{finish=resolve;});
  const pending=c.loadJobDetail("id");assert.notEqual(n.get("detail-message").textContent,"正在读取任务详情…");
  c.api=async()=>({logs:[{ts:"now",level:"info",message:"new"}]});
  finish({id:"id",status:"running"});await pending;assert.equal(n.get("job-log").scrollTop,32);
});

const queueJobFixture = (id, status, actions = []) => ({id, status, actions, input: {root: "media", path: `${id}.mkv`}, output: {root: "out", path: `${id}.mp4`}, preset_id: "custom", container: "mp4", created_at: "2026-10-04T08:00:00Z"});

function setQueue(h, jobs) {
  vm.runInContext(`state.jobs = ${JSON.stringify(jobs)}; state.jobCounts = {running: 10, succeeded: 150};`, h.context);
  h.context.renderQueue();
}

test("queue filters and literal search only operate on the recent list", () => {
  const h = settingsHarness();
  const jobs = [queueJobFixture("clip <test>", "running"), queueJobFixture("archive", "succeeded"), queueJobFixture("broken", "interrupted")];
  setQueue(h, jobs);
  assert.match(h.controls.get("job-list-note").textContent, /全库 160/);
  vm.runInContext('state.queueFilter = "active"', h.context);
  assert.equal(h.context.filteredJobs().length, 1);
  vm.runInContext('state.queueFilter = "issues"', h.context);
  assert.equal(h.context.filteredJobs()[0].id, "broken");
  vm.runInContext('state.queueFilter = "all"; state.queueSearch = "<test>"', h.context);
  h.context.renderQueue();
  assert.equal(h.controls.get("job-list").children.length, 1);
  assert.equal(h.controls.get("job-list").querySelector("strong").textContent, "clip <test>.mkv");
  vm.runInContext('state.queueSearch = "not found"', h.context);
  h.context.renderQueue();
  assert.equal(h.controls.get("queue-empty").hidden, false);
  assert.equal(h.controls.get("queue-empty-title").textContent, "没有匹配的任务");
});

test("queue progress clamps invalid values and preserves zero speed / ETA", () => {
  const h = settingsHarness();
  assert.equal(h.context.jobPercent({progress: -1}), 0);
  assert.equal(h.context.jobPercent({progress: 2}), 100);
  assert.equal(h.context.jobPercent({progress: "bad"}), 0);
  assert.equal(h.context.formatDuration(0), "0 秒");
  assert.equal(h.context.formatDuration(null), "—");
  const job = {...queueJobFixture("movie", "running"), progress: .45, speed: "0", eta_seconds: 0};
  const row = h.context.renderQueueJob(job);
  assert.match(row.querySelector(".queue-job-timing").textContent, /0 帧\/秒.*剩余 0 秒/);
  assert.equal(row.querySelector(".queue-progress-track").attributes["aria-valuenow"], "45");
  assert.equal(row.querySelector(".queue-progress-fill").style.width, "45%");
});

test("selection survives filters, reports hidden selections and respects server actions", () => {
  const h = settingsHarness();
  setQueue(h, [queueJobFixture("a", "running", ["pause", "cancel"]), queueJobFixture("b", "waiting", ["start", "pause", "cancel", "delete"])]);
  vm.runInContext('state.checkedJobs = new Set(["a", "b"]); state.queueFilter = "active"', h.context);
  h.context.renderQueue();
  assert.equal(h.controls.get("queue-select-all").checked, true);
  assert.match(h.controls.get("queue-selection-note").textContent, /已选 2 项.*当前结果 1 项/);
  assert.equal(h.context.eligibleJobs("delete").length, 1);
  vm.runInContext('state.jobActionsPending.add("b")', h.context);
  h.context.renderQueueSelection();
  assert.equal(h.controls.get("btn-batch-start").disabled, true);
});

test("batch requests are sequential, permission-filtered and leave failed selections retryable", async () => {
  const h = settingsHarness();
  setQueue(h, [queueJobFixture("a", "waiting", ["start"]), queueJobFixture("b", "failed", ["start"]), queueJobFixture("c", "succeeded", ["delete"])]);
  vm.runInContext('state.checkedJobs = new Set(["a", "b", "c"])', h.context);
  let finish;
  const calls = [];
  h.context.api = path => {calls.push(path); return path.includes("/a/") ? new Promise(resolve => {finish = resolve;}) : Promise.reject(new Error("409 state changed"));};
  h.context.refreshJobs = async () => {};
  const pending = h.context.batchJobAction("start");
  await h.context.batchJobAction("start");
  assert.deepEqual(calls, ["/jobs/a/start"]);
  finish({});
  await pending;
  assert.deepEqual(calls, ["/jobs/a/start", "/jobs/b/start"]);
  assert.equal(vm.runInContext('state.checkedJobs.has("a")', h.context), false);
  assert.equal(vm.runInContext('state.checkedJobs.has("b")', h.context), true);
  assert.match(h.controls.get("queue-message").textContent, /成功 1 项，失败 1 项/);
  assert.equal(vm.runInContext('state.jobActionsPending.size', h.context), 0);
});

test("batch cancel and deletion require confirmation and never target ineligible jobs", async () => {
  const h = settingsHarness();
  setQueue(h, [queueJobFixture("a", "running", ["cancel"]), queueJobFixture("b", "succeeded", ["delete"])]);
  vm.runInContext('state.checkedJobs = new Set(["a", "b"])', h.context);
  const calls = [];
  h.context.api = async (path, options) => {calls.push([path, options.method]); return {};};
  h.context.refreshJobs = async () => {};
  h.context.confirm = () => false;
  await h.context.batchJobAction("delete");
  await h.context.batchJobAction("cancel");
  assert.deepEqual(calls, []);
  h.context.confirm = () => true;
  await h.context.batchJobAction("delete");
  assert.deepEqual(calls, [["/jobs/b", "DELETE"]]);
});

test("queue refresh removes disappeared selections but preserves batch feedback", async () => {
  const h = settingsHarness();
  vm.runInContext('state.checkedJobs = new Set(["gone", "a"]); state.queueFeedback = "批量操作完成：成功 1 项。"', h.context);
  h.context.api = async () => ({jobs: [queueJobFixture("a", "waiting", ["start"])], counts: {waiting: 1}});
  await h.context.refreshJobs();
  assert.equal(vm.runInContext('state.checkedJobs.size', h.context), 1);
  assert.match(h.controls.get("queue-message").textContent, /批量操作完成/);
});

test("Chinese labels preserve original values, official names and user content", () => {
  const {context:c,control} = harness();
  const speed=control("speed");c.renderChoice(speed,["ultrafast","medium","veryslow"],{value:"medium"});
  assert.equal(c.choiceValue(speed),"medium");
  assert.match(speed.children[1].textContent || speed.children[1].children[1].textContent,/均衡/);
  c.setChoiceValue(speed,"ultrafast");assert.equal(c.choiceValue(speed),"ultrafast");
  const official=c.UI.preset({name:"General/Fast 1080p30",description:"A useful fast preset"},"official");
  assert.match(official.name,/快速/);assert.equal(official.original,"General/Fast 1080p30");
  assert.equal(official.rawDescription,"A useful fast preset");
  const custom=c.UI.preset({name:"My Fast Template",description:"My personal text"},"imported");
  assert.equal(custom.name,"My Fast Template");assert.equal(custom.description,"My personal text");
  assert.match(c.UI.error("output already exists"),/输出位置已被占用/);
  assert.match(c.UI.error("unknown upstream failure"),/原始错误/);
  assert.equal(c.UI.raw({rawMessage:"unchanged diagnostic"}),"unchanged diagnostic");
  assert.equal(c.UI.status.verified,"已验证");
  const html=fs.readFileSync(path.join(__dirname,"../web/index.html"),"utf8");
  assert.doesNotMatch(html,/>Tune<|>Level<|Chroma Smooth|placeholder="auto"/);
});

test("SpringHub theme and branding keep legacy preference identities", () => {
  const html=fs.readFileSync(path.join(__dirname,"../web/index.html"),"utf8");
  const css=fs.readFileSync(path.join(__dirname,"../web/styles.css"),"utf8");
  const settings=fs.readFileSync(path.join(__dirname,"../web/settings.js"),"utf8");
  assert.match(html,/<title>SpringHub/);
  assert.match(html,/aria-label="SpringHub"/);
  assert.match(html,/brand-spring/);
  assert.match(html,/brand-hub/);
  assert.doesNotMatch(html,/Cute Cat|🐱/);
  assert.match(css,/--accent: #ff9900/);
  assert.match(css,/--bg: #000000/);
  assert.doesNotMatch(css,/#4f9cf9|79,156,249|#40536b|#a9c9f3|linear-gradient/);
  assert.match(settings,/SpringHub · 测试提醒/);
  assert.match(settings,/springhub-task-templates\.json/);
  assert.match(settings,/cute-cat\.preferences\.v1/);
  assert.match(settings,/cute-cat\.notifications\.v1/);
});

test("playback fields and audio samplerate survive template application and editing", () => {
  const {context:c,controls:n,control} = settingsHarness();
  for (const id of ["stream-faststart","stream-downscale","stream-maxrate","stream-buffer","stream-keyframe","task-template-note"]) control(id);
  c.renderChoice(control("stream-pixel"),["","yuv420p","yuv420p10le"]);
  c.renderChoice(n.get("task-template-select"),["builtin"]);
  const form = plain(c.buildSpec(true));
  form.video.quality=19;
  form.streaming={faststart:true,only_downscale:true,pixel_format:"yuv420p",maxrate_kbps:5000,buffer_kbps:10000,keyframe_interval:60};
  form.audio.tracks=[{encoder:"aac",source:"auto",samplerate:"48000",mixdown:"stereo",bitrate:"128"}];
  vm.runInContext('state.taskTemplates='+JSON.stringify([{id:"builtin",builtin:true,name:"食品",description:"保留细节",spec:form,form_spec:form,baseline:null}]),c);
  c.applyTaskTemplate("builtin");
  assert.equal(vm.runInContext('state.templateEditing',c),null);
  const built=plain(c.buildSpec());
  assert.deepEqual(built.streaming,form.streaming);
  assert.equal(built.audio.tracks[0].samplerate,"48000");
  assert.match(n.get("task-template-note").textContent,/内置只读/);
});

test("engine switch ignores late capability responses and invalidates validation", async () => {
  const {context:c,controls:n,control} = settingsHarness();
  c.renderChoice(control("engine-select"),["handbrake","ffmpeg","rffmpeg"]);
  for (const id of ["engine-choice-note","btn-open-presets"]) control(id);
  vm.runInContext('state.validation={status:"passed"}',c);
  const finishes=[];
  c.api=()=>new Promise(resolve=>finishes.push(resolve));
  const first=c.changeEngine("ffmpeg");
  const second=c.changeEngine("rffmpeg");
  assert.equal(n.get("btn-queue").disabled,true);
  finishes[1]({engine:{available:false,notes:[]},encoder_catalog:{video:[],audio:[]}});await second;
  finishes[0]({engine:{available:true},encoder_catalog:{video:[{id:"x264"}]}});await first;
  assert.equal(vm.runInContext('state.engineId',c),"rffmpeg");
  assert.equal(vm.runInContext('state.caps.engine.available',c),false);
  assert.equal(n.get("spec-validation").dataset.status,"stale");
  assert.equal(n.get("btn-open-presets").disabled,true);
  assert.match(n.get("engine-choice-note").textContent,/不支持运行态暂停/);
});

function remoteSettingsHarness() {
  const h=expandedSettingsHarness();
  for (const id of ["setting-rffmpeg-bin","setting-rffprobe-bin","setting-remote-probe-dir","rffmpeg-config-state","btn-check-rffmpeg","rffmpeg-check-message","rffmpeg-check-results","engine-choice-note"]) h.control(id);
  return h;
}

test("remote settings persist three fields without automatically contacting wrapper", async () => {
  const {context:c,controls:n}=remoteSettingsHarness();
  await c.Settings.open();
  n.get("setting-rffmpeg-bin").value="/opt/rffmpeg/ffmpeg";
  n.get("setting-rffprobe-bin").value="ffprobe";
  n.get("setting-remote-probe-dir").value="/out/probes";
  n.get("setting-timeout").value="99";
  const calls=[];
  c.api=async(route,options)=>{calls.push(route);return {values:JSON.parse(options.body),revision:1};};
  await c.Settings.save();
  assert.deepEqual(calls,["/settings"]);
  assert.equal(vm.runInContext('state.runtimeSettings.rffmpeg_bin',c),"/opt/rffmpeg/ffmpeg");
  assert.equal(c.Settings.dirty(),false);
  c.api=async route=>{calls.push(route);return {status:"passed",message:"bounded version check",checks:[{ok:true,message:"version"}]};};
  await c.Settings.checkRemote();
  assert.deepEqual(calls,["/settings","/rffmpeg/check"]);
  assert.match(n.get("rffmpeg-check-message").textContent,/基础检查通过/);
  n.get("setting-rffmpeg-bin").value="changed";
  await c.Settings.checkRemote();
  assert.match(n.get("rffmpeg-check-message").textContent,/先保存/);
  assert.equal(calls.length,2);
});

test("remote check avoids repeated clicks and discards closed drawer responses", async () => {
  const {context:c,controls:n}=remoteSettingsHarness();
  await c.Settings.open();
  let finish;let calls=0;
  c.api=()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
  const pending=c.Settings.checkRemote();await c.Settings.checkRemote();
  assert.equal(calls,1);
  c.Settings.close();
  finish({status:"passed",message:"LATE",checks:[]});await pending;
  assert.doesNotMatch(n.get("rffmpeg-check-message").textContent,/LATE/);
});

test("remote save invalidates selected engine without probing it", async () => {
  const {context:c,controls:n}=remoteSettingsHarness();
  await c.Settings.open();
  vm.runInContext('state.engineId="rffmpeg";state.validation={status:"passed"}',c);
  n.get("setting-rffmpeg-bin").value="wrapper";n.get("setting-rffprobe-bin").value="probe";n.get("setting-remote-probe-dir").value="/out";
  const calls=[];c.api=async(route,options)=>{calls.push(route);return {values:JSON.parse(options.body),revision:1};};
  await c.Settings.save();
  assert.deepEqual(calls,["/settings"]);
  assert.equal(vm.runInContext('state.caps.engine.available',c),false);
  assert.equal(n.get("spec-validation").dataset.status,"stale");
  assert.match(n.get("engine-choice-note").textContent,/保存不会联系远端/);
});

test("remote defaults reset only remote draft and does not discard other fields", async () => {
  const {context:c,controls:n}=remoteSettingsHarness();
  const original=c.api;
  c.api=async route=>{const result=await original(route);if(route==="/settings")result.defaults={...result.defaults,rffmpeg_bin:"toml-wrapper",rffprobe_bin:"toml-probe",remote_probe_dir:"/out"};return result;};
  await c.Settings.open();
  n.get("setting-timeout").value="42";
  c.Settings.selectSection("remote");c.Settings.resetSection();
  assert.equal(n.get("setting-rffmpeg-bin").value,"toml-wrapper");
  assert.equal(n.get("setting-timeout").value,"42");
  assert.equal(c.Settings.dirty(),true);
});

test("engine choices disable missing engines and synthetic switching cannot bypass them", async () => {
  const {context:c,control,controls:n}=settingsHarness();
  control("engine-select");control("engine-choice-note");control("engine-availability-note");
  vm.runInContext('state.engineAvailability=[{id:"handbrake",selectable:true,status:"available"},{id:"ffmpeg",selectable:false,status:"missing",reason:"未安装"},{id:"rffmpeg",selectable:false,status:"unconfigured",reason:"未配置"}]',c);
  c.renderEngineChoices();
  const radios=n.get("engine-select").querySelectorAll("input[type=radio]");
  assert.equal(radios[0].disabled,false);assert.equal(radios[1].disabled,true);assert.equal(radios[2].disabled,true);
  let calls=0;c.api=async()=>{calls++;};
  await c.changeEngine("ffmpeg");
  assert.equal(vm.runInContext('state.engineId',c),"handbrake");assert.equal(calls,0);
  assert.match(n.get("engine-choice-note").textContent,/未安装/);
});

test("unavailable template engine cannot replace task draft", () => {
  const {context:c,controls:n,control}=settingsHarness();control("task-template-note");
  vm.runInContext('state.engineAvailability=[{id:"handbrake",selectable:true,status:"available"}]; state.taskTemplates=[{id:"remote",engine:"rffmpeg",spec:{},form_spec:{},name:"remote"}]',c);
  const before=plain(c.buildSpec());c.applyTaskTemplate("remote");
  assert.deepEqual(plain(c.buildSpec()),before);
  assert.match(n.get("task-template-note").textContent,/无法应用/);
});

test("availability ignores stale responses and never silently changes selected engine", async () => {
  const {context:c,control,controls:n}=settingsHarness();control("engine-select");control("engine-availability-note");
  vm.runInContext('refreshEngineAvailability=realRefreshEngineAvailability; state.engineId="ffmpeg"',c);
  const finishes=[];c.api=()=>new Promise(resolve=>finishes.push(resolve));
  const first=c.refreshEngineAvailability();const second=c.refreshEngineAvailability();
  finishes[1]({engines:[{id:"handbrake",selectable:true,status:"available"},{id:"ffmpeg",selectable:false,status:"missing"},{id:"rffmpeg",selectable:false,status:"unconfigured"}]});await second;
  finishes[0]({engines:[{id:"handbrake",selectable:true},{id:"ffmpeg",selectable:true},{id:"rffmpeg",selectable:true}]});await first;
  assert.equal(vm.runInContext('state.engineId',c),"ffmpeg");assert.equal(c.engineSelectable("ffmpeg"),false);
  assert.equal(n.get("btn-queue").disabled,true);
});

test("creation summary groups choices and keeps template and playback fields accessible", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
  const summary = html.split('data-panel="summary">')[1].split('<!-- Dimensions -->')[0];
  assert.equal((summary.match(/class="summary-section"/g) || []).length, 3);
  for (const group of ["engine", "template", "output"]) {
    assert.match(summary, new RegExp(`aria-labelledby="summary-${group}-title"`));
    assert.match(summary, new RegExp(`id="summary-${group}-title"`));
  }
  const disclosures = [...summary.matchAll(/<details class="summary-disclosure">([\s\S]*?)<\/details>/g)];
  assert.equal(disclosures.length, 2);
  assert.match(disclosures[0][1], /保存当前编码为模板/);
  for (const id of ["template-name", "template-description", "btn-save-template", "template-message"]) {
    assert.match(disclosures[0][1], new RegExp(`id="${id}"`));
  }
  assert.match(disclosures[1][1], /网页播放参数/);
  for (const id of ["engine-select", "task-template-select", "container-select", "output-root-select", "output-name", "stream-faststart", "stream-downscale", "stream-pixel", "stream-maxrate", "stream-buffer", "stream-keyframe"]) {
    assert.equal((summary.match(new RegExp(`id="${id}"`, "g")) || []).length, 1);
  }
});

function installSettingsHarness() {
  const h = remoteSettingsHarness();
  for (const id of ["ffmpeg-install-state", "ffmpeg-install-paths", "ffmpeg-install-message", "btn-install-ffmpeg", "btn-refresh-ffmpeg"]) h.control(id);
  h.context.clearTimeout = () => {};
  h.context.setTimeout = () => 1;
  return h;
}

test("engine refresh is icon-only and FFmpeg shares the remote settings entry", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
  const button = html.match(/<button id="btn-refresh-engine-status"[\s\S]*?<\/button>/)[0];
  assert.match(button, /aria-label="刷新引擎状态"/);
  assert.match(button, /<svg[^>]*aria-hidden="true"/);
  assert.doesNotMatch(html, /id="btn-configure-rffmpeg"/);
  assert.match(html, /data-settings-section="remote">FFmpeg 与远程/);
  assert.match(html, /id="btn-install-ffmpeg"[^>]*disabled/);
});

test("FFmpeg installation requires confirmation, sends no paths and refreshes status", async () => {
  const {context:c, controls:n} = installSettingsHarness();
  await c.Settings.open();
  const calls = [];
  c.api = async (route, options) => {
    calls.push({route, body:options?.body});
    return {installed:false, supported:true, can_install:true, status:"idle", directory:"/data/tools"};
  };
  await c.Settings.refreshInstall();
  assert.equal(n.get("btn-install-ffmpeg").disabled, false);
  c.confirm = () => false;
  await c.Settings.installFFmpeg();
  assert.equal(calls.length, 1);
  c.confirm = () => true;
  await c.Settings.installFFmpeg();
  assert.equal(calls[1].route, "/ffmpeg/install");
  assert.deepEqual(JSON.parse(calls[1].body), {confirm:true});
  assert.equal(calls[2].route, "/ffmpeg/install");
});

test("FFmpeg status ignores closed drawers and installation errors stay visible", async () => {
  const {context:c, controls:n} = installSettingsHarness();
  await c.Settings.open();
  let finish;
  c.api = () => new Promise(resolve => {finish=resolve;});
  const pending = c.Settings.refreshInstall();
  c.Settings.close();
  finish({installed:true, status:"succeeded"}); await pending;
  assert.notEqual(n.get("ffmpeg-install-state").textContent, "已安装 · undefined。");
  c.api = async route => route === "/ffmpeg/install" ? {installed:false, supported:true, can_install:true, status:"idle"} : route === "/task-templates" ? {templates:[]} : {values:{},revision:0};
  await c.Settings.open(); await c.Settings.refreshInstall();
  c.confirm = () => true;
  c.api = async () => {throw new Error("offline");};
  await c.Settings.installFFmpeg();
  assert.match(n.get("ffmpeg-install-message").textContent, /无法开始安装/);
});

test("numeric choice reads zero/empty without losing semantics", () => {
  const { context, control } = harness();
  const node = control("dim-modulus");
  context.renderChoice(node, [{ value: "", label: "默认" }, { value: "2", label: "2" }]);
  assert.equal(context.numberOrNull("dim-modulus"), null);
  context.setChoiceValue(node, "2");
  assert.equal(context.numberOrNull("dim-modulus"), 2);
});
