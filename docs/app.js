const VAPID_PUBLIC_KEY = "BEdVpuEowSmtY-4vciGaidhIUR44Lad1k2lzM-uwTacvM54ZTszzxLbswpyaJCRoKGC_fZIbySzTvS2tXM1h4y0";
const GITHUB_REPO = "johnwogrady-max/Daily-reminder";
const WORKFLOW_FILE = "daily-alert.yml";

function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function registerSW() {
  if (!("serviceWorker" in navigator)) return null;
  return navigator.serviceWorker.register("./service-worker.js");
}

async function loadBriefing() {
  const el = document.getElementById("briefing");
  const meta = document.getElementById("generated");

  // The real briefing is delivered via encrypted web push and stashed in
  // the local cache by the service worker. The public site hosts only a
  // placeholder.
  try {
    const cached = await caches.match("./cached-briefing.json");
    if (cached) {
      const data = await cached.json();
      el.textContent = (data.body || "").trim() || "No briefing yet.";
      if (data.generated_at) {
        meta.textContent = "Updated " + new Date(data.generated_at).toLocaleString();
      }
      return;
    }
  } catch (_) {
    // fall through to placeholder
  }

  try {
    const res = await fetch("./briefing.json?_=" + Date.now(), { cache: "no-cache" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    el.textContent = (data.body || "").trim() || "No briefing yet.";
    meta.textContent = "Waiting for the next 7am push.";
  } catch (e) {
    el.textContent = "Couldn't load briefing. " + e.message;
  }
}

async function enablePush() {
  if (VAPID_PUBLIC_KEY === "PASTE_VAPID_PUBLIC_KEY_HERE") {
    alert("VAPID public key not configured yet. Edit docs/app.js.");
    return;
  }
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    alert("Push notifications aren't supported on this device.");
    return;
  }
  const reg = await registerSW();
  if (!reg) return;
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    alert("Notification permission denied. You can enable it in Settings → Notifications.");
    return;
  }
  let sub;
  try {
    sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY),
      });
    }
  } catch (e) {
    alert("Subscription failed: " + e.message);
    return;
  }
  const json = JSON.stringify(sub.toJSON(), null, 2);
  const ta = document.getElementById("subscription");
  ta.value = json;
  document.getElementById("sub-wrap").hidden = false;
}

function copySub() {
  const ta = document.getElementById("subscription");
  ta.select();
  ta.setSelectionRange(0, 99999);
  if (navigator.clipboard) {
    navigator.clipboard.writeText(ta.value).then(
      () => flash("Copied"),
      () => document.execCommand && document.execCommand("copy")
    );
  } else {
    document.execCommand("copy");
    flash("Copied");
  }
}

function flash(msg) {
  const btn = document.getElementById("copy-btn");
  const old = btn.textContent;
  btn.textContent = msg;
  setTimeout(() => (btn.textContent = old), 1200);
}

function savePat() {
  const val = document.getElementById("pat-input").value.trim();
  if (!val) { alert("Enter a PAT first."); return; }
  localStorage.setItem("github_pat", val);
  document.getElementById("pat-input").value = "";
  const s = document.getElementById("pat-status");
  s.textContent = "Saved ✓";
  setTimeout(() => (s.textContent = ""), 2000);
}

async function triggerRun() {
  const pat = localStorage.getItem("github_pat");
  if (!pat) {
    document.getElementById("setup").hidden = false;
    alert("Save your GitHub PAT in the Setup section first.");
    return;
  }
  const btn = document.getElementById("run-now-btn");
  const old = btn.textContent;
  btn.textContent = "Triggering…";
  btn.disabled = true;
  try {
    const res = await fetch(
      `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: "Bearer " + pat,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main", inputs: { force: true } }),
      }
    );
    if (res.status === 204) {
      btn.textContent = "Triggered ✓";
      setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 4000);
    } else {
      const text = await res.text();
      alert("GitHub API error " + res.status + ": " + text);
      btn.textContent = old;
      btn.disabled = false;
    }
  } catch (e) {
    alert("Request failed: " + e.message);
    btn.textContent = old;
    btn.disabled = false;
  }
}

document.getElementById("enable-btn").addEventListener("click", enablePush);
document.getElementById("copy-btn").addEventListener("click", copySub);
document.getElementById("pat-save-btn").addEventListener("click", savePat);
document.getElementById("run-now-btn").addEventListener("click", triggerRun);
document.getElementById("refresh-btn").addEventListener("click", loadBriefing);
document.getElementById("setup-toggle").addEventListener("click", () => {
  const s = document.getElementById("setup");
  s.hidden = !s.hidden;
});

registerSW();
loadBriefing();
