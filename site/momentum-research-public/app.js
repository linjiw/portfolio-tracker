const DATA_URL = new URL("./data/study.json", document.baseURI);
const SVG_NS = "http://www.w3.org/2000/svg";

const ALLOWED = Object.freeze({
  study: ["schemaVersion", "dataStatus", "siteTitle", "generatedDate", "notice", "strategies", "methodology", "limitations"],
  strategy: ["id", "name", "family", "summary", "exposureUnit", "metrics", "currentSnapshot", "currentModels", "series", "qualityLedger"],
  metrics: ["cagr", "maxDrawdown", "calmar", "annualTurnover", "status"],
  currentSnapshot: ["asOf", "mode", "newCapitalGate", "action", "basketBasis", "modelBasket", "cashWeight", "nextTrigger", "riskTrigger", "note"],
  currentModel: ["track", "label", "asOf", "riskyAsset", "targetExposure", "cashExposure", "action", "gate", "note"],
  point: ["date", "nav", "drawdown", "exposure", "decision"],
  decision: ["kind", "action", "regime", "reason", "targetExposure", "modelBasket"],
  basketItem: ["asset", "weight"],
  qualityLedger: ["top3", "top5"],
  qualityItem: ["rank", "asset", "momentumReturn", "status", "evidence"],
  methodology: ["title", "body"],
});

const DECISION_KINDS = Object.freeze([
  "ENTER",
  "ADD",
  "HOLD",
  "REDUCE",
  "EXIT",
  "REBALANCE",
  "BLOCK",
  "REFERENCE",
]);
const SNAPSHOT_MODES = Object.freeze(["PRODUCTION_HOLD", "SHADOW_ONLY", "BENCHMARK", "RESEARCH_BLOCKED"]);
const BASKET_BASES = Object.freeze(["account-target", "shadow-account-target", "benchmark", "research-sleeve"]);
const MODE_LABELS = Object.freeze({
  PRODUCTION_HOLD: "生产 · 持有",
  SHADOW_ONLY: "影子 · 不下单",
  BENCHMARK: "基准 · 不交易",
  RESEARCH_BLOCKED: "研究 · 已阻断",
});
const BASKET_BASIS_LABELS = Object.freeze({
  "account-target": "生产模型目标",
  "shadow-account-target": "影子模型目标",
  benchmark: "买入持有基准",
  "research-sleeve": "纸面研究篮子",
});
const KIND_LABELS = Object.freeze({
  ENTER: "ENTER · 进入",
  ADD: "ADD · 加仓",
  HOLD: "HOLD · 持有",
  REDUCE: "REDUCE · 减仓",
  EXIT: "EXIT · 退出",
  REBALANCE: "REBALANCE · 换仓",
  BLOCK: "BLOCK · 阻断",
  REFERENCE: "REFERENCE · 基准",
});

const FORBIDDEN_KEYS = new Set([
  "account",
  "accountid",
  "accountnumber",
  "position",
  "positions",
  "holding",
  "holdings",
  "quantity",
  "shares",
  "transaction",
  "transactions",
  "costbasis",
  "marketvalue",
  "cashflow",
  "broker",
  "email",
  "filepath",
  "absolutepath",
]);

const state = {
  study: null,
  strategyIndex: 0,
  pointIndex: 0,
  ledger: "top3",
};

const ui = {
  dataStatus: document.querySelector("#data-status"),
  generatedDate: document.querySelector("#generated-date"),
  strategySelect: document.querySelector("#strategy-select"),
  strategyFamily: document.querySelector("#strategy-family"),
  strategyName: document.querySelector("#strategy-name"),
  strategySummary: document.querySelector("#strategy-summary"),
  overviewGrid: document.querySelector("#overview-grid"),
  researchSection: document.querySelector("#research"),
  metricGrid: document.querySelector("#metric-grid"),
  comparisonBody: document.querySelector("#comparison-body"),
  navOutput: document.querySelector("#nav-output"),
  navTitle: document.querySelector("#nav-title"),
  drawdownOutput: document.querySelector("#drawdown-output"),
  exposureOutput: document.querySelector("#exposure-output"),
  exposureTitle: document.querySelector("#exposure-title"),
  timeSlider: document.querySelector("#time-slider"),
  selectedDate: document.querySelector("#selected-date"),
  prevDecision: document.querySelector("#prev-decision"),
  nextDecision: document.querySelector("#next-decision"),
  decisionShortcuts: document.querySelector("#decision-shortcuts"),
  decisionCard: document.querySelector("#decision-card"),
  decisionKind: document.querySelector("#decision-kind"),
  decisionDetail: document.querySelector("#decision-detail"),
  currentModel: document.querySelector("#current-model"),
  qualityBody: document.querySelector("#quality-body"),
  qualityAsOf: document.querySelector("#quality-asof"),
  methodList: document.querySelector("#method-list"),
  limitationList: document.querySelector("#limitation-list"),
  siteNotice: document.querySelector("#site-notice"),
  fatalError: document.querySelector("#fatal-error"),
  ledgerButtons: [...document.querySelectorAll("[data-ledger]")],
};

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function assertExactKeys(value, keys, path) {
  assert(isPlainObject(value), `${path} 必须是对象`);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  assert(
    actual.length === expected.length && actual.every((key, index) => key === expected[index]),
    `${path} 包含未允许或缺失的字段`,
  );
}

