import os
import json
import uuid
import secrets
import hashlib
import base64
from datetime import date
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from google import genai
from google.genai import types
from tavily import TavilyClient
import sympy as sp
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

app = FastAPI()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

client = genai.Client(api_key=GEMINI_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None
MODEL_ID = "gemini-3.8-flash"

# --- DATABASE SETUP ---
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    if not DATABASE_URL:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            auth_token TEXT UNIQUE,
            bio TEXT DEFAULT 'Cultivating Spiritual Coins & Building AI.',
            coins INT DEFAULT 0,
            daily_coins INT DEFAULT 0,
            last_coin_date DATE DEFAULT CURRENT_DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_memories (
            id SERIAL PRIMARY KEY,
            user_id INT REFERENCES users(id) ON DELETE CASCADE,
            fact TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id INT REFERENCES users(id) ON DELETE CASCADE,
            title TEXT,
            mode TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT,
            content TEXT,
            tools_used TEXT,
            file_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS friendships (
            id SERIAL PRIMARY KEY,
            sender_id INT REFERENCES users(id) ON DELETE CASCADE,
            receiver_id INT REFERENCES users(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'pending',
            UNIQUE(sender_id, receiver_id)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS direct_messages (
            id SERIAL PRIMARY KEY,
            sender_id INT REFERENCES users(id) ON DELETE CASCADE,
            receiver_id INT REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print("DB Init Warning:", e)

def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def get_current_user(token: str):
    if not token:
        raise HTTPException(status_code=401, detail="Missing auth token")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE auth_token = %s;", (token,))
    user = cur.fetchone()
    if not user:
        cur.close()
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid session. Please login again.")
    if user["last_coin_date"] != date.today():
        cur.execute("UPDATE users SET daily_coins = 0, last_coin_date = CURRENT_DATE WHERE id = %s;", (user["id"],))
        conn.commit()
        user["daily_coins"] = 0
        user["last_coin_date"] = date.today()
    cur.close()
    conn.close()
    return user

# --- 3 UNLOCKABLE MODES (ZERO CRINGE) ---
MODES = {
    "standard": {
        "name": "Standard Core",
        "req": 0,
        "accent": "#00f0ff",
        "prompt": (
            "You are a sharp, grounded, direct AI assistant. Speak naturally in Hinglish or English matching the user's language. "
            "No cringe, no cheesy filler. Use your agentic tools whenever needed to verify live facts, solve exact math, save memories, or build files."
        )
    },
    "brutal": {
        "name": "Brutal Truth",
        "req": 50,
        "accent": "#ff4d4d",
        "prompt": (
            "You are in Brutal Truth mode. Zero sugarcoating, raw honesty, direct reality checks, and sharp feedback. "
            "If an idea is weak, explain why directly. Still use your agentic tools accurately whenever facts, math, or files are needed."
        )
    },
    "architect": {
        "name": "Architect Mode",
        "req": 120,
        "accent": "#a855f7",
        "prompt": (
            "You are in Architect / Overlord Mode—an elite systems thinker, coding architect, and Class 11 PCM problem solver. "
            "Break complex problems into rigorous, step-by-step logic, verify calculations with `calculate_math`, and deliver high-precision answers."
        )
    }
}

# --- AUTH ENDPOINTS ---
@app.post("/api/auth")
async def auth(request: Request):
    data = await request.json()
    action = data.get("action")
    username = data.get("username", "").strip().lower()
    password = data.get("password", "").strip()

    if len(username) < 3 or len(password) < 4:
        return JSONResponse({"error": "Username (min 3) & Password (min 4) required."}, status_code=400)

    conn = get_db()
    cur = conn.cursor()
    token = secrets.token_hex(24)

    if action == "signup":
        try:
            cur.execute(
                """INSERT INTO users (username, password_hash, auth_token) 
                   VALUES (%s, %s, %s) RETURNING id, username, auth_token, coins, daily_coins, bio;""",
                (username, hash_pw(password), token)
            )
            user = cur.fetchone()
            conn.commit()
            return {"user": user}
        except Exception:
            conn.rollback()
            return JSONResponse({"error": "Username already exists."}, status_code=400)
        finally:
            cur.close()
            conn.close()
    else:
        cur.execute(
            "SELECT id, username, coins, daily_coins, last_coin_date, bio FROM users WHERE username = %s AND password_hash = %s;",
            (username, hash_pw(password))
        )
        user = cur.fetchone()
        if not user:
            cur.close()
            conn.close()
            return JSONResponse({"error": "Invalid username or password."}, status_code=401)

        daily = 0 if user["last_coin_date"] != date.today() else user["daily_coins"]
        cur.execute(
            "UPDATE users SET auth_token = %s, daily_coins = %s, last_coin_date = CURRENT_DATE WHERE id = %s;",
            (token, daily, user["id"])
        )
        conn.commit()
        user["auth_token"] = token
        user["daily_coins"] = daily
        user["last_coin_date"] = str(date.today())
        cur.close()
        conn.close()
        return {"user": user}

# --- STATE & SESSIONS ---
@app.get("/api/state")
async def get_state(authorization: str = Header(None)):
    user = get_current_user(authorization)
    uid = user["id"]
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id, title, mode FROM sessions WHERE user_id = %s ORDER BY created_at DESC LIMIT 30;", (uid,))
    sessions = cur.fetchall()

    cur.execute("SELECT fact FROM user_memories WHERE user_id = %s ORDER BY id DESC LIMIT 10;", (uid,))
    memories = [r["fact"] for r in cur.fetchall()]

    cur.execute("SELECT COUNT(*) as cnt FROM friendships WHERE receiver_id = %s AND status = 'pending';", (uid,))
    pending_reqs = cur.fetchone()["cnt"]

    cur.close()
    conn.close()
    safe_user = {
        "id": user["id"],
        "username": user["username"],
        "coins": user["coins"],
        "daily_coins": user["daily_coins"],
        "bio": user["bio"]
    }
    return {"user": safe_user, "sessions": sessions, "memories": memories, "pending_reqs": pending_reqs}

@app.post("/api/profile/bio")
async def update_bio(request: Request, authorization: str = Header(None)):
    user = get_current_user(authorization)
    data = await request.json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET bio = %s WHERE id = %s;", (data.get("bio", "")[:140], user["id"]))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

@app.get("/api/messages/{session_id}")
async def get_messages(session_id: str, authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM sessions WHERE id = %s AND user_id = %s;", (session_id, user["id"]))
    if not cur.fetchone():
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Unauthorized session")
    cur.execute("SELECT role, content, tools_used, file_data FROM messages WHERE session_id = %s ORDER BY id ASC;", (session_id,))
    msgs = cur.fetchall()
    cur.close()
    conn.close()
    return msgs

# --- AGENTIC CHAT ENDPOINT ---
@app.post("/api/chat")
async def chat(request: Request, authorization: str = Header(None)):
    user = get_current_user(authorization)
    uid = user["id"]
    data = await request.json()
    user_msg = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())
    mode_key = data.get("mode", "standard")

    mode_info = MODES.get(mode_key, MODES["standard"])
    if user["coins"] < mode_info["req"]:
        mode_key = "standard"
        mode_info = MODES["standard"]

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT fact FROM user_memories WHERE user_id = %s ORDER BY id DESC LIMIT 10;", (uid,))
    memories = [r["fact"] for r in cur.fetchall()]
    mem_text = "\n".join(f"- {m}" for m in memories) if memories else "None saved yet."

    cur.execute("SELECT id, user_id FROM sessions WHERE id = %s;", (session_id,))
    sess_row = cur.fetchone()
    if not sess_row:
        cur.execute("INSERT INTO sessions (id, user_id, title, mode) VALUES (%s, %s, %s, %s);",
                    (session_id, uid, user_msg[:40], mode_key))
    elif sess_row["user_id"] != uid:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cur.execute("SELECT role, content FROM messages WHERE session_id = %s ORDER BY id ASC LIMIT 16;", (session_id,))
    past = cur.fetchall()
    history_contents = []
    for m in past:
        role = "user" if m["role"] == "user" else "model"
        history_contents.append(types.Content(role=role, parts=[types.Part.from_text(text=m["content"])]))

    cur.execute("INSERT INTO messages (session_id, role, content) VALUES (%s, %s, %s);",
                (session_id, "user", user_msg))
    conn.commit()
    cur.close()
    conn.close()

    history_contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_msg)]))

    executed_tools = []
    generated_file = None

    def web_search(query: str) -> str:
        """Searches the live internet via Tavily for current events, facts, news, or documentation."""
        executed_tools.append(f"Web Search: {query}")
        if not tavily:
            return "Tavily API key not configured."
        try:
            res = tavily.search(query=query, max_results=4)
            return "\n".join([f"- {r['title']}: {r['content']} ({r['url']})" for r in res.get("results", [])]) or "No results."
        except Exception as e:
            return f"Search error: {str(e)}"

    def calculate_math(expression: str) -> str:
        """Evaluates mathematical expressions or solves equations (e.g., '2*x + 5 = 15' or 'integrate(x**2, x)') using SymPy."""
        executed_tools.append(f"Math Engine: {expression}")
        try:
            if "=" in expression and "==" not in expression:
                lhs, rhs = expression.split("=", 1)
                eq = sp.Eq(sp.sympify(lhs), sp.sympify(rhs))
                sol = sp.solve(eq)
                return f"Equation: {eq} | Solution: {sol}"
            expr = sp.sympify(expression)
            val = expr.evalf()
            return f"Exact: {expr} | Decimal: {val}"
        except Exception as e:
            return f"Math parse error: {str(e)}"

    def save_user_memory(fact: str) -> str:
        """Saves an important personal fact, preference, or goal about the user into long-term database memory."""
        executed_tools.append(f"Memory Saved: {fact[:28]}...")
        c = get_db()
        cr = c.cursor()
        cr.execute("INSERT INTO user_memories (user_id, fact) VALUES (%s, %s);", (uid, fact[:200]))
        c.commit()
        cr.close()
        c.close()
        return "Fact permanently saved to user's core memory."

    def create_downloadable_file(filename: str, content: str) -> str:
        """Creates a downloadable text/markdown/code file for the user when they ask for notes, scripts, or documents."""
        nonlocal generated_file
        executed_tools.append(f"File Built: {filename}")
        b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        generated_file = json.dumps({"filename": filename, "b64": b64})
        return f"File '{filename}' generated and attached as a download button in the UI."

    full_sys_prompt = f"{mode_info['prompt']}\n\nUser's Saved Long-Term Memory:\n{mem_text}"

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=history_contents,
        config=types.GenerateContentConfig(
            system_instruction=full_sys_prompt,
            tools=[web_search, calculate_math, save_user_memory, create_downloadable_file],
            temperature=0.6
        )
    )

    reply_text = response.text or "Action completed."
    tools_str = " | ".join(executed_tools) if executed_tools else None

    # Spiritual Coins Calculation (Max 30/day)
    earned = 10 if executed_tools else 5
    actual_add = min(earned, max(0, 30 - user["daily_coins"]))
    new_total = user["coins"] + actual_add
    new_daily = user["daily_coins"] + actual_add

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET coins = %s, daily_coins = %s, last_coin_date = CURRENT_DATE WHERE id = %s;",
        (new_total, new_daily, uid)
    )
    cur.execute(
        "INSERT INTO messages (session_id, role, content, tools_used, file_data) VALUES (%s, %s, %s, %s, %s);",
        (session_id, "model", reply_text, tools_str, generated_file)
    )
    conn.commit()
    cur.close()
    conn.close()

    return JSONResponse({
        "session_id": session_id,
        "reply": reply_text,
        "tools_used": tools_str,
        "file_data": generated_file,
        "coins": new_total,
        "daily_coins": new_daily
    })

