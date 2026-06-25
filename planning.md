# Provenance Guard – planning.md

## Detection Signals

Every signal in this system reports on the **same scale**: a single **AI-likelihood score `p ∈ [0, 1]`**, where

- `p = 1.0` → maximally AI-like
- `p = 0.0` → maximally human-like
- `p = 0.5` → **no information** (a coin flip)

This is a deliberate choice. Both signals are forced onto this scale so they can be combined arithmetically and so that uncertainty has a single, concrete meaning everywhere in the system: how close the score is to 0.5. A signal that says 0.5 is not "50% AI", it's "I cannot tell." Strength of evidence is `2 · |p − 0.5|` (0 = useless, 1 = maximally confident in *a* direction). We never report a bare "confidence" without first deciding *which side* of 0.5 we are on, because that is the trap that makes a 0.62 unexplainable. See [Combining the signals](#combining-the-signals-into-one-confidence-score) for how the two scores below become the one number the user sees.

---

### Signal 1 — LLM Attribution Assessment (Groq, Llama 3.3 70B)

**What it measures.** A semantic, holistic judgment of authorship: tone consistency, predictability of phrasing, repeated sentence structures, stock transitions/clichés, narrative flow, and the presence or absence of the small inconsistencies typical of human writing. This catches meaning-level cues that arithmetic heuristics cannot.

**Raw output (forced JSON).** The model is prompted to return exactly:

```json
{
  "label": "ai" | "human",
  "confidence": 0.0-1.0,        // the model's certainty in ITS OWN label
  "reasoning": "one-paragraph justification"
}
```

`confidence` here is *label-relative* ("how sure am I of the label I just gave"), which is not on our shared scale yet.

**Normalization to the shared scale.** Convert label + label-confidence into a directional AI-likelihood `llm_score`:

```
llm_score = confidence            if label == "ai"
llm_score = 1 - confidence        if label == "human"
```

Example: `{label: "human", confidence: 0.90}` → `llm_score = 0.10`.

Example: `{label: "ai", confidence: 0.55}` → `llm_score = 0.55` (weak AI lean).

**Output type:** continuous score in `[0, 1]` (plus a human-readable `reasoning` string carried into the audit log).

**Known blind spots:** highly polished human writing, heavily post-edited AI text, unusual genres. The model is also non-deterministic, so we set `temperature = 0` to keep scores stable across identical submissions.

---

### Signal 2 — Stylometric Heuristics (pure Python, no LLM)

**What it measures.** Five independent, language-statistic features that need no model and run locally. Each feature produces its own `[0, 1]` AI-leaning **sub-score** (higher = more AI-like), and the five are combined by a weighted average into a single `stylometry_score`. AI text tends to be smoother: uniform sentence lengths, narrow punctuation, repeated discourse scaffolding, heavy stock transitions.

| # | Feature | Raw measurement | Direction | Normalization → sub-score (AI-leaning) | Weight |
|---|---------|-----------------|-----------|----------------------------------------|--------|
| 1 | **Sentence-length burstiness** | Coefficient of variation `CV = std/mean` of sentence lengths (in words) | Low CV (uniform) → AI | `clamp((0.60 − CV) / (0.60 − 0.25), 0, 1)`  → CV≥0.60 = 0, CV≤0.25 = 1 | **0.35** |
| 2 | **Discourse repetition** | Fraction of repeated sentence-opening words + repeated tri-grams | More repetition → AI | `clamp(rep_rate / 0.15, 0, 1)` → ≥15% repetition = 1 | **0.25** |
| 3 | **Lexical diversity** | MATTR (moving-average type–token ratio, window = 50; length-independent) | Lower diversity → AI | `clamp((0.80 − MATTR) / (0.80 − 0.55), 0, 1)` | **0.15** |
| 4 | **Punctuation diversity** | Shannon entropy of punctuation marks `. , ; : — ! ? ( )` | Lower entropy (mostly `.` `,`) → AI | `clamp((1.4 − H) / (1.4 − 0.5), 0, 1)` | **0.15** |
| 5 | **Stock-transition density** | Count per 100 words of a fixed lexicon ("however", "moreover", "furthermore", "in conclusion", "it's important to note", "delve", "tapestry", …) | Higher → AI | `clamp(density / 2.0, 0, 1)` → ≥2 per 100 words = 1 | **0.10** |

```
stylometry_score = Σ (weight_i · sub_score_i)        # weights sum to 1.0
```

**Output type:** continuous score in `[0, 1]`. The five sub-scores are retained for the audit log / debugging so any final number is traceable to its parts.

