const express = require('express');
const cors = require('cors');
const QRCode = require('qrcode');
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage } = require('baileys');
const pino = require('pino');
const axios = require('axios');
const fs = require('fs');
const path = require('path');

require('dotenv').config({ path: path.join(__dirname, '.env'), override: true });

const app = express();
app.use(cors());
app.use(express.json({ limit: process.env.JSON_BODY_LIMIT || '25mb' }));

const logger = pino({ level: 'silent' });

const DJANGO_WEBHOOK_URL = process.env.DJANGO_WEBHOOK_URL || 'http://localhost:8005/webhook/whatsapp/';
const DJANGO_BASE_URL = new URL(DJANGO_WEBHOOK_URL).origin;
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || 'secreto_webhook_123';
const PORT = process.env.PORT || 3001;
const HOST = process.env.HOST || '127.0.0.1';
const DEFAULT_LINE_ID = process.env.DEFAULT_LINE_ID || 'principal';
const DEFAULT_COUNTRY_CODE = String(process.env.WHATSAPP_DEFAULT_COUNTRY_CODE || '57').replace(/\D/g, '') || '57';
const BUILD_TAG = 'voice-reply-buffer-v2';

const MAX_QR_GENERATIONS = 20;
const QR_RETRY_DELAY_MS = 8000;
const CONNECTED_RETRY_DELAY_MS = 3000;
const POST_SCAN_RESTART_DELAY_MS = 1500;
const LOGOUT_TIMEOUT_MS = Number(process.env.LOGOUT_TIMEOUT_MS || 3000);

const sessionsRoot = path.join(__dirname, 'baileys_sessions');
fs.mkdirSync(sessionsRoot, { recursive: true });

const legacyAuthFolder = path.join(__dirname, 'baileys_auth_info');
const defaultAuthFolder = path.join(sessionsRoot, DEFAULT_LINE_ID);
if (fs.existsSync(legacyAuthFolder) && !fs.existsSync(defaultAuthFolder)) {
    fs.renameSync(legacyAuthFolder, defaultAuthFolder);
}

const sessions = new Map();

function normalizeLineId(lineId) {
    const clean = String(lineId || DEFAULT_LINE_ID)
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9_-]/g, '-')
        .replace(/-+/g, '-')
        .replace(/^-|-$/g, '');
    return clean || DEFAULT_LINE_ID;
}

function getSession(lineId = DEFAULT_LINE_ID) {
    const id = normalizeLineId(lineId);
    if (!sessions.has(id)) {
        sessions.set(id, {
            id,
            currentQR: null,
            connectionStatus: 'disconnected',
            socket: null,
            isConnecting: false,
            wasConnected: false,
            qrGenerationCount: 0,
            reconnectTimeout: null,
            phoneNumber: null,
            lastError: null,
        });
    }
    return sessions.get(id);
}

function getAuthFolder(session) {
    return path.join(sessionsRoot, session.id);
}

function sessionToJSON(session) {
    return {
        id: session.id,
        status: session.connectionStatus,
        hasQR: !!session.currentQR,
        wasConnected: session.wasConnected,
        qrCount: session.qrGenerationCount,
        maxQr: MAX_QR_GENERATIONS,
        phoneNumber: session.phoneNumber,
        lastError: session.lastError,
    };
}

function normalizePhoneNumber(number) {
    const clean = String(number || '').replace(/\D/g, '');
    if (clean.length === 10 && clean.startsWith('3')) {
        return `${DEFAULT_COUNTRY_CODE}${clean}`;
    }
    if (clean.length === 11 && clean.startsWith('03')) {
        return `${DEFAULT_COUNTRY_CODE}${clean.slice(1)}`;
    }
    return clean;
}

function unwrapMessageContent(message) {
    let content = message || {};
    const wrappers = [
        'ephemeralMessage',
        'viewOnceMessage',
        'viewOnceMessageV2',
        'viewOnceMessageV2Extension',
        'documentWithCaptionMessage',
    ];

    let guard = 0;
    while (content && guard < 10) {
        const wrapper = wrappers.find((key) => content[key]?.message);
        if (!wrapper) break;
        content = content[wrapper].message;
        guard++;
    }

    return content || {};
}

