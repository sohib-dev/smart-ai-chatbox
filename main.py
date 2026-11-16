import os
import json
import uuid
import time
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from google.genai import types
from tavily import TavilyClient
import sympy as sp

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
MODELS_CHAIN = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
app = FastAPI(title="Sovereign Agentic AI")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None


def get_db():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    if not DATABASE_URL:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            auth_token TEXT UNIQUE,
            bio TEXT DEFAULT 'Sovereign Explorer',
            avatar_seed TEXT DEFAULT 'dragon',
            spiritual_coins INTEGER DEFAULT 0,
            daily_coins_earned INTEGER DEFAULT 0,
            last_coin_date TEXT DEFAULT '',
            unlocked_modes TEXT DEFAULT '["standard"]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_seed TEXT DEFAULT 'dragon';")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tools_used TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_memories (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            fact TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS friendships (
            id SERIAL PRIMARY KEY,
            requester_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            receiver_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(requester_id, receiver_id)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS direct_messages (
            id SERIAL PRIMARY KEY,
            sender_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            receiver_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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
    print("DB Init warning:", e)


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1].strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE auth_token = %s", (token,))
    user = cur.fetchone()
    if not user:
        cur.close()
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid session token")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if user["last_coin_date"] != today_str:
        cur.execute(
            "UPDATE users SET daily_coins_earned = 0, last_coin_date = %s WHERE id = %s",
            (today_str, user["id"]),
        )
        conn.commit()
        user["daily_coins_earned"] = 0
        user["last_coin_date"] = today_str

    cur.close()
    conn.close()
    return user


def award_spiritual_coins(user_id: str, amount: int) -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT spiritual_coins, daily_coins_earned, last_coin_date FROM users WHERE id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return {"coins": 0, "daily": 0, "added": 0}

    daily = row["daily_coins_earned"] if row["last_coin_date"] == today_str else 0
    remaining = max(0, 30 - daily)
    actual_add = min(amount, remaining)
    new_total = row["spiritual_coins"] + actual_add
    new_daily = daily + actual_add

    cur.execute(
        "UPDATE users SET spiritual_coins = %s, daily_coins_earned = %s, last_coin_date = %s WHERE id = %s",
        (new_total, new_daily, today_str, user_id),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"coins": new_total, "daily": new_daily, "added": actual_add}


# --- Pydantic Models ---
class AuthRequest(BaseModel):
    username: str
    password: str


class ProfileUpdate(BaseModel):
    bio: str
    avatar_seed: str = "dragon"


class UnlockModeRequest(BaseModel):
    mode: str


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    mode: str = "standard"


class FriendRequestPayload(BaseModel):
    username: str


class FriendRespondPayload(BaseModel):
    friendship_id: int
    action: str


class DMPayload(BaseModel):
    receiver_id: str
    content: str


# --- Auth & User Endpoints ---
@app.post("/api/auth/register")
def register(req: AuthRequest):
    uname = req.username.strip()
    if len(uname) < 3 or len(req.password) < 4:
        raise HTTPException(status_code=400, detail="Username >= 3 chars & password >= 4 chars required")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (uname,))
    if cur.fetchone():
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Username already taken")

    uid = str(uuid.uuid4())
    token = secrets.token_hex(32)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur.execute(
        """INSERT INTO users (id, username, password_hash, auth_token, last_coin_date)
           VALUES (%s, %s, %s, %s, %s)""",
        (uid, uname, hash_password(req.password), token, today_str),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"token": token, "username": uname}


@app.post("/api/auth/login")
def login(req: AuthRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, username, password_hash FROM users WHERE LOWER(username) = LOWER(%s)",
        (req.username.strip(),),
    )
    user = cur.fetchone()
    if not user or user["password_hash"] != hash_password(req.password):
        cur.close()
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = secrets.token_hex(32)
    cur.execute("UPDATE users SET auth_token = %s WHERE id = %s", (token, user["id"]))
    conn.commit()
    cur.close()
    conn.close()
    return {"token": token, "username": user["username"]}


@app.get("/api/me")
def get_me(authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, fact FROM user_memories WHERE user_id = %s ORDER BY id DESC", (user["id"],))
    memories = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS cnt FROM sessions WHERE user_id = %s", (user["id"],))
    sess_count = cur.fetchone()["cnt"]
    cur.close()
    conn.close()
    return {
        "id": user["id"],
        "username": user["username"],
        "bio": user["bio"],
        "avatar_seed": user.get("avatar_seed") or "dragon",
        "spiritual_coins": user["spiritual_coins"],
        "daily_coins_earned": user["daily_coins_earned"],
        "unlocked_modes": json.loads(user["unlocked_modes"] or '["standard"]'),
        "memories": memories,
        "session_count": sess_count,
    }


@app.post("/api/me/profile")
def update_profile(payload: ProfileUpdate, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET bio = %s, avatar_seed = %s WHERE id = %s",
        (payload.bio[:200], payload.avatar_seed[:30], user["id"]),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "ok"}


@app.post("/api/modes/unlock")
def unlock_mode(payload: UnlockModeRequest, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    costs = {"standard": 0, "brutal": 50, "architect": 120, "overdrive": 200}
    if payload.mode not in costs:
        raise HTTPException(status_code=400, detail="Unknown upgrade")

    unlocked = json.loads(user["unlocked_modes"] or '["standard"]')
    if payload.mode in unlocked:
        return {"unlocked_modes": unlocked, "spiritual_coins": user["spiritual_coins"]}

    cost = costs[payload.mode]
    if user["spiritual_coins"] < cost:
        raise HTTPException(status_code=400, detail=f"Need {cost} Spiritual Coins")

    new_coins = user["spiritual_coins"] - cost
    unlocked.append(payload.mode)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET spiritual_coins = %s, unlocked_modes = %s WHERE id = %s",
        (new_coins, json.dumps(unlocked), user["id"]),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"unlocked_modes": unlocked, "spiritual_coins": new_coins}


# --- Sessions & Chat ---
@app.get("/api/sessions")
def list_sessions(authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, created_at FROM sessions WHERE user_id = %s ORDER BY created_at DESC",
        (user["id"],),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.get("/api/sessions/{session_id}")
def get_session_messages(session_id: str, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM sessions WHERE id = %s AND user_id = %s", (session_id, user["id"]))
    if not cur.fetchone():
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    cur.execute(
        "SELECT role, content, tools_used FROM messages WHERE session_id = %s ORDER BY id ASC",
        (session_id,),
    )
    msgs = cur.fetchall()
    cur.close()
    conn.close()
    return msgs


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE id = %s AND user_id = %s", (session_id, user["id"]))
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "deleted"}

def generate_with_fallback(contents, config):
    last_err = None
    for model_name in MODELS_CHAIN:
        try:
            res = gemini_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            if res and res.text:
                return res.text
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"AI Error: {last_err}")


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    if not gemini_client:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY missing on server")

    unlocked = json.loads(user["unlocked_modes"] or '["standard"]')
    active_mode = req.mode if req.mode in unlocked else "standard"

    conn = get_db()
    cur = conn.cursor()

    session_id = req.session_id
    if not session_id:
        session_id = str(uuid.uuid4())
        title = req.message.strip()[:42] or "New Task"
        cur.execute(
            "INSERT INTO sessions (id, user_id, title) VALUES (%s, %s, %s)",
            (session_id, user["id"], title),
        )
        conn.commit()
    else:
        cur.execute("SELECT id FROM sessions WHERE id = %s AND user_id = %s", (session_id, user["id"]))
        if not cur.fetchone():
            cur.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Session not found")

    cur.execute(
        "INSERT INTO messages (session_id, role, content) VALUES (%s, 'user', %s)",
        (session_id, req.message),
    )
    conn.commit()

    cur.execute(
        "SELECT role, content FROM messages WHERE session_id = %s ORDER BY id DESC LIMIT 12",
        (session_id,),
    )
    history_rows = list(reversed(cur.fetchall()))

    cur.execute("SELECT fact FROM user_memories WHERE user_id = %s ORDER BY id DESC LIMIT 15", (user["id"],))
    mem_rows = cur.fetchall()
    cur.close()
    conn.close()

    memories_text = "\n".join(f"- {m['fact']}" for m in mem_rows) if mem_rows else "None saved yet."
    tools_used = []
    generated_files = []

    def web_search(query: str) -> str:
        """Search the live internet for real-time facts, news, prices, or current events."""
        if "Live Search" not in tools_used:
            tools_used.append("Live Search")
        if not tavily_client:
            return "Live search unavailable: TAVILY_API_KEY not configured."
        try:
            res = tavily_client.search(query=query, max_results=4)
            snippets = [f"- {r.get('title')}: {r.get('content')} ({r.get('url')})" for r in res.get("results", [])]
            return "\n".join(snippets) if snippets else "No web results found."
        except Exception as e:
            return f"Search error: {e}"

    def solve_math(expression: str) -> str:
        """Solve algebraic equations, calculus, derivatives, integrals, or arithmetic using SymPy."""
        if "SymPy Math" not in tools_used:
            tools_used.append("SymPy Math")
        try:
            x, y, z = sp.symbols("x y z")
            if "=" in expression:
                lhs, rhs = expression.split("=", 1)
                eq = sp.Eq(sp.sympify(lhs), sp.sympify(rhs))
                sol = sp.solve(eq)
                return f"Exact Solution: {sol}"
            expr = sp.sympify(expression)
            simplified = sp.simplify(expr)
            evaluated = expr.evalf()
            return f"Simplified: {simplified} | Numeric: {evaluated}"
        except Exception as e:
            return f"Math engine note: {e}"

    def save_memory(fact: str) -> str:
        """Save an important user preference, goal, or fact to long-term cloud memory."""
        if "Core Memory" not in tools_used:
            tools_used.append("Core Memory")
        c = get_db()
        cr = c.cursor()
        cr.execute("INSERT INTO user_memories (user_id, fact) VALUES (%s, %s)", (user["id"], fact.strip()))
        c.commit()
        cr.close()
        c.close()
        return f"Saved to long-term memory: {fact}"

    def create_downloadable_file(filename: str, content: str) -> str:
        """Generate a downloadable code, markdown, CSV, or text file for the user."""
        if "File Builder" not in tools_used:
            tools_used.append("File Builder")
        safe_name = os.path.basename(filename) or "output.txt"
        generated_files.append({"filename": safe_name, "content": content})
        return f"File '{safe_name}' generated and attached for download."

    mode_directives = {
        "standard": "Be sharp, direct, structured, and zero-fluff. Never use cringey filler or robotic intros.",
        "brutal": "BRUTAL TRUTH MODE: Deliver unfiltered, razor-sharp objective critique. Expose weak assumptions and hard realities immediately without sugarcoating.",
        "architect": "ARCHITECT MODE: Think like a Principal Systems & Strategy Architect. Break down edge cases, trade-offs, system topology, and concrete execution steps.",
        "overdrive": "SOVEREIGN OVERDRIVE MODE: Combine deep multi-step reasoning, strategic foresight, and elite execution clarity.",
    }

    system_instruction = f"""You are SOVEREIGN, an autonomous agentic intelligence.
Active Reasoning Mode: {active_mode.upper()} — {mode_directives.get(active_mode, mode_directives['standard'])}
User Profile: {user['username']} | Bio: {user['bio']}
User Long-Term Memories:
{memories_text}

Rules:
- Use `web_search` whenever current facts, news, or live info are needed.
- Use `solve_math` for equations, algebra, or exact calculations.
- Use `save_memory` if the user explicitly asks you to remember something or shares an important personal preference/goal.
- Use `create_downloadable_file` when the user asks to generate/export a file, script, report, or document.
- Answer directly in the user's language."""

    contents = []
    for row in history_rows:
        role = "user" if row["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=row["content"])]))

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[web_search, solve_math, save_memory, create_downloadable_file],
        temperature=0.6,
    )

    try:
        reply_text = generate_with_fallback(contents, config)
    except Exception as e:
        reply_text = f"Temporarily overloaded—try again in a moment. ({e})"

    if generated_files:
        for gf in generated_files:
            reply_text += f"\n\n[FILE_READY::{gf['filename']}::{gf['content']}]"

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (session_id, role, content, tools_used) VALUES (%s, 'assistant', %s, %s)",
        (session_id, reply_text, json.dumps(tools_used)),
    )
    conn.commit()
    cur.close()
    conn.close()

    bonus = 2 if "overdrive" in unlocked else 0
    coin_reward = (10 if tools_used else 5) + bonus
    coin_status = award_spiritual_coins(user["id"], coin_reward)

    return {
        "session_id": session_id,
        "reply": reply_text,
        "tools_used": tools_used,
        "coins": coin_status,
    }


