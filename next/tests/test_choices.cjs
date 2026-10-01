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
    };
  }
  setAttribute(key, value) { this.attributes[key] = String(value); this[key] = String(value); }
  focus() { this.focused = true; }
  close() { this.closed = true; }
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
    const matches = node => selector === "select" ? node.tagName === "select"
      : selector === "input:checked" ? node.tagName === "input" && node.checked
      : selector === "input[type=radio]" ? node.tagName === "input" && node.type === "radio"
      : selector.startsWith(".") ? node.classList.contains(selector.slice(1)) : node.tagName === selector;
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
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
      getElementById: id => controls.get(id),
    },
    Event: class { constructor(type, init = {}) { this.type = type; this.bubbles = Boolean(init.bubbles); } },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8"), context);
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
  assert.equal(labels.installed, "不适用（无此编码器）");
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

test("numeric choice reads zero/empty without losing semantics", () => {
  const { context, control } = harness();
  const node = control("dim-modulus");
  context.renderChoice(node, [{ value: "", label: "默认" }, { value: "2", label: "2" }]);
  assert.equal(context.numberOrNull("dim-modulus"), null);
  context.setChoiceValue(node, "2");
  assert.equal(context.numberOrNull("dim-modulus"), 2);
});
