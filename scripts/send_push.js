#!/usr/bin/env node
/**
 * Send a web push to the iPhone PWA. Reads:
 *   - VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT (mailto:...)
 *   - PUSH_SUBSCRIPTION  (JSON the PWA gave you when you tapped Enable notifications)
 *   - docs/briefing.json (written by daily_alert.py earlier in the workflow)
 *
 * No-op if PUSH_SUBSCRIPTION is missing — lets you set up the rest of the
 * pipeline before capturing the iPhone subscription.
 */
const fs = require("fs");
const path = require("path");
const webpush = require("web-push");

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

  const briefingPath = path.join(__dirname, "..", "docs", "briefing.json");
  let briefing;
  try {
    briefing = JSON.parse(fs.readFileSync(briefingPath, "utf-8"));
  } catch (e) {
    console.error("ERROR: could not read docs/briefing.json.", e.message);
    process.exit(1);
  }

  webpush.setVapidDetails(subject, publicKey, privateKey);

  const payload = JSON.stringify({
    title: "Daily Briefing",
    body: briefing.headline || "Today's briefing is ready.",
    url: "./",
  });

  webpush
    .sendNotification(subscription, payload, { TTL: 60 * 60 * 6 })
    .then((res) => {
      console.log("Push sent. statusCode=", res.statusCode);
    })
    .catch((err) => {
      console.error("Push failed:", err.statusCode, err.body || err.message);
      // 404 / 410 means the subscription is dead — surface but don't fail
      // the whole workflow over it.
      if (err.statusCode === 404 || err.statusCode === 410) {
        console.error("Subscription is gone. Re-capture from the PWA and update PUSH_SUBSCRIPTION.");
        return;
      }
      process.exit(1);
    });
}

main();
