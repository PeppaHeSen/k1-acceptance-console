"use strict";

const byId = id => document.getElementById(id);
const set = (id, text) => { byId(id).textContent = text; };
const form = byId("submitForm");
const stateNames = {queued:"排队中", running:"执行中", review:"待核对", disconnected:"失联待核对", done:"已结束"};
const resultNames = {pass:"通过", fail:"未通过", blocked:"阻塞", review:"待补验", error:"执行异常"};
let view = {sheets:{items:[]}, nodes:[], jobs:[]};
let stream;
let jobView;
let submitAttempt;
let loadingState = false;
let stateReady = false;
let pairings = [];
let requestedPairing;
let detailGeneration = 0;
const logs = new Set();
const time = value => new Date(value * 1000).toLocaleString("zh-CN");
const clockTime = value => new Date(value*1000).toLocaleTimeString("zh-CN",{hour12:false});
const loadedVersion = document.querySelector('meta[name="k1-version"]').content;
const caseLabels = {pending:"待执行",running:"验收中",confirm:"待确认",...resultNames};
let queueFilter = "all";
let queueSnapshot;
let sheetChoice = "";
let sheetRefreshError = "";
let branchLookup;
let inferredBranch = "";
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const transparency = byId("glassTransparency");
const themeChoices = [...document.querySelectorAll('input[name="theme"]')];
const settingsPages = {settingsHome:"设置",appearanceSettings:"外观",retentionForm:"任务保留"};
let settingsPage = "settingsHome";
let pageMotion;

function animatePage(page, returning) {
  pageMotion?.cancel();
  if (reducedMotion.matches || !page.animate) return;
  pageMotion = page.animate([
    {translate:(returning ? "-14px" : "14px")+" 0",opacity:.68},
    {translate:"0 0",opacity:1}
  ],{duration:260,easing:"cubic-bezier(.22,1,.36,1)"});
}

function renderAppearance() {
  const selected = themeChoices.find(input=>input.checked);
  byId("glassValue").value = transparency.value+"%";
  set("themeSummary",selected.labels[0].textContent.trim());
}

function showSettings(page, focus = byId("settingsTitle")) {
  const changed = settingsPage !== page;
  settingsPage = page;
  for (const key of Object.keys(settingsPages)) byId(key).hidden = key !== page;
  byId("settingsBack").hidden = page === "settingsHome";
  set("settingsTitle",settingsPages[page]);
  if (byId("settingsDialog").open) {
    focus.focus({preventScroll:true});
    if (changed) animatePage(byId(page),page==="settingsHome");
  }
}

function settingsBack() {
  showSettings("settingsHome",byId(settingsPage==="appearanceSettings" ? "appearanceButton" : "retentionButton"));
}

function retentionEffect() {
  const input = byId("retentionLimit");
  const removed = input.validity.valid ? Math.max(0,(view.retention?.completed || 0)-input.valueAsNumber) : 0;
  set("retentionEffect",removed ? `保存后将删除最早完成的 ${removed} 条任务。` : "");
  set("saveRetention",removed ? "保存并清理" : "保存");
  return removed;
}

async function api(path, body) {
  // Skip the browser's HTTP cache even when another tab is polling the same URL.
  const result = await fetch("/api/" + path, body === undefined ? {cache:"no-store", signal:AbortSignal.timeout(8000)} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
  });
  if (result.status === 401) throw new Error("请先登录验收台。");
  const data = await result.json();
  if (!result.ok) throw Object.assign(new Error(data.error || "操作失败。"), {status:result.status});
  return data;
}

function message(text) {
  byId("notice").hidden = !text;
  set("notice",text);
}

function label(text, tone = "") {
  const elem = document.createElement("span");
  elem.className = "pill " + tone;
  elem.textContent = text;
  return elem;
}

function jobStatus(job) {
  const status = job.state === "done" && job.stage === "已停止" ? "已停止"
    : job.state === "running" ? (job.stop_requested ? "正在停止" : "验收中") : stateNames[job.state];
  const badge = label(status,
                      job.state === "running" ? "active" : "");
  if (job.started) {
    const clock = document.createElement("time");
    clock.className = "mono elapsed";
    clock.setAttribute("aria-label","执行耗时");
    clock.dataset.start = job.started;
    clock.dataset.end = job.ended || "";
    badge.append(clock);
  }
  return badge;
}

