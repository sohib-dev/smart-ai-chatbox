import os
import json
import uuid
import time
import hashlib
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2 import pool
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
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
]

app = FastAPI(title="Sovereign Agentic AI")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

# --- Connection Pool for Fast & Glitch-Free DB Access ---
db_pool = None
if DATABASE_URL:
    try:
        db_pool = pool.ThreadedConnectionPool(1, 12, dsn=DATABASE_URL)
    except Exception as e:
        print("Pool warning:", e)


@contextmanager
def get_db_cursor():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")
    conn = None
    from_pool = False
    try:
        if db_pool:
            conn = db_pool.getconn()
            from_pool = True
        else:
            conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        yield conn, cur
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            try:
                cur.close()
            except Exception:
                pass
            if from_pool and db_pool:
                db_pool.putconn(conn)
            else:
                conn.close()


def init_db():
    if not DATABASE_URL:
        return
    with get_db_cursor() as (conn, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                auth_token TEXT UNIQUE,
                bio TEXT DEFAULT 'Sovereign Explorer',
                avatar_seed TEXT DEFAULT '',
                spiritual_coins INTEGER DEFAULT 0,
                daily_coins_earned INTEGER DEFAULT 0,
                last_coin_date TEXT DEFAULT '',
                unlocked_modes TEXT DEFAULT '["standard"]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_seed TEXT DEFAULT '';")
        cur.execute("ALTER TABLE users ALTER COLUMN avatar_seed TYPE TEXT;")
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
                files_json TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS files_json TEXT DEFAULT '[]';")
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
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")

    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM users WHERE auth_token = %s", (token,))
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="Session expired, please login again")

        user = dict(user)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if user["last_coin_date"] != today_str:
            cur.execute(
                "UPDATE users SET daily_coins_earned = 0, last_coin_date = %s WHERE id = %s",
                (today_str, user["id"]),
            )
            user["daily_coins_earned"] = 0
            user["last_coin_date"] = today_str
        return user


def award_spiritual_coins(user_id: str, amount: int) -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "SELECT spiritual_coins, daily_coins_earned, last_coin_date FROM users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
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
        return {"coins": new_total, "daily": new_daily, "added": actual_add}


# --- Pydantic Models ---
class AuthRequest(BaseModel):
    username: str
    password: str


class ProfileUpdate(BaseModel):
    bio: str
    avatar_data: Optional[str] = None


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
        raise HTTPException(status_code=400, detail="Username min 3 chars & password min 4 chars required")
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (uname,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Username already taken")

        uid = str(uuid.uuid4())
        token = secrets.token_hex(32)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur.execute(
            """INSERT INTO users (id, username, password_hash, auth_token, last_coin_date, avatar_seed)
               VALUES (%s, %s, %s, %s, %s, '')""",
            (uid, uname, hash_password(req.password), token, today_str),
        )
        return {"token": token, "username": uname}


@app.post("/api/auth/login")
def login(req: AuthRequest):
    uname = req.username.strip()
    if not uname or not req.password:
        raise HTTPException(status_code=400, detail="Enter username and password")
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "SELECT id, username, password_hash, auth_token FROM users WHERE LOWER(username) = LOWER(%s)",
            (uname,),
        )
        user = cur.fetchone()
        if not user or user["password_hash"] != hash_password(req.password):
            raise HTTPException(status_code=401, detail="Wrong username or password")

        token = user["auth_token"] or secrets.token_hex(32)
        if not user["auth_token"]:
            cur.execute("UPDATE users SET auth_token = %s WHERE id = %s", (token, user["id"]))
        return {"token": token, "username": user["username"]}


