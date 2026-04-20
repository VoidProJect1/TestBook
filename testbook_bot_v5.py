#!/usr/bin/env python3
"""
📚 TestBook Pro Bot v2 — Telegram Exam Preparation Bot
Improvements:
  • Near-perfect PDF MCQ extraction (multi-strategy, handles all layouts)
  • Questions sent as Telegram native Quiz polls (inline 4-option format)
  • Cleaner UI — unnecessary messages → Reply keyboard buttons
  • Better answer feedback & progress tracking
"""

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    Poll
)
import sqlite3
import json
import re
import time
import threading
import os
import io
import random
import unicodedata
from datetime import datetime

try:
    import pdfplumber
    PDFPLUMBER_OK = True
except ImportError:
    PDFPLUMBER_OK = False

try:
    import fitz  # PyMuPDF — better text extraction
    PYMUPDF_OK = True
except ImportError:
    PYMUPDF_OK = False

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN = "8780139812:AAGaUTndxedBe-N9eXb9Q7_pvq0sn96YoxQ"
ADMIN_ID  = 5479881365
DB_PATH   = "testbook.db"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  BULK MANUAL QUESTION PARSER
# ─────────────────────────────────────────────
def parse_bulk_questions(text):
    """
    Parse a bulk question block pasted by admin.
    Supports format:
        1. Question text
        A.) Option A  (or A) / A. / (A) / A - )
        B.) Option B
        C.) Option C
        D.) Option D
        Correct : A          ← optional correct answer line
        Explanation : ...    ← optional

    Returns list of dicts with keys:
        question, option_a, option_b, option_c, option_d, correct, explanation
    And a list of error strings for skipped blocks.
    """
    questions = []
    errors    = []

    # Split into numbered blocks: lines starting with a question number
    Q_SPLIT = re.compile(r'(?=^\s*\d{1,3}[\.\)]\s+)', re.MULTILINE)
    blocks   = Q_SPLIT.split(text.strip())
    blocks   = [b.strip() for b in blocks if b.strip()]

    OPT_RE   = re.compile(r'^[(\[]?([ABCDabcd])[)\]\.:\s]\)?\.?\s*(.+)', re.DOTALL)
    # Detect correct-answer override line: "Correct: B" / "Answer: C" etc.
    CORR_RE  = re.compile(
        r'(?:correct(?:\s*ans(?:wer)?)?|answer|ans|key|उत्तर)\s*[:\-]\s*([ABCDabcd])',
        re.IGNORECASE
    )
    # Explanation line
    EXP_RE   = re.compile(
        r'(?:explanation|expl?|solution|sol|note)\s*[:\-]\s*(.+)',
        re.IGNORECASE | re.DOTALL
    )
    # Strip leading question number
    QNUM_RE  = re.compile(r'^\d{1,3}[\.\)]\s*')

    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue

        q_text      = QNUM_RE.sub('', lines[0]).strip()
        opts        = {}
        correct     = None
        explanation = ''
        i           = 1

        # Absorb multi-line question text (lines before first option)
        while i < len(lines):
            m = OPT_RE.match(lines[i])
            if m:
                break
            # Check if it's a correct/explanation line
            if CORR_RE.match(lines[i]) or EXP_RE.match(lines[i]):
                break
            q_text += ' ' + lines[i]
            i += 1

        # Collect options A-D
        while i < len(lines) and len(opts) < 4:
            m = OPT_RE.match(lines[i])
            if m:
                key = m.group(1).upper()
                val = m.group(2).strip()
                i  += 1
                # Absorb wrapped option lines
                while i < len(lines):
                    if OPT_RE.match(lines[i]):
                        break
                    if CORR_RE.match(lines[i]) or EXP_RE.match(lines[i]):
                        break
                    val += ' ' + lines[i].strip()
                    i   += 1
                opts[key] = val.strip()
            elif CORR_RE.match(lines[i]) or EXP_RE.match(lines[i]):
                break
            else:
                i += 1

        # Look for correct-answer and explanation in remaining lines
        remaining = ' '.join(lines[i:])
        cm = CORR_RE.search(remaining)
        if cm:
            correct = cm.group(1).upper()

        em = EXP_RE.search(remaining)
        if em:
            explanation = em.group(1).strip()
            # Remove any trailing correct-answer fragment from explanation
            explanation = CORR_RE.sub('', explanation).strip(' :-')

        # Validate
        q_text = q_text.strip()
        if not q_text or len(q_text) < 5:
            errors.append(f"⚠️ Block skipped (no question text): {block[:50]}…")
            continue
        missing = [k for k in 'ABCD' if k not in opts]
        if missing:
            errors.append(f"⚠️ '{q_text[:40]}…' skipped — missing options: {', '.join(missing)}")
            continue
        if not correct:
            # Default to A if not specified — admin will be told
            correct = 'A'

        questions.append({
            'question':    q_text,
            'option_a':    opts.get('A', ''),
            'option_b':    opts.get('B', ''),
            'option_c':    opts.get('C', ''),
            'option_d':    opts.get('D', ''),
            'correct':     correct,
            'explanation': explanation,
        })

    return questions, errors