function tick() {
  for (const clock of document.querySelectorAll("time[data-start]")) {
    const elapsed = Math.max(0,Math.floor((Number(clock.dataset.end) || Date.now()/1000)-Number(clock.dataset.start)));
    clock.textContent = [Math.floor(elapsed/3600),Math.floor(elapsed/60)%60,elapsed%60]
      .map(value=>String(value).padStart(2,"0")).join(":");
  }
}

function completion(job) {
  if (job.config.mode === "branch" && job.items.length === 1) {
    const cases = job.items[0].cases;
    if (cases?.length) {
      return [cases.filter(item=>["pass","fail","blocked"].includes(item.state)).length,cases.length,"用例"];
    }
    if (job.state === "running") return null;
  }
  return [job.items.filter(item=>item.result).length,job.items.length,"事项"];
}

function setProgress(bar, counts) {
  bar.classList.toggle("unknown",!counts);
  if (!counts) {
    bar.removeAttribute("aria-valuenow");
    bar.removeAttribute("aria-valuemax");
    bar.style.removeProperty("--progress");
    bar.setAttribute("aria-valuetext","进度待确认");
    return;
  }
  bar.removeAttribute("aria-valuetext");
  bar.setAttribute("aria-valuemax",Math.max(1,counts[1]));
  bar.setAttribute("aria-valuenow",counts[0]);
  bar.style.setProperty("--progress",100*counts[0]/Math.max(1,counts[1])+"%");
}

function followLatest() {
  const entries = byId("logEntries");
  if (byId("followLog").checked && !entries.hidden) entries.scrollTop = entries.scrollHeight;
}

function nodeName(id) {
  return view.nodes.find(node => node.id === id)?.name || "";
}

function modeSummary(config) {
  return config.mode === "version" ? `版本日验收 · ${config.sheet_name} · ${config.branch}`
    : `分支验收 · ${config.branch} · ${config.source}`;
}

function choices(select, options, placeholder) {
  const current = select.value;
  const values = placeholder ? [["",placeholder], ...options] : options;
  if (select.dataset.values === JSON.stringify(values)) return;
  select.replaceChildren(...values.map(([value,title]) => new Option(title,value)));
  select.dataset.values = JSON.stringify(values);
  if (values.some(([value]) => value === current)) select.value = current;
}

function modeChanged() {
  const branch = form.elements.mode.value === "branch";
  byId("branchFields").disabled = byId("branchFields").hidden = !branch;
  byId("versionFields").disabled = byId("versionFields").hidden = branch;
  nodeOptions();
}

async function findBranch() {
  const source = byId("source").value.trim();
  if (!source) {
    byId("source").reportValidity();
    return;
  }
  if (branchLookup?.source !== source) {
    branchLookup = {source, result:api("branches?source="+encodeURIComponent(source))};
  }
  const lookup = branchLookup;
  const button = byId("lookupBranch");
  const candidates = byId("branchCandidates");
  const previous = byId("featureBranch").value;
  button.disabled = true;
  candidates.hidden = true;
  set("branchLookupStatus","正在查找客户端分支…");
  try {
    const {branches,basis} = await lookup.result;
    if (source !== byId("source").value.trim() || byId("featureBranch").value!==previous) return;
    if (branches.length===1) {
      byId("featureBranch").value = inferredBranch = branches[0];
      set("branchLookupStatus",basis==="jira" ? "已按 Jira 关联核实分支。" : "已按单号匹配远端分支。");
    } else if (branches.length) {
      choices(candidates,branches.map(ref=>[ref,ref]),"请选择候选分支");
      candidates.value = "";
      candidates.hidden = false;
      set("branchLookupStatus","找到多个候选，请选择或手动填写。");
    } else {
      set("branchLookupStatus","未找到匹配的远端分支，请手动填写。");
    }
  } catch(error) {
    if (source===byId("source").value.trim()) set("branchLookupStatus",error.name==="TimeoutError" ? "查询超时，请重试或手动填写。" : error.message);
  } finally {
    if (branchLookup===lookup) {
      branchLookup = undefined;
      button.disabled = false;
    }
  }
}

function nodeOptions() {
  const filter = byId("platform").value;
  choices(byId("node"), view.nodes.filter(node => !filter || filter === node.os)
    .map(node => [node.id,`${node.name} · ${node.os} · ${nodeStatus(node)}`]), "自动分配");
}

function nodeStatus(node) {
  return !node.online ? "离线" : node.busy ? "执行中" : node.capabilities.length ? "可用" : "检查配置";
}

