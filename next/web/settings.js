/* Categorised settings, browser preferences and task completion events. */
"use strict";
var Settings = (() => {
  // Keep legacy storage/channel identities so rebranding preserves preferences.
  const key = "cute-cat.preferences.v1";
  const defaults = {refresh: 2, filter: "all", density: "standard", success: true, failure: true, sound: false, desktop: false};
  let prefs = {...defaults}, report = null, baseline = "", ready = false, section = "general";
  let request = 0, naming = 0, cleanup = null, transferBusy = false, maintenanceBusy = false;
  let cursor = null, eventsBusy = false, audio = null, channel = null;
  const seen = new Set();
  const localFields = {refresh: "setting-refresh", filter: "setting-queue-filter", density: "setting-density", success: "setting-notify-success", failure: "setting-notify-failure", sound: "setting-notify-sound", desktop: "setting-notify-desktop"};
  const groups = {general: ["refresh", "filter", "density"], notifications: ["success", "failure", "sound", "desktop"], output: ["default_output_root", "output_name_template", "output_collision_policy"], queue: ["max_concurrent_jobs", "auto_start", "job_timeout_seconds"], templates: ["default_task_template_id"]};

  function validPrefs(raw) {
    const out = {...defaults};
    if (raw && [2, 5, 10, 30].includes(raw.refresh)) out.refresh = raw.refresh;
    if (raw && ["all", "pending", "active", "paused", "completed", "issues", "canceled"].includes(raw.filter)) out.filter = raw.filter;
    if (raw && ["standard", "compact"].includes(raw.density)) out.density = raw.density;
    for (const name of ["success", "failure", "sound", "desktop"]) if (typeof raw?.[name] === "boolean") out[name] = raw[name];
    return out;
  }

  function init() {
    try { prefs = validPrefs(JSON.parse(localStorage.getItem(key) || "null")); } catch { prefs = {...defaults}; }
    applyPrefs(true);
    if (typeof BroadcastChannel !== "undefined") {
      channel = new BroadcastChannel("cute-cat.notifications.v1");
      channel.onmessage = event => { if (typeof event.data === "string") {seen.add(event.data); if (seen.size > 500) seen.delete(seen.values().next().value);} };
    }
  }

  function applyPrefs(initial = false) {
    document.body.classList.toggle("queue-compact", prefs.density === "compact");
    if (initial) state.queueFilter = prefs.filter;
  }

  function draft() {
    const local = {};
    for (const [name, id] of Object.entries(localFields)) local[name] = typeof defaults[name] === "boolean" ? $(id).checked : name === "refresh" ? Number(choiceValue($(id))) : choiceValue($(id));
    return {local, service: {max_concurrent_jobs: Number($("setting-concurrency").value), auto_start: $("setting-auto-start").checked,
      job_timeout_seconds: Number($("setting-timeout").value), default_output_root: choiceValue($("setting-output-root")),
      output_name_template: $("setting-name-template").value, default_task_template_id: choiceValue($("setting-default-template")) || null,
      output_collision_policy: choiceValue($("setting-collision"))}};
  }

  function fillLocal(values) {
    for (const [name, id] of Object.entries(localFields)) {
      if (typeof defaults[name] === "boolean") $(id).checked = values[name]; else setChoiceValue($(id), String(values[name]));
    }
  }

  function fillService(values) {
    $("setting-concurrency").value = values.max_concurrent_jobs;
    $("setting-auto-start").checked = values.auto_start;
    $("setting-timeout").value = values.job_timeout_seconds;
    $("setting-name-template").value = values.output_name_template;
    setChoiceValue($("setting-collision"), values.output_collision_policy || "reject");
    renderChoice($("setting-output-root"), state.roots.filter(r => !r.read_only).map(r => ({value:r.id,label:r.label})), {value:values.default_output_root});
    renderChoice($("setting-default-template"), [{value:"",label:"不使用模板"}, ...state.taskTemplates.map(t => ({value:t.id,label:t.name}))], {value:values.default_task_template_id || ""});
  }

  function dirty() { return ready && baseline !== JSON.stringify(draft()); }
  function updateDirty() {
    $("settings-dirty").textContent = dirty() ? "有未保存的修改" : "";
    $("btn-save-settings").disabled = !ready || state.settingsSaving || transferBusy || maintenanceBusy;
    $("btn-reset-settings-section").disabled = !ready || state.settingsSaving || !groups[section];
  }
  function canClose() {
    if (state.settingsSaving || transferBusy || maintenanceBusy) return false;
    return !dirty() || confirm("设置尚未保存。放弃修改并关闭？");
  }
  function close() { if (canClose()) { request++; $("settings-drawer").close(); } }

  function selectSection(name) {
    section = name;
    document.querySelectorAll("[data-settings-section]").forEach(button => {
      const active = button.dataset.settingsSection === name;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-settings-panel]").forEach(panel => {panel.hidden = panel.dataset.settingsPanel !== name;});
    $("settings-content").scrollTop = 0;
    updateDirty();
  }

  async function open() {
    $("settings-drawer").showModal(); syncDialogLock();
    const id = ++request;
    ready = false; baseline = ""; cleanup = null;
    $("settings-fields").disabled = true;
    $("settings-dirty").textContent = "";
    $("template-transfer-message").textContent = "";
    $("settings-content").setAttribute("aria-busy", "true");
    $("settings-message").textContent = "正在读取设置…";
    $("cleanup-message").textContent = "";
    $("btn-confirm-cleanup").disabled = true;
    updateDirty();
    try {
      const loaded = await api("/settings");
      if (id !== request || !$("settings-drawer").open) return;
      const templates = await api("/task-templates");
      if (id !== request || !$("settings-drawer").open) return;
      report = loaded;
      state.runtimeSettings = loaded.values;
      state.taskTemplates = templates.templates;
      renderTemplateChoices();
      fillLocal(prefs); fillService(loaded.values);
      $("setting-active-slots").textContent = `当前占用 ${loaded.active_slots} 个执行名额。`;
      renderStorage(); rootNote(); renderSettingsTemplates(); permissionNote();
      ready = true; baseline = JSON.stringify(draft());
      $("settings-fields").disabled = false;
      $("settings-message").textContent = "服务配置保存在 SQLite，界面与通知仅保存在本浏览器。";
      selectSection(section);
      previewName();
    } catch (err) {
      if (id === request) $("settings-message").textContent = "无法读取设置，请关闭后重试：" + UI.error(err);
    } finally {
      if (id === request) {$("settings-content").setAttribute("aria-busy", "false"); updateDirty();}
    }
    $("btn-close-settings").focus();
  }

  async function save() {
    if (!ready || state.settingsSaving || transferBusy || maintenanceBusy) return;
    const fields = $("settings-fields");
    if (!fields.checkValidity()) {fields.reportValidity(); return;}
    const values = draft();
    state.settingsSaving = true; updateDirty(); fields.disabled = true;
    try {
      const saved = await api("/settings", {method:"POST", body:JSON.stringify({...values.service, expected_revision:report.revision})});
      report = saved; state.runtimeSettings = saved.values;
      prefs = validPrefs(values.local);
      let persistent = true;
      try {localStorage.setItem(key, JSON.stringify(prefs));} catch {persistent = false;}
      applyPrefs(); state.queueFilter = prefs.filter; renderQueue(); startPolling();
      baseline = JSON.stringify(draft());
      $("settings-message").textContent = persistent ? "设置已保存。并发立即生效；输出、超时与模板用于新任务。" : "服务设置已保存；浏览器存储不可用，界面与通知只在本次会话生效。";
    } catch (err) {
      $("settings-message").textContent = "保存失败，修改仍保留：" + UI.error(err);
    } finally {state.settingsSaving = false; fields.disabled = false; updateDirty();}
  }

  function resetSection() {
    if (!ready || state.settingsSaving) return;
    const values = draft();
    if (section === "general" || section === "notifications") {
      for (const name of groups[section]) values.local[name] = defaults[name];
      fillLocal(values.local);
    } else if (groups[section]) {
      for (const name of groups[section]) values.service[name] = report.defaults[name];
      fillService(values.service);
    }
    updateDirty(); rootNote(); previewName(); permissionNote();
    $("settings-message").textContent = "本类已恢复默认草稿，保存后生效。模板和任务不会删除。";
  }

  async function previewName() {
    if (!ready) return;
    const id = ++naming;
    const template = $("setting-name-template").value;
    try {
      const result = await api("/output-name", {method:"POST", body:JSON.stringify({template,source:"旅行视频.mp4",encoder:"x264",container:"mp4"})});
      if (id === naming) $("name-preview").textContent = result.path;
    } catch (err) {if (id === naming) $("name-preview").textContent = "命名无效：" + UI.error(err);}
  }

  function rootNote() {
    const root = state.roots.find(r => r.id === choiceValue($("setting-output-root")));
    $("setting-root-note").textContent = root ? `${root.label} · ${root.available && root.marker_present !== false ? "可用" : "当前不可用，请检查挂载"}` : "没有可写存储根，请检查部署配置。";
  }
  function renderStorage() {
    $("settings-storage-list").replaceChildren(...state.roots.map(root => el("div", {class:"storage-row"}, [
      el("strong", {text:root.label}), el("span", {text:root.read_only ? "只读" : "可写"}),
      el("span", {text:root.available && root.marker_present !== false ? "可用" : "不可用 / 挂载标记缺失"}),
      el("code", {class:"storage-path",text:root.path}),
    ])));
  }

  async function refreshTemplates() {
    state.taskTemplates = (await api("/task-templates")).templates;
    const updated = await api("/settings");
    // Template actions must not absorb another browser's settings revision.
    // Default-template deletion is handled explicitly by removeTemplate().
    if (report && updated.revision === report.revision) report = updated;
    state.runtimeSettings = updated.values;
    // Never throw away other unsaved fields when template actions refresh lists.
    const identity = choiceValue($("setting-default-template"));
    renderTemplateChoices();
    if (state.taskTemplates.some(t => t.id === identity)) setChoiceValue($("setting-default-template"), identity);
    else setChoiceValue($("setting-default-template"), "");
    renderSettingsTemplates(); updateDirty();
  }
  async function removeTemplate(template) {
    if (transferBusy || !confirm("删除此编码任务模板？已创建任务不会改变。")) return;
    transferBusy = true; updateDirty();
    try {
      const previous = report;
      await api("/task-templates/" + template.id, {method:"DELETE"}); await refreshTemplates();
      if (previous?.values.default_task_template_id === template.id) {
        const updated = await api("/settings");
        const expected = {...previous.values, default_task_template_id:null};
        if (updated.revision === previous.revision + 1 && Object.keys(expected).every(k => expected[k] === updated.values[k])) report = updated;
      }
    }
    catch (err) {$("template-transfer-message").textContent = "删除失败：" + UI.error(err);}
    finally {transferBusy = false; updateDirty();}
  }
  async function copyTemplate(template) {
    if (transferBusy) return;
    transferBusy = true; updateDirty();
    try {
      const payload = Object.fromEntries(Object.entries(template).filter(([k]) => !["id","builtin","slug","supported_engines"].includes(k)));
      payload.name = (template.name + " 副本").slice(0,80);
      await api("/task-templates", {method:"POST",body:JSON.stringify(payload)});
      await refreshTemplates();
      $("template-transfer-message").textContent = "副本已创建，原模板保持不变。";
    } catch (err) {$("template-transfer-message").textContent = "复制失败：" + UI.error(err);}
    finally {transferBusy = false; updateDirty();}
  }
  async function exportTemplates(ids) {
    if (transferBusy || !ids.length) return;
    transferBusy = true; updateDirty();
    try {
      const bundle = await api("/task-templates/export", {method:"POST",body:JSON.stringify({ids})});
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle,null,2)], {type:"application/json"}));
      const link = el("a", {href:url,download:"springhub-task-templates.json"});
      document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      $("template-transfer-message").textContent = `已导出 ${bundle.templates.length} 个模板，包含所需预设，不含源文件或凭据。`;
    } catch (err) {$("template-transfer-message").textContent = "导出失败：" + UI.error(err);}
    finally {transferBusy = false; updateDirty();}
  }
  async function importFile(file) {
    if (!file || transferBusy) return;
    transferBusy = true; updateDirty();
    try {
      if (file.size > 900000) throw new Error("文件不能超过 900 KB");
      const bundle = JSON.parse(await file.text());
      const preview = await api("/task-templates/import-preview", {method:"POST",body:JSON.stringify(bundle)});
      if (!confirm(`导入 ${preview.count} 个模板？\n${preview.names.join("\n")}\n会生成新的模板身份，不覆盖原模板。`)) return;
      const result = await api("/task-templates/import", {method:"POST",body:JSON.stringify(bundle)});
      await refreshTemplates();
      $("template-transfer-message").textContent = `已导入 ${result.templates.length} 个模板。`;
    } catch (err) {$("template-transfer-message").textContent = "导入失败：" + UI.error(err);}
    finally {transferBusy = false; $("template-import-file").value = ""; updateDirty();}
  }

  function cleanupFilter() {
    return {days:Number(choiceValue($("cleanup-days"))), statuses:[...document.querySelectorAll("[data-cleanup-status]")].filter(n => n.checked).map(n => n.dataset.cleanupStatus)};
  }
  function invalidateCleanup() {cleanup = null; $("btn-confirm-cleanup").disabled = true; $("cleanup-message").textContent = "条件已修改，请重新预览。";}
  async function previewCleanup() {
    if (maintenanceBusy) return;
    maintenanceBusy = true; updateDirty(); invalidateCleanup();
    try {
      const filter = cleanupFilter();
      const result = await api("/maintenance/preview", {method:"POST",body:JSON.stringify(filter)});
      if (JSON.stringify(filter) !== JSON.stringify(cleanupFilter())) return;
      cleanup = result;
      $("cleanup-message").textContent = `${result.count} 条记录可清理（每批最多 ${result.limit} 条）。确认有效期 5 分钟，仅清理记录和日志。`;
      $("btn-confirm-cleanup").disabled = !result.count;
    } catch (err) {$("cleanup-message").textContent = "预览失败：" + UI.error(err);}
    finally {maintenanceBusy = false; updateDirty();}
  }
  async function confirmCleanup() {
    if (maintenanceBusy || !cleanup || !confirm(`清理预览的 ${cleanup.count} 条已结束记录及日志？\n不会删除源文件、输出媒体、模板或预设。`)) return;
    maintenanceBusy = true; updateDirty(); $("btn-confirm-cleanup").disabled = true;
    try {
      const result = await api("/maintenance/cleanup", {method:"POST",body:JSON.stringify({token:cleanup.token})});
      cleanup = null;
      $("cleanup-message").textContent = `已清理 ${result.deleted} 条，跳过 ${result.skipped} 条状态已变化的记录。不保证数据库文件立即缩小。`;
      await refreshJobs();
      if (state.selectedJob && !state.jobs.some(j => j.id === state.selectedJob)) { closeJobDrawer(); state.selectedJob = null; }
    } catch (err) {$("cleanup-message").textContent = "清理失败，请重新预览：" + UI.error(err); cleanup = null;}
    finally {maintenanceBusy = false; updateDirty();}
  }

  function permissionNote(message = "") {
    const supported = typeof Notification !== "undefined" && window.isSecureContext;
    $("notification-permission").textContent = message || (!supported ? "系统通知需要受支持的浏览器与 HTTPS 或 localhost。" : `系统通知权限：${{granted:"已允许",denied:"已拒绝，请在浏览器站点设置中修改",default:"尚未授权"}[Notification.permission]}。提示音需通过点击启用。`);
  }
  async function requestPermission() {
    try {
      if (typeof Notification === "undefined" || !window.isSecureContext) throw new Error("当前环境不支持系统通知");
      await Notification.requestPermission(); permissionNote();
    } catch (err) {permissionNote(UI.error(err));}
  }
  async function playSound() {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) throw new Error("当前浏览器不支持提示音");
    if (!audio) audio = new Audio();
    await audio.resume();
    if (audio.state !== "running") throw new Error("提示音被浏览器阻止，请点击测试声音");
    const oscillator = audio.createOscillator(), volume = audio.createGain();
    oscillator.connect(volume); volume.connect(audio.destination);
    oscillator.frequency.value = 660;
    volume.gain.setValueAtTime(.07,audio.currentTime);
    volume.gain.exponentialRampToValueAtTime(.001,audio.currentTime+.25);
    oscillator.start(); oscillator.stop(audio.currentTime+.25);
  }
  function pageNotice(text, failed = false) {
    const box = $("completion-notices");
    if (!box) return;
    if (box.children.length >= 4) box.firstElementChild.remove();
    const node = el("div", {class:"completion-notice" + (failed ? " failed" : ""),text});
    box.appendChild(node); setTimeout(() => node.remove(), 8000);
  }
  async function deliver(event, test = false) {
    const failed = event.status !== "succeeded";
    const config = test ? draft().local : prefs;
    if (!test && !(failed ? config.failure : config.success)) return;
    const title = test ? "SpringHub · 测试提醒" : failed ? "SpringHub · 转换失败 / 中断" : "SpringHub · 转换完成";
    const text = `${title}：${event.source}`;
    pageNotice(text, failed);
    if (config.sound) {try {await playSound();} catch (err) {permissionNote(UI.error(err));}}
    if (config.desktop) {
      try {
        if (typeof Notification === "undefined" || Notification.permission !== "granted" || !window.isSecureContext) throw new Error("系统通知未授权，已显示页面提醒");
        new Notification(title, {body:event.source, tag:test ? "cute-cat-test" : "cute-cat-event-" + event.id});
      } catch (err) {permissionNote(UI.error(err));}
    }
  }
  async function notifyOnce(event) {
    if (event.deleted || !["succeeded","failed","interrupted"].includes(event.status)) return;
    const identity = location.origin + ":" + event.id + ":" + event.job_id;
    const run = async () => {
      if (seen.has(identity)) return;
      try {
        const stored = JSON.parse(localStorage.getItem("cute-cat.notices.v1") || "[]");
        const recent = Array.isArray(stored) ? stored.filter(value => typeof value === "string").slice(-500) : [];
        if (recent.includes(identity)) return;
        localStorage.setItem("cute-cat.notices.v1", JSON.stringify([...recent, identity].slice(-500)));
      } catch { /* Session-only dedup. */ }
      seen.add(identity);
      if (seen.size > 500) seen.delete(seen.values().next().value);
      if (channel) channel.postMessage(identity);
      await deliver(event);
    };
    if (navigator.locks?.request) await navigator.locks.request("cute-cat-notifications", run);
    else await run(); // BroadcastChannel fallback is best-effort, not an atomic lock.
  }
  async function pollEvents() {
    if (eventsBusy) return;
    eventsBusy = true;
    try {
      for (let page = 0; page < 10; page++) {
        const data = await api("/job-events" + (cursor === null ? "" : `?after=${cursor}&limit=100`));
        for (const event of data.events) await notifyOnce(event);
        cursor = data.cursor;
        if (!data.has_more) break;
      }
    } catch { /* Retain cursor and retry on next poll, not a false completion. */ }
    finally {eventsBusy = false;}
  }

  function wire() {
    document.querySelectorAll("[data-settings-section]").forEach(button => {
      button.addEventListener("click", () => selectSection(button.dataset.settingsSection));
      button.addEventListener("keydown", event => {
        const buttons = [...document.querySelectorAll("[data-settings-section]")], index = buttons.indexOf(button);
        if (["ArrowDown","ArrowRight","ArrowUp","ArrowLeft","Home","End"].includes(event.key)) {
          event.preventDefault();
          const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length-1 : (index+(["ArrowDown","ArrowRight"].includes(event.key)?1:-1)+buttons.length)%buttons.length;
          buttons[next].focus(); selectSection(buttons[next].dataset.settingsSection);
        }
      });
    });
    $("settings-fields").addEventListener("input", updateDirty);
    $("settings-fields").addEventListener("change", updateDirty);
    $("setting-name-template").addEventListener("input", previewName);
    $("setting-output-root").addEventListener("change", rootNote);
    $("btn-reset-settings-section").addEventListener("click", resetSection);
    document.querySelectorAll("[data-name-token]").forEach(button => button.addEventListener("click", () => {
      const input = $("setting-name-template"), start = input.selectionStart ?? input.value.length, end = input.selectionEnd ?? start;
      input.value = input.value.slice(0,start) + button.dataset.nameToken + input.value.slice(end); input.focus(); updateDirty(); previewName();
    }));
    $("btn-export-templates").addEventListener("click", () => exportTemplates(state.taskTemplates.map(t => t.id)));
    $("btn-import-templates").addEventListener("click", () => {if (!transferBusy) $("template-import-file").click();});
    $("template-import-file").addEventListener("change", () => importFile($("template-import-file").files[0]));
    $("btn-notification-permission").addEventListener("click", requestPermission);
    $("btn-test-notification").addEventListener("click", () => deliver({status:"succeeded",source:"旅行视频.mp4"}, true));
    $("btn-test-sound").addEventListener("click", async () => {try {await playSound(); permissionNote("测试声音已请求播放。");} catch (err) {permissionNote(UI.error(err));}});
    $("setting-notify-sound").addEventListener("change", async () => {if ($("setting-notify-sound").checked) try {await playSound();} catch(err) {permissionNote(UI.error(err));}});
    $("cleanup-days").addEventListener("change", invalidateCleanup);
    document.querySelectorAll("[data-cleanup-status]").forEach(input => input.addEventListener("change", invalidateCleanup));
    $("btn-preview-cleanup").addEventListener("click", previewCleanup);
    $("btn-confirm-cleanup").addEventListener("click", confirmCleanup);
  }
  return {init, wire, open, save, close, canClose, dirty, selectSection, resetSection, previewName, refreshTemplates, removeTemplate, copyTemplate, exportTemplates, importFile, previewCleanup, confirmCleanup, pollEvents, validPrefs, refreshSeconds: () => prefs.refresh};
})();