function resolveDjangoUrl(url) {
    if (!url) return '';
    return new URL(url, DJANGO_BASE_URL).toString();
}

function audioExtensionFromUrl(url) {
    const pathname = new URL(resolveDjangoUrl(url)).pathname.toLowerCase();
    if (pathname.endsWith('.wav')) return '.wav';
    if (pathname.endsWith('.ogg')) return '.ogg';
    if (pathname.endsWith('.m4a')) return '.m4a';
    return '.mp3';
}

function audioMimeFromPath(filePath) {
    const extension = path.extname(filePath).toLowerCase();
    if (extension === '.wav') return 'audio/wav';
    if (extension === '.ogg') return 'audio/ogg';
    if (extension === '.m4a') return 'audio/mp4';
    return 'audio/mpeg';
}

async function expandAudioUrls(audioUrl) {
    const resolvedUrl = resolveDjangoUrl(audioUrl);
    const parsed = new URL(resolvedUrl);
    const partsMetadata = parsed.searchParams.get('parts');
    if (!partsMetadata) {
        return [resolvedUrl];
    }

    const metadataUrl = resolveDjangoUrl(`/media/audios/${partsMetadata}`);
    const metadataRes = await axios.get(metadataUrl, { timeout: 15000 });
    const parts = Array.isArray(metadataRes.data?.parts) ? metadataRes.data.parts : [];
    if (!parts.length) {
        return [resolvedUrl];
    }

    return parts.map(resolveDjangoUrl);
}

async function sendAudioUrl(session, remoteJid, audioUrl, index = 0) {
    const resolvedUrl = resolveDjangoUrl(audioUrl);
    const extension = audioExtensionFromUrl(resolvedUrl);
    const audioPath = path.join(__dirname, `temp_${session.id}_${Date.now()}_${index}${extension}`);

    try {
        const audioRes = await axios.get(resolvedUrl, { responseType: 'arraybuffer', timeout: 60000 });
        const audioBuffer = Buffer.from(audioRes.data);
        fs.writeFileSync(audioPath, audioBuffer);

        await session.socket.sendMessage(remoteJid, {
            audio: audioBuffer,
            mimetype: audioMimeFromPath(audioPath),
            ptt: true,
        });
    } finally {
        if (fs.existsSync(audioPath)) {
            fs.unlinkSync(audioPath);
        }
    }
}

