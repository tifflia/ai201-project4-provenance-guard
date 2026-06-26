# Provenance Guard

Provenance Guard is a backend system that any creative sharing platform can plug into to classify submitted content, score confidence in that classification, surface a transparency label to users, and handle appeals from creators who believe they've been misclassified. This tool gives online audiences the context and confidence they need when it comes to differentiating between AI and human-made content.

## Multi-Signal Detection Pipeline

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
| 1 | **Sentence-length burstiness** | Coefficient of variation `CV = std/mean` of sentence lengths (in words) | Low CV (uniform) → AI | `clamp((0.60 − CV) / (0.60 − 0.25), 0, 1)`  → CV≥0.60 = 0, CV≤0.25 = 1 | **0.20** |
| 2 | **Discourse repetition** | Fraction of repeated sentence-prefixes + repeated bi-grams | More repetition → AI | `clamp(rep_rate / 0.35, 0, 1)` → ≥35% repetition = 1 | **0.15** |
| 3 | **Lexical diversity** | MATTR (moving-average type–token ratio, window = 50; length-independent) | Lower diversity → AI | `clamp((0.80 − MATTR) / (0.80 − 0.55), 0, 1)` | **0.25** |
| 4 | **Punctuation diversity** | Shannon entropy of punctuation marks `. , ; : — ! ? ( )` | Lower entropy (mostly `.` `,`) → AI | `clamp((1.4 − H) / (1.4 − 0.5), 0, 1)` | **0.20** |
| 5 | **Stock-transition density** | Count per 100 words of a fixed lexicon ("however", "moreover", "furthermore", "in conclusion", "it's important to note", "delve", "tapestry", …) | Higher → AI | `clamp(density / 2.0, 0, 1)` → ≥2 per 100 words = 1 | **0.20** |

```
stylometry_score = Σ (weight_i · sub_score_i)        # weights sum to 1.0
```

**Output type:** continuous score in `[0, 1]`. The five sub-scores are retained for the audit log / debugging so any final number is traceable to its parts.