function assertString(value, path, maxLength = 600) {
  assert(typeof value === "string" && value.length > 0 && value.length <= maxLength, `${path} 文本无效`);
  assert(!/(?:\/Users\/|\/home\/|file:\/\/|[A-Za-z]:\\)/.test(value), `${path} 含本地绝对路径`);
  assert(!/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i.test(value), `${path} 含电子邮件地址`);
}

function assertNumber(value, path, min, max) {
  assert(Number.isFinite(value) && value >= min && value <= max, `${path} 数值越界`);
}

function scanForbiddenKeys(value, path = "study") {
  if (Array.isArray(value)) {
    value.forEach((item, index) => scanForbiddenKeys(item, `${path}[${index}]`));
    return;
  }
  if (!isPlainObject(value)) return;
  Object.entries(value).forEach(([key, child]) => {
    const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, "");
    assert(!FORBIDDEN_KEYS.has(normalized), `${path} 含禁止字段`);
    scanForbiddenKeys(child, `${path}.${key}`);
  });
}

function validateModelBasket(items, path) {
  assert(Array.isArray(items) && items.length <= 5, `${path} 无效`);
  let basketWeight = 0;
  items.forEach((item, index) => {
    const itemPath = `${path}[${index}]`;
    assertExactKeys(item, ALLOWED.basketItem, itemPath);
    assertString(item.asset, `${itemPath}.asset`, 16);
    assertNumber(item.weight, `${itemPath}.weight`, Number.EPSILON, 1.25);
    basketWeight += item.weight;
  });
  assert(basketWeight <= 1.250001, `${path} 总权重越界`);
  return basketWeight;
}

function validateDecision(decision, path) {
  if (decision === null) return;
  assertExactKeys(decision, ALLOWED.decision, path);
  assert(DECISION_KINDS.includes(decision.kind), `${path}.kind 无效`);
  assertString(decision.action, `${path}.action`, 80);
  assertString(decision.regime, `${path}.regime`, 80);
  assertString(decision.reason, `${path}.reason`, 300);
  assertNumber(decision.targetExposure, `${path}.targetExposure`, 0, 1.25);
  validateModelBasket(decision.modelBasket, `${path}.modelBasket`);
}

function validateCurrentSnapshot(snapshot, path) {
  assertExactKeys(snapshot, ALLOWED.currentSnapshot, path);
  assert(/^\d{4}-\d{2}-\d{2}$/.test(snapshot.asOf), `${path}.asOf 无效`);
  assert(SNAPSHOT_MODES.includes(snapshot.mode), `${path}.mode 无效`);
  assert(["ALLOW", "WATCH", "BLOCK", "NA"].includes(snapshot.newCapitalGate), `${path}.newCapitalGate 无效`);
  assertString(snapshot.action, `${path}.action`, 180);
  assert(BASKET_BASES.includes(snapshot.basketBasis), `${path}.basketBasis 无效`);
  const basketWeight = validateModelBasket(snapshot.modelBasket, `${path}.modelBasket`);
  assertNumber(snapshot.cashWeight, `${path}.cashWeight`, 0, 1);
  assert(Math.abs(basketWeight + snapshot.cashWeight - 1) <= 0.000001, `${path} 模型总权重必须为100%`);
  assertString(snapshot.nextTrigger, `${path}.nextTrigger`, 240);
  assertString(snapshot.riskTrigger, `${path}.riskTrigger`, 240);
  assertString(snapshot.note, `${path}.note`, 300);
}

function validateQuality(items, expectedLength, path) {
  assert(Array.isArray(items) && items.length === expectedLength, `${path} 长度必须为 ${expectedLength}`);
  items.forEach((item, index) => {
    const itemPath = `${path}[${index}]`;
    assertExactKeys(item, ALLOWED.qualityItem, itemPath);
    assert(item.rank === index + 1, `${itemPath}.rank 必须连续`);
    assertString(item.asset, `${itemPath}.asset`, 80);
    assertNumber(item.momentumReturn, `${itemPath}.momentumReturn`, -1, 1000);
    assert(["PASS", "WATCH", "BLOCK_DECISION_GRADE"].includes(item.status), `${itemPath}.status 无效`);
    assertString(item.evidence, `${itemPath}.evidence`, 240);
  });
}

