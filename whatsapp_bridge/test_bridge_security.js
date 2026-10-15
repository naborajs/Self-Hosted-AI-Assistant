// Test suite for WhatsApp Bridge Security & Authentication (Phase 1B)
const assert = require("assert");
const http = require("http");

process.env.SESSION_SECRET = "test-session-secret-must-be-32-bytes-long!";
process.env.WHATSAPP_BRIDGE_SECRET = "test-bridge-secret-value-9999";
process.env.WHATSAPP_BRIDGE_HOST = "127.0.0.1";
process.env.WHATSAPP_BRIDGE_PORT = "0"; // Ephemeral port

const { app, isLocalLoopbackRequest } = require("./index.js");

async function runTests() {
  console.log("Starting WhatsApp Bridge Security verification...");
  
  // 1. Verify loopback helper function
  assert.strictEqual(isLocalLoopbackRequest({ socket: { remoteAddress: "127.0.0.1" } }), true);
  assert.strictEqual(isLocalLoopbackRequest({ socket: { remoteAddress: "::1" } }), true);
  assert.strictEqual(isLocalLoopbackRequest({ socket: { remoteAddress: "::ffff:127.0.0.1" } }), true);
  assert.strictEqual(isLocalLoopbackRequest({ socket: { remoteAddress: "192.168.1.100" } }), false);
  assert.strictEqual(isLocalLoopbackRequest({ socket: { remoteAddress: "10.0.0.1" } }), false);
  assert.strictEqual(isLocalLoopbackRequest({ socket: { remoteAddress: "8.8.8.8" } }), false);
  console.log("✔ Loopback detection helper works correctly");

  // 2. Start server on ephemeral port
  const server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;

  try {
    // 3. Health endpoint is public
    const resHealth = await fetch(`${baseUrl}/health`);
    assert.strictEqual(resHealth.status, 200);
    console.log("✔ /health is reachable without auth");

    // 4. Operational endpoints reject unauthenticated requests
    const resStatusNoAuth = await fetch(`${baseUrl}/status`);
    assert.strictEqual(resStatusNoAuth.status, 401);

    const resPollNoAuth = await fetch(`${baseUrl}/poll`);
    assert.strictEqual(resPollNoAuth.status, 401);

    const resSendNoAuth = await fetch(`${baseUrl}/send-message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: "test", text: "hello" }),
    });
    assert.strictEqual(resSendNoAuth.status, 401);

    const resPairNoAuth = await fetch(`${baseUrl}/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone_number: "+12345" }),
    });
    assert.strictEqual(resPairNoAuth.status, 401);

    const resLogoutNoAuth = await fetch(`${baseUrl}/logout`, { method: "POST" });
    assert.strictEqual(resLogoutNoAuth.status, 401);
    console.log("✔ Operational endpoints (/status, /poll, /send-message, /pair, /logout) strictly reject unauthenticated requests");

    // 5. Query parameter secret is REJECTED (no secret in URL allowed!)
    const resQrQuery = await fetch(`${baseUrl}/qr?secret=test-bridge-secret-value-9999`);
    assert.strictEqual(resQrQuery.status, 401, "Expected 401 when passing secret as URL query parameter");

    const resPollQuery = await fetch(`${baseUrl}/poll?secret=test-bridge-secret-value-9999`);
    assert.strictEqual(resPollQuery.status, 401, "Expected 401 when passing secret as URL query parameter to /poll");

    const resStatusQuery = await fetch(`${baseUrl}/status?secret=test-bridge-secret-value-9999`);
    assert.strictEqual(resStatusQuery.status, 401, "Expected 401 when passing secret as URL query parameter to /status");
    console.log("✔ Query parameter authentication (?secret=...) is rejected across all endpoints");

    // 6. Header authentication succeeds
    const resStatusWithHeader = await fetch(`${baseUrl}/status`, {
      headers: { "x-bridge-secret": "test-bridge-secret-value-9999" },
    });
    assert.strictEqual(resStatusWithHeader.status, 200);

    const resStatusWithBearer = await fetch(`${baseUrl}/status`, {
      headers: { Authorization: "Bearer test-bridge-secret-value-9999" },
    });
    assert.strictEqual(resStatusWithBearer.status, 200);

    const resStatusWrongHeader = await fetch(`${baseUrl}/status`, {
      headers: { "x-bridge-secret": "wrong-secret-value" },
    });
    assert.strictEqual(resStatusWrongHeader.status, 401);
    console.log("✔ Header authentication (X-Bridge-Secret and Bearer) succeeds with correct secret and rejects incorrect secret");

    // 7. /qr requires secret header; returns 404 when QR not active, or 200 when active
    const resQrWithHeader = await fetch(`${baseUrl}/qr`, {
      headers: { "x-bridge-secret": "test-bridge-secret-value-9999" },
    });
    // Since currentQr is null in test, should be 404 with {"error": "QR code not available"}
    assert.strictEqual(resQrWithHeader.status, 404);
    const qrData = await resQrWithHeader.json();
    assert.strictEqual(qrData.error, "QR code not available");
    console.log("✔ /qr endpoint requires valid secret header and returns expected response");

    // 8. /qr-display on local loopback returns HTML page
    const resQrDisplay = await fetch(`${baseUrl}/qr-display`);
    assert.strictEqual(resQrDisplay.status, 200);
    const html = await resQrDisplay.text();
    assert.ok(html.includes("<!DOCTYPE html>"), "Expected HTML doctype");
    assert.ok(html.includes("EDITH"), "Expected title/branding");
    assert.ok(!html.includes("test-bridge-secret-value-9999"), "Secret must NEVER be embedded in HTML");
    console.log("✔ /qr-display delivers local loopback HTML page without leaking secrets");

    // 9. /qr-display rejects invalid Host header
    const badHostStatus = await new Promise((resolve, reject) => {
      const req = http.request(
        { hostname: "127.0.0.1", port, path: "/qr-display", headers: { Host: "evil-domain.com" } },
        (res) => resolve(res.statusCode)
      );
      req.on("error", reject);
      req.end();
    });
    assert.strictEqual(badHostStatus, 403);
    console.log("✔ /qr-display rejects non-local Host header with 403 Forbidden");

    // 10. /qr-display rejects cross-site fetch
    const resQrDisplayCrossSite = await fetch(`${baseUrl}/qr-display`, {
      headers: { "Sec-Fetch-Site": "cross-site" },
    });
    assert.strictEqual(resQrDisplayCrossSite.status, 403);
    console.log("✔ /qr-display rejects cross-site Sec-Fetch-Site requests with 403 Forbidden");

    console.log("\n==========================================");
    console.log("All WhatsApp Bridge Security tests PASSED!");
    console.log("==========================================");
  } finally {
    server.close();
  }
}

runTests().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
