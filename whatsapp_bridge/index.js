require("dotenv").config();

const express = require("express");
const cors = require("cors");
const crypto = require("crypto");
const fs = require("fs").promises;
const path = require("path");
const qrcode = require("qrcode");
const { fetch } = require("undici");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  BufferJSON,
  DisconnectReason,
  fetchLatestBaileysVersion,
  Browsers,
  downloadContentFromMessage,
} = require("@whiskeysockets/baileys");

const app = express();
app.use(cors());
app.use(express.json({ limit: "50mb" }));

const BRIDGE_HOST = process.env.WHATSAPP_BRIDGE_HOST || "127.0.0.1";
const BRIDGE_PORT = Number(process.env.WHATSAPP_BRIDGE_PORT || 3000);
const STORAGE_PATH = path.resolve(process.env.WHATSAPP_BRIDGE_STORAGE || "./storage/whatsapp_bridge");
const AUTH_DIR = path.join(STORAGE_PATH, "auth_state");
const ENCRYPTED_AUTH_FILE = path.join(STORAGE_PATH, "auth_state.json.enc");
const MEDIA_PATH = path.join(STORAGE_PATH, "media");
const SESSION_SECRET = process.env.SESSION_SECRET;
const BRIDGE_SECRET = process.env.WHATSAPP_BRIDGE_SECRET || process.env.BRIDGE_SECRET;

if (!SESSION_SECRET) {
  throw new Error("SESSION_SECRET is required for encrypted WhatsApp auth storage.");
}

const KEY = crypto.createHash("sha256").update(SESSION_SECRET, "utf8").digest();

function constantTimeCompare(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const bufA = Buffer.from(a, "utf8");
  const bufB = Buffer.from(b, "utf8");
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

function isLocalLoopbackRequest(req) {
  const remoteIp = req.socket?.remoteAddress || req.connection?.remoteAddress || req.ip;
  if (!remoteIp) return false;
  return (
    remoteIp === "127.0.0.1" ||
    remoteIp === "::1" ||
    remoteIp === "::ffff:127.0.0.1"
  );
}

function requireBridgeAuth(req, res, next) {
  if (!BRIDGE_SECRET) {
    console.error("[bridge] Request rejected: WHATSAPP_BRIDGE_SECRET is not configured on the bridge.");
    return res.status(500).json({ error: "Bridge secret is not configured" });
  }

  const headerSecret = req.headers["x-bridge-secret"] || (req.headers.authorization?.startsWith("Bearer ") ? req.headers.authorization.slice(7) : null);

  if (!headerSecret || !constantTimeCompare(headerSecret, BRIDGE_SECRET)) {
    return res.status(401).json({
      error: "Unauthorized: Invalid or missing bridge secret. Operational and programmatic endpoints require X-Bridge-Secret header. For local browser pairing, visit /qr-display on localhost.",
    });
  }
  next();
}

let socket = null;
let currentQr = null;
let connectionState = "disconnected";
let connected = false;
let messageQueue = [];
let reconnectTask = null;

const QUEUE_FILE = path.join(STORAGE_PATH, "inbound_queue.json");

let queueLock = Promise.resolve();
function withQueueLock(fn) {
  const result = queueLock.then(fn, fn);
  queueLock = result.catch(() => {});
  return result;
}

async function ensureStorage() {
  await fs.mkdir(STORAGE_PATH, { recursive: true });
  await fs.mkdir(MEDIA_PATH, { recursive: true });
}

async function saveQueueToFile(messages) {
  await ensureStorage();
  const tmpFile = `${QUEUE_FILE}.tmp.${Date.now()}.${Math.random().toString(36).slice(2)}`;
  const data = JSON.stringify({ version: 1, messages }, null, 2);
  await fs.writeFile(tmpFile, data, { encoding: "utf8", mode: 0o600 });
  await fs.rename(tmpFile, QUEUE_FILE);
}

async function loadInboundQueue() {
  try {
    await ensureStorage();
    const data = await fs.readFile(QUEUE_FILE, "utf8");
    const parsed = JSON.parse(data);
    if (Array.isArray(parsed.messages)) {
      messageQueue = parsed.messages;
      console.log(`[bridge] Loaded ${messageQueue.length} pending messages from durable queue.`);
    }
  } catch (err) {
    if (err.code !== "ENOENT") {
      console.error("[bridge] Failed to load durable queue:", err.message);
    }
  }
}

ensureStorage().then(loadInboundQueue).catch((err) => console.error("[bridge] Storage init error:", err.message));

function encryptStateBuffer(buffer) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", KEY, iv);
  const encrypted = Buffer.concat([cipher.update(buffer), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, encrypted]);
}

