from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["display"])

# The same heart-badge mark as frontend/public/favicon.svg, inlined as a data
# URI since this page is served standalone from the backend and doesn't share
# the frontend's static assets. Substituted in below rather than embedded
# directly in _PAGE so its one long base64 line doesn't blow past ruff's
# line-length limit.
_FAVICON_HREF = (
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+"
    "CiAgPHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0iIzI1NGRkMSIvPgogIDxwYXRoCiAgICBmaWxsPSIjZmZmZmZm"
    "IgogICAgZD0iTTEyLDE4LjZsLTEuMDItMC45M0M3LjE0LDE0LjI0LDQuNSwxMS44Niw0LjUsOC45NAogICAgICAgQzQuNSw2LjU2LDYuMzYs"
    "NC43LDguNzQsNC43YzEuMzIsMCwyLjU5LDAuNjIsMy4yNiwxLjU1CiAgICAgICBjMC42Ny0wLjkzLDEuOTQtMS41NSwzLjI2LTEuNTVjMi4z"
    "OCwwLDQuMjQsMS44Niw0LjI0LDQuMjQKICAgICAgIGMwLDIuOTItMi42NCw1LjMtNi40OCw4Ljc0TDEyLDE4LjZ6IgogIC8+Cjwvc3ZnPgo="
)

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="__FAVICON_HREF__">
<title>Let's Give</title>
<style>
  :root { color-scheme: dark; }
  html, body {
    margin: 0; height: 100%; background: #0b1220; color: #f5f7fa;
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    overflow: hidden;
  }
  [hidden] { display: none !important; }
  #generic-stage {
    height: 100%; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center;
    gap: 1.25rem; padding: 2rem; box-sizing: border-box;
  }
  #org {
    font-size: clamp(1.2rem, 3vw, 2.2rem); font-weight: normal; opacity: 0.85;
    letter-spacing: 0.04em; margin: 0;
  }
  #count {
    font-size: clamp(6rem, 22vw, 16rem); font-weight: 800; line-height: 1;
    font-variant-numeric: tabular-nums; transition: transform 200ms ease;
  }
  #count.pulse { transform: scale(1.06); }
  #label { font-size: clamp(1rem, 2.4vw, 1.6rem); opacity: 0.8; }
  #amount { font-size: clamp(1.4rem, 3.4vw, 2.6rem); font-weight: 600; }
  #timer { font-size: clamp(1.2rem, 2.8vw, 2rem); font-variant-numeric: tabular-nums; opacity: 0.9; }
  #status {
    position: fixed; top: 1rem; right: 1.25rem; font-size: 0.85rem;
    opacity: 0.65; letter-spacing: 0.03em; z-index: 1000;
  }
  .dot { display: inline-block; width: 0.55em; height: 0.55em; border-radius: 50%; margin-right: 0.4em; }
  .dot.live { background: #34d399; }
  .dot.down { background: #f87171; }
  .sr-only {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
  }
  #template-stage {
    position: fixed; top: 50%; left: 50%; transform-origin: center center;
    overflow: hidden;
  }
  .el {
    position: absolute; box-sizing: border-box; display: flex; align-items: center;
    overflow: hidden; white-space: pre-wrap;
  }
  .el-progress-track { padding: 0 !important; }
  .el-progress-fill { height: 100%; transition: width 400ms ease; }
  #celebration {
    position: fixed; inset: 0; z-index: 2000; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 0.5rem; text-align: center;
    padding: 2rem; box-sizing: border-box; pointer-events: none;
    background: radial-gradient(ellipse at center, rgba(52,211,153,0.22), transparent 70%);
  }
  #celebration .emoji {
    font-size: clamp(3rem, 10vw, 7rem); line-height: 1;
    animation: celebrate-pop 900ms ease-out;
  }
  #celebration .message {
    font-size: clamp(1.4rem, 4vw, 3rem); font-weight: 800; color: #34d399;
    text-shadow: 0 2px 12px rgba(0,0,0,0.5);
  }
  @keyframes celebrate-pop {
    0% { transform: scale(0.4); opacity: 0; }
    60% { transform: scale(1.15); opacity: 1; }
    100% { transform: scale(1); opacity: 1; }
  }
  @media (prefers-reduced-motion: reduce) {
    #count { transition: none; }
    .el-progress-fill { transition: none; }
    #celebration .emoji { animation: none; }
  }
