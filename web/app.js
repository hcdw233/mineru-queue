const apiBase = (window.MINERU_MONITOR?.apiBase || "").replace(/\/$/, "");
const cards = document.querySelector("#cards");
const completedCount = document.querySelector("#completed-count");
const completedDetail = document.querySelector("#completed-detail");
const completionFill = document.querySelector("#completion-fill");
const rollingCount = document.querySelector("#rolling-count");
const rollingComparison = document.querySelector("#rolling-comparison");
const banner = document.querySelector("#banner");
const clock = document.querySelector("#clock");
const chart = document.querySelector("#chart");
const table = document.querySelector("#buckets");
const dateInput = document.querySelector("#date");
const hourInput = document.querySelector("#hour");
const hourLabel = document.querySelector("#hour-label");
const viewHour = document.querySelector("#view-hour");
const viewDay = document.querySelector("#view-day");
const refreshButton = document.querySelector("#refresh");
const windowSpeed = document.querySelector("#window-speed");
const refreshIntervalMs = 60 * 1000;

let view = "day";
let currentBuckets = [];
let currentSpeed = {};
let barHits = [];
let selectedBucket = null;

for (let hour = 0; hour < 24; hour += 1) {
  const option = document.createElement("option");
  option.value = String(hour);
  option.textContent = `${String(hour).padStart(2, "0")}:00`;
  hourInput.append(option);
}

function todayValue(now) {
  const local = now ? new Date(now) : new Date();
  const month = String(local.getMonth() + 1).padStart(2, "0");
  const day = String(local.getDate()).padStart(2, "0");
  return `${local.getFullYear()}-${month}-${day}`;
}

dateInput.value = todayValue();
hourInput.value = String(new Date().getHours());

viewHour.addEventListener("click", () => setView("hour"));
viewDay.addEventListener("click", () => setView("day"));
dateInput.addEventListener("change", () => refresh());
hourInput.addEventListener("change", () => refresh());
refreshButton.addEventListener("click", () => refresh());

function setView(next) {
  view = next;
  viewHour.classList.toggle("active", view === "hour");
  viewDay.classList.toggle("active", view === "day");
  hourLabel.hidden = view !== "hour";
  refresh();
}

async function refresh() {
  refreshButton.disabled = true;
  try {
    const summary = await getJSON("/api/summary");
    const params = new URLSearchParams({ view, date: dateInput.value });
    if (view === "hour") params.set("hour", hourInput.value);
    const series = await getJSON(`/api/series?${params}`);
    banner.hidden = true;
    renderSummary(summary);
    selectedBucket = null;
    renderSeries(series.buckets || [], series.speed || {});
    clock.textContent = summary.server_now ? `上次刷新 ${summary.server_now}` : "上次刷新";
  } catch (error) {
    banner.hidden = false;
    banner.textContent = `读不到进度 API：${error.message}`;
  } finally {
    refreshButton.disabled = false;
  }
}

async function getJSON(path) {
  const response = await fetch(`${apiBase}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${response.status}`);
  }
  return response.json();
}

function renderSummary(summary) {
  const latest = summary.latest || {};
  const speed = summary.speed || {};
  const total = summary.total_tasks || 0;
  const completed = summary.completed_tasks ?? latest.success ?? 0;
  const remaining = summary.remaining_tasks ?? Math.max(0, total - completed);
  const completionPercent = total ? Math.round((completed / total) * 100) : 0;
  completedCount.textContent = String(completed);
  completedDetail.textContent = total
    ? `任务表共 ${total} 篇 · 待完成 ${remaining} 篇 · 完成率 ${completionPercent}%`
    : "任务表暂无任务";
  completionFill.style.width = `${completionPercent}%`;
  completionFill.parentElement.setAttribute("aria-valuenow", String(completionPercent));
  const recent = speed.recent_hour_success || 0;
  const previous = speed.previous_rolling_hour_success || 0;
  const delta = recent - previous;
  rollingCount.textContent = String(recent);
  rollingComparison.textContent = `前一个 60 分钟 ${previous} 篇 · ${delta > 0 ? "增加" : delta < 0 ? "减少" : "持平"}${delta === 0 ? "" : ` ${Math.abs(delta)} 篇`}`;
  const items = [
    ["本小时成功", `${speed.current_clock_hour_success || 0} 篇`, "从整点到现在"],
    ["上个完整小时", `${speed.previous_clock_hour_success || 0} 篇`, "整整 60 分钟"],
    ["今天成功", `${speed.today_success || 0} 篇`, "从今天 00:00 起"],
    ["最近 10 分钟", `${speed.recent_10_minutes_success || 0} 篇`, "观察短时变化"],
    ["近 60 分钟失败 / 其他", `${speed.recent_hour_failed || 0} / ${speed.recent_hour_other || 0}`, "成功数不含这些任务"],
    ["近 60 分钟单篇耗时", formatDuration(speed.average_duration_seconds), "仅统计有开始时间的成功记录"],
  ];
  cards.replaceChildren(...items.map(([label, value, note]) => {
    const card = document.createElement("article");
    card.className = "card";
    const name = document.createElement("span");
    name.textContent = label;
    const number = document.createElement("strong");
    number.textContent = String(value);
    const description = document.createElement("small");
    description.textContent = note;
    card.append(name, number, description);
    return card;
  }));
}

function formatDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds >= 60) return `${(seconds / 60).toFixed(1)} 分钟`;
  return `${Math.round(seconds)} 秒`;
}

