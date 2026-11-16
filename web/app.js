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
let pairings = [];
let requestedPairing;
let detailGeneration = 0;
const logs = new Set();
const time = value => new Date(value * 1000).toLocaleString("zh-CN");

async function api(path, body) {
  const result = await fetch("/api/" + path, body === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
  });
  if (result.status === 401) throw new Error("请先登录验收台。");
  const data = await result.json();
  if (!result.ok) throw new Error(data.error || "操作失败。");
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
  try {
    view = await api("state");
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
    if (jobView) await refreshJob(jobView, detailGeneration);
    else renderRoute();
  } catch (error) { message(error.message); }
  finally { loadingState = false; }
}

function renderDashboard() {
  choices(byId("sheet"),view.sheets.items.map(sheet => [sheet.id,sheet.name]),"请选择进包页签");
  set("sheetTime",view.sheets.time ? "最近成功刷新 " + time(view.sheets.time) : "点击「刷新页签」加载钉钉进包表。");
  set("online", `${view.nodes.filter(node => node.online).length} 台节点在线`);
  set("counts", `排队 ${view.jobs.filter(job => job.state === "queued").length} · 共 ${view.jobs.length} 批`);
  nodeOptions();
  byId("queue").replaceChildren(...view.jobs.map(job => {
    const row = document.createElement("a");
    row.href = `#batch-${job.id}`;
    row.className = "job";
    const group = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = `#${job.id} · ${modeSummary(job.config)}`;
    const detail = document.createElement("small");
    detail.textContent = job.waiting || `${nodeName(job.node)} · 已处理 ${job.items.filter(item => item.result).length}/${job.items.length}`;
    group.append(title, detail);
    row.append(group, label(stateNames[job.state] || job.state, job.state === "running" ? "active" : ""));
    return row;
  }));
  if (!view.jobs.length) set("queue","暂无批次");
  byId("nodes").replaceChildren(...view.nodes.map(node => {
    const row = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = `${node.name} · ${node.os}`;
    row.append(title, label(nodeStatus(node),node.capabilities.length ? "pass" : "review"));
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
      job.config.server ? job.config.server + " 服" : ""].filter(Boolean).join(" · "));
    set("state",stateNames[job.state] || job.state);
    set("stage",job.stage || "等待节点接单");
    byId("closeReview").hidden = job.state !== "review";
    byId("progress").max = Math.max(job.items.length,1);
    byId("progress").value = job.items.filter(item => item.result).length;
    set("summary",Object.entries(resultNames).map(([key,name]) =>
      `${name} ${job.items.filter(item => item.result === key).length}`).join(" · ") + " · 未回写");
    byId("items").replaceChildren(...job.items.map(item => {
      const row = document.createElement("tr");
      for (const field of [`${item.key} · ${item.title}`,resultNames[item.result] || "待执行",item.evidence?.reason || ""]) {
        const cell = document.createElement("td");
        cell.textContent = field;
        row.append(cell);
      }
      return row;
    }));
  } catch(error) { message(error.message); }
}

function renderRoute() {
  const id = /^#batch-(\d+)$/.exec(location.hash)?.[1];
  const setup = location.hash === "#setup";
  byId("dashboard").hidden = Boolean(id) || setup;
  byId("setup").hidden = !setup;
  byId("detail").hidden = !id;
  if (id === jobView) return;
  jobView = id;
  detailGeneration += 1;
  stream?.close();
  logs.clear();
  byId("logEntries").replaceChildren();
  if (!id) return;
  const generation = detailGeneration;
  refreshJob(id,generation);
  stream = new EventSource(`/api/jobs/${id}/events`);
  stream.onmessage = event => {
    if (generation !== detailGeneration || logs.has(event.lastEventId)) return;
    logs.add(event.lastEventId);
    const data = JSON.parse(event.data);
    const payload = JSON.parse(data.payload);
    const line = document.createElement("p");
    line.style.whiteSpace = "pre-wrap";
    line.textContent = payload.text || payload.conclusion || (data.kind === "execution_started"
      ? payload.runtime
        ? `Unity 工程 ${payload.root}\n客户端提交 ${payload.versions.client.target}\n验收交接文件 ${payload.runtime.manifest}`
        : "技能版本 " + payload.skill_version + "\n" + JSON.stringify(payload.versions,null,2)
      : `${payload.key || ""} ${resultNames[payload.result] || ""}`.trim() || data.kind);
    byId("logEntries").append(line);
    if (data.kind !== "ai_output") refreshJob(id,generation);
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
window.addEventListener("hashchange",renderRoute);
byId("setupNode").addEventListener("change",renderSetup);
byId("refresh").addEventListener("click",async event => {
  const button = event.currentTarget;
  button.disabled = true;
  message("正在读取钉钉进包表…");
  try { await api("sheets/refresh",{}); await refreshState(); message("页签已刷新。"); }
  catch(error) { message(error.message); }
  finally { button.disabled = false; }
});
action(form,async data => {
  const options = Object.fromEntries(data);
  const signature = JSON.stringify(options);
  if (!submitAttempt || submitAttempt.signature !== signature) {
    const request = Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2,"0")).join("");
    submitAttempt = {signature,request};
  }
  const {id} = await api("jobs",{...options,request:submitAttempt.request});
  submitAttempt = null;
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
fetch("/operator").then(response => response.ok ? response.json() : {}).then(({name}) => {
  if (name) {
    set("switchOperator",name + " · 切换");
    byId("operatorSession").hidden = false;
  }
}).catch(() => {});
