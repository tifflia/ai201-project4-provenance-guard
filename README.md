# Provenance Guard

Provenance Guard is a backend system that any creative sharing platform can plug into to classify submitted content, score confidence in that classification, surface a transparency label to users, and handle appeals from creators who believe they've been misclassified. This tool gives online audiences the context and confidence they need when it comes to differentiating between AI and human-made content.

## Setup

Requires Python 3.9+ and a [Groq API key](https://console.groq.com/keys).

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
GROQ_API_KEY=your_key_here
```

Run the app:

```bash
python app.py                       # http://127.0.0.1:5000
PORT=8000 python app.py             # or pick a port
```

To exercise the two signals directly against the built-in sample texts, without starting the server:

```bash
python signals.py
```

## Usage

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Health check; returns a plain-text string. |
| `POST /submit` | Classify text. [Rate Limited](#rate-limiting). |
| `POST /appeal` | Flag a classification for human review. |
| `GET /log` | Return every audit-log entry, newest first. |

**Classify a submission.** Both `text` and `creator_id` are required; either one missing or blank returns `400`.

```bash
curl -X POST http://127.0.0.1:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "example text", "creator_id": "creator_ex"}'
```

**Appeal a classification.** Takes the `content_id` from the submit response plus `creator_reasoning`; both are required (`400` otherwise), and an unrecognized `content_id` returns `404`. The matching audit row moves to `status: "under_review"` and stores the reasoning and a timestamp.

```bash
curl -X POST http://127.0.0.1:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "3f9a1c8e-...", "creator_reasoning": "example reasoning"}'
```

**Read the audit log.** Returns `{"entries": [...]}`, each entry carrying the full stored row (see [What is persisted](#confidence-scoring-with-uncertainty)).

```bash
curl http://127.0.0.1:5000/log
```

## Multi-Signal Detection Pipeline

Every signal in this system reports on the **same scale**: a single **AI-likelihood score `p ∈ [0, 1]`**, where

- `p = 1.0` → maximally AI-like
- `p = 0.0` → maximally human-like
- `p = 0.5` → **no information** (a coin flip)

This is a deliberate choice. Both signals are forced onto this scale so they can be combined arithmetically and so that uncertainty has a single, concrete meaning everywhere in the system: how close the score is to 0.5. A signal that says 0.5 is not "50% AI", it's "I cannot tell."

---

### Signal 1 — LLM Attribution Assessment (Groq, Llama 3.3 70B)

**What it measures.** A semantic, holistic judgment of authorship: tone consistency, predictability of phrasing, repeated sentence structures, stock transitions/clichés, narrative flow, and the presence or absence of the small inconsistencies typical of human writing. This catches meaning-level cues that arithmetic heuristics cannot.

**Raw output (forced JSON).** The model is prompted to return exactly:

```json
{
  "label": "ai" | "human",
  "confidence": 0.0-1.0,        // the model's certainty in ITS OWN label
  "reasoning": "one-sentence justification"
}
```

`confidence` here is *label-relative* ("how sure am I of the label I just gave"), which is not on our shared scale yet.

**Normalization to the shared scale.** Convert label + label-confidence into a directional AI-likelihood `llm_score`:

```
llm_score = confidence            if label == "ai"
llm_score = 1 - confidence        if label == "human"
```

Example: `{label: "human", confidence: 0.90}` → `llm_score = 0.10`. `{label: "ai", confidence: 0.55}` → `llm_score = 0.55` (weak AI lean).

**Output type:** continuous score in `[0, 1]`, plus the label, the raw label-confidence, and a human-readable `reasoning` string.

---

### Signal 2 — Stylometric Heuristics (no LLM)

**What it measures.** Five independent, language-statistic features that need no model and run locally. Each feature produces its own `[0, 1]` AI-leaning **sub-score** (higher = more AI-like), and the five are combined by a weighted average into a single `stylometry_score`. AI text tends to be smoother: uniform sentence lengths, narrow punctuation, repeated discourse scaffolding, heavy stock transitions.

| # | Feature | Raw measurement | Direction | Normalization → sub-score (AI-leaning) | Weight |
|---|---------|-----------------|-----------|----------------------------------------|--------|
| 1 | **Sentence-length burstiness** | Coefficient of variation `CV = std/mean` of sentence lengths (in words) | Low CV (uniform) → AI | `clamp((0.45 − CV) / 0.25, 0, 1)` → CV≥0.45 = 0, CV≤0.20 = 1 | **0.20** |
| 2 | **Discourse repetition** | Mean of repeated sentence-prefix rate (first 3 words, pairwise) and repeated bi-gram rate | More repetition → AI | `clamp(rep_rate / 0.35, 0, 1)` → ≥35% repetition = 1 | **0.15** |
| 3 | **Lexical diversity** | MATTR (moving-average type–token ratio, window = 50; length-independent) | Lower diversity → AI | `clamp((0.80 − MATTR) / 0.25, 0, 1)` → MATTR≥0.80 = 0, MATTR≤0.55 = 1 | **0.25** |
| 4 | **Punctuation diversity** | Shannon entropy `H` of punctuation marks `. , ; : — ! ? ( ) [ ] { }` | Lower entropy (mostly `.` `,`) → AI | `clamp((1.2 − H) / 0.7, 0, 1)` → H≥1.2 = 0, H≤0.5 = 1 | **0.20** |
| 5 | **Stock-transition density** | Count per 100 words of a fixed lexicon ("however", "moreover", "furthermore", "in conclusion", "it is important to note", "delve", "tapestry", …) | Higher → AI | `clamp(density / 2.0, 0, 1)` → ≥2 per 100 words = 1 | **0.20** |

```
stylometry_score = Σ (weight_i · sub_score_i)        # weights sum to 1.0
```

**Output type:** continuous score in `[0, 1]`, plus the five sub-scores so any final number is traceable to its parts.

## Confidence Scoring with Uncertainty

**Step 1 — Weighted blend.** The LLM is the stronger semantic signal, so it carries more weight:

```
w_llm = 0.6,  w_sty = 0.4
p_raw = w_llm · llm_score + w_sty · stylometry_score
```

**Step 2 — Disagreement shrinkage (this is how disagreement *lowers* confidence).** Plain averaging already pulls opposing signals toward 0.5, but we penalize disagreement *explicitly* so two confident-but-conflicting signals don't masquerade as a confident middle:

```
disagreement = |llm_score − stylometry_score|        # 0..1
p_ai = 0.5 + (p_raw − 0.5) · (1 − λ · disagreement)   # λ = 0.5
```

With `λ = 0.5`, total disagreement (1.0) halves the distance from 0.5, dragging the result toward "uncertain." Full agreement (`disagreement = 0`) leaves `p_ai = p_raw` untouched. This is the single mechanism that encodes "strong agreement → higher confidence, disagreement → lower confidence."

**Step 3 — Classify and report.** `p_ai` maps to a label via an explicit **dead-zone** around 0.5, and the reported confidence is always the probability of the *chosen* class:

| Condition | `classification` | Reported `confidence` |
|-----------|------------------|-----------------------|
| `p_ai ≥ 0.65` | `likely_ai` | `p_ai` |
| `p_ai ≤ 0.35` | `likely_human` | `1 − p_ai` |
| `0.35 < p_ai < 0.65` | `uncertain` | `max(p_ai, 1 − p_ai)` |

Reporting the chosen-class probability is why the user never sees a confusing score: an 8%-AI result is presented as **"likely human, 92% confident,"** not "8% confident." An `uncertain` result's confidence sits in 0.50–0.65 by construction (e.g. 58% = "barely past a coin flip, not enough to commit").

**What is persisted.** The audit log row keeps `llm_score` and `stylometry_score` alongside the final `attribution` and `confidence`, so every decision can be recomputed from its inputs. The stylometry sub-scores and the LLM's `reasoning` string are returned in-process for debugging but are not written to the database. `POST /submit` responds with `content_id`, `classification`, `confidence`, `label`, and `status`.

---

### High-Confidence Example — clearly human-written

**Text**: "ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got the spicy version and said it was better. probably won't go back unless someone drags me there"

| `llm_score` | `stylometry_score` | combined `p_ai` | `classification` | `confidence` |
|---|---|---|---|---|
| 0.100 | 0.114 | 0.109 | `likely_human` | 0.891 |

The two signals agree closely (disagreement = 0.014), so shrinkage barely applies and the result stays confident.

### Low-Confidence Example — borderline formal human writing

**Text**: "The relationship between monetary policy and asset price inflation has been extensively studied in the literature. Central banks face a fundamental tension between their mandate for price stability and the unintended consequences of prolonged low interest rates on equity and real estate valuations."

| `llm_score` | `stylometry_score` | combined `p_ai` | `classification` | `confidence` |
|---|---|---|---|---|
| 0.800 | 0.445 | 0.630 | `uncertain` | 0.630 |

Here `p_raw` was 0.658, but a disagreement of 0.355 shrinks it to 0.630 and the dead-zone catches it as `uncertain` instead of a confident false positive.

## Transparency Label

The final classification and confidence are passed into the label generator, which converts the technical result into language an ordinary platform user can read.

| `classification` | Label text |
|------------------|------------|
| `likely_ai` | Likely AI-generated content. Multiple signals indicate this text was produced primarily by an AI system. Confidence: 94%. |
| `likely_human` | Likely human-written content. Multiple signals indicate this text was written by a human author. Confidence: 92%. |
| `uncertain` | Attribution uncertain. The available signals do not provide enough agreement to confidently determine whether this content was AI-generated or human-written. Confidence: 58%. |

## Rate Limiting

Only `POST /submit` is rate-limited, because it is the one expensive endpoint: every call makes an outbound Groq LLM request that costs money and latency. `GET /log` and `POST /appeal` are cheap, local-only operations and are left unlimited. Limits are applied **per client IP** (`get_remote_address`) so one abusive caller cannot starve the rest, and stored in-memory (`memory://`) since this is a single-process development service. A client over the limit receives `429` with `{"error": "Rate limit exceeded. Please try again later."}`.

**Chosen limits on `/submit`: `10 per minute; 100 per day`.**

| Limit | Reasoning |
|-------|-----------|
| **10 / minute** | A real creator submits a poem, a story excerpt, or a blog post and then likely reads the result. Ten in a minute comfortably covers someone re-checking a few drafts back-to-back, while immediately throttling a script firing requests as fast as it can. It is the burst ceiling. |
| **100 / day** | This is the abuse/cost ceiling. A platform integrator testing the service, or a prolific creator clearing a backlog, realistically stays well under 100 attributions in a day. Sustained traffic above this looks like automated scraping of the classifier. |

## Known Limitations

**The content we most reliably get wrong: highly structured, formal human writing** — academic abstracts, legal prose, financial/policy analysis, technical documentation. This is a direct consequence of *what our two signals actually measure*, not a data-volume problem:

- **Stylometry is built to reward messiness as "human."** Four of its five features (sentence-length burstiness, lexical diversity, punctuation entropy, low stock-transition density) treat *uniformity* as the fingerprint of AI. But a trained human writing formally produces that uniformity on purpose.
- **The LLM signal shares the same bias.** It was trained on text where formal, evenly-structured, cliché-dense prose strongly correlates with AI generation, so it tends to *agree* with stylometry on this class of writing rather than correct it. Because both signals lean the same wrong direction, there is no disagreement to shrink and the false "AI" lean is preserved.

The financial-policy example in the [Low-Confidence Example](#low-confidence-example--borderline-formal-human-writing) above is exactly this case. The dead-zone caught it, but a slightly more polished passage would clear the `0.65` bar and be mislabeled `likely_ai`. The mitigation is structural (the `uncertain` band and the appeals workflow), because the signals cannot distinguish disciplined human craft from machine fluency.

The symmetric failure also holds: **casual, error-laden AI output** (text prompted to be sloppy, with typos and slang) reads as human, and heavily post-edited AI text inherits enough human variation to slip through, because the same features score deliberate messiness as authenticity.
