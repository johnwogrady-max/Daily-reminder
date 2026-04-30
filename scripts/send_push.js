#!/usr/bin/env node
/**
 * Send the briefing as an end-to-end-encrypted web push to the iPhone PWA.
 *
 * The full briefing body travels inside the encrypted push payload — only
 * the subscribed device can decrypt it. The service worker stashes the body
 * in the local Cache API; the PWA reads from there. Nothing personal is
 * ever published to the public Pages site.
 *
 * Reads:
 *   - VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT (mailto:...)
 *   - PUSH_SUBSCRIPTION  (JSON the PWA gave you when you tapped Enable notifications)
 *   - _briefing.json     (written by daily_alert.py earlier in the workflow,
 *                         gitignored, never committed)
 *
 * No-op if PUSH_SUBSCRIPTION is missing — lets the rest of the pipeline run
 * before you've captured the iPhone subscription.
 */
const fs = require("fs");
const path = require("path");
const webpush = require("web-push");

// Web Push payload limit on most browsers/services is ~4 KB encrypted.
// Plaintext should stay under ~3 KB to be safe.
const MAX_BODY_BYTES = 2800;

function envOrNull(name) {
  const v = process.env[name];
  return v && v.trim() ? v.trim() : null;
}

function envOrDie(name) {
  const v = envOrNull(name);
  if (!v) {
    console.error(`ERROR: ${name} is not set.`);
    process.exit(1);
  }
  return v;
}

function truncateUtf8(text, maxBytes) {
  if (Buffer.byteLength(text, "utf8") <= maxBytes) return text;
  const suffix = "\n\n[Briefing truncated — open the app for the full version]";
  const budget = maxBytes - Buffer.byteLength(suffix, "utf8");
  let out = text;
  while (Buffer.byteLength(out, "utf8") > budget) {
    out = out.slice(0, -16);
  }
  return out + suffix;
}

function main() {
  const subRaw = envOrNull("PUSH_SUBSCRIPTION");
  if (!subRaw) {
    console.log("PUSH_SUBSCRIPTION not set; skipping web push.");
    return;
  }

  const publicKey = envOrDie("VAPID_PUBLIC_KEY");
  const privateKey = envOrDie("VAPID_PRIVATE_KEY");
  const subject = envOrNull("VAPID_SUBJECT") || "mailto:noreply@example.com";

  let subscription;
  try {
    subscription = JSON.parse(subRaw);
  } catch (e) {
    console.error("ERROR: PUSH_SUBSCRIPTION is not valid JSON.", e.message);
    process.exit(1);
  }

  const briefingPath = path.join(__dirname, "..", "_briefing.json");
  let briefing;
  try {
    briefing = JSON.parse(fs.readFileSync(briefingPath, "utf-8"));
  } catch (e) {
    console.error("ERROR: could not read _briefing.json.", e.message);
    process.exit(1);
  }

  webpush.setVapidDetails(subject, publicKey, privateKey);

  const body = truncateUtf8(briefing.body || "", MAX_BODY_BYTES);
  const payload = JSON.stringify({
    title: "Daily Briefing",
    headline: briefing.headline || "Today's briefing is ready.",
    body,
    generated_at: briefing.generated_at,
    url: "./",
  });

  console.log(`Push payload size: ${Buffer.byteLength(payload, "utf8")} bytes`);

  webpush
    .sendNotification(subscription, payload, { TTL: 60 * 60 * 6 })
    .then((res) => {
      console.log("Push sent. statusCode=", res.statusCode);
    })
    .catch((err) => {
      console.error("Push failed:", err.statusCode, err.body || err.message);
      if (err.statusCode === 404 || err.statusCode === 410) {
        console.error("Subscription is gone. Re-capture from the PWA and update PUSH_SUBSCRIPTION.");
        return;
      }
      process.exit(1);
    });
}

main();