async function refreshState() {
  if (loadingState) return;
  loadingState = true;
  if (!stateReady) byId("retryQueue").disabled = true;
  try {
    const next = await api("state");
    if ((next.sheets.time ?? 0) < (view.sheets.time ?? 0)) next.sheets = view.sheets;
    view = next;
    if (!location.hash && !view.nodes.some(node => node.root)) location.hash = "#setup";
    if (location.hash === "#setup") {
      try {
        pairings = (await api("nodes/pairings")).items;
        renderPairings();
      }
      catch (error) { set("pairings",error.message); }
    }
    renderDashboard();
    renderSetup();
    if (!stateReady) {
      stateReady = true;
      byId("queueStatus").hidden = true;
      byId("queue").hidden = false;
      byId("online").disabled = byId("newJob").disabled = false;
    }
    if (view.retention) {
      set("retentionSummary",`最多 ${view.retention.limit} 条`);
      if (settingsPage==="retentionForm" && byId("settingsDialog").open) retentionEffect();
    }
    if (jobView) await refreshJob(jobView, detailGeneration);
    else renderRoute();
  } catch (error) {
    if (!stateReady && !byId("dashboard").hidden) {
      set("queueStatusText",error.name==="TimeoutError" ? "读取队列超时，正在重试。"
        : "暂时无法读取队列。"+error.message);
      byId("retryQueue").hidden = false;
    } else {
      message(error.name==="TimeoutError" ? "读取状态超时，稍后自动重试。" : error.message);
    }
  } finally {
    loadingState = false;
    byId("retryQueue").disabled = false;
  }
}

function renderSheets() {
  const {items, time: refreshedAt} = view.sheets;
  const select = byId("sheet");
  choices(select,items.map(sheet=>[sheet.id,sheet.name]),"请选择进包页签");
  if (!items.some(sheet=>sheet.id===sheetChoice)) sheetChoice = "";
  const newest = items.filter(sheet=>/^\d+$/.test(sheet.name.trim()))
    .sort((a,b)=>Number(b.name)-Number(a.name))[0] ?? items[0];
  select.value = sheetChoice || newest?.id || "";
  set("sheetTime",byId("refresh").disabled ? "正在读取钉钉进包表…" : sheetRefreshError ||
    (refreshedAt ? "最近成功刷新 " + time(refreshedAt) : "点击「刷新页签」加载钉钉进包表。"));
}

function renderDashboard() {
  renderSheets();
  set("online", `${view.nodes.filter(node => node.online).length} 台节点在线`);
  set("counts", `${view.jobs.filter(job=>job.state==="running").length} 批验收中 · ${view.jobs.filter(job=>job.state==="queued").length} 批排队`);
  nodeOptions();
  const jobs = view.jobs.filter(job=>queueFilter==="all" || (queueFilter==="running" ?
    ["running","review","disconnected"].includes(job.state) : job.state===queueFilter));
  const snapshot = JSON.stringify(jobs.map(job=>[job.id,job.state,job.config.branch,job.config.source,
    job.config.sheet_name,job.config.method,job.config.server,nodeName(job.node || job.assigned),
    job.started,job.ended,job.waiting || job.stage,job.stop_requested,canStop(job),completion(job)]));
  if (snapshot!==queueSnapshot) {
    queueSnapshot = snapshot;
    renderQueue(jobs);
  }
  byId("nodes").replaceChildren(...view.nodes.map(node => {
    const row = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = `${node.name} · ${node.os}`;
    row.append(title, label(nodeStatus(node),node.capabilities.length ? "pass" : "review"));
    return row;
  }));
  renderRelease();
  tick();
}

function canStop(job) {
  return job.state==="running" && view.nodes.some(node=>node.id===job.node && node.stop_supported);
}

function queueAction(job, action, caption) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary queue-action";
  button.textContent = caption;
  button.setAttribute("aria-label",caption+"批次 #"+job.id);
  button.addEventListener("click",async event=>{
    event.stopPropagation();
    if (!confirm(action==="stop" ? `停止批次 #${job.id}？节点完成清理后会更新状态。` : `删除等待中的批次 #${job.id}？`)) return;
    button.disabled = true;
    try {
      await api(`jobs/${job.id}/${action}`,{});
      message(action==="stop" ? `已请求停止批次 #${job.id}。` : `已删除批次 #${job.id}。`);
      await refreshState();
    } catch (error) {
      message(error.message);
      button.disabled = false;
    }
  });
  return button;
}