function decryptStateBuffer(buffer) {
  const iv = buffer.subarray(0, 12);
  const tag = buffer.subarray(12, 28);
  const encrypted = buffer.subarray(28);
  const decipher = crypto.createDecipheriv("aes-256-gcm", KEY, iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(encrypted), decipher.final()]);
}

async function loadAuthState() {
  try {
    const encrypted = await fs.readFile(ENCRYPTED_AUTH_FILE);
    const decrypted = decryptStateBuffer(encrypted);
    const authState = JSON.parse(decrypted.toString("utf8"), BufferJSON.reviver);

    await fs.rm(AUTH_DIR, { recursive: true, force: true });
    await fs.mkdir(AUTH_DIR, { recursive: true });

    await fs.writeFile(
      path.join(AUTH_DIR, "creds.json"),
      JSON.stringify(authState.creds, BufferJSON.replacer),
      { mode: 0o600 }
    );

    if (authState.keys) {
      for (const fileName of Object.keys(authState.keys)) {
        await fs.writeFile(
          path.join(AUTH_DIR, `${fileName}.json`),
          JSON.stringify(authState.keys[fileName], BufferJSON.replacer),
          { mode: 0o600 }
        );
      }
    }

    return true;
  } catch {
    return false;
  }
}

async function saveAuthState() {
  try {
    const credsPath = path.join(AUTH_DIR, "creds.json");
    const creds = JSON.parse(await fs.readFile(credsPath, "utf8"), BufferJSON.reviver);
    const files = await fs.readdir(AUTH_DIR);
    const keys = {};

    for (const file of files) {
      if (file === "creds.json" || !file.endsWith(".json")) {
        continue;
      }
      const keyName = file.slice(0, -5);
      keys[keyName] = JSON.parse(await fs.readFile(path.join(AUTH_DIR, file), "utf8"), BufferJSON.reviver);
    }

    const authState = { creds, keys };
    const encrypted = encryptStateBuffer(Buffer.from(JSON.stringify(authState, BufferJSON.replacer), "utf8"));
    await fs.writeFile(ENCRYPTED_AUTH_FILE, encrypted, { mode: 0o600 });
  } catch (error) {
    console.error("Failed to encrypt auth state:", error.message);
  }
}

async function createAuthState() {
  await ensureStorage();
  const loaded = await loadAuthState();
  if (!loaded) {
    await fs.rm(AUTH_DIR, { recursive: true, force: true });
    await fs.mkdir(AUTH_DIR, { recursive: true });
  }
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  async function wrappedSaveState() {
    await saveCreds();
    await saveAuthState();
  }

  return { state, saveState: wrappedSaveState };
}

function messageTypeFromMessage(message) {
  if (!message) return "unknown";
  // Unwrap ephemeral/view-once wrappers if present
  if (message.ephemeralMessage && message.ephemeralMessage.message) {
    return messageTypeFromMessage(message.ephemeralMessage.message);
  }
  if (message.viewOnceMessage && message.viewOnceMessage.message) {
    return messageTypeFromMessage(message.viewOnceMessage.message);
  }
  const keys = Object.keys(message || {}).filter((key) => key !== "viewOnceMessage");
  return keys.length > 0 ? keys[0] : "unknown";
}