**Known blind spots:** can't read meaning; highly structured
human writing (legal, academic) reads as AI; informal/typo-laden AI reads as human; unreliable on short text (see [known limitations](#known-limitations)). The thresholds above are starting heuristics to be tuned against a small labeled sample, not learned parameters.

## Confidence Scoring with Uncertainty

### Combining the signals into one confidence score

**Step 1 — Weighted blend.** The LLM is the stronger semantic signal, so it carries more weight:

```
w_llm = 0.6,  w_sty = 0.4
p_raw = w_llm · llm_score + w_sty · stylometry_score
```

**Step 2 — Disagreement shrinkage (this is how disagreement *lowers*
confidence).** Plain averaging already pulls opposing signals toward 0.5, but we penalize disagreement *explicitly* so two confident-but-conflicting signals don't masquerade as a confident middle:

```
disagreement = |llm_score − stylometry_score|        # 0..1
p_ai = 0.5 + (p_raw − 0.5) · (1 − λ · disagreement)   # λ = 0.5
```

With `λ = 0.5`, total disagreement (1.0) halves the distance from 0.5, dragging the result toward "uncertain." Full agreement (`disagreement = 0`) leaves `p_ai = p_raw` untouched. This is the single mechanism that encodes "strong agreement → higher confidence, disagreement → lower confidence."

**Step 3 — Classify and report.** `p_ai` maps to a label via an explicit
**dead-zone** around 0.5, and the reported confidence is always the probability of the *chosen* class:

| Condition | `classification` | Reported `confidence` |
|-----------|------------------|-----------------------|
| `p_ai ≥ 0.65` | `likely_ai` | `p_ai` |
| `p_ai ≤ 0.35` | `likely_human` | `1 − p_ai` |
| `0.35 < p_ai < 0.65` | `uncertain` | `max(p_ai, 1 − p_ai)` |

Reporting the chosen-class probability is why the user never sees a confusing score: an 8%-AI result is presented as **"likely human, 92% confident,"** not "8% confident." An `uncertain` result's confidence sits in 0.50–0.65 by construction (e.g. 58% = "barely past a coin flip, not enough to commit").

**The two numbers stored vs. the one number shown.** The audit log keeps `llm_score` and `stylometry_score` (and the stylometry sub-scores) so every decision is reproducible; the API returns `classification` + `confidence` only.

---

### High-Confidence Example

#### _Clearly human-written (should score low)_

**Text**: "ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got the spicy version and said it was better. probably won't go back unless someone drags me there"

- **llm_score**: `0.100`
- **stylometry_score**: `0.114`
- **combined p_ai**: `0.109`

**Results:**
- **final_classification**: `likely_human`
- **final_confidence**: `0.891`

---

### Low-Confidence Example

#### _Borderline: formal human writing (may score mid-high on stylometrics)_

**Text**: "The relationship between monetary policy and asset price inflation has been extensively studied in the literature. Central banks face a fundamental tension between their mandate for price stability and the unintended consequences of prolonged low interest rates on equity and real estate valuations."

- **llm_score**: `0.800`
- **stylometry_score**: `0.445`
- **combined p_ai**: `0.630`

**Results:**
- **final_classification**: `uncertain`
- **final_confidence**: `0.630`


## Transparency Label

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

## Rate Limiting

Only `POST /submit` is rate-limited, because it is the one expensive endpoint: every call makes an outbound Groq LLM request that costs money and latency. `GET /log` and `POST /appeal` are cheap, local-only operations and are left unlimited. Limits are applied **per client IP** (`get_remote_address`) so one abusive caller cannot starve the rest, and stored in-memory (`memory://`) since this is a single-process development service.

**Chosen limits on `/submit`: `10 per minute; 100 per day`.**

| Limit | Reasoning |
|-------|-----------|
| **10 / minute** | A real creator submits a poem, a story excerpt, or a blog post and then *reads the result* — that is a human-paced action, not a tight loop. Ten in a minute comfortably covers someone re-checking a few drafts back-to-back, while immediately throttling a script firing requests as fast as it can. It is the burst ceiling. |
| **100 / day** | This is the abuse/cost ceiling. A platform integrator testing the service, or a prolific creator clearing a backlog, realistically stays well under 100 attributions in a day; sustained traffic above that looks like automated scraping of the classifier (e.g. an adversary probing the thresholds to learn how to evade them). Capping the daily total bounds the maximum Groq spend per client even if they pace themselves to stay under the per-minute limit. |

The two limits work together: the per-minute cap stops bursts, and the per-day cap stops a slow drip that would respect the per-minute cap but still rack up thousands of paid LLM calls. A client over the limit receives `429` with `{"error": "Rate limit exceeded. Please try again later."}`.

## Known Limitations

**The content we most reliably get wrong: highly structured, formal human writing** — academic abstracts, legal prose, financial/policy analysis, technical documentation. This is a direct consequence of *what our two signals actually measure*, not a data-volume problem:

- **Stylometry is built to reward messiness as "human."** Four of its five features (sentence-length burstiness, lexical diversity, punctuation entropy, low stock-transition density) treat *uniformity* as the fingerprint of AI. But a trained human writing formally produces exactly that uniformity on purpose: even sentence lengths, a controlled vocabulary, a narrow punctuation set, and dense connectives like "however / moreover / furthermore." The stylometry score reads that discipline as machine-like and pushes the result toward AI.
- **The LLM signal shares the same bias.** It was trained on text where formal, evenly-structured, cliché-dense prose strongly correlates with AI generation, so it tends to *agree* with stylometry on this class of writing rather than correct it. Because both signals lean the same wrong direction, there is no disagreement to shrink and the false "AI" lean is preserved or amplified.

The financial-policy example in the [Low-Confidence Example](#low-confidence-example) above is exactly this case: genuinely human text landing at `p_ai = 0.63`. Our dead-zone catches it as `uncertain` rather than a confident false positive, but a slightly more polished passage would clear the `0.65` bar and be mislabeled `likely_ai`. The mitigation is structural (the `uncertain` band and the appeals workflow). The signals cannot distinguish disciplined human craft from machine fluency, because fluency is the only thing they know how to measure.

The symmetric failure also holds: **casual, error-laden AI output** (text prompted to be sloppy, with typos and slang) reads as human, because the same features score deliberate messiness as authenticity.


## Spec Reflection

- The spec helped by providing a clear outline for all the components that needed to go into the multi-detection signal pipeline. Everything from that and the confidence scoring section was down to the formula, which was an excellent artifact to use when prompting.
- The implementation diverged because I ended up modifying some of the stylometric heuristic measurements for the second detection signal. I replaced the measures that went into the discourse repetition score after realizing it was a little too strict. I also changed the weight of each feature to be a little more balanced as the heuristics that looked promising were not reliable enough to contribute to 60% of the stylometry score.

## AI Usage

1. **Milestone 3 — LLM attribution signal.** I directed the AI to implement Signal 1 from the spec: call the Groq Llama 3.3 70B model, prompt it to assess authorship, and return the forced JSON (`label`, `confidence`, `reasoning`) normalized to the shared `llm_score` scale. It produced working code that matched the spec's behavior. **What I revised:** the generated version packed all of the instructions into a single user-prompt block. I split it into a proper system prompt (the persistent role and scoring rubric) and a user prompt (just the submitted text), so the instructions stay stable and the model treats the content as data to analyze rather than as part of its instructions.

2. **Milestone 4 — stylometric heuristics.** I directed the AI to implement Signal 2's five stylometric features and the weighting from the spec. It produced the measurements as written. **What I overrode:** while testing against sample text I found the discourse-repetition feature was too strict and rarely fired, so I replaced the measures feeding it (loosening from the original definition to repeated sentence-prefixes + repeated bi-grams, with the threshold widened to `0.35`). I also rebalanced the feature weights away from the spec's draft (see [Spec Reflection](#spec-reflection)).