function validateStrategy(strategy, index) {
  const path = `study.strategies[${index}]`;
  assertExactKeys(strategy, ALLOWED.strategy, path);
  assert(/^[a-z0-9][a-z0-9_-]{1,48}$/.test(strategy.id), `${path}.id 无效`);
  assertString(strategy.name, `${path}.name`, 80);
  assertString(strategy.family, `${path}.family`, 80);
  assertString(strategy.summary, `${path}.summary`, 300);
  assert(["percent", "multiplier"].includes(strategy.exposureUnit), `${path}.exposureUnit 无效`);

  assertExactKeys(strategy.metrics, ALLOWED.metrics, `${path}.metrics`);
  assertNumber(strategy.metrics.cagr, `${path}.metrics.cagr`, -1, 5);
  assertNumber(strategy.metrics.maxDrawdown, `${path}.metrics.maxDrawdown`, -1, 0);
  assertNumber(strategy.metrics.calmar, `${path}.metrics.calmar`, -20, 50);
  assertNumber(strategy.metrics.annualTurnover, `${path}.metrics.annualTurnover`, 0, 100);
  assert(["采用", "影子", "基准", "非决策级代理", "研究中"].includes(strategy.metrics.status), `${path}.metrics.status 无效`);

  validateCurrentSnapshot(strategy.currentSnapshot, `${path}.currentSnapshot`);

  assert(Array.isArray(strategy.currentModels) && strategy.currentModels.length === 2, `${path}.currentModels 必须包含两轨`);
  const tracks = new Set();
  strategy.currentModels.forEach((model, modelIndex) => {
    const modelPath = `${path}.currentModels[${modelIndex}]`;
    assertExactKeys(model, ALLOWED.currentModel, modelPath);
    assert(["existing-sleeve", "new-capital"].includes(model.track), `${modelPath}.track 无效`);
    assert(!tracks.has(model.track), `${modelPath}.track 重复`);
    tracks.add(model.track);
    assertString(model.label, `${modelPath}.label`, 80);
    assert(/^\d{4}-\d{2}-\d{2}$/.test(model.asOf), `${modelPath}.asOf 无效`);
    assertString(model.riskyAsset, `${modelPath}.riskyAsset`, 32);
    assertNumber(model.targetExposure, `${modelPath}.targetExposure`, 0, 1.25);
    assertNumber(model.cashExposure, `${modelPath}.cashExposure`, 0, 1);
    assert(model.targetExposure + model.cashExposure <= 1.250001, `${modelPath} 总暴露越界`);
    assertString(model.action, `${modelPath}.action`, 100);
    assert(["ALLOW", "WATCH", "BLOCK"].includes(model.gate), `${modelPath}.gate 无效`);
    assertString(model.note, `${modelPath}.note`, 240);
  });

  assert(Array.isArray(strategy.series) && strategy.series.length >= 8, `${path}.series 至少需要 8 个点`);
  let previousDate = "";
  strategy.series.forEach((point, pointIndex) => {
    const pointPath = `${path}.series[${pointIndex}]`;
    assertExactKeys(point, ALLOWED.point, pointPath);
    assert(/^\d{4}-\d{2}-\d{2}$/.test(point.date) && point.date > previousDate, `${pointPath}.date 必须递增`);
    previousDate = point.date;
    assertNumber(point.nav, `${pointPath}.nav`, 0.01, 10000);
    assertNumber(point.drawdown, `${pointPath}.drawdown`, -1, 0);
    assertNumber(point.exposure, `${pointPath}.exposure`, 0, 2);
    validateDecision(point.decision, `${pointPath}.decision`);
  });

  assertExactKeys(strategy.qualityLedger, ALLOWED.qualityLedger, `${path}.qualityLedger`);
  validateQuality(strategy.qualityLedger.top3, 3, `${path}.qualityLedger.top3`);
  validateQuality(strategy.qualityLedger.top5, 5, `${path}.qualityLedger.top5`);
}

function validateStudy(study) {
  assertExactKeys(study, ALLOWED.study, "study");
  scanForbiddenKeys(study);
  assert(study.schemaVersion === 1, "schemaVersion 不受支持");
  assert(["placeholder", "public-research"].includes(study.dataStatus), "dataStatus 无效");
  assertString(study.siteTitle, "study.siteTitle", 100);
  assert(/^\d{4}-\d{2}-\d{2}$/.test(study.generatedDate), "generatedDate 无效");
  assertString(study.notice, "study.notice", 300);
  assert(Array.isArray(study.strategies) && study.strategies.length >= 1, "strategies 不能为空");
  const ids = new Set();
  study.strategies.forEach((strategy, index) => {
    validateStrategy(strategy, index);
    assert(!ids.has(strategy.id), "strategy id 必须唯一");
    ids.add(strategy.id);
  });
  assert(Array.isArray(study.methodology) && study.methodology.length >= 2, "methodology 不完整");
  study.methodology.forEach((item, index) => {
    assertExactKeys(item, ALLOWED.methodology, `study.methodology[${index}]`);
    assertString(item.title, `study.methodology[${index}].title`, 100);
    assertString(item.body, `study.methodology[${index}].body`, 600);
  });
  assert(Array.isArray(study.limitations) && study.limitations.length >= 2, "limitations 不完整");
  study.limitations.forEach((item, index) => assertString(item, `study.limitations[${index}]`, 400));
  return study;
}

function makeSvgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
  return element;
}

function formatPercent(value, digits = 1) {
  return `${(value * 100).toFixed(digits)}%`;
}

function formatNumber(value, digits = 2) {
  return Number(value).toFixed(digits);
}

