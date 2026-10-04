/* Chinese presentation only: protocol values, names and raw diagnostics stay intact. */
"use strict";
var UI = (() => {
  const choices = {
    auto:"自动", off:"关闭", none:"无", default:"默认", custom:"自定义",
    ultrafast:"极速", superfast:"超快", veryfast:"很快", faster:"较快", fast:"快速", medium:"均衡", slow:"慢速", slower:"更慢", veryslow:"很慢", placebo:"极慢（最高计算开销）",
    film:"电影", animation:"动画", grain:"保留颗粒", stillimage:"静态图像", psnr:"峰值信噪比测试（PSNR）", ssim:"结构相似度测试（SSIM）", fastdecode:"快速解码", zerolatency:"低延迟",
    baseline:"基础档次", main:"主档次", high:"高档次", high10:"高档次（10 位）", high422:"高档次（4:2:2）", high444:"高档次（4:4:4）",
    nlmeans:"非局部均值降噪（NLMeans）", hqdn3d:"三维降噪（HQDN3D）", bob:"逐场去隔行（双倍帧率）", loose:"宽松",
    rf:"恒定质量（RF）", crf:"恒定质量（CRF）", cqp:"恒定量化（CQP）", constant:"恒定质量", abr:"平均码率（ABR）", vbr:"可变码率（VBR）", lossless:"无损",
    av_mp4:"MP4", av_mkv:"MKV", mp4:"MP4", mkv:"MKV", webm:"WebM",
    mono:"单声道", stereo:"立体声", left_only:"仅左声道", right_only:"仅右声道", dpl1:"杜比环绕", dpl2:"杜比定向逻辑 II", "5point1":"5.1 声道", "6point1":"6.1 声道", "7point1":"7.1 声道", "5_2_lfe":"5.2 声道（含低频）",
    add:"添加字幕", "add-first":"添加首条字幕", burn:"烧录字幕", foreign:"外语字幕搜索",
  };
  const status = {implemented:"已实现",verified:"已验证",unverified:"未验证",disabled:"未启用",blocked:"受限",unknown:"未知"};
  const errors = [
    [/output (?:already )?exists|reserved by another task/i,"输出位置已被占用，请更换文件名或启用自动编号。"],
    [/input and output must be different/i,"源文件与输出文件不能相同。"],
    [/storage root is unavailable|mount marker is missing/i,"存储位置不可用，请检查目录和挂载标记。"],
    [/path escapes|absolute paths|drive-qualified|\.\.[' ] segments/i,"路径不安全，请使用存储位置内的相对路径。"],
    [/path does not exist|input is not a file/i,"源文件不存在或不是文件，请重新选择。"],
    [/read-only|must be writable/i,"输出位置只读，请选择可写存储位置。"],
    [/unsupported output name placeholder|output name.*characters/i,"命名模板无效，请使用支持的占位符并检查长度。"],
    [/output filename is required/i,"请填写输出文件名，不能使用目录作为输出。"],
    [/no available output name|collision limit/i,"同名编号已达上限，请修改输出文件名。"],
    [/unknown default task template|unknown template id/i,"模板已不存在，请重新选择。"],
    [/cleanup preview expired/i,"清理预览已过期，请重新预览后确认。"],
    [/unsupported template version|unsupported template bundle/i,"模板文件版本或格式不受支持。"],
    [/JSON|Unexpected token|Unexpected end/i,"JSON 文件格式无效，请检查文件内容。"],
    [/preset id\/name mismatch|unknown or ambiguous preset/i,"预设身份无效或存在同名预设，请重新选择。"],
    [/bitrate.*required|requires.*bitrate|bitrate_kbps.*required/i,"码率模式需要填写有效的视频码率。"],
    [/unknown runtime setting|unknown naming field|unknown template field/i,"提交内容包含不支持的字段，请检查设置或模板文件。"],
    [/output collision policy/i,"同名处理策略无效，请选择拒绝或自动编号。"],
    [/max_concurrent_jobs must be/i,"并发任务数必须是 1 到 8 之间的整数。"],
    [/job_timeout_seconds must be/i,"编码超时必须是 0 到 86400 之间的整数秒数。"],
    [/invalid template name\/description/i,"请填写有效的模板名称与说明，名称不超过 80 字。"],
    [/invalid spec/i,"编码参数无效，请检查当前设置。"],
    [/not available|encoder.*absent|encoder.*blocked|admission/i,"编码器当前不可用，请检查引擎、硬件和驱动。"],
    [/timed out|timeout|超时/i,"操作超时，请稍后重试或检查服务。"],
    [/unauthorized|401/i,"鉴权失败，请检查访问令牌。"],
    [/Failed to fetch|NetworkError|network|offline/i,"无法连接服务，请检查网络和服务状态。"],
    [/cancel requested/i,"已请求取消，正在等待任务停止。"],
    [/canceled before publication/i,"任务已取消，未发布输出文件。"],
    [/service restarted while/i,"服务重启导致任务中断，请重新启动任务。"],
    [/output validation failed/i,"输出校验未通过，未发布文件，请查看原始日志。"],
  ];
  const rawErrors = [];
  function raw(error) { return String(error?.rawMessage ?? error?.message ?? error ?? ""); }
  function error(error) {
    const text = raw(error);
    if (!text) return "操作失败，请稍后重试。";
    const known = errors.find(([pattern]) => pattern.test(text));
    // Already localised application messages must keep their useful context.
    const translated = /[㐀-鿿]/.test(text) ? text : known ? known[1] : error?.status === 409 ? "操作存在冲突，请刷新状态后重试。" : "操作失败，请查看原始错误信息。";
    if (translated !== text && !rawErrors.includes(text)) { rawErrors.push(text); if (rawErrors.length > 20) rawErrors.shift(); }
    const box = typeof document !== "undefined" ? document.getElementById("ui-error-list") : null;
    if (box) {box.textContent = rawErrors.join("\n\n"); document.getElementById("ui-error-details").hidden = !rawErrors.length;}
    return translated;
  }
  function english(value) {return /[A-Za-z]{3}/.test(String(value || "")) && !/[㐀-鿿]/.test(String(value || ""));}
  function preset(preset, source = "official") {
    const original = preset.name || "";
    if (source !== "official") return {name:original,description:preset.description || "此预设暂无说明。",original:"",rawDescription:""};
    let name = original.split("/").pop();
    const replacements = [[/\bSuper HQ\b/g,"超高质量"],[/\bHQ\b/g,"高质量"],[/\bVery Fast\b/g,"极速"],[/\bFast\b/g,"快速"],[/\bSurround\b/g,"环绕声"],[/\bProduction\b/g,"制作"],[/\bStandard\b/g,"标准"],[/\bProxy\b/g,"代理剪辑"],[/\bSocial\b/g,"社交平台"],[/\bLarge\b/g,"大尺寸"],[/\bSmall\b/g,"小尺寸"],[/\bMinutes\b/g,"分钟"],[/\bGmail\b/g,"邮件分享"]];
    for (const [pattern,text] of replacements) name = name.replace(pattern,text);
    return {name,original,description:english(preset.description) ? "官方预设，以引擎参数为准。可展开查看原始说明。" : preset.description || "官方预设，以引擎参数为准。",rawDescription:english(preset.description) ? preset.description : ""};
  }
  function initMessages() {
    if (typeof MutationObserver === "undefined") return;
    const ids = ["settings-message","template-transfer-message","template-message","cleanup-message","create-message","queue-message","detail-message","browser-message","codec-message","notification-permission","name-preview"];
    for (const id of ids) {
      const node = document.getElementById(id);
      if (!node) continue;
      const update = () => {
        const text = node.textContent;
        node.classList.toggle("ui-message-error", /失败|无效|无法|不可用|已拒绝|被阻止/.test(text));
        node.classList.toggle("ui-message-success", /已保存|已导入|已导出|已创建|已清理|已复制|检测完成/.test(text) && !/失败|无效/.test(text));
      };
      new MutationObserver(update).observe(node, {childList:true,subtree:true,characterData:true});
      update();
    }
  }
  return {choices,status,error,raw,english,preset,initMessages};
})();
