import os
import sqlite3
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from google import genai
from google.genai import types
from dotenv import load_dotenv

# ==========================================
# UPGRADE SETTINGS (Yahan se AI upgrade karein)
# ==========================================
PRIMARY_MODEL = "gemini-2.5-flash"
BACKUP_MODEL = "gemini-3.5-flash-lite"
AI_PERSONALITY = "You are a smart, fast AI assistant. Keep answers clean, direct, and easy to read without raw markdown stars or hashtags unless writing code."
# ==========================================

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None
app = FastAPI()

conn = sqlite3.connect("chat_memory.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
cursor.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, text TEXT)")
conn.commit()

HTML_UI = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Smart AI - Gold Edition</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: radial-gradient(circle at top, #211c11 0%, #040405 100%);
            color: #fff; height: 100vh; overflow: hidden; display: flex;
        }
        .bubbles { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; }
        .bubble { position: absolute; bottom: -110px; border-radius: 50%; animation: floatUp linear infinite; }
        .bubble:nth-child(1) {
            width: 75px; height: 75px; left: 12%; animation-duration: 13s;
            background: radial-gradient(circle at 30% 30%, rgba(255, 215, 0, 0.45), rgba(212, 175, 55, 0.08));
            border: 1px solid rgba(255, 215, 0, 0.4); box-shadow: 0 0 18px rgba(255, 215, 0, 0.25);
        }
        .bubble:nth-child(2) {
            width: 48px; height: 48px; left: 32%; animation-duration: 10s; animation-delay: 2s;
            background: radial-gradient(circle at 30% 30%, rgba(0, 229, 255, 0.4), rgba(0, 150, 255, 0.08));
            border: 1px solid rgba(0, 229, 255, 0.35); box-shadow: 0 0 16px rgba(0, 229, 255, 0.2);
        }
        .bubble:nth-child(3) {
            width: 95px; height: 95px; left: 55%; animation-duration: 17s; animation-delay: 1s;
            background: radial-gradient(circle at 30% 30%, rgba(255, 180, 0, 0.4), rgba(255, 100, 0, 0.08));
            border: 1px solid rgba(255, 180, 0, 0.35); box-shadow: 0 0 20px rgba(255, 165, 0, 0.2);
        }
        .bubble:nth-child(4) {
            width: 60px; height: 60px; left: 76%; animation-duration: 12s; animation-delay: 4s;
            background: radial-gradient(circle at 30% 30%, rgba(186, 85, 211, 0.4), rgba(138, 43, 226, 0.08));
            border: 1px solid rgba(186, 85, 211, 0.35); box-shadow: 0 0 16px rgba(186, 85, 211, 0.2);
        }
        .bubble:nth-child(5) {
            width: 82px; height: 82px; left: 89%; animation-duration: 15s; animation-delay: 3s;
            background: radial-gradient(circle at 30% 30%, rgba(0, 255, 170, 0.38), rgba(0, 180, 120, 0.08));
            border: 1px solid rgba(0, 255, 170, 0.35); box-shadow: 0 0 18px rgba(0, 255, 170, 0.2);
        }
        @keyframes floatUp {
            0% { transform: translateY(0) scale(1); opacity: 0; }
            20% { opacity: 0.95; }
            80% { opacity: 0.75; }
            100% { transform: translateY(-115vh) scale(1.18); opacity: 0; }
        }
        .sidebar {
            position: relative; z-index: 2; width: 260px; height: 100vh;
            background: rgba(10, 10, 12, 0.9); backdrop-filter: blur(16px);
            border-right: 1px solid rgba(255, 215, 0, 0.22);
            display: flex; flex-direction: column; padding: 15px;
        }
        .new-chat-btn {
            width: 100%; padding: 12px; border-radius: 10px;
            background: linear-gradient(135deg, #ffd700, #c59b27);
            color: #000; font-weight: bold; font-size: 14px;
            border: none; cursor: pointer; margin-bottom: 15px; transition: 0.2s;
        }
        .new-chat-btn:hover { opacity: 0.9; }
        .history-label { font-size: 11px; color: #aaa; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; padding-left: 5px; }
        #session-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
        #session-list::-webkit-scrollbar { width: 4px; }
        #session-list::-webkit-scrollbar-thumb { background: rgba(255, 215, 0, 0.35); border-radius: 10px; }
        .session-item {
            display: flex; justify-content: space-between; align-items: center;
            padding: 10px 12px; border-radius: 8px; cursor: pointer;
            background: rgba(255, 255, 255, 0.03); border: 1px solid transparent;
            font-size: 13.5px; color: #ddd; transition: 0.2s;
        }
        .session-item:hover { background: rgba(255, 215, 0, 0.08); }
        .session-item.active { background: rgba(255, 215, 0, 0.16); border-color: rgba(255, 215, 0, 0.45); color: #ffd700; font-weight: 600; }
        .session-title { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 185px; }
        .del-btn { color: #ff6666; font-size: 14px; padding: 2px 6px; border-radius: 4px; }
        .del-btn:hover { background: rgba(255, 77, 77, 0.2); }

        .main { position: relative; z-index: 1; flex: 1; display: flex; justify-content: center; align-items: center; padding: 20px; }
        .container {
            width: 100%; max-width: 780px; height: 92vh;
            background: rgba(14, 14, 16, 0.8); backdrop-filter: blur(14px);
            border: 1px solid rgba(255, 215, 0, 0.28); border-radius: 16px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.92), inset 0 1px 0 rgba(255,215,0,0.22);
            display: flex; flex-direction: column; padding: 20px;
        }
        .header {
            display: flex; justify-content: space-between; align-items: center;
            padding-bottom: 12px; border-bottom: 1px solid rgba(255,215,0,0.2); margin-bottom: 14px;
        }
        .header h2 {
            font-size: 21px; background: linear-gradient(90deg, #ffd700, #f5b041);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .badge { font-size: 11px; background: rgba(255, 215, 0, 0.12); color: #ffd700; padding: 4px 10px; border-radius: 20px; border: 1px solid rgba(255, 215, 0, 0.35); }
        #chat { flex: 1; overflow-y: auto; padding-right: 6px; display: flex; flex-direction: column; gap: 12px; }
        #chat::-webkit-scrollbar { width: 5px; }
        #chat::-webkit-scrollbar-thumb { background: rgba(255, 215, 0, 0.35); border-radius: 10px; }
        .msg { padding: 11px 15px; border-radius: 12px; line-height: 1.5; white-space: pre-wrap; max-width: 85%; font-size: 15px; }
        .user { align-self: flex-end; background: linear-gradient(135deg, #d4af37, #aa7c11); color: #000; font-weight: 600; border: 1px solid rgba(255, 235, 130, 0.45); }
        .model { align-self: flex-start; background: rgba(28, 28, 32, 0.94); color: #f2f2f2; border: 1px solid rgba(255, 215, 0, 0.18); }
        .controls { display: flex; gap: 10px; margin-top: 14px; }
        input {
            flex: 1; padding: 13px 15px; border-radius: 10px;
            border: 1px solid rgba(255, 215, 0, 0.28); background: rgba(10, 10, 12, 0.92);
            color: #fff; font-size: 15px; outline: none;
        }
        input:focus { border-color: #ffd700; }
        .send-btn {
            padding: 13px 22px; border: none; border-radius: 10px;
            background: linear-gradient(135deg, #ffd700, #c59b27);
            color: #000; font-weight: bold; cursor: pointer;
        }
    </style>
</head>
<body>
    <div class="bubbles">
        <div class="bubble"></div><div class="bubble"></div><div class="bubble"></div>
        <div class="bubble"></div><div class="bubble"></div>
    </div>

    <div class="sidebar">
        <button class="new-chat-btn" onclick="newChat()">+ New Chat</button>
        <div class="history-label">Saved Chats</div>
        <div id="session-list"></div>
    </div>

    <div class="main">
        <div class="container">
            <div class="header">
                <h2>Smart AI</h2>
                <span class="badge">Turbo Mode</span>
            </div>
            <div id="chat"></div>
            <div class="controls">
                <input type="text" id="prompt" placeholder="Kuch bhi pucho..." onkeypress="if(event.key==='Enter') send()">
                <button class="send-btn" onclick="send()">Send</button>
            </div>
        </div>
    </div>

    <script>
        let currentSessionId = null;

        function playBubblePop() {
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(320, ctx.currentTime);
                osc.frequency.exponentialRampToValueAtTime(980, ctx.currentTime + 0.08);
                gain.gain.setValueAtTime(0.35, ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.11);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.11);
            } catch (e) {}
        }

        window.onload = async () => {
            await loadSessions(true);
        };

        async function loadSessions(loadMsgs = false) {
            const res = await fetch('/sessions');
            const sessions = await res.json();
            const list = document.getElementById('session-list');
            list.innerHTML = '';

            if (sessions.length === 0) {
                await newChat();
                return;
            }

            if (!currentSessionId || !sessions.find(s => s.id === currentSessionId)) {
                currentSessionId = sessions[0].id;
            }

            sessions.forEach(s => {
                const div = document.createElement('div');
                div.className = 'session-item' + (s.id === currentSessionId ? ' active' : '');
                div.innerHTML = `
                    <span class="session-title" onclick="selectSession('${s.id}')">${s.title}</span>
                    <span class="del-btn" onclick="deleteSession(event, '${s.id}')">✕</span>
                `;
                list.appendChild(div);
            });

            if (loadMsgs) {
                await loadMessages(currentSessionId);
            }
        }

        async function newChat() {
            const res = await fetch('/sessions/new', { method: 'POST' });
            const data = await res.json();
            currentSessionId = data.id;
            await loadSessions(true);
        }

        async function selectSession(id) {
            currentSessionId = id;
            await loadSessions(true);
        }

        async function deleteSession(e, id) {
            e.stopPropagation();
            await fetch('/sessions/' + id, { method: 'DELETE' });
            if (currentSessionId === id) currentSessionId = null;
            await loadSessions(true);
        }

        async function loadMessages(id) {
            const res = await fetch('/history/' + id);
            const msgs = await res.json();
            const chat = document.getElementById('chat');
            chat.innerHTML = '';
            msgs.forEach(m => {
                chat.innerHTML += `<div class="msg ${m.role}">${m.text}</div>`;
            });
            chat.scrollTop = chat.scrollHeight;
        }

        async function send() {
            const input = document.getElementById('prompt');
            const chat = document.getElementById('chat');
            const text = input.value.trim();
            if (!text || !currentSessionId) return;

            playBubblePop();

            chat.innerHTML += `<div class="msg user">${text}</div>`;
            input.value = '';
            chat.scrollTop = chat.scrollHeight;

            const aiDiv = document.createElement('div');
            aiDiv.className = 'msg model';
            aiDiv.innerText = '...';
            chat.appendChild(aiDiv);

            try {
                const response = await fetch('/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: currentSessionId, message: text })
                });

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let started = false;

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    if (!started) { aiDiv.innerText = ''; started = true; }
                    aiDiv.innerText += decoder.decode(value, { stream: true });
                    chat.scrollTop = chat.scrollHeight;
                }
                await loadSessions(false);
            } catch (err) {
                aiDiv.innerText = 'Connection check karo ya page refresh karo.';
            }
        }
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_UI

@app.get("/sessions")
async def get_sessions():
    cursor.execute("SELECT id, title FROM sessions ORDER BY created_at DESC")
    return [{"id": r[0], "title": r[1]} for r in cursor.fetchall()]

@app.post("/sessions/new")
async def create_session():
    sid = str(uuid.uuid4())
    cursor.execute("INSERT INTO sessions (id, title) VALUES (?, ?)", (sid, "New Chat"))
    conn.commit()
    return {"id": sid}

@app.delete("/sessions/{sid}")
async def delete_session(sid: str):
    cursor.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
    cursor.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    conn.commit()
    return {"status": "deleted"}

@app.get("/history/{sid}")
async def get_history(sid: str):
    cursor.execute("SELECT role, text FROM messages WHERE session_id = ? ORDER BY id ASC", (sid,))
    return [{"role": r[0], "text": r[1]} for r in cursor.fetchall()]

@app.post("/chat")
async def chat_stream(request: Request):
    data = await request.json()
    sid = data.get("session_id", "")
    user_msg = data.get("message", "").strip()

    if not user_msg or not sid:
        return StreamingResponse(iter([""]), media_type="text/plain")

    if not client:
        return StreamingResponse(iter(["API Key missing hai! .env file check karo."]), media_type="text/plain")

    cursor.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,))
    if cursor.fetchone()[0] == 0:
        short_title = user_msg[:26] + ("..." if len(user_msg) > 26 else "")
        cursor.execute("UPDATE sessions SET title = ? WHERE id = ?", (short_title, sid))

    cursor.execute("INSERT INTO messages (session_id, role, text) VALUES (?, ?, ?)", (sid, "user", user_msg))
    conn.commit()

    cursor.execute("SELECT role, text FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 10", (sid,))
    past_rows = cursor.fetchall()[::-1]

    clean_rows = []
    for role, text in past_rows:
        if not text.strip():
            continue
        if clean_rows and clean_rows[-1][0] == role:
            clean_rows[-1] = (role, clean_rows[-1][1] + "\n" + text)
        else:
            clean_rows.append((role, text))

    contents = [types.Content(role=r[0], parts=[types.Part.from_text(text=r[1])]) for r in clean_rows]

    def generate():
        full_reply = ""
        last_err = ""
        for m in [PRIMARY_MODEL, BACKUP_MODEL]:
            try:
                response = client.models.generate_content_stream(
                    model=m,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=AI_PERSONALITY,
                        thinking_config=types.ThinkingConfig(thinking_budget=0) if "lite" not in m else None                    )
                )
                for chunk in response:
                    if chunk.text:
                        full_reply += chunk.text
                        yield chunk.text
                if full_reply.strip():
                    cursor.execute("INSERT INTO messages (session_id, role, text) VALUES (?, ?, ?)", (sid, "model", full_reply))
                    conn.commit()
                return
            except Exception as e:
                last_err = str(e)
                print("AI ERROR:", last_err)
                continue
        yield f"[Error: {last_err}]"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )