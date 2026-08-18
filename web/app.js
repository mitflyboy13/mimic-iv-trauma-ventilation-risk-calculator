const state = {
  csrfToken: "",
};

const loginView = document.querySelector("#login-view");
const calculatorView = document.querySelector("#calculator-view");
const loginForm = document.querySelector("#login-form");
const calculatorForm = document.querySelector("#calculator-form");
const loginError = document.querySelector("#login-error");
const calculatorError = document.querySelector("#calculator-error");
const currentUser = document.querySelector("#current-user");
const logoutButton = document.querySelector("#logout-button");
const riskPercent = document.querySelector("#risk-percent");
const riskLabel = document.querySelector("#risk-label");
const modelSource = document.querySelector("#model-source");
const meterRing = document.querySelector("#meter-ring");
const riskDetails = document.querySelector("#risk-details");
const modelTrainingStatus = document.querySelector("#model-training-status");
const modelType = document.querySelector("#model-type");
const modelTarget = document.querySelector("#model-target");
const modelAuroc = document.querySelector("#model-auroc");
const modelAuprc = document.querySelector("#model-auprc");
const modelF1 = document.querySelector("#model-f1");
const rocCurve = document.querySelector("#roc-curve");
const prCurve = document.querySelector("#pr-curve");
const shapSummary = document.querySelector("#shap-summary");

function wait(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

async function api(path, options = {}) {
  const headers = {
    "Accept": "application/json",
    ...(options.body ? {"Content-Type": "application/json"} : {}),
    ...(state.csrfToken ? {"X-CSRF-Token": state.csrfToken} : {}),
    ...(options.headers || {}),
  };

  for (let attempt = 0; attempt < 3; attempt += 1) {
    let response;
    let payload = {};
    let rawText = "";

    try {
      response = await fetch(path, {
        credentials: "same-origin",
        ...options,
        headers,
      });
    } catch (error) {
      if (attempt < 2) {
        await wait(1500 * (attempt + 1));
        continue;
      }
      throw new Error("Network request failed. Please refresh and try again.");
    }

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      payload = await response.json().catch(() => ({}));
    } else {
      rawText = await response.text().catch(() => "");
    }

    if (response.ok) {
      return payload;
    }

    if (attempt < 2 && [404, 502, 503, 504].includes(response.status) && !payload.model_required) {
      await wait(1500 * (attempt + 1));
      continue;
    }

    const fallback = rawText.trim().replace(/\s+/g, " ").slice(0, 140);
    throw new Error(payload.error || fallback || `Request failed (${response.status})`);
  }

  throw new Error("Request failed. Please refresh and try again.");
}

async function refreshCsrf() {
  const payload = await api("/api/csrf");
  state.csrfToken = payload.csrfToken;
}

function showCalculator(user) {
  loginView.classList.add("hidden");
  calculatorView.classList.remove("hidden");
  currentUser.textContent = user.username;
  loadModelCard();
}

function showLogin() {
  calculatorView.classList.add("hidden");
  loginView.classList.remove("hidden");
  currentUser.textContent = "";
}