async function connectToWhatsApp(lineId = DEFAULT_LINE_ID) {
    const session = getSession(lineId);
    if (session.isConnecting) {
        return;
    }

    if (session.qrGenerationCount >= MAX_QR_GENERATIONS) {
        console.log(`[${session.id}] ⏱️ Límite alcanzado. Presiona "Conectar" para reintentar.`);
        stopReconnect(session);
        session.isConnecting = false;
        session.qrGenerationCount = 0;
        return;
    }

    stopReconnect(session);
    session.isConnecting = true;
    session.connectionStatus = 'connecting';

    try {
        const { version } = await fetchLatestBaileysVersion();
        const authFolder = getAuthFolder(session);
        const { state, saveCreds } = await useMultiFileAuthState(authFolder);

        session.socket = makeWASocket({
            version,
            auth: state,
            printQRInTerminal: true,
            logger,
            browser: [`Asistente ${session.id}`, 'Chrome', '120.0.0'],
            markOnlineOnConnect: false,
        });

        session.socket.ev.on('creds.update', saveCreds);

        session.socket.ev.on('connection.update', async (update) => {
            const { connection, lastDisconnect, qr } = update;

            if (qr && qr !== session.currentQR) {
                session.qrGenerationCount++;
                session.currentQR = qr;
                session.connectionStatus = 'connecting';
                console.log(`[${session.id}] 📱 QR (${session.qrGenerationCount}/${MAX_QR_GENERATIONS}) - Escanéalo ahora`);
            }

            if (connection === 'close') {
                const statusCode = lastDisconnect?.error?.output?.statusCode;
                const reason = lastDisconnect?.error?.toString() || 'Unknown';
                const errorData = lastDisconnect?.error?.output?.payload || lastDisconnect?.error?.data;
                const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
                const scannedQrNeedsRestart = statusCode === DisconnectReason.restartRequired;
                const invalidAuthBeforeQr = statusCode === DisconnectReason.loggedOut && !session.wasConnected;

                session.lastError = lastDisconnect?.error?.message || errorData?.message || reason;
                session.connectionStatus = 'disconnected';
                session.currentQR = null;
                session.isConnecting = false;

                console.log(`[${session.id}] ❌ Desconectado:`, reason);
                console.log(`[${session.id}] Código:`, statusCode || 'sin codigo', errorData ? JSON.stringify(errorData) : '');

                if (invalidAuthBeforeQr) {
                    try {
                        fs.rmSync(getAuthFolder(session), { recursive: true, force: true });
                        console.log(`[${session.id}] 🧹 Sesión local inválida borrada. Generando QR limpio en 1.5s...`);
                    } catch (cleanupError) {
                        console.error(`[${session.id}] No pude limpiar sesión inválida:`, cleanupError.message);
                    }
                    session.qrGenerationCount = 0;
                    session.socket = null;
                    session.reconnectTimeout = setTimeout(() => connectToWhatsApp(session.id), POST_SCAN_RESTART_DELAY_MS);
                } else if (scannedQrNeedsRestart) {
                    console.log(`[${session.id}] 🔄 WhatsApp aceptó el QR, reiniciando sesión en 1.5s...`);
                    session.reconnectTimeout = setTimeout(() => connectToWhatsApp(session.id), POST_SCAN_RESTART_DELAY_MS);
                } else if (shouldReconnect && session.wasConnected) {
                    console.log(`[${session.id}] 🔄 Reconectando en 3s...`);
                    session.reconnectTimeout = setTimeout(() => connectToWhatsApp(session.id), CONNECTED_RETRY_DELAY_MS);
                } else if (!session.wasConnected && session.qrGenerationCount < MAX_QR_GENERATIONS) {
                    console.log(`[${session.id}] 🔄 Nuevo QR en ${QR_RETRY_DELAY_MS / 1000}s... (${session.qrGenerationCount}/${MAX_QR_GENERATIONS})`);
                    session.reconnectTimeout = setTimeout(() => connectToWhatsApp(session.id), QR_RETRY_DELAY_MS);
                } else if (!session.wasConnected) {
                    stopReconnect(session);
                    console.log(`[${session.id}] ⏱️ Presiona "Conectar" para reintentar`);
                }
            } else if (connection === 'open') {
                session.connectionStatus = 'connected';
                session.currentQR = null;
                session.isConnecting = false;
                session.wasConnected = true;
                session.qrGenerationCount = 0;
                session.lastError = null;
                session.phoneNumber = session.socket.user?.id.split(':')[0] || null;
                stopReconnect(session);
                console.log(`[${session.id}] ✅ ¡Conectado!`);
                console.log(`[${session.id}] 📱 Número:`, session.phoneNumber);
            }
        });

        session.socket.ev.on('messages.upsert', async ({ messages, type }) => {
            if (type !== 'notify') return;

            for (const msg of messages) {
                if (!msg.message) continue;
                if (msg.key.remoteJid?.includes('status@broadcast')) continue;
                if (msg.key.fromMe) continue;

                const remoteJid = msg.key.remoteJid;
                const pushName = msg.pushName || 'Usuario';
                const phoneNumber = remoteJid.split('@')[0];
                const isGroup = remoteJid.endsWith('@g.us');
                const senderJid = msg.key.participant || remoteJid;
                const senderNumber = senderJid.split('@')[0];

                let messageText = '';
                let messageType = 'texto';
                let audioBase64 = null;
                let audioMimetype = null;

                const msgContent = unwrapMessageContent(msg.message);
                if (msgContent.conversation) {
                    messageText = msgContent.conversation;
                } else if (msgContent.extendedTextMessage?.text) {
                    messageText = msgContent.extendedTextMessage.text;
                } else if (msgContent.imageMessage) {
                    messageText = msgContent.imageMessage.caption || '[Imagen]';
                } else if (msgContent.videoMessage) {
                    messageText = msgContent.videoMessage.caption || '[Video]';
                } else if (msgContent.audioMessage) {
                    messageType = 'voz';
                    messageText = '[Audio]';
                    audioMimetype = msgContent.audioMessage.mimetype || 'audio/ogg';
                    try {
                        const mediaMessage = {
                            ...msg,
                            message: msgContent,
                        };
                        const buffer = await downloadMediaMessage(
                            mediaMessage,
                            'buffer',
                            {},
                            {
                                logger,
                                reuploadRequest: session.socket.updateMediaMessage?.bind(session.socket),
                            }
                        );
                        audioBase64 = buffer.toString('base64');
                        console.log(`[${session.id}] 🎙️ Audio descargado (${Math.round(buffer.length / 1024)} KB, ${audioMimetype})`);
                    } catch (audioError) {
                        console.error(`[${session.id}] ❌ No pude descargar audio:`, audioError.message);
                    }
                } else if (msgContent.protocolMessage) {
                    continue;
                }

                if (!messageText) continue;

                if (messageType === 'voz' && !audioBase64) {
                    console.warn(`[${session.id}] ⚠️ Audio detectado, pero no se pudo adjuntar audio_base64 al webhook`);
                }

                console.log(`[${session.id}] 📩 ${phoneNumber}: ${messageText.substring(0, 40)}...`);

                try {
                    const response = await axios.post(DJANGO_WEBHOOK_URL, {
                        linea: session.id,
                        linea_numero: session.phoneNumber,
                        numero: phoneNumber,
                        mensaje: messageText,
                        tipo: messageType,
                        nombre: pushName,
                        es_grupo: isGroup,
                        grupo_id: isGroup ? phoneNumber : null,
                        remitente_grupo: isGroup ? senderNumber : null,
                        audio_base64: audioBase64,
                        audio_mimetype: audioMimetype,
                    }, {
                        headers: {
                            'X-Webhook-Secret': WEBHOOK_SECRET,
                            'Content-Type': 'application/json',
                        },
                    });

                    const { respuesta, audio_url, transcripcion } = response.data;

                    if (messageType === 'voz') {
                        console.log(`[${session.id}] 📝 Transcripcion: ${(transcripcion || '').substring(0, 80) || 'sin transcripcion'}`);
                    }

                    let audioEnviado = false;
                    if (audio_url) {
                        try {
                            const audioUrls = await expandAudioUrls(audio_url);
                            console.log(`[${session.id}] 🔊 Enviando ${audioUrls.length} audio(s)`);
                            for (const [index, url] of audioUrls.entries()) {
                                console.log(`[${session.id}] 🔊 Audio ${index + 1}/${audioUrls.length}: ${url}`);
                                await sendAudioUrl(session, remoteJid, url, index);
                            }
                            audioEnviado = true;
                            console.log(`[${session.id}] ✅ Audio enviado`);
                        } catch (audioError) {
                            console.error(`[${session.id}] ❌ Error enviando audio de respuesta:`, audioError.message);
                        }
                    }

                    if (respuesta && !audioEnviado) {
                        await session.socket.sendMessage(remoteJid, { text: respuesta });
                        console.log(`[${session.id}] ✅ Texto enviado`);
                    }
                } catch (error) {
                    console.error('❌ Error webhook:', error.response?.data || error.message);
                }
            }
        });

    } catch (error) {
        console.error(`[${session.id}] ❌ Error conexión:`, error.message);
        session.lastError = error.message;
        session.isConnecting = false;
        session.connectionStatus = 'disconnected';
    }
}

