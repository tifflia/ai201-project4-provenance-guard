import uuid
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from auditor import read_log

app = Flask(__name__)

# Initialize the limiter right after you create your app — for local development, in-memory storage is the simplest choice.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

@app.route("/")
def home():
    return "Provenance Guard is running."

# TODO: Implement the log retrieval endpoint
@app.route("/log", methods=["GET"])
def get_log():
    return jsonify({"entries": read_log()})

@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json()
    text = data.get("text")
    creator_id = data.get("creator_id")

    # TODO: Placeholder response — wire in your detection signal next.
    return jsonify({
        "content_id": str(uuid.uuid4()),
        "attribution": "uncertain",
        "confidence": 0.5,
        "label": "We're not sure who wrote this.",
    })

@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json()
    content_id = data.get("content_id")
    reasoning = data.get("creator_reasoning")

    # TODO: Update the content's status and log the appeal (see section 6).
    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Your appeal was received and is under review.",
    })

if __name__ == "__main__":
    app.run(port=5000, debug=True)