# ─────────────────────────────────────────────
#  BOT SETTINGS HELPERS
# ─────────────────────────────────────────────
def get_setting(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO bot_settings(key,value) VALUES(?,?)", (key, str(value)))

# ─────────────────────────────────────────────
#  PUBLIC TRIVIA API FETCHER
# ─────────────────────────────────────────────
import urllib.request, urllib.parse, html

TOPIC_API_MAP = {
    "general":          9,   # General Knowledge
    "science":          17,  # Science & Nature
    "history":          23,  # History
    "geography":        22,  # Geography
    "politics":         24,  # Politics
    "math":             19,  # Mathematics
    "computers":        18,  # Computers
    "sports":           21,  # Sports
    "art":              25,  # Art
    "animals":          27,  # Animals
    "vehicles":         28,  # Vehicles
    "books":            10,  # Entertainment: Books
    "film":             11,  # Entertainment: Film
    "music":            12,  # Entertainment: Music
}

TOPIC_LABELS = {
    "general":   "🌍 General Knowledge",
    "science":   "🔬 General Science",
    "history":   "🏛️ Indian & World History",
    "geography": "🗺️ Geography",
    "politics":  "🏛️ Polity & Constitution",
    "math":      "🔢 Mathematics",
    "computers": "💻 Computers & Technology",
    "sports":    "⚽ Sports & GK",
    "art":       "🎨 Art & Culture",
    "animals":   "🐾 Animals & Nature",
    "books":     "📚 Books & Literature",
    "film":      "🎬 Entertainment",
    "music":     "🎵 Music",
}

def fetch_trivia_question(topic="general"):
    """Fetch one MCQ from Open Trivia DB. Returns dict or None."""
    cat = TOPIC_API_MAP.get(topic, 9)
    url = f"https://opentdb.com/api.php?amount=1&category={cat}&type=multiple"
    try:
        req  = urllib.request.Request(url, headers={"User-Agent": "TestBookBot/5"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read().decode())
        if data.get("response_code") != 0 or not data.get("results"):
            return None
        r   = data["results"][0]
        q   = html.unescape(r["question"])
        ca  = html.unescape(r["correct_answer"])
        inc = [html.unescape(x) for x in r["incorrect_answers"]]
        opts = inc[:3] + [ca]
        random.shuffle(opts)
        letter = ["A","B","C","D"][opts.index(ca)]
        return {
            "question":    q[:290],
            "option_a":    opts[0][:95],
            "option_b":    opts[1][:95],
            "option_c":    opts[2][:95],
            "option_d":    opts[3][:95],
            "correct":     letter,
            "explanation": f"✅ Correct answer: {ca}",
            "source":      "api"
        }
    except Exception as e:
        print(f"API fetch error: {e}")
        return None

def fetch_question_for_topic(topic):
    """Try API first, fallback to local general_questions DB."""
    q = fetch_trivia_question(topic)
    if q:
        return q, "api"
    # Fallback: local DB
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM general_questions WHERE topic=? OR topic='general' ORDER BY RANDOM() LIMIT 1",
            (topic,)
        ).fetchall()
    if rows:
        r = rows[0]
        return {
            "question":    r["question"],
            "option_a":    r["option_a"],
            "option_b":    r["option_b"],
            "option_c":    r["option_c"],
            "option_d":    r["option_d"],
            "correct":     r["correct"],
            "explanation": r["explanation"] or "",
            "source":      "db"
        }, "db"
    return None, None

# ─────────────────────────────────────────────
#  ENDLESS /QUIZ FLOW (Private Chat)
# ─────────────────────────────────────────────
def start_endless_quiz(chat_id, user_id, topic):
    """Start an endless quiz session for a user in private."""
    # Cancel any existing session
    with db() as conn:
        conn.execute(
            "UPDATE endless_sessions SET active=0 WHERE user_id=? AND chat_id=? AND active=1",
            (user_id, chat_id)
        )
        conn.execute("""
            INSERT INTO endless_sessions(user_id, chat_id, topic, active)
            VALUES(?,?,?,1)
        """, (user_id, chat_id, topic))
        sess_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    label = TOPIC_LABELS.get(topic, "🌍 General Knowledge")
    bot.send_message(
        chat_id,
        f"🚀 <b>Endless Quiz Started!</b>\n\n"
        f"📌 Topic: <b>{label}</b>\n"
        f"✅ Correct → +1 point\n"
        f"❌ Wrong → 0 points\n\n"
        f"🔄 Questions never stop — send /stop to end anytime!\n\n"
        f"<i>Fetching your first question...</i> 🎯"
    )
    send_endless_question(chat_id, user_id, sess_id, topic)

def send_endless_question(chat_id, user_id, sess_id, topic=None):
    """Fetch next question and send as quiz poll."""
    with db() as conn:
        sess = conn.execute(
            "SELECT * FROM endless_sessions WHERE id=? AND active=1", (sess_id,)
        ).fetchone()
    if not sess:
        return
    if not topic:
        topic = sess["topic"]

    q, source = fetch_question_for_topic(topic)
    if not q:
        bot.send_message(chat_id,
            "⚠️ <b>Couldn't fetch a question right now!</b>\n"
            "Please try again in a moment or /stop and restart.")
        return

    opt_map = {"A": 0, "B": 1, "C": 2, "D": 3}
    correct_idx = opt_map.get(q["correct"], 0)
    opts = [q["option_a"], q["option_b"], q["option_c"], q["option_d"]]
    exp  = q.get("explanation") or f"✅ Correct: {opts[correct_idx]}"

    try:
        pm = bot.send_poll(
            chat_id=chat_id,
            question=q["question"][:300],
            options=[o[:100] for o in opts],
            type="quiz",
            correct_option_id=correct_idx,
            explanation=exp[:200],
            is_anonymous=False,
            open_period=30,
        )
        with db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO endless_poll_map
                (poll_id, session_id, user_id, chat_id, correct_answer, q_source)
                VALUES(?,?,?,?,?,?)
            """, (pm.poll.id, sess_id, user_id, chat_id, q["correct"], source))
            conn.execute(
                "UPDATE endless_sessions SET total=total+1 WHERE id=?", (sess_id,)
            )
    except Exception as e:
        print(f"Endless poll error: {e}")
        bot.send_message(chat_id, "⚠️ Poll failed — fetching next question...")
        send_endless_question(chat_id, user_id, sess_id, topic)

def stop_endless_quiz(chat_id, user_id):
    """Stop the user's endless quiz and show results."""
    with db() as conn:
        sess = conn.execute(
            "SELECT * FROM endless_sessions WHERE user_id=? AND chat_id=? AND active=1",
            (user_id, chat_id)
        ).fetchone()
        if not sess:
            bot.send_message(chat_id, "ℹ️ No active quiz to stop! Start one with /quiz")
            return
        conn.execute(
            "UPDATE endless_sessions SET active=0 WHERE id=?", (sess["id"],)
        )

    pct = (sess["correct"] / sess["total"] * 100) if sess["total"] else 0
    bar = "🟩" * int(pct // 10) + "⬜" * (10 - int(pct // 10))
    badge = (
        "🏆 Champion!"     if pct >= 90 else
        "🥇 Excellent!"    if pct >= 80 else
        "🥈 Very Good!"    if pct >= 70 else
        "🥉 Good!"         if pct >= 60 else
        "📖 Keep Going!"   if pct >= 40 else
        "💪 Don't Give Up!"
    )
    label = TOPIC_LABELS.get(sess["topic"], "Quiz")
    text = (
        f"🏁 <b>Quiz Stopped!</b>\n\n"
        f"📌 Topic: <b>{label}</b>\n"
        f"{badge}\n\n"
        f"{bar} {pct:.0f}%\n\n"
        f"📊 <b>Results:</b>\n"
        f"✅ Correct: <b>{sess['correct']}</b>\n"
        f"❌ Wrong: <b>{sess['wrong']}</b>\n"
        f"📝 Total: <b>{sess['total']}</b>\n"
        f"⭐ Score: <b>{sess['score']}</b> pts\n\n"
        f"Start again with /quiz 🚀"
    )
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔁 New Quiz", callback_data=f"endless_topic_menu"))
    bot.send_message(chat_id, text, reply_markup=kb)

# ─────────────────────────────────────────────
#  GROUP QUIZ /set + GROUP POLL FLOW
# ─────────────────────────────────────────────
def send_group_question(chat_id):
    """Send next question in a group quiz. Non-blocking — previous polls stay."""
    with db() as conn:
        cfg = conn.execute(
            "SELECT * FROM group_quiz_config WHERE chat_id=? AND active=1", (chat_id,)
        ).fetchone()
    if not cfg:
        return

    topic   = cfg["topic"]
    exam_id = cfg["exam_id"]

    q = None
    if exam_id:
        # Pull from exam question bank
        with db() as conn:
            if cfg["section_id"]:
                row = conn.execute(
                    "SELECT * FROM questions WHERE exam_id=? AND section_id=? ORDER BY RANDOM() LIMIT 1",
                    (exam_id, cfg["section_id"])
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM questions WHERE exam_id=? ORDER BY RANDOM() LIMIT 1",
                    (exam_id,)
                ).fetchone()
        if row:
            q = {
                "question":    row["question"],
                "option_a":    row["option_a"],
                "option_b":    row["option_b"],
                "option_c":    row["option_c"],
                "option_d":    row["option_d"],
                "correct":     row["correct"],
                "explanation": row["explanation"] or "",
            }
    else:
        q, _ = fetch_question_for_topic(topic)

    if not q:
        bot.send_message(chat_id,
            "⚠️ <b>No questions available!</b>\n"
            "Admin: Add questions to the bank or check your topic settings.")
        return

    opt_map = {"A": 0, "B": 1, "C": 2, "D": 3}
    correct_idx = opt_map.get(q["correct"], 0)
    opts = [q["option_a"][:100], q["option_b"][:100], q["option_c"][:100], q["option_d"][:100]]
    exp  = q.get("explanation") or f"✅ Answer: ({q['correct']}) {opts[correct_idx]}"

    with db() as conn:
        cnt = conn.execute(
            "SELECT question_count FROM group_quiz_config WHERE chat_id=?", (chat_id,)
        ).fetchone()["question_count"] + 1

    try:
        pm = bot.send_poll(
            chat_id=chat_id,
            question=f"❓ Q{cnt}: {q['question'][:280]}",
            options=opts,
            type="quiz",
            correct_option_id=correct_idx,
            explanation=exp[:200],
            is_anonymous=False,
            open_period=30,
        )
        with db() as conn:
            conn.execute("""
                UPDATE group_quiz_config
                SET current_poll_id=?, current_q_id=?, correct_answer=?, question_count=?
                WHERE chat_id=?
            """, (pm.poll.id, 0, q["correct"], cnt, chat_id))
            # Store in endless_poll_map for answer tracking
            conn.execute("""
                INSERT OR REPLACE INTO endless_poll_map
                (poll_id, session_id, user_id, chat_id, correct_answer, q_source)
                VALUES(?,?,?,?,?,?)
            """, (pm.poll.id, -1, -1, chat_id, q["correct"], "group"))
    except Exception as e:
        print(f"Group poll error: {e}")

    # Auto-send next question after 35 seconds
    def auto_next():
        time.sleep(35)
        with db() as conn2:
            cfg2 = conn2.execute(
                "SELECT active FROM group_quiz_config WHERE chat_id=?", (chat_id,)
            ).fetchone()
        if cfg2 and cfg2["active"]:
            send_group_question(chat_id)

    threading.Thread(target=auto_next, daemon=True).start()

def show_group_scoreboard(chat_id, announce=False):
    with db() as conn:
        rows = conn.execute("""
            SELECT user_name, score, correct, wrong
            FROM group_scores WHERE chat_id=?
            ORDER BY score DESC LIMIT 10
        """, (chat_id,)).fetchall()
        cfg = conn.execute(
            "SELECT question_count FROM group_quiz_config WHERE chat_id=?", (chat_id,)
        ).fetchone()

    if not rows:
        bot.send_message(chat_id, "📊 No scores yet! Answer some questions first 🎯")
        return

    medals = ["🥇","🥈","🥉"] + ["🏅"] * 7
    text   = "🏆 <b>Group Scoreboard</b>\n\n"
    for i, r in enumerate(rows):
        text += f"{medals[i]} <b>{r['user_name'] or 'Student'}</b> — <b>{r['score']} pts</b> (✅{r['correct']} ❌{r['wrong']})\n"

    if cfg:
        text += f"\n📝 Total Questions Asked: <b>{cfg['question_count']}</b>"

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔁 Refresh", callback_data="group_score_refresh"))
    bot.send_message(chat_id, text, reply_markup=kb)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS exams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        icon TEXT DEFAULT '📘',
        support_group TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS sections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        icon TEXT DEFAULT '📂',
        FOREIGN KEY(exam_id) REFERENCES exams(id)
    );
    CREATE TABLE IF NOT EXISTS quiz_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id INTEGER NOT NULL,
        section_id INTEGER,
        positive_marks REAL DEFAULT 1.0,
        negative_marks REAL DEFAULT 0.25,
        time_per_question INTEGER DEFAULT 60,
        total_questions INTEGER DEFAULT 20,
        FOREIGN KEY(exam_id) REFERENCES exams(id)
    );
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id INTEGER NOT NULL,
        section_id INTEGER,
        question TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        correct TEXT NOT NULL,
        explanation TEXT,
        source TEXT DEFAULT 'manual',
        FOREIGN KEY(exam_id) REFERENCES exams(id)
    );
    CREATE TABLE IF NOT EXISTS practice_sets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id INTEGER NOT NULL,
        section_id INTEGER,
        name TEXT NOT NULL,
        description TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(exam_id) REFERENCES exams(id)
    );
    CREATE TABLE IF NOT EXISTS practice_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        practice_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        correct TEXT NOT NULL,
        explanation TEXT,
        FOREIGN KEY(practice_id) REFERENCES practice_sets(id)
    );
    CREATE TABLE IF NOT EXISTS resources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id INTEGER NOT NULL,
        section_id INTEGER,
        title TEXT NOT NULL,
        file_id TEXT,
        file_type TEXT,
        url TEXT,
        FOREIGN KEY(exam_id) REFERENCES exams(id)
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS quiz_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        exam_id INTEGER,
        section_id INTEGER,
        practice_id INTEGER,
        session_type TEXT DEFAULT 'quiz',
        question_ids TEXT,
        current_index INTEGER DEFAULT 0,
        answers TEXT DEFAULT '{}',
        score REAL DEFAULT 0,
        start_time TEXT,
        end_time TEXT,
        status TEXT DEFAULT 'active',
        positive_marks REAL DEFAULT 1.0,
        negative_marks REAL DEFAULT 0.25,
        time_per_question INTEGER DEFAULT 60,
        poll_msg_ids TEXT DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS user_question_history (
        user_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        session_type TEXT DEFAULT 'quiz',
        asked_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, question_id, session_type)
    );
    CREATE TABLE IF NOT EXISTS admin_states (
        user_id INTEGER PRIMARY KEY,
        state TEXT,
        data TEXT DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS poll_map (
        poll_id TEXT PRIMARY KEY,
        session_id INTEGER,
        q_index INTEGER,
        q_db_id INTEGER,
        chat_id INTEGER,
        user_id INTEGER,
        session_type TEXT DEFAULT 'quiz'
    );
    CREATE TABLE IF NOT EXISTS group_quiz_config (
        chat_id INTEGER PRIMARY KEY,
        exam_id INTEGER,
        section_id INTEGER,
        topic TEXT DEFAULT 'general',
        active INTEGER DEFAULT 0,
        current_poll_id TEXT,
        current_q_id INTEGER,
        correct_answer TEXT,
        question_count INTEGER DEFAULT 0,
        set_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS group_scores (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        user_name TEXT,
        score INTEGER DEFAULT 0,
        correct INTEGER DEFAULT 0,
        wrong INTEGER DEFAULT 0,
        PRIMARY KEY(chat_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS endless_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        topic TEXT NOT NULL,
        score INTEGER DEFAULT 0,
        correct INTEGER DEFAULT 0,
        wrong INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        last_q_id TEXT,
        started_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS endless_poll_map (
        poll_id TEXT PRIMARY KEY,
        session_id INTEGER,
        user_id INTEGER,
        chat_id INTEGER,
        correct_answer TEXT,
        q_source TEXT DEFAULT 'api'
    );
    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS general_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        question TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        correct TEXT NOT NULL,
        explanation TEXT,
        source TEXT DEFAULT 'admin'
    );
    """)
    conn.commit()
    conn.close()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def is_admin(uid): return uid == ADMIN_ID

def register_user(user):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users(id,username,full_name) VALUES(?,?,?)",
            (user.id, user.username, user.full_name)
        )

def get_admin_state(uid):
    with db() as conn:
        row = conn.execute("SELECT state,data FROM admin_states WHERE user_id=?", (uid,)).fetchone()
        if row: return row["state"], json.loads(row["data"])
    return None, {}

def set_admin_state(uid, state, data=None):
    if data is None: data = {}
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_states(user_id,state,data) VALUES(?,?,?)",
            (uid, state, json.dumps(data))
        )

def clear_admin_state(uid):
    with db() as conn:
        conn.execute("DELETE FROM admin_states WHERE user_id=?", (uid,))

# ─────────────────────────────────────────────
#  ██████╗ ██████╗ ███████╗    ███████╗██╗  ██╗████████╗██████╗  █████╗  ██████╗████████╗
#  PDF EXTRACTION — NEAR-PERFECT MULTI-STRATEGY ENGINE
# ─────────────────────────────────────────────

def _clean(s):
    """Normalize unicode, remove junk characters."""
    if not s: return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    s = re.sub(r'[ \t]+', ' ', s)
    return s.strip()

def _extract_text_pymupdf(file_bytes):
    if not PYMUPDF_OK: return ""
    text = ""
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page in doc:
            text += page.get_text("text") + "\n\n"
        doc.close()
    except Exception as e:
        print(f"PyMuPDF error: {e}")
    return text

def _extract_text_pdfplumber(file_bytes):
    if not PDFPLUMBER_OK: return ""
    text = ""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=2, y_tolerance=2)
                if t: text += t + "\n\n"
    except Exception as e:
        print(f"pdfplumber error: {e}")
    return text

def extract_raw_text(file_bytes):
    """Get best raw text from PDF."""
    for fn in [_extract_text_pymupdf, _extract_text_pdfplumber]:
        t = fn(file_bytes)
        if t.strip():
            return t
    return ""

# ── Option letter normalizer ────────────────────────────────────────────────
_OPT_NUM = {'1':'A','2':'B','3':'C','4':'D',
            '①':'A','②':'B','③':'C','④':'D'}

_OPT_RE = re.compile(
    r'^[\s\(\[]*([ABCDabcd1234①②③④])[)\]\.:\s]+(.+)',
    re.DOTALL
)

def _parse_option_line(line):
    m = _OPT_RE.match(line.strip())
    if not m: return None, None
    k = m.group(1).upper()
    k = _OPT_NUM.get(k, k)
    return k, _clean(m.group(2))

# ── Answer line detector ────────────────────────────────────────────────────
_ANS_RE = re.compile(
    r'(?:ans(?:wer)?|correct\s*(?:ans(?:wer)?)?|key|उत्तर)[:\.\s]*'
    r'[\(\[]?([ABCDabcd1234①②③④])[\)\]]?',
    re.IGNORECASE
)

def _find_answer(text):
    m = _ANS_RE.search(text)
    if m:
        k = m.group(1).upper()
        return _OPT_NUM.get(k, k)
    return None

def _is_junk_line(line):
    """Detect header/footer/watermark/page-number junk lines."""
    s = line.strip()
    if not s: return True
    if re.match(r'^\d{1,4}$', s): return True          # page numbers
    if len(s) < 3: return True                          # too short
    # Lines that are only punctuation or symbols
    if re.match(r'^[\-=_\*\.•~]+$', s): return True
    # Common watermark / header patterns
    if re.search(r'(www\.|\.com|\.in|copyright|©|all rights|visit us)', s, re.IGNORECASE): return True
    return False

def _option_is_garbage(text):
    """Detect options that are clearly not MCQ options."""
    if not text or len(text.strip()) < 2: return True
    # If option text is just a number or single letter
    if re.match(r'^[A-Da-d\d]\.?$', text.strip()): return True
    return False

def _question_is_garbage(text):
    """Detect question text that is clearly not a real question."""
    if not text or len(text.strip()) < 8: return True
    # Purely numeric
    if re.match(r'^[\d\s\.]+$', text.strip()): return True
    return False

def extract_mcqs_from_pdf(file_bytes):
    """
    Master MCQ extractor. Returns (list_of_mcq_dicts, error_or_None).
    Uses line-by-line state machine — most reliable across PDF formats.
    """
    raw = extract_raw_text(file_bytes)
    if not raw.strip():
        return [], "❌ No readable text found in PDF. It may be a scanned/image-based PDF."

    # ── Pre-clean: remove junk lines ──────────────────────────────────────
    lines = []
    for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if not _is_junk_line(line):
            lines.append(_clean(line))

    # ── State-machine parser ───────────────────────────────────────────────
    questions = []
    i = 0
    last_q_num = -1

    # Detect question-start line: optional Q/Q. prefix, number, dot/paren, then text
    Q_START = re.compile(
        r'^(?:Q\.?\s*)?(\d{1,3})[\.)\s]\s*(.{5,})',
        re.IGNORECASE
    )

    while i < len(lines):
        line = lines[i]
        qm = Q_START.match(line)

        if qm:
            q_num = int(qm.group(1))
            # Accept if sequential or first question or reasonable jump
            if last_q_num == -1 or q_num == last_q_num + 1 or (q_num > last_q_num and q_num <= last_q_num + 3):
                q_text = _clean(qm.group(2))
                j = i + 1
                opts = {}
                answer = None
                explanation = ""

                # Absorb multi-line question body (lines before first option)
                while j < len(lines):
                    k, v = _parse_option_line(lines[j])
                    if k and k in 'ABCD':
                        break
                    if Q_START.match(lines[j]):
                        break  # next question started without any options — abort
                    if _ANS_RE.search(lines[j]):
                        break
                    # Only absorb if it looks like question continuation (not an isolated header)
                    candidate = lines[j].strip()
                    if candidate and len(candidate) > 2:
                        q_text += ' ' + candidate
                    j += 1

                # Collect options A-D
                while j < len(lines) and len(opts) < 4:
                    k, v = _parse_option_line(lines[j])
                    if k and k in 'ABCD':
                        opts[k] = v
                        j += 1
                        # Handle multi-line option text
                        while j < len(lines):
                            nk, _ = _parse_option_line(lines[j])
                            if nk and nk in 'ABCD': break
                            if _ANS_RE.search(lines[j]): break
                            if Q_START.match(lines[j]): break
                            tail = lines[j].strip()
                            if tail:
                                opts[k] += ' ' + tail
                            j += 1
                    elif _ANS_RE.search(lines[j]):
                        break
                    elif Q_START.match(lines[j]) and len(lines[j]) > 8:
                        break
                    else:
                        j += 1

                # Look for answer line within next 4 lines
                for k2 in range(j, min(j + 4, len(lines))):
                    a = _find_answer(lines[k2])
                    if a:
                        answer = a
                        exp_m = re.search(r'(?:expl?(?:anation)?|solution|sol)[:\.\s]+(.+)',
                                          lines[k2], re.IGNORECASE)
                        if exp_m:
                            explanation = _clean(exp_m.group(1))
                        j = k2 + 1
                        break

                # Validate and store
                if (all(k in opts for k in 'ABCD') and
                        not any(_option_is_garbage(opts[k]) for k in 'ABCD') and
                        not _question_is_garbage(q_text)):
                    if not answer:
                        answer = 'A'  # default if answer key missing
                    questions.append({
                        "question":   q_text.strip()[:900],
                        "option_a":   opts['A'].strip()[:200],
                        "option_b":   opts['B'].strip()[:200],
                        "option_c":   opts['C'].strip()[:200],
                        "option_d":   opts['D'].strip()[:200],
                        "correct":    answer,
                        "explanation": explanation[:500]
                    })
                    last_q_num = q_num
                    i = j
                    continue

        i += 1

    # Deduplicate by question text
    seen = set()
    unique = []
    for q in questions:
        key = q['question'][:60].lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(q)

    return unique, None

# ─────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────
def main_menu_kb(uid):
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row(KeyboardButton("📚 Exams"), KeyboardButton("🎯 Quick Quiz"))
    kb.row(KeyboardButton("🏆 Leaderboard"), KeyboardButton("📊 My Progress"))
    kb.row(KeyboardButton("🤝 Doubt Group"), KeyboardButton("ℹ️ Help"))
    if is_admin(uid):
        kb.row(KeyboardButton("⚙️ Admin Panel"))
    return kb

def back_btn(cb):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️ Back", callback_data=cb))
    return kb

def exam_menu_kb(exam_id):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📝 Take Test", callback_data=f"exam_test_{exam_id}"),
        InlineKeyboardButton("📖 Practice", callback_data=f"exam_practice_{exam_id}"),
        InlineKeyboardButton("📂 Resources", callback_data=f"exam_resources_{exam_id}"),
        InlineKeyboardButton("💬 Help Group", callback_data=f"exam_group_{exam_id}"),
        InlineKeyboardButton("◀️ Back to Exams", callback_data="back_exams")
    )
    return kb

def admin_main_kb():
    """Admin panel — clean 2-column inline layout."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Add Exam", callback_data="admin_add_exam"),
        InlineKeyboardButton("📋 Manage Exams", callback_data="admin_manage_exams"),
        InlineKeyboardButton("➕ Add Section", callback_data="admin_add_section"),
        InlineKeyboardButton("✏️ Add Question", callback_data="admin_add_question"),
        InlineKeyboardButton("📤 Upload Quiz PDF", callback_data="admin_upload_quiz_pdf"),
        InlineKeyboardButton("📤 Practice PDF", callback_data="admin_upload_practice_pdf"),
        InlineKeyboardButton("⚙️ Quiz Settings", callback_data="admin_quiz_settings"),
        InlineKeyboardButton("📎 Add Resource", callback_data="admin_add_resource"),
        InlineKeyboardButton("💬 Set Support Group", callback_data="admin_set_group"),
        InlineKeyboardButton("🤝 Set Doubt Group", callback_data="admin_set_doubt_group"),
        InlineKeyboardButton("🌍 General Qs Bank", callback_data="admin_general_qs"),
        InlineKeyboardButton("🔧 Manage Qs", callback_data="admin_manage_questions"),
        InlineKeyboardButton("👥 User Stats", callback_data="admin_stats"),
    )
    return kb

# ─────────────────────────────────────────────
#  QUIZ FLOW — TELEGRAM NATIVE QUIZ POLLS
# ─────────────────────────────────────────────
def send_quiz_poll(chat_id, session_id, idx):
    """Send question as Telegram native quiz poll ONLY — no header message, no fallback inline."""
    with db() as conn:
        sess = conn.execute("SELECT * FROM quiz_sessions WHERE id=?", (session_id,)).fetchone()
        if not sess or sess["status"] != "active":
            return

        q_ids = json.loads(sess["question_ids"])
        if idx >= len(q_ids):
            end_quiz(chat_id, session_id)
            return

        # Check this index hasn't already been answered (race condition guard)
        answers = json.loads(sess["answers"])
        if str(q_ids[idx]) in answers:
            # Already answered, advance
            send_quiz_poll(chat_id, session_id, idx + 1)
            return

        q = conn.execute("SELECT * FROM questions WHERE id=?", (q_ids[idx],)).fetchone()
        if not q:
            send_quiz_poll(chat_id, session_id, idx + 1)
            return

    opt_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
    correct_idx = opt_map.get(q['correct'].upper(), 0)

    options = [o[:100] for o in [q['option_a'], q['option_b'], q['option_c'], q['option_d']]]
    total = len(json.loads(sess["question_ids"]))
    # Put progress in poll question since we can't send a separate header
    progress = f"[{idx+1}/{total}] "
    question_text = (progress + q['question'])[:300]

    explanation = q['explanation'] or f"✅ Correct: ({q['correct']}) {options[correct_idx]}"

    try:
        poll_msg = bot.send_poll(
            chat_id=chat_id,
            question=question_text,
            options=options,
            type="quiz",
            correct_option_id=correct_idx,
            explanation=explanation[:200],
            is_anonymous=False,
            open_period=max(5, min(sess['time_per_question'], 600)),
        )

        with db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO poll_map
                (poll_id, session_id, q_index, q_db_id, chat_id, user_id, session_type)
                VALUES(?,?,?,?,?,?,?)
            """, (poll_msg.poll.id, session_id, idx, q_ids[idx],
                  chat_id, sess['user_id'], 'quiz'))

            pmids = json.loads(sess['poll_msg_ids'] or '{}')
            pmids[str(idx)] = poll_msg.message_id
            conn.execute("UPDATE quiz_sessions SET poll_msg_ids=? WHERE id=?",
                         (json.dumps(pmids), session_id))

    except Exception as e:
        print(f"Poll send failed: {e}")
        # Only use fallback for inline if poll truly failed
        _send_quiz_fallback(chat_id, session_id, idx, q)
        return

    # Auto-advance timer: only triggers if user has NOT answered yet
    wait_secs = max(5, min(sess['time_per_question'], 600))
    q_db_id = q_ids[idx]

    def auto_advance():
        time.sleep(wait_secs + 3)
        with db() as conn:
            s = conn.execute("SELECT * FROM quiz_sessions WHERE id=?", (session_id,)).fetchone()
            if not s or s['status'] != 'active':
                return
            answers = json.loads(s['answers'])
            key = str(q_db_id)
            if key in answers:
                return  # already answered — poll handler will advance
            # Time expired, mark skipped and advance
            answers[key] = "SKIP"
            conn.execute("UPDATE quiz_sessions SET answers=?,current_index=? WHERE id=?",
                         (json.dumps(answers), idx + 1, session_id))
        send_quiz_poll(chat_id, session_id, idx + 1)

    threading.Thread(target=auto_advance, daemon=True).start()

def _send_quiz_fallback(chat_id, session_id, idx, q):
    """Fallback: inline keyboard buttons when poll API fails."""
    with db() as conn:
        sess = conn.execute("SELECT question_ids FROM quiz_sessions WHERE id=?", (session_id,)).fetchone()
    q_ids = json.loads(sess['question_ids'])
    total = len(q_ids)

    text = (
        f"<b>{q['question']}</b>\n\n"
        f"🅐 {q['option_a']}\n"
        f"🅑 {q['option_b']}\n"
        f"🅒 {q['option_c']}\n"
        f"🅓 {q['option_d']}"
    )
    kb = InlineKeyboardMarkup(row_width=4)
    kb.add(
        InlineKeyboardButton("A", callback_data=f"ans_{session_id}_{idx}_A"),
        InlineKeyboardButton("B", callback_data=f"ans_{session_id}_{idx}_B"),
        InlineKeyboardButton("C", callback_data=f"ans_{session_id}_{idx}_C"),
        InlineKeyboardButton("D", callback_data=f"ans_{session_id}_{idx}_D"),
    )
    kb.add(InlineKeyboardButton("⏭ Skip", callback_data=f"ans_{session_id}_{idx}_SKIP"))
    bot.send_message(chat_id, text, reply_markup=kb)

# ─────────────────────────────────────────────
#  POLL ANSWER HANDLER — handles endless + group + regular quiz
# ─────────────────────────────────────────────
@bot.poll_answer_handler()
def handle_poll_answer_all(poll_answer):
    """Universal poll answer handler."""
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    chosen  = poll_answer.option_ids

    # 1. Check endless/group poll map first
    with db() as conn:
        ep = conn.execute(
            "SELECT * FROM endless_poll_map WHERE poll_id=?", (poll_id,)
        ).fetchone()

    if ep:
        chat_id      = ep["chat_id"]
        correct_ans  = ep["correct_answer"]
        session_id   = ep["session_id"]
        q_source     = ep["q_source"]

        opt_letters  = ["A", "B", "C", "D"]
        given        = opt_letters[chosen[0]] if chosen else None
        is_correct   = (given == correct_ans) if given else False

        if q_source == "group":
            # Group quiz — update group scores
            try:
                member = bot.get_chat_member(chat_id, user_id)
                uname  = member.user.first_name or member.user.username or "Student"
            except:
                uname = "Student"
            with db() as conn:
                if is_correct:
                    conn.execute("""
                        INSERT INTO group_scores(chat_id, user_id, user_name, score, correct)
                        VALUES(?,?,?,1,1)
                        ON CONFLICT(chat_id,user_id) DO UPDATE SET
                        score=score+1, correct=correct+1, user_name=excluded.user_name
                    """, (chat_id, user_id, uname))
                else:
                    conn.execute("""
                        INSERT INTO group_scores(chat_id, user_id, user_name, wrong)
                        VALUES(?,?,?,1)
                        ON CONFLICT(chat_id,user_id) DO UPDATE SET
                        wrong=wrong+1, user_name=excluded.user_name
                    """, (chat_id, user_id, uname))
            return  # next q is sent by auto_next thread

        else:
            # Endless personal quiz
            with db() as conn:
                sess = conn.execute(
                    "SELECT * FROM endless_sessions WHERE id=? AND active=1", (session_id,)
                ).fetchone()
            if not sess or sess["user_id"] != user_id:
                return
            with db() as conn:
                if is_correct:
                    conn.execute("""
                        UPDATE endless_sessions
                        SET score=score+1, correct=correct+1
                        WHERE id=?
                    """, (session_id,))
                else:
                    conn.execute(
                        "UPDATE endless_sessions SET wrong=wrong+1 WHERE id=?",
                        (session_id,)
                    )
            # Send next question after short delay
            def next_q():
                time.sleep(2)
                send_endless_question(chat_id, user_id, session_id)
            threading.Thread(target=next_q, daemon=True).start()
        return

    # 2. Fallthrough to original quiz poll handler
    handle_poll_answer_original(poll_answer)

def handle_poll_answer_original(poll_answer):
    """Handle user's answer to a quiz poll."""
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    chosen  = poll_answer.option_ids

    with db() as conn:
        pm = conn.execute(
            "SELECT * FROM poll_map WHERE poll_id=? AND user_id=?", (poll_id, user_id)
        ).fetchone()

    if not pm:
        return

    session_id = pm['session_id']
    idx        = pm['q_index']
    q_db_id    = pm['q_db_id']
    chat_id    = pm['chat_id']

    with db() as conn:
        sess = conn.execute("SELECT * FROM quiz_sessions WHERE id=?", (session_id,)).fetchone()
        if not sess or sess['status'] != 'active':
            return

        answers = json.loads(sess['answers'])
        key = str(q_db_id)

        if key in answers:
            return  # already answered (by timer or previous click)

        answer_letter = "SKIP" if not chosen else (['A','B','C','D'][chosen[0]] if chosen[0] < 4 else 'A')

        answers[key] = answer_letter
        conn.execute("UPDATE quiz_sessions SET answers=?,current_index=? WHERE id=?",
                     (json.dumps(answers), idx + 1, session_id))
        conn.execute("""
            INSERT OR REPLACE INTO user_question_history(user_id, question_id, session_type)
            VALUES(?,?,'quiz')
        """, (user_id, q_db_id))

    # Advance — small delay so poll UI shows result first
    time.sleep(2)
    send_quiz_poll(chat_id, session_id, idx + 1)

def handle_quiz_answer(chat_id, user_id, session_id, idx, answer):
    """Handle fallback inline button answer."""
    with db() as conn:
        sess = conn.execute("SELECT * FROM quiz_sessions WHERE id=?", (session_id,)).fetchone()
        if not sess or sess["status"] != "active": return

        q_ids   = json.loads(sess["question_ids"])
        answers = json.loads(sess["answers"])
        key     = str(q_ids[idx])

        if key in answers: return

        q = conn.execute("SELECT * FROM questions WHERE id=?", (q_ids[idx],)).fetchone()
        answers[key] = answer
        conn.execute("UPDATE quiz_sessions SET answers=?,current_index=? WHERE id=?",
                     (json.dumps(answers), idx + 1, session_id))
        conn.execute("""
            INSERT OR REPLACE INTO user_question_history(user_id,question_id,session_type)
            VALUES(?,?,'quiz')
        """, (user_id, q_ids[idx]))

    correct = answer == q["correct"]
    if answer == "SKIP":
        result = "⏭ <b>Skipped!</b>"
    elif correct:
        result = f"✅ <b>Correct!</b> +{sess['positive_marks']} marks"
    else:
        result = (f"❌ <b>Wrong!</b> -{sess['negative_marks']} marks\n"
                  f"✅ Correct: <b>({q['correct']}) {q['option_' + q['correct'].lower()]}</b>")

    if q["explanation"] and answer != "SKIP":
        result += f"\n\n💡 <i>{q['explanation']}</i>"

    bot.send_message(chat_id, result)
    time.sleep(0.8)
    send_quiz_poll(chat_id, session_id, idx + 1)

def start_quiz(chat_id, user_id, exam_id, section_id):
    with db() as conn:
        if section_id:
            qs = conn.execute(
                "SELECT * FROM quiz_settings WHERE exam_id=? AND section_id=? LIMIT 1",
                (exam_id, section_id)
            ).fetchone()
        else:
            qs = None

        if not qs:
            qs = conn.execute(
                "SELECT * FROM quiz_settings WHERE exam_id=? AND section_id IS NULL LIMIT 1",
                (exam_id,)
            ).fetchone()

        pos  = qs["positive_marks"]   if qs else 1.0
        neg  = qs["negative_marks"]   if qs else 0.25
        tpq  = qs["time_per_question"] if qs else 60
        total = qs["total_questions"]  if qs else 20

        seen = conn.execute(
            "SELECT question_id FROM user_question_history WHERE user_id=? AND session_type='quiz'",
            (user_id,)
        ).fetchall()
        seen_ids = {r["question_id"] for r in seen}

        if section_id:
            all_qs = conn.execute(
                "SELECT id FROM questions WHERE exam_id=? AND section_id=?", (exam_id, section_id)
            ).fetchall()
        else:
            all_qs = conn.execute(
                "SELECT id FROM questions WHERE exam_id=?", (exam_id,)
            ).fetchall()

        all_ids = [r["id"] for r in all_qs]
        fresh   = [i for i in all_ids if i not in seen_ids]

        if len(fresh) < 5:
            conn.execute(
                "DELETE FROM user_question_history WHERE user_id=? AND session_type='quiz'",
                (user_id,)
            )
            fresh = all_ids

        if not fresh:
            bot.send_message(chat_id,
                "❌ <b>No questions available yet!</b>\n"
                "Admin needs to add questions for this exam.",
                reply_markup=back_btn(f"exam_open_{exam_id}"))
            return

        selected = random.sample(fresh, min(total, len(fresh)))

        conn.execute("""
            INSERT INTO quiz_sessions
            (user_id,exam_id,section_id,question_ids,start_time,positive_marks,negative_marks,time_per_question)
            VALUES(?,?,?,?,?,?,?,?)
        """, (user_id, exam_id, section_id, json.dumps(selected),
              datetime.now().isoformat(), pos, neg, tpq))
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    bot.send_message(
        chat_id,
        f"🚀 <b>Quiz Starting!</b>\n\n"
        f"📊 {len(selected)} questions\n"
        f"✅ +{pos} correct  ❌ -{neg} wrong\n"
        f"⏱ {tpq}s per question\n\n"
        f"<i>Answer each poll question before the timer runs out!</i>"
    )
    time.sleep(1)
    send_quiz_poll(chat_id, session_id, 0)

def end_quiz(chat_id, session_id):
    with db() as conn:
        sess = conn.execute("SELECT * FROM quiz_sessions WHERE id=?", (session_id,)).fetchone()
        if not sess: return

        q_ids   = json.loads(sess["question_ids"])
        answers = json.loads(sess["answers"])
        pos     = sess["positive_marks"]
        neg     = sess["negative_marks"]

        score = 0
        correct = wrong = skipped = 0
        for qid in q_ids:
            ans = answers.get(str(qid), "SKIP")
            q = conn.execute("SELECT correct FROM questions WHERE id=?", (qid,)).fetchone()
            if not q: continue
            if ans == "SKIP":
                skipped += 1
            elif ans == q["correct"]:
                score += pos; correct += 1
            else:
                score -= neg; wrong += 1

        score = max(0, score)
        total = len(q_ids)
        pct   = (correct / total * 100) if total else 0

        conn.execute("""
            UPDATE quiz_sessions SET score=?,end_time=?,status='completed' WHERE id=?
        """, (score, datetime.now().isoformat(), session_id))

    badge = (
        "🏆 Excellent!" if pct >= 80 else
        "👍 Good Job!"  if pct >= 60 else
        "📖 Keep Practicing" if pct >= 40 else
        "💪 Don't Give Up!"
    )
    bar = "🟩" * int(pct // 10) + "🟥" * (10 - int(pct // 10))

    text = (
        f"🎉 <b>Quiz Completed!</b>\n\n"
        f"{badge}\n\n"
        f"{bar}\n"
        f"📊 <b>Score:</b> {score:.1f} / {total * pos:.1f}\n"
        f"📈 <b>{pct:.1f}%</b>\n\n"
        f"✅ Correct: <b>{correct}</b>  "
        f"❌ Wrong: <b>{wrong}</b>  "
        f"⏭ Skipped: <b>{skipped}</b>\n\n"
        f"Keep practicing to improve! 📚"
    )
    kb = InlineKeyboardMarkup(row_width=2)
    if sess["exam_id"]:
        kb.add(
            InlineKeyboardButton("🔁 Retry", callback_data=f"start_quiz_{sess['exam_id']}_{sess['section_id'] or 0}"),
            InlineKeyboardButton("🏠 Home", callback_data=f"exam_open_{sess['exam_id']}")
        )
    bot.send_message(chat_id, text, reply_markup=kb)

# ─────────────────────────────────────────────
#  USER FLOW
# ─────────────────────────────────────────────
def show_exams(chat_id, msg_id=None):
    with db() as conn:
        exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()

    if not exams:
        text = "📭 <b>No exams available yet!</b>\nCheck back later. 🙏"
        if msg_id:
            try: bot.edit_message_text(text, chat_id, msg_id)
            except: bot.send_message(chat_id, text)
        else:
            bot.send_message(chat_id, text)
        return

    kb = InlineKeyboardMarkup(row_width=1)
    for e in exams:
        kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}", callback_data=f"exam_open_{e['id']}"))

    text = "📚 <b>Available Exams</b>\n\nChoose your exam to get started! 🎯"
    if msg_id:
        try: bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)
        except: bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

