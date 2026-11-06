from flask import Flask, request, session
import redis

from users import check_password

app = Flask(__name__)
app.secret_key = "dev-only"
store = redis.Redis()


@app.post("/login")
def login():
    username = request.form["username"]
    if not check_password(username, request.form["password"]):
        return {"error": "invalid credentials"}, 401
    session["user"] = username
    return {"ok": True}