function renderQueue(jobs) {
  byId("queue").replaceChildren(...jobs.map(job => {
    const row = document.createElement("div");
    row.className = "job";
    row.addEventListener("click",event=>{
      if (!event.target.closest("button,a")) location.hash = `#batch-${job.id}`;
    });
    const group = document.createElement("span");
    const title = document.createElement("a");
    title.href = `#batch-${job.id}`;
    title.className = "job-title";
    title.textContent = job.config.mode==="branch" ? `#${job.id} · ${job.config.source}` : `#${job.id} · ${job.config.sheet_name} 版本日验收`;
    const detail = document.createElement("small");
    detail.textContent = [job.config.branch,job.config.method,job.config.server && job.config.server+" 服",
      nodeName(job.node || job.assigned)].filter(Boolean).join(" · ");
    group.append(title, detail);
    const phase = document.createElement("div");
    phase.className = "job-phase";
    const stage = document.createElement("div");
    const counts = completion(job);
    const description = document.createElement("span");
    description.textContent = job.waiting || job.stage;
    stage.append(description);
    if (counts && job.state!=="queued") {
      const count = document.createElement("span");
      count.className = "mono";
      count.textContent = counts[0]+" / "+counts[1];
      stage.append(count);
    }
    phase.append(stage);
    if (job.state!=="queued") {
      const progress = document.createElement("div");
      progress.className = "track";
      progress.setAttribute("role","progressbar");
      progress.setAttribute("aria-label",`批次 #${job.id} 验收进度`);
      const fill = document.createElement("span");
      fill.className = "track-fill";
      progress.append(fill);
      setProgress(progress,counts);
      phase.append(progress);
    }
    const open = document.createElement("span");
    open.className = "job-open";
    open.textContent = "详情 →";
    const controls = document.createElement("div");
    controls.className = "job-controls";
    if (job.state==="queued") controls.append(queueAction(job,"delete","删除"));
    else if (canStop(job)) {
      const stop = queueAction(job,"stop",job.stop_requested ? "正在停止" : "停止");
      stop.disabled = Boolean(job.stop_requested);
      controls.append(stop);
    }
    controls.append(open);
    row.append(group,phase,jobStatus(job),controls);
    return row;
  }));
  if (!jobs.length) set("queue","暂无符合条件的批次");
}

function renderRelease() {
  const release = view.release;
  byId("releaseButton").hidden = !release;
  if (!release) return;
  const show = loadedVersion !== release.version || view.nodes.some(node=>node.update && node.update!=="installed");
  byId("releaseButton").classList.toggle("update-available",show);
  set("releaseButton",show ? "有新版本 · "+release.version : "版本 "+release.version);
  set("releaseTitle","更新到 "+release.version);
  set("releaseNotes",release.notes);
  set("browserVersion","当前网页 "+loadedVersion);
  byId("reloadBrowser").disabled = loadedVersion===release.version;
  byId("nodeUpdates").replaceChildren(...view.nodes.map(node=>{
    const row = document.createElement("li");
    const title = document.createElement("span");
    title.textContent = node.name+" · "+nodeStatus(node);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = {installed:"已更新",requested:node.busy?"验收结束后更新":"等待更新",
      failed:"重试更新",available:node.busy?"结束后更新":"更新并重启"}[node.update] || "需初次部署";
    button.disabled = !node.update || ["installed","requested"].includes(node.update);
    button.addEventListener("click",async ()=>{
      button.disabled = true;
      try {
        await api("nodes/"+node.id+"/update",{target:release.worker_sha256});
        await refreshState();
      } catch(error) {
        button.disabled = false;
        set("releaseNotes",error.message);
      }
    });
    row.append(title,button);
    return row;
  }));
}

