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

async function api(path, options = {}) {
  const headers = {
    "Accept": "application/json",
    ...(options.body ? {"Content-Type": "application/json"} : {}),
    ...(state.csrfToken ? {"X-CSRF-Token": state.csrfToken} : {}),
    ...(options.headers || {}),
  };
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

async function refreshCsrf() {
  const payload = await api("/api/csrf");
  state.csrfToken = payload.csrfToken;
}

function showCalculator(user) {
  loginView.classList.add("hidden");
  calculatorView.classList.remove("hidden");
  currentUser.textContent = user.username;
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
    "tbi_flag",
    "spine_flag",
    "thoracic_trauma_flag",
    "abdominal_pelvic_trauma_flag",
    "extremity_trauma_flag",
    "burn_flag",
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

  payload.injury_body_region_count = [
    "tbi_flag",
    "spine_flag",
    "thoracic_trauma_flag",
    "abdominal_pelvic_trauma_flag",
    "extremity_trauma_flag",
    "burn_flag",
  ].reduce((total, name) => total + payload[name], 0);
  payload.polytrauma_proxy = payload.injury_body_region_count >= 2 ? 1 : 0;

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
  await refreshCsrf();
  try {
    const payload = await api("/api/me");
    showCalculator(payload.user);
  } catch {
    showLogin();
  }
}

boot();