function extractText(message) {
  if (!message) return "";
  // If already a plain string, return it directly
  if (typeof message === "string") return message;

  // Unwrap common wrappers
  if (message.ephemeralMessage && message.ephemeralMessage.message) {
    return extractText(message.ephemeralMessage.message);
  }
  if (message.viewOnceMessage && message.viewOnceMessage.message) {
    return extractText(message.viewOnceMessage.message);
  }

  // Direct conversation text
  if (typeof message.conversation === "string") return message.conversation;

  // Extended text (quoted/replied messages and improved text handling)
  if (message.extendedTextMessage && typeof message.extendedTextMessage.text === "string") {
    return message.extendedTextMessage.text;
  }

  // Captions on media
  if (message.imageMessage && typeof message.imageMessage.caption === "string") return message.imageMessage.caption;
  if (message.videoMessage && typeof message.videoMessage.caption === "string") return message.videoMessage.caption;
  if (message.audioMessage && typeof message.audioMessage.caption === "string") return message.audioMessage.caption;
  if (message.documentMessage && (typeof message.documentMessage.caption === "string" || typeof message.documentMessage.fileName === "string")) {
    return message.documentMessage.caption || message.documentMessage.fileName;
  }

  // Some variants use a `text` property
  if (typeof message.text === "string") return message.text;

  // Fall back to scanning for the first string value in the object
  try {
    for (const v of Object.values(message)) {
      if (typeof v === "string" && v.trim()) return v;
      if (typeof v === "object") {
        // nested object may contain text
        const nested = extractText(v);
        if (nested) return nested;
      }
    }
  } catch (e) {
    // ignore
  }

  // If we reach here, extraction failed — log the raw payload for debugging
  try {
    console.warn('[bridge] extractText failed - raw message payload:', JSON.stringify(message));
  } catch (e) {
    console.warn('[bridge] extractText failed - raw message payload (non-serializable)');
  }

  return "";
}

async function persistMedia(messageType, messageContent) {
  try {
    const stream = await downloadContentFromMessage(messageContent, messageType);
    const chunks = [];
    for await (const chunk of stream) {
      chunks.push(chunk);
    }
    const buffer = Buffer.concat(chunks);
    const extension = messageContent.mimetype?.split("/")[1] || messageType;
    const fileName = `${Date.now()}-${crypto.randomBytes(4).toString("hex")}.${extension}`;
    const filePath = path.join(MEDIA_PATH, fileName);
    await fs.writeFile(filePath, buffer);
    return `/media/${fileName}`;
  } catch (error) {
    console.error("Failed to persist media message:", error.message);
    return null;
  }
}

async function handleIncomingMessages(upsert) {
  for (const msg of upsert.messages || []) {
    // Log raw upsert event
    try {
      console.log('[bridge] messages.upsert received:', JSON.stringify({ id: msg.key?.id, fromMe: msg.key?.fromMe, remoteJid: msg.key?.remoteJid }));
    } catch (e) {
      console.log('[bridge] messages.upsert received');
    }

    if (!msg.message) {
      console.log('[bridge] messages.upsert ignored: no message payload', msg.key?.id);
      continue;
    }
    if (msg.key && msg.key.fromMe) {
      console.log('[bridge] messages.upsert ignored: fromMe=true', msg.key?.id);
      continue;
    }

    const messageType = messageTypeFromMessage(msg.message);
    const content = extractText(msg.message);
    let mediaUrl = null;

    if (["imageMessage", "videoMessage", "audioMessage", "documentMessage"].includes(messageType)) {
      mediaUrl = await persistMedia(messageType, msg.message[messageType]);
    }

    const sender = msg.key?.participant || msg.key?.remoteJid;
    const senderName = msg.pushName || sender;

    let messageId = msg.key?.id;
    if (!messageId || typeof messageId !== "string" || !messageId.trim()) {
      const hashInput = `${msg.key?.remoteJid || ""}-${msg.messageTimestamp || Date.now()}-${content || ""}`;
      messageId = "fb_" + crypto.createHash("sha256").update(hashInput).digest("hex").slice(0, 24);
    }

    try {
      console.log('[bridge] incoming message details:', JSON.stringify({ message_id: messageId, sender, sender_name: senderName, chat_id: msg.key?.remoteJid, message_type: messageType, content: content ? (content.length>200? content.slice(0,200)+'...': content) : null }));
    } catch (e) {
      console.log('[bridge] incoming message details');
    }

    const event = {
      message_id: messageId,
      chat_id: msg.key?.remoteJid,
      sender,
      sender_name: senderName,
      content,
      message_type: messageType,
      timestamp: msg.messageTimestamp || Date.now(),
      media_url: mediaUrl,
      from_group: msg.key?.remoteJid?.endsWith("@g.us") || false,
    };

    await withQueueLock(async () => {
      const exists = messageQueue.some((m) => m.message_id === messageId);
      if (exists) {
        console.log('[bridge] Duplicate message ignored:', messageId);
        return;
      }
      messageQueue.push(event);
      await saveQueueToFile(messageQueue);
      console.log('[bridge] message queued durably, queue_size=', messageQueue.length, 'message_id=', messageId);
    });
  }
}