def show_exam_detail(chat_id, exam_id, msg_id=None):
    with db() as conn:
        e       = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
        sections = conn.execute("SELECT * FROM sections WHERE exam_id=?", (exam_id,)).fetchall()
        q_count  = conn.execute("SELECT COUNT(*) as c FROM questions WHERE exam_id=?", (exam_id,)).fetchone()["c"]
        p_count  = conn.execute("SELECT COUNT(*) as c FROM practice_sets WHERE exam_id=?", (exam_id,)).fetchone()["c"]

    if not e:
        bot.send_message(chat_id, "❌ Exam not found!"); return

    text = (
        f"{e['icon']} <b>{e['name']}</b>\n\n"
        f"📄 {e['description'] or 'Exam preparation course'}\n\n"
        f"📂 Sections: <b>{len(sections)}</b>\n"
        f"❓ Questions: <b>{q_count}</b>\n"
        f"📝 Practice Sets: <b>{p_count}</b>\n\n"
        f"Choose an option below 👇"
    )
    kb = exam_menu_kb(exam_id)
    if msg_id:
        try: bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)
        except: bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

def show_test_sections(chat_id, exam_id, msg_id=None):
    with db() as conn:
        sections = conn.execute("SELECT * FROM sections WHERE exam_id=?", (exam_id,)).fetchall()
        qs       = conn.execute(
            "SELECT * FROM quiz_settings WHERE exam_id=? AND section_id IS NULL LIMIT 1", (exam_id,)
        ).fetchone()

    marks = f"✅ +{qs['positive_marks']}  ❌ -{qs['negative_marks']}" if qs else "✅ +1  ❌ -0.25"
    time_q = f"⏱ {qs['time_per_question']}s/q" if qs else "⏱ 60s/q"

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🎯 Full Exam Test", callback_data=f"start_quiz_{exam_id}_0"))
    for s in sections:
        kb.add(InlineKeyboardButton(f"{s['icon']} {s['name']}", callback_data=f"start_quiz_{exam_id}_{s['id']}"))
    kb.add(InlineKeyboardButton("◀️ Back", callback_data=f"exam_open_{exam_id}"))

    text = f"📝 <b>Select Test Section</b>\n\n{marks} · {time_q}\n\nChoose a section or full exam:"
    if msg_id:
        try: bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)
        except: bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