function formatDate(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00Z`));
}

function formatExposure(strategy, value, digits = 0) {
  return strategy.exposureUnit === "multiplier"
    ? `${Number(value).toFixed(digits === 0 ? 2 : digits)}×`
    : formatPercent(value, digits);
}

function makeElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function toneForMode(mode) {
  if (["SHADOW_ONLY", "RESEARCH_BLOCKED"].includes(mode)) return "violet";
  if (mode === "BENCHMARK") return "cyan";
  return "amber";
}

function toneForGate(gate) {
  if (gate === "BLOCK") return "red";
  if (gate === "WATCH") return "amber";
  if (gate === "NA") return "cyan";
  return "green";
}

function toneForKind(kind) {
  if (["ENTER", "ADD"].includes(kind)) return "green";
  if (kind === "HOLD") return "amber";
  if (["REBALANCE", "REFERENCE"].includes(kind)) return "cyan";
  return "red";
}

function inferActionKind(action) {
  const normalized = action.toUpperCase();
  if (/REBALANCE|再平衡|调仓/.test(normalized)) return "REBALANCE";
  if (/^\s*(HOLD|持有)|继续持有|沿用/.test(normalized)) return "HOLD";
  if (/REDUCE|减仓|降低/.test(normalized)) return "REDUCE";
  if (/EXIT|退出|清仓/.test(normalized)) return "EXIT";
  if (/BLOCK|阻断|禁止|不买/.test(normalized)) return "BLOCK";
  if (/ENTER|买入|进入|建仓/.test(normalized)) return "ENTER";
  if (/ADD|加仓/.test(normalized)) return "ADD";
  if (/REFERENCE|参考/.test(normalized)) return "REFERENCE";
  return "HOLD";
}

function kindForSnapshot(snapshot) {
  if (["PRODUCTION_HOLD", "SHADOW_ONLY"].includes(snapshot.mode)) return "HOLD";
  if (snapshot.mode === "BENCHMARK") return "REFERENCE";
  return "BLOCK";
}

function kindLabel(kind) {
  return KIND_LABELS[kind] ?? kind;
}

function gateLabel(gate) {
  return gate === "NA" ? "N/A · 基准" : gate;
}

function makeDecisionMarker(kind, cx, cy, selected) {
  const size = selected ? 4.2 : 3.2;
  const tone = toneForKind(kind);
  const attributes = { class: `decision-marker tone-${tone}` };
  if (["ENTER", "ADD"].includes(kind)) {
    return makeSvgElement("circle", { ...attributes, cx, cy, r: size });
  }
  if (kind === "HOLD") {
    return makeSvgElement("polygon", {
      ...attributes,
      points: `${cx},${cy - size} ${cx + size},${cy} ${cx},${cy + size} ${cx - size},${cy}`,
    });
  }
  if (["REBALANCE", "REFERENCE"].includes(kind)) {
    return makeSvgElement("rect", {
      ...attributes,
      x: cx - size,
      y: cy - size,
      width: size * 2,
      height: size * 2,
    });
  }
  return makeSvgElement("polygon", {
    ...attributes,
    points: `${cx},${cy - size} ${cx + size},${cy + size} ${cx - size},${cy + size}`,
  });
}

function makeSemanticBadge(text, tone, className = "") {
  const badge = makeElement("span", `semantic-badge tone-${tone}${className ? ` ${className}` : ""}`, text);
  badge.dataset.tone = tone;
  return badge;
}

function basketLabel(items) {
  return items.length
    ? items.map((item) => `${item.asset} ${formatPercent(item.weight, 1)}`).join(" · ")
    : "无风险资产 / 现金";
}

function chartValueFormat(kind, value, exposureUnit) {
  if (kind === "nav") {
    if (value >= 100) return formatNumber(value, 0);
    if (value >= 10) return formatNumber(value, 1);
    return formatNumber(value, 2);
  }
  if (kind === "exposure" && exposureUnit === "multiplier") return `${Number(value).toFixed(2)}×`;
  return formatPercent(value, 0);
}

function renderChart(svg, series, field, selectedIndex, exposureUnit = "percent", logScale = false, mode = "") {
  const width = 640;
  const height = 260;
  const margin = { top: 12, right: 14, bottom: 28, left: 48 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const transform = (value) => (logScale ? Math.log10(value) : value);
  const values = series.map((point) => transform(point[field]));
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (field === "drawdown") {
    max = 0;
    min = Math.min(min, -0.02);
  } else if (field === "exposure") {
    min = 0;
    max = Math.max(1.25, max);
  } else {
    const pad = Math.max((max - min) * 0.12, 0.02);
    min -= pad;
    max += pad;
  }
  const span = Math.max(max - min, 1e-9);
  const x = (index) => margin.left + (index / Math.max(series.length - 1, 1)) * innerWidth;
  const y = (value) => margin.top + ((max - transform(value)) / span) * innerHeight;
  const baseline = field === "drawdown" ? y(0) : margin.top + innerHeight;

  const chartLabels = {
    nav: [
      logScale ? "对数模型净值时间序列" : "模型净值时间序列",
      `所选策略的标准化模型净值${logScale ? "，纵轴为对数刻度" : ""}；不代表真实账户。`,
    ],
    drawdown: ["模型回撤时间序列", "所选策略相对阶段高点的回撤路径。"],
    exposure: ["模型暴露时间序列", "所选策略的内部目标暴露；不代表真实持仓。"],
  };
  const [chartTitle, chartDescription] = chartLabels[field];
  const titleId = `${svg.id}-title`;
  const descriptionId = `${svg.id}-description`;
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.setAttribute("aria-labelledby", `${titleId} ${descriptionId}`);
  const title = makeSvgElement("title", { id: titleId });
  const description = makeSvgElement("desc", { id: descriptionId });
  title.textContent = chartTitle;
  description.textContent = chartDescription;
  svg.append(title, description);

  for (let index = 0; index <= 4; index += 1) {
    const plottedValue = max - (span * index) / 4;
    const value = logScale ? 10 ** plottedValue : plottedValue;
    const gridY = margin.top + (index / 4) * innerHeight;
    svg.append(
      makeSvgElement("line", {
        class: "grid-line",
        x1: margin.left,
        x2: width - margin.right,
        y1: gridY,
        y2: gridY,
      }),
    );
    const label = makeSvgElement("text", {
      class: "axis-label",
      x: margin.left - 8,
      y: gridY + 3,
      "text-anchor": "end",
    });
    label.textContent = chartValueFormat(field, value, exposureUnit);
    svg.append(label);
  }

  const linePath = series
    .map((point, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(2)},${y(point[field]).toFixed(2)}`)
    .join(" ");
  const areaPath = `${linePath} L${x(series.length - 1).toFixed(2)},${baseline.toFixed(2)} L${x(0).toFixed(2)},${baseline.toFixed(2)} Z`;
  svg.append(makeSvgElement("path", { class: "data-area", d: areaPath }));
  svg.append(makeSvgElement("path", { class: "data-line", d: linePath }));

  series.forEach((point, index) => {
    if (point.decision !== null) {
      const marker = makeDecisionMarker(
        point.decision.kind,
        x(index),
        y(point[field]),
        index === selectedIndex,
      );
      const markerTitle = makeSvgElement("title");
      markerTitle.textContent = `${point.date} · ${kindLabel(point.decision.kind)} · ${point.decision.action}`;
      marker.append(markerTitle);
      svg.append(marker);
    }
  });

  const cursorX = x(selectedIndex);
  svg.append(
    makeSvgElement("line", {
      class: "cursor-line",
      x1: cursorX,
      x2: cursorX,
      y1: margin.top,
      y2: margin.top + innerHeight,
    }),
  );
  svg.append(
    makeSvgElement("circle", {
      class: "cursor-point",
      cx: cursorX,
      cy: y(series[selectedIndex][field]),
      r: 4,
    }),
  );

  const startLabel = makeSvgElement("text", {
    class: "axis-label",
    x: margin.left,
    y: height - 7,
    "text-anchor": "start",
  });
  startLabel.textContent = series[0].date.slice(0, 7);
  const endLabel = makeSvgElement("text", {
    class: "axis-label",
    x: width - margin.right,
    y: height - 7,
    "text-anchor": "end",
  });
  endLabel.textContent = series.at(-1).date.slice(0, 7);
  svg.append(startLabel, endLabel);
}