function renderSetup() {
  const menu = byId("setupNode");
  choices(menu,view.nodes.map(node => [node.id,`${node.name} · ${node.os}`]),"选择节点");
  const registered = pairings.find(pair => pair.ip === requestedPairing?.ip);
  if (registered?.node && view.nodes.some(node => node.id === registered.node)) {
    menu.value = registered.node;
    requestedPairing = undefined;
    message(`${registered.name} 已登记。请保存该节点的三端工程根目录。`);
  } else if (requestedPairing && !registered && Date.now() >= requestedPairing.deadline) {
    requestedPairing = undefined;
    message("等待配对已超时。请检查节点 IP 和执行程序，再点击「连接并登记」。");
  }
  const node = view.nodes.find(node => node.id === menu.value);
  if (!node) return set("checks","请先登记并启动执行节点。");
  if (byId("root").dataset.node !== node.id) {
    byId("root").value = node.root || "";
    byId("root").dataset.node = node.id;
  }
  if (!node.checks.length) return set("checks",node.root ? "等待节点返回检查结果。" : "保存三端工程根目录后开始检查。");
  byId("checks").replaceChildren(...node.checks.map(check => {
    const row = document.createElement("li");
    const lhs = document.createElement("span");
    lhs.textContent = check.label + (check.detail ? " · " + check.detail : "");
    row.append(lhs, label(check.ok ? "通过" : "待配置",check.ok ? "pass" : "review"));
    return row;
  }));
}

function renderPairings() {
  byId("pairings").replaceChildren(...pairings.map(pair => {
    const node = view.nodes.find(node => node.id === pair.node);
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = `${pair.name} · ${pair.ip}`;
    const status = node ? nodeStatus(node) : pair.node ? "已登记" : "等待配对";
    row.append(name,label(status,node?.capabilities.length ? "pass" : "review"));
    return row;
  }));
}

async function refreshJob(id, generation) {
  try {
    const job = await api("jobs/" + id);
    if (generation !== detailGeneration) return;
    set("detailTitle",`验收批次 #${job.id}`);
    set("context",[modeSummary(job.config),job.node_name || "等待节点",
      job.config.server ? job.config.server + " 服" : "", "操作人 " + (job.operator || "未记录")].filter(Boolean).join(" · "));
    byId("state").replaceChildren(jobStatus(job));
    set("stage",job.state==="running" && job.stop_requested ? "正在停止，等待节点清理。" : job.stage || "等待节点接单");
    byId("closeReview").hidden = job.state !== "review";
    const counts = completion(job);
    setProgress(byId("progress"),counts);
    set("progressCount",counts ? `${counts[0]} / ${counts[1]} ${counts[2]}` : "进度待确认");
    set("summary",Object.entries(resultNames).map(([key,name]) =>
      `${name} ${job.items.filter(item => item.result === key).length}`).join(" · ") + " · 未回写");
    const expanded = new Set([...byId("items").querySelectorAll("details[open]")].map(details=>details.dataset.item));
    byId("items").replaceChildren(...job.items.map(item => {
      const row = document.createElement("tr");
      for (const field of [`${item.key} · ${item.title}`,resultNames[item.result] || "待执行",item.evidence?.reason || ""]) {
        const cell = document.createElement("td");
        cell.textContent = field;
        row.append(cell);
      }
      if (item.cases?.length) {
        const details = document.createElement("details");
        details.className = "case-list";
        details.dataset.item = item.key;
        details.open = expanded.has(item.key) || job.config.mode==="branch";
        const title = document.createElement("summary");
        const completed = item.cases.filter(test=>["pass","fail","blocked"].includes(test.state)).length;
        title.textContent = `测试用例 · 已处理 ${completed} / ${item.cases.length}`;
        details.append(title);
        for (const test of item.cases) {
          const line = document.createElement("p");
          line.append(document.createTextNode(test.id+" · "+test.title+" "),label(caseLabels[test.state]));
          if (test.evidence) line.append(document.createTextNode(" · "+test.evidence));
          details.append(line);
        }
        row.firstElementChild.append(details);
      }
      return row;
    }));
    tick();
  } catch(error) {
    if (generation !== detailGeneration) return;
    if (error.status===404) location.hash = "#dashboard";
    message(error.message);
  }
}

