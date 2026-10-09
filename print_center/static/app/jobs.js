// The job list: current jobs, recent problems and history.
import { $, state, make, icon, showToast, api, emptyState } from "./core.js";

function dateLabel(value) {
  if (!value) return "";
  const date = typeof value === "number" ? new Date(value * (value < 1e12 ? 1000 : 1)) : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-Hant", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

const activeJobStates = [3, 4, 5, 6];
// A job that ended badly stays with the current ones this long, so the reason is still in view.
const recentProblemSeconds = 30 * 60;
const jobActions = {
  hold: { label: "暫停", busy: "暫停中…" },
  release: { label: "繼續", busy: "繼續中…" },
  cancel: { label: "取消", busy: "取消中…" },
};

function isActiveJob(job) {
  return activeJobStates.includes(Number(job.state));
}

function isCurrentJob(job) {
  if (isActiveJob(job)) return true;
  const ended = Number(job.completed_at || job.created_at) || 0;
  return job.attention === true && Date.now() / 1000 - ended < recentProblemSeconds;
}

async function runJobAction(job, action, button) {
  button.disabled = true;
  button.textContent = jobActions[action].busy;
  try {
    await api(`/api/jobs/${encodeURIComponent(job.id)}/${action}`, { method: "POST" });
    if (action === "cancel") showToast("已取消列印任務。已印出的紙張無法收回。");
    else if (action === "hold") showToast(Number(job.state) === 5 ? "已暫停。按「繼續」後會從第一頁重新列印。" : "已暫停，按「繼續」才會列印。");
    else showToast("已繼續，任務重新排隊列印。");
    await refreshJobs();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
    button.textContent = jobActions[action].label;
  }
}

function jobItem(job) {
  const item = make("div", job.attention ? "job-item attention" : "job-item");
  const symbol = make("span", "job-symbol");
  symbol.append(icon("file"));
  const info = make("div", "job-info");
  const title = make("p", "job-title", job.title || `列印任務 #${job.id}`);
  title.title = job.title || "";
  const printer = state.printers.find((p) => p.name === job.printer);
  info.append(title, make("p", "job-meta", [`#${job.id}`, printer?.label || job.printer, dateLabel(job.created_at)].filter(Boolean).join(" · ")));
  const reasons = Array.isArray(job.reasons) ? job.reasons.filter((reason) => typeof reason === "string") : [];
  if (reasons.length) info.append(make("p", "job-reasons", reasons.join("、")));
  if (typeof job.message === "string" && job.message) {
    // The printer's own words, untranslated.
    const message = make("p", "job-message");
    message.append(make("span", "", "打印機訊息"), make("q", "", job.message));
    info.append(message);
  }
  if (Number(job.state) === 4 && job.started) info.append(make("p", "job-note", "按「繼續」會從第一頁重新列印。"));
  if (job.printer_stopped && isActiveJob(job)) info.append(make("p", "job-note", "處理好打印機後，請家中管理員到管理後台按「恢復」。"));
  const actions = make("div", "job-actions");
  const jobState = Number(job.state);
  let className = "job-state";
  if (jobState === 5) className += " processing";
  if (jobState === 9) className += " completed";
  if (jobState === 8 || jobState === 6 || (jobState !== 9 && job.attention)) className += " failed";
  actions.append(make("span", className, job.state_label || "等待列印"));
  const buttons = make("div", "job-buttons");
  for (const action of ["release", "hold", "cancel"]) {
    const allowed = { hold: job.can_hold, release: job.can_release, cancel: job.can_cancel }[action];
    if (!allowed) continue;
    const button = make("button", `job-button ${action}-job`, jobActions[action].label);
    button.type = "button";
    button.setAttribute("aria-label", `${jobActions[action].label}任務 ${job.title || job.id}`);
    button.addEventListener("click", () => runJobAction(job, action, button));
    buttons.append(button);
  }
  if (buttons.children.length) actions.append(buttons);
  item.append(symbol, info, actions);
  return item;
}

function renderJobs(jobs) {
  // Polling repeats the same list; rebuilding it would drop focus from the buttons.
  const snapshot = JSON.stringify([jobs, state.printers.map((printer) => [printer.name, printer.label])]);
  if (snapshot === state.jobsSnapshot) return;
  state.jobsSnapshot = snapshot;
  const current = jobs.filter(isCurrentJob).sort((a, b) => (isActiveJob(b) - isActiveJob(a)) || (isActiveJob(a) ? a.id - b.id : b.id - a.id));
  const finished = jobs.filter((job) => !isCurrentJob(job));
  const list = $("jobs-list");
  if (current.length) list.replaceChildren(...current.map(jobItem));
  else emptyState(list, "目前沒有列印中的任務。\n出了狀況的任務會留在這裡，並附上打印機的說明。", "clock");
  $("jobs-history").hidden = finished.length === 0;
  $("jobs-history-count").textContent = ` ${finished.length}`;
  $("jobs-history-list").replaceChildren(...finished.slice(0, 20).map(jobItem));
  const troubled = current.filter((job) => job.attention).length;
  $("stage-alert").hidden = troubled === 0;
  $("stage-alert").textContent = troubled ? `${troubled} 份任務出了狀況，看看原因 ↓` : "";
}

export async function refreshJobs() {
  window.clearTimeout(state.jobsTimer);
  const data = await api("/api/jobs");
  const jobs = Array.isArray(data.jobs) ? data.jobs : [];
  renderJobs(jobs);
  // Follow unfinished jobs more closely than the rest of the page.
  if (jobs.some(isActiveJob)) state.jobsTimer = window.setTimeout(() => { if (!document.hidden) refreshJobs().catch(() => {}); }, 4000);
}