</style>
</head>
<body>
  <div id="status" role="status" aria-live="polite">
    <span class="dot down" id="statusDot" aria-hidden="true"></span>
    <span id="statusText">connecting</span>
  </div>

  <main id="generic-stage" hidden>
    <h1 id="org">&nbsp;</h1>
    <div id="live-region" aria-live="polite" aria-atomic="true">
      <div id="count" aria-label="Contribution count">0</div>
      <div id="label">contributions</div>
      <div id="amount" hidden></div>
    </div>
    <div id="timer" role="timer" aria-label="Time remaining in this session"></div>
    <p class="sr-only">
      Live giving count for this session. This page updates automatically; no interaction is needed.
    </p>
  </main>

  <div id="template-stage" hidden aria-hidden="true"></div>

  <div id="celebration" hidden aria-live="polite">
    <div class="emoji" aria-hidden="true">🎉🎊🎉</div>
    <div class="message">Target reached! We made it!</div>
  </div>
<script>
(function () {
  const params = new URLSearchParams(location.search);
  const token = params.get("token") || "";
  const sessionId = location.pathname.split("/").filter(Boolean).pop();
  const authHeaders = { Authorization: "Bearer " + token };

  const genericStage = document.getElementById("generic-stage");
  const orgEl = document.getElementById("org");
  const countEl = document.getElementById("count");
  const amountEl = document.getElementById("amount");
  const timerEl = document.getElementById("timer");
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const templateStage = document.getElementById("template-stage");
  const celebrationEl = document.getElementById("celebration");

  let lastCount = null;
  let endsAt = null;
  let template = null; // set once, before the socket connects

  function formatMoney(amount, currency) {
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: currency }).format(amount);
    } catch (e) {
      return currency + " " + amount;
    }
  }

  function timerText() {
    if (!endsAt) return "";
    const remainingMs = endsAt.getTime() - Date.now();
    if (remainingMs <= 0) return "0:00";
    const totalSeconds = Math.floor(remainingMs / 1000);
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return m + ":" + String(s).padStart(2, "0");
  }

  // --- generic (no template bound) rendering -------------------------------

  function renderGeneric(state) {
    orgEl.textContent = state.organization_name || "";
    countEl.textContent = state.contribution_count;
    if (lastCount !== null && state.contribution_count > lastCount) {
      countEl.classList.remove("pulse");
      void countEl.offsetWidth; // restart animation
      countEl.classList.add("pulse");
    }
    if (state.amount_visible && state.total_amount != null) {
      amountEl.hidden = false;
      amountEl.textContent = formatMoney(state.total_amount, state.currency);
    } else {
      amountEl.hidden = true;
    }
  }

  function tickGenericTimer() {
    timerEl.textContent = timerText();
  }

  // --- template rendering ---------------------------------------------------

  function fitTemplateStage() {
    if (!template) return;
    const scaleX = window.innerWidth / template.canvas.width;
    const scaleY = window.innerHeight / template.canvas.height;
    const scale = Math.min(scaleX, scaleY);
    templateStage.style.width = template.canvas.width + "px";
    templateStage.style.height = template.canvas.height + "px";
    templateStage.style.transform = "translate(-50%, -50%) scale(" + scale + ")";
  }
  window.addEventListener("resize", fitTemplateStage);

  function elementContent(el, state) {
    const b = el.binding || {};
    switch (el.type) {
      case "heading":
      case "body_text":
      case "sponsor_message":
      case "payment_instructions":
        return b.text || "";
      case "contribution_count":
        return String(state.contribution_count);
      case "amount":
        return state.amount_visible && state.total_amount != null
          ? formatMoney(state.total_amount, state.currency)
          : "";
      case "goal":
        if (state.goal_amount == null) return "";
        return "Goal: " + formatMoney(state.goal_amount, state.currency);
      case "qr_code":
        return b.value || "";
      case "countdown":
        return timerText();
      default:
        return "";
    }
  }

  function progressFraction(state) {
    if (state.goal_amount == null || !state.amount_visible || state.total_amount == null) return 0;
    if (Number(state.goal_amount) <= 0) return 0;
    return Math.max(0, Math.min(1, Number(state.total_amount) / Number(state.goal_amount)));
  }

  function goalReached(state) {
    // Computed server-side from the real total regardless of
    // amount_visible -- a real deposit hit a $2 goal and the celebration
    // never fired because this used to also require the exact running
    // total to be public. A binary "did we hit it" milestone reveals far
    // less than the running total/progress bar does, so it's sent even
    // when the org has chosen to keep the exact amount private.
    return !!state.goal_reached;
  }

  function buildTemplateStage() {
    templateStage.innerHTML = "";
    templateStage.style.backgroundColor = template.canvas.background_color || "#0b1220";
    const sorted = template.elements.slice().sort((a, b) => a.z_index - b.z_index);
    for (const el of sorted) {
      if (el.is_hidden) continue;
      const div = document.createElement("div");
      div.className = "el";
      div.dataset.id = el.id;
      div.dataset.type = el.type;
      div.style.left = el.x + "px";
      div.style.top = el.y + "px";
      div.style.width = el.width + "px";
      div.style.height = el.height + "px";
      div.style.zIndex = String(el.z_index);
      const st = el.style || {};
      div.style.color = st.color || "#f5f7fa";
      div.style.backgroundColor = st.background_color || "transparent";
      div.style.fontSize = (typeof st.font_size === "number" ? st.font_size : 32) + "px";
      const justify =
        st.text_align === "left" ? "flex-start" : st.text_align === "right" ? "flex-end" : "center";
      div.style.justifyContent = justify;
      div.style.textAlign = st.text_align || "center";
      if (typeof st.opacity === "number") div.style.opacity = String(st.opacity);

      if (el.type === "logo" && el.binding && el.binding.image_url) {
        const img = document.createElement("img");
        img.src = el.binding.image_url;
        img.alt = "";
        img.style.maxWidth = "100%";
        img.style.maxHeight = "100%";
        img.style.objectFit = "contain";
        div.appendChild(img);
      } else if (el.type === "qr_code" && el.binding && el.binding.qr_data_uri) {
        div.style.backgroundColor = st.background_color || "#ffffff";
        const img = document.createElement("img");
        img.src = el.binding.qr_data_uri;
        img.alt = "";
        img.style.maxWidth = "100%";
        img.style.maxHeight = "100%";
        img.style.objectFit = "contain";
        div.appendChild(img);
      } else if (el.type === "progress_bar") {
        div.classList.add("el-progress-track");
        div.style.backgroundColor = st.background_color || "rgba(255,255,255,0.15)";
        const fill = document.createElement("div");
        fill.className = "el-progress-fill";
        fill.style.backgroundColor = st.color || "#34d399";
        fill.style.width = "0%";
        div.appendChild(fill);
      } else {
        div.textContent = elementContent(el, { contribution_count: 0, amount_visible: false });
      }
      templateStage.appendChild(div);
    }
  }

  function renderTemplateDynamicContent(state) {
    const els = templateStage.querySelectorAll("[data-type]");
    els.forEach(function (div) {
      const type = div.dataset.type;
      if (type === "progress_bar") {
        const fill = div.querySelector(".el-progress-fill");
        if (fill) fill.style.width = progressFraction(state) * 100 + "%";
        return;
      }
      if ((type === "logo" || type === "qr_code") && div.querySelector("img")) return; // static image
      const templateEl = template.elements.find(function (e) { return e.id === div.dataset.id; });
      if (!templateEl) return;
      div.textContent = elementContent(templateEl, state);
    });
  }

  // --- shared: state + connection -------------------------------------------

  function render(state) {
    document.title = "Let's Give — " + (state.organization_name || "");
    endsAt = state.ends_at ? new Date(state.ends_at) : null;
    if (template) {
      renderTemplateDynamicContent(state);
    } else {
      renderGeneric(state);
    }
    // Persists for as long as the state stays at/above goal (not a one-shot
    // fire-once effect) -- robust to a WebSocket reconnect landing after the
    // threshold was already crossed, e.g. a projector/OBS source reloading
    // mid-session.
    celebrationEl.hidden = !goalReached(state);
    lastCount = state.contribution_count;
  }

  setInterval(function () {
    if (template) {
      templateStage.querySelectorAll('[data-type="countdown"]').forEach(function (el) {
        el.textContent = timerText();
      });
    } else {
      tickGenericTimer();
    }
  }, 250);

  let retryDelayMs = 1000;
  let activeSocket = null;
  function connect() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    const url = proto + location.host + "/v1/sessions/" + sessionId + "/live?token="
      + encodeURIComponent(token);
    const ws = new WebSocket(url);
    activeSocket = ws;

    ws.onopen = function () {
      retryDelayMs = 1000;
      statusDot.className = "dot live";
      statusText.textContent = "live";
    };
    ws.onmessage = function (evt) {
      try { render(JSON.parse(evt.data)); } catch (e) { /* ignore malformed frame */ }
    };
    ws.onclose = function () {
      if (activeSocket === ws) activeSocket = null;
      statusDot.className = "dot down";
      statusText.textContent = "reconnecting";
      setTimeout(connect, retryDelayMs);
      retryDelayMs = Math.min(retryDelayMs * 1.7, 15000);
    };
    ws.onerror = function () { ws.close(); };
  }

  // This channel carries no periodic "still alive" pulse of its own -- a
  // browser tab only ever hears about a NEW contribution, never a
  // heartbeat -- so a single silently-dropped frame (a brief network blip
  // that never actually closed the socket, unlike a real disconnect, which
  // ws.onclose above already recovers from) would otherwise leave a
  // projector/OBS source stuck on a stale count indefinitely, with nothing
  // to ever notice or correct it. Force a clean reconnect periodically so a
  // fresh, correct snapshot (the same initial_payload the socket already
  // sends right after subscribing) is re-synced even if nothing else would
  // have surfaced the gap -- the operator console's own polling safety net
  // exists for exactly this reason, this channel just never had one.
  setInterval(function () {
    if (activeSocket) activeSocket.close();
  }, 45000);

  async function init() {
    try {
      const templateUrl = "/v1/sessions/" + sessionId + "/display-template/public";
      const resp = await fetch(templateUrl, { headers: authHeaders });
      if (resp.ok) {
        const data = await resp.json();
        if (data && Array.isArray(data.elements) && data.elements.length > 0) {
          template = data;
        }
      }
    } catch (e) {
      // Network hiccup fetching the template -- fall back to the generic layout.
    }

    if (template) {
      templateStage.hidden = false;
      buildTemplateStage();
      fitTemplateStage();
    } else {
      genericStage.hidden = false;
    }
    connect();
  }

  init();
})();
</script>
</body>
</html>
""".replace("__FAVICON_HREF__", _FAVICON_HREF)


@router.get("/display/{session_id}", response_class=HTMLResponse, include_in_schema=False)
async def display_page(session_id: str) -> str:
    """Fullscreen projection output (spec 6.2 'Fullscreen output') --
    a dedicated URL suitable for a projector window, secondary display, or
    OBS Browser Source. `session_id` is only used client-side to build the
    WebSocket URL; the page itself carries no session data, so the display
    token in the query string (`?token=...`) is what actually authorizes it.

    On load, the page fetches the session's bound Display Studio template
    (GET .../display-template/public) and renders it if one exists with at
    least one element; otherwise it falls back to the fixed generic
    count/amount/timer layout below. Either way, live updates (count,
    amount, countdown, connection status) come from the same WebSocket
    channel -- the template only changes *how* that data is laid out, not
    where it comes from.
    """
    return _PAGE