function appendTerm(list, label, value, wide = false) {
  const wrapper = document.createElement("div");
  if (wide) wrapper.className = "detail-list__wide";
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value;
  wrapper.append(term, description);
  list.append(wrapper);
}

function appendNodeTerm(list, label, node, wide = false) {
  const wrapper = document.createElement("div");
  if (wide) wrapper.className = "detail-list__wide";
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.append(node);
  wrapper.append(term, description);
  list.append(wrapper);
}

function currentStrategy() {
  return state.study.strategies[state.strategyIndex];
}

function renderOverview() {
  ui.overviewGrid.replaceChildren();
  state.study.strategies.slice(0, 6).forEach((strategy, index) => {
    const snapshot = strategy.currentSnapshot;
    const modeTone = toneForMode(snapshot.mode);
    const actionKind = kindForSnapshot(snapshot);
    const actionTone = toneForKind(actionKind);
    const gateTone = toneForGate(snapshot.newCapitalGate);
    const researchMode = ["SHADOW_ONLY", "RESEARCH_BLOCKED"].includes(snapshot.mode);
    const card = makeElement(
      "button",
      `overview-card card-tone-${actionTone}${researchMode ? " has-research-mode" : ""}`,
    );
    card.type = "button";
    card.dataset.strategyIndex = String(index);
    card.setAttribute("aria-pressed", String(index === state.strategyIndex));
    card.setAttribute(
      "aria-label",
      `${strategy.name}：${snapshot.action}；新资金门 ${snapshot.newCapitalGate}；打开研究`,
    );
    if (index === state.strategyIndex) card.classList.add("is-selected");

    const top = makeElement("span", "overview-card__top");
    const identity = makeElement("span", "overview-card__identity");
    identity.append(
      makeElement("span", "overview-card__family", strategy.family),
      makeElement("span", "overview-card__name", strategy.name),
      makeElement("time", "overview-card__date", `截至 ${snapshot.asOf}`),
    );
    identity.querySelector("time").dateTime = snapshot.asOf;
    top.append(identity, makeSemanticBadge(MODE_LABELS[snapshot.mode], modeTone, "semantic-badge--compact"));

    const directives = makeElement("span", "overview-card__directives");
    const actionLine = makeElement("span", "overview-card__directive");
    actionLine.append(
      makeElement("span", "overview-card__label", "当前动作"),
      makeSemanticBadge(kindLabel(actionKind), actionTone),
      makeElement("span", "overview-card__action-copy", snapshot.action),
    );
    const gateLine = makeElement("span", "overview-card__directive");
    gateLine.append(
      makeElement("span", "overview-card__label", "新资金门"),
      makeSemanticBadge(gateLabel(snapshot.newCapitalGate), gateTone),
    );
    directives.append(actionLine, gateLine);

    const basket = makeElement("span", "overview-card__basket");
    basket.append(
      makeElement("span", "overview-card__basket-title", `模型篮子 · ${BASKET_BASIS_LABELS[snapshot.basketBasis]}`),
    );
    const holdings = makeElement("span", "holding-chips");
    snapshot.modelBasket.forEach((item) => {
      const chip = makeElement("span", `holding-chip tone-${modeTone}`);
      chip.append(
        makeElement("strong", "", item.asset),
        makeElement("span", "", formatPercent(item.weight, 1)),
      );
      holdings.append(chip);
    });
    const cash = makeElement("span", "holding-chip holding-chip--cash tone-cyan");
    cash.append(makeElement("strong", "", "现金"), makeElement("span", "", formatPercent(snapshot.cashWeight, 1)));
    holdings.append(cash);
    basket.append(holdings);

    const triggers = makeElement("span", "overview-card__triggers");
    const next = makeElement("span", `trigger trigger--next tone-${gateTone}`);
    next.append(makeElement("strong", "", "下一触发"), makeElement("span", "", snapshot.nextTrigger));
    const riskTone = snapshot.mode === "BENCHMARK" ? "cyan" : "red";
    const riskLabel = snapshot.mode === "BENCHMARK" ? "参考边界" : "风险触发";
    const risk = makeElement("span", `trigger trigger--risk tone-${riskTone}`);
    risk.append(makeElement("strong", "", riskLabel), makeElement("span", "", snapshot.riskTrigger));
    triggers.append(next, risk);

    card.append(
      top,
      directives,
      basket,
      triggers,
      makeElement("span", "overview-card__note", snapshot.note),
      makeElement("span", "overview-card__open", "打开研究 →"),
    );
    card.addEventListener("click", () => {
      state.strategyIndex = index;
      ui.strategySelect.value = String(index);
      renderStrategy();
      requestAnimationFrame(() => {
        ui.researchSection.focus({ preventScroll: true });
        ui.researchSection.scrollIntoView({
          behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
          block: "start",
        });
      });
    });
    ui.overviewGrid.append(card);
  });
}