async function startSocket() {
  if (socket) {
    return;
  }

  try {
    const { state, saveState } = await createAuthState();
    const { version } = await fetchLatestBaileysVersion();
    currentQr = null;
    connectionState = "connecting";
    connected = false;

    socket = makeWASocket({
      auth: state,
      browser: Browsers.macOS("WhatsApp Assistant"),
      version,
    });

    socket.ev.on("connection.update", async (update) => {
      if (update.qr) {
        currentQr = update.qr;
        connectionState = "pairing";
        connected = false;
      }
      if (update.connection === "connecting") {
        connectionState = "connecting";
      }
      if (update.connection === "open") {
        currentQr = null;
        connectionState = "connected";
        connected = true;
      }
      if (update.connection === "close") {
        connected = false;
        connectionState = "disconnected";
        const shouldReconnect = update.lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
        if (shouldReconnect) {
          socket = null;
          reconnectTask = setTimeout(() => {
            startSocket().catch((error) => console.error("Reconnect failed:", error.message));
          }, 5000);
        }
      }
    });

    socket.ev.on("creds.update", saveState);
    socket.ev.on("messages.upsert", handleIncomingMessages);
  } catch (error) {
    console.error("WhatsApp bridge socket failed to start:", error.message);
    socket = null;
    connected = false;
    connectionState = "error";
  }
}

async function sendMessageToChat(chatId, payload) {
  try {
    console.log('[bridge] sendMessageToChat called for chat:', chatId);
  } catch (e) {}
  if (!socket) {
    console.log('[bridge] sendMessageToChat failed: socket not available');
    throw new Error("WhatsApp socket is not available");
  }
  try {
    const result = await socket.sendMessage(chatId, payload);
    try { console.log('[bridge] sendMessageToChat success for', chatId); } catch (e) {}
    return result;
  } catch (err) {
    console.error('[bridge] sendMessageToChat error:', err && err.message);
    throw err;
  }
}

app.get("/health", (_, res) => {
  res.status(200).json({ status: "ok" });
});

app.get("/status", requireBridgeAuth, (_, res) => {
  res.json({ connected, connectionState });
});