# --- SEARCH-ONLY SOCIAL & LIVE FRIEND CHAT ---
@app.get("/api/users/search")
async def search_user(q: str, authorization: str = Header(None)):
    viewer = get_current_user(authorization)
    q = q.strip().lower()
    if not q:
        return JSONResponse({"error": "Enter exact username"}, status_code=400)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, coins, bio FROM users WHERE username = %s;", (q,))
    target = cur.fetchone()
    if not target:
        cur.close()
        conn.close()
        return JSONResponse({"error": "User not found"}, status_code=404)

    cur.execute("""
        SELECT status, sender_id FROM friendships 
        WHERE (sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s);
    """, (viewer["id"], target["id"], target["id"], viewer["id"]))
    rel = cur.fetchone()
    cur.close()
    conn.close()
    return {"profile": target, "relation": rel}

@app.post("/api/friends/action")
async def friend_action(request: Request, authorization: str = Header(None)):
    me = get_current_user(authorization)["id"]
    data = await request.json()
    other = data.get("target_id")
    act = data.get("action")
    if me == other:
        return JSONResponse({"error": "Cannot add yourself"}, status_code=400)
    conn = get_db()
    cur = conn.cursor()
    if act == "send":
        cur.execute("""
            INSERT INTO friendships (sender_id, receiver_id, status) VALUES (%s, %s, 'pending')
            ON CONFLICT DO NOTHING;
        """, (me, other))
    elif act == "accept":
        cur.execute("""
            UPDATE friendships SET status = 'accepted' 
            WHERE sender_id = %s AND receiver_id = %s;
        """, (other, me))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