**Known blind spots:** can't read meaning; highly structured
human writing (legal, academic) reads as AI; informal/typo-laden AI reads as
human; unreliable on short text (see [degraded modes](#degraded-modes--edge-cases)).
The thresholds above are starting heuristics to be tuned against a small labeled
sample, not learned parameters.

---

### Combining the signals into one confidence score

Three steps: weighted blend → disagreement shrinkage → classify & report.

**Step 1 — Weighted blend.** The LLM is the stronger semantic signal, so it
carries more weight:

```
w_llm = 0.6,  w_sty = 0.4
p_raw = w_llm · llm_score + w_sty · stylometry_score
```

**Step 2 — Disagreement shrinkage (this is how disagreement *lowers*
confidence).** Plain averaging already pulls opposing signals toward 0.5, but we
penalize disagreement *explicitly* so two confident-but-conflicting signals
don't masquerade as a confident middle:

```
disagreement = |llm_score − stylometry_score|        # 0..1
p_ai = 0.5 + (p_raw − 0.5) · (1 − λ · disagreement)   # λ = 0.5
```

With `λ = 0.5`, total disagreement (1.0) halves the distance from 0.5, dragging
the result toward "uncertain." Full agreement (`disagreement = 0`) leaves
`p_ai = p_raw` untouched. This is the single mechanism that encodes "strong
agreement → higher confidence, disagreement → lower confidence."

**Step 3 — Classify and report.** `p_ai` maps to a label via an explicit
**dead-zone** around 0.5, and the reported confidence is always the probability
of the *chosen* class:

| Condition | `classification` | Reported `confidence` |
|-----------|------------------|-----------------------|
| `p_ai ≥ 0.65` | `likely_ai` | `p_ai` |
| `p_ai ≤ 0.35` | `likely_human` | `1 − p_ai` |
| `0.35 < p_ai < 0.65` | `uncertain` | `max(p_ai, 1 − p_ai)` |

Reporting the chosen-class probability is why the user never sees a confusing score: an 8%-AI result is presented as **"likely human, 92% confident,"** not "8% confident." An `uncertain` result's confidence sits in 0.50–0.65 by construction (e.g. 58% = "barely past a coin flip, not enough to commit").

**The two numbers stored vs. the one number shown.** The audit log keeps `llm_score` and `stylometry_score` (and the stylometry sub-scores) so every decision is reproducible; the API returns `classification` + `confidence` only.

---

### Representing uncertainty

Uncertainty is **distance from 0.5**, and it grows from exactly three sources, all visible in the pipeline:

1. **Weak individual signals** — either score near 0.5 means that signal itself
   is unsure, and the blend inherits it.
2. **Disagreement** — Step 2 actively pushes conflicting signals toward 0.5.
3. **The dead-zone** — `0.35–0.65` is a committed "we won't guess" band rather
   than forcing a coin-flip label.

This is why we can explain any score. **Worked example of a 0.62:** `llm_score = 0.70` (LLM leans AI), `stylometry_score = 0.50` (heuristics flat). `p_raw = 0.6·0.70 + 0.4·0.50 = 0.62`; `disagreement = 0.20`, shrinkage `= 1 − 0.5·0.20 = 0.90`, so `p_ai = 0.5 + 0.12·0.90 = 0.608` → **uncertain, confidence ≈ 0.61.** Plain-language: *"One signal leans AI, the other is neutral, and they don't strongly agree — so we won't commit."* No mystery.

---

### Degraded modes & edge cases

- **Short text (< ~40 words):** stylometry is statistically unreliable. Drop its
  weight to `w_sty = 0.15` (`w_llm = 0.85`) **and** widen the dead-zone to
  `0.30–0.70`, so short submissions resolve to `uncertain` unless the LLM is
  very confident.
- **LLM unavailable (Groq error/timeout/rate-limit):** fall back to stylometry
  only, but **cap reported confidence at 0.75** and tag the audit entry, since a
  single heuristic signal should never look maximally sure.
- **Stylometry unparsable (no sentence breaks, e.g. one giant token):** fall
  back to LLM only, same 0.75 cap.
- **Empty/whitespace text:** rejected at validation before the pipeline runs.

---

## Transparency Labels

The final classification and confidence score are passed into the label generator which converts technical classification results into language understandable by ordinary platform users.

The generator selects one of three reader-facing transparency labels:

### High-Confidence AI

Displayed when both signals strongly indicate AI generation.

Example:
```
Likely AI-generated content. Multiple detection signals indicate this text was produced primarily by an AI system. Confidence: 94%.
```

### High-Confidence Human

Displayed when both signals strongly indicate human authorship.

Example:
```
Likely human-written content. Multiple detection signals indicate this text was written by a human author. Confidence: 92%.
```

### Uncertain

Displayed when evidence is mixed or confidence is low.

Example:
```
Attribution uncertain. The available signals do not provide enough agreement to confidently determine whether this content was AI-generated or human-written. Confidence: 58%.
```

## Appeals Workflow

Any creator who originally submitted the content may appeal a classification. The appeal endpoint is designed for the same `creator_id` associated with the original submission, but the system requires only `content_id` and creator reasoning for this project scope.

The appeal request includes:

- `content_id`: the unique identifier of the previously classified content
- `creator_reasoning`: a short explanation of why the creator believes the classification is incorrect

When the appeal is received, the system:

1. Finds the original audit record for the given `content_id`.
2. Updates the stored status from `classified` to `under_review`.
3. Adds or updates the `appeal_reasoning` field with the creator's explanation.
4. Records the appeal update in the audit log alongside the original decision, so the original classification and the appeal are visible together.

No automatic re-classification occurs as part of this workflow.

Upon opening the appeal queue, a human reviewer would see:

- `content_id`
- `creator_id` (who submitted the original content)
- timestamp of the original classification and the appeal submission
- original classification result (`likely_ai`, `likely_human`, or `uncertain`)
- original confidence score
- original signal scores (`llm_score`, `stylometry_score`)
- current `status`: `under_review`
- `appeal_reasoning`: the creator's explanation of why the decision should be revisited

This makes the appeal queue a full, structured view of the decision plus the creator's protest, without changing the original classification data.

---

## Architecture

The submission flow begins with `POST /submit`, where raw text and creator identity enter the API, pass rate limiting, and are evaluated by two independent signals. The resulting signal scores are combined into a single confidence score, converted into reader-facing label text, recorded in the audit log, and returned with the final attribution.

The appeal flow begins with `POST /appeal`, where a creator submits a content ID and reasoning; the system updates the stored status to `under_review`, logs the appeal alongside the original decision, and returns a confirmation response.

### Submission Flow
```
POST /submit
        │ raw text + creator_id
        ▼
 Flask API Endpoint
        │ validated request
        ▼
 Flask-Limiter
        │ allowed request
        ▼
 ┌────────────────────┐
 │ Detection Signal 1 │
 │   Groq LLM         │
 └────────────────────┘
        │ llm_score
        ▼
 ┌────────────────────┐
 │ Detection Signal 2 │
 │   Stylometry       │
 └────────────────────┘
        │ stylometry_score
        ▼
 Confidence Scoring
        │ combined confidence
        ▼
 Transparency Label Generator
        │ label text
        ▼
 Audit Log (SQLite/JSON)
        │ structured entry
        ▼
 API Response Returned
        │ content_id, attribution, confidence, label, status
```

### Appeal Flow
```
POST /appeal
        │ content_id + creator_reasoning
        ▼
 Appeals API Endpoint
        │ update status, attach reasoning
        ▼
 Audit Log (SQLite/JSON)
        │ same content_id, status under_review, appeal_reasoning
        ▼
 API Response Returned
        │ content_id, status, message
```

---

## AI Tool Plan

### Milestone 3 — Submission endpoint + first signal
- Provide: the `Detection Signals` section and the `Architecture` diagram.
- Ask for: a Flask app skeleton with `POST /submit` and a first signal function that returns the LLM-based score and normalized `llm_score` output.
- Verify by: testing the first signal function directly with a few example inputs, then confirming `POST /submit` returns `content_id`, `attribution`, provisional `confidence`, and `label`.

### Milestone 4 — Second signal + confidence scoring
- Provide: the `Detection Signals` section, the `Representing uncertainty` section, and the `Architecture` diagram.
- Ask for: a second stylometric signal function plus scoring logic that combines `llm_score` and `stylometry_score` into one calibrated `confidence` and classification.
- Verify by: checking that the combined score differs for clearly AI-like versus clearly human-like text and that the audit log records both individual signal scores.

### Milestone 5 — Production layer
- Provide: the `Transparency Labels` section, the `Appeals Workflow` section, and the `Architecture` diagram.
- Ask for: label generation logic that returns the exact three written label variants based on the confidence thresholds, plus the `POST /appeal` endpoint that updates status and logs appeal reasoning.
- Verify by: ensuring all three label variants can be produced from different inputs and confirming that submitting an appeal sets status to `under_review` and stores `appeal_reasoning` in the audit log.