function readForm() {
  const formData = new FormData(calculatorForm);
  const numeric = [
    "age",
    "icu_los_days",
    "invasive_vent_duration_hours",
    "head_neck_ais",
    "spine_ais",
    "chest_ais",
    "abdomen_pelvis_ais",
    "extremity_ais",
    "external_burn_ais",
    "gcs_min_24h",
    "sofa_before_liberation",
    "oasis",
    "charlson_comorbidity_index",
    "pao2fio2_last_6h",
    "fio2_last_6h",
    "peep_last_6h",
    "rsbi_proxy_last_6h",
  ];
  const checkbox = [
    "vasopressor_any_24h",
    "suspected_infection_flag",
    "sedative_proxy_any_24h",
    "sbt_mode_proxy",
    "low_support_proxy",
  ];
  const payload = {
    gender: formData.get("gender"),
    race: "UNKNOWN",
    admission_type: "EW EMER.",
  };

  for (const name of numeric) {
    payload[name] = Number(formData.get(name));
  }
  for (const name of checkbox) {
    payload[name] = formData.has(name) ? 1 : 0;
  }

  const aisValues = [
    payload.head_neck_ais,
    payload.spine_ais,
    payload.chest_ais,
    payload.abdomen_pelvis_ais,
    payload.extremity_ais,
    payload.external_burn_ais,
  ];
  const sortedAis = [...aisValues].sort((a, b) => b - a);
  payload.max_ais = sortedAis[0] || 0;
  payload.iss_proxy = sortedAis.slice(0, 3).reduce((total, value) => total + value * value, 0);
  payload.injury_body_region_count = aisValues.filter((value) => value > 0).length;
  payload.severe_ais_region_count = aisValues.filter((value) => value >= 3).length;
  payload.polytrauma_proxy = payload.injury_body_region_count >= 2 ? 1 : 0;
  payload.tbi_flag = payload.head_neck_ais > 0 ? 1 : 0;
  payload.spine_flag = payload.spine_ais > 0 ? 1 : 0;
  payload.thoracic_trauma_flag = payload.chest_ais > 0 ? 1 : 0;
  payload.abdominal_pelvic_trauma_flag = payload.abdomen_pelvis_ais > 0 ? 1 : 0;
  payload.extremity_trauma_flag = payload.extremity_ais > 0 ? 1 : 0;
  payload.burn_flag = payload.external_burn_ais > 0 ? 1 : 0;

  return payload;
}

function riskClass(probability) {
  if (probability >= 0.75) return "Higher liberation success likelihood";
  if (probability >= 0.45) return "Intermediate liberation success likelihood";
  return "Lower liberation success likelihood";
}

function renderResult(result) {
  const probability = result.liberation_success_probability_48h;
  const percent = Math.round(probability * 100);
  riskPercent.textContent = `${percent}%`;
  riskLabel.textContent = riskClass(probability);
  modelSource.textContent = result.model_source;
  meterRing.style.background = `conic-gradient(${result.color} ${percent * 3.6}deg, #e1e9e6 0deg)`;
  riskDetails.innerHTML = `
    <div><dt>Risk class</dt><dd>${riskClass(probability)}</dd></div>
    <div><dt>Predicted reinstitution risk</dt><dd>${Math.round((1 - probability) * 100)}%</dd></div>
    <div><dt>Reintubation/reinstitution window</dt><dd>48 hours</dd></div>
    <div><dt>Decision time</dt><dd>Before liberation</dd></div>
  `;
}

function formatMetric(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "Pending";
  return Number(value).toFixed(3);
}

function metricSource(modelCard) {
  return modelCard?.metrics?.test || modelCard?.metrics || {};
}

function svgCurve(points, xLabel, yLabel) {
  if (!points || points.length < 2) {
    return `<div class="empty-curve">Pending trained-model metrics</div>`;
  }
  const width = 280;
  const height = 180;
  const pad = 34;
  const xScale = (x) => pad + Math.max(0, Math.min(1, x)) * (width - 2 * pad);
  const yScale = (y) => height - pad - Math.max(0, Math.min(1, y)) * (height - 2 * pad);
  const path = points.map(([x, y], index) => `${index === 0 ? "M" : "L"} ${xScale(x).toFixed(1)} ${yScale(y).toFixed(1)}`).join(" ");
  return `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${yLabel} by ${xLabel}">
      <path class="axis" d="M ${pad} ${pad} L ${pad} ${height - pad} L ${width - pad} ${height - pad}"></path>
      <path class="baseline" d="M ${pad} ${height - pad} L ${width - pad} ${pad}"></path>
      <path class="curve-line" d="${path}"></path>
      <text x="${width / 2}" y="${height - 6}" text-anchor="middle">${xLabel}</text>
      <text x="13" y="${height / 2}" text-anchor="middle" transform="rotate(-90 13 ${height / 2})">${yLabel}</text>
    </svg>
  `;
}

function renderRocCurve(metrics) {
  const curve = metrics?.roc_curve;
  const fpr = curve?.false_positive_rate || [];
  const tpr = curve?.true_positive_rate || [];
  const points = fpr.map((x, index) => [Number(x), Number(tpr[index])]);
  rocCurve.innerHTML = svgCurve(points, "False positive rate", "True positive rate");
}

