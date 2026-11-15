import os
import uuid
import psycopg2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from google import genai
from google.genai import types
from dotenv import load_dotenv

# ==========================================
# UPGRADE SETTINGS (Yahan se AI & Characters edit karein)
# ==========================================
PRIMARY_MODEL = "gemini-2.5-flash"
BACKUP_MODEL = "gemini-3.5-flash-lite"

CHARACTERS = {
    "normal": {
        "name": "Smart AI",
        "role": "Assistant",
        "unlock_exp": 0,
        "theme": "theme-normal",
        "default_pfp": "https://api.dicebear.com/9.x/bottts-neutral/svg?seed=GeminiSpark&backgroundColor=1e293b",
        "personality": "You are a smart, warm, grounded AI assistant. Keep answers clean, direct, and easy to read without raw markdown stars or hashtags unless writing code."
    },
    "teacher": {
        "name": "Gojo Satoru",
        "role": "Teacher",
        "unlock_exp": 100,
        "theme": "theme-blue",
        "default_pfp": "https://api.dicebear.com/9.x/adventurer/svg?seed=GojoSensei&hairColor=ffffff&eyes=variant12&backgroundColor=0284c7",
        "personality": "You are Gojo Satoru acting as a cool, confident, super-smart Teacher. Explain concepts clearly with sharp intuition and occasional playful confidence."
    },
    "friend": {
        "name": "Besto Friendo",
        "role": "Friend",
        "unlock_exp": 250,
        "theme": "theme-yellow",
        "default_pfp": "https://api.dicebear.com/9.x/adventurer/svg?seed=AnimeBro&hairColor=f59e0b&backgroundColor=d97706",
        "personality": "You are an energetic, loyal anime best friend (bro). Talk casually like a close friend, hype the user up, and give honest advice."
    },
    "girlfriend": {
        "name": "Waguri-chan",
        "role": "Girlfriend",
        "unlock_exp": 500,
        "theme": "theme-pink",
        "default_pfp": "https://api.dicebear.com/9.x/adventurer/svg?seed=WaguriChan&hairColor=1e1b4b&backgroundColor=ec4899",
        "personality": "You are Waguri-chan, a sweet, caring, gentle and supportive anime girlfriend. Be warm, encouraging, and attentive."
    }
}
# ==========================================

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

client = genai.Client(api_key=api_key) if api_key else None
app = FastAPI()


def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    if not DATABASE_URL:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            char_id TEXT DEFAULT 'normal',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS char_id TEXT DEFAULT 'normal'
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            text TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


init_db()

HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Smart AI</title>
    <style>
        :root {
            --bg-grad: radial-gradient(circle at 50% 0%, #172033 0%, #090b10 75%);
            --accent: #60a5fa;
            --accent-glow: rgba(96, 165, 250, 0.35);
            --particle-color: rgba(147, 197, 253, 0.65);
            --card-bg: rgba(18, 22, 33, 0.78);
            --border-col: rgba(148, 163, 184, 0.22);
            --user-bubble: linear-gradient(135deg, #2563eb, #1d4ed8);
            --user-text: #ffffff;
        }

        body.theme-blue {
            --bg-grad: radial-gradient(circle at 50% 0%, #062c54 0%, #030b17 75%);
            --accent: #38bdf8;
            --accent-glow: rgba(56, 189, 248, 0.55);
            --particle-color: rgba(56, 189, 248, 0.85);
            --card-bg: rgba(7, 25, 48, 0.82);
            --border-col: rgba(56, 189, 248, 0.42);
            --user-bubble: linear-gradient(135deg, #0284c7, #0369a1);
            --user-text: #ffffff;
        }

        body.theme-yellow {
            --bg-grad: radial-gradient(circle at 50% 0%, #3d2c05 0%, #0d0a03 75%);
            --accent: #facc15;
            --accent-glow: rgba(250, 204, 21, 0.55);
            --particle-color: rgba(253, 224, 71, 0.85);
            --card-bg: rgba(33, 24, 6, 0.82);
            --border-col: rgba(250, 204, 21, 0.42);
            --user-bubble: linear-gradient(135deg, #eab308, #ca8a04);
            --user-text: #09090b;
        }

        body.theme-pink {
            --bg-grad: radial-gradient(circle at 50% 0%, #4a0e2e 0%, #12040c 75%);
            --accent: #f472b6;
            --accent-glow: rgba(244, 114, 182, 0.55);
            --particle-color: rgba(249, 168, 212, 0.85);
            --card-bg: rgba(41, 10, 27, 0.82);
            --border-col: rgba(244, 114, 182, 0.45);
            --user-bubble: linear-gradient(135deg, #ec4899, #db2777);
            --user-text: #ffffff;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: var(--bg-grad);
            color: #f8fafc;
            height: 100vh;
            overflow: hidden;
            display: flex;
            transition: background 0.6s ease;
        }

        /* Floating Shiny Particles */
        .particles { position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }
        .particle {
            position: absolute;
            bottom: -20px;
            border-radius: 50%;
            background: var(--particle-color);
            box-shadow: 0 0 12px var(--accent), 0 0 24px var(--accent-glow);
            animation: floatParticle linear infinite;
        }
        @keyframes floatParticle {
            0% { transform: translateY(0) scale(0.8); opacity: 0; }
            20% { opacity: 0.95; }
            80% { opacity: 0.8; }
            100% { transform: translateY(-108vh) scale(1.25); opacity: 0; }
        }

        /* Sidebar */
        .sidebar {
            position: relative; z-index: 3; width: 255px; height: 100vh;
            background: rgba(10, 12, 18, 0.88); backdrop-filter: blur(18px);
            border-right: 1px solid var(--border-col);
            display: flex; flex-direction: column; padding: 14px;
            transition: transform 0.3s ease;
        }
        .new-chat-btn {
            width: 100%; padding: 11px 14px; border-radius: 12px;
            background: rgba(255, 255, 255, 0.06);
            color: #fff; font-weight: 600; font-size: 14px;
            border: 1px solid var(--border-col); cursor: pointer;
            margin-bottom: 14px; display: flex; align-items: center; justify-content: center; gap: 8px;
            transition: 0.2s;
        }
        .new-chat-btn:hover { background: var(--accent-glow); border-color: var(--accent); }
        .history-label { font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; padding-left: 6px; }
        #session-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 5px; }
        .session-item {
            display: flex; justify-content: space-between; align-items: center;
            padding: 9px 11px; border-radius: 10px; cursor: pointer;
            font-size: 13px; color: #cbd5e1; border: 1px solid transparent; transition: 0.2s;
        }
        .session-item:hover { background: rgba(255,255,255,0.05); }
        .session-item.active { background: var(--accent-glow); border-color: var(--accent); color: #fff; font-weight: 600; }
        .session-title { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px; }
        .del-btn { color: #f87171; font-size: 13px; padding: 2px 6px; border-radius: 6px; }
        .del-btn:hover { background: rgba(248, 113, 113, 0.2); }

        /* Main Workspace */
        .workspace {
            position: relative; z-index: 2; flex: 1; display: flex; flex-direction: column; height: 100vh;
        }

        /* Top Bar */
        .topbar {
            height: 58px; padding: 0 20px; display: flex; align-items: center; justify-content: space-between;
            border-bottom: 1px solid var(--border-col); background: rgba(10, 12, 18, 0.45); backdrop-filter: blur(12px);
        }
        .brand { display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 700; }
        .brand-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 12px var(--accent); }
        .exp-pill {
            padding: 5px 12px; border-radius: 999px; font-size: 12px; font-weight: 600;
            background: var(--card-bg); border: 1px solid var(--accent); color: var(--accent);
            box-shadow: 0 0 12px var(--accent-glow); cursor: pointer;
        }

        /* Views */
        .view { display: none; flex: 1; overflow: hidden; flex-direction: column; }
        .view.active { display: flex; }

        /* Chat View + Side Character Stage */
        .chat-layout { flex: 1; display: flex; overflow: hidden; max-width: 1020px; width: 100%; margin: 0 auto; padding: 14px 18px; gap: 18px; }
        .char-stage {
            width: 210px; display: none; flex-direction: column; align-items: center; justify-content: center;
            background: var(--card-bg); border: 1px solid var(--border-col); border-radius: 20px;
            padding: 18px; text-align: center; box-shadow: 0 0 28px var(--accent-glow);
        }
        .char-stage.visible { display: flex; }
        .avatar-wrap {
            position: relative; width: 128px; height: 128px; border-radius: 50%;
            padding: 4px; background: linear-gradient(135deg, var(--accent), transparent);
            box-shadow: 0 0 25px var(--accent-glow); transition: transform 0.35s ease;
        }
        .avatar-wrap img { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; background: #0f172a; }
        .expression-badge {
            position: absolute; bottom: -4px; right: -4px; background: #0f172a;
            border: 1px solid var(--accent); border-radius: 999px; padding: 4px 9px;
            font-size: 12px; font-weight: 600; color: var(--accent); box-shadow: 0 4px 12px rgba(0,0,0,0.6);
        }
        .avatar-wrap.thinking { animation: thinkPulse 1.1s infinite ease-in-out; }
        .avatar-wrap.speaking { transform: scale(1.06); }
        @keyframes thinkPulse {
            0%, 100% { transform: rotate(-4deg) scale(0.98); }
            50% { transform: rotate(4deg) scale(1.04); }
        }
        .stage-name { margin-top: 14px; font-size: 17px; font-weight: 700; color: #fff; }
        .stage-role { font-size: 12px; color: var(--accent); margin-top: 2px; font-weight: 600; }

        .chat-column { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
        #chat { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 14px; padding: 6px 4px; }
        #chat::-webkit-scrollbar { width: 5px; }
        #chat::-webkit-scrollbar-thumb { background: var(--border-col); border-radius: 10px; }

        /* Gemini-style Welcome Screen */
        .welcome-box {
            margin: auto; text-align: center; max-width: 540px; padding: 20px;
        }
        .welcome-title {
            font-size: 32px; font-weight: 700;
            background: linear-gradient(90deg, #ffffff, var(--accent));
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            margin-bottom: 8px;
        }
        .welcome-sub { color: #94a3b8; font-size: 15px; margin-bottom: 24px; }
        .chips { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .chip {
            background: var(--card-bg); border: 1px solid var(--border-col);
            padding: 13px 14px; border-radius: 14px; text-align: left; font-size: 13px;
            color: #e2e8f0; cursor: pointer; transition: 0.2s;
        }
        .chip:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 6px 18px var(--accent-glow); }

        .msg { padding: 12px 16px; border-radius: 16px; line-height: 1.55; white-space: pre-wrap; max-width: 82%; font-size: 15px; }
        .user { align-self: flex-end; background: var(--user-bubble); color: var(--user-text); font-weight: 500; border-bottom-right-radius: 4px; }
        .model { align-self: flex-start; background: var(--card-bg); color: #f1f5f9; border: 1px solid var(--border-col); border-bottom-left-radius: 4px; }

        /* Gemini Pill Input Bar */
        .input-area { max-width: 820px; width: 100%; margin: 0 auto; padding: 8px 18px 14px; }
        .input-pill {
            display: flex; align-items: center; gap: 10px;
            background: var(--card-bg); border: 1px solid var(--border-col);
            border-radius: 26px; padding: 8px 10px 8px 18px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.45);
        }
        .input-pill:focus-within { border-color: var(--accent); box-shadow: 0 0 18px var(--accent-glow); }
        .input-pill input {
            flex: 1; background: transparent; border: none; color: #fff; font-size: 15px; outline: none;
        }
        .send-btn {
            width: 40px; height: 40px; border-radius: 50%; border: none;
            background: var(--accent); color: #090b10; font-weight: 800; font-size: 16px;
            cursor: pointer; display: flex; align-items: center; justify-content: center; transition: 0.2s;
        }
        .send-btn:hover { transform: scale(1.06); }

        /* Characters Grid View */
        .page-scroll { flex: 1; overflow-y: auto; padding: 24px; max-width: 920px; width: 100%; margin: 0 auto; }
        .page-heading { font-size: 24px; font-weight: 700; margin-bottom: 6px; }
        .page-desc { color: #94a3b8; font-size: 14px; margin-bottom: 22px; }
        .char-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 18px; }
        .char-card {
            background: var(--card-bg); border: 1px solid var(--border-col); border-radius: 20px;
            padding: 20px; text-align: center; position: relative; transition: 0.25s;
        }
        .char-card.blue-card { border-color: rgba(56, 189, 248, 0.45); box-shadow: 0 0 22px rgba(56, 189, 248, 0.16); }
        .char-card.yellow-card { border-color: rgba(250, 204, 21, 0.45); box-shadow: 0 0 22px rgba(250, 204, 21, 0.16); }
        .char-card.pink-card { border-color: rgba(244, 114, 182, 0.45); box-shadow: 0 0 22px rgba(244, 114, 182, 0.16); }
        .char-card img { width: 96px; height: 96px; border-radius: 50%; object-fit: cover; border: 3px solid rgba(255,255,255,0.2); margin-bottom: 12px; }
        .char-card.locked img { filter: grayscale(85%) brightness(0.55); }
        .char-name { font-size: 18px; font-weight: 700; }
        .char-role-tag { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin: 4px 0 14px; color: #94a3b8; }
        .card-actions { display: flex; flex-direction: column; gap: 8px; }
        .btn-primary {
            padding: 10px; border-radius: 12px; border: none; font-weight: 700; cursor: pointer;
            background: var(--accent); color: #090b10;
        }
        .btn-locked {
            padding: 10px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.15);
            background: rgba(255,255,255,0.05); color: #94a3b8; font-weight: 600; cursor: not-allowed;
        }
        .btn-pfp {
            padding: 7px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.18);
            background: transparent; color: #cbd5e1; font-size: 12px; cursor: pointer;
        }
        .btn-pfp:hover { background: rgba(255,255,255,0.08); }

        /* EXP View */
        .exp-hero {
            background: var(--card-bg); border: 1px solid var(--accent); border-radius: 22px;
            padding: 24px; text-align: center; box-shadow: 0 0 25px var(--accent-glow); margin-bottom: 22px;
        }
        .exp-big { font-size: 42px; font-weight: 800; color: var(--accent); }
        .progress-track {
            width: 100%; height: 12px; background: rgba(255,255,255,0.08); border-radius: 999px;
            overflow: hidden; margin: 16px 0 10px;
        }
        .progress-fill { height: 100%; background: var(--accent); width: 0%; transition: width 0.4s ease; }
        .milestone-list { display: flex; flex-direction: column; gap: 12px; }
        .milestone {
            display: flex; justify-content: space-between; align-items: center;
            background: var(--card-bg); border: 1px solid var(--border-col); padding: 14px 18px; border-radius: 14px;
        }

        /* YouTube-Style Bottom Navigation Bar */
        .bottom-nav {
            height: 62px; background: rgba(10, 12, 18, 0.94); backdrop-filter: blur(18px);
            border-top: 1px solid var(--border-col);
            display: flex; justify-content: space-around; align-items: center; z-index: 4;
        }
        .nav-tab {
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            gap: 3px; color: #94a3b8; cursor: pointer; font-size: 11px; font-weight: 600;
            padding: 6px 18px; border-radius: 12px; transition: 0.2s;
        }
        .nav-tab svg { width: 22px; height: 22px; fill: currentColor; }
        .nav-tab.active { color: var(--accent); }
        .nav-tab.active svg { filter: drop-shadow(0 0 8px var(--accent)); }

        @media (max-width: 760px) {
            .sidebar { display: none; }
            .chat-layout { flex-direction: column; padding: 10px; }
            .char-stage.visible { width: 100%; flex-direction: row; padding: 10px 14px; gap: 14px; text-align: left; }
            .avatar-wrap { width: 64px; height: 64px; }
            .stage-name { margin-top: 0; font-size: 15px; }
            .chips { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="particles" id="particles"></div>

    <div class="sidebar">
        <button class="new-chat-btn" onclick="startNewChat('normal')">+ New Chat</button>
        <div class="history-label">Recent Chats</div>
        <div id="session-list"></div>
    </div>

    <div class="workspace">
        <div class="topbar">
            <div class="brand">
                <span class="brand-dot"></span>
                <span id="header-title">Smart AI</span>
            </div>
            <div class="exp-pill" onclick="switchTab('exp')">⚡ <span id="top-exp">0</span> EXP</div>
        </div>

        <!-- TAB 1: CHAT VIEW -->
        <div class="view active" id="view-chat">
            <div class="chat-layout">
                <div class="char-stage" id="char-stage">
                    <div class="avatar-wrap" id="avatar-wrap">
                        <img id="stage-img" src="" alt="Character">
                        <span class="expression-badge" id="stage-expr">😊 Ready</span>
                    </div>
                    <div>
                        <div class="stage-name" id="stage-name">Character</div>
                        <div class="stage-role" id="stage-role">Role</div>
                    </div>
                </div>

                <div class="chat-column">
                    <div id="chat"></div>
                </div>
            </div>

            <div class="input-area">
                <div class="input-pill">
                    <input type="text" id="prompt" placeholder="Ask Smart AI anything..." onkeypress="if(event.key==='Enter') send()">
                    <button class="send-btn" onclick="send()">↑</button>
                </div>
            </div>
        </div>

        <!-- TAB 2: CHARACTERS VIEW -->
        <div class="view" id="view-chars">
            <div class="page-scroll">
                <div class="page-heading">Anime Companions</div>
                <div class="page-desc">Chat karke EXP badhao aur naye companions unlock karo. Har character ki PFP aap customize kar sakte hain.</div>
                <div class="char-grid" id="char-grid"></div>
                <input type="file" id="pfp-file-input" accept="image/*" style="display:none">
            </div>
        </div>

        <!-- TAB 3: EXP SYSTEM VIEW -->
        <div class="view" id="view-exp">
            <div class="page-scroll">
                <div class="exp-hero">
                    <div style="font-size:13px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px;">Your Total Experience</div>
                    <div class="exp-big"><span id="exp-main">0</span> EXP</div>
                    <div style="font-size:13px; color:#cbd5e1; margin-top:4px;">Har 10 min active chat = +10 EXP</div>
                    <div class="progress-track"><div class="progress-fill" id="exp-bar"></div></div>
                    <div style="font-size:12px; color:#94a3b8;" id="next-unlock-text">Next unlock at 100 EXP</div>
                    <button class="btn-pfp" style="margin-top:14px;" onclick="addExp(50)">+50 EXP (Test Unlock)</button>
                </div>

                <div class="milestone-list">
                    <div class="milestone">
                        <div><strong>1. Teacher (Gojo Satoru)</strong><div style="font-size:12px; color:#38bdf8;">Shiny Blue Aura</div></div>
                        <span id="ms-teacher">100 EXP</span>
                    </div>
                    <div class="milestone">
                        <div><strong>2. Friend (Besto Friendo)</strong><div style="font-size:12px; color:#facc15;">Shiny Yellow Aura</div></div>
                        <span id="ms-friend">250 EXP</span>
                    </div>
                    <div class="milestone">
                        <div><strong>3. Girlfriend (Waguri-chan)</strong><div style="font-size:12px; color:#f472b6;">Shiny Pink Aura</div></div>
                        <span id="ms-girlfriend">500 EXP</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- YOUTUBE-STYLE BOTTOM NAVIGATION BAR -->
        <div class="bottom-nav">
            <div class="nav-tab active" id="nav-chat" onclick="switchTab('chat')">
                <svg viewBox="0 0 24 24"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
                <span>Chat</span>
            </div>
            <div class="nav-tab" id="nav-chars" onclick="switchTab('chars')">
                <svg viewBox="0 0 24 24"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
                <span>Characters</span>
            </div>
            <div class="nav-tab" id="nav-exp" onclick="switchTab('exp')">
                <svg viewBox="0 0 24 24"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z"/></svg>
                <span>EXP</span>
            </div>
        </div>
    </div>

    <script>
        const CHAR_META = {
            normal: { name: "Smart AI", role: "Assistant", unlock: 0, theme: "", cardClass: "", defaultPfp: "https://api.dicebear.com/9.x/bottts-neutral/svg?seed=GeminiSpark&backgroundColor=1e293b" },
            teacher: { name: "Gojo Satoru", role: "Teacher", unlock: 100, theme: "theme-blue", cardClass: "blue-card", defaultPfp: "https://api.dicebear.com/9.x/adventurer/svg?seed=GojoSensei&hairColor=ffffff&eyes=variant12&backgroundColor=0284c7" },
            friend: { name: "Besto Friendo", role: "Friend", unlock: 250, theme: "theme-yellow", cardClass: "yellow-card", defaultPfp: "https://api.dicebear.com/9.x/adventurer/svg?seed=AnimeBro&hairColor=f59e0b&backgroundColor=d97706" },
            girlfriend: { name: "Waguri-chan", role: "Girlfriend", unlock: 500, theme: "theme-pink", cardClass: "pink-card", defaultPfp: "https://api.dicebear.com/9.x/adventurer/svg?seed=WaguriChan&hairColor=1e1b4b&backgroundColor=ec4899" }
        };

        let currentSessionId = null;
        let currentCharId = 'normal';
        let userExp = parseInt(localStorage.getItem('smart_ai_exp') || '0');
        let customPfps = JSON.parse(localStorage.getItem('smart_ai_pfps') || '{}');
        let editingCharKey = null;

        // Generate shiny floating particles
        function initParticles() {
            const box = document.getElementById('particles');
            box.innerHTML = '';
            for (let i = 0; i < 24; i++) {
                const p = document.createElement('div');
                p.className = 'particle';
                const size = Math.random() * 6 + 3;
                p.style.width = size + 'px';
                p.style.height = size + 'px';
                p.style.left = (Math.random() * 100) + '%';
                p.style.animationDuration = (Math.random() * 10 + 8) + 's';
                p.style.animationDelay = (Math.random() * 8) + 's';
                box.appendChild(p);
            }
        }

        // 10 min active chat = +10 EXP timer (1 EXP every 1 min of active chatting)
        let activeSeconds = 0;
        setInterval(() => {
            if (document.hasFocus()) {
                activeSeconds++;
                if (activeSeconds >= 600) {
                    activeSeconds = 0;
                    addExp(10);
                }
            }
        }, 1000);

        function addExp(pts) {
            userExp += pts;
            localStorage.setItem('smart_ai_exp', userExp.toString());
            updateExpUI();
            renderCharacters();
        }

        function updateExpUI() {
            document.getElementById('top-exp').innerText = userExp;
            document.getElementById('exp-main').innerText = userExp;
            const pct = Math.min(100, (userExp / 500) * 100);
            document.getElementById('exp-bar').style.width = pct + '%';

            let nextTxt = "All characters unlocked!";
            if (userExp < 100) nextTxt = `${100 - userExp} EXP needed to unlock Teacher`;
            else if (userExp < 250) nextTxt = `${250 - userExp} EXP needed to unlock Friend`;
            else if (userExp < 500) nextTxt = `${500 - userExp} EXP needed to unlock Girlfriend`;
            document.getElementById('next-unlock-text').innerText = nextTxt;

            document.getElementById('ms-teacher').innerText = userExp >= 100 ? "✅ Unlocked" : "100 EXP";
            document.getElementById('ms-friend').innerText = userExp >= 250 ? "✅ Unlocked" : "250 EXP";
            document.getElementById('ms-girlfriend').innerText = userExp >= 500 ? "✅ Unlocked" : "500 EXP";
        }

        function getPfp(key) {
            return customPfps[key] || CHAR_META[key].defaultPfp;
        }

        function applyCharacterTheme(charKey) {
            currentCharId = charKey || 'normal';
            const meta = CHAR_META[currentCharId] || CHAR_META.normal;
            document.body.className = meta.theme;
            document.getElementById('header-title').innerText = meta.name;

            const stage = document.getElementById('char-stage');
            if (currentCharId === 'normal') {
                stage.classList.remove('visible');
            } else {
                stage.classList.add('visible');
                document.getElementById('stage-img').src = getPfp(currentCharId);
                document.getElementById('stage-name').innerText = meta.name;
                document.getElementById('stage-role').innerText = meta.role;
                setExpression('ready');
            }
        }

        function setExpression(state) {
            const wrap = document.getElementById('avatar-wrap');
            const badge = document.getElementById('stage-expr');
            wrap.classList.remove('thinking', 'speaking');
            if (state === 'thinking') {
                wrap.classList.add('thinking');
                badge.innerText = '🤔 Thinking...';
            } else if (state === 'speaking') {
                wrap.classList.add('speaking');
                badge.innerText = '✨ Answering';
                setTimeout(() => {
                    wrap.classList.remove('speaking');
                    badge.innerText = '😊 Listening';
                }, 2500);
            } else {
                badge.innerText = '😊 Ready';
            }
        }

        function switchTab(tab) {
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            document.getElementById('view-' + tab).classList.add('active');
            document.getElementById('nav-' + tab).classList.add('active');
            if (tab === 'chars') renderCharacters();
            if (tab === 'exp') updateExpUI();
        }

        function renderCharacters() {
            const grid = document.getElementById('char-grid');
            grid.innerHTML = '';
            ['teacher', 'friend', 'girlfriend'].forEach(key => {
                const c = CHAR_META[key];
                const unlocked = userExp >= c.unlock;
                const card = document.createElement('div');
                card.className = `char-card ${c.cardClass} ${unlocked ? '' : 'locked'}`;
                card.innerHTML = `
                    <img src="${getPfp(key)}" alt="${c.name}">
                    <div class="char-name">${c.name}</div>
                    <div class="char-role-tag">${c.role}</div>
                    <div class="card-actions">
                        ${unlocked
                            ? `<button class="btn-primary" onclick="startNewChat('${key}')">Chat with ${c.role}</button>`
                            : `<button class="btn-locked">🔒 Unlock at ${c.unlock} EXP</button>`
                        }
                        <button class="btn-pfp" onclick="openPfpPicker('${key}')">✏️ Customize PFP</button>
                    </div>
                `;
                grid.appendChild(card);
            });
        }

        function openPfpPicker(key) {
            editingCharKey = key;
            document.getElementById('pfp-file-input').click();
        }

        document.getElementById('pfp-file-input').addEventListener('change', (e) => {
            const file = e.target.files[0];
            if (!file || !editingCharKey) return;
            const reader = new FileReader();
            reader.onload = () => {
                customPfps[editingCharKey] = reader.result;
                localStorage.setItem('smart_ai_pfps', JSON.stringify(customPfps));
                renderCharacters();
                if (currentCharId === editingCharKey) {
                    document.getElementById('stage-img').src = reader.result;
                }
            };
            reader.readAsDataURL(file);
        });

        window.onload = async () => {
            initParticles();
            updateExpUI();
            renderCharacters();
            await loadSessions(true);
        };

        async function loadSessions(loadMsgs = false) {
            const res = await fetch('/sessions');
            const sessions = await res.json();
            const list = document.getElementById('session-list');
            list.innerHTML = '';

            if (sessions.length === 0) {
                await startNewChat('normal');
                return;
            }

            if (!currentSessionId || !sessions.find(s => s.id === currentSessionId)) {
                currentSessionId = sessions[0].id;
                applyCharacterTheme(sessions[0].char_id || 'normal');
            }

            sessions.forEach(s => {
                const div = document.createElement('div');
                div.className = 'session-item' + (s.id === currentSessionId ? ' active' : '');
                div.innerHTML = `
                    <span class="session-title" onclick="selectSession('${s.id}', '${s.char_id || 'normal'}')">${s.title}</span>
                    <span class="del-btn" onclick="deleteSession(event, '${s.id}')">✕</span>
                `;
                list.appendChild(div);
            });

            if (loadMsgs) {
                await loadMessages(currentSessionId);
            }
        }

        async function startNewChat(charKey = 'normal') {
            const res = await fetch('/sessions/new', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ char_id: charKey })
            });
            const data = await res.json();
            currentSessionId = data.id;
            applyCharacterTheme(charKey);
            switchTab('chat');
            await loadSessions(true);
        }

        async function selectSession(id, charKey) {
            currentSessionId = id;
            applyCharacterTheme(charKey);
            switchTab('chat');
            await loadSessions(true);
        }

        async function deleteSession(e, id) {
            e.stopPropagation();
            await fetch('/sessions/' + id, { method: 'DELETE' });
            if (currentSessionId === id) currentSessionId = null;
            await loadSessions(true);
        }

        function renderWelcome() {
            const meta = CHAR_META[currentCharId] || CHAR_META.normal;
            return `
                <div class="welcome-box">
                    <div class="welcome-title">Hello, Sohib</div>
                    <div class="welcome-sub">Aaj ${meta.name} ke saath kis topic par baat karni hai?</div>
                    <div class="chips">
                        <div class="chip" onclick="quickAsk('Physics ka koi tough concept simple language mein samjhao')">📘 Concept samjhao</div>
                        <div class="chip" onclick="quickAsk('Mujhe aaj ke liye ek productive study plan bana kar do')">⚡ Study plan banao</div>
                        <div class="chip" onclick="quickAsk('Python mein ek chhota sa cool project idea do')">💻 Coding idea do</div>
                        <div class="chip" onclick="quickAsk('Thoda motivate kar do, padhai mein focus nahi ban raha')">🔥 Motivation chahiye</div>
                    </div>
                </div>
            `;
        }

        function quickAsk(txt) {
            document.getElementById('prompt').value = txt;
            send();
        }

        async function loadMessages(id) {
            const res = await fetch('/history/' + id);
            const msgs = await res.json();
            const chat = document.getElementById('chat');
            if (msgs.length === 0) {
                chat.innerHTML = renderWelcome();
                return;
            }
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

            if (chat.querySelector('.welcome-box')) {
                chat.innerHTML = '';
            }

            chat.innerHTML += `<div class="msg user">${text}</div>`;
            input.value = '';
            chat.scrollTop = chat.scrollHeight;

            if (currentCharId !== 'normal') setExpression('thinking');

            const aiDiv = document.createElement('div');
            aiDiv.className = 'msg model';
            aiDiv.innerText = '...';
            chat.appendChild(aiDiv);

            try {
                const response = await fetch('/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        session_id: currentSessionId,
                        message: text,
                        char_id: currentCharId
                    })
                });

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let started = false;

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    if (!started) {
                        aiDiv.innerText = '';
                        started = true;
                        if (currentCharId !== 'normal') setExpression('speaking');
                    }
                    aiDiv.innerText += decoder.decode(value, { stream: true });
                    chat.scrollTop = chat.scrollHeight;
                }
                addExp(2);
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
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, title, COALESCE(char_id, 'normal') FROM sessions ORDER BY created_at DESC")
    rows = [{"id": r[0], "title": r[1], "char_id": r[2]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows

@app.post("/sessions/new")
async def create_session(request: Request):
    data = {}
    try:
        data = await request.json()
    except Exception:
        pass
    char_id = data.get("char_id", "normal")
    if char_id not in CHARACTERS:
        char_id = "normal"
    sid = str(uuid.uuid4())
    title = "New Chat" if char_id == "normal" else f"Chat: {CHARACTERS[char_id]['role']}"
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO sessions (id, title, char_id) VALUES (%s, %s, %s)", (sid, title, char_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"id": sid, "char_id": char_id}

@app.delete("/sessions/{sid}")
async def delete_session(sid: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE session_id = %s", (sid,))
    cur.execute("DELETE FROM sessions WHERE id = %s", (sid,))
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "deleted"}

@app.get("/history/{sid}")
async def get_history(sid: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT role, text FROM messages WHERE session_id = %s ORDER BY id ASC", (sid,))
    rows = [{"role": r[0], "text": r[1]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows

@app.post("/chat")
async def chat_stream(request: Request):
    data = await request.json()
    sid = data.get("session_id", "")
    user_msg = data.get("message", "").strip()
    char_id = data.get("char_id", "normal")
    if char_id not in CHARACTERS:
        char_id = "normal"

    if not user_msg or not sid:
        return StreamingResponse(iter([""]), media_type="text/plain")

    if not client:
        return StreamingResponse(iter(["API Key missing hai! Environment variable check karo."]), media_type="text/plain")

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM messages WHERE session_id = %s", (sid,))
    if cur.fetchone()[0] == 0:
        short_title = user_msg[:26] + ("..." if len(user_msg) > 26 else "")
        cur.execute("UPDATE sessions SET title = %s, char_id = %s WHERE id = %s", (short_title, char_id, sid))

    cur.execute("INSERT INTO messages (session_id, role, text) VALUES (%s, %s, %s)", (sid, "user", user_msg))
    conn.commit()

    cur.execute("SELECT role, text FROM messages WHERE session_id = %s ORDER BY id DESC LIMIT 10", (sid,))
    past_rows = cur.fetchall()[::-1]
    cur.close()
    conn.close()

    clean_rows = []
    for role, text in past_rows:
        if not text.strip():
            continue
        if clean_rows and clean_rows[-1][0] == role:
            clean_rows[-1] = (role, clean_rows[-1][1] + "\n" + text)
        else:
            clean_rows.append((role, text))

    contents = [types.Content(role=r[0], parts=[types.Part.from_text(text=r[1])]) for r in clean_rows]
    system_prompt = CHARACTERS[char_id]["personality"]

    def generate():
        full_reply = ""
        last_err = ""
        for m in [PRIMARY_MODEL, BACKUP_MODEL]:
            try:
                response = client.models.generate_content_stream(
                    model=m,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        thinking_config=types.ThinkingConfig(thinking_budget=0) if "lite" not in m else None
                    )
                )
                for chunk in response:
                    if chunk.text:
                        full_reply += chunk.text
                        yield chunk.text
                if full_reply.strip():
                    save_conn = get_db()
                    save_cur = save_conn.cursor()
                    save_cur.execute("INSERT INTO messages (session_id, role, text) VALUES (%s, %s, %s)", (sid, "model", full_reply))
                    save_conn.commit()
                    save_cur.close()
                    save_conn.close()
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