function stopReconnect(session) {
    if (session?.reconnectTimeout) {
        clearTimeout(session.reconnectTimeout);
        session.reconnectTimeout = null;
    }
}

function resetSessionRuntime(session) {
    stopReconnect(session);
    session.wasConnected = false;
    session.connectionStatus = 'disconnected';
    session.isConnecting = false;
    session.qrGenerationCount = 0;
    session.currentQR = null;
    session.phoneNumber = null;
    session.lastError = null;
    session.socket = null;
}

function withTimeout(promise, milliseconds, message) {
    let timeoutId;
    const timeout = new Promise((_, reject) => {
        timeoutId = setTimeout(() => reject(new Error(message)), milliseconds);
    });

    return Promise.race([promise, timeout]).finally(() => clearTimeout(timeoutId));
}

async function logoutAndCloseSession(session) {
    stopReconnect(session);
    try {
        if (session.socket && session.connectionStatus === 'connected') {
            await withTimeout(
                session.socket.logout(),
                LOGOUT_TIMEOUT_MS,
                `logout excedio ${LOGOUT_TIMEOUT_MS}ms`
            );
        } else if (session.socket?.ws?.close) {
            session.socket.ws.close();
        }
    } catch (error) {
        console.error(`[${session.id}] Error cerrando sesion:`, error.message);
    }
    resetSessionRuntime(session);
}

app.get('/status', (req, res) => {
    res.json(sessionToJSON(getSession(req.query.linea)));
});

app.get('/status/:linea', (req, res) => {
    res.json(sessionToJSON(getSession(req.params.linea)));
});

app.get('/sessions', (req, res) => {
    const knownSessions = Array.from(sessions.values()).map(sessionToJSON);
    res.json({ sessions: knownSessions.length ? knownSessions : [sessionToJSON(getSession())] });
});

app.get('/qr', async (req, res) => {
    const session = getSession(req.query.linea);
    if (!session.currentQR) {
        return res.json({ error: 'Sin QR', ...sessionToJSON(session) });
    }

    const qrImage = await QRCode.toDataURL(session.currentQR);
    res.json({
        qr: qrImage,
        ...sessionToJSON(session),
    });
});

app.get('/qr/:linea', async (req, res) => {
    req.query.linea = req.params.linea;
    const session = getSession(req.params.linea);
    if (!session.currentQR) {
        return res.json({ error: 'Sin QR', ...sessionToJSON(session) });
    }
    const qrImage = await QRCode.toDataURL(session.currentQR);
    res.json({ qr: qrImage, ...sessionToJSON(session) });
});

app.post('/connect', (req, res) => {
    const body = req.body || {};
    const session = getSession(body.linea || req.query.linea);
    if (session.connectionStatus === 'connected') {
        return res.json({ error: 'Ya conectado' });
    }

    if (session.isConnecting || session.connectionStatus === 'connecting') {
        return res.json({
            message: 'Conexión ya iniciada',
            ...sessionToJSON(session),
        });
    }

    session.qrGenerationCount = 0;
    session.wasConnected = false;
    session.isConnecting = false;
    stopReconnect(session);

    connectToWhatsApp(session.id);
    res.json({ message: 'Iniciando...', ...sessionToJSON(session) });
});

app.post('/connect/:linea', (req, res) => {
    const session = getSession(req.params.linea);
    if (session.connectionStatus === 'connected') {
        return res.json({ error: 'Ya conectado', ...sessionToJSON(session) });
    }
    if (session.isConnecting || session.connectionStatus === 'connecting') {
        return res.json({ message: 'Conexión ya iniciada', ...sessionToJSON(session) });
    }
    session.qrGenerationCount = 0;
    session.wasConnected = false;
    session.isConnecting = false;
    stopReconnect(session);
    connectToWhatsApp(session.id);
    res.json({ message: 'Iniciando...', ...sessionToJSON(session) });
});

