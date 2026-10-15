// Test suite for WhatsApp Bridge Durable Inbound Queue (Phase 2 Task 1)
const assert = require("assert");
const fs = require("fs").promises;
const http = require("http");
const path = require("path");

const TEST_STORAGE = path.resolve("./storage/test_whatsapp_bridge_queue");
process.env.SESSION_SECRET = "test-session-secret-must-be-32-bytes-long!";
process.env.WHATSAPP_BRIDGE_SECRET = "test-bridge-secret-value-9999";
process.env.WHATSAPP_BRIDGE_STORAGE = TEST_STORAGE;
process.env.WHATSAPP_BRIDGE_HOST = "127.0.0.1";
process.env.WHATSAPP_BRIDGE_PORT = "0";

const {
  app,
  handleIncomingMessages,
  loadInboundQueue,
  getQueue,
  setQueue,
  QUEUE_FILE,
} = require("./index.js");

async function cleanupStorage() {
  try {
    await fs.rm(TEST_STORAGE, { recursive: true, force: true });
  } catch (e) {}
}

async function runQueueTests() {
  console.log("Starting WhatsApp Bridge Durable Queue verification...");
  await cleanupStorage();

  const server = http.createServer(app);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;
  const authHeaders = {
    "Content-Type": "application/json",
    "x-bridge-secret": "test-bridge-secret-value-9999",
  };

  try {
    // Reset in-memory queue
    setQueue([]);

    // 1. Ingest synthetic incoming messages
    await handleIncomingMessages({
      messages: [
        {
          key: { id: "msg_001", remoteJid: "12345@s.whatsapp.net", fromMe: false },
          message: { conversation: "Hello from user 1" },
          messageTimestamp: 1000,
        },
        {
          key: { id: "msg_002", remoteJid: "67890@s.whatsapp.net", fromMe: false },
          message: { conversation: "Hello from user 2" },
          messageTimestamp: 1001,
        },
      ],
    });

    assert.strictEqual(getQueue().length, 2);
    console.log("✔ Messages ingested into queue");

    // 2. Verify durable file was created on disk
    const fileContent = await fs.readFile(QUEUE_FILE, "utf8");
    const parsedFile = JSON.parse(fileContent);
    assert.strictEqual(parsedFile.messages.length, 2);
    assert.strictEqual(parsedFile.messages[0].message_id, "msg_001");
    assert.strictEqual(parsedFile.messages[1].message_id, "msg_002");
    console.log("✔ Messages persisted to disk in QUEUE_FILE");

    // 3. GET /poll does NOT delete messages
    const poll1 = await fetch(`${baseUrl}/poll`, { headers: authHeaders });
    assert.strictEqual(poll1.status, 200);
    const pollData1 = await poll1.json();
    assert.strictEqual(pollData1.messages.length, 2);
    assert.strictEqual(pollData1.pending_count, 2);

    // Poll a second time: must still be 2
    const poll2 = await fetch(`${baseUrl}/poll`, { headers: authHeaders });
    assert.strictEqual(poll2.status, 200);
    const pollData2 = await poll2.json();
    assert.strictEqual(pollData2.messages.length, 2);
    assert.strictEqual(getQueue().length, 2);
    console.log("✔ GET /poll is non-destructive (unacknowledged messages remain)");

    // 4. POST /ack requires authentication
    const unauthAck = await fetch(`${baseUrl}/ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_ids: ["msg_001"] }),
    });
    assert.strictEqual(unauthAck.status, 401);
    console.log("✔ POST /ack strictly requires authentication");

    // 5. POST /ack rejects invalid payloads
    const invalidAck1 = await fetch(`${baseUrl}/ack`, {
      method: "POST",
      headers: authHeaders,
      body: JSON.stringify({}),
    });
    assert.strictEqual(invalidAck1.status, 400);

    const invalidAck2 = await fetch(`${baseUrl}/ack`, {
      method: "POST",
      headers: authHeaders,
      body: JSON.stringify({ message_ids: [] }),
    });
    assert.strictEqual(invalidAck2.status, 400);

    const invalidAck3 = await fetch(`${baseUrl}/ack`, {
      method: "POST",
      headers: authHeaders,
      body: JSON.stringify({ message_ids: [123, null] }),
    });
    assert.strictEqual(invalidAck3.status, 400);
    console.log("✔ POST /ack rejects invalid payloads with 400 Bad Request");

    // 6. POST /ack removes only specified IDs
    const ackResp = await fetch(`${baseUrl}/ack`, {
      method: "POST",
      headers: authHeaders,
      body: JSON.stringify({ message_ids: ["msg_001"] }),
    });
    assert.strictEqual(ackResp.status, 200);
    const ackData = await ackResp.json();
    assert.strictEqual(ackData.status, "acknowledged");
    assert.strictEqual(ackData.acknowledged_count, 1);
    assert.strictEqual(ackData.pending_count, 1);
    assert.strictEqual(getQueue().length, 1);
    assert.strictEqual(getQueue()[0].message_id, "msg_002");
    console.log("✔ POST /ack removes only specified IDs from memory");

    // Verify disk was updated atomically
    const fileContentAfterAck = await fs.readFile(QUEUE_FILE, "utf8");
    const parsedAfterAck = JSON.parse(fileContentAfterAck);
    assert.strictEqual(parsedAfterAck.messages.length, 1);
    assert.strictEqual(parsedAfterAck.messages[0].message_id, "msg_002");
    console.log("✔ POST /ack persists updated queue to disk");

    // 7. Idempotent acknowledgment (re-acknowledging msg_001 is harmless)
    const repeatAckResp = await fetch(`${baseUrl}/ack`, {
      method: "POST",
      headers: authHeaders,
      body: JSON.stringify({ message_ids: ["msg_001"] }),
    });
    assert.strictEqual(repeatAckResp.status, 200);
    const repeatAckData = await repeatAckResp.json();
    assert.strictEqual(repeatAckData.acknowledged_count, 0);
    assert.strictEqual(repeatAckData.pending_count, 1);
    console.log("✔ Repeated acknowledgment is idempotent and harmless");

    // 8. Simulated restart recovery: clear in-memory queue and reload from disk
    setQueue([]);
    assert.strictEqual(getQueue().length, 0);
    await loadInboundQueue();
    assert.strictEqual(getQueue().length, 1);
    assert.strictEqual(getQueue()[0].message_id, "msg_002");
    console.log("✔ Queue recovers unacknowledged messages after simulated restart");

    // 9. Deduplication check
    await handleIncomingMessages({
      messages: [
        {
          key: { id: "msg_002", remoteJid: "67890@s.whatsapp.net", fromMe: false },
          message: { conversation: "Duplicate of msg_002" },
          messageTimestamp: 1001,
        },
      ],
    });
    assert.strictEqual(getQueue().length, 1, "Duplicate message_id must not be appended");
    console.log("✔ Duplicate incoming message IDs are suppressed");

    // 10. Fallback ID generation for events lacking ID
    await handleIncomingMessages({
      messages: [
        {
          key: { remoteJid: "fallback_user@s.whatsapp.net", fromMe: false },
          message: { conversation: "Message without ID" },
          messageTimestamp: 2000,
        },
      ],
    });
    assert.strictEqual(getQueue().length, 2);
    const fallbackMsg = getQueue().find((m) => m.chat_id === "fallback_user@s.whatsapp.net");
    assert.ok(fallbackMsg.message_id.startsWith("fb_"), "Fallback ID should start with fb_");
    console.log("✔ Fallback stable ID generated when Baileys key.id is missing");

    // 11. Concurrent operations stress test
    const concurrentCount = 20;
    const promises = [];
    for (let i = 0; i < concurrentCount; i++) {
      promises.push(
        handleIncomingMessages({
          messages: [
            {
              key: { id: `concurrent_${i}`, remoteJid: `user_${i}@s.whatsapp.net`, fromMe: false },
              message: { conversation: `Concurrent message ${i}` },
              messageTimestamp: 3000 + i,
            },
          ],
        })
      );
    }
    await Promise.all(promises);

    // Verify all concurrent messages were recorded without loss
    const pollAll = await fetch(`${baseUrl}/poll?limit=100`, { headers: authHeaders });
    const allData = await pollAll.json();
    assert.strictEqual(allData.messages.length, 2 + concurrentCount);
    console.log("✔ Concurrent message insertions executed cleanly with serialization lock");

    console.log("\n==========================================");
    console.log("All WhatsApp Bridge Queue tests PASSED!");
    console.log("==========================================");
  } finally {
    server.close();
    await cleanupStorage();
  }
}

runQueueTests().catch((err) => {
  console.error("Queue test failed:", err);
  process.exit(1);
});