function renderPrCurve(metrics) {
  const curve = metrics?.precision_recall_curve;
  const recall = curve?.recall || [];
  const precision = curve?.precision || [];
  const points = recall.map((x, index) => [Number(x), Number(precision[index])]);
  prCurve.innerHTML = svgCurve(points, "Recall", "Precision");
}

function renderShap(modelCard) {
  const shap = modelCard.shap || {};
  if (Array.isArray(shap.features) && shap.features.length) {
    const maxValue = Math.max(...shap.features.map((item) => Math.abs(Number(item.mean_abs_shap || 0))), 0.001);
    shapSummary.innerHTML = shap.features.slice(0, 8).map((item) => {
      const value = Math.abs(Number(item.mean_abs_shap || 0));
      const width = Math.max(4, (value / maxValue) * 100);
      return `
        <div class="shap-row">
          <span>${item.feature}</span>
          <b data-width="${width.toFixed(1)}"></b>
          <em>${value.toFixed(3)}</em>
        </div>
      `;
    }).join("");
    shapSummary.querySelectorAll("[data-width]").forEach((bar) => {
      bar.style.width = `${bar.dataset.width}%`;
    });
    return;
  }
  const artifacts = modelCard.training_artifacts || [];
  if (artifacts.length) {
    shapSummary.innerHTML = `
      <p>${shap.message}</p>
      <ul>${artifacts.map((item) => `<li>${item}</li>`).join("")}</ul>
    `;
    return;
  }
  const drivers = modelCard.heuristic_drivers || [];
  if (drivers.length) {
    shapSummary.innerHTML = `
      <p>${shap.message}</p>
      <ul>${drivers.map((item) => `<li><strong>${item.feature}</strong>: ${item.direction}</li>`).join("")}</ul>
    `;
    return;
  }
  shapSummary.innerHTML = `<p>${shap.message || "SHAP summary is pending trained-model deployment."}</p>`;
}

function renderModelCard(modelCard) {
  const metrics = metricSource(modelCard);
  if (modelCard.deployed_trained_model) {
    modelTrainingStatus.textContent = "Trained model deployed";
  } else if (modelCard.model_required) {
    modelTrainingStatus.textContent = "Trained model required";
  } else {
    modelTrainingStatus.textContent = "Heuristic fallback";
  }
  modelTrainingStatus.classList.toggle("warning", !modelCard.deployed_trained_model);
  modelTrainingStatus.classList.toggle("danger", Boolean(modelCard.model_required));
  modelType.textContent = modelCard.model_type || "--";
  modelTarget.textContent = modelCard.prediction_target || "--";
  modelAuroc.textContent = formatMetric(metrics.roc_auc);
  modelAuprc.textContent = formatMetric(metrics.average_precision);
  modelF1.textContent = formatMetric(metrics.f1_score);
  renderRocCurve(metrics);
  renderPrCurve(metrics);
  renderShap(modelCard);
}

async function loadModelCard() {
  try {
    const modelCard = await api("/api/model-card");
    renderModelCard(modelCard);
  } catch (error) {
    modelTrainingStatus.textContent = "Unavailable";
    modelType.textContent = "Unable to load model evidence";
    shapSummary.innerHTML = `<p>${error.message}</p>`;
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.textContent = "";
  const formData = new FormData(loginForm);
  try {
    await refreshCsrf();
    const payload = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: formData.get("username"),
        password: formData.get("password"),
      }),
    });
    await refreshCsrf();
    showCalculator(payload.user);
    loginForm.reset();
  } catch (error) {
    loginError.textContent = error.message;
  }
});

logoutButton.addEventListener("click", async () => {
  try {
    await api("/api/logout", {method: "POST", body: JSON.stringify({})});
  } finally {
    await refreshCsrf();
    showLogin();
  }
});

calculatorForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  calculatorError.textContent = "";
  try {
    const result = await api("/api/calculate", {
      method: "POST",
      body: JSON.stringify(readForm()),
    });
    renderResult(result);
  } catch (error) {
    calculatorError.textContent = error.message;
  }
});

async function boot() {
  try {
    await refreshCsrf();
    const payload = await api("/api/me");
    showCalculator(payload.user);
  } catch {
    showLogin();
  }
}

boot();