app.post('/disconnect', async (req, res) => {
    const body = req.body || {};
    const session = getSession(body.linea || req.query.linea);
    await logoutAndCloseSession(session);
    res.json({ message: 'Desconectado', ...sessionToJSON(session) });
});

app.post('/disconnect/:linea', async (req, res) => {
    const session = getSession(req.params.linea);
    await logoutAndCloseSession(session);
    res.json({ message: 'Desconectado', ...sessionToJSON(session) });
});

app.post('/delete-session/:linea', async (req, res) => {
    const session = getSession(req.params.linea);
    const authFolder = path.join(sessionsRoot, session.id);

    await logoutAndCloseSession(session);

    try {
        fs.rmSync(authFolder, { recursive: true, force: true });
        sessions.delete(session.id);
        res.json({
            message: 'Sesion eliminada',
            id: session.id,
            authDeleted: true,
            authFolder,
        });
    } catch (error) {
        res.status(500).json({
            error: `No pude borrar la sesion ${session.id}: ${error.message}`,
            id: session.id,
            authDeleted: false,
            authFolder,
        });
    }
});

app.post('/send-message', async (req, res) => {
    const body = req.body || {};
    const session = getSession(body.linea || req.query.linea);
    const numero = normalizePhoneNumber(body.numero);
    const mensaje = String(body.mensaje || '');

    if (!numero || !mensaje) {
        return res.status(400).json({ error: 'Faltan numero o mensaje' });
    }
    if (session.connectionStatus !== 'connected' || !session.socket) {
        return res.status(409).json({ error: `La linea ${session.id} no esta conectada`, ...sessionToJSON(session) });
    }

    const jid = `${numero}@s.whatsapp.net`;
    const result = await session.socket.sendMessage(jid, { text: mensaje });
    const messageId = result?.key?.id || null;
    console.log(`[${session.id}] 📤 Enviado a ${numero}${messageId ? ` (${messageId})` : ''}`);
    res.json({ message: 'Mensaje enviado', numero, jid, messageId, ...sessionToJSON(session) });
});

app.post('/send-audio', async (req, res) => {
    try {
        const body = req.body || {};
        const session = getSession(body.linea || req.query.linea);
        const numero = normalizePhoneNumber(body.numero);
        const audioUrl = String(body.audio_url || body.audioUrl || '');

        if (!numero || !audioUrl) {
            return res.status(400).json({ error: 'Faltan numero o audio_url' });
        }
        if (session.connectionStatus !== 'connected' || !session.socket) {
            return res.status(409).json({ error: `La linea ${session.id} no esta conectada`, ...sessionToJSON(session) });
        }

        const jid = `${numero}@s.whatsapp.net`;
        const urls = await expandAudioUrls(audioUrl);
        for (let index = 0; index < urls.length; index++) {
            await sendAudioUrl(session, jid, urls[index], index);
        }
        console.log(`[${session.id}] 🔊 Audio enviado a ${numero} (${urls.length} parte(s))`);
        res.json({ message: 'Audio enviado', numero, jid, parts: urls.length, ...sessionToJSON(session) });
    } catch (error) {
        res.status(500).json({ error: `No pude enviar audio: ${error.message}` });
    }
});

const server = app.listen(PORT, HOST);

server.on('listening', () => {
    console.log(`🚀 Baileys en http://${HOST}:${PORT}`);
    console.log(`🧩 Build: ${BUILD_TAG}`);
    console.log(`⏳ QR se regenera por ~5 minutos`);
    console.log(`📱 Línea por defecto: ${DEFAULT_LINE_ID}`);
    console.log(`➕ Para otra línea: POST /connect con {"linea":"ventas"} o POST /connect/ventas`);
});

server.on('error', (error) => {
    console.error(`❌ No se pudo iniciar Baileys en ${HOST}:${PORT}`);
    console.error(`   ${error.code || 'ERROR'}: ${error.message}`);
    if (error.code === 'EADDRINUSE') {
        console.error('   Ese puerto ya esta ocupado. Cambia PORT en .env o cierra el proceso anterior.');
    }
    process.exit(1);
});
