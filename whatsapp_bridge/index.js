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

if (!SESSION_SECRET) {
  throw new Error("SESSION_SECRET is required for encrypted WhatsApp auth storage.");
}

const KEY = crypto.createHash("sha256").update(SESSION_SECRET, "utf8").digest();

let socket = null;
let currentQr = null;
let connectionState = "disconnected";
let connected = false;
let messageQueue = [];
let reconnectTask = null;

async function ensureStorage() {
  await fs.mkdir(STORAGE_PATH, { recursive: true });
  await fs.mkdir(MEDIA_PATH, { recursive: true });
}

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
      console.log('[bridge] messages.upsert ignored: fromMe=true', msg.key.id);
      continue;
    }

    const messageType = messageTypeFromMessage(msg.message);
    const content = extractText(msg.message);
    let mediaUrl = null;

    if (["imageMessage", "videoMessage", "audioMessage", "documentMessage"].includes(messageType)) {
      mediaUrl = await persistMedia(messageType, msg.message[messageType]);
    }

    const sender = msg.key.participant || msg.key.remoteJid;
    const senderName = msg.pushName || sender;

    try {
      console.log('[bridge] incoming message details:', JSON.stringify({ message_id: msg.key?.id, sender, sender_name: senderName, chat_id: msg.key?.remoteJid, message_type: messageType, content: content ? (content.length>200? content.slice(0,200)+'...': content) : null }));
    } catch (e) {
      console.log('[bridge] incoming message details');
    }

    const event = {
      message_id: msg.key.id,
      chat_id: msg.key.remoteJid,
      sender,
      sender_name: senderName,
      content,
      message_type: messageType,
      timestamp: msg.messageTimestamp || Date.now(),
      media_url: mediaUrl,
      from_group: msg.key.remoteJid?.endsWith("@g.us") || false,
    };
    messageQueue.push(event);
    console.log('[bridge] message queued, queue_size=', messageQueue.length, 'message_id=', msg.key.id);
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

app.get("/status", (_, res) => {
  res.json({ connected, connectionState });
});

app.post("/pair", async (req, res) => {
  try {
    await startSocket();
    const data = { pairing_code: currentQr, connected, connectionState };
    res.json(data);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

app.get("/pairing_code", (_, res) => {
  res.json({
    qr_available: Boolean(currentQr),
    connection_state: connectionState,
  });
});

app.get("/qr", async (req, res) => {
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

app.get("/poll", (req, res) => {
  try {
    console.log('[bridge] /poll requested - queue_size=', messageQueue.length);
  } catch (e) {}
  const items = [...messageQueue];
  messageQueue = [];
  res.json({ messages: items });
});

app.post("/send-message", async (req, res) => {
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

app.post("/typing", async (req, res) => {
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

app.post("/logout", async (_, res) => {
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

app.get("/media/:fileName", async (req, res) => {
  const filePath = path.join(MEDIA_PATH, req.params.fileName);
  try {
    res.sendFile(filePath);
  } catch (error) {
    res.status(404).json({ error: "Media not found" });
  }
});

app.listen(BRIDGE_PORT, BRIDGE_HOST, () => {
  console.log(`WhatsApp bridge listening on http://${BRIDGE_HOST}:${BRIDGE_PORT}`);
  startSocket().catch((error) => console.error("Failed to start WhatsApp socket:", error.message));
});
