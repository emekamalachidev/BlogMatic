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
import stripe
import requests
from dotenv import load_dotenv

# Load env vars
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
JWT_SECRET = os.getenv("JWT_SECRET", "blogmatic-secret")

openai.api_key = OPENAI_API_KEY
stripe.api_key = STRIPE_SECRET_KEY

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
    free_credits INTEGER DEFAULT 3,
    stripe_customer_id TEXT
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


# ---------------- STRIPE CHECKOUT ----------------
@app.route("/api/checkout", methods=["POST"])
@jwt_required()
def checkout():
    email = get_jwt_identity()
    c.execute("SELECT stripe_customer_id FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if row and row[0]:
        customer_id = row[0]
    else:
        customer = stripe.Customer.create(email=email)
        customer_id = customer.id
        c.execute(
            "UPDATE users SET stripe_customer_id=? WHERE email=?",
            (customer_id, email)
        )
        conn.commit()

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=request.host_url + "?success=true",
        cancel_url=request.host_url + "?cancelled=true"
    )

    return jsonify(url=session.url)


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, STRIPE_WEBHOOK_SECRET
        )
    except:
        return "", 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer = stripe.Customer.retrieve(session.customer)
        c.execute(
            "UPDATE users SET subscribed=1 WHERE email=?",
            (customer.email,)
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