function renderMetrics(metrics) {
  const entries = [
    ["年化收益", formatPercent(metrics.cagr)],
    ["最大回撤", formatPercent(metrics.maxDrawdown)],
    ["Calmar", formatNumber(metrics.calmar)],
    ["年换手", `${formatNumber(metrics.annualTurnover)}×`],
    ["研究状态", metrics.status],
  ];
  ui.metricGrid.replaceChildren();
  entries.forEach(([label, value]) => {
    const wrapper = document.createElement("dl");
    wrapper.className = "metric";
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = value;
    wrapper.append(term, description);
    ui.metricGrid.append(wrapper);
  });
}

function renderComparison(strategies) {
  ui.comparisonBody.replaceChildren();
  strategies.forEach((strategy) => {
    const row = document.createElement("tr");
    const cells = [
      strategy.name,
      strategy.family,
      `${formatNumber(strategy.series.at(-1).nav, 2)}×`,
      formatPercent(strategy.metrics.cagr),
      formatPercent(strategy.metrics.maxDrawdown),
      strategy.metrics.status,
    ];
    cells.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    });
    ui.comparisonBody.append(row);
  });
}

function renderDecisionShortcuts(strategy) {
  const decisions = strategy.series
    .map((point, index) => ({ point, index }))
    .filter(({ point }) => point.decision !== null);
  const signature = ({ point }) => JSON.stringify([
    point.decision.action,
    point.decision.regime,
    point.decision.targetExposure,
    point.decision.modelBasket,
  ]);
  const compacted = [];
  let run = [];
  decisions.forEach((entry) => {
    if (run.length === 0 || signature(entry) === signature(run[0])) {
      run.push(entry);
      return;
    }
    compacted.push(run[0]);
    if (run.length > 1) compacted.push(run.at(-1));
    run = [entry];
  });
  if (run.length) {
    compacted.push(run[0]);
    if (run.length > 1) compacted.push(run.at(-1));
  }
  let visible = compacted.slice(-6);
  const selectedDecision = decisions.find(({ index }) => index === state.pointIndex);
  if (selectedDecision && !visible.some(({ index }) => index === selectedDecision.index)) {
    visible = [...compacted.slice(-5), selectedDecision].sort((a, b) => a.index - b.index);
  }
  visible.reverse();
  ui.decisionShortcuts.replaceChildren();
  if (visible.length === 0) {
    ui.decisionShortcuts.append(makeElement("p", "decision-shortcuts__empty", "当前序列没有可回放的决策。"));
    return;
  }
  visible.forEach(({ point, index }) => {
    const decision = point.decision;
    const tone = toneForKind(decision.kind, strategy.currentSnapshot.mode);
    const button = makeElement("button", `decision-shortcut tone-${tone}`);
    button.type = "button";
    button.setAttribute("aria-pressed", String(index === state.pointIndex));
    if (index === state.pointIndex) button.setAttribute("aria-current", "date");
    button.setAttribute("aria-label", `${point.date}，${decision.kind}，${decision.action}，跳到此决策`);
    if (index === state.pointIndex) button.classList.add("is-selected");
    const top = makeElement("span", "decision-shortcut__top");
    const time = makeElement("time", "decision-shortcut__date", point.date);
    time.dateTime = point.date;
    top.append(time, makeSemanticBadge(kindLabel(decision.kind), tone, "semantic-badge--compact"));
    button.append(
      top,
      makeElement("strong", "decision-shortcut__action", decision.action),
      makeElement("span", "decision-shortcut__basket", basketLabel(decision.modelBasket)),
    );
    button.addEventListener("click", () => {
      state.pointIndex = index;
      ui.timeSlider.value = String(index);
      renderDecision();
      requestAnimationFrame(() => ui.decisionCard.focus({ preventScroll: true }));
    });
    ui.decisionShortcuts.append(button);
  });
}