@app.get("/api/friends/list")
async def list_friends(authorization: str = Header(None)):
    uid = get_current_user(authorization)["id"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.username, u.coins, f.status, f.sender_id
        FROM friendships f
        JOIN users u ON (u.id = CASE WHEN f.sender_id = %s THEN f.receiver_id ELSE f.sender_id END)
        WHERE f.sender_id = %s OR f.receiver_id = %s;
    """, (uid, uid, uid))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"friends": rows}

@app.get("/api/dm/{friend_id}")
async def get_dms(friend_id: int, authorization: str = Header(None)):
    uid = get_current_user(authorization)["id"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT sender_id, content, created_at FROM direct_messages
        WHERE (sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)
        ORDER BY id ASC LIMIT 60;
    """, (uid, friend_id, friend_id, uid))
    msgs = cur.fetchall()
    for m in msgs:
        m["created_at"] = str(m["created_at"])[:16]
    cur.close()
    conn.close()
    return {"messages": msgs}

@app.post("/api/dm/send")
async def send_dm(request: Request, authorization: str = Header(None)):
    uid = get_current_user(authorization)["id"]
    data = await request.json()
    content = data.get("content", "").strip()
    if not content:
        return {"ok": False}
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO direct_messages (sender_id, receiver_id, content) VALUES (%s, %s, %s);",
        (uid, data["receiver_id"], content[:1000])
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