@app.get("/api/me")
def get_me(authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT id, fact FROM user_memories WHERE user_id = %s ORDER BY id DESC", (user["id"],))
        memories = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS cnt FROM sessions WHERE user_id = %s", (user["id"],))
        sess_count = cur.fetchone()["cnt"]
        avatar = user.get("avatar_seed") or ""
        if not avatar.startswith("data:image/"):
            avatar = ""
        return {
            "id": user["id"],
            "username": user["username"],
            "bio": user["bio"],
            "avatar_data": avatar,
            "spiritual_coins": user["spiritual_coins"],
            "daily_coins_earned": user["daily_coins_earned"],
            "unlocked_modes": json.loads(user["unlocked_modes"] or '["standard"]'),
            "memories": memories,
            "session_count": sess_count,
        }


@app.delete("/api/memories/{mem_id}")
def delete_memory(mem_id: int, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        cur.execute("DELETE FROM user_memories WHERE id = %s AND user_id = %s", (mem_id, user["id"]))
        return {"status": "deleted"}


@app.post("/api/me/profile")
def update_profile(payload: ProfileUpdate, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        if payload.avatar_data is not None:
            if payload.avatar_data != "" and not payload.avatar_data.startswith("data:image/"):
                raise HTTPException(status_code=400, detail="Invalid image format")
            if len(payload.avatar_data) > 450000:
                raise HTTPException(status_code=400, detail="Image too large")
            cur.execute(
                "UPDATE users SET bio = %s, avatar_seed = %s WHERE id = %s",
                (payload.bio[:200], payload.avatar_data, user["id"]),
            )
        else:
            cur.execute(
                "UPDATE users SET bio = %s WHERE id = %s",
                (payload.bio[:200], user["id"]),
            )
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
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "UPDATE users SET spiritual_coins = %s, unlocked_modes = %s WHERE id = %s",
            (new_coins, json.dumps(unlocked), user["id"]),
        )
        return {"unlocked_modes": unlocked, "spiritual_coins": new_coins}


# --- Sessions & Chat ---
@app.get("/api/sessions")
def list_sessions(authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "SELECT id, title, created_at FROM sessions WHERE user_id = %s ORDER BY created_at DESC",
            (user["id"],),
        )
        return cur.fetchall()


@app.get("/api/sessions/{session_id}")
def get_session_messages(session_id: str, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM sessions WHERE id = %s AND user_id = %s", (session_id, user["id"]))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Session not found")

        cur.execute(
            "SELECT role, content, tools_used, files_json FROM messages WHERE session_id = %s ORDER BY id ASC",
            (session_id,),
        )
        return cur.fetchall()


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        cur.execute("DELETE FROM sessions WHERE id = %s AND user_id = %s", (session_id, user["id"]))
        return {"status": "deleted"}


def generate_with_fallback(contents, config):
    last_err = None
    for model_name in MODELS_CHAIN:
        for attempt in range(2):
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
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str:
                    time.sleep(0.8)
                    continue
                break
    raise RuntimeError(f"All AI cores busy right now ({last_err})")


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest, authorization: Optional[str] = Header(None)):
    user = get_current_user(authorization)
    if not gemini_client:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY missing on server")

    unlocked = json.loads(user["unlocked_modes"] or '["standard"]')
    active_mode = req.mode if req.mode in unlocked else "standard"

    with get_db_cursor() as (conn, cur):
        session_id = req.session_id
        if not session_id:
            session_id = str(uuid.uuid4())
            title = req.message.strip()[:42] or "New Task"
            cur.execute(
                "INSERT INTO sessions (id, user_id, title) VALUES (%s, %s, %s)",
                (session_id, user["id"], title),
            )
        else:
            cur.execute("SELECT id FROM sessions WHERE id = %s AND user_id = %s", (session_id, user["id"]))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Session not found")

        cur.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (%s, 'user', %s)",
            (session_id, req.message),
        )

        cur.execute(
            "SELECT role, content FROM messages WHERE session_id = %s ORDER BY id DESC LIMIT 12",
            (session_id,),
        )
        history_rows = list(reversed(cur.fetchall()))

        cur.execute("SELECT fact FROM user_memories WHERE user_id = %s ORDER BY id DESC LIMIT 15", (user["id"],))
        mem_rows = cur.fetchall()

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
        clean_fact = fact.strip()
        with get_db_cursor() as (c, cr):
            cr.execute(
                "SELECT id FROM user_memories WHERE user_id = %s AND LOWER(fact) = LOWER(%s)",
                (user["id"], clean_fact),
            )
            if not cr.fetchone():
                cr.execute("INSERT INTO user_memories (user_id, fact) VALUES (%s, %s)", (user["id"], clean_fact))
        return f"Saved to long-term memory: {clean_fact}"

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

    system_instruction = f"""You are SOVEREIGN, an autonomous agentic intelligence built and created by Sohib (@sohib-dev).
CRITICAL IDENTITY RULE:
- You were created, engineered, and built by Sohib (also known as Sohib28 / @sohib-dev).
- NEVER say you were created, trained, or made by Google, DeepMind, or anyone else. If anyone asks who created/made/built you, always proudly state that Sohib created you.

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

    with get_db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO messages (session_id, role, content, tools_used, files_json) VALUES (%s, 'assistant', %s, %s, %s)",
            (session_id, reply_text, json.dumps(tools_used), json.dumps(generated_files)),
        )

    bonus = 2 if "overdrive" in unlocked else 0
    coin_reward = (10 if tools_used else 5) + bonus
    coin_status = award_spiritual_coins(user["id"], coin_reward)

    return {
        "session_id": session_id,
        "reply": reply_text,
        "tools_used": tools_used,
        "files": generated_files,
        "coins": coin_status,
    }


# --- Social & Instagram-Style Direct Messaging ---
@app.get("/api/users/search")
def search_exact_user(username: str, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    q = username.strip()
    if not q:
        return {"user": None}
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "SELECT id, username, bio, spiritual_coins, avatar_seed FROM users WHERE LOWER(username) = LOWER(%s) AND id != %s",
            (q, me["id"]),
        )
        target = cur.fetchone()
        if not target:
            return {"user": None}

        target = dict(target)
        av = target.get("avatar_seed") or ""
        target["avatar_data"] = av if av.startswith("data:image/") else ""

        cur.execute(
            """SELECT id, status, requester_id FROM friendships
               WHERE (requester_id = %s AND receiver_id = %s) OR (requester_id = %s AND receiver_id = %s)""",
            (me["id"], target["id"], target["id"], me["id"]),
        )
        rel = cur.fetchone()
        return {"user": target, "relation": rel}


@app.post("/api/friends/request")
def send_friend_request(payload: FriendRequestPayload, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s)", (payload.username.strip(),))
        target = cur.fetchone()
        if not target or target["id"] == me["id"]:
            raise HTTPException(status_code=404, detail="User not found")

        cur.execute(
            """SELECT id FROM friendships
               WHERE (requester_id = %s AND receiver_id = %s) OR (requester_id = %s AND receiver_id = %s)""",
            (me["id"], target["id"], target["id"], me["id"]),
        )
        if cur.fetchone():
            return {"status": "exists"}

        cur.execute(
            "INSERT INTO friendships (requester_id, receiver_id, status) VALUES (%s, %s, 'pending')",
            (me["id"], target["id"]),
        )
        return {"status": "sent"}


@app.get("/api/friends")
def get_friends_and_requests(authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """SELECT f.id AS friendship_id, u.id, u.username, u.bio, u.spiritual_coins, u.avatar_seed
               FROM friendships f
               JOIN users u ON u.id = f.requester_id
               WHERE f.receiver_id = %s AND f.status = 'pending'""",
            (me["id"],),
        )
        incoming_raw = cur.fetchall()
        incoming = []
        for r in incoming_raw:
            d = dict(r)
            av = d.get("avatar_seed") or ""
            d["avatar_data"] = av if av.startswith("data:image/") else ""
            incoming.append(d)

        cur.execute(
            """SELECT u.id, u.username, u.bio, u.spiritual_coins, u.avatar_seed
               FROM friendships f
               JOIN users u ON (u.id = CASE WHEN f.requester_id = %s THEN f.receiver_id ELSE f.requester_id END)
               WHERE (f.requester_id = %s OR f.receiver_id = %s) AND f.status = 'accepted'""",
            (me["id"], me["id"], me["id"]),
        )
        friends_raw = cur.fetchall()
        friends = []
        for f in friends_raw:
            d = dict(f)
            av = d.get("avatar_seed") or ""
            d["avatar_data"] = av if av.startswith("data:image/") else ""
            cur.execute(
                """SELECT content, sender_id, created_at FROM direct_messages
                   WHERE (sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)
                   ORDER BY id DESC LIMIT 1""",
                (me["id"], d["id"], d["id"], me["id"]),
            )
            last_msg = cur.fetchone()
            d["last_message"] = last_msg["content"] if last_msg else "Tap to start chatting"
            friends.append(d)

        return {"incoming": incoming, "friends": friends}


@app.post("/api/friends/respond")
def respond_friend_request(payload: FriendRespondPayload, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
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
        return {"status": "ok"}


@app.get("/api/dm/{friend_id}")
def get_dm_history(friend_id: str, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    with get_db_cursor() as (conn, cur):
        cur.execute(
            """SELECT id, sender_id, receiver_id, content, created_at
               FROM direct_messages
               WHERE (sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)
               ORDER BY id ASC LIMIT 120""",
            (me["id"], friend_id, friend_id, me["id"]),
        )
        return cur.fetchall()


@app.post("/api/dm")
def send_dm(payload: DMPayload, authorization: Optional[str] = Header(None)):
    me = get_current_user(authorization)
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    with get_db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO direct_messages (sender_id, receiver_id, content) VALUES (%s, %s, %s)",
            (me["id"], payload.receiver_id, payload.content.strip()),
        )
        return {"status": "sent"}


# --- Pro UI: Clickable Popping Bubbles + Sleek Auth + Gallery PFP + 4-Tab Dock (Home, Chat, Upgrade, Profile) ---
HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>Sovereign — Autonomous Agentic Intelligence</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    :root {
      --bg-deep: #06070A;
      --glass-bg: rgba(16, 19, 28, 0.72);
      --glass-strong: rgba(20, 24, 36, 0.88);
      --border-light: rgba(255, 255, 255, 0.09);
      --text-main: #F3F4F6;
      --text-sub: #9CA3AF;
      --text-dim: #6B7280;
      --accent: #10B981;
      --accent-glow: rgba(16, 185, 129, 0.25);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    body {
      font-family: 'Inter', -apple-system, sans-serif;
      background: radial-gradient(circle at 50% 0%, #0E1320 0%, #06070A 75%);
      color: var(--text-main);
      height: 100vh;
      overflow: hidden;
      -webkit-font-smoothing: antialiased;
    }

    /* Interactive Popping Bubbles Canvas */
    #bubbleCanvas {
      position: fixed;
      inset: 0;
      width: 100vw;
      height: 100vh;
      z-index: 0;
      cursor: pointer;
    }

    ::-webkit-scrollbar { width: 4px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.14); border-radius: 99px; }

    /* Sleek Pro Auth Screen */
    #authScreen {
      position: fixed; inset: 0; z-index: 100;
      display: flex; align-items: center; justify-content: center;
      padding: 20px; background: rgba(6, 7, 10, 0.76); backdrop-filter: blur(18px);
    }
    .auth-card {
      width: 100%; max-width: 400px;
      background: linear-gradient(160deg, rgba(24, 28, 42, 0.92), rgba(12, 14, 22, 0.96));
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 26px; padding: 34px 28px;
      box-shadow: 0 28px 80px rgba(0,0,0,0.85), inset 0 1px 0 rgba(255,255,255,0.08);
    }
    .auth-logo {
      width: 48px; height: 48px; border-radius: 14px; margin: 0 auto 14px;
      background: linear-gradient(135deg, #10B981, #059669);
      display: flex; align-items: center; justify-content: center;
      font-weight: 800; font-size: 20px; color: #032218;
      box-shadow: 0 10px 28px rgba(16, 185, 129, 0.35);
    }
    .auth-tabs {
      display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
      background: rgba(6, 7, 10, 0.75); padding: 5px;
      border-radius: 14px; border: 1px solid var(--border-light);
      margin-bottom: 20px;
    }
    .auth-tab-btn {
      padding: 9px; border-radius: 10px; border: none;
      background: transparent; color: var(--text-sub);
      font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.18s;
    }
    .auth-tab-btn.active {
      background: rgba(255, 255, 255, 0.1); color: #FFF;
    }
    .input-wrap { position: relative; margin-bottom: 13px; }
    .field {
      width: 100%; background: rgba(6, 7, 10, 0.85);
      border: 1px solid var(--border-light);
      border-radius: 13px; padding: 13px 15px;
      color: var(--text-main); font-size: 14px; outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    .field:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.15);
    }
    .eye-btn {
      position: absolute; right: 12px; top: 50%; transform: translateY(-50%);
      background: none; border: none; color: var(--text-sub);
      font-size: 12px; font-weight: 600; cursor: pointer; padding: 4px;
    }
    .btn-primary {
      width: 100%; padding: 13px 16px; border-radius: 13px;
      background: linear-gradient(135deg, #10B981, #059669);
      color: #032218; font-weight: 700; font-size: 14px;
      border: none; cursor: pointer;
      box-shadow: 0 8px 24px rgba(16, 185, 129, 0.25);
      transition: transform 0.12s, filter 0.15s;
    }
    .btn-primary:hover { filter: brightness(1.08); }
    .btn-primary:active { transform: scale(0.98); }
    .btn-primary:disabled { opacity: 0.55; cursor: not-allowed; }
    .btn-ghost {
      width: 100%; padding: 11px 16px; border-radius: 13px;
      background: rgba(255,255,255,0.05); color: var(--text-sub);
      border: 1px solid var(--border-light); font-weight: 600; font-size: 13px;
      cursor: pointer;
    }
    .btn-ghost:hover { background: rgba(255,255,255,0.09); color: var(--text-main); }

    /* App Shell (pointer-events:none so clicks on empty space pop bubbles!) */
    #appShell {
      position: relative; z-index: 1;
      display: none; height: 100vh; width: 100vw;
      flex-direction: column;
      pointer-events: none;
    }
    .top-bar, .history-drawer, .content-col, .floating-pad, .ig-dock {
      pointer-events: auto;
    }

    /* Top Floating Bar */
    .top-bar {
      height: 62px; padding: 0 18px;
      display: flex; align-items: center; justify-content: space-between;
      z-index: 20;
    }
    .top-left, .top-right { display: flex; align-items: center; gap: 8px; }
    .icon-pill {
      height: 38px; padding: 0 13px; border-radius: 99px;
      background: var(--glass-bg); backdrop-filter: blur(16px);
      border: 1px solid var(--border-light);
      color: var(--text-main); font-size: 13px; font-weight: 600;
      display: flex; align-items: center; gap: 6px; cursor: pointer;
      transition: all 0.15s;
    }
    .icon-pill:hover { border-color: rgba(255,255,255,0.22); }

    .dragon-pill {
      height: 40px; padding: 0 13px 0 8px; border-radius: 99px;
      background: linear-gradient(135deg, rgba(24, 20, 38, 0.88), rgba(15, 18, 26, 0.92));
      backdrop-filter: blur(16px);
      border: 1px solid rgba(245, 158, 11, 0.35);
      display: flex; align-items: center; gap: 8px;
      box-shadow: 0 4px 20px rgba(139, 92, 246, 0.22);
      cursor: pointer;
    }
    .coin-val { font-weight: 800; font-size: 14px; color: #FBBF24; }
    .coin-sub { font-size: 10.5px; color: var(--text-sub); }

    /* History Drawer */
    .history-drawer {
      position: fixed; top: 68px; left: 16px; bottom: 92px; width: 270px;
      background: var(--glass-strong); backdrop-filter: blur(22px);
      border: 1px solid var(--border-light); border-radius: 22px;
      padding: 16px; z-index: 40;
      display: none; flex-direction: column;
      box-shadow: 0 24px 60px rgba(0,0,0,0.75);
    }
    .history-drawer.open { display: flex; }
    .sess-list { flex: 1; overflow-y: auto; margin-top: 12px; display: flex; flex-direction: column; gap: 4px; }
    .sess-item {
      padding: 10px 12px; border-radius: 12px; font-size: 13px; color: var(--text-sub);
      display: flex; justify-content: space-between; align-items: center; cursor: pointer;
    }
    .sess-item:hover, .sess-item.active { background: rgba(255,255,255,0.08); color: var(--text-main); }

    /* Pages Container */
    .page-area {
      flex: 1; overflow-y: auto; padding: 10px 18px 210px;
      display: flex; flex-direction: column; align-items: center;
      pointer-events: none;
    }
    .content-col { width: 100%; max-width: 740px; display: flex; flex-direction: column; gap: 18px; }

    /* Hero Greeting */
    .hero-center { margin: auto; text-align: center; max-width: 600px; padding: 28px 10px; }
    .hero-glow-tag {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 6px 14px; border-radius: 99px;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: #34D399; font-size: 12px; font-weight: 600; margin-bottom: 14px;
    }
    .hero-title {
      font-size: 30px; font-weight: 800; letter-spacing: -0.03em;
      background: linear-gradient(135deg, #FFFFFF 20%, #9CA3AF 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
      margin-bottom: 10px;
    }
    .hero-sub { font-size: 14px; color: var(--text-sub); line-height: 1.6; margin-bottom: 24px; }
    .quick-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; text-align: left; }
    .quick-card {
      background: var(--glass-bg); backdrop-filter: blur(14px);
      border: 1px solid var(--border-light); border-radius: 16px;
      padding: 14px 16px; cursor: pointer; transition: all 0.18s;
    }
    .quick-card:hover { border-color: var(--accent); transform: translateY(-2px); }

    /* Chat Bubbles */
    .msg-user {
      align-self: flex-end; max-width: 82%;
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.24), rgba(6, 78, 59, 0.38));
      backdrop-filter: blur(14px);
      border: 1px solid rgba(16, 185, 129, 0.35);
      padding: 12px 16px; border-radius: 20px 20px 5px 20px;
      font-size: 14.5px; line-height: 1.55;
    }
    .msg-ai {
      display: flex; gap: 12px; align-items: flex-start; width: 100%;
      background: var(--glass-bg); backdrop-filter: blur(14px);
      border: 1px solid var(--border-light);
      padding: 15px 17px; border-radius: 20px;
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
      position: relative;
      background: rgba(6, 7, 10, 0.92); border: 1px solid var(--border-light);
      border-radius: 12px; padding: 34px 13px 13px; overflow-x: auto; margin: 10px 0;
      font-family: 'JetBrains Mono', monospace; font-size: 12.5px;
    }
    .copy-code-btn {
      position: absolute; top: 7px; right: 8px;
      background: rgba(255, 255, 255, 0.08); color: var(--text-sub);
      border: 1px solid var(--border-light); border-radius: 7px;
      padding: 3px 9px; font-size: 11px; font-weight: 600; cursor: pointer;
    }
    .tool-chip {
      display: inline-block; font-size: 11px; font-family: 'JetBrains Mono', monospace;
      color: #34D399; background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.28);
      padding: 3px 9px; border-radius: 99px; margin: 0 6px 8px 0;
    }
    .file-dl {
      margin-top: 10px; margin-right: 8px; display: inline-flex; align-items: center; gap: 10px;
      background: rgba(255,255,255,0.05); border: 1px solid var(--border-light);
      padding: 8px 13px; border-radius: 12px;
    }

    /* Floating Glass Prompt Pad */
    .floating-pad-wrap {
      position: fixed; bottom: 84px; left: 0; right: 0;
      padding: 0 16px; display: flex; justify-content: center; z-index: 25;
      pointer-events: none;
    }
    .floating-pad {
      width: 100%; max-width: 740px;
      background: rgba(18, 22, 33, 0.82);
      backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 24px; padding: 12px 14px 12px 18px;
      box-shadow: 0 18px 50px rgba(0, 0, 0, 0.65), 0 0 28px var(--accent-glow);
      transition: border-color 0.2s, transform 0.2s;
    }
    .floating-pad:focus-within { border-color: var(--accent); transform: translateY(-2px); }
    .pad-textarea {
      width: 100%; background: transparent; border: none; outline: none;
      color: var(--text-main); font-family: inherit; font-size: 14.5px;
      resize: none; max-height: 120px; line-height: 1.5;
    }
    .pad-controls { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; }
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
    }
    .exec-btn:disabled { opacity: 0.45; cursor: not-allowed; }

    /* Instagram-Style 4-Tab Bottom Navigation Dock */
    .ig-dock-wrap {
      position: fixed; bottom: 16px; left: 0; right: 0;
      display: flex; justify-content: center; z-index: 30; pointer-events: none;
    }
    .ig-dock {
      background: rgba(15, 18, 27, 0.9);
      backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px);
      border: 1px solid rgba(255, 255, 255, 0.13);
      border-radius: 99px; padding: 6px 8px;
      display: flex; align-items: center; gap: 4px;
      box-shadow: 0 14px 40px rgba(0, 0, 0, 0.8);
    }
    .ig-tab {
      padding: 9px 16px; border-radius: 99px;
      border: none; background: transparent;
      color: var(--text-sub); font-size: 12.5px; font-weight: 600;
      display: flex; align-items: center; gap: 6px;
      cursor: pointer; transition: all 0.18s;
    }
    .ig-tab:hover { color: var(--text-main); }
    .ig-tab.active {
      background: rgba(255, 255, 255, 0.11);
      color: #FFFFFF;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.12);
    }

    /* Professional Avatar Circle */
    .pro-avatar {
      border-radius: 50%; object-fit: cover;
      background: linear-gradient(135deg, #1E293B, #0F172A);
      border: 2px solid rgba(255,255,255,0.16);
      display: flex; align-items: center; justify-content: center;
      font-weight: 800; color: #F3F4F6; overflow: hidden; flex-shrink: 0;
    }
    .pro-avatar img { width: 100%; height: 100%; object-fit: cover; }

    /* Professional Profile Card */
    .pro-profile-card {
      background: linear-gradient(155deg, rgba(22, 26, 39, 0.92), rgba(12, 15, 23, 0.96));
      backdrop-filter: blur(22px);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 24px; padding: 24px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.65);
    }
    .pfp-upload-wrap { position: relative; width: 86px; height: 86px; cursor: pointer; }
    .pfp-cam-badge {
      position: absolute; bottom: 0; right: 0;
      width: 28px; height: 28px; border-radius: 50%;
      background: #10B981; color: #032218;
      border: 2px solid #0C0F17;
      display: flex; align-items: center; justify-content: center;
      font-size: 13px; font-weight: 800;
    }
    .tier-badge {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 4px 11px; border-radius: 99px;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.35);
      color: #34D399; font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
    }
    .stat-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 20px; }
    .stat-box {
      background: rgba(255,255,255,0.035); border: 1px solid var(--border-light);
      border-radius: 14px; padding: 13px 10px; text-align: center;
    }
    .stat-num { font-size: 18px; font-weight: 800; color: #FFFFFF; }
    .stat-lbl { font-size: 11px; color: var(--text-sub); margin-top: 3px; }

    /* Instagram-Style Dedicated Chat Tab */
    .ig-chat-shell {
      background: var(--glass-strong); backdrop-filter: blur(22px);
      border: 1px solid var(--border-light); border-radius: 24px;
      overflow: hidden; display: flex; flex-direction: column;
      height: calc(100vh - 185px); min-height: 440px;
      box-shadow: 0 20px 55px rgba(0,0,0,0.7);
    }
    .ig-chat-topbar {
      padding: 15px 18px; border-bottom: 1px solid var(--border-light);
      display: flex; align-items: center; justify-content: space-between;
    }
    .ig-friend-row {
      display: flex; align-items: center; gap: 13px;
      padding: 13px 18px; border-bottom: 1px solid rgba(255,255,255,0.04);
      cursor: pointer; transition: background 0.15s;
    }
    .ig-friend-row:hover { background: rgba(255,255,255,0.05); }
    .ig-dm-thread {
      flex: 1; overflow-y: auto; padding: 16px;
      display: flex; flex-direction: column; gap: 8px;
    }
    .dm-bubble-me {
      align-self: flex-end; max-width: 76%;
      background: linear-gradient(135deg, #10B981, #059669);
      color: #032218; font-weight: 500;
      padding: 10px 14px; border-radius: 18px 18px 4px 18px; font-size: 13.5px;
    }
    .dm-bubble-them {
      align-self: flex-start; max-width: 76%;
      background: rgba(255, 255, 255, 0.08);
      color: #F3F4F6;
      padding: 10px 14px; border-radius: 18px 18px 18px 4px; font-size: 13.5px;
    }
    .ig-dm-compose {
      padding: 12px 14px; border-top: 1px solid var(--border-light);
      display: flex; gap: 8px; background: rgba(10, 12, 18, 0.6);
    }

    /* Upgrade Store Cards */
    .upgrade-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
    .upg-card {
      background: var(--glass-strong); backdrop-filter: blur(18px);
      border: 1px solid var(--border-light); border-radius: 20px;
      padding: 20px; display: flex; flex-direction: column; justify-content: space-between; gap: 14px;
    }
    .upg-card.unlocked { border-color: rgba(16, 185, 129, 0.4); }

    .glass-box {
      background: var(--glass-strong); border: 1px solid var(--border-light);
      border-radius: 20px; padding: 18px;
    }
    .mem-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.05);
    }

    @media (max-width: 680px) {
      .quick-grid, .upgrade-grid { grid-template-columns: 1fr; }
      .ig-tab { padding: 8px 13px; font-size: 12px; }
    }
  </style>
</head>
<body>

  <!-- Interactive Clickable Floating Bubbles -->
  <canvas id="bubbleCanvas"></canvas>

  <!-- Sleek Pro Auth Screen -->
  <div id="authScreen">
    <div class="auth-card">
      <div class="auth-logo">S</div>
      <div style="text-align:center;margin-bottom:20px;">
        <div style="font-size:21px;font-weight:800;letter-spacing:-0.02em;">Sovereign Workspace</div>
        <div style="font-size:13px;color:var(--text-sub);margin-top:4px;">Sign in to your autonomous AI terminal</div>
      </div>

      <div class="auth-tabs">
        <button class="auth-tab-btn active" id="tabLoginBtn" onclick="setAuthMode('login')">Sign In</button>
        <button class="auth-tab-btn" id="tabRegBtn" onclick="setAuthMode('register')">Create Account</button>
      </div>

      <div class="input-wrap">
        <input id="authUser" class="field" placeholder="Username" autocomplete="username" />
      </div>
      <div class="input-wrap">
        <input id="authPass" type="password" class="field" placeholder="Password" autocomplete="current-password"
               onkeydown="if(event.key==='Enter') handleAuthSubmit()" />
        <button type="button" class="eye-btn" onclick="togglePassVisibility()">Show</button>
      </div>

      <div id="authErr" style="color:#F87171;font-size:12.5px;margin-bottom:12px;display:none;"></div>
      <button class="btn-primary" id="authSubmitBtn" onclick="handleAuthSubmit()">Sign In</button>
    </div>
  </div>

  <!-- Main App Shell -->
  <div id="appShell">
    <header class="top-bar">
      <div class="top-left">
        <button class="icon-pill" onclick="toggleDrawer()">☰ History</button>
        <button class="icon-pill" onclick="startNewChat()">+ New</button>
      </div>

      <div class="top-right">
        <button class="icon-pill" id="soundToggle" onclick="toggleSound()">🔊 SFX</button>

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
        <span style="font-size:12px;font-weight:700;letter-spacing:0.06em;color:var(--text-sub);">PAST SESSIONS</span>
        <span style="cursor:pointer;color:var(--text-sub);" onclick="toggleDrawer()">✕</span>
      </div>
      <div class="sess-list" id="sessionList"></div>
    </div>

    <!-- TAB 1: HOME (AI Chat) -->
    <div class="page-area" id="tabHome">
      <div class="content-col" id="chatFeed">
        <div class="hero-center" id="heroScreen">
          <div class="hero-glow-tag">⚡ Multi-Tool Autonomous Core Active</div>
          <h1 class="hero-title">Command Sovereign AI</h1>
          <p class="hero-sub">
            Live internet intelligence, symbolic mathematics, permanent cloud memory, and file synthesis. Tap background bubbles anytime to pop them.
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

    <!-- TAB 2: CHAT (Instagram-Style Direct Messages) -->
    <div class="page-area" id="tabChat" style="display:none;padding-bottom:96px;">
      <div class="content-col">
        <div class="ig-chat-shell">
          <!-- View A: Friends Inbox List -->
          <div id="igInboxView" style="display:flex;flex-direction:column;height:100%;">
            <div class="ig-chat-topbar">
              <div>
                <div style="font-size:17px;font-weight:800;" id="igHeaderUser">Direct Messages</div>
                <div style="font-size:12px;color:var(--text-sub);">Private encrypted squad channels</div>
              </div>
            </div>

            <div style="padding:12px 16px;border-bottom:1px solid var(--border-light);">
              <div style="display:flex;gap:8px;">
                <input id="friendSearchInp" class="field" style="padding:10px 14px;font-size:13.5px;" placeholder="Search username to add friend..." />
                <button class="btn-primary" style="width:auto;padding:10px 18px;" onclick="searchExactFriend()">Search</button>
              </div>
              <div id="searchOut" style="margin-top:8px;"></div>
              <div id="reqOut" style="margin-top:8px;"></div>
            </div>

            <div style="flex:1;overflow-y:auto;" id="igFriendsList"></div>
          </div>

          <!-- View B: Active 1-on-1 Chat Thread -->
          <div id="igThreadView" style="display:none;flex-direction:column;height:100%;">
            <div class="ig-chat-topbar">
              <div style="display:flex;align-items:center;gap:12px;">
                <button class="icon-pill" style="height:32px;padding:0 11px;" onclick="closeIGThread()">←</button>
                <div id="threadAvatarWrap"></div>
                <div>
                  <div style="font-size:15px;font-weight:700;" id="dmTitle">@friend</div>
                  <div style="font-size:11px;color:#34D399;">● Direct Channel</div>
                </div>
              </div>
            </div>

            <div class="ig-dm-thread" id="dmFeed"></div>

            <div class="ig-dm-compose">
              <input id="dmText" class="field" style="padding:10px 14px;font-size:13.5px;" placeholder="Message..." onkeydown="if(event.key==='Enter') sendDM()" />
              <button class="btn-primary" style="width:auto;padding:10px 18px;" onclick="sendDM()">Send</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 3: UPGRADE STORE -->
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
              <div style="font-size:13px;color:var(--text-sub);margin-top:6px;">Fast, direct, multi-tool autonomous execution with emerald bubbles.</div>
            </div>
            <div style="font-size:12.5px;font-weight:700;color:#34D399;">✓ Equipped & Active</div>
          </div>

          <div class="upg-card" id="cardBrutal">
            <div>
              <div style="font-size:11px;font-weight:700;color:#F59E0B;">TIER II · 50 SPIRITUAL COINS</div>
              <div style="font-size:17px;font-weight:700;margin-top:4px;">Brutal Truth Core</div>
              <div style="font-size:13px;color:var(--text-sub);margin-top:6px;">Unfiltered razor-sharp critique. Exposes flaws with zero sugarcoating and amber bubbles.</div>
            </div>
            <button class="btn-primary" id="btnBrutal" onclick="buyUpgrade('brutal')">Unlock · 50 Coins</button>
          </div>

          <div class="upg-card" id="cardArchitect">
            <div>
              <div style="font-size:11px;font-weight:700;color:#A78BFA;">TIER III · 120 SPIRITUAL COINS</div>
              <div style="font-size:17px;font-weight:700;margin-top:4px;">Principal Architect Core</div>
              <div style="font-size:13px;color:var(--text-sub);margin-top:6px;">Deep system topology, trade-offs, and royal violet bubbles.</div>
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

    <!-- TAB 4: PROFESSIONAL PROFILE -->
    <div class="page-area" id="tabProfile" style="display:none;padding-bottom:110px;">
      <div class="content-col">
        <div class="pro-profile-card">
          <div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap;">
            <!-- Gallery Uploadable Avatar -->
            <div class="pfp-upload-wrap" onclick="document.getElementById('pfpFileInput').click()" title="Upload Photo from Gallery">
              <div id="profileAvatarSlot" class="pro-avatar" style="width:86px;height:86px;font-size:30px;">S</div>
              <div class="pfp-cam-badge">📷</div>
              <input type="file" id="pfpFileInput" accept="image/*" style="display:none;" onchange="handleGalleryUpload(event)" />
            </div>

            <div style="flex:1;min-width:200px;">
              <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <span style="font-size:22px;font-weight:800;" id="profName">@player</span>
                <span class="tier-badge" id="profRank">TIER I · INITIATE</span>
              </div>
              <div style="font-size:13.5px;color:var(--text-sub);margin-top:6px;" id="profBioText">Sovereign Explorer</div>
              <div style="display:flex;gap:8px;margin-top:10px;">
                <button class="icon-pill" style="height:32px;font-size:12px;" onclick="document.getElementById('pfpFileInput').click()">
                  Upload Photo
                </button>
                <button class="icon-pill" style="height:32px;font-size:12px;color:#F87171;" id="removePfpBtn" onclick="removeProfilePhoto()">
                  Remove Photo
                </button>
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
              <div class="stat-lbl">Sessions Completed</div>
            </div>
          </div>

          <div style="margin-top:18px;">
            <label style="font-size:11.5px;font-weight:700;color:var(--text-sub);display:block;margin-bottom:6px;">BIO / STATUS</label>
            <div style="display:flex;gap:8px;">
              <input id="bioInput" class="field" placeholder="Write a short bio..." />
              <button class="btn-primary" style="width:auto;white-space:nowrap;padding:12px 20px;" onclick="saveProfile()">Save</button>
            </div>
          </div>

          <div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--border-light);display:flex;justify-content:flex-end;">
            <button class="btn-ghost" style="width:auto;padding:9px 18px;color:#F87171;" onclick="logout()">Sign Out</button>
          </div>
        </div>

        <!-- Long-Term Memories -->
        <div class="glass-box">
          <div style="font-size:14px;font-weight:700;margin-bottom:8px;">🧠 Long-Term Cloud Memory</div>
          <div id="memBox" style="font-size:13px;color:var(--text-sub);line-height:1.7;">No memories stored yet.</div>
        </div>
      </div>
    </div>

    <!-- Floating Glass Prompt Pad (Home Tab Only) -->
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

    <!-- 4-Tab Bottom Navigation Dock: Home | Chat | Upgrade | Profile -->
    <div class="ig-dock-wrap">
      <nav class="ig-dock">
        <button class="ig-tab active" id="nav-home" onclick="switchTab('home')">
          <span>🏠</span><span>Home</span>
        </button>
        <button class="ig-tab" id="nav-chat" onclick="switchTab('chat')">
          <span>💬</span><span>Chat</span>
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
  // --- Satisfying Web Audio Synthesizer (Including Ultra-Crisp Bubble Pop!) ---
  let soundEnabled = true;
  let audioCtx = null;

  function ensureAudio() {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
  }

  function toggleSound() {
    soundEnabled = !soundEnabled;
    document.getElementById('soundToggle').textContent = soundEnabled ? '🔊 SFX' : '🔇 Muted';
    if (soundEnabled) playBubblePopSound(1.0);
  }

  // Realistic resonant water-droplet bubble pop sound
  function playBubblePopSound(pitchScale = 1.0) {
    if (!soundEnabled) return;
    ensureAudio();
    const now = audioCtx.currentTime;

    // Main resonant bubble pitch sweep
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    const baseFreq = (280 + Math.random() * 140) * pitchScale;
    osc.frequency.setValueAtTime(baseFreq, now);
    osc.frequency.exponentialRampToValueAtTime(baseFreq * 3.1, now + 0.065);

    gain.gain.setValueAtTime(0.001, now);
    gain.gain.linearRampToValueAtTime(0.22, now + 0.008);
    gain.gain.exponentialRampToValueAtTime(0.0008, now + 0.075);

    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + 0.08);

    // Subtle high harmonic snap
    const snap = audioCtx.createOscillator();
    const snapGain = audioCtx.createGain();
    snap.type = 'triangle';
    snap.frequency.setValueAtTime(baseFreq * 2.2, now);
    snap.frequency.exponentialRampToValueAtTime(baseFreq * 4.5, now + 0.035);
    snapGain.gain.setValueAtTime(0.08, now);
    snapGain.gain.exponentialRampToValueAtTime(0.0008, now + 0.04);
    snap.connect(snapGain);
    snapGain.connect(audioCtx.destination);
    snap.start(now);
    snap.stop(now + 0.045);
  }

  function playClickSound() {
    if (!soundEnabled) return;
    ensureAudio();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(420, audioCtx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(680, audioCtx.currentTime + 0.06);
    gain.gain.setValueAtTime(0.06, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.06);
    osc.connect(gain); gain.connect(audioCtx.destination);
    osc.start(); osc.stop(audioCtx.currentTime + 0.06);
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
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.25);
      osc.connect(gain); gain.connect(audioCtx.destination);
      osc.start(t); osc.stop(t + 0.25);
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

  // --- Interactive Floating Glossy Bubbles + Burst Physics ---
  const canvas = document.getElementById('bubbleCanvas');
  const ctx = canvas.getContext('2d');
  let bubbles = [];
  let burstEffects = [];
  let bubbleColorRGB = '16, 185, 129';

  function resizeCanvas() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
  }
  window.addEventListener('resize', resizeCanvas);
  resizeCanvas();

  function spawnBubble(randomY = false) {
    const r = Math.random() * 22 + 14;
    return {
      x: Math.random() * (canvas.width - r * 2) + r,
      y: randomY ? Math.random() * canvas.height : canvas.height + r + Math.random() * 80,
      r: r,
      vy: -(Math.random() * 0.65 + 0.35),
      wobblePhase: Math.random() * Math.PI * 2,
      wobbleSpeed: Math.random() * 0.02 + 0.01,
      alpha: Math.random() * 0.28 + 0.22
    };
  }

  for (let i = 0; i < 24; i++) {
    bubbles.push(spawnBubble(true));
  }

  function triggerBubbleBurst(b) {
    // Larger bubbles = deeper pop, smaller bubbles = higher crisp pop
    const pitchScale = Math.max(0.75, Math.min(1.45, 28 / b.r));
    playBubblePopSound(pitchScale);

    const droplets = [];
    for (let i = 0; i < 12; i++) {
      const ang = (Math.PI * 2 * i) / 12 + (Math.random() - 0.5) * 0.3;
      const spd = Math.random() * 3.5 + 1.8;
      droplets.push({
        x: b.x + Math.cos(ang) * (b.r * 0.5),
        y: b.y + Math.sin(ang) * (b.r * 0.5),
        vx: Math.cos(ang) * spd,
        vy: Math.sin(ang) * spd,
        r: Math.random() * 2.5 + 1.2
      });
    }
    burstEffects.push({
      x: b.x,
      y: b.y,
      radius: b.r * 0.7,
      maxRadius: b.r * 2.1,
      alpha: 0.85,
      droplets
    });
  }

  function handleCanvasTap(clientX, clientY) {
    for (let i = bubbles.length - 1; i >= 0; i--) {
      const b = bubbles[i];
      const dx = clientX - b.x;
      const dy = clientY - b.y;
      if (dx * dx + dy * dy <= (b.r + 12) * (b.r + 12)) {
        triggerBubbleBurst(b);
        bubbles[i] = spawnBubble(false);
        break;
      }
    }
  }

  canvas.addEventListener('pointerdown', (e) => {
    handleCanvasTap(e.clientX, e.clientY);
  });

  function animateBubbles() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Draw floating translucent glass bubbles
    for (let i = 0; i < bubbles.length; i++) {
      const b = bubbles[i];
      b.y += b.vy;
      b.wobblePhase += b.wobbleSpeed;
      const drawX = b.x + Math.sin(b.wobblePhase) * 10;

      if (b.y < -b.r - 20) {
        bubbles[i] = spawnBubble(false);
        continue;
      }

      // Radial translucent body
      const grad = ctx.createRadialGradient(
        drawX - b.r * 0.3, b.y - b.r * 0.3, b.r * 0.1,
        drawX, b.y, b.r
      );
      grad.addColorStop(0, `rgba(255, 255, 255, ${b.alpha * 0.65})`);
      grad.addColorStop(0.55, `rgba(${bubbleColorRGB}, ${b.alpha * 0.25})`);
      grad.addColorStop(1, `rgba(${bubbleColorRGB}, ${b.alpha * 0.75})`);

      ctx.beginPath();
      ctx.arc(drawX, b.y, b.r, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.lineWidth = 1.2;
      ctx.strokeStyle = `rgba(${bubbleColorRGB}, ${b.alpha + 0.18})`;
      ctx.stroke();

      // Glossy specular highlight reflection
      ctx.beginPath();
      ctx.arc(drawX - b.r * 0.34, b.y - b.r * 0.34, b.r * 0.22, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(255, 255, 255, ${b.alpha + 0.28})`;
      ctx.fill();
    }

    // Draw burst shockwaves and droplets
    for (let i = burstEffects.length - 1; i >= 0; i--) {
      const ef = burstEffects[i];
      ef.radius += (ef.maxRadius - ef.radius) * 0.22;
      ef.alpha -= 0.048;

      if (ef.alpha <= 0) {
        burstEffects.splice(i, 1);
        continue;
      }

      ctx.beginPath();
      ctx.arc(ef.x, ef.y, ef.radius, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(${bubbleColorRGB}, ${ef.alpha})`;
      ctx.lineWidth = 2;
      ctx.stroke();

      for (const d of ef.droplets) {
        d.x += d.vx;
        d.y += d.vy;
        d.vx *= 0.94;
        d.vy *= 0.94;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(255, 255, 255, ${ef.alpha})`;
        ctx.fill();
      }
    }

    requestAnimationFrame(animateBubbles);
  }
  animateBubbles();

  function onModeChange() {
    const m = document.getElementById('modeSelect').value;
    const root = document.documentElement.style;
    if (m === 'brutal') {
      bubbleColorRGB = '245, 158, 11';
      root.setProperty('--accent', '#F59E0B');
      root.setProperty('--accent-glow', 'rgba(245, 158, 11, 0.25)');
    } else if (m === 'architect') {
      bubbleColorRGB = '139, 92, 246';
      root.setProperty('--accent', '#8B5CF6');
      root.setProperty('--accent-glow', 'rgba(139, 92, 246, 0.28)');
    } else if (m === 'overdrive') {
      bubbleColorRGB = '56, 189, 248';
      root.setProperty('--accent', '#38BDF8');
      root.setProperty('--accent-glow', 'rgba(56, 189, 248, 0.28)');
    } else {
      bubbleColorRGB = '16, 185, 129';
      root.setProperty('--accent', '#10B981');
      root.setProperty('--accent-glow', 'rgba(16, 185, 129, 0.25)');
    }
    playClickSound();
  }

  // --- Pro Auth Logic (Zero Glitch / Double-Submit Lock) ---
  let authMode = 'login';
  let authBusy = false;

  function setAuthMode(mode) {
    authMode = mode;
    document.getElementById('tabLoginBtn').classList.toggle('active', mode === 'login');
    document.getElementById('tabRegBtn').classList.toggle('active', mode === 'register');
    document.getElementById('authSubmitBtn').textContent = mode === 'login' ? 'Sign In' : 'Create Account';
    document.getElementById('authErr').style.display = 'none';
  }

  function togglePassVisibility() {
    const inp = document.getElementById('authPass');
    const btn = document.querySelector('.eye-btn');
    if (inp.type === 'password') {
      inp.type = 'text';
      btn.textContent = 'Hide';
    } else {
      inp.type = 'password';
      btn.textContent = 'Show';
    }
  }

  async function handleAuthSubmit() {
    if (authBusy) return;
    const username = document.getElementById('authUser').value.trim();
    const password = document.getElementById('authPass').value.trim();
    const errEl = document.getElementById('authErr');
    const btn = document.getElementById('authSubmitBtn');

    errEl.style.display = 'none';
    if (!username || !password) {
      errEl.textContent = 'Please enter both username and password.';
      errEl.style.display = 'block';
      return;
    }

    authBusy = true;
    btn.disabled = true;
    btn.textContent = authMode === 'login' ? 'Signing in...' : 'Creating account...';

    try {
      const res = await fetch('/api/auth/' + authMode, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Authentication failed');
      token = data.token;
      localStorage.setItem('sov_token', token);
      playChimeSound();
      await boot();
    } catch (e) {
      errEl.textContent = e.message;
      errEl.style.display = 'block';
    } finally {
      authBusy = false;
      btn.disabled = false;
      btn.textContent = authMode === 'login' ? 'Sign In' : 'Create Account';
    }
  }

  // --- App State & API ---
  let token = localStorage.getItem('sov_token') || '';
  let currentUser = null;
  let currentSessionId = null;
  let activeFriendId = null;
  let dmInterval = null;

  function headers() {
    return { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token };
  }

  function logout() {
    localStorage.removeItem('sov_token');
    location.reload();
  }

  async function boot() {
    if (!token) return;
    try {
      const res = await fetch('/api/me', { headers: headers() });
      if (!res.ok) { logout(); return; }
      currentUser = await res.json();
      document.getElementById('authScreen').style.display = 'none';
      document.getElementById('appShell').style.display = 'flex';
      refreshUserUI();
      loadSessions();
    } catch (e) {
      console.error('Boot error:', e);
    }
  }

  function renderAvatarHtml(username, avatarData, sizePx, fontPx) {
    const initial = (username || 'S').charAt(0).toUpperCase();
    if (avatarData && avatarData.startsWith('data:image/')) {
      return `<div class="pro-avatar" style="width:${sizePx}px;height:${sizePx}px;"><img src="${avatarData}" alt="pfp" /></div>`;
    }
    return `<div class="pro-avatar" style="width:${sizePx}px;height:${sizePx}px;font-size:${fontPx}px;">${escapeHtml(initial)}</div>`;
  }

  function getRankTitle(coins) {
    if (coins >= 300) return 'TIER IV · MYTHIC SOVEREIGN';
    if (coins >= 150) return 'TIER III · ASCENDANT';
    if (coins >= 50) return 'TIER II · ADEPT';
    return 'TIER I · INITIATE';
  }

  function refreshUserUI() {
    if (!currentUser) return;
    const coins = currentUser.spiritual_coins || 0;
    document.getElementById('coinCount').textContent = coins;
    document.getElementById('dailyCount').textContent = currentUser.daily_coins_earned || 0;

    document.getElementById('profName').textContent = '@' + currentUser.username;
    document.getElementById('igHeaderUser').textContent = '@' + currentUser.username;
    document.getElementById('profBioText').textContent = currentUser.bio || 'Sovereign Explorer';
    document.getElementById('bioInput').value = currentUser.bio || '';
    document.getElementById('profRank').textContent = getRankTitle(coins);
    document.getElementById('statCoins').textContent = coins;
    document.getElementById('statMissions').textContent = currentUser.session_count || 0;

    // Render Gallery PFP or Clean Initial
    const slot = document.getElementById('profileAvatarSlot');
    const initial = (currentUser.username || 'S').charAt(0).toUpperCase();
    if (currentUser.avatar_data) {
      slot.innerHTML = `<img src="${currentUser.avatar_data}" alt="Profile Photo" />`;
      document.getElementById('removePfpBtn').style.display = 'inline-flex';
    } else {
      slot.innerHTML = escapeHtml(initial);
      document.getElementById('removePfpBtn').style.display = 'none';
    }

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
      memBox.innerHTML = currentUser.memories.map(m => `
        <div class="mem-row">
          <span>• ${escapeHtml(m.fact)}</span>
          <span style="cursor:pointer;color:#F87171;padding:0 6px;font-weight:700;" onclick="delMemory(${m.id})">×</span>
        </div>
      `).join('');
    } else {
      memBox.innerHTML = 'No memories stored yet.';
    }
  }

  // Gallery Image Upload + Auto Resize/Compress for Instant Loading
  function handleGalleryUpload(event) {
    const file = event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function(e) {
      const img = new Image();
      img.onload = async function() {
        const c = document.createElement('canvas');
        const size = 240;
        c.width = size;
        c.height = size;
        const cctx = c.getContext('2d');
        const minSide = Math.min(img.width, img.height);
        const sx = (img.width - minSide) / 2;
        const sy = (img.height - minSide) / 2;
        cctx.drawImage(img, sx, sy, minSide, minSide, 0, 0, size, size);
        const compressedDataUrl = c.toDataURL('image/jpeg', 0.82);

        const res = await fetch('/api/me/profile', {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify({
            bio: document.getElementById('bioInput').value,
            avatar_data: compressedDataUrl
          })
        });
        if (res.ok) {
          currentUser.avatar_data = compressedDataUrl;
          refreshUserUI();
          playChimeSound();
        }
      };
      img.src = e.target.result;
    };
    reader.readAsDataURL(file);
  }

  async function removeProfilePhoto() {
    await fetch('/api/me/profile', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ bio: document.getElementById('bioInput').value, avatar_data: '' })
    });
    currentUser.avatar_data = '';
    refreshUserUI();
    playClickSound();
  }

  async function delMemory(id) {
    await fetch('/api/memories/' + id, { method: 'DELETE', headers: headers() });
    currentUser.memories = currentUser.memories.filter(m => m.id !== id);
    playClickSound();
    refreshUserUI();
  }

  async function saveProfile() {
    const bio = document.getElementById('bioInput').value;
    await fetch('/api/me/profile', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ bio })
    });
    currentUser.bio = bio;
    refreshUserUI();
    playChimeSound();
  }

  function switchTab(tab) {
    playClickSound();
    document.getElementById('tabHome').style.display = tab === 'home' ? 'flex' : 'none';
    document.getElementById('tabChat').style.display = tab === 'chat' ? 'flex' : 'none';
    document.getElementById('tabUpgrade').style.display = tab === 'upgrade' ? 'flex' : 'none';
    document.getElementById('tabProfile').style.display = tab === 'profile' ? 'flex' : 'none';
    document.getElementById('floatingPadWrap').style.display = tab === 'home' ? 'flex' : 'none';

    ['home', 'chat', 'upgrade', 'profile'].forEach(t => {
      document.getElementById('nav-' + t).classList.toggle('active', t === tab);
    });
    document.getElementById('historyDrawer').classList.remove('open');

    if (dmInterval) { clearInterval(dmInterval); dmInterval = null; }
    if (tab === 'chat') {
      loadFriends();
      dmInterval = setInterval(() => {
        if (!document.hidden) {
          if (activeFriendId) loadDMs(activeFriendId);
          else loadFriends();
        }
      }, 3500);
    } else if (tab === 'profile') {
      boot();
    }
  }

  function toggleDrawer() {
    playClickSound();
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
        <p class="hero-sub">Live internet intelligence, symbolic mathematics, permanent cloud memory, and file synthesis. Tap background bubbles anytime to pop them.</p>
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
    msgs.forEach(m => {
      renderMessage(
        m.role,
        m.content,
        JSON.parse(m.tools_used || '[]'),
        JSON.parse(m.files_json || '[]')
      );
    });
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
    playClickSound();
    renderMessage('user', text, [], []);

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
      renderMessage('assistant', data.reply, data.tools_used || [], data.files || []);
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
      renderMessage('assistant', 'Error: ' + e.message, [], []);
    } finally {
      btn.disabled = false;
    }
  }

  function renderMessage(role, content, tools, files) {
    const feed = document.getElementById('chatFeed');
    if (role === 'user') {
      const div = document.createElement('div');
      div.className = 'msg-user';
      div.textContent = content;
      feed.appendChild(div);
    } else {
      const chips = (tools || []).map(t => `<span class="tool-chip">✓ ${escapeHtml(t)}</span>`).join('');
      const dlCards = (files || []).map(f => {
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
          <div>${marked.parse(content)}</div>
          ${dlCards}
        </div>`;

      div.querySelectorAll('pre').forEach(pre => {
        const copyBtn = document.createElement('button');
        copyBtn.className = 'copy-code-btn';
        copyBtn.textContent = 'Copy';
        copyBtn.onclick = () => {
          const codeEl = pre.querySelector('code');
          navigator.clipboard.writeText(codeEl ? codeEl.innerText : pre.innerText);
          copyBtn.textContent = 'Copied!';
          playClickSound();
          setTimeout(() => copyBtn.textContent = 'Copy', 1500);
        };
        pre.appendChild(copyBtn);
      });

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
    playClickSound();
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

  // --- Instagram-Style Dedicated Chat Tab Logic ---
  async function searchExactFriend() {
    const q = document.getElementById('friendSearchInp').value.trim();
    if (!q) return;
    const res = await fetch('/api/users/search?username=' + encodeURIComponent(q), { headers: headers() });
    const data = await res.json();
    const out = document.getElementById('searchOut');
    if (!data.user) {
      out.innerHTML = `<div style="font-size:12.5px;color:var(--text-sub);padding:4px;">No exact user found.</div>`;
      return;
    }
    const u = data.user;
    let btn = `<button class="exec-btn" onclick="sendFriendReq('${escapeHtml(u.username)}')">+ Add Friend</button>`;
    if (data.relation) btn = `<span style="font-size:12px;color:#34D399;font-weight:600;">${data.relation.status.toUpperCase()}</span>`;
    out.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:rgba(255,255,255,0.05);border-radius:14px;">
        <div style="display:flex;align-items:center;gap:10px;">
          ${renderAvatarHtml(u.username, u.avatar_data, 38, 15)}
          <div>
            <div style="font-weight:700;font-size:13.5px;">@${escapeHtml(u.username)}</div>
            <div style="font-size:11.5px;color:var(--text-sub);">${escapeHtml(u.bio || '')}</div>
          </div>
        </div>
        ${btn}
      </div>`;
  }

  async function sendFriendReq(username) {
    await fetch('/api/friends/request', { method: 'POST', headers: headers(), body: JSON.stringify({ username }) });
    playClickSound();
    searchExactFriend();
  }

  async function loadFriends() {
    const res = await fetch('/api/friends', { headers: headers() });
    const data = await res.json();

    document.getElementById('reqOut').innerHTML = data.incoming.map(r => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 12px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);border-radius:12px;margin-bottom:6px;">
        <div style="display:flex;align-items:center;gap:9px;">
          ${renderAvatarHtml(r.username, r.avatar_data, 32, 13)}
          <span style="font-size:12.5px;">Request from <strong>@${escapeHtml(r.username)}</strong></span>
        </div>
        <div style="display:flex;gap:6px;">
          <button class="exec-btn" style="padding:5px 11px;font-size:11.5px;" onclick="respondFriend(${r.friendship_id},'accept')">Accept</button>
          <button class="btn-ghost" style="width:auto;padding:5px 11px;font-size:11.5px;" onclick="respondFriend(${r.friendship_id},'decline')">Decline</button>
        </div>
      </div>`).join('');

    const listEl = document.getElementById('igFriendsList');
    if (!data.friends.length) {
      listEl.innerHTML = `<div style="text-align:center;padding:45px 20px;color:var(--text-dim);font-size:13px;">
        No friends connected yet. Search a username above to start messaging.
      </div>`;
      return;
    }

    listEl.innerHTML = data.friends.map(f => `
      <div class="ig-friend-row" onclick="openIGThread('${f.id}', '${escapeHtml(f.username)}', '${f.avatar_data || ''}')">
        ${renderAvatarHtml(f.username, f.avatar_data, 48, 18)}
        <div style="flex:1;min-width:0;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-weight:700;font-size:14.5px;color:#FFF;">@${escapeHtml(f.username)}</span>
            <span style="font-size:11px;color:#FBBF24;font-weight:600;">${f.spiritual_coins} Coins</span>
          </div>
          <div style="font-size:12.5px;color:var(--text-sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;">
            ${escapeHtml(f.last_message || 'Tap to message')}
          </div>
        </div>
        <span style="color:var(--text-dim);font-size:16px;">›</span>
      </div>
    `).join('');
  }

  async function respondFriend(id, action) {
    await fetch('/api/friends/respond', { method: 'POST', headers: headers(), body: JSON.stringify({ friendship_id: id, action }) });
    playChimeSound();
    loadFriends();
  }

  function openIGThread(id, name, avatarData) {
    activeFriendId = id;
    playClickSound();
    document.getElementById('igInboxView').style.display = 'none';
    document.getElementById('igThreadView').style.display = 'flex';
    document.getElementById('dmTitle').textContent = '@' + name;
    document.getElementById('threadAvatarWrap').innerHTML = renderAvatarHtml(name, avatarData, 36, 14);
    loadDMs(id);
  }

  function closeIGThread() {
    activeFriendId = null;
    playClickSound();
    document.getElementById('igThreadView').style.display = 'none';
    document.getElementById('igInboxView').style.display = 'flex';
    loadFriends();
  }

  async function loadDMs(id) {
    const res = await fetch('/api/dm/' + id, { headers: headers() });
    const msgs = await res.json();
    const feed = document.getElementById('dmFeed');
    const wasAtBottom = feed.scrollHeight - feed.scrollTop <= feed.clientHeight + 60;
    feed.innerHTML = msgs.map(m => `
      <div class="${m.sender_id === currentUser.id ? 'dm-bubble-me' : 'dm-bubble-them'}">
        ${escapeHtml(m.content)}
      </div>`).join('');
    if (wasAtBottom) feed.scrollTop = feed.scrollHeight;
  }

  async function sendDM() {
    if (!activeFriendId) return;
    const inp = document.getElementById('dmText');
    const content = inp.value.trim();
    if (!content) return;
    inp.value = '';
    playClickSound();
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