# --- Social & Direct Messaging ---
@app.get("/api/users/search")
def search_exact_user(username: str, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    q = username.strip()
    if not q:
        return {"user": None}
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, username, bio, spiritual_coins FROM users WHERE LOWER(username) = LOWER(%s) AND id != %s",
        (q, me["id"]),
    )
    target = cur.fetchone()
    if not target:
        cur.close()
        conn.close()
        return {"user": None}

    cur.execute(
        """SELECT id, status, requester_id FROM friendships
           WHERE (requester_id = %s AND receiver_id = %s) OR (requester_id = %s AND receiver_id = %s)""",
        (me["id"], target["id"], target["id"], me["id"]),
    )
    rel = cur.fetchone()
    cur.close()
    conn.close()
    return {"user": target, "relation": rel}


@app.post("/api/friends/request")
def send_friend_request(payload: FriendRequestPayload, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (payload.username.strip(),))
    target = cur.fetchone()
    if not target or target["id"] == me["id"]:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    cur.execute(
        """SELECT id FROM friendships
           WHERE (requester_id = %s AND receiver_id = %s) OR (requester_id = %s AND receiver_id = %s)""",
        (me["id"], target["id"], target["id"], me["id"]),
    )
    if cur.fetchone():
        cur.close()
        conn.close()
        return {"status": "exists"}

    cur.execute(
        "INSERT INTO friendships (requester_id, receiver_id, status) VALUES (%s, %s, 'pending')",
        (me["id"], target["id"]),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "sent"}