function renderDecision() {
  const strategy = currentStrategy();
  const point = strategy.series[state.pointIndex];
  const visibleKind = point.decision?.kind ?? "HOLD";
  const kindTone = toneForKind(visibleKind, strategy.currentSnapshot.mode);
  ui.selectedDate.textContent = formatDate(point.date);
  ui.timeSlider.setAttribute("aria-valuetext", formatDate(point.date));
  ui.navOutput.textContent = formatNumber(point.nav, 2);
  ui.drawdownOutput.textContent = formatPercent(point.drawdown);
  ui.exposureOutput.textContent = formatExposure(strategy, point.exposure);
  ui.decisionKind.className = `semantic-badge tone-${kindTone}`;
  ui.decisionKind.textContent = point.decision ? kindLabel(visibleKind) : "HOLD · 沿用";
  ui.decisionDetail.replaceChildren();
  appendTerm(ui.decisionDetail, "模型净值", formatNumber(point.nav, 2));
  appendTerm(ui.decisionDetail, "阶段回撤", formatPercent(point.drawdown));
  appendTerm(ui.decisionDetail, "模型暴露", formatExposure(strategy, point.exposure));
  if (point.decision) {
    appendTerm(ui.decisionDetail, "动作", point.decision.action);
    appendTerm(ui.decisionDetail, "状态", point.decision.regime);
    appendTerm(ui.decisionDetail, "目标暴露", formatExposure(strategy, point.decision.targetExposure));
    appendTerm(ui.decisionDetail, "模型篮子", basketLabel(point.decision.modelBasket), true);
    appendTerm(ui.decisionDetail, "理由", point.decision.reason, true);
  } else {
    appendTerm(ui.decisionDetail, "动作", "HOLD");
    appendTerm(ui.decisionDetail, "状态", "无新决策");
    appendTerm(ui.decisionDetail, "理由", "沿用上一有效模型目标；本截面没有新增决策。", true);
  }

  const logNav = strategy.metrics.status === "非决策级代理";
  const mode = strategy.currentSnapshot.mode;
  renderChart(document.querySelector("#nav-chart"), strategy.series, "nav", state.pointIndex, "percent", logNav, mode);
  renderChart(document.querySelector("#drawdown-chart"), strategy.series, "drawdown", state.pointIndex, "percent", false, mode);
  renderChart(document.querySelector("#exposure-chart"), strategy.series, "exposure", state.pointIndex, strategy.exposureUnit, false, mode);
  renderDecisionShortcuts(strategy);

  const priorDecisions = strategy.series
    .map((item, index) => (item.decision ? index : -1))
    .filter((index) => index >= 0 && index < state.pointIndex);
  const laterDecisions = strategy.series
    .map((item, index) => (item.decision ? index : -1))
    .filter((index) => index > state.pointIndex);
  ui.prevDecision.disabled = priorDecisions.length === 0;
  ui.nextDecision.disabled = laterDecisions.length === 0;
}

function renderCurrentModels(models, mode) {
  ui.currentModel.replaceChildren();
  models.forEach((model) => {
    const track = document.createElement("section");
    const heading = document.createElement("h4");
    const list = document.createElement("dl");
    track.className = "model-track";
    list.className = "detail-list detail-list--hero";
    heading.textContent = model.label;
    appendTerm(list, "截至", formatDate(model.asOf));
    appendTerm(list, "风险资产", model.riskyAsset);
    appendTerm(list, "目标暴露", formatPercent(model.targetExposure, 0));
    appendTerm(list, "现金", formatPercent(model.cashExposure, 0));
    const displayedGate = mode === "BENCHMARK" ? "NA" : model.gate;
    appendNodeTerm(
      list,
      "市场门",
      makeSemanticBadge(gateLabel(displayedGate), toneForGate(displayedGate), "semantic-badge--compact"),
    );
    const actionKind = mode === "BENCHMARK"
      ? "REFERENCE"
      : mode === "RESEARCH_BLOCKED" || (model.track === "new-capital" && model.gate === "BLOCK")
        ? "BLOCK"
        : inferActionKind(model.action);
    const actionDisplay = makeElement("span", "model-action");
    actionDisplay.append(
      makeSemanticBadge(kindLabel(actionKind), toneForKind(actionKind), "semantic-badge--compact"),
      makeElement("span", "", model.action),
    );
    appendNodeTerm(list, "动作", actionDisplay);
    appendTerm(list, "说明", model.note, true);
    track.append(heading, list);
    ui.currentModel.append(track);
  });
}