function renderRoute(navigating = false) {
  const id = /^#batch-(\d+)$/.exec(location.hash)?.[1];
  const setup = location.hash === "#setup";
  const page = byId(id ? "detail" : setup ? "setup" : "dashboard");
  const changed = page.hidden || id !== jobView;
  document.body.classList.toggle("view-detail",Boolean(id));
  byId("dashboard").hidden = Boolean(id) || setup;
  byId("setup").hidden = !setup;
  byId("detail").hidden = !id;
  if (navigating && changed) {
    window.scrollTo(0,0);
    const heading = page.querySelector("h1");
    heading.tabIndex = -1;
    heading.focus({preventScroll:true});
    animatePage(page,!id && !setup);
  }
  if (id === jobView) return;
  jobView = id;
  detailGeneration += 1;
  stream?.close();
  logs.clear();
  byId("logEntries").replaceChildren();
  if (!id) return;
  const generation = detailGeneration;
  refreshJob(id,generation);
  let live = false;
  stream = new EventSource(`/api/jobs/${id}/events`);
  stream.onopen = () => { live = false; };
  stream.addEventListener("live",()=>{ live = true; });
  stream.onmessage = event => {
    if (generation !== detailGeneration || logs.has(event.lastEventId)) return;
    logs.add(event.lastEventId);
    const data = JSON.parse(event.data);
    const payload = JSON.parse(data.payload);
    const line = document.createElement("div");
    line.className = live && !reducedMotion.matches ? "entry is-new" : "entry";
    const stamp = document.createElement("time");
    stamp.textContent = clockTime(data.created);
    stamp.dateTime = new Date(data.created*1000).toISOString();
    const kind = document.createElement("span");
    kind.textContent = data.kind==="ai_output" ? "AI" : "节点";
    const content = document.createElement("p");
    content.textContent = payload.text || payload.conclusion || (data.kind === "execution_started"
      ? payload.runtime
        ? `Unity 工程 ${payload.root}\n客户端提交 ${payload.versions.client.target}\n验收交接文件 ${payload.runtime.manifest}`
        : "技能版本 " + payload.skill_version + "\n" + JSON.stringify(payload.versions,null,2)
      : data.kind === "case_snapshot" ? payload.key+" · 已同步 "+payload.cases.length+" 条用例"
      : `${payload.key || ""} ${resultNames[payload.result] || ""}`.trim() || data.kind);
    line.append(stamp,kind,content);
    byId("logEntries").append(line);
    followLatest();
  };
}

function action(element, handler) {
  element.addEventListener("submit",async event => {
    event.preventDefault();
    message("");
    const button = element.querySelector("button[type=submit]");
    button.disabled = true;
    try { await handler(new FormData(element)); }
    catch(error) { message(error.message); }
    finally { button.disabled = false; }
  });
}