function speedScope(bucket) {
  if (bucket) {
    return view === "day" ? `${bucket.label} 起 1 小时` : `${bucket.label} 起 5 分钟`;
  }
  if (view === "hour") {
    const hour = String(hourInput.value).padStart(2, "0");
    return `${dateInput.value} ${hour}:00 起 1 小时`;
  }
  return `${dateInput.value} 全天`;
}

function renderWindowSpeed(speed, bucket) {
  const source = bucket || speed || {};
  const timed = source.timed_success_events || 0;
  const finished = bucket ? source.success || 0 : source.success_events || 0;
  const failed = bucket ? source.failed || 0 : source.failed_events || 0;
  const other = bucket ? source.other || 0 : source.other_events || 0;
  const items = [
    [speedScope(bucket), `${finished} 篇成功`],
    ["失败", `${failed} 篇`],
    ["其他", `${other} 篇`],
    ["成功任务平均耗时", timed ? formatDuration(source.average_duration_seconds) : "—"],
  ];
  windowSpeed.replaceChildren(...items.map(([label, value]) => {
    const item = document.createElement("p");
    if (bucket) item.className = "selected";
    const name = document.createElement("span");
    name.textContent = label;
    const number = document.createElement("strong");
    number.textContent = value;
    item.append(name, number);
    return item;
  }));
}

function renderSeries(buckets, speed) {
  currentBuckets = buckets;
  currentSpeed = speed;
  renderWindowSpeed(speed, selectedBucket);
  drawChart(buckets, selectedBucket);
  const head = document.createElement("tr");
  for (const title of ["时间", "成功篇数", "失败", "其他", "完成合计"]) {
    const cell = document.createElement("th");
    cell.textContent = title;
    head.append(cell);
  }
  table.replaceChildren(head);
  for (const bucket of buckets) {
    const row = document.createElement("tr");
    for (const value of [
      bucket.label,
      bucket.success,
      bucket.failed,
      bucket.other,
      bucket.success + bucket.failed + bucket.other,
    ]) {
      const cell = document.createElement("td");
      cell.textContent = String(value ?? 0);
      row.append(cell);
    }
    table.append(row);
  }
  const totals = buckets.reduce((sum, bucket) => {
    sum.success += bucket.success;
    sum.failed += bucket.failed;
    sum.other += bucket.other;
    return sum;
  }, { success: 0, failed: 0, other: 0 });
  const totalRow = document.createElement("tr");
  totalRow.className = "total-row";
  for (const value of ["合计", totals.success, totals.failed, totals.other, totals.success + totals.failed + totals.other]) {
    const cell = document.createElement("td");
    cell.textContent = String(value);
    totalRow.append(cell);
  }
  table.append(totalRow);
}

function drawChart(buckets, active) {
  const width = chart.clientWidth || 960;
  const height = 280;
  const ratio = window.devicePixelRatio || 1;
  chart.width = Math.floor(width * ratio);
  chart.height = Math.floor(height * ratio);
  const ctx = chart.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const max = Math.max(1, ...buckets.map((bucket) => bucket.success + bucket.failed + bucket.other));
  const left = 42;
  const bottom = 28;
  const plotWidth = width - left - 8;
  const plotHeight = height - bottom - 10;
  const gap = width < 600 ? 3 : 6;
  const barWidth = Math.max(4, (plotWidth - gap * buckets.length) / Math.max(buckets.length, 1));
  barHits = [];
  ctx.strokeStyle = "#e4dccf";
  ctx.fillStyle = "#6d6458";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "right";
  for (const value of new Set([0, Math.ceil(max / 2), max])) {
    const y = height - bottom - (value / max) * plotHeight;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(width - 8, y);
    ctx.stroke();
    ctx.fillText(String(value), left - 7, y + 4);
  }
  ctx.beginPath();
  ctx.moveTo(left, 10);
  ctx.lineTo(left, height - bottom);
  ctx.stroke();
  buckets.forEach((bucket, index) => {
    const x = left + gap / 2 + index * (barWidth + gap);
    barHits.push({ x, width: barWidth, bucket });
    if (bucket === active) {
      ctx.fillStyle = "rgba(31, 122, 77, 0.12)";
      ctx.fillRect(x - 2, 10, barWidth + 4, height - bottom - 10);
    }
    let y = height - bottom;
    for (const [key, color] of [["success", "#1f7a4d"], ["failed", "#a33b32"], ["other", "#b86e2a"]]) {
      const barHeight = (bucket[key] / max) * plotHeight;
      y -= barHeight;
      ctx.fillStyle = color;
      ctx.fillRect(x, y, barWidth, barHeight);
    }
    ctx.fillStyle = "#6d6458";
    ctx.font = "11px sans-serif";
    ctx.textAlign = "center";
    if (buckets.length <= 12 || index % (width < 600 ? 4 : 2) === 0) {
      ctx.fillText(bucket.label, x + barWidth / 2, height - 10);
    }
  });
}

function bucketAt(offsetX) {
  return barHits.find((hit) => offsetX >= hit.x && offsetX <= hit.x + hit.width)?.bucket || null;
}

chart.addEventListener("mousemove", (event) => {
  const bucket = bucketAt(event.offsetX);
  if (bucket === selectedBucket) return;
  selectedBucket = bucket;
  renderWindowSpeed(currentSpeed, selectedBucket);
  drawChart(currentBuckets, selectedBucket);
});

chart.addEventListener("mouseleave", () => {
  if (!selectedBucket) return;
  selectedBucket = null;
  renderWindowSpeed(currentSpeed, null);
  drawChart(currentBuckets, null);
});

window.addEventListener("resize", () => drawChart(currentBuckets, selectedBucket));
setView("day");
setInterval(() => refresh(), refreshIntervalMs);
