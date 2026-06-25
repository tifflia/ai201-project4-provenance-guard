import uuid
import os
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

from signals import signal_1_llm_attribution
from auditor import log_event, read_log, get_entry, update_appeal, init_db

load_dotenv()
init_db()

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

@app.route("/log", methods=["GET"])
def get_log():
    """Return audit log entries."""
    entries = read_log(limit=None)
    return jsonify({"entries": entries})

@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    """Submit content for attribution analysis."""
    data = request.get_json()
    
    # Validate input
    text = data.get("text", "").strip()
    creator_id = data.get("creator_id", "").strip()
    
    if not text:
        return jsonify({"error": "Content is required."}), 400
    if not creator_id:
        return jsonify({"error": "Creator ID is required."}), 400
    
    # Generate unique content ID
    content_id = str(uuid.uuid4())
    
    # Run first signal (LLM attribution)
    signal_result = signal_1_llm_attribution(text)
    llm_score = signal_result["llm_score"]
    
    # For now, use LLM score directly as confidence; in Milestone 4 we'll combine with stylometry
    # Map llm_score to classification and displayed confidence
    if llm_score >= 0.65:
        classification = "likely_ai"
        displayed_confidence = llm_score
        label = f"Likely AI-generated content. Multiple signals indicate this text was produced primarily by an AI system. Confidence: {int(displayed_confidence * 100)}%."
    elif llm_score <= 0.35:
        classification = "likely_human"
        displayed_confidence = 1 - llm_score
        label = f"Likely human-written content. Multiple signals indicate this text was written by a human author. Confidence: {int(displayed_confidence * 100)}%."
    else:
        classification = "uncertain"
        displayed_confidence = max(llm_score, 1 - llm_score)
        label = f"Attribution uncertain. The available signals do not provide enough agreement to confidently determine whether this content was AI-generated or human-written. Confidence: {int(displayed_confidence * 100)}%."
    
    # Log to audit log
    log_event({
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": classification,
        "confidence": displayed_confidence,
        "llm_score": llm_score,
        "stylometry_score": None,  # Will be filled in Milestone 4
        "status": "classified"
    })
    
    return jsonify({
        "content_id": content_id,
        "classification": classification,
        "confidence": displayed_confidence,
        "label": label,
        "status": "classified"
    })

@app.route("/appeal", methods=["POST"])
def appeal():
    """Submit an appeal for a previous classification."""
    data = request.get_json()
    content_id = data.get("content_id")
    reasoning = data.get("creator_reasoning")
    
    # Validate input
    if not content_id or not reasoning:
        return jsonify({"error": "Content ID and reasoning are required."}), 400
    
    # Check if content exists
    entry = get_entry(content_id)
    if not entry:
        return jsonify({"error": "Content ID not found."}), 404
    
    # Update status and reasoning
    update_appeal(content_id, reasoning)
    
    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Your appeal was received and is under review."
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(port=port, debug=True)