app.post("/pair", requireBridgeAuth, async (req, res) => {
  try {
    await startSocket();
    const data = { pairing_code: currentQr, connected, connectionState };
    res.json(data);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.get("/pairing_code", requireBridgeAuth, (_, res) => {
  res.json({
    qr_available: Boolean(currentQr),
    connection_state: connectionState,
  });
});

app.get("/qr", requireBridgeAuth, async (req, res) => {
  if (!currentQr) {
    return res.status(404).json({ error: "QR code not available" });
  }

  try {
    const buffer = await qrcode.toBuffer(currentQr, { type: "png", width: 500 });
    res.type("image/png");
    res.send(buffer);
  } catch (error) {
    res.status(500).json({ error: "Failed to generate QR image" });
  }
});

app.get("/qr-display", async (req, res) => {
  if (!isLocalLoopbackRequest(req)) {
    return res.status(403).json({ error: "Forbidden: Local pairing display is restricted to loopback connections" });
  }

  const host = req.headers.host || "";
  const isLocalHost = host.startsWith("localhost") || host.startsWith("127.0.0.1") || host.startsWith("[::1]");
  if (!isLocalHost) {
    return res.status(403).json({ error: "Forbidden: Invalid Host header" });
  }

  const secFetchSite = req.headers["sec-fetch-site"];
  if (secFetchSite && !["same-origin", "none", "same-site"].includes(secFetchSite)) {
    return res.status(403).json({ error: "Forbidden: Cross-site access not permitted" });
  }

  if (connected) {
    return res.type("html").send(`<!DOCTYPE html>
<html>
<head><title>WhatsApp Connected - EDITH</title><style>body{font-family:sans-serif;background:#0f172a;color:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}.card{background:#1e293b;padding:2.5rem;border-radius:12px;text-align:center;max-width:400px;box-shadow:0 10px 25px rgba(0,0,0,0.5);}.ok{color:#4ade80;font-size:3rem;margin-bottom:0.5rem;}h2{margin:0.5rem 0;}p{color:#94a3b8;}</style></head>
<body><div class="card"><div class="ok">&#x2714;</div><h2>WhatsApp is Connected</h2><p>Your companion session is active and ready.</p></div></body>
</html>`);
  }

  if (!currentQr) {
    return res.type("html").send(`<!DOCTYPE html>
<html>
<head><title>WhatsApp Pairing - EDITH</title><meta http-equiv="refresh" content="3"><style>body{font-family:sans-serif;background:#0f172a;color:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}.card{background:#1e293b;padding:2.5rem;border-radius:12px;text-align:center;max-width:420px;box-shadow:0 10px 25px rgba(0,0,0,0.5);}h2{color:#38bdf8;margin-top:0;}p{color:#94a3b8;font-size:0.9rem;}</style></head>
<body><div class="card"><h2>Initializing WhatsApp...</h2><p>Waiting for pairing QR code from WhatsApp socket. Page refreshes automatically every 3 seconds.</p></div></body>
</html>`);
  }

  try {
    const qrDataUrl = await qrcode.toDataURL(currentQr, { width: 300, margin: 2 });
    return res.type("html").send(`<!DOCTYPE html>
<html>
<head><title>WhatsApp Companion Pairing - EDITH</title><meta http-equiv="refresh" content="20"><style>body{font-family:sans-serif;background:#0f172a;color:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}.card{background:#1e293b;padding:2rem;border-radius:12px;text-align:center;max-width:440px;box-shadow:0 10px 25px rgba(0,0,0,0.5);}h2{color:#38bdf8;margin:0 0 0.5rem 0;font-size:1.3rem;}ol{color:#cbd5e1;text-align:left;font-size:0.85rem;line-height:1.5;margin:1rem 0;padding-left:1.5rem;}img{background:#fff;padding:10px;border-radius:8px;margin:0.5rem 0;display:inline-block;}.footer{font-size:0.75rem;color:#64748b;margin-top:1rem;}</style></head>
<body><div class="card"><h2>WhatsApp Companion Pairing</h2><ol><li>Open WhatsApp on your phone</li><li>Tap Menu or Settings &gt; Linked Devices</li><li>Tap Link a Device and scan the QR code below</li></ol><img src="${qrDataUrl}" alt="WhatsApp QR Code" /><div class="footer">Local loopback pairing interface (no secrets in URL). Refreshes every 20 seconds.</div></div></body>
</html>`);
  } catch (err) {
    return res.status(500).json({ error: "Failed to render QR code image" });
  }
});

app.get("/poll", requireBridgeAuth, async (req, res) => {
  await withQueueLock(async () => {
    try {
      console.log('[bridge] /poll requested - queue_size=', messageQueue.length);
    } catch (e) {}
    const limitParam = parseInt(req.query.limit, 10);
    const limit = (!isNaN(limitParam) && limitParam > 0) ? Math.min(limitParam, 500) : 50;
    const items = messageQueue.slice(0, limit);
    res.json({ messages: items, pending_count: messageQueue.length });
  });
});

app.post("/ack", requireBridgeAuth, async (req, res) => {
  await withQueueLock(async () => {
    const rawIds = req.body?.message_ids !== undefined ? req.body.message_ids : req.body?.message_id;
    if (rawIds === undefined || rawIds === null) {
      return res.status(400).json({ error: "Invalid payload: message_ids array or message_id string is required" });
    }
    const idsToAck = Array.isArray(rawIds) ? rawIds : [rawIds];
    if (idsToAck.length === 0 || !idsToAck.every((id) => typeof id === "string" && id.trim())) {
      return res.status(400).json({ error: "Invalid payload: message_ids must be a non-empty array of valid strings" });
    }

    const idSet = new Set(idsToAck);
    const originalCount = messageQueue.length;
    messageQueue = messageQueue.filter((m) => !idSet.has(m.message_id));
    const acknowledgedCount = originalCount - messageQueue.length;

    await saveQueueToFile(messageQueue);
    console.log(`[bridge] /ack processed: ${acknowledgedCount} acknowledged, ${messageQueue.length} pending.`);
    res.json({
      status: "acknowledged",
      acknowledged_count: acknowledgedCount,
      pending_count: messageQueue.length,
    });
  });
});

app.post("/send-message", requireBridgeAuth, async (req, res) => {
  try {
    const { chat_id, text, file_url, mime_type, caption } = req.body;
    try { console.log('[bridge] /send-message payload received:', JSON.stringify({ chat_id, text: text? (text.length>200? text.slice(0,200)+'...': text): null, file_url, mime_type, caption })); } catch (e) {}
    const payload = {};
    if (file_url) {
      if (mime_type?.startsWith("image/")) {
        payload.image = { url: file_url, caption: caption || text };
      } else if (mime_type?.startsWith("video/")) {
        payload.video = { url: file_url, caption: caption || text };
      } else if (mime_type?.startsWith("audio/")) {
        payload.audio = { url: file_url, mimetype: mime_type, ptt: true };
      } else {
        payload.document = { url: file_url, mimetype: mime_type || "application/octet-stream", fileName: caption || "attachment" };
      }
    } else {
      payload.text = text || "";
    }
    await sendMessageToChat(chat_id, payload);
    res.json({ status: "queued" });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.post("/typing", requireBridgeAuth, async (req, res) => {
  try {
    const { chat_id } = req.body;
    if (!socket) {
      throw new Error("WhatsApp socket is not available");
    }
    await socket.presenceSubscribe(chat_id);
    await socket.sendPresenceUpdate("composing", chat_id);
    setTimeout(() => socket.sendPresenceUpdate("paused", chat_id), 2500);
    res.json({ status: "typing" });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.post("/logout", requireBridgeAuth, async (_, res) => {
  try {
    if (socket) {
      await socket.logout();
      socket = null;
    }
    try {
      await fs.unlink(ENCRYPTED_AUTH_FILE);
    } catch {
      // ignore
    }
    try {
      await fs.rm(AUTH_DIR, { recursive: true, force: true });
    } catch {
      // ignore
    }
    connected = false;
    connectionState = "logged_out";
    res.json({ status: "logged_out" });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.get("/media/:fileName", requireBridgeAuth, async (req, res) => {
  const filePath = path.join(MEDIA_PATH, req.params.fileName);
  try {
    res.sendFile(filePath);
  } catch (error) {
    res.status(404).json({ error: "Media not found" });
  }
});

if (require.main === module) {
  app.listen(BRIDGE_PORT, BRIDGE_HOST, () => {
    console.log(`WhatsApp bridge listening on http://${BRIDGE_HOST}:${BRIDGE_PORT}`);
    startSocket().catch((error) => console.error("Failed to start WhatsApp socket:", error.message));
  });
}

module.exports = {
  app,
  requireBridgeAuth,
  isLocalLoopbackRequest,
  handleIncomingMessages,
  loadInboundQueue,
  saveQueueToFile,
  withQueueLock,
  QUEUE_FILE,
  getQueue: () => messageQueue,
  setQueue: (q) => { messageQueue = q; },
};