@app.get("/api/friends")
def get_friends_and_requests(authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT f.id AS friendship_id, u.id, u.username, u.bio, u.spiritual_coins
           FROM friendships f
           JOIN users u ON u.id = f.requester_id
           WHERE f.receiver_id = %s AND f.status = 'pending'""",
        (me["id"],),
    )
    incoming = cur.fetchall()

    cur.execute(
        """SELECT u.id, u.username, u.bio, u.spiritual_coins
           FROM friendships f
           JOIN users u ON (u.id = CASE WHEN f.requester_id = %s THEN f.receiver_id ELSE f.requester_id END)
           WHERE (f.requester_id = %s OR f.receiver_id = %s) AND f.status = 'accepted'""",
        (me["id"], me["id"], me["id"]),
    )
    friends = cur.fetchall()
    cur.close()
    conn.close()
    return {"incoming": incoming, "friends": friends}


@app.post("/api/friends/respond")
def respond_friend_request(payload: FriendRespondPayload, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    if payload.action == "accept":
        cur.execute(
            "UPDATE friendships SET status = 'accepted' WHERE id = %s AND receiver_id = %s",
            (payload.friendship_id, me["id"]),
        )
    else:
        cur.execute(
            "DELETE FROM friendships WHERE id = %s AND receiver_id = %s",
            (payload.friendship_id, me["id"]),
        )
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "ok"}


@app.get("/api/dm/{friend_id}")
def get_dm_history(friend_id: str, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, sender_id, receiver_id, content, created_at
           FROM direct_messages
           WHERE (sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)
           ORDER BY id ASC LIMIT 100""",
        (me["id"], friend_id, friend_id, me["id"]),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.post("/api/dm")
def send_dm(payload: DMPayload, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO direct_messages (sender_id, receiver_id, content) VALUES (%s, %s, %s)",
        (me["id"], payload.receiver_id, payload.content.strip()),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "sent"}


# --- Particle Background + Floating Glass Pad + Instagram 3-Tab Dock + Gamer Profile + Tactile Audio ---
HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sovereign — Autonomous Agentic Intelligence</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    :root {
      --bg-deep: #07080C;
      --glass-bg: rgba(17, 20, 29, 0.72);
      --glass-strong: rgba(22, 26, 38, 0.85);
      --border-light: rgba(255, 255, 255, 0.09);
      --border-glow: rgba(16, 185, 129, 0.35);
      --text-main: #F3F4F6;
      --text-sub: #9CA3AF;
      --text-dim: #6B7280;
      --accent: #10B981;
      --accent-glow: rgba(16, 185, 129, 0.25);
      --gold: #F59E0B;
      --purple: #8B5CF6;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, sans-serif;
      background: var(--bg-deep);
      color: var(--text-main);
      height: 100vh;
      overflow: hidden;
      -webkit-font-smoothing: antialiased;
    }

    /* 60fps Particle Canvas */
    #particleCanvas {
      position: fixed;
      inset: 0;
      width: 100vw;
      height: 100vh;
      z-index: 0;
      pointer-events: none;
    }

    ::-webkit-scrollbar { width: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.12); border-radius: 99px; }

    /* Auth Screen */
    #authScreen {
      position: fixed; inset: 0; z-index: 100;
      display: flex; align-items: center; justify-content: center;
      padding: 20px; background: rgba(7, 8, 12, 0.78); backdrop-filter: blur(14px);
    }
    .auth-card {
      width: 100%; max-width: 390px;
      background: var(--glass-strong);
      border: 1px solid var(--border-light);
      border-radius: 24px; padding: 34px 30px;
      box-shadow: 0 24px 70px rgba(0,0,0,0.75);
    }
    .field {
      width: 100%; background: rgba(7, 8, 12, 0.85);
      border: 1px solid var(--border-light);
      border-radius: 13px; padding: 12px 15px;
      color: var(--text-main); font-size: 14px; outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    .field:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.14);
    }
    .btn-primary {
      width: 100%; padding: 12px 16px; border-radius: 13px;
      background: linear-gradient(135deg, #10B981, #059669);
      color: #032218; font-weight: 700; font-size: 14px;
      border: none; cursor: pointer;
      box-shadow: 0 8px 24px rgba(16, 185, 129, 0.25);
      transition: transform 0.12s, filter 0.15s;
    }
    .btn-primary:hover { filter: brightness(1.08); }
    .btn-primary:active { transform: scale(0.98); }
    .btn-ghost {
      width: 100%; padding: 11px 16px; border-radius: 13px;
      background: rgba(255,255,255,0.04); color: var(--text-sub);
      border: 1px solid var(--border-light); font-weight: 600; font-size: 13px;
      cursor: pointer; margin-top: 10px;
    }
    .btn-ghost:hover { background: rgba(255,255,255,0.08); color: var(--text-main); }

    /* App Shell */
    #appShell {
      position: relative; z-index: 1;
      display: none; height: 100vh; width: 100vw;
      flex-direction: column;
    }

    /* Top Floating Glass Bar */
    .top-bar {
      height: 62px; padding: 0 20px;
      display: flex; align-items: center; justify-content: space-between;
      z-index: 20;
    }
    .top-left, .top-right { display: flex; align-items: center; gap: 10px; }
    .icon-pill {
      height: 38px; padding: 0 13px; border-radius: 99px;
      background: var(--glass-bg); backdrop-filter: blur(16px);
      border: 1px solid var(--border-light);
      color: var(--text-main); font-size: 13px; font-weight: 600;
      display: flex; align-items: center; gap: 7px; cursor: pointer;
      transition: all 0.15s;
    }
    .icon-pill:hover { border-color: rgba(255,255,255,0.22); transform: translateY(-1px); }

    /* Dragon Coin Pill */
    .dragon-pill {
      height: 40px; padding: 0 14px 0 8px; border-radius: 99px;
      background: linear-gradient(135deg, rgba(24, 20, 38, 0.85), rgba(15, 18, 26, 0.9));
      backdrop-filter: blur(16px);
      border: 1px solid rgba(245, 158, 11, 0.35);
      display: flex; align-items: center; gap: 8px;
      box-shadow: 0 4px 20px rgba(139, 92, 246, 0.22);
      cursor: pointer;
    }
    .coin-val { font-weight: 800; font-size: 14px; color: #FBBF24; }
    .coin-sub { font-size: 11px; color: var(--text-sub); }

    /* Slide-Out History Drawer */
    .history-drawer {
      position: fixed; top: 68px; left: 18px; bottom: 92px; width: 270px;
      background: var(--glass-strong); backdrop-filter: blur(22px);
      border: 1px solid var(--border-light); border-radius: 22px;
      padding: 16px; z-index: 40;
      display: none; flex-direction: column;
      box-shadow: 0 24px 60px rgba(0,0,0,0.7);
    }
    .history-drawer.open { display: flex; }
    .sess-list { flex: 1; overflow-y: auto; margin-top: 12px; display: flex; flex-direction: column; gap: 4px; }
    .sess-item {
      padding: 10px 12px; border-radius: 12px; font-size: 13px; color: var(--text-sub);
      display: flex; justify-content: space-between; align-items: center; cursor: pointer;
    }
    .sess-item:hover, .sess-item.active { background: rgba(255,255,255,0.07); color: var(--text-main); }

    /* Pages Container */
    .page-area {
      flex: 1; overflow-y: auto; padding: 10px 20px 210px;
      display: flex; flex-direction: column; align-items: center;
    }
    .content-col { width: 100%; max-width: 760px; display: flex; flex-direction: column; gap: 20px; }

    /* Hero Greeting */
    .hero-center {
      margin: auto; text-align: center; max-width: 600px; padding: 36px 12px;
    }
    .hero-glow-tag {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 6px 14px; border-radius: 99px;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: #34D399; font-size: 12px; font-weight: 600; margin-bottom: 16px;
    }
    .hero-title {
      font-size: 32px; font-weight: 800; letter-spacing: -0.03em;
      background: linear-gradient(135deg, #FFFFFF 20%, #9CA3AF 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      margin-bottom: 10px;
    }
    .hero-sub { font-size: 14.5px; color: var(--text-sub); line-height: 1.6; margin-bottom: 26px; }
    .quick-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 11px; text-align: left; }
    .quick-card {
      background: var(--glass-bg); backdrop-filter: blur(14px);
      border: 1px solid var(--border-light); border-radius: 16px;
      padding: 14px 16px; cursor: pointer; transition: all 0.18s;
    }
    .quick-card:hover {
      border-color: var(--accent); transform: translateY(-2px);
      box-shadow: 0 10px 28px rgba(16, 185, 129, 0.14);
    }

    /* Chat Bubbles */
    .msg-user {
      align-self: flex-end; max-width: 82%;
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.22), rgba(6, 78, 59, 0.35));
      backdrop-filter: blur(14px);
      border: 1px solid rgba(16, 185, 129, 0.35);
      padding: 12px 17px; border-radius: 20px 20px 5px 20px;
      font-size: 14.5px; line-height: 1.55;
    }
    .msg-ai {
      display: flex; gap: 13px; align-items: flex-start; width: 100%;
      background: var(--glass-bg); backdrop-filter: blur(14px);
      border: 1px solid var(--border-light);
      padding: 16px 18px; border-radius: 20px;
    }
    .ai-orb {
      width: 30px; height: 30px; border-radius: 10px; flex-shrink: 0;
      background: linear-gradient(135deg, #10B981, #059669);
      display: flex; align-items: center; justify-content: center;
      color: #032218; font-weight: 800; font-size: 13px;
    }
    .ai-text { flex: 1; min-width: 0; font-size: 14.5px; line-height: 1.68; color: #E5E7EB; }
    .ai-text p { margin-bottom: 10px; }
    .ai-text p:last-child { margin-bottom: 0; }
    .ai-text pre {
      background: rgba(7, 8, 12, 0.9); border: 1px solid var(--border-light);
      border-radius: 12px; padding: 13px; overflow-x: auto; margin: 10px 0;
      font-family: 'JetBrains Mono', monospace; font-size: 12.5px;
    }
    .tool-chip {
      display: inline-block; font-size: 11px; font-family: 'JetBrains Mono', monospace;
      color: #34D399; background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.28);
      padding: 3px 9px; border-radius: 99px; margin: 0 6px 8px 0;
    }
    .file-dl {
      margin-top: 10px; display: inline-flex; align-items: center; gap: 10px;
      background: rgba(255,255,255,0.05); border: 1px solid var(--border-light);
      padding: 8px 13px; border-radius: 12px;
    }

    /* Floating Glass Prompt Pad (Above Bottom Dock) */
    .floating-pad-wrap {
      position: fixed; bottom: 84px; left: 0; right: 0;
      padding: 0 18px; display: flex; justify-content: center; z-index: 25;
      pointer-events: none;
    }
    .floating-pad {
      pointer-events: auto;
      width: 100%; max-width: 740px;
      background: rgba(20, 24, 35, 0.78);
      backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 24px; padding: 12px 14px 12px 18px;
      box-shadow: 0 18px 50px rgba(0, 0, 0, 0.65), 0 0 30px var(--accent-glow);
      transition: border-color 0.2s, box-shadow 0.25s, transform 0.2s;
    }
    .floating-pad:focus-within {
      border-color: var(--accent);
      transform: translateY(-2px);
    }
    .pad-textarea {
      width: 100%; background: transparent; border: none; outline: none;
      color: var(--text-main); font-family: inherit; font-size: 14.5px;
      resize: none; max-height: 120px; line-height: 1.5;
    }
    .pad-controls {
      display: flex; align-items: center; justify-content: space-between; margin-top: 8px;
    }
    .mode-pill-select {
      background: rgba(255,255,255,0.06); color: var(--text-main);
      border: 1px solid var(--border-light); border-radius: 99px;
      padding: 5px 12px; font-size: 12px; font-weight: 600; outline: none; cursor: pointer;
    }
    .mode-pill-select option { background: #11141D; color: #F3F4F6; }
    .exec-btn {
      background: linear-gradient(135deg, #10B981, #059669);
      color: #032218; border: none; border-radius: 99px;
      padding: 7px 16px; font-size: 13px; font-weight: 700;
      cursor: pointer; display: flex; align-items: center; gap: 6px;
      box-shadow: 0 4px 16px rgba(16, 185, 129, 0.3);
      transition: transform 0.12s;
    }
    .exec-btn:active { transform: scale(0.95); }
    .exec-btn:disabled { opacity: 0.45; cursor: not-allowed; }

    /* Instagram-Style Floating 3-Tab Bottom Navigation Dock */
    .ig-dock-wrap {
      position: fixed; bottom: 16px; left: 0; right: 0;
      display: flex; justify-content: center; z-index: 30; pointer-events: none;
    }
    .ig-dock {
      pointer-events: auto;
      background: rgba(16, 19, 28, 0.86);
      backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px);
      border: 1px solid rgba(255, 255, 255, 0.13);
      border-radius: 99px; padding: 6px 10px;
      display: flex; align-items: center; gap: 8px;
      box-shadow: 0 14px 40px rgba(0, 0, 0, 0.75);
    }
    .ig-tab {
      padding: 9px 22px; border-radius: 99px;
      border: none; background: transparent;
      color: var(--text-sub); font-size: 13px; font-weight: 600;
      display: flex; align-items: center; gap: 8px;
      cursor: pointer; transition: all 0.18s;
    }
    .ig-tab:hover { color: var(--text-main); }
    .ig-tab.active {
      background: rgba(255, 255, 255, 0.1);
      color: #FFFFFF;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.12);
    }

    /* Upgrade Store Cards */
    .upgrade-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
    .upg-card {
      background: var(--glass-strong); backdrop-filter: blur(18px);
      border: 1px solid var(--border-light); border-radius: 20px;
      padding: 20px; display: flex; flex-direction: column; justify-content: space-between; gap: 14px;
    }
    .upg-card.unlocked { border-color: rgba(16, 185, 129, 0.4); }

    /* Gamer-Style Profile Card */
    .gamer-banner {
      background: linear-gradient(135deg, rgba(24, 28, 44, 0.92), rgba(13, 16, 25, 0.96));
      backdrop-filter: blur(20px);
      border: 1px solid rgba(245, 158, 11, 0.28);
      border-radius: 24px; padding: 24px;
      position: relative; overflow: hidden;
      box-shadow: 0 20px 50px rgba(0,0,0,0.6);
    }
    .gamer-top { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
    .avatar-ring {
      width: 84px; height: 84px; border-radius: 50%; padding: 3px;
      background: linear-gradient(135deg, #F59E0B, #8B5CF6, #10B981);
      flex-shrink: 0; box-shadow: 0 0 25px rgba(245, 158, 11, 0.35);
    }
    .avatar-inner {
      width: 100%; height: 100%; border-radius: 50%;
      background: #0B0D13; display: flex; align-items: center; justify-content: center;
      overflow: hidden;
    }
    .rank-badge {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 12px; border-radius: 99px;
      background: rgba(245, 158, 11, 0.15);
      border: 1px solid rgba(245, 158, 11, 0.4);
      color: #FBBF24; font-size: 11.5px; font-weight: 700; letter-spacing: 0.04em;
    }
    .stat-row {
      display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 18px;
    }
    .stat-box {
      background: rgba(255,255,255,0.04); border: 1px solid var(--border-light);
      border-radius: 14px; padding: 12px; text-align: center;
    }
    .stat-num { font-size: 18px; font-weight: 800; color: #FFFFFF; }
    .stat-lbl { font-size: 11px; color: var(--text-sub); margin-top: 2px; }
    .avatar-picker { display: flex; gap: 8px; margin-top: 10px; }
    .av-opt {
      padding: 6px 12px; border-radius: 99px; font-size: 12px; font-weight: 600;
      background: rgba(255,255,255,0.05); border: 1px solid var(--border-light);
      color: var(--text-sub); cursor: pointer;
    }
    .av-opt.active { border-color: var(--accent); color: #34D399; background: rgba(16, 185, 129, 0.12); }

    /* Social DM Box */
    .dm-grid { display: grid; grid-template-columns: 220px 1fr; gap: 12px; height: 320px; }
    .glass-box {
      background: var(--glass-strong); border: 1px solid var(--border-light);
      border-radius: 18px; padding: 14px; overflow-y: auto;
    }

    @media (max-width: 680px) {
      .quick-grid, .upgrade-grid, .dm-grid { grid-template-columns: 1fr; }
      .ig-tab { padding: 9px 16px; font-size: 12.5px; }
    }
  </style>
</head>
<body>

  <canvas id="particleCanvas"></canvas>

  <!-- Auth Screen -->
  <div id="authScreen">
    <div class="auth-card">
      <div style="text-align:center;margin-bottom:22px;">
        <div style="font-size:24px;font-weight:800;letter-spacing:-0.02em;">SOVEREIGN AI</div>
        <div style="font-size:13px;color:var(--text-sub);margin-top:4px;">Autonomous Agent & Spiritual Progression</div>
      </div>
      <input id="authUser" class="field" placeholder="Username" style="margin-bottom:12px;" />
      <input id="authPass" type="password" class="field" placeholder="Password" style="margin-bottom:14px;" />
      <div id="authErr" style="color:#F87171;font-size:12.5px;margin-bottom:10px;display:none;"></div>
      <button class="btn-primary" onclick="submitAuth('login')">Enter Workspace</button>
      <button class="btn-ghost" onclick="submitAuth('register')">Create New Account</button>
    </div>
  </div>

  <!-- Main App Shell -->
  <div id="appShell">
    <!-- Top Floating Bar -->
    <header class="top-bar">
      <div class="top-left">
        <button class="icon-pill" onclick="toggleDrawer()">☰ History</button>
        <button class="icon-pill" onclick="startNewChat()">+ New</button>
      </div>

      <div class="top-right">
        <button class="icon-pill" id="soundToggle" onclick="toggleSound()">🔊 SFX</button>

        <!-- Horned Eastern Dragon Spiritual Coin Pill -->
        <div class="dragon-pill" onclick="switchTab('upgrade')" title="Open Upgrade Store">
          <svg viewBox="0 0 64 64" width="28" height="28" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <radialGradient id="cGold" cx="35%" cy="30%" r="70%">
                <stop offset="0%" stop-color="#FEF08A"/>
                <stop offset="50%" stop-color="#F59E0B"/>
                <stop offset="100%" stop-color="#92400E"/>
              </radialGradient>
              <linearGradient id="jDrag" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#6EE7B7"/>
                <stop offset="50%" stop-color="#10B981"/>
                <stop offset="100%" stop-color="#047857"/>
              </linearGradient>
            </defs>
            <circle cx="32" cy="32" r="30" fill="#18122B" stroke="#9333EA" stroke-width="2.5"/>
            <circle cx="32" cy="32" r="26.5" fill="url(#cGold)" stroke="#FDE047" stroke-width="1"/>
            <circle cx="32" cy="32" r="21.5" fill="#0B0E14" stroke="#B45309" stroke-width="1.2"/>
            <path d="M27 17 L22 11 M25 14 L21 14 M31 16 L28 10" stroke="#A7F3D0" stroke-width="1.7" stroke-linecap="round"/>
            <path d="M26 18 C30 16, 36 17, 38 20 L33 21 L37 23 C34 25, 29 24, 27 22 Z" fill="url(#jDrag)"/>
            <circle cx="32" cy="19.2" r="1.2" fill="#FDE047"/>
            <path d="M37 20 Q42 17 44 20" stroke="#6EE7B7" stroke-width="1.2" fill="none" stroke-linecap="round"/>
            <path d="M26 18 L21 19 L25 21 L20 23 L25 24" stroke="#34D399" stroke-width="1.4" fill="none" stroke-linecap="round"/>
            <path d="M27 22 C19 25, 19 33, 32 33 C44 33, 44 42, 30 43 C23 43.5, 20 40, 22 37"
                  stroke="url(#jDrag)" stroke-width="4.5" fill="none" stroke-linecap="round"/>
            <path d="M22 25 L19 24 M22 29 L19 29 M36 33 L38 30 M41 37 L44 36 M36 42 L38 45 M28 43 L27 46"
                  stroke="#A7F3D0" stroke-width="1.3" stroke-linecap="round"/>
            <path d="M28 29 L25 27 M25 27 L23 28 M25 27 L24 25" stroke="#6EE7B7" stroke-width="1.2" stroke-linecap="round"/>
            <path d="M35 38 L38 36 M38 36 L40 37 M38 36 L39 34" stroke="#6EE7B7" stroke-width="1.2" stroke-linecap="round"/>
            <path d="M22 37 L18 35 L20 39 L17 41" stroke="#34D399" stroke-width="1.4" fill="none" stroke-linecap="round"/>
          </svg>
          <div>
            <div class="coin-val" id="coinCount">0</div>
            <div class="coin-sub"><span id="dailyCount">0</span>/30 today</div>
          </div>
        </div>
      </div>
    </header>

    <!-- Slide-out Session History Drawer -->
    <div class="history-drawer" id="historyDrawer">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <span style="font-size:12px;font-weight:700;letter-spacing:0.06em;color:var(--text-sub);">PAST MISSIONS</span>
        <span style="cursor:pointer;color:var(--text-sub);" onclick="toggleDrawer()">✕</span>
      </div>
      <div class="sess-list" id="sessionList"></div>
    </div>

    <!-- TAB 1: HOME (Chat Stage) -->
    <div class="page-area" id="tabHome">
      <div class="content-col" id="chatFeed">
        <div class="hero-center" id="heroScreen">
          <div class="hero-glow-tag">⚡ Multi-Tool Autonomous Core Active</div>
          <h1 class="hero-title">Command Sovereign AI</h1>
          <p class="hero-sub">
            Live internet intelligence, symbolic mathematics, permanent cloud memory, and file synthesis. Earn Spiritual Coins with every task to upgrade your AI cores.
          </p>
          <div class="quick-grid">
            <div class="quick-card" onclick="quickPrompt('Search the latest AI breakthroughs today and give bullet points.')">
              <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">🌐 Live Web Search</div>
              <div style="font-size:12px;color:var(--text-sub);">Real-time Tavily internet lookup</div>
            </div>
            <div class="quick-card" onclick="quickPrompt('Solve 2x^2 - 9x + 4 = 0 and compute its derivative.')">
              <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">∑ Symbolic Math</div>
              <div style="font-size:12px;color:var(--text-sub);">Exact algebra & calculus engine</div>
            </div>
            <div class="quick-card" onclick="quickPrompt('Remember that my goal is to master AI engineering.')">
              <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">🧠 Cloud Memory</div>
              <div style="font-size:12px;color:var(--text-sub);">Save persistent facts permanently</div>
            </div>
            <div class="quick-card" onclick="quickPrompt('Build a clean Python FastAPI starter script and export it as server.py.')">
              <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">📦 File Synthesizer</div>
              <div style="font-size:12px;color:var(--text-sub);">Generate & download code or reports</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 2: UPGRADE STORE -->
    <div class="page-area" id="tabUpgrade" style="display:none;padding-bottom:110px;">
      <div class="content-col">
        <div>
          <h2 style="font-size:22px;font-weight:800;">Spiritual Coin Upgrades</h2>
          <p style="font-size:13.5px;color:var(--text-sub);margin-top:4px;">
            Earn up to 30 Spiritual Coins daily by running tasks and unlock higher-tier AI reasoning cores.
          </p>
        </div>

        <div class="upgrade-grid">
          <div class="upg-card unlocked">
            <div>
              <div style="font-size:11px;font-weight:700;color:#34D399;">TIER I · DEFAULT</div>
              <div style="font-size:17px;font-weight:700;margin-top:4px;">Standard Core</div>
              <div style="font-size:13px;color:var(--text-sub);margin-top:6px;">Fast, direct, multi-tool autonomous execution with emerald aura.</div>
            </div>
            <div style="font-size:12.5px;font-weight:700;color:#34D399;">✓ Equipped & Active</div>
          </div>

          <div class="upg-card" id="cardBrutal">
            <div>
              <div style="font-size:11px;font-weight:700;color:#F59E0B;">TIER II · 50 SPIRITUAL COINS</div>
              <div style="font-size:17px;font-weight:700;margin-top:4px;">Brutal Truth Core</div>
              <div style="font-size:13px;color:var(--text-sub);margin-top:6px;">Unfiltered razor-sharp critique. Exposes flaws with zero sugarcoating and gold-crimson particles.</div>
            </div>
            <button class="btn-primary" id="btnBrutal" onclick="buyUpgrade('brutal')">Unlock · 50 Coins</button>
          </div>

          <div class="upg-card" id="cardArchitect">
            <div>
              <div style="font-size:11px;font-weight:700;color:#A78BFA;">TIER III · 120 SPIRITUAL COINS</div>
              <div style="font-size:17px;font-weight:700;margin-top:4px;">Principal Architect Core</div>
              <div style="font-size:13px;color:var(--text-sub);margin-top:6px;">Deep system topology, trade-offs, and royal violet particle aura.</div>
            </div>
            <button class="btn-primary" id="btnArchitect" onclick="buyUpgrade('architect')">Unlock · 120 Coins</button>
          </div>

          <div class="upg-card" id="cardOverdrive">
            <div>
              <div style="font-size:11px;font-weight:700;color:#38BDF8;">TIER IV · 200 SPIRITUAL COINS</div>
              <div style="font-size:17px;font-weight:700;margin-top:4px;">Sovereign Overdrive (+2 Coins/Task)</div>
              <div style="font-size:13px;color:var(--text-sub);margin-top:6px;">Unlocks maximum reasoning depth and permanent +2 bonus coins per prompt.</div>
            </div>
            <button class="btn-primary" id="btnOverdrive" onclick="buyUpgrade('overdrive')">Unlock · 200 Coins</button>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 3: GAMER-STYLE PROFILE & FRIENDS -->
    <div class="page-area" id="tabProfile" style="display:none;padding-bottom:110px;">
      <div class="content-col">
        <!-- Gamer Banner Card -->
        <div class="gamer-banner">
          <div class="gamer-top">
            <div class="avatar-ring">
              <div class="avatar-inner" id="avatarDisplay"></div>
            </div>
            <div style="flex:1;min-width:200px;">
              <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <span style="font-size:22px;font-weight:800;" id="profName">@player</span>
                <span class="rank-badge" id="profRank">🐉 SPIRIT INITIATE</span>
              </div>
              <div style="font-size:13px;color:var(--text-sub);margin-top:6px;" id="profBioText">Sovereign Explorer</div>
              <div class="avatar-picker">
                <button class="av-opt" id="av-dragon" onclick="selectAvatar('dragon')">🐉 Dragon</button>
                <button class="av-opt" id="av-cyber" onclick="selectAvatar('cyber')">⚔️ Cyber</button>
                <button class="av-opt" id="av-phantom" onclick="selectAvatar('phantom')">🦊 Phantom</button>
              </div>
            </div>
          </div>

          <div class="stat-row">
            <div class="stat-box">
              <div class="stat-num" style="color:#FBBF24;" id="statCoins">0</div>
              <div class="stat-lbl">Spiritual Coins</div>
            </div>
            <div class="stat-box">
              <div class="stat-num" style="color:#34D399;" id="statModes">1 / 4</div>
              <div class="stat-lbl">Cores Unlocked</div>
            </div>
            <div class="stat-box">
              <div class="stat-num" style="color:#A78BFA;" id="statMissions">0</div>
              <div class="stat-lbl">Missions Done</div>
            </div>
          </div>

          <div style="display:flex;gap:8px;margin-top:16px;">
            <input id="bioInput" class="field" placeholder="Update status bio..." />
            <button class="btn-primary" style="width:auto;white-space:nowrap;" onclick="saveProfile()">Save</button>
            <button class="btn-ghost" style="width:auto;margin:0;color:#F87171;" onclick="logout()">Logout</button>
          </div>
        </div>

        <!-- Long-Term Memories -->
        <div class="glass-box">
          <div style="font-size:14px;font-weight:700;margin-bottom:8px;">🧠 Long-Term Cloud Memory</div>
          <div id="memBox" style="font-size:13px;color:var(--text-sub);line-height:1.7;">No memories stored yet.</div>
        </div>

        <!-- Friends & Direct Messaging -->
        <div class="glass-box">
          <div style="font-size:14px;font-weight:700;margin-bottom:10px;">👥 Squad & Direct Messages</div>
          <div style="display:flex;gap:8px;margin-bottom:12px;">
            <input id="friendSearchInp" class="field" placeholder="Search exact username..." />
            <button class="btn-primary" style="width:auto;white-space:nowrap;" onclick="searchExactFriend()">Find</button>
          </div>
          <div id="searchOut" style="margin-bottom:10px;"></div>
          <div id="reqOut" style="margin-bottom:10px;"></div>

          <div class="dm-grid">
            <div style="border-right:1px solid var(--border-light);padding-right:10px;overflow-y:auto;">
              <div style="font-size:11px;font-weight:700;color:var(--text-dim);margin-bottom:8px;">CONNECTED FRIENDS</div>
              <div id="friendsContainer"></div>
            </div>
            <div style="display:flex;flex-direction:column;height:100%;">
              <div id="dmTitle" style="font-size:12.5px;font-weight:700;padding-bottom:8px;border-bottom:1px solid var(--border-light);">
                Select a friend to message
              </div>
              <div id="dmFeed" style="flex:1;overflow-y:auto;padding:10px 0;display:flex;flex-direction:column;gap:6px;"></div>
              <div style="display:flex;gap:6px;">
                <input id="dmText" class="field" style="padding:8px 12px;font-size:13px;" placeholder="Send DM..." onkeydown="if(event.key==='Enter') sendDM()" />
                <button class="btn-primary" style="width:auto;padding:8px 14px;" onclick="sendDM()">Send</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Floating Glass Prompt Pad (Visible on Home Tab) -->
    <div class="floating-pad-wrap" id="floatingPadWrap">
      <div class="floating-pad">
        <textarea id="promptInput" class="pad-textarea" rows="1" placeholder="Ask Sovereign anything — search, compute, remember, or build..." onkeydown="handleKey(event)"></textarea>
        <div class="pad-controls">
          <select id="modeSelect" class="mode-pill-select" onchange="onModeChange()">
            <option value="standard">🟢 Standard Core</option>
            <option value="brutal" id="optBrutal" disabled>🔒 Brutal Truth (50 Coins)</option>
            <option value="architect" id="optArchitect" disabled>🔒 Architect Mode (120 Coins)</option>
            <option value="overdrive" id="optOverdrive" disabled>🔒 Overdrive Mode (200 Coins)</option>
          </select>
          <button class="exec-btn" id="sendBtn" onclick="sendMessage()">
            <span>Execute</span>
            <span>↑</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Instagram-Style Floating 3-Tab Bottom Dock -->
    <div class="ig-dock-wrap">
      <nav class="ig-dock">
        <button class="ig-tab active" id="nav-home" onclick="switchTab('home')">
          <span>🏠</span><span>Home</span>
        </button>
        <button class="ig-tab" id="nav-upgrade" onclick="switchTab('upgrade')">
          <span>⚡</span><span>Upgrade</span>
        </button>
        <button class="ig-tab" id="nav-profile" onclick="switchTab('profile')">
          <span>👤</span><span>Profile</span>
        </button>
      </nav>
    </div>
  </div>

<script>
  // --- Tactile Web Audio Synthesizer (Zero External Audio Lag) ---
  let soundEnabled = true;
  let audioCtx = null;

  function ensureAudio() {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
  }

  function toggleSound() {
    soundEnabled = !soundEnabled;
    document.getElementById('soundToggle').textContent = soundEnabled ? '🔊 SFX' : '🔇 Muted';
    if (soundEnabled) playPopSound();
  }

  function playPopSound() {
    if (!soundEnabled) return;
    ensureAudio();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(340, audioCtx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(620, audioCtx.currentTime + 0.09);
    gain.gain.setValueAtTime(0.08, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.09);
    osc.connect(gain); gain.connect(audioCtx.destination);
    osc.start(); osc.stop(audioCtx.currentTime + 0.09);
  }

  function playChimeSound() {
    if (!soundEnabled) return;
    ensureAudio();
    [523.25, 659.25, 783.99].forEach((freq, i) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'triangle';
      const t = audioCtx.currentTime + i * 0.06;
      osc.frequency.setValueAtTime(freq, t);
      gain.gain.setValueAtTime(0.07, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.28);
      osc.connect(gain); gain.connect(audioCtx.destination);
      osc.start(t); osc.stop(t + 0.28);
    });
  }

  function playCoinSound() {
    if (!soundEnabled) return;
    ensureAudio();
    [987.77, 1318.51].forEach((freq, i) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      const t = audioCtx.currentTime + i * 0.07;
      osc.frequency.setValueAtTime(freq, t);
      gain.gain.setValueAtTime(0.09, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
      osc.connect(gain); gain.connect(audioCtx.destination);
      osc.start(t); osc.stop(t + 0.25);
    });
  }

  // --- 60fps Glowing Particle Background ---
  const canvas = document.getElementById('particleCanvas');
  const ctx = canvas.getContext('2d');
  let particles = [];
  let particleTheme = 'rgba(16, 185, 129, ';

  function resizeCanvas() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
  }
  window.addEventListener('resize', resizeCanvas);
  resizeCanvas();

  for (let i = 0; i < 65; i++) {
    particles.push({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      r: Math.random() * 2.1 + 0.6,
      vx: (Math.random() - 0.5) * 0.35,
      vy: -Math.random() * 0.45 - 0.12,
      alpha: Math.random() * 0.55 + 0.15
    });
  }

  function animateParticles() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const p of particles) {
      p.x += p.vx;
      p.y += p.vy;
      if (p.y < -10) { p.y = canvas.height + 10; p.x = Math.random() * canvas.width; }
      if (p.x < -10) p.x = canvas.width + 10;
      if (p.x > canvas.width + 10) p.x = -10;

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = particleTheme + p.alpha + ')';
      ctx.shadowBlur = 10;
      ctx.shadowColor = particleTheme + '0.8)';
      ctx.fill();
    }
    requestAnimationFrame(animateParticles);
  }
  animateParticles();

  function onModeChange() {
    const m = document.getElementById('modeSelect').value;
    const root = document.documentElement.style;
    if (m === 'brutal') {
      particleTheme = 'rgba(245, 158, 11, ';
      root.setProperty('--accent', '#F59E0B');
      root.setProperty('--accent-glow', 'rgba(245, 158, 11, 0.25)');
    } else if (m === 'architect') {
      particleTheme = 'rgba(139, 92, 246, ';
      root.setProperty('--accent', '#8B5CF6');
      root.setProperty('--accent-glow', 'rgba(139, 92, 246, 0.28)');
    } else if (m === 'overdrive') {
      particleTheme = 'rgba(56, 189, 248, ';
      root.setProperty('--accent', '#38BDF8');
      root.setProperty('--accent-glow', 'rgba(56, 189, 248, 0.28)');
    } else {
      particleTheme = 'rgba(16, 185, 129, ';
      root.setProperty('--accent', '#10B981');
      root.setProperty('--accent-glow', 'rgba(16, 185, 129, 0.25)');
    }
    playPopSound();
  }

  // --- App State & API Logic ---
  let token = localStorage.getItem('sov_token') || '';
  let currentUser = null;
  let currentSessionId = null;
  let activeFriendId = null;
  let selectedAvatar = 'dragon';
  let dmInterval = null;

  const AVATARS = {
    dragon: `<svg viewBox="0 0 80 80" width="76" height="76"><circle cx="40" cy="40" r="38" fill="#0F172A"/><path d="M25 58 C25 42, 55 42, 55 58" fill="#10B981"/><circle cx="40" cy="31" r="13" fill="#34D399"/><path d="M29 22 L22 12 M51 22 L58 12" stroke="#FBBF24" stroke-width="3" stroke-linecap="round"/><circle cx="35" cy="30" r="2.2" fill="#090A0D"/><circle cx="45" cy="30" r="2.2" fill="#090A0D"/></svg>`,
    cyber: `<svg viewBox="0 0 80 80" width="76" height="76"><circle cx="40" cy="40" r="38" fill="#111827"/><path d="M22 60 C24 44, 56 44, 58 60" fill="#6366F1"/><circle cx="40" cy="31" r="13" fill="#A5B4FC"/><rect x="29" y="27" width="22" height="6" rx="3" fill="#38BDF8"/></svg>`,
    phantom: `<svg viewBox="0 0 80 80" width="76" height="76"><circle cx="40" cy="40" r="38" fill="#1E1B4B"/><path d="M24 60 C24 43, 56 43, 56 60" fill="#F59E0B"/><polygon points="27,25 32,12 38,22" fill="#FBBF24"/><polygon points="53,25 48,12 42,22" fill="#FBBF24"/><circle cx="40" cy="32" r="12" fill="#FDE68A"/><circle cx="36" cy="31" r="2" fill="#111"/><circle cx="44" cy="31" r="2" fill="#111"/></svg>`
  };

  function headers() {
    return { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token };
  }

  async function submitAuth(type) {
    const username = document.getElementById('authUser').value.trim();
    const password = document.getElementById('authPass').value.trim();
    const errEl = document.getElementById('authErr');
    errEl.style.display = 'none';
    try {
      const res = await fetch('/api/auth/' + type, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Auth error');
      token = data.token;
      localStorage.setItem('sov_token', token);
      playChimeSound();
      boot();
    } catch (e) {
      errEl.textContent = e.message;
      errEl.style.display = 'block';
    }
  }

  function logout() {
    localStorage.removeItem('sov_token');
    location.reload();
  }

  async function boot() {
    if (!token) return;
    const res = await fetch('/api/me', { headers: headers() });
    if (!res.ok) { logout(); return; }
    currentUser = await res.json();
    document.getElementById('authScreen').style.display = 'none';
    document.getElementById('appShell').style.display = 'flex';
    refreshUserUI();
    loadSessions();
  }

  function getRankTitle(coins) {
    if (coins >= 300) return '👑 MYTHIC SOVEREIGN';
    if (coins >= 150) return '🔥 DRAGON ASCENDANT';
    if (coins >= 50) return '⚔️ SPIRIT ADEPT';
    return '🐉 SPIRIT INITIATE';
  }

  function refreshUserUI() {
    if (!currentUser) return;
    const coins = currentUser.spiritual_coins || 0;
    document.getElementById('coinCount').textContent = coins;
    document.getElementById('dailyCount').textContent = currentUser.daily_coins_earned || 0;

    document.getElementById('profName').textContent = '@' + currentUser.username;
    document.getElementById('profBioText').textContent = currentUser.bio || 'Sovereign Explorer';
    document.getElementById('bioInput').value = currentUser.bio || '';
    document.getElementById('profRank').textContent = getRankTitle(coins);
    document.getElementById('statCoins').textContent = coins;
    document.getElementById('statMissions').textContent = currentUser.session_count || 0;

    selectedAvatar = currentUser.avatar_seed || 'dragon';
    document.getElementById('avatarDisplay').innerHTML = AVATARS[selectedAvatar] || AVATARS.dragon;
    ['dragon', 'cyber', 'phantom'].forEach(k => {
      document.getElementById('av-' + k).classList.toggle('active', k === selectedAvatar);
    });

    const modes = currentUser.unlocked_modes || ['standard'];
    document.getElementById('statModes').textContent = `${modes.length} / 4`;

    if (modes.includes('brutal')) {
      document.getElementById('optBrutal').disabled = false;
      document.getElementById('optBrutal').textContent = '🟠 Brutal Truth Core';
      document.getElementById('btnBrutal').textContent = '✓ Unlocked';
      document.getElementById('btnBrutal').disabled = true;
      document.getElementById('cardBrutal').classList.add('unlocked');
    }
    if (modes.includes('architect')) {
      document.getElementById('optArchitect').disabled = false;
      document.getElementById('optArchitect').textContent = '🟣 Principal Architect';
      document.getElementById('btnArchitect').textContent = '✓ Unlocked';
      document.getElementById('btnArchitect').disabled = true;
      document.getElementById('cardArchitect').classList.add('unlocked');
    }
    if (modes.includes('overdrive')) {
      document.getElementById('optOverdrive').disabled = false;
      document.getElementById('optOverdrive').textContent = '🔵 Sovereign Overdrive';
      document.getElementById('btnOverdrive').textContent = '✓ Unlocked';
      document.getElementById('btnOverdrive').disabled = true;
      document.getElementById('cardOverdrive').classList.add('unlocked');
    }

    const memBox = document.getElementById('memBox');
    if (currentUser.memories && currentUser.memories.length) {
      memBox.innerHTML = currentUser.memories.map(m => `<div>• ${escapeHtml(m.fact)}</div>`).join('');
    }
  }

  function selectAvatar(seed) {
    selectedAvatar = seed;
    document.getElementById('avatarDisplay').innerHTML = AVATARS[seed];
    ['dragon', 'cyber', 'phantom'].forEach(k => {
      document.getElementById('av-' + k).classList.toggle('active', k === seed);
    });
    playPopSound();
  }

  async function saveProfile() {
    const bio = document.getElementById('bioInput').value;
    await fetch('/api/me/profile', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ bio, avatar_seed: selectedAvatar })
    });
    currentUser.bio = bio;
    currentUser.avatar_seed = selectedAvatar;
    refreshUserUI();
    playChimeSound();
  }

  function switchTab(tab) {
    playPopSound();
    document.getElementById('tabHome').style.display = tab === 'home' ? 'flex' : 'none';
    document.getElementById('tabUpgrade').style.display = tab === 'upgrade' ? 'flex' : 'none';
    document.getElementById('tabProfile').style.display = tab === 'profile' ? 'flex' : 'none';
    document.getElementById('floatingPadWrap').style.display = tab === 'home' ? 'flex' : 'none';

    ['home', 'upgrade', 'profile'].forEach(t => {
      document.getElementById('nav-' + t).classList.toggle('active', t === tab);
    });
    document.getElementById('historyDrawer').classList.remove('open');

    if (dmInterval) { clearInterval(dmInterval); dmInterval = null; }
    if (tab === 'profile') {
      loadFriends();
      dmInterval = setInterval(() => { if (activeFriendId) loadDMs(activeFriendId); }, 3500);
    }
  }

  function toggleDrawer() {
    playPopSound();
    document.getElementById('historyDrawer').classList.toggle('open');
  }

  async function loadSessions() {
    const res = await fetch('/api/sessions', { headers: headers() });
    const rows = await res.json();
    const list = document.getElementById('sessionList');
    list.innerHTML = rows.map(s => `
      <div class="sess-item ${s.id === currentSessionId ? 'active' : ''}" onclick="openSession('${s.id}')">
        <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:190px;">${escapeHtml(s.title)}</span>
        <span style="color:var(--text-dim);padding:0 4px;" onclick="delSession(event, '${s.id}')">×</span>
      </div>
    `).join('');
  }

  function startNewChat() {
    currentSessionId = null;
    switchTab('home');
    const feed = document.getElementById('chatFeed');
    feed.innerHTML = `
      <div class="hero-center" id="heroScreen">
        <div class="hero-glow-tag">⚡ Multi-Tool Autonomous Core Active</div>
        <h1 class="hero-title">Command Sovereign AI</h1>
        <p class="hero-sub">Live internet intelligence, symbolic mathematics, permanent cloud memory, and file synthesis. Earn Spiritual Coins with every task to upgrade your AI cores.</p>
        <div class="quick-grid">
          <div class="quick-card" onclick="quickPrompt('Search the latest AI breakthroughs today and give bullet points.')">
            <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">🌐 Live Web Search</div>
            <div style="font-size:12px;color:var(--text-sub);">Real-time Tavily internet lookup</div>
          </div>
          <div class="quick-card" onclick="quickPrompt('Solve 2x^2 - 9x + 4 = 0 and compute its derivative.')">
            <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">∑ Symbolic Math</div>
            <div style="font-size:12px;color:var(--text-sub);">Exact algebra & calculus engine</div>
          </div>
          <div class="quick-card" onclick="quickPrompt('Remember that my goal is to master AI engineering.')">
            <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">🧠 Cloud Memory</div>
            <div style="font-size:12px;color:var(--text-sub);">Save persistent facts permanently</div>
          </div>
          <div class="quick-card" onclick="quickPrompt('Build a clean Python FastAPI starter script and export it as server.py.')">
            <div style="font-weight:700;font-size:13.5px;margin-bottom:4px;">📦 File Synthesizer</div>
            <div style="font-size:12px;color:var(--text-sub);">Generate & download code or reports</div>
          </div>
        </div>
      </div>`;
    loadSessions();
  }

  function quickPrompt(txt) {
    document.getElementById('promptInput').value = txt;
    sendMessage();
  }

  async function openSession(id) {
    currentSessionId = id;
    switchTab('home');
    loadSessions();
    const res = await fetch('/api/sessions/' + id, { headers: headers() });
    const msgs = await res.json();
    const feed = document.getElementById('chatFeed');
    feed.innerHTML = '';
    msgs.forEach(m => renderMessage(m.role, m.content, JSON.parse(m.tools_used || '[]')));
  }

  async function delSession(ev, id) {
    ev.stopPropagation();
    await fetch('/api/sessions/' + id, { method: 'DELETE', headers: headers() });
    if (currentSessionId === id) startNewChat();
    else loadSessions();
  }

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  async function sendMessage() {
    const inp = document.getElementById('promptInput');
    const text = inp.value.trim();
    if (!text) return;

    document.getElementById('heroScreen')?.remove();
    inp.value = '';
    playPopSound();
    renderMessage('user', text, []);

    const btn = document.getElementById('sendBtn');
    btn.disabled = true;

    const loadId = 'ld-' + Date.now();
    const feed = document.getElementById('chatFeed');
    const ldDiv = document.createElement('div');
    ldDiv.id = loadId;
    ldDiv.className = 'msg-ai';
    ldDiv.innerHTML = `<div class="ai-orb">⚡</div><div class="ai-text" style="color:var(--text-sub);">Reasoning & executing tools...</div>`;
    feed.appendChild(ldDiv);
    scrollBottom();

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({
          session_id: currentSessionId,
          message: text,
          mode: document.getElementById('modeSelect').value
        })
      });
      const data = await res.json();
      document.getElementById(loadId)?.remove();

      if (!res.ok) throw new Error(data.detail || 'Server error');
      currentSessionId = data.session_id;
      renderMessage('assistant', data.reply, data.tools_used || []);
      playChimeSound();

      if (data.coins) {
        currentUser.spiritual_coins = data.coins.coins;
        currentUser.daily_coins_earned = data.coins.daily;
        refreshUserUI();
        if (data.coins.added > 0) setTimeout(playCoinSound, 220);
      }
      loadSessions();
    } catch (e) {
      document.getElementById(loadId)?.remove();
      renderMessage('assistant', 'Error: ' + e.message, []);
    } finally {
      btn.disabled = false;
    }
  }

  function renderMessage(role, content, tools) {
    const feed = document.getElementById('chatFeed');
    if (role === 'user') {
      const div = document.createElement('div');
      div.className = 'msg-user';
      div.textContent = content;
      feed.appendChild(div);
    } else {
      const files = [];
      const regex = /\[FILE_READY::(.*?)::([\s\S]*?)\]/g;
      let m;
      while ((m = regex.exec(content)) !== null) {
        files.push({ filename: m[1], content: m[2] });
      }
      const clean = content.replace(regex, '').trim();

      const chips = tools.map(t => `<span class="tool-chip">✓ ${escapeHtml(t)}</span>`).join('');
      const dlCards = files.map(f => {
        const b64 = btoa(unescape(encodeURIComponent(f.content)));
        return `<div class="file-dl">
          <span style="font-size:12.5px;font-family:'JetBrains Mono',monospace;">📄 ${escapeHtml(f.filename)}</span>
          <button class="exec-btn" style="padding:5px 11px;font-size:11.5px;" onclick="dlFile('${escapeHtml(f.filename)}','${b64}')">Download</button>
        </div>`;
      }).join('');

      const div = document.createElement('div');
      div.className = 'msg-ai';
      div.innerHTML = `
        <div class="ai-orb">S</div>
        <div class="ai-text">
          <div>${chips}</div>
          <div>${marked.parse(clean)}</div>
          ${dlCards}
        </div>`;
      feed.appendChild(div);
    }
    scrollBottom();
  }

  function dlFile(name, b64) {
    const txt = decodeURIComponent(escape(atob(b64)));
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([txt], { type: 'text/plain;charset=utf-8' }));
    a.download = name;
    a.click();
    playPopSound();
  }

  function scrollBottom() {
    const area = document.getElementById('tabHome');
    area.scrollTop = area.scrollHeight;
  }

  async function buyUpgrade(mode) {
    const res = await fetch('/api/modes/unlock', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ mode })
    });
    const data = await res.json();
    if (!res.ok) { alert(data.detail); return; }
    currentUser.spiritual_coins = data.spiritual_coins;
    currentUser.unlocked_modes = data.unlocked_modes;
    playCoinSound();
    refreshUserUI();
  }

  async function searchExactFriend() {
    const q = document.getElementById('friendSearchInp').value.trim();
    if (!q) return;
    const res = await fetch('/api/users/search?username=' + encodeURIComponent(q), { headers: headers() });
    const data = await res.json();
    const out = document.getElementById('searchOut');
    if (!data.user) {
      out.innerHTML = `<div style="font-size:12.5px;color:var(--text-sub);">No exact user found.</div>`;
      return;
    }
    const u = data.user;
    let btn = `<button class="exec-btn" onclick="sendFriendReq('${escapeHtml(u.username)}')">+ Add Friend</button>`;
    if (data.relation) btn = `<span style="font-size:12px;color:#34D399;">Status: ${data.relation.status}</span>`;
    out.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;padding:10px;background:rgba(255,255,255,0.04);border-radius:12px;">
      <span><strong>@${escapeHtml(u.username)}</strong> · <span style="color:#FBBF24;">${u.spiritual_coins} Coins</span></span>
      ${btn}
    </div>`;
  }

  async function sendFriendReq(username) {
    await fetch('/api/friends/request', { method: 'POST', headers: headers(), body: JSON.stringify({ username }) });
    playPopSound();
    searchExactFriend();
  }

  async function loadFriends() {
    const res = await fetch('/api/friends', { headers: headers() });
    const data = await res.json();
    document.getElementById('reqOut').innerHTML = data.incoming.map(r => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:rgba(16,185,129,0.1);border-radius:10px;margin-bottom:6px;">
        <span style="font-size:12.5px;">Request from <strong>@${escapeHtml(r.username)}</strong></span>
        <div style="display:flex;gap:6px;">
          <button class="exec-btn" style="padding:4px 10px;font-size:11.5px;" onclick="respondFriend(${r.friendship_id},'accept')">Accept</button>
          <button class="btn-ghost" style="width:auto;margin:0;padding:4px 10px;font-size:11.5px;" onclick="respondFriend(${r.friendship_id},'decline')">Decline</button>
        </div>
      </div>`).join('');

    document.getElementById('friendsContainer').innerHTML = data.friends.length
      ? data.friends.map(f => `<div class="sess-item ${f.id === activeFriendId ? 'active' : ''}" onclick="openDM('${f.id}','${escapeHtml(f.username)}')">@${escapeHtml(f.username)}</div>`).join('')
      : `<div style="font-size:12px;color:var(--text-dim);">No friends yet.</div>`;
  }

  async function respondFriend(id, action) {
    await fetch('/api/friends/respond', { method: 'POST', headers: headers(), body: JSON.stringify({ friendship_id: id, action }) });
    playChimeSound();
    loadFriends();
  }

  function openDM(id, name) {
    activeFriendId = id;
    document.getElementById('dmTitle').textContent = '@' + name;
    loadDMs(id);
  }

  async function loadDMs(id) {
    const res = await fetch('/api/dm/' + id, { headers: headers() });
    const msgs = await res.json();
    const feed = document.getElementById('dmFeed');
    feed.innerHTML = msgs.map(m => `
      <div style="align-self:${m.sender_id === currentUser.id ? 'flex-end' : 'flex-start'};
        background:${m.sender_id === currentUser.id ? 'rgba(16,185,129,0.25)' : 'rgba(255,255,255,0.07)'};
        padding:7px 12px;border-radius:12px;font-size:13px;max-width:78%;">
        ${escapeHtml(m.content)}
      </div>`).join('');
    feed.scrollTop = feed.scrollHeight;
  }

  async function sendDM() {
    if (!activeFriendId) return;
    const inp = document.getElementById('dmText');
    const content = inp.value.trim();
    if (!content) return;
    inp.value = '';
    playPopSound();
    await fetch('/api/dm', { method: 'POST', headers: headers(), body: JSON.stringify({ receiver_id: activeFriendId, content }) });
    loadDMs(activeFriendId);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, m => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m]));
  }

  boot();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def serve_home():
    return HTML_UI