function renderQuality() {
  const entries = currentStrategy().qualityLedger[state.ledger];
  ui.qualityBody.replaceChildren();
  entries.forEach((entry) => {
    const row = document.createElement("tr");
    const rank = document.createElement("td");
    const asset = document.createElement("td");
    const returnCell = document.createElement("td");
    const status = document.createElement("td");
    const evidence = document.createElement("td");
    const momentumReturn = document.createElement("span");
    const badge = document.createElement("span");
    rank.textContent = String(entry.rank).padStart(2, "0");
    asset.textContent = entry.asset;
    momentumReturn.className = "quality-return";
    momentumReturn.textContent = formatPercent(entry.momentumReturn);
    returnCell.append(momentumReturn);
    const statusTone = entry.status === "PASS" ? "green" : entry.status === "WATCH" ? "amber" : "red";
    badge.className = `quality-state semantic-badge tone-${statusTone} semantic-badge--compact`;
    badge.textContent = entry.status;
    status.append(badge);
    evidence.textContent = entry.evidence;
    row.append(rank, asset, returnCell, status, evidence);
    ui.qualityBody.append(row);
  });
}

function renderStrategy() {
  const strategy = currentStrategy();
  const latestDecisionIndex = strategy.series.findLastIndex((point) => point.decision !== null);
  state.pointIndex = latestDecisionIndex >= 0 ? latestDecisionIndex : strategy.series.length - 1;
  ui.strategyFamily.textContent = strategy.family;
  ui.strategyName.textContent = strategy.name;
  ui.strategySummary.textContent = strategy.summary;
  ui.navTitle.textContent = strategy.metrics.status === "非决策级代理" ? "模型净值（对数）" : "模型净值";
  ui.exposureTitle.textContent = strategy.exposureUnit === "multiplier" ? "袖套倍率" : "模型暴露";
  ui.timeSlider.max = String(strategy.series.length - 1);
  ui.timeSlider.value = String(state.pointIndex);
  renderOverview();
  renderMetrics(strategy.metrics);
  renderCurrentModels(strategy.currentModels, strategy.currentSnapshot.mode);
  renderDecision();
  renderQuality();
}

function renderStudy(study) {
  state.study = study;
  document.title = study.siteTitle;
  ui.dataStatus.textContent = study.dataStatus === "placeholder" ? "结构占位数据" : "公开研究数据";
  ui.generatedDate.textContent = `数据日期 ${study.generatedDate}`;
  ui.siteNotice.textContent = study.notice;
  const top3Proxy = study.strategies.find((strategy) => strategy.id === "top3-11m-proxy");
  ui.qualityAsOf.textContent = top3Proxy
    ? `已完成月末信号：${top3Proxy.currentModels[0].asOf} · 11M 锚点取每只证券在日历回看日前不超过3个交易日的最后有效收盘。`
    : "未找到11M候选信号截面。";

  ui.strategySelect.replaceChildren();
  study.strategies.forEach((strategy, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = strategy.name;
    ui.strategySelect.append(option);
  });
  ui.strategySelect.disabled = false;
  ui.timeSlider.disabled = false;

  ui.methodList.replaceChildren();
  study.methodology.forEach((method) => {
    const article = document.createElement("article");
    const body = document.createElement("div");
    const title = document.createElement("h3");
    const copy = document.createElement("p");
    article.className = "method-item";
    title.textContent = method.title;
    copy.textContent = method.body;
    body.append(title, copy);
    article.append(body);
    ui.methodList.append(article);
  });

  ui.limitationList.replaceChildren();
  study.limitations.forEach((limitation) => {
    const item = document.createElement("li");
    item.textContent = limitation;
    ui.limitationList.append(item);
  });
  renderComparison(study.strategies);
  renderStrategy();
}

function bindEvents() {
  ui.strategySelect.addEventListener("change", () => {
    state.strategyIndex = Number(ui.strategySelect.value);
    renderStrategy();
  });
  ui.timeSlider.addEventListener("input", () => {
    state.pointIndex = Number(ui.timeSlider.value);
    renderDecision();
  });
  ui.prevDecision.addEventListener("click", () => {
    const series = currentStrategy().series;
    for (let index = state.pointIndex - 1; index >= 0; index -= 1) {
      if (series[index].decision) {
        state.pointIndex = index;
        ui.timeSlider.value = String(index);
        renderDecision();
        break;
      }
    }
  });
  ui.nextDecision.addEventListener("click", () => {
    const series = currentStrategy().series;
    for (let index = state.pointIndex + 1; index < series.length; index += 1) {
      if (series[index].decision) {
        state.pointIndex = index;
        ui.timeSlider.value = String(index);
        renderDecision();
        break;
      }
    }
  });
  ui.ledgerButtons.forEach((button) => {
    button.addEventListener("click", () => {
      state.ledger = button.dataset.ledger;
      ui.ledgerButtons.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      renderQuality();
    });
  });
}

async function loadStudy() {
  assert(["http:", "https:"].includes(location.protocol), "请通过本地 HTTP 服务或 GitHub Pages 打开站点");
  assert(DATA_URL.origin === location.origin, "公开数据必须与页面同源");
  assert(DATA_URL.pathname.endsWith("/data/study.json"), "公开数据路径不受允许");
  const response = await fetch(DATA_URL, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  assert(response.ok, "无法读取公开研究数据");
  const study = validateStudy(await response.json());
  renderStudy(study);
}

bindEvents();
loadStudy().catch((error) => {
  console.error("Public study validation failed:", error);
  ui.dataStatus.textContent = "验证失败";
  ui.fatalError.hidden = false;
});