def show_practice_sets(chat_id, exam_id, msg_id=None):
    with db() as conn:
        sets = conn.execute(
            "SELECT * FROM practice_sets WHERE exam_id=? ORDER BY id DESC", (exam_id,)
        ).fetchall()

    if not sets:
        text = "📭 <b>No practice sets yet!</b>"
        _edit_or_send(chat_id, msg_id, text, back_btn(f"exam_open_{exam_id}"))
        return

    kb = InlineKeyboardMarkup(row_width=1)
    for s in sets:
        with db() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) as c FROM practice_questions WHERE practice_id=?", (s["id"],)
            ).fetchone()["c"]
        kb.add(InlineKeyboardButton(
            f"📝 {s['name']} ({cnt} Qs)",
            callback_data=f"practice_start_{exam_id}_{s['id']}"
        ))
    kb.add(InlineKeyboardButton("◀️ Back", callback_data=f"exam_open_{exam_id}"))

    _edit_or_send(chat_id, msg_id, "📖 <b>Practice Sets</b>\n\nChoose a set to start:", kb)

def _edit_or_send(chat_id, msg_id, text, kb=None):
    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)
            return
        except: pass
    bot.send_message(chat_id, text, reply_markup=kb)

def start_practice(chat_id, user_id, exam_id, practice_id, page=0):
    PER_PAGE = 10
    with db() as conn:
        seen = conn.execute(
            "SELECT question_id FROM user_question_history WHERE user_id=? AND session_type='practice'",
            (user_id,)
        ).fetchall()
        seen_ids = {r["question_id"] for r in seen}

        all_qs = conn.execute(
            "SELECT * FROM practice_questions WHERE practice_id=?", (practice_id,)
        ).fetchall()

        fresh = [q for q in all_qs if q["id"] not in seen_ids]
        if not fresh:
            conn.execute(
                "DELETE FROM user_question_history WHERE user_id=? AND session_type='practice'",
                (user_id,)
            )
            fresh = list(all_qs)

    if not fresh:
        bot.send_message(chat_id, "❌ No questions in this practice set!"); return

    batch      = fresh[page * PER_PAGE:(page + 1) * PER_PAGE]
    total_pages = (len(fresh) + PER_PAGE - 1) // PER_PAGE

    for q in batch:
        # Use native quiz poll for practice too
        opt_map = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        correct_idx = opt_map.get(q['correct'].upper(), 0)
        options = [q['option_a'][:100], q['option_b'][:100], q['option_c'][:100], q['option_d'][:100]]
        exp = q['explanation'] or f"✅ Answer: ({q['correct']}) {options[correct_idx]}"

        try:
            bot.send_poll(
                chat_id=chat_id,
                question=q['question'][:300],
                options=options,
                type="quiz",
                correct_option_id=correct_idx,
                explanation=exp[:200],
                is_anonymous=False,
            )
        except Exception:
            # Fallback
            text = (
                f"📌 <b>Practice</b>\n\n<b>{q['question']}</b>\n\n"
                f"🅐 {q['option_a']}\n🅑 {q['option_b']}\n🅒 {q['option_c']}\n🅓 {q['option_d']}"
            )
            kb = InlineKeyboardMarkup(row_width=4)
            kb.add(
                InlineKeyboardButton("A", callback_data=f"pans_{practice_id}_{q['id']}_A_{page}"),
                InlineKeyboardButton("B", callback_data=f"pans_{practice_id}_{q['id']}_B_{page}"),
                InlineKeyboardButton("C", callback_data=f"pans_{practice_id}_{q['id']}_C_{page}"),
                InlineKeyboardButton("D", callback_data=f"pans_{practice_id}_{q['id']}_D_{page}"),
            )
            bot.send_message(chat_id, text, reply_markup=kb)

        with db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO user_question_history(user_id,question_id,session_type)
                VALUES(?,?,'practice')
            """, (user_id, q["id"]))

    # Navigation
    kb2 = InlineKeyboardMarkup(row_width=1)
    if page + 1 < total_pages:
        remaining = len(fresh) - (page + 1) * PER_PAGE
        kb2.add(InlineKeyboardButton(
            f"▶️ Next 10 Questions ({remaining} remaining)",
            callback_data=f"practice_next_{exam_id}_{practice_id}_{page+1}"
        ))
    kb2.add(InlineKeyboardButton("◀️ Back to Practice Sets", callback_data=f"exam_practice_{exam_id}"))

    msg = f"📊 <b>Showing {len(batch)} of {len(fresh)} questions</b>"
    if page + 1 >= total_pages:
        msg += "\n✅ All questions shown! Great job!"

    bot.send_message(chat_id, msg, reply_markup=kb2)

def handle_practice_answer(chat_id, practice_id, q_id, answer, page):
    with db() as conn:
        q = conn.execute("SELECT * FROM practice_questions WHERE id=?", (q_id,)).fetchone()
    if not q: return
    correct = answer == q["correct"]
    result = (
        f"{'✅' if correct else '❌'} <b>{'Correct!' if correct else 'Wrong!'}</b>\n"
        f"✅ Answer: <b>({q['correct']}) {q['option_' + q['correct'].lower()]}</b>"
    )
    if q["explanation"]:
        result += f"\n💡 <i>{q['explanation']}</i>"
    bot.send_message(chat_id, result)

def show_resources(chat_id, exam_id, msg_id=None):
    with db() as conn:
        resources = conn.execute(
            "SELECT * FROM resources WHERE exam_id=? ORDER BY id DESC", (exam_id,)
        ).fetchall()

    if not resources:
        _edit_or_send(chat_id, msg_id, "📭 <b>No resources available yet!</b>",
                      back_btn(f"exam_open_{exam_id}"))
        return

    kb = InlineKeyboardMarkup(row_width=1)
    for r in resources:
        icon = "📄" if r["file_type"] == "pdf" else "🖼" if r["file_type"] == "photo" else "🔗"
        kb.add(InlineKeyboardButton(f"{icon} {r['title']}", callback_data=f"res_get_{r['id']}"))
    kb.add(InlineKeyboardButton("◀️ Back", callback_data=f"exam_open_{exam_id}"))

    text = f"📂 <b>Study Resources</b>\n\nTap any resource to download:"
    _edit_or_send(chat_id, msg_id, text, kb)

# ─────────────────────────────────────────────
#  ADMIN PANEL
# ─────────────────────────────────────────────
def show_admin_panel(chat_id, msg_id=None):
    with db() as conn:
        exams     = conn.execute("SELECT COUNT(*) as c FROM exams").fetchone()["c"]
        questions = conn.execute("SELECT COUNT(*) as c FROM questions").fetchone()["c"]
        users     = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]

    text = (
        f"⚙️ <b>Admin Panel</b>\n\n"
        f"📚 Exams: <b>{exams}</b>  ❓ Qs: <b>{questions}</b>  👥 Users: <b>{users}</b>\n\n"
        f"What would you like to do?"
    )
    _edit_or_send(chat_id, msg_id, text, admin_main_kb())

# ─────────────────────────────────────────────
#  COMMAND HANDLERS
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  ENDLESS /quiz COMMAND
# ─────────────────────────────────────────────
@bot.message_handler(commands=['quiz'])
def cmd_quiz(msg):
    register_user(msg.from_user)
    uid  = msg.from_user.id
    chat = msg.chat.id
    # Check if already in a session
    with db() as conn:
        sess = conn.execute(
            "SELECT id,topic FROM endless_sessions WHERE user_id=? AND chat_id=? AND active=1",
            (uid, chat)
        ).fetchone()
    if sess:
        label = TOPIC_LABELS.get(sess["topic"], sess["topic"])
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("⏹ Stop Current", callback_data="endless_stop"),
            InlineKeyboardButton("🔀 Change Topic", callback_data="endless_topic_menu"),
        )
        bot.send_message(chat,
            f"⚡ <b>Quiz already running!</b>\n📌 Topic: <b>{label}</b>\n\n"
            f"Send /stop to end it or change topic below:", reply_markup=kb)
        return
    show_quiz_topic_menu(chat)

@bot.message_handler(commands=['stop'])
def cmd_stop(msg):
    stop_endless_quiz(msg.chat.id, msg.from_user.id)

@bot.message_handler(commands=['score'])
def cmd_score(msg):
    uid  = msg.from_user.id
    chat = msg.chat.id
    if msg.chat.type in ('group', 'supergroup'):
        show_group_scoreboard(chat)
        return
    with db() as conn:
        sess = conn.execute(
            "SELECT * FROM endless_sessions WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT 1",
            (uid, chat)
        ).fetchone()
    if not sess:
        bot.send_message(chat, "📊 No quiz history found! Start one with /quiz 🚀"); return
    status = "🟢 Active" if sess["active"] else "🔴 Ended"
    pct = (sess["correct"] / sess["total"] * 100) if sess["total"] else 0
    bar = "🟩" * int(pct // 10) + "⬜" * (10 - int(pct // 10))
    label = TOPIC_LABELS.get(sess["topic"], sess["topic"])
    bot.send_message(chat,
        f"📊 <b>Your Quiz Stats</b>\n\n"
        f"📌 Topic: <b>{label}</b>  {status}\n"
        f"{bar} {pct:.0f}%\n\n"
        f"✅ Correct: <b>{sess['correct']}</b>\n"
        f"❌ Wrong: <b>{sess['wrong']}</b>\n"
        f"📝 Total: <b>{sess['total']}</b>\n"
        f"⭐ Score: <b>{sess['score']}</b> pts")

# ─────────────────────────────────────────────
#  GROUP /set COMMAND
# ─────────────────────────────────────────────
@bot.message_handler(commands=['set'])
def cmd_set(msg):
    chat = msg.chat.id
    uid  = msg.from_user.id
    if msg.chat.type not in ('group', 'supergroup'):
        bot.send_message(chat, "⚠️ /set only works in groups! Add me to a group first 🤖"); return
    register_user(msg.from_user)

    with db() as conn:
        exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()

    kb = InlineKeyboardMarkup(row_width=1)
    for e in exams:
        kb.add(InlineKeyboardButton(
            f"{e['icon']} {e['name']}",
            callback_data=f"gset_exam_{e['id']}"
        ))
    kb.add(InlineKeyboardButton("🌍 General Awareness Only", callback_data="gset_exam_0"))
    bot.send_message(chat,
        "⚙️ <b>Group Quiz Setup</b>\n\n"
        "🎯 Which question bank should I use for this group?\n\n"
        "<i>Select an exam or General Awareness to start an infinite quiz!</i>",
        reply_markup=kb)

@bot.message_handler(commands=['startquiz'])
def cmd_startquiz(msg):
    chat = msg.chat.id
    if msg.chat.type not in ('group', 'supergroup'):
        bot.send_message(chat, "⚠️ Only works in groups!"); return
    with db() as conn:
        cfg = conn.execute(
            "SELECT * FROM group_quiz_config WHERE chat_id=?", (chat,)
        ).fetchone()
    if not cfg:
        bot.send_message(chat, "⚙️ Set up first with /set command!"); return
    with db() as conn:
        conn.execute(
            "UPDATE group_quiz_config SET active=1 WHERE chat_id=?", (chat,)
        )
    bot.send_message(chat,
        "🚀 <b>Group Quiz Started!</b>\n\n"
        "✅ +1 for correct  ❌ 0 for wrong\n"
        "📊 /score — see scoreboard\n"
        "⏹ /stopquiz — stop the quiz\n\n"
        "<i>Questions every 35 seconds! Good luck! 🍀</i>")
    send_group_question(chat)

@bot.message_handler(commands=['stopquiz'])
def cmd_stopquiz(msg):
    chat = msg.chat.id
    if msg.chat.type not in ('group', 'supergroup'):
        bot.send_message(chat, "⚠️ Only works in groups!"); return
    with db() as conn:
        conn.execute(
            "UPDATE group_quiz_config SET active=0 WHERE chat_id=?", (chat,)
        )
    show_group_scoreboard(chat, announce=True)
    bot.send_message(chat,
        "⏹ <b>Group Quiz Stopped!</b>\n\nFinal scoreboard above 👆\n"
        "Restart anytime with /startquiz 🚀")

@bot.message_handler(commands=['resetscores'])
def cmd_resetscores(msg):
    chat = msg.chat.id
    uid  = msg.from_user.id
    if msg.chat.type not in ('group', 'supergroup'): return
    # Only group admins or bot admin
    try:
        member = bot.get_chat_member(chat, uid)
        if member.status not in ('administrator', 'creator') and not is_admin(uid):
            bot.send_message(chat, "❌ Only group admins can reset scores!"); return
    except: return
    with db() as conn:
        conn.execute("DELETE FROM group_scores WHERE chat_id=?", (chat,))
        conn.execute("UPDATE group_quiz_config SET question_count=0 WHERE chat_id=?", (chat,))
    bot.send_message(chat, "✅ <b>All scores reset!</b> Fresh start 🚀")

def show_quiz_topic_menu(chat_id, edit_mid=None):
    """Show topic picker for endless quiz."""
    kb = InlineKeyboardMarkup(row_width=2)
    topics_row = list(TOPIC_LABELS.items())
    for i in range(0, len(topics_row), 2):
        row_btns = []
        for key, label in topics_row[i:i+2]:
            row_btns.append(InlineKeyboardButton(label, callback_data=f"endless_start_{key}"))
        kb.row(*row_btns)
    text = (
        "🎯 <b>Endless Quiz</b>\n\n"
        "Pick a topic and I'll ask questions forever!\n"
        "✅ +1 per correct  ❌ 0 per wrong\n"
        "Send /stop anytime to end\n\n"
        "📌 <b>Choose your topic:</b>"
    )
    if edit_mid:
        try:
            bot.edit_message_text(text, chat_id, edit_mid, reply_markup=kb)
            return
        except: pass
    bot.send_message(chat_id, text, reply_markup=kb)

@bot.message_handler(commands=['start'])
def cmd_start(msg):
    register_user(msg.from_user)
    name = msg.from_user.first_name or "Student"
    doubt_link = get_setting("doubt_group_link", "")
    doubt_line = f"\n🤝 <b>Doubt Group</b> — AI answers your doubts!" if doubt_link else ""
    text = (
        f"🎓 <b>Welcome to TestBook Pro, {name}!</b>\n\n"
        f"Your ultimate Telegram exam prep companion! 🚀\n\n"
        f"📚 <b>Exams</b> — Timed mock tests with quiz polls\n"
        f"🎯 <b>Quick Quiz</b> — Endless topic-wise questions\n"
        f"📖 <b>Practice</b> — Previous year paper sets\n"
        f"📂 <b>Resources</b> — Download study material\n"
        f"📊 <b>Progress</b> — Track your scores\n"
        f"🏆 <b>Leaderboard</b> — Top performers{doubt_line}\n\n"
        f"👇 <b>Choose from the menu below!</b>"
    )
    bot.send_message(msg.chat.id, text, reply_markup=main_menu_kb(msg.from_user.id))

@bot.message_handler(commands=['admin'])
def cmd_admin(msg):
    if not is_admin(msg.from_user.id):
        bot.send_message(msg.chat.id, "❌ Access denied!"); return
    show_admin_panel(msg.chat.id)

@bot.message_handler(commands=['help'])
def cmd_help(msg):
    text = (
        "ℹ️ <b>TestBook Pro v5 — Help</b>\n\n"
        "📚 <b>Exams</b> — Browse & take timed mock tests\n"
        "🎯 <b>Quick Quiz</b> — Endless topic-wise quiz\n"
        "📖 <b>Practice</b> — Practice question sets\n"
        "📂 <b>Resources</b> — Download study material\n"
        "🤝 <b>Doubt Group</b> — AI-powered doubt solving\n"
        "📊 <b>My Progress</b> — View test history\n"
        "🏆 <b>Leaderboard</b> — Top performers\n\n"
        "📲 <b>Commands:</b>\n"
        "/quiz — Start endless topic quiz (private)\n"
        "/stop — Stop your endless quiz\n"
        "/score — Your quiz score\n"
        "/set — Setup quiz in a group\n"
        "/startquiz — Start group quiz\n"
        "/stopquiz — Stop group quiz\n"
        "/score — Group scoreboard (in group)\n"
        "/resetscores — Reset group scores (admin only)\n\n"
        "⭐ Questions never repeat in same session!\n"
        "⭐ Polls auto-advance when timer expires\n"
        "🌍 Uses live API + local question bank"
    )
    bot.send_message(msg.chat.id, text, reply_markup=main_menu_kb(msg.from_user.id))

@bot.message_handler(func=lambda m: m.text == "📚 Exams")
def btn_exams(msg):
    register_user(msg.from_user)
    show_exams(msg.chat.id)

@bot.message_handler(func=lambda m: m.text == "⚙️ Admin Panel")
def btn_admin(msg):
    if not is_admin(msg.from_user.id):
        bot.send_message(msg.chat.id, "❌ Access denied!"); return
    show_admin_panel(msg.chat.id)

@bot.message_handler(func=lambda m: m.text == "📊 My Progress")
def btn_progress(msg):
    uid = msg.from_user.id
    with db() as conn:
        sessions = conn.execute("""
            SELECT qs.*, e.name as exam_name, e.icon as exam_icon
            FROM quiz_sessions qs
            LEFT JOIN exams e ON qs.exam_id = e.id
            WHERE qs.user_id=? AND qs.status='completed'
            ORDER BY qs.id DESC LIMIT 10
        """, (uid,)).fetchall()

    if not sessions:
        bot.send_message(msg.chat.id, "📊 <b>No tests taken yet!</b>\nStart a test to see your progress! 🚀")
        return

    text = "📊 <b>Your Recent Tests</b>\n\n"
    for s in sessions:
        q_ids = json.loads(s["question_ids"])
        total = len(q_ids)
        pct = (s["score"] / (total * s["positive_marks"]) * 100) if total else 0
        bar = "🟩" * int(pct // 20) + "⬜" * (5 - int(pct // 20))
        text += (
            f"{s['exam_icon'] or '📘'} <b>{s['exam_name'] or 'Test'}</b>\n"
            f"   {bar} {pct:.0f}% — Score: {s['score']:.1f}\n\n"
        )
    bot.send_message(msg.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "🏆 Leaderboard")
def btn_leaderboard(msg):
    with db() as conn:
        rows = conn.execute("""
            SELECT u.full_name, u.username, SUM(qs.score) as total_score, COUNT(qs.id) as tests
            FROM quiz_sessions qs
            JOIN users u ON qs.user_id = u.id
            WHERE qs.status='completed'
            GROUP BY qs.user_id
            ORDER BY total_score DESC
            LIMIT 10
        """).fetchall()

    if not rows:
        bot.send_message(msg.chat.id, "🏆 <b>Leaderboard is empty!</b>\nBe the first to take a test! 🚀")
        return

    medals = ["🥇","🥈","🥉"] + ["🏅"] * 7
    text = "🏆 <b>Top Performers</b>\n\n"
    for i, r in enumerate(rows):
        name = r["full_name"] or r["username"] or "Student"
        text += f"{medals[i]} <b>{name}</b> — {r['total_score']:.0f} pts ({r['tests']} tests)\n"
    bot.send_message(msg.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "ℹ️ Help")
def btn_help(msg):
    cmd_help(msg)

@bot.message_handler(func=lambda m: m.text == "🎯 Quick Quiz")
def btn_quick_quiz(msg):
    register_user(msg.from_user)
    uid  = msg.from_user.id
    chat = msg.chat.id
    with db() as conn:
        sess = conn.execute(
            "SELECT id,topic FROM endless_sessions WHERE user_id=? AND chat_id=? AND active=1",
            (uid, chat)
        ).fetchone()
    if sess:
        label = TOPIC_LABELS.get(sess["topic"], sess["topic"])
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("⏹ Stop Quiz", callback_data="endless_stop"),
            InlineKeyboardButton("🔀 Change Topic", callback_data="endless_topic_menu"),
        )
        bot.send_message(chat,
            f"⚡ <b>Quiz Running!</b> 📌 Topic: <b>{label}</b>\n/stop to end it:", reply_markup=kb)
    else:
        show_quiz_topic_menu(chat)

@bot.message_handler(func=lambda m: m.text == "🤝 Doubt Group")
def btn_doubt_group(msg):
    register_user(msg.from_user)
    chat = msg.chat.id
    link = get_setting("doubt_group_link", "")
    name = get_setting("doubt_group_name", "Doubt Group")
    if not link:
        bot.send_message(chat,
            "🤝 <b>Doubt Group</b>\n\n"
            "⚙️ Not set up yet! Ask admin to configure it.")
        return
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(f"🤝 Join {name}", url=link))
    bot.send_message(chat,
        f"🤝 <b>Doubt Group — {name}</b>\n\n"
        f"🤖 <b>AI-Powered Doubt Solving!</b>\n\n"
        f"✨ Our group has an advanced AI that automatically analyzes your doubts "
        f"and provides instant, detailed answers!\n\n"
        f"📌 Just post your question in the group and AI will reply instantly! 🚀",
        reply_markup=kb)

# ─────────────────────────────────────────────
#  ADMIN TEXT / FILE INPUT HANDLER
# ─────────────────────────────────────────────
@bot.message_handler(content_types=['text', 'document', 'photo'])
def handle_text(msg):
    uid = msg.from_user.id
    if not is_admin(uid): return

    state, data = get_admin_state(uid)
    if not state: return

    chat = msg.chat.id

    # ── ADD QUESTION MANUALLY ──
    if state == "mq_select_exam":
        # handled via callback, not text
        pass

    # ── BULK QUESTION PASTE ──
    elif state == "mq_bulk_paste":
        text_in = msg.text or ""
        if not text_in.strip():
            bot.send_message(chat, "⚠️ Empty message. Please paste your questions:"); return

        parsed, errs = parse_bulk_questions(text_in)

        if not parsed:
            err_msg = "❌ <b>No valid questions found.</b>\n\nMake sure your format is:\n" \
                      "<code>1. Question\nA.) ...\nB.) ...\nC.) ...\nD.) ...</code>"
            if errs:
                err_msg += "\n\n" + "\n".join(errs[:5])
            bot.send_message(chat, err_msg); return

        # Save all parsed questions to DB
        exam_id    = data["exam_id"]
        section_id = data.get("section_id")
        saved      = 0
        with db() as conn:
            for q in parsed:
                conn.execute("""
                    INSERT INTO questions
                    (exam_id,section_id,question,option_a,option_b,option_c,option_d,correct,explanation,source)
                    VALUES(?,?,?,?,?,?,?,?,?,'manual')
                """, (exam_id, section_id,
                      q['question'], q['option_a'], q['option_b'],
                      q['option_c'], q['option_d'],
                      q['correct'], q['explanation']))
                saved += 1
            total_qs = conn.execute(
                "SELECT COUNT(*) as c FROM questions WHERE exam_id=?", (exam_id,)
            ).fetchone()["c"]

        # Build result message
        no_ans = [q['question'][:35] + '…' for q in parsed if q['correct'] == 'A'
                  and not re.search(r'correct|answer|ans|key', text_in[
                      text_in.lower().find(q['question'][:20].lower()):
                      text_in.lower().find(q['question'][:20].lower()) + 200
                  ], re.IGNORECASE)]

        result = (
            f"✅ <b>{saved} question(s) saved successfully!</b>\n"
            f"📊 Total in exam now: <b>{total_qs}</b>\n"
        )
        if errs:
            result += f"\n⚠️ <b>{len(errs)} block(s) skipped:</b>\n" + "\n".join(errs[:5])

        kb2 = InlineKeyboardMarkup(row_width=1)
        kb2.add(
            InlineKeyboardButton("📋 Paste More Questions",
                                 callback_data=f"mq_bulk_{exam_id}_{section_id or 0}"),
            InlineKeyboardButton("✏️ Add Single Question",
                                 callback_data=f"mq_single_{exam_id}_{section_id or 0}"),
            InlineKeyboardButton("✅ Done", callback_data="admin_panel"),
        )
        clear_admin_state(uid)
        bot.send_message(chat, result, reply_markup=kb2)

    # ── ADD QUESTION MANUALLY (single step-by-step) ──
    elif state == "mq_question":
        data["mq_text"] = msg.text.strip()
        set_admin_state(uid, "mq_opta", data)
        bot.send_message(chat, "🅐 Enter <b>Option A</b>:")

    elif state == "mq_opta":
        data["mq_a"] = msg.text.strip()
        set_admin_state(uid, "mq_optb", data)
        bot.send_message(chat, "🅑 Enter <b>Option B</b>:")

    elif state == "mq_optb":
        data["mq_b"] = msg.text.strip()
        set_admin_state(uid, "mq_optc", data)
        bot.send_message(chat, "🅒 Enter <b>Option C</b>:")

    elif state == "mq_optc":
        data["mq_c"] = msg.text.strip()
        set_admin_state(uid, "mq_optd", data)
        bot.send_message(chat, "🅓 Enter <b>Option D</b>:")

    elif state == "mq_optd":
        data["mq_d"] = msg.text.strip()
        set_admin_state(uid, "mq_answer", data)
        bot.send_message(chat, "✅ Correct answer? Send <b>A</b>, <b>B</b>, <b>C</b> or <b>D</b>:")

    elif state == "mq_answer":
        ans = msg.text.strip().upper()
        if ans not in ('A','B','C','D'):
            bot.send_message(chat, "⚠️ Send exactly A, B, C or D:"); return
        data["mq_ans"] = ans
        set_admin_state(uid, "mq_explanation", data)
        bot.send_message(chat, "💡 Enter explanation (or type <code>skip</code>):")

    elif state == "mq_explanation":
        exp = "" if msg.text.strip().lower() == "skip" else msg.text.strip()
        data["mq_exp"] = exp
        set_admin_state(uid, "mq_more", data)
        # Save this question
        with db() as conn:
            conn.execute("""
                INSERT INTO questions
                (exam_id,section_id,question,option_a,option_b,option_c,option_d,correct,explanation,source)
                VALUES(?,?,?,?,?,?,?,?,?,'manual')
            """, (data["exam_id"], data.get("section_id"),
                  data["mq_text"], data["mq_a"], data["mq_b"], data["mq_c"], data["mq_d"],
                  data["mq_ans"], data["mq_exp"]))
            total_qs = conn.execute("SELECT COUNT(*) as c FROM questions WHERE exam_id=?",
                                    (data["exam_id"],)).fetchone()["c"]
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("➕ Add Another Question", callback_data=f"mq_another_{data['exam_id']}_{data.get('section_id',0)}"),
            InlineKeyboardButton("✅ Done", callback_data="admin_panel")
        )
        bot.send_message(chat,
            f"✅ <b>Question saved!</b>\n"
            f"📊 Total questions in exam: <b>{total_qs}</b>\n\n"
            f"Add another or press Done:", reply_markup=kb)

    # ── ADD EXAM ──
    if state == "add_exam_name":
        data["name"] = msg.text
        set_admin_state(uid, "add_exam_icon", data)
        bot.send_message(chat, "📌 Enter exam icon emoji (e.g. 📘) or type <code>skip</code>:")

    elif state == "add_exam_icon":
        data["icon"] = msg.text if msg.text.lower() != "skip" else "📘"
        set_admin_state(uid, "add_exam_desc", data)
        bot.send_message(chat, "📝 Enter exam description or type <code>skip</code>:")

    elif state == "add_exam_desc":
        data["description"] = msg.text if msg.text.lower() != "skip" else ""
        with db() as conn:
            conn.execute("INSERT INTO exams(name,icon,description) VALUES(?,?,?)",
                         (data["name"], data["icon"], data["description"]))
        clear_admin_state(uid)
        bot.send_message(chat, f"✅ Exam <b>{data['name']}</b> created!", reply_markup=admin_main_kb())

    # ── ADD SECTION ──
    elif state == "add_section_name":
        data["sec_name"] = msg.text
        set_admin_state(uid, "add_section_icon", data)
        bot.send_message(chat, "📌 Section icon emoji or type <code>skip</code>:")

    elif state == "add_section_icon":
        data["sec_icon"] = msg.text if msg.text.lower() != "skip" else "📂"
        with db() as conn:
            conn.execute("INSERT INTO sections(exam_id,name,icon) VALUES(?,?,?)",
                         (data["exam_id"], data["sec_name"], data["sec_icon"]))
        clear_admin_state(uid)
        bot.send_message(chat, f"✅ Section <b>{data['sec_name']}</b> added!", reply_markup=admin_main_kb())

    # ── QUIZ SETTINGS ──
    elif state == "qs_positive":
        try:
            data["positive"] = float(msg.text)
            set_admin_state(uid, "qs_negative", data)
            bot.send_message(chat, "❌ Enter negative marks per wrong answer (e.g. 0.25):")
        except:
            bot.send_message(chat, "⚠️ Enter a valid number:")

    elif state == "qs_negative":
        try:
            data["negative"] = float(msg.text)
            set_admin_state(uid, "qs_time", data)
            bot.send_message(chat, "⏱ Time per question in seconds (e.g. 60):")
        except:
            bot.send_message(chat, "⚠️ Enter a valid number:")

    elif state == "qs_time":
        try:
            data["time"] = int(msg.text)
            set_admin_state(uid, "qs_total", data)
            bot.send_message(chat, "🔢 Total questions per test (e.g. 20):")
        except:
            bot.send_message(chat, "⚠️ Enter a valid integer:")

    elif state == "qs_total":
        try:
            data["total"] = int(msg.text)
            with db() as conn:
                conn.execute("DELETE FROM quiz_settings WHERE exam_id=? AND section_id IS NULL",
                             (data["exam_id"],))
                conn.execute("""
                    INSERT INTO quiz_settings(exam_id,positive_marks,negative_marks,time_per_question,total_questions)
                    VALUES(?,?,?,?,?)
                """, (data["exam_id"], data["positive"], data["negative"], data["time"], data["total"]))
            clear_admin_state(uid)
            bot.send_message(chat,
                f"✅ <b>Settings Saved!</b>\n"
                f"✅ +{data['positive']}  ❌ -{data['negative']}  ⏱ {data['time']}s  🔢 {data['total']}q",
                reply_markup=admin_main_kb())
        except:
            bot.send_message(chat, "⚠️ Enter a valid integer:")

    # ── SUPPORT GROUP ──
    elif state == "set_group":
        with db() as conn:
            conn.execute("UPDATE exams SET support_group=? WHERE id=?",
                         (msg.text.strip(), data["exam_id"]))
        clear_admin_state(uid)
        bot.send_message(chat, "✅ Support group link saved!", reply_markup=admin_main_kb())

    # ── DOUBT GROUP ──
    elif state == "set_doubt_group_name":
        data["dg_name"] = msg.text.strip()
        set_admin_state(uid, "set_doubt_group_link", data)
        bot.send_message(chat, f"🔗 Now send the <b>Telegram group/channel link</b>:\n(e.g. https://t.me/...)")

    elif state == "set_doubt_group_link":
        link = msg.text.strip()
        name = data.get("dg_name", "Doubt Group")
        set_setting("doubt_group_link", link)
        set_setting("doubt_group_name", name)
        clear_admin_state(uid)
        bot.send_message(chat,
            f"✅ <b>Doubt Group Configured!</b>\n\n"
            f"🤝 Name: <b>{name}</b>\n"
            f"🔗 Link: {link}\n\n"
            f"Users can now access it from main menu! 🚀",
            reply_markup=admin_main_kb())

    # ── GENERAL QUESTIONS BULK PASTE ──
    elif state == "gq_bulk_paste":
        text_in  = msg.text or ""
        topic    = data.get("topic", "general")
        parsed, errs = parse_bulk_questions(text_in)
        if not parsed:
            bot.send_message(chat, "❌ No valid questions found. Check format and try again."); return
        with db() as conn:
            for q in parsed:
                conn.execute("""
                    INSERT INTO general_questions
                    (topic,question,option_a,option_b,option_c,option_d,correct,explanation,source)
                    VALUES(?,?,?,?,?,?,?,?,'admin')
                """, (topic, q["question"], q["option_a"], q["option_b"],
                      q["option_c"], q["option_d"], q["correct"], q["explanation"]))
            total = conn.execute(
                "SELECT COUNT(*) as c FROM general_questions WHERE topic=?", (topic,)
            ).fetchone()["c"]
        label = TOPIC_LABELS.get(topic, topic)
        err_txt = f"\n⚠️ {len(errs)} skipped" if errs else ""
        kb2 = InlineKeyboardMarkup(row_width=1)
        kb2.add(
            InlineKeyboardButton("📋 Add More", callback_data=f"admin_gq_topic_{topic}"),
            InlineKeyboardButton("◀️ Back to Bank", callback_data="admin_general_qs"),
        )
        clear_admin_state(uid)
        bot.send_message(chat,
            f"✅ <b>{len(parsed)} questions saved!</b>\n"
            f"📌 Topic: {label}  |  Total: <b>{total}</b>{err_txt}",
            reply_markup=kb2)

    # ── RESOURCE ──
    elif state == "add_resource_title":
        data["res_title"] = msg.text
        set_admin_state(uid, "add_resource_file", data)
        bot.send_message(chat, "📤 Send the file (PDF/photo) or a URL:")

    elif state == "add_resource_file":
        file_id = file_type = url = None
        if msg.document:
            file_id, file_type = msg.document.file_id, "pdf"
        elif msg.photo:
            file_id, file_type = msg.photo[-1].file_id, "photo"
        elif msg.text and msg.text.startswith("http"):
            url, file_type = msg.text, "url"
        else:
            bot.send_message(chat, "⚠️ Send a file or URL:"); return

        with db() as conn:
            conn.execute("""
                INSERT INTO resources(exam_id,section_id,title,file_id,file_type,url)
                VALUES(?,?,?,?,?,?)
            """, (data["exam_id"], data.get("section_id"), data["res_title"], file_id, file_type, url))
        clear_admin_state(uid)
        bot.send_message(chat, "✅ Resource added!", reply_markup=admin_main_kb())

    # ── PDF UPLOAD ──
    elif state == "upload_quiz_pdf":
        if not msg.document:
            bot.send_message(chat, "⚠️ Please send a PDF file:"); return
        process_pdf_upload(msg, data, "quiz")

    elif state == "upload_practice_pdf_name":
        data["practice_name"] = msg.text
        set_admin_state(uid, "upload_practice_pdf", data)
        bot.send_message(chat, "📤 Now send the Practice Set PDF file:")

    elif state == "upload_practice_pdf":
        if not msg.document:
            bot.send_message(chat, "⚠️ Please send a PDF file:"); return
        process_pdf_upload(msg, data, "practice")

def process_pdf_upload(msg, data, upload_type):
    uid  = msg.from_user.id
    chat = msg.chat.id

    prog_msg = bot.send_message(
        chat,
        "⏳ <b>Analyzing PDF...</b>\n\n"
        "🔍 Extracting questions using multi-strategy parser...\n"
        "⚙️ This may take a moment for large PDFs."
    )

    try:
        file_info  = bot.get_file(msg.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)

        try:
            bot.edit_message_text(
                "⏳ <b>Processing PDF...</b>\n🔄 Text extracted, parsing MCQs...",
                chat, prog_msg.message_id
            )
        except: pass

        questions, error = extract_mcqs_from_pdf(file_bytes)

        try:
            bot.delete_message(chat, prog_msg.message_id)
        except: pass

        if error:
            bot.send_message(chat, f"❌ {error}", reply_markup=admin_main_kb())
            clear_admin_state(uid); return

        if not questions:
            bot.send_message(chat,
                "❌ <b>No MCQs found!</b>\n\n"
                "Supported formats:\n"
                "• <code>1. Question text\n(A) Option A\n(B) Option B\n(C) Option C\n(D) Option D\nAns: A</code>\n\n"
                "• <code>Q1. Question\nA. Option\nB. Option\nC. Option\nD. Option\nAnswer: B</code>\n\n"
                "Make sure PDF has selectable text (not scanned image).",
                reply_markup=admin_main_kb()
            )
            clear_admin_state(uid); return

        with db() as conn:
            if upload_type == "quiz":
                exam_id    = data["exam_id"]
                section_id = data.get("section_id")
                for q in questions:
                    conn.execute("""
                        INSERT INTO questions
                        (exam_id,section_id,question,option_a,option_b,option_c,option_d,correct,explanation,source)
                        VALUES(?,?,?,?,?,?,?,?,?,'pdf')
                    """, (exam_id, section_id, q["question"], q["option_a"], q["option_b"],
                          q["option_c"], q["option_d"], q["correct"], q["explanation"]))

                bot.send_message(chat,
                    f"✅ <b>PDF Processed Successfully!</b>\n\n"
                    f"📊 Extracted: <b>{len(questions)}</b> questions\n"
                    f"💾 Saved to: Quiz Bank ✅\n\n"
                    f"Questions are ready for tests! 🎉",
                    reply_markup=admin_main_kb())

            elif upload_type == "practice":
                exam_id    = data["exam_id"]
                section_id = data.get("section_id")
                name       = data.get("practice_name", "Practice Set")
                conn.execute("INSERT INTO practice_sets(exam_id,section_id,name) VALUES(?,?,?)",
                             (exam_id, section_id, name))
                pset_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                for q in questions:
                    conn.execute("""
                        INSERT INTO practice_questions
                        (practice_id,question,option_a,option_b,option_c,option_d,correct,explanation)
                        VALUES(?,?,?,?,?,?,?,?)
                    """, (pset_id, q["question"], q["option_a"], q["option_b"],
                          q["option_c"], q["option_d"], q["correct"], q["explanation"]))

                bot.send_message(chat,
                    f"✅ <b>Practice Set Created!</b>\n\n"
                    f"📝 Name: <b>{name}</b>\n"
                    f"📊 Questions: <b>{len(questions)}</b>\n\n"
                    f"Users can now practice! 🎉",
                    reply_markup=admin_main_kb())

        clear_admin_state(uid)

    except Exception as e:
        try: bot.delete_message(chat, prog_msg.message_id)
        except: pass
        bot.send_message(chat, f"❌ Error: {str(e)}", reply_markup=admin_main_kb())
        clear_admin_state(uid)

# ─────────────────────────────────────────────
#  CALLBACK HANDLER
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: True)
def handle_callback(c):
    uid  = c.from_user.id
    chat = c.message.chat.id
    mid  = c.message.message_id
    d    = c.data
    bot.answer_callback_query(c.id)

    # Navigation
    if d in ("back_exams", "home"):
        show_exams(chat, mid)

    elif d.startswith("exam_open_"):
        show_exam_detail(chat, int(d.split("_")[-1]), mid)

    elif d.startswith("exam_test_"):
        show_test_sections(chat, int(d.split("_")[-1]), mid)

    elif d.startswith("exam_practice_"):
        show_practice_sets(chat, int(d.split("_")[-1]), mid)

    elif d.startswith("exam_resources_"):
        show_resources(chat, int(d.split("_")[-1]), mid)

    elif d.startswith("exam_group_"):
        exam_id = int(d.split("_")[-1])
        with db() as conn:
            e = conn.execute("SELECT support_group,name FROM exams WHERE id=?", (exam_id,)).fetchone()
        if e and e["support_group"]:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton(f"💬 Join {e['name']} Group", url=e["support_group"]))
            kb.add(InlineKeyboardButton("◀️ Back", callback_data=f"exam_open_{exam_id}"))
            _edit_or_send(chat, mid,
                "💬 <b>Join the Help Group</b>\n\nGet help, discuss doubts, share notes!", kb)
        else:
            bot.send_message(chat, "❌ No support group linked yet.")

    elif d.startswith("start_quiz_"):
        parts    = d.split("_")
        exam_id  = int(parts[2])
        sec_id   = int(parts[3]) if parts[3] != "0" else None
        start_quiz(chat, uid, exam_id, sec_id)

    elif d.startswith("ans_"):
        parts = d.split("_")
        sess_id = int(parts[1])
        idx     = int(parts[2])
        answer  = parts[3]
        try: bot.edit_message_reply_markup(chat, mid, reply_markup=None)
        except: pass
        handle_quiz_answer(chat, uid, sess_id, idx, answer)

    elif d.startswith("practice_start_"):
        parts = d.split("_")
        start_practice(chat, uid, int(parts[2]), int(parts[3]), 0)

    elif d.startswith("practice_next_"):
        parts = d.split("_")
        start_practice(chat, uid, int(parts[2]), int(parts[3]), int(parts[4]))

    elif d.startswith("pans_"):
        parts = d.split("_")
        try: bot.edit_message_reply_markup(chat, mid, reply_markup=None)
        except: pass
        handle_practice_answer(chat, int(parts[1]), int(parts[2]), parts[3], int(parts[4]))

    elif d.startswith("res_get_"):
        res_id = int(d.split("_")[-1])
        with db() as conn:
            r = conn.execute("SELECT * FROM resources WHERE id=?", (res_id,)).fetchone()
        if not r:
            bot.send_message(chat, "❌ Resource not found!"); return
        if r["file_type"] == "pdf" and r["file_id"]:
            bot.send_document(chat, r["file_id"], caption=f"📄 {r['title']}")
        elif r["file_type"] == "photo" and r["file_id"]:
            bot.send_photo(chat, r["file_id"], caption=f"🖼 {r['title']}")
        elif r["url"]:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔗 Open Link", url=r["url"]))
            bot.send_message(chat, f"🔗 <b>{r['title']}</b>", reply_markup=kb)


    # ── ENDLESS QUIZ CALLBACKS ──
    elif d == "endless_topic_menu":
        show_quiz_topic_menu(chat, mid)

    elif d.startswith("endless_start_"):
        topic = d.split("_", 2)[2]
        bot.answer_callback_query(c.id, f"🚀 Starting {TOPIC_LABELS.get(topic, topic)[:20]}...")
        start_endless_quiz(chat, uid, topic)

    elif d == "endless_stop":
        bot.answer_callback_query(c.id, "⏹ Stopping quiz...")
        stop_endless_quiz(chat, uid)

    # ── GROUP QUIZ SETUP CALLBACKS ──
    elif d.startswith("gset_exam_"):
        exam_id_str = d.split("_")[-1]
        exam_id = int(exam_id_str) if exam_id_str != "0" else None
        if exam_id:
            with db() as conn:
                sections = conn.execute(
                    "SELECT * FROM sections WHERE exam_id=?", (exam_id,)
                ).fetchall()
            kb2 = InlineKeyboardMarkup(row_width=1)
            kb2.add(InlineKeyboardButton("📚 All Sections (Full Exam)", callback_data=f"gset_sec_{exam_id}_0"))
            for s in sections:
                kb2.add(InlineKeyboardButton(f"{s['icon']} {s['name']}", callback_data=f"gset_sec_{exam_id}_{s['id']}"))
            _edit_or_send(chat, mid, "📂 <b>Select section:</b>", kb2)
        else:
            # General awareness — pick topic
            kb2 = InlineKeyboardMarkup(row_width=2)
            for key, label in list(TOPIC_LABELS.items())[:8]:
                kb2.add(InlineKeyboardButton(label, callback_data=f"gset_topic_{key}"))
            _edit_or_send(chat, mid, "🌍 <b>Select General Awareness topic:</b>", kb2)

    elif d.startswith("gset_sec_"):
        parts   = d.split("_")
        exam_id = int(parts[2])
        sec_id  = int(parts[3]) if parts[3] != "0" else None
        with db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO group_quiz_config
                (chat_id, exam_id, section_id, topic, active, question_count, set_by)
                VALUES(?,?,?,'exam',0,0,?)
            """, (chat, exam_id, sec_id, uid))
        with db() as conn:
            e = conn.execute("SELECT name,icon FROM exams WHERE id=?", (exam_id,)).fetchone()
        exam_name = f"{e['icon']} {e['name']}" if e else "Exam"
        kb2 = InlineKeyboardMarkup(row_width=2)
        kb2.add(
            InlineKeyboardButton("🚀 Start Quiz Now!", callback_data="gset_activate"),
            InlineKeyboardButton("⏳ Start Later (/startquiz)", callback_data="gset_done"),
        )
        _edit_or_send(chat, mid,
            f"✅ <b>Group Quiz Configured!</b>\n\n"
            f"📚 Exam: <b>{exam_name}</b>\n"
            f"✅ +1 per correct  ❌ 0 per wrong\n"
            f"📊 /score — scoreboard  ⏹ /stopquiz — stop\n\n"
            f"Ready to start?", kb2)

    elif d.startswith("gset_topic_"):
        topic = d.split("_", 2)[2]
        with db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO group_quiz_config
                (chat_id, exam_id, section_id, topic, active, question_count, set_by)
                VALUES(?,NULL,NULL,?,0,0,?)
            """, (chat, topic, uid))
        label = TOPIC_LABELS.get(topic, topic)
        kb2 = InlineKeyboardMarkup(row_width=2)
        kb2.add(
            InlineKeyboardButton("🚀 Start Quiz Now!", callback_data="gset_activate"),
            InlineKeyboardButton("⏳ Start Later (/startquiz)", callback_data="gset_done"),
        )
        _edit_or_send(chat, mid,
            f"✅ <b>Group Quiz Configured!</b>\n\n"
            f"🌍 Topic: <b>{label}</b>\n"
            f"✅ +1 per correct  ❌ 0 per wrong\n\n"
            f"Ready to start?", kb2)

    elif d == "gset_activate":
        with db() as conn:
            conn.execute("UPDATE group_quiz_config SET active=1 WHERE chat_id=?", (chat,))
        bot.answer_callback_query(c.id, "🚀 Quiz Starting!")
        bot.send_message(chat,
            "🚀 <b>Group Quiz Started!</b>\n\n"
            "📊 /score — scoreboard\n"
            "⏹ /stopquiz — stop\n"
            "🔁 /resetscores — reset scores\n\n"
            "<i>First question coming up! 🎯</i>")
        send_group_question(chat)

    elif d == "gset_done":
        bot.answer_callback_query(c.id, "✅ Configured! Use /startquiz to begin.")
        _edit_or_send(chat, mid, "✅ <b>Configured!</b> Use /startquiz when ready 🚀", None)

    elif d == "group_score_refresh":
        show_group_scoreboard(chat)

    # ── ADMIN: DOUBT GROUP ──
    elif d == "admin_set_doubt_group" and is_admin(uid):
        set_admin_state(uid, "set_doubt_group_name", {})
        bot.send_message(chat, "🤝 Enter a <b>name</b> for the Doubt Group (e.g. <i>Physics Doubt Group</i>):")

    # ── ADMIN: GENERAL QUESTIONS BANK ──
    elif d == "admin_general_qs" and is_admin(uid):
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) as c FROM general_questions").fetchone()["c"]
        topics_cnt = {}
        with db() as conn:
            rows = conn.execute(
                "SELECT topic, COUNT(*) as c FROM general_questions GROUP BY topic"
            ).fetchall()
            for r in rows:
                topics_cnt[r["topic"]] = r["c"]
        summary = "\n".join([f"  📌 {TOPIC_LABELS.get(k,k)}: <b>{v}</b>" for k, v in topics_cnt.items()]) or "  (empty)"
        kb2 = InlineKeyboardMarkup(row_width=1)
        kb2.add(
            InlineKeyboardButton("📋 Bulk Add Questions", callback_data="admin_gq_bulk"),
            InlineKeyboardButton("🗑️ Clear All", callback_data="admin_gq_clear"),
            InlineKeyboardButton("◀️ Back", callback_data="admin_panel"),
        )
        _edit_or_send(chat, mid,
            f"🌍 <b>General Questions Bank</b>\n\n"
            f"📊 Total: <b>{total}</b> questions\n\n{summary}\n\n"
            f"<i>These questions are used for /quiz and group quizzes when no exam is selected</i>",
            kb2)

    elif d == "admin_gq_bulk" and is_admin(uid):
        kb2 = InlineKeyboardMarkup(row_width=2)
        for key, label in TOPIC_LABELS.items():
            kb2.add(InlineKeyboardButton(label, callback_data=f"admin_gq_topic_{key}"))
        _edit_or_send(chat, mid, "📌 <b>Select topic for these questions:</b>", kb2)

    elif d.startswith("admin_gq_topic_") and is_admin(uid):
        topic = d.split("_", 3)[3]
        set_admin_state(uid, "gq_bulk_paste", {"topic": topic})
        label = TOPIC_LABELS.get(topic, topic)
        bot.send_message(chat,
            f"📋 <b>Bulk Add to {label}</b>\n\n"
            f"Paste questions in this format:\n\n"
            f"<code>1. Question text\n"
            f"A.) Option A\nB.) Option B\nC.) Option C\nD.) Option D\n"
            f"Correct : A\nExplanation : ...</code>\n\n"
            f"📨 Send all questions now:")

    elif d == "admin_gq_clear" and is_admin(uid):
        kb2 = InlineKeyboardMarkup(row_width=2)
        kb2.add(
            InlineKeyboardButton("✅ Yes, Clear All", callback_data="admin_gq_confirm_clear"),
            InlineKeyboardButton("❌ Cancel", callback_data="admin_general_qs"),
        )
        _edit_or_send(chat, mid, "⚠️ <b>Clear ALL general questions?</b>", kb2)

    elif d == "admin_gq_confirm_clear" and is_admin(uid):
        with db() as conn:
            conn.execute("DELETE FROM general_questions")
        _edit_or_send(chat, mid, "✅ All general questions cleared!", back_btn("admin_general_qs"))

    # ── ADMIN CALLBACKS ──
    elif d == "admin_panel" and is_admin(uid):
        show_admin_panel(chat, mid)

    elif d == "admin_add_exam" and is_admin(uid):
        set_admin_state(uid, "add_exam_name", {})
        bot.send_message(chat, "📝 Enter the <b>Exam Name</b> (e.g. Indian Navy MED):")

    elif d == "admin_manage_exams" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        if not exams:
            bot.send_message(chat, "📭 No exams yet!", reply_markup=back_btn("admin_panel")); return
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}",
                                        callback_data=f"admin_exam_detail_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "📋 <b>Manage Exams</b>\n\nSelect an exam:", kb)

    elif d.startswith("admin_exam_detail_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        with db() as conn:
            e     = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
            q_cnt = conn.execute("SELECT COUNT(*) as c FROM questions WHERE exam_id=?", (exam_id,)).fetchone()["c"]
            s_cnt = conn.execute("SELECT COUNT(*) as c FROM sections WHERE exam_id=?", (exam_id,)).fetchone()["c"]
            p_cnt = conn.execute("SELECT COUNT(*) as c FROM practice_sets WHERE exam_id=?", (exam_id,)).fetchone()["c"]
        text = (
            f"{e['icon']} <b>{e['name']}</b>\n\n"
            f"📂 Sections: {s_cnt}  ❓ Qs: {q_cnt}  📝 Sets: {p_cnt}"
        )
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("📤 Upload Quiz PDF",     callback_data=f"admin_upq_{exam_id}"),
            InlineKeyboardButton("📤 Upload Practice PDF", callback_data=f"admin_upp_{exam_id}"),
            InlineKeyboardButton("⚙️ Quiz Settings",       callback_data=f"admin_qset_{exam_id}"),
            InlineKeyboardButton("💬 Set Group",           callback_data=f"admin_sgroup_{exam_id}"),
            InlineKeyboardButton("📎 Add Resource",        callback_data=f"admin_ares_{exam_id}"),
            InlineKeyboardButton("❌ Delete Exam",         callback_data=f"admin_del_exam_{exam_id}"),
            InlineKeyboardButton("◀️ Back",               callback_data="admin_manage_exams")
        )
        _edit_or_send(chat, mid, text, kb)

    elif d.startswith("admin_del_exam_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"admin_confirm_del_{exam_id}"),
            InlineKeyboardButton("❌ Cancel",      callback_data=f"admin_exam_detail_{exam_id}")
        )
        _edit_or_send(chat, mid, "⚠️ <b>Delete this exam and ALL its data?</b>", kb)

    elif d.startswith("admin_confirm_del_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        with db() as conn:
            conn.execute("DELETE FROM questions WHERE exam_id=?", (exam_id,))
            conn.execute("DELETE FROM sections WHERE exam_id=?", (exam_id,))
            conn.execute("DELETE FROM quiz_settings WHERE exam_id=?", (exam_id,))
            conn.execute("DELETE FROM resources WHERE exam_id=?", (exam_id,))
            psets = conn.execute("SELECT id FROM practice_sets WHERE exam_id=?", (exam_id,)).fetchall()
            for ps in psets:
                conn.execute("DELETE FROM practice_questions WHERE practice_id=?", (ps["id"],))
            conn.execute("DELETE FROM practice_sets WHERE exam_id=?", (exam_id,))
            conn.execute("DELETE FROM exams WHERE id=?", (exam_id,))
        _edit_or_send(chat, mid, "✅ Exam deleted successfully.", back_btn("admin_manage_exams"))

    elif d.startswith("admin_upq_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        set_admin_state(uid, "upload_quiz_pdf", {"exam_id": exam_id})
        bot.send_message(chat,
            "📤 <b>Upload Quiz PDF</b>\n\n"
            "Send the PDF file with MCQ questions.\n\n"
            "<b>Supported formats:</b>\n"
            "<code>1. Question text\n(A) Option\n(B) Option\n(C) Option\n(D) Option\nAns: A</code>")

    elif d.startswith("admin_upp_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        set_admin_state(uid, "upload_practice_pdf_name", {"exam_id": exam_id})
        bot.send_message(chat, "📝 Enter a name for this Practice Set (e.g. <i>Biology Practice 1</i>):")

    elif d.startswith("admin_qset_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        set_admin_state(uid, "qs_positive", {"exam_id": exam_id})
        bot.send_message(chat, "✅ Enter <b>positive marks</b> per correct answer (e.g. 1 or 2):")

    elif d.startswith("admin_sgroup_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        set_admin_state(uid, "set_group", {"exam_id": exam_id})
        bot.send_message(chat, "💬 Enter Telegram group/channel link (e.g. https://t.me/...):")

    elif d.startswith("admin_ares_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        set_admin_state(uid, "add_resource_title", {"exam_id": exam_id})
        bot.send_message(chat, "📎 Enter resource title (e.g. <i>Biology Notes PDF</i>):")

    elif d == "admin_add_question" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        if not exams:
            bot.send_message(chat, "❌ Create an exam first!"); return
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}",
                                        callback_data=f"mq_exam_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "✏️ <b>Add Question Manually</b>\n\nSelect exam:", kb)

    elif d.startswith("mq_exam_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        with db() as conn:
            sections = conn.execute("SELECT * FROM sections WHERE exam_id=?", (exam_id,)).fetchall()
        if sections:
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(InlineKeyboardButton("📚 No Section (General)", callback_data=f"mq_start_{exam_id}_0"))
            for s in sections:
                kb.add(InlineKeyboardButton(f"{s['icon']} {s['name']}",
                                            callback_data=f"mq_start_{exam_id}_{s['id']}"))
            kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_add_question"))
            _edit_or_send(chat, mid, "📂 Select section (or General):", kb)
        else:
            kb2 = InlineKeyboardMarkup(row_width=1)
            kb2.add(
                InlineKeyboardButton("📋 Bulk Paste (50-60 Qs at once)",
                                     callback_data=f"mq_bulk_{exam_id}_0"),
                InlineKeyboardButton("✏️ Single Question (step-by-step)",
                                     callback_data=f"mq_single_{exam_id}_0"),
                InlineKeyboardButton("◀️ Back", callback_data="admin_add_question"),
            )
            _edit_or_send(chat, mid, "➕ <b>Add Questions</b>\n\nHow do you want to add?", kb2)

    elif d.startswith("mq_start_") and is_admin(uid):
        parts   = d.split("_")
        exam_id = int(parts[2])
        sec_id  = int(parts[3]) if parts[3] != "0" else None
        kb2 = InlineKeyboardMarkup(row_width=1)
        kb2.add(
            InlineKeyboardButton("📋 Bulk Paste (50-60 Qs at once)",
                                 callback_data=f"mq_bulk_{exam_id}_{sec_id or 0}"),
            InlineKeyboardButton("✏️ Single Question (step-by-step)",
                                 callback_data=f"mq_single_{exam_id}_{sec_id or 0}"),
            InlineKeyboardButton("◀️ Back", callback_data=f"mq_exam_{exam_id}"),
        )
        _edit_or_send(chat, mid,
            "➕ <b>Add Questions</b>\n\nHow do you want to add questions?",
            kb2)

    elif d.startswith("mq_bulk_") and is_admin(uid):
        parts   = d.split("_")
        exam_id = int(parts[2])
        sec_id  = int(parts[3]) if parts[3] != "0" else None
        set_admin_state(uid, "mq_bulk_paste", {"exam_id": exam_id, "section_id": sec_id})
        bot.send_message(chat,
            "📋 <b>Bulk Question Paste Mode</b>\n\n"
            "Paste all your questions below in this format:\n\n"
            "<code>1. Question text here\n"
            "A.) Option A\n"
            "B.) Option B\n"
            "C.) Option C\n"
            "D.) Option D\n"
            "Correct : A\n"
            "Explanation : Your explanation here\n\n"
            "2. Next question...\n"
            "A.) ...\n"
            "...</code>\n\n"
            "✅ <b>Tips:</b>\n"
            "• <i>Correct</i> and <i>Explanation</i> lines are optional\n"
            "• If no correct answer given, defaults to <b>A</b>\n"
            "• Options can use <code>A)</code> / <code>A.</code> / <code>(A)</code> style\n"
            "• Send up to 60 questions in one message!\n\n"
            "📨 <b>Paste your questions now:</b>")

    elif d.startswith("mq_single_") and is_admin(uid):
        parts   = d.split("_")
        exam_id = int(parts[2])
        sec_id  = int(parts[3]) if parts[3] != "0" else None
        set_admin_state(uid, "mq_question", {"exam_id": exam_id, "section_id": sec_id})
        bot.send_message(chat, "✏️ <b>New Question</b>\n\nEnter the <b>question text</b>:")

    elif d.startswith("mq_another_") and is_admin(uid):
        parts   = d.split("_")
        exam_id = int(parts[2])
        sec_id  = int(parts[3]) if parts[3] != "0" else None
        kb2 = InlineKeyboardMarkup(row_width=1)
        kb2.add(
            InlineKeyboardButton("📋 Bulk Paste (50-60 Qs at once)",
                                 callback_data=f"mq_bulk_{exam_id}_{sec_id or 0}"),
            InlineKeyboardButton("✏️ Single Question (step-by-step)",
                                 callback_data=f"mq_single_{exam_id}_{sec_id or 0}"),
            InlineKeyboardButton("✅ Done", callback_data="admin_panel"),
        )
        bot.send_message(chat, "➕ Add more questions how?", reply_markup=kb2)

    elif d == "admin_add_section" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        if not exams:
            bot.send_message(chat, "❌ Create an exam first!"); return
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}",
                                        callback_data=f"admin_sec_exam_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "📂 Select exam to add section:", kb)

    elif d.startswith("admin_sec_exam_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        set_admin_state(uid, "add_section_name", {"exam_id": exam_id})
        bot.send_message(chat, "📂 Enter <b>Section Name</b> (e.g. Biology, English, GS):")

    elif d == "admin_upload_quiz_pdf" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        if not exams:
            bot.send_message(chat, "❌ Create an exam first!"); return
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}",
                                        callback_data=f"admin_upq_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "📤 Select exam for Quiz PDF upload:", kb)

    elif d == "admin_upload_practice_pdf" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        if not exams:
            bot.send_message(chat, "❌ Create an exam first!"); return
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}",
                                        callback_data=f"admin_upp_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "📤 Select exam for Practice PDF upload:", kb)

    elif d == "admin_quiz_settings" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}",
                                        callback_data=f"admin_qset_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "⚙️ Select exam to configure:", kb)

    elif d == "admin_set_group" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}",
                                        callback_data=f"admin_sgroup_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "💬 Select exam to set support group:", kb)

    elif d == "admin_add_resource" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']}",
                                        callback_data=f"admin_ares_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "📎 Select exam for resource:", kb)

    elif d == "admin_stats" and is_admin(uid):
        with db() as conn:
            users      = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
            total_tests = conn.execute("SELECT COUNT(*) as c FROM quiz_sessions WHERE status='completed'").fetchone()["c"]
            total_q    = conn.execute("SELECT COUNT(*) as c FROM questions").fetchone()["c"]
            today      = datetime.now().strftime("%Y-%m-%d")
            today_u    = conn.execute(
                "SELECT COUNT(*) as c FROM users WHERE joined_at LIKE ?", (f"{today}%",)
            ).fetchone()["c"]
        text = (
            f"📊 <b>Bot Statistics</b>\n\n"
            f"👥 Total Users: <b>{users}</b>\n"
            f"🆕 New Today: <b>{today_u}</b>\n"
            f"📝 Total Tests: <b>{total_tests}</b>\n"
            f"❓ Total Questions: <b>{total_q}</b>"
        )
        _edit_or_send(chat, mid, text, back_btn("admin_panel"))

    elif d == "admin_manage_questions" and is_admin(uid):
        with db() as conn:
            exams = conn.execute("SELECT * FROM exams ORDER BY id DESC").fetchall()
        kb = InlineKeyboardMarkup(row_width=1)
        for e in exams:
            with db() as conn2:
                q_cnt = conn2.execute(
                    "SELECT COUNT(*) as c FROM questions WHERE exam_id=?", (e["id"],)
                ).fetchone()["c"]
            kb.add(InlineKeyboardButton(f"{e['icon']} {e['name']} ({q_cnt} Qs)",
                                        callback_data=f"admin_qlist_{e['id']}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_panel"))
        _edit_or_send(chat, mid, "🔧 <b>Manage Questions</b>\nSelect exam:", kb)

    elif d.startswith("admin_qlist_") and is_admin(uid):
        exam_id = int(d.split("_")[-1])
        with db() as conn:
            qs = conn.execute(
                "SELECT id,question FROM questions WHERE exam_id=? ORDER BY id DESC LIMIT 20",
                (exam_id,)
            ).fetchall()
        if not qs:
            bot.send_message(chat, "📭 No questions yet!", reply_markup=back_btn("admin_manage_questions")); return
        kb = InlineKeyboardMarkup(row_width=1)
        for q in qs:
            short = q["question"][:45] + "…" if len(q["question"]) > 45 else q["question"]
            kb.add(InlineKeyboardButton(f"❌ {short}", callback_data=f"admin_delq_{q['id']}_{exam_id}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_manage_questions"))
        _edit_or_send(chat, mid, "🔧 Tap to delete a question:", kb)

    elif d.startswith("admin_delq_") and is_admin(uid):
        parts   = d.split("_")
        q_id    = int(parts[2])
        exam_id = int(parts[3])
        with db() as conn:
            conn.execute("DELETE FROM questions WHERE id=?", (q_id,))
        bot.answer_callback_query(c.id, "✅ Question deleted!")
        with db() as conn:
            qs = conn.execute(
                "SELECT id,question FROM questions WHERE exam_id=? ORDER BY id DESC LIMIT 20",
                (exam_id,)
            ).fetchall()
        kb = InlineKeyboardMarkup(row_width=1)
        for q in qs:
            short = q["question"][:45] + "…" if len(q["question"]) > 45 else q["question"]
            kb.add(InlineKeyboardButton(f"❌ {short}", callback_data=f"admin_delq_{q['id']}_{exam_id}"))
        kb.add(InlineKeyboardButton("◀️ Back", callback_data="admin_manage_questions"))
        try: bot.edit_message_reply_markup(chat, mid, reply_markup=kb)
        except: pass

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("🎓  TestBook Pro Bot v5  — Starting Up...")
    print("=" * 55)
    init_db()
    print("✅  Database initialized (new tables added)")
    print("🌍  OpenTriviaDB API integration: READY")
    print("🎯  Endless /quiz: READY")
    print("👥  Group /set quiz: READY")
    print("🤝  Doubt Group: READY")
    print("📋  Bulk question paste: READY")
    print("🤖  Bot is running! Press Ctrl+C to stop")
    print("=" * 55)
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
