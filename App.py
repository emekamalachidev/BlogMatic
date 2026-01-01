import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    jwt_required,
    get_jwt_identity
)
import openai
import requests
from dotenv import load_dotenv

# ---------------- ENV ----------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
JWT_SECRET = os.getenv("JWT_SECRET", "blogmatic-secret")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_PLAN_ID = os.getenv("PAYSTACK_PLAN_ID")

openai.api_key = OPENAI_API_KEY

# ---------------- APP SETUP ----------------
app = Flask(__name__, static_folder="static")
CORS(app)
app.config["JWT_SECRET_KEY"] = JWT_SECRET
jwt = JWTManager(app)

# ---------------- DATABASE ----------------
conn = sqlite3.connect("db.sqlite3", check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE,
    password TEXT,
    subscribed INTEGER DEFAULT 0,
    free_credits INTEGER DEFAULT 3
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS blogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    topic TEXT,
    content TEXT,
    created_at TEXT
)
""")
conn.commit()

# ---------------- AUTH ----------------
@app.route("/api/register", methods=["POST"])
def register():
    data = request.json
    try:
        c.execute(
            "INSERT INTO users (email, password) VALUES (?, ?)",
            (data["email"], data["password"])
        )
        conn.commit()
        token = create_access_token(identity=data["email"])
        return jsonify(token=token)
    except:
        return jsonify(error="User already exists"), 400

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    c.execute(
        "SELECT * FROM users WHERE email=? AND password=?",
        (data["email"], data["password"])
    )
    if c.fetchone():
        token = create_access_token(identity=data["email"])
        return jsonify(token=token)
    return jsonify(error="Invalid credentials"), 401

# ---------------- AI BLOG GENERATION ----------------
@app.route("/api/generate", methods=["POST"])
@jwt_required()
def generate():
    email = get_jwt_identity()
    c.execute("SELECT id, free_credits, subscribed FROM users WHERE email=?", (email,))
    user = c.fetchone()

    if not user:
        return jsonify(error="User not found"), 404

    user_id, free_credits, subscribed = user

    if free_credits <= 0 and not subscribed:
        return jsonify(error="Free limit reached. Please subscribe."), 402

    topic = request.json.get("topic")

    prompt = f"""
Write a high-quality, SEO-optimized blog post about "{topic}".

Return JSON ONLY with:
- title
- meta_description (155 chars)
- content (HTML with H1, H2, paragraphs)
- tags (5 keywords)
"""

    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )

    blog_json = json.loads(response.choices[0].message.content)

    c.execute(
        "INSERT INTO blogs (user_id, topic, content, created_at) VALUES (?, ?, ?, ?)",
        (user_id, topic, json.dumps(blog_json), datetime.utcnow().isoformat())
    )

    if not subscribed:
        c.execute(
            "UPDATE users SET free_credits = free_credits - 1 WHERE id=?",
            (user_id,)
        )

    conn.commit()
    return jsonify(content=blog_json)

# ---------------- PAYSTACK CHECKOUT ----------------
@app.route("/api/checkout", methods=["POST"])
@jwt_required()
def checkout():
    email = get_jwt_identity()
    c.execute("SELECT subscribed FROM users WHERE email=?", (email,))
    user = c.fetchone()
    if not user:
        return jsonify(error="User not found"), 404

    # Create a Paystack transaction
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "email": email,
        "plan": PAYSTACK_PLAN_ID
    }
    response = requests.post(
        "https://api.paystack.co/transaction/initialize",
        headers=headers,
        json=payload
    )
    data = response.json()
    if not data.get("status"):
        return jsonify(error="Paystack initialization failed"), 500

    return jsonify(url=data["data"]["authorization_url"])

# ---------------- WEBHOOK ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    event = request.json

    # Only handle subscription payment success events
    if event.get("event") == "charge.success":
        customer_email = event["data"]["customer"]["email"]
        c.execute(
            "UPDATE users SET subscribed=1 WHERE email=?",
            (customer_email,)
        )
        conn.commit()
    return "", 200

# ---------------- ADMIN ----------------
@app.route("/api/admin/stats")
@jwt_required()
def admin():
    email = get_jwt_identity()
    if email != "admin@example.com":
        return jsonify(error="Forbidden"), 403

    c.execute("SELECT email, subscribed, free_credits FROM users")
    users = c.fetchall()
    return jsonify(users=users)

# ---------------- FRONTEND ----------------
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    if path and os.path.exists(os.path.join("static", path)):
        return send_from_directory("static", path)
    return send_from_directory("static", "index.html")

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