form.addEventListener("change",modeChanged);
for (const [button,dialog] of [["online","nodeDialog"],["newJob","jobDialog"],["releaseButton","releaseDialog"]]) {
  byId(button).addEventListener("click",()=>byId(dialog).showModal());
}
document.querySelectorAll("[data-close-dialog]").forEach(element=>element.addEventListener("click",()=>element.closest("dialog").close()));
themeChoices.find(input=>input.value===consoleAppearance.theme).checked = true;
transparency.value = consoleAppearance.transparency;
renderAppearance();
reducedMotion.addEventListener("change",()=>{
  if (reducedMotion.matches) pageMotion?.cancel();
});
byId("appearanceSettings").addEventListener("input",()=>{
  consoleAppearance.update(themeChoices.find(input=>input.checked).value,transparency.valueAsNumber);
  renderAppearance();
});
byId("settingsButton").addEventListener("click",()=>{
  showSettings("settingsHome");
  byId("settingsDialog").showModal();
  byId("settingsTitle").focus();
});
byId("settingsBack").addEventListener("click",settingsBack);
byId("cancelRetention").addEventListener("click",settingsBack);
byId("appearanceButton").addEventListener("click",()=>showSettings("appearanceSettings"));
byId("settingsDialog").addEventListener("cancel",event=>{
  if (settingsPage==="settingsHome") return;
  event.preventDefault();
  settingsBack();
});
byId("retentionButton").addEventListener("click",()=>{
  const input = byId("retentionLimit");
  input.value = view.retention?.limit ?? 100;
  set("retentionError","");
  retentionEffect();
  showSettings("retentionForm",input);
  input.select();
});
byId("retentionLimit").addEventListener("input",retentionEffect);
byId("retentionForm").addEventListener("submit",async event=>{
  event.preventDefault();
  const button = byId("saveRetention");
  const input = byId("retentionLimit");
  const expected = retentionEffect();
  button.disabled = input.disabled = true;
  set("retentionError","");
  try {
    const result = await api("settings/retention",{limit:input.valueAsNumber,expected_deleted:expected});
    if (byId("settingsDialog").open && settingsPage==="retentionForm") settingsBack();
    await refreshState();
    if (result.removed) message(`已清理最早完成的 ${result.removed} 条任务。`);
  } catch (error) {
    set("retentionError",error.message);
    await refreshState();
  } finally {
    button.disabled = input.disabled = false;
  }
});
byId("reloadBrowser").addEventListener("click",()=>location.reload());
byId("retryQueue").addEventListener("click",refreshState);
byId("queueFilters").addEventListener("click",event=>{
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  queueFilter = button.dataset.filter;
  byId("queueFilters").querySelectorAll("button").forEach(item=>item.setAttribute("aria-pressed",String(item===button)));
  renderDashboard();
});
byId("summaryToggle").addEventListener("click",event=>{
  const button = event.currentTarget;
  const limited = button.getAttribute("aria-pressed")==="false";
  button.setAttribute("aria-pressed",String(limited));
  button.textContent = limited ? "显示全部详情" : "仅看进度";
  byId("checklist").hidden = byId("logsPanel").hidden = limited;
  if (!limited) followLatest();
});
byId("followLog").addEventListener("change",followLatest);
byId("logToggle").addEventListener("click",event=>{
  const entries = byId("logEntries");
  const expanded = entries.hidden;
  event.currentTarget.setAttribute("aria-expanded",String(expanded));
  byId("logsPanel").classList.toggle("collapsed",!expanded);
  entries.hidden = !expanded;
  if (expanded) followLatest();
});
for (const name of ["animationend","animationcancel"]) {
  byId("logEntries").addEventListener(name,event=>{
    if (event.animationName==="log-highlight") event.target.classList.remove("is-new");
  });
}
window.addEventListener("hashchange",renderRoute);
byId("setupNode").addEventListener("change",renderSetup);
byId("source").addEventListener("input",()=>{
  if (byId("featureBranch").value===inferredBranch) byId("featureBranch").value = "";
  inferredBranch = "";
  byId("branchCandidates").hidden = true;
  set("branchLookupStatus","");
});
byId("lookupBranch").addEventListener("click",findBranch);
byId("featureBranch").addEventListener("input",()=>{
  inferredBranch = "";
  set("branchLookupStatus","");
});
byId("branchCandidates").addEventListener("change",event=>{
  byId("featureBranch").value = inferredBranch = event.target.value;
});
byId("sheet").addEventListener("change",event=>{ sheetChoice = event.target.value; });
byId("refresh").addEventListener("click",async event => {
  const button = event.currentTarget;
  button.disabled = true;
  sheetRefreshError = "";
  renderSheets();
  try { view.sheets = await api("sheets/refresh",{}); }
  catch(error) { sheetRefreshError = error.message; }
  finally {
    button.disabled = false;
    renderSheets();
  }
});
action(form,async data => {
  const options = Object.fromEntries(data);
  if (options.mode==="branch" && !options.branch.trim()) {
    await findBranch();
    if (options.source.trim()!==byId("source").value.trim()) return;
    options.branch = byId("featureBranch").value.trim();
    if (!options.branch) return;
  }
  const signature = JSON.stringify(options);
  if (!submitAttempt || submitAttempt.signature !== signature) {
    const request = Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2,"0")).join("");
    submitAttempt = {signature,request};
  }
  const {id} = await api("jobs",{...options,request:submitAttempt.request});
  submitAttempt = null;
  sheetChoice = "";
  renderSheets();
  byId("jobDialog").close();
  message(`已提交批次 #${id}。`);
  await refreshState();
});
action(byId("setupForm"),async () => {
  await api(`nodes/${byId("setupNode").value}/setup`,{root:byId("root").value});
  await refreshState();
  message("已保存。等待节点检查结果。");
});
action(byId("register"),async data => {
  const result = await api("nodes/pairings",Object.fromEntries(data));
  pairings = [...pairings.filter(pair => pair.ip !== result.ip),result];
  requestedPairing = {ip:result.ip, deadline:Date.now() + 300000};
  renderPairings();
  message("等待执行程序配对。登记请求 5 分钟内有效。");
});
action(byId("closeReview"),async () => {
  await api(`jobs/${jobView}/close`,{reason:byId("closeReason").value});
  byId("closeReason").value = "";
  await refreshJob(jobView,detailGeneration);
});
renderRoute();
refreshState();
setInterval(refreshState,3000);
setInterval(tick,1000);
fetch("/operator",{cache:"no-store"}).then(response => response.ok ? response.json() : {}).then(({name}) => {
  if (name) {
    set("switchOperator",name + " · 切换");
    byId("operatorSession").hidden = false;
  }
}).catch(() => {});