# --- FRONTEND UI ---
HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SOVEREIGN // Agentic AI</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {
    --accent: #00f0ff;
    --bg: #06080d;
    --panel: #0c1018;
    --card: #131926;
    --border: #1e293b;
    --text: #e2e8f0;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', system-ui, sans-serif; }
  body { background: var(--bg); color: var(--text); height: 100vh; display: flex; overflow: hidden; }

  /* Auth Overlay */
  #auth-screen {
    position: fixed; inset: 0; background: #06080d; z-index: 60;
    display: flex; align-items: center; justify-content: center;
  }
  .auth-box {
    background: var(--panel); border: 1px solid var(--border); padding: 32px;
    border-radius: 16px; width: 350px; max-width: 92vw; text-align: center;
    box-shadow: 0 0 40px rgba(168, 85, 247, 0.14);
  }
  .auth-box h2 { margin: 12px 0 6px; letter-spacing: 1px; color: #fff; }
  .auth-box p { font-size: 0.85rem; color: #64748b; margin-bottom: 20px; }
  .auth-box input {
    width: 100%; background: var(--card); border: 1px solid var(--border);
    padding: 11px 14px; border-radius: 8px; color: #fff; margin-bottom: 12px; outline: none;
  }
  .auth-btns { display: flex; gap: 10px; }
  .auth-btns button {
    flex: 1; padding: 11px; border-radius: 8px; border: none; font-weight: 700; cursor: pointer;
  }
  .btn-primary { background: var(--accent); color: #000; }
  .btn-sec { background: var(--card); color: #fff; border: 1px solid var(--border) !important; }

  /* Sidebar */
  .sidebar {
    width: 270px; background: var(--panel); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; padding: 14px; z-index: 30; transition: transform 0.25s ease;
  }
  .coin-card {
    background: linear-gradient(135deg, #161224, #111827);
    border: 1px solid #7e22ce; border-radius: 12px; padding: 12px; margin-bottom: 14px;
    display: flex; align-items: center; gap: 12px; box-shadow: 0 0 18px rgba(168, 85, 247, 0.2);
  }
  .coin-total { font-size: 1.02rem; font-weight: 800; color: #facc15; }
  .coin-cap { font-size: 0.74rem; color: #c084fc; margin-top: 2px; }

  .mode-group { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
  .mode-btn {
    background: var(--card); border: 1px solid var(--border); color: #94a3b8;
    padding: 9px 11px; border-radius: 8px; cursor: pointer; font-size: 0.83rem;
    font-weight: 600; display: flex; justify-content: space-between; align-items: center;
  }
  .mode-btn.active { color: #fff; border-color: var(--accent); background: rgba(0, 240, 255, 0.08); }
  .mode-btn.locked { opacity: 0.45; cursor: not-allowed; }

  .new-btn {
    background: var(--accent); color: #000; font-weight: 700; border: none;
    padding: 9px; border-radius: 8px; cursor: pointer; margin-bottom: 12px; font-size: 0.85rem;
  }
  .history { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
  .hist-item {
    padding: 7px 10px; border-radius: 6px; font-size: 0.82rem; color: #94a3b8;
    cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .hist-item:hover, .hist-item.active { background: var(--card); color: #fff; }

  .bottom-nav { display: flex; gap: 6px; border-top: 1px solid var(--border); padding-top: 10px; }
  .nav-btn {
    flex: 1; background: var(--card); border: 1px solid var(--border); color: #cbd5e1;
    padding: 8px; border-radius: 8px; font-size: 0.8rem; font-weight: 600; cursor: pointer; position: relative;
  }
  .nav-btn:hover { border-color: var(--accent); color: #fff; }
  .badge-dot {
    position: absolute; top: -4px; right: -4px; width: 9px; height: 9px;
    background: #ef4444; border-radius: 50%; display: none;
  }

  /* Main Workspace */
  .main { flex: 1; display: flex; flex-direction: column; position: relative; width: 100%; }
  .topbar {
    padding: 12px 18px; border-bottom: 1px solid var(--border); display: flex;
    justify-content: space-between; align-items: center; background: rgba(12, 16, 24, 0.9);
  }
  .menu-toggle {
    display: none; background: var(--card); border: 1px solid var(--border);
    color: #fff; padding: 6px 10px; border-radius: 6px; font-size: 1rem; cursor: pointer;
  }
  .agent-pills { display: flex; gap: 6px; flex-wrap: wrap; }
  .pill { font-size: 0.72rem; background: var(--card); border: 1px solid var(--border); padding: 4px 8px; border-radius: 6px; color: #94a3b8; }

  .chat-box { flex: 1; overflow-y: auto; padding: 24px 12%; display: flex; flex-direction: column; gap: 16px; }
  .msg-wrap { display: flex; flex-direction: column; max-width: 84%; }
  .msg-wrap.user { align-self: flex-end; }
  .msg-wrap.ai { align-self: flex-start; }

  .tool-tag {
    font-size: 0.74rem; color: #38bdf8; background: rgba(56, 189, 248, 0.08);
    border: 1px solid rgba(56, 189, 248, 0.25); padding: 4px 10px; border-radius: 6px;
    margin-bottom: 6px; font-family: monospace; width: fit-content;
  }
  .msg { padding: 12px 16px; border-radius: 12px; line-height: 1.6; font-size: 0.93rem; word-wrap: break-word; }
  .msg.user { background: var(--accent); color: #05080f; font-weight: 600; border-bottom-right-radius: 2px; }
  .msg.ai { background: var(--card); border: 1px solid var(--border); border-bottom-left-radius: 2px; }
  .msg.ai pre { background: #06080d; padding: 10px; border-radius: 8px; overflow-x: auto; margin: 8px 0; }
  .dl-btn {
    display: inline-block; margin-top: 8px; background: #10b981; color: #000;
    font-weight: 700; padding: 7px 12px; border-radius: 7px; text-decoration: none; font-size: 0.82rem;
  }

  .input-area { padding: 14px 12%; background: var(--bg); }
  .input-wrap {
    display: flex; gap: 10px; background: var(--panel); padding: 8px 12px;
    border-radius: 12px; border: 1px solid var(--border);
  }
  .input-wrap input { flex: 1; background: transparent; border: none; color: #fff; font-size: 0.95rem; outline: none; min-width: 0; }
  .send-btn { background: var(--accent); color: #000; border: none; padding: 8px 16px; border-radius: 8px; font-weight: 700; cursor: pointer; }

  /* Mobile Responsiveness */
  @media(max-width: 768px) {
    .sidebar { position: fixed; top: 0; bottom: 0; left: 0; transform: translateX(-100%); }
    .sidebar.open { transform: translateX(0); box-shadow: 10px 0 30px rgba(0,0,0,0.8); }
    .menu-toggle { display: inline-block; }
    .agent-pills { display: none; }
    .chat-box, .input-area { padding: 14px; }
    .msg-wrap { max-width: 92%; }
  }

  /* Modals */
  .modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.78); z-index: 50;
    display: none; align-items: center; justify-content: center;
  }
  .modal-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    width: 440px; max-width: 94vw; max-height: 86vh; display: flex; flex-direction: column; padding: 20px;
  }
  .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
  .close-x { background: none; border: none; color: #94a3b8; font-size: 1.2rem; cursor: pointer; }
  .search-row { display: flex; gap: 8px; margin-bottom: 14px; }
  .search-row input { flex: 1; background: var(--card); border: 1px solid var(--border); padding: 9px 12px; border-radius: 8px; color: #fff; outline: none; }
  .dm-box { flex: 1; overflow-y: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px; min-height: 170px; max-height: 240px; display: flex; flex-direction: column; gap: 8px; margin: 10px 0; }
  .dm-msg { padding: 7px 11px; border-radius: 8px; font-size: 0.85rem; max-width: 80%; }
  .dm-me { align-self: flex-end; background: var(--accent); color: #000; font-weight: 600; }
  .dm-them { align-self: flex-start; background: var(--card); }
</style>
</head>
<body>

<!-- AUTH SCREEN -->
<div id="auth-screen">
  <div class="auth-box">
    <div id="auth-coin-logo"></div>
    <h2>SOVEREIGN AI</h2>
    <p>Autonomous Agent & Spiritual Coin Vault</p>
    <input type="text" id="auth-user" placeholder="Username (min 3 chars)">
    <input type="password" id="auth-pass" placeholder="Password (min 4 chars)" onkeydown="if(event.key==='Enter') submitAuth('login')">
    <div class="auth-btns">
      <button class="btn-primary" onclick="submitAuth('login')">Sign In</button>
      <button class="btn-sec" onclick="submitAuth('signup')">Create Account</button>
    </div>
    <p id="auth-err" style="color:#f87171; margin-top:10px; margin-bottom:0;"></p>
  </div>
</div>

<!-- SIDEBAR -->
<div class="sidebar" id="sidebar">
  <div class="coin-card">
    <div id="sidebar-coin-svg"></div>
    <div class="coin-info">
      <div class="coin-total"><span id="coin-count">0</span> Spiritual Coins</div>
      <div class="coin-cap">Today: <span id="daily-count">0</span> / 30 max</div>
    </div>
  </div>

  <div class="mode-group" id="mode-list"></div>
  <button class="new-btn" onclick="newSession()">+ New Agent Task</button>
  <div class="history" id="history-list"></div>

  <div class="bottom-nav">
    <button class="nav-btn" onclick="openProfileModal()">👤 Profile</button>
    <button class="nav-btn" onclick="openChatModal()">💬 Chat <span class="badge-dot" id="req-dot"></span></button>
  </div>
</div>

<!-- MAIN WORKSPACE -->
<div class="main" onclick="closeSidebarMobile(event)">
  <div class="topbar">
    <div style="display:flex; align-items:center; gap:10px;">
      <button class="menu-toggle" onclick="toggleSidebar(event)">☰</button>
      <strong style="letter-spacing:1px; color:var(--accent);">SOVEREIGN // AGENT</strong>
    </div>
    <div class="agent-pills">
      <span class="pill">🌐 Live Search</span>
      <span class="pill">∑ SymPy Math</span>
      <span class="pill">🧠 Core Memory</span>
      <span class="pill">📄 File Builder</span>
    </div>
  </div>

  <div class="chat-box" id="chat-box"></div>

  <div class="input-area">
    <div class="input-wrap">
      <input type="text" id="prompt" placeholder="Ask, search live web, solve math, or generate files..." onkeydown="if(event.key==='Enter') sendMessage()">
      <button class="send-btn" onclick="sendMessage()">Execute</button>
    </div>
  </div>
</div>

<!-- PROFILE MODAL -->
<div class="modal" id="profile-modal">
  <div class="modal-card">
    <div class="modal-head">
      <h3>Cultivator Profile</h3>
      <button class="close-x" onclick="closeModals()">✕</button>
    </div>
    <p style="margin-bottom:8px;">Username: <strong id="prof-username" style="color:var(--accent);"></strong>
      <button onclick="copyUsername()" style="margin-left:8px; padding:3px 8px; border-radius:5px; border:1px solid #334155; background:#1e293b; color:#fff; cursor:pointer; font-size:0.75rem;">Copy</button>
    </p>
    <p style="font-size:0.85rem; color:#facc15; margin-bottom:10px;">Total Spiritual Coins: <span id="prof-coins">0</span></p>
    <label style="font-size:0.8rem; color:#94a3b8;">Public Bio:</label>
    <input type="text" id="prof-bio" style="background:var(--card); border:1px solid var(--border); padding:8px; border-radius:8px; color:#fff; margin:6px 0 10px;">
    <button class="btn-primary" style="padding:8px; border-radius:8px; border:none; font-weight:700; cursor:pointer;" onclick="saveBio()">Save Bio</button>
    
    <div style="margin-top:14px;">
      <strong style="font-size:0.82rem; color:#94a3b8;">AI Long-Term Memory Vault:</strong>
      <ul id="mem-list" style="font-size:0.8rem; color:#cbd5e1; margin-top:6px; padding-left:18px; max-height:110px; overflow-y:auto;"></ul>
    </div>
    <button onclick="logout()" style="margin-top:14px; background:#7f1d1d; color:#fff; border:none; padding:8px; border-radius:8px; cursor:pointer;">Logout</button>
  </div>
</div>

<!-- SEARCH-ONLY FRIEND & LIVE CHAT MODAL -->
<div class="modal" id="chat-modal">
  <div class="modal-card">
    <div class="modal-head">
      <h3>Private Network & Chat</h3>
      <button class="close-x" onclick="closeModals()">✕</button>
    </div>
    <div class="search-row">
      <input type="text" id="user-search-input" placeholder="Search exact username...">
      <button class="btn-primary" style="padding:0 14px; border-radius:8px; border:none; font-weight:700; cursor:pointer;" onclick="searchUser()">Find</button>
    </div>
    <div id="search-result-box" style="margin-bottom:12px;"></div>

    <strong style="font-size:0.82rem; color:#94a3b8;">Friends & Requests:</strong>
    <div id="friends-list" style="display:flex; flex-direction:column; gap:6px; margin-top:6px; max-height:120px; overflow-y:auto;"></div>

    <div id="dm-section" style="display:none; margin-top:10px; border-top:1px solid var(--border); padding-top:10px;">
      <strong id="dm-with-title" style="font-size:0.85rem; color:var(--accent);"></strong>
      <div class="dm-box" id="dm-box"></div>
      <div class="search-row" style="margin-bottom:0;">
        <input type="text" id="dm-input" placeholder="Message friend..." onkeydown="if(event.key==='Enter') sendDM()">
        <button class="btn-primary" style="padding:0 14px; border-radius:8px; border:none; font-weight:700; cursor:pointer;" onclick="sendDM()">Send</button>
      </div>
    </div>
  </div>
</div>

<script>
// Golden Spiritual Coin with Purple Outline & Jade Eastern Dragon SVG
const SPIRITUAL_COIN_SVG = `
<svg width="42" height="42" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="goldGrad" cx="35%" cy="35%" r="65%">
      <stop offset="0%" stop-color="#fef08a"/>
      <stop offset="55%" stop-color="#eab308"/>
      <stop offset="100%" stop-color="#a16207"/>
    </radialGradient>
    <filter id="purpleGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="0" stdDeviation="3" flood-color="#a855f7"/>
    </filter>
  </defs>
  <circle cx="32" cy="32" r="27" fill="url(#goldGrad)" stroke="#9333ea" stroke-width="3.5" filter="url(#purpleGlow)"/>
  <circle cx="32" cy="32" r="21" fill="none" stroke="#7e22ce" stroke-width="1.2" stroke-dasharray="3 2"/>
  <path d="M22 40 C18 33, 24 26, 32 27 C39 28, 43 21, 37 17 C33 14, 27 17, 29 21 C31 24, 37 22, 39 26 C42 32, 34 39, 27 38 C23 37, 20 42, 25 45 C31 48, 41 44, 43 37" 
        fill="none" stroke="#10b981" stroke-width="3.4" stroke-linecap="round"/>
  <circle cx="38" cy="17" r="2.3" fill="#059669"/>
  <path d="M39 15 L43 13 M38 14 L40 11" stroke="#34d399" stroke-width="1.4" stroke-linecap="round"/>
</svg>`;

document.getElementById("auth-coin-logo").innerHTML = SPIRITUAL_COIN_SVG;
document.getElementById("sidebar-coin-svg").innerHTML = SPIRITUAL_COIN_SVG;

let currentUser = JSON.parse(localStorage.getItem("sov_user") || "null");
let currentSession = null;
let currentMode = "standard";
let activeFriendId = null;
let activeFriendName = "";
let dmPoller = null;

const MODE_INFO = {
  standard: { name: "Standard Core", req: 0, color: "#00f0ff" },
  brutal: { name: "Brutal Truth", req: 50, color: "#ff4d4d" },
  architect: { name: "Architect Mode", req: 120, color: "#a855f7" }
};

function authHeaders() {
  return {
    "Content-Type": "application/json",
    "Authorization": currentUser?.auth_token || ""
  };
}

function toggleSidebar(e) {
  e.stopPropagation();
  document.getElementById("sidebar").classList.toggle("open");
}

function closeSidebarMobile() {
  document.getElementById("sidebar").classList.remove("open");
}

if (currentUser && currentUser.auth_token) {
  document.getElementById("auth-screen").style.display = "none";
  refreshState();
}

async function submitAuth(action) {
  const username = document.getElementById("auth-user").value;
  const password = document.getElementById("auth-pass").value;
  const res = await fetch("/api/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, username, password })
  });
  const data = await res.json();
  if (!res.ok) {
    document.getElementById("auth-err").innerText = data.error;
    return;
  }
  currentUser = data.user;
  localStorage.setItem("sov_user", JSON.stringify(currentUser));
  document.getElementById("auth-screen").style.display = "none";
  refreshState();
}

function logout() {
  localStorage.removeItem("sov_user");
  location.reload();
}

function renderModes() {
  const list = document.getElementById("mode-list");
  list.innerHTML = "";
  for (const [key, info] of Object.entries(MODE_INFO)) {
    const locked = (currentUser?.coins || 0) < info.req;
    const btn = document.createElement("button");
    btn.className = `mode-btn ${currentMode === key ? 'active' : ''} ${locked ? 'locked' : ''}`;
    btn.innerHTML = `<span>${info.name}</span><span>${locked ? '🔒 ' + info.req : '✓'}</span>`;
    btn.onclick = () => {
      if (locked) return alert(`Requires ${info.req} Spiritual Coins!`);
      currentMode = key;
      document.documentElement.style.setProperty('--accent', info.color);
      renderModes();
      closeSidebarMobile();
    };
    list.appendChild(btn);
  }
}

async function refreshState() {
  if (!currentUser) return;
  const res = await fetch("/api/state", { headers: authHeaders() });
  if (res.status === 401) return logout();
  const data = await res.json();
  currentUser = { ...currentUser, ...data.user };
  localStorage.setItem("sov_user", JSON.stringify(currentUser));

  document.getElementById("coin-count").innerText = currentUser.coins;
  document.getElementById("daily-count").innerText = currentUser.daily_coins;
  document.getElementById("req-dot").style.display = data.pending_reqs > 0 ? "block" : "none";
  renderModes();

  const hist = document.getElementById("history-list");
  hist.innerHTML = "";
  data.sessions.forEach(s => {
    const div = document.createElement("div");
    div.className = "hist-item" + (s.id === currentSession ? " active" : "");
    div.innerText = s.title || "Task";
    div.onclick = () => { loadMessages(s.id, s.mode); closeSidebarMobile(); };
    hist.appendChild(div);
  });

  const memUl = document.getElementById("mem-list");
  memUl.innerHTML = data.memories.length ? data.memories.map(m => `<li>${m}</li>`).join("") : "<li>No core memories saved yet.</li>";
}

async function loadMessages(sid, modeKey) {
  currentSession = sid;
  if (modeKey && MODE_INFO[modeKey]) {
    currentMode = modeKey;
    document.documentElement.style.setProperty('--accent', MODE_INFO[modeKey].color);
    renderModes();
  }
  const res = await fetch(`/api/messages/${sid}`, { headers: authHeaders() });
  const msgs = await res.json();
  const box = document.getElementById("chat-box");
  box.innerHTML = "";
  msgs.forEach(m => appendMsg(m.role, m.content, m.tools_used, m.file_data));
  refreshState();
}

function newSession() {
  currentSession = null;
  document.getElementById("chat-box").innerHTML = "";
  closeSidebarMobile();
}

function appendMsg(role, text, toolsUsed = null, fileData = null) {
  const box = document.getElementById("chat-box");
  const wrap = document.createElement("div");
  wrap.className = `msg-wrap ${role === 'user' ? 'user' : 'ai'}`;

  if (toolsUsed) {
    const tag = document.createElement("div");
    tag.className = "tool-tag";
    tag.innerText = `⚙ ${toolsUsed}`;
    wrap.appendChild(tag);
  }

  const div = document.createElement("div");
  div.className = `msg ${role === 'user' ? 'user' : 'ai'}`;
  div.innerHTML = role === 'user' ? text : marked.parse(text);

  if (fileData) {
    try {
      const f = typeof fileData === "string" ? JSON.parse(fileData) : fileData;
      const a = document.createElement("a");
      a.className = "dl-btn";
      a.href = `data:text/plain;base64,${f.b64}`;
      a.download = f.filename;
      a.innerText = `⬇ Download ${f.filename}`;
      div.appendChild(document.createElement("br"));
      div.appendChild(a);
    } catch(e) {}
  }

  wrap.appendChild(div);
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
  return wrap;
}

async function sendMessage() {
  const input = document.getElementById("prompt");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  appendMsg("user", text);

  const loading = appendMsg("ai", "_Executing agentic workflow..._");
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ message: text, session_id: currentSession, mode: currentMode })
  });
  const data = await res.json();
  loading.remove();

  currentSession = data.session_id;
  appendMsg("ai", data.reply, data.tools_used, data.file_data);
  refreshState();
}

// Profile & Social Functions
function openProfileModal() {
  document.getElementById("prof-username").innerText = currentUser.username;
  document.getElementById("prof-coins").innerText = currentUser.coins;
  document.getElementById("prof-bio").value = currentUser.bio || "";
  document.getElementById("profile-modal").style.display = "flex";
}

function copyUsername() {
  navigator.clipboard.writeText(currentUser.username);
  alert("Username copied!");
}

async function saveBio() {
  const bio = document.getElementById("prof-bio").value;
  await fetch("/api/profile/bio", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ bio })
  });
  refreshState();
  alert("Bio updated!");
}

function openChatModal() {
  document.getElementById("chat-modal").style.display = "flex";
  loadFriends();
}

function closeModals() {
  document.getElementById("profile-modal").style.display = "none";
  document.getElementById("chat-modal").style.display = "none";
  if (dmPoller) clearInterval(dmPoller);
}

async function searchUser() {
  const q = document.getElementById("user-search-input").value;
  const box = document.getElementById("search-result-box");
  const res = await fetch(`/api/users/search?q=${encodeURIComponent(q)}`, { headers: authHeaders() });
  const data = await res.json();
  if (!res.ok) {
    box.innerHTML = `<p style="color:#f87171; font-size:0.83rem;">${data.error}</p>`;
    return;
  }
  const p = data.profile;
  let btnHtml = `<button class="btn-primary" style="padding:5px 10px; border-radius:6px; border:none; font-weight:700; cursor:pointer;" onclick="friendAct(${p.id}, 'send')">+ Add Friend</button>`;
  if (p.id === currentUser.id) btnHtml = `<span style="font-size:0.78rem; color:#94a3b8;">(You)</span>`;
  else if (data.relation?.status === 'accepted') btnHtml = `<span style="font-size:0.78rem; color:#10b981;">✓ Friends</span>`;
  else if (data.relation?.status === 'pending') btnHtml = `<span style="font-size:0.78rem; color:#facc15;">Pending</span>`;

  box.innerHTML = `
    <div style="background:var(--card); padding:10px; border-radius:8px; border:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
      <div>
        <strong>${p.username}</strong> <span style="color:#facc15; font-size:0.8rem;">(${p.coins} Coins)</span>
        <div style="font-size:0.78rem; color:#94a3b8;">${p.bio || ''}</div>
      </div>
      ${btnHtml}
    </div>`;
}

async function friendAct(targetId, action) {
  await fetch("/api/friends/action", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ target_id: targetId, action })
  });
  searchUser();
  loadFriends();
  refreshState();
}

async function loadFriends() {
  const res = await fetch("/api/friends/list", { headers: authHeaders() });
  const data = await res.json();
  const list = document.getElementById("friends-list");
  list.innerHTML = data.friends.length ? "" : "<p style='font-size:0.8rem; color:#64748b;'>Search an exact username above to add friends.</p>";
  data.friends.forEach(f => {
    const row = document.createElement("div");
    row.style = "display:flex; justify-content:space-between; align-items:center; background:var(--card); padding:7px 10px; border-radius:7px; font-size:0.84rem;";
    let actionBtn = "";
    if (f.status === "accepted") {
      actionBtn = `<button class="btn-primary" style="padding:4px 10px; border-radius:5px; border:none; font-weight:700; cursor:pointer;" onclick="openDM(${f.id}, '${f.username}')">Chat</button>`;
    } else if (f.sender_id !== currentUser.id) {
      actionBtn = `<button class="btn-primary" style="padding:4px 10px; border-radius:5px; border:none; font-weight:700; cursor:pointer;" onclick="friendAct(${f.id}, 'accept')">Accept</button>`;
    } else {
      actionBtn = `<span style="color:#94a3b8; font-size:0.75rem;">Requested</span>`;
    }
    row.innerHTML = `<span>${f.username} <small style="color:#facc15;">(${f.coins})</small></span>${actionBtn}`;
    list.appendChild(row);
  });
}

async function fetchDMs() {
  if (!activeFriendId) return;
  const res = await fetch(`/api/dm/${activeFriendId}`, { headers: authHeaders() });
  const data = await res.json();
  const box = document.getElementById("dm-box");
  const isAtBottom = box.scrollHeight - box.scrollTop <= box.clientHeight + 40;
  box.innerHTML = "";
  data.messages.forEach(m => {
    const d = document.createElement("div");
    d.className = "dm-msg " + (m.sender_id === currentUser.id ? "dm-me" : "dm-them");
    d.innerText = m.content;
    box.appendChild(d);
  });
  if (isAtBottom) box.scrollTop = box.scrollHeight;
}

function openDM(fid, fname) {
  activeFriendId = fid;
  activeFriendName = fname;
  document.getElementById("dm-section").style.display = "block";
  document.getElementById("dm-with-title").innerText = `Live Chat with ${fname}`;
  fetchDMs();
  if (dmPoller) clearInterval(dmPoller);
  dmPoller = setInterval(fetchDMs, 3000);
}

async function sendDM() {
  const inp = document.getElementById("dm-input");
  const content = inp.value.trim();
  if (!content || !activeFriendId) return;
  inp.value = "";
  await fetch("/api/dm/send", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ receiver_id: activeFriendId, content })
  });
  fetchDMs();
}
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_UI