"""AI Summarization and Key Topics analysis (Phase 6).

Generates:
  - Executive summary
  - Key discussion topics
  - Questions & Answers / highlights
  - Speaker participation analytics (% talk time, turns)

Works with local LLMs (Ollama / vLLM / OpenAI-compatible) and includes an
algorithmic fallback when no LLM is configured.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from xsa.config import Settings, get_settings
from xsa.db import Space, Topic, get_session
from xsa.logging_setup import get_logger
from xsa.paths import SpacePaths

log = get_logger("ANALYZER")
LLM_CHUNK_CHARS = 9000
LLM_REDUCTION_CHARS = 9000


def _split_long_line(line: str, limit: int) -> list[str]:
    """Split oversized transcript lines without dropping any source text."""
    words = line.split()
    if not words:
        return []
    parts: list[str] = []
    current = ""
    for word in words:
        if len(word) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.extend(word[index : index + limit] for index in range(0, len(word), limit))
            continue
        candidate = f"{current} {word}" if current else word
        if len(candidate) > limit:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _chunk_lines(lines: list[str], limit: int = LLM_CHUNK_CHARS) -> list[str]:
    """Pack transcript lines into bounded chunks while preserving every word."""
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for raw_line in lines:
        for line in _split_long_line(raw_line, limit):
            added_size = len(line) + (1 if current else 0)
            if current and current_size + added_size > limit:
                chunks.append("\n".join(current))
                current = []
                current_size = 0
                added_size = len(line)
            current.append(line)
            current_size += added_size
    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_transcript_segments(segments: list[dict]) -> list[str]:
    lines = []
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        try:
            start = max(0, int(float(segment.get("start", 0) or 0)))
        except (TypeError, ValueError, OverflowError):
            start = 0
        timestamp = f"{start // 3600:02d}:{(start // 60) % 60:02d}:{start % 60:02d}"
        segment_id = segment.get("id", index)
        speaker = segment.get("display_name") or segment.get("speaker") or "متحدث غير محدد"
        prefix = f"[S{segment_id}] [{timestamp}] [{speaker}]: "
        lines.extend(prefix + part for part in _split_long_line(text, LLM_CHUNK_CHARS - len(prefix)))
    return lines


def _summary_prompt(content: str, *, stage: str) -> str:
    schema = (
        "أعد JSON صالحاً بالمفاتيح executive_summary وkey_topics وkey_takeaways وopen_questions. "
        "اجعل الملخص موجزاً، والموضوعات عناوين، والنتائج نقاطاً لا تتجاوز ما تدعمه المادة."
    )
    if stage == "transcript_chunk":
        instruction = (
            "حلل هذا الجزء فقط من تفريغ مساحة. لا تفترض أن الجزء يمثل المساحة كاملة، ولا تضف حقائق من خارج النص. "
            "لا تستنتج المشاعر أو النوايا أو المصداقية. حافظ على أسماء المتحدثين وأرقام المقاطع كما وردت. "
        )
    elif stage == "intermediate":
        instruction = (
            "ادمج الملاحظات الجزئية التالية في ملخص مرحلي. احتفظ بالاختلافات والأسئلة غير المحسومة، "
            "ولا تحول رأياً منقولاً إلى حقيقة. لا تستنتج المشاعر أو النوايا. "
        )
    else:
        instruction = (
            "هذه ملاحظات جزئية تغطي تفريغ المساحة كاملاً. اكتب خلاصة موحدة لا تدعي تفاصيل غير موجودة "
            "في الملاحظات، وميّز الرأي عن الحقيقة، واحتفظ بالأسئلة التي بقيت بلا جواب. لا تستنتج المشاعر أو النوايا. "
        )
    return f"أنت محلل محايد. {instruction}{schema}\n\nالمادة:\n{content}"


def calculate_speaker_participation(segments: list[dict]) -> dict[str, dict]:
    """Calculate speaking duration and turns per speaker."""
    stats: dict[str, dict] = {}
    total_speaking_seconds = 0.0

    for seg in segments:
        speaker = seg.get("speaker") or "unknown"
        start = seg.get("start", 0.0)
        end = seg.get("end", 0.0)
        duration = max(0.0, end - start)

        total_speaking_seconds += duration

        if speaker not in stats:
            stats[speaker] = {
                "username": speaker,
                "display_name": seg.get("display_name", ""),
                "total_seconds": 0.0,
                "turns_count": 0,
            }
        stats[speaker]["total_seconds"] += duration
        stats[speaker]["turns_count"] += 1

    # Calculate percentages
    for _spk, data in stats.items():
        data["total_seconds"] = round(data["total_seconds"], 1)
        data["percentage"] = (
            round((data["total_seconds"] / total_speaking_seconds) * 100, 1)
            if total_speaking_seconds > 0
            else 0.0
        )

    return stats


def extract_questions_and_quotes(segments: list[dict]) -> list[dict]:
    """Identify questions and attach a nearby response as a reviewable lead."""
    questions = []
    ordered = sorted(segments, key=lambda item: float(item.get("start", 0) or 0))
    for index, seg in enumerate(ordered):
        text = seg.get("text", "").strip()
        speaker = seg.get("speaker", "unknown")
        # Check Arabic or English question marks
        starts_with_question = bool(re.match(r"^(?:هل|لماذا|كيف|متى|أين|اين|ماذا|من|وش|ليش|وين)\b", text))
        if "؟" in text or "?" in text or starts_with_question:
            response = next(
                (
                    candidate
                    for candidate in ordered[index + 1 : index + 5]
                    if candidate.get("speaker") not in (speaker, None, "unknown")
                    and float(candidate.get("start", 0) or 0)
                    - float(seg.get("end", seg.get("start", 0)) or 0)
                    <= 90
                ),
                None,
            )
            questions.append(
                {
                    "speaker": speaker,
                    "question": text,
                    "start": seg.get("start", 0.0),
                    "timestamp_str": f"{int(seg.get('start', 0.0) // 60):02d}:{int(seg.get('start', 0.0) % 60):02d}",
                    "possible_response": response.get("text", "") if response else "",
                    "response_speaker": response.get("speaker", "") if response else "",
                    "response_start": response.get("start") if response else None,
                    "response_is_candidate": bool(response),
                }
            )
    return questions[:15]


def extract_algorithmic_topics(full_text: str) -> list[str]:
    """Heuristic / TF-IDF style keyword and topic extraction."""
    # Common Arabic & English stopwords
    stopwords = {
        "في",
        "من",
        "على",
        "إلى",
        "عن",
        "مع",
        "هذا",
        "هذه",
        "تم",
        "كان",
        "أن",
        "إن",
        "لا",
        "ما",
        "هو",
        "هي",
        "التي",
        "الذي",
        "كل",
        "أو",
        "ثم",
        "بعد",
        "قبل",
        "عند",
        "وقد",
        "فقد",
        "حيث",
        "كما",
        "the",
        "and",
        "to",
        "of",
        "a",
        "in",
        "is",
        "that",
        "for",
        "it",
        "with",
        "as",
        "on",
        "was",
        "are",
    }
    words = re.findall(r"[\w\u0600-\u06FF]{4,}", full_text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in stopwords and not w.isdigit():
            freq[w] = freq.get(w, 0) + 1

    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [w for w, c in sorted_words[:10]]


def _normalize_token(token: str) -> str:
    token = unicodedata.normalize("NFKC", token.lower())
    token = re.sub(r"[\u064b-\u065f\u0670ـ]", "", token)
    return token.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}))


def extract_topic_evidence(segments: list[dict], limit: int = 8) -> list[dict]:
    """Return frequent terms with actual transcript excerpts and time anchors."""
    stopwords = {
        "في",
        "من",
        "على",
        "الى",
        "عن",
        "مع",
        "هذا",
        "هذه",
        "ذلك",
        "تلك",
        "تم",
        "كان",
        "كانت",
        "ان",
        "إن",
        "لا",
        "ما",
        "هو",
        "هي",
        "التي",
        "الذي",
        "كل",
        "او",
        "ثم",
        "بعد",
        "قبل",
        "عند",
        "وقد",
        "فقد",
        "حيث",
        "كما",
        "لكن",
        "بين",
        "حتى",
        "اذا",
        "ايضا",
        "يعني",
        "والله",
        "طيب",
        "بس",
        "انه",
        "انها",
        "احنا",
        "انا",
        "انت",
        "انتم",
        "هم",
        "نحن",
        "there",
        "their",
        "about",
        "from",
        "with",
        "that",
        "this",
        "have",
        "were",
        "what",
        "when",
        "your",
        "they",
        "them",
        "then",
        "than",
        "just",
        "like",
        "know",
        "would",
        "could",
        "should",
        "because",
        "really",
        "very",
        "into",
        "also",
        "will",
        "been",
        "being",
        "the",
        "and",
        "for",
        "are",
        "was",
        "you",
        "not",
        "but",
        "all",
        "can",
        "one",
        "out",
        "get",
        "has",
        "had",
        "its",
        "our",
        "who",
        "how",
        "why",
        "his",
        "her",
        "she",
        "him",
        "those",
        "these",
        "more",
        "some",
        "any",
        "now",
        "well",
        "yeah",
        "yes",
        "okay",
        "ok",
    }
    occurrences: dict[str, list[dict]] = {}
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        seen = set()
        for raw in re.findall(r"[\w\u0600-\u06FF]{3,}", text.lower()):
            token = _normalize_token(raw)
            if token in stopwords or token.isdigit() or token in seen:
                continue
            seen.add(token)
            occurrences.setdefault(token, []).append(
                {
                    "start": float(seg.get("start", 0) or 0),
                    "timestamp": f"{int(float(seg.get('start', 0) or 0) // 60):02d}:{int(float(seg.get('start', 0) or 0) % 60):02d}",
                    "speaker": seg.get("speaker") or "unknown",
                    "excerpt": text[:260],
                }
            )
    ranked = sorted(occurrences.items(), key=lambda item: (len(item[1]), -item[1][0]["start"]), reverse=True)
    return [
        {"topic": token, "mentions": len(evidence), "evidence": evidence[:3]}
        for token, evidence in ranked[:limit]
        if len(evidence) >= 2
    ]


def build_deep_insights(
    segments: list[dict], speaker_stats: dict[str, dict], questions: list[dict], topic_evidence: list[dict]
) -> dict:
    """Produce explainable, transcript-grounded indicators; avoid sentiment claims."""
    total_seconds = sum(max(0.0, float(item.get("total_seconds", 0) or 0)) for item in speaker_stats.values())
    shares = (
        [float(item.get("total_seconds", 0) or 0) / total_seconds for item in speaker_stats.values()]
        if total_seconds
        else []
    )
    hhi = sum(share * share for share in shares)
    unknown_share = (
        (speaker_stats.get("unknown", {}).get("total_seconds", 0) / total_seconds) if total_seconds else 0
    )
    dominant = max(speaker_stats.values(), key=lambda item: item.get("total_seconds", 0), default=None)
    participation = "لا توجد بيانات كافية"
    if shares:
        participation = (
            "مشاركة موزعة نسبياً"
            if hhi < 0.40
            else ("مشاركة مركزة لدى متحدث واحد" if hhi > 0.65 else "مشاركة متفاوتة بين المتحدثين")
        )

    moments = []
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        words = text.split()
        if len(words) < 9:
            continue
        start = float(seg.get("start", 0) or 0)
        moments.append(
            {
                "start": start,
                "timestamp": f"{int(start // 60):02d}:{int(start % 60):02d}",
                "speaker": seg.get("speaker") or "unknown",
                "excerpt": text[:300],
                "word_count": len(words),
            }
        )
    moments.sort(key=lambda item: item["word_count"], reverse=True)

    return {
        "participation": {
            "label": participation,
            "speaker_count": len([name for name in speaker_stats if name != "unknown"]),
            "dominant_speaker": dominant.get("username") if dominant else None,
            "dominant_share_percent": round(max(shares) * 100, 1) if shares else 0,
            "concentration_index": round(hhi, 3),
            "unknown_speaker_share_percent": round(unknown_share * 100, 1),
            "method": "مؤشر وصفي محسوب من مقاطع التفريغ وربط المتحدثين؛ لا يقيس جودة الحوار.",
        },
        "topic_evidence": topic_evidence,
        "key_moments": moments[:8],
        "question_response_leads": [q for q in questions if q.get("response_is_candidate")][:10],
        "limitations": [
            "قد يخطئ التفريغ الآلي في الكلمات والأسماء، خصوصاً مع اللهجات أو تداخل الأصوات.",
            "الرد المحتمل اقتراح مبني على قرب المقطع واختلاف المتحدث، وليس إثباتاً أنه جواب مباشر.",
            "مؤشر التوازن يعتمد على دقة التقاط المتحدثين وتوقيتاتهم.",
        ],
    }


async def summarize_with_llm(
    text: str,
    cfg: Settings,
    segments: list[dict] | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> dict | None:
    """Summarize the complete transcript through bounded hierarchical LLM calls."""
    if not cfg.local_llm_base_url:
        return None

    url = f"{cfg.local_llm_base_url.rstrip('/')}/v1/chat/completions"
    model = cfg.local_llm_model or "llama3"

    transcript_lines = _format_transcript_segments(segments) if segments else text.splitlines()
    transcript_for_model = "\n".join(transcript_lines)
    chunks = _chunk_lines(transcript_lines)
    if not chunks:
        return None

    headers = {"Content-Type": "application/json"}
    if cfg.local_llm_api_key:
        headers["Authorization"] = f"Bearer {cfg.local_llm_api_key.get_secret_value()}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:

            async def request_summary(content: str, stage: str) -> dict | None:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": _summary_prompt(content, stage=stage)}],
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                }
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    log.warning("LLM API error", status=resp.status_code, body=resp.text[:200])
                    return None
                data = resp.json()
                result = json.loads(data["choices"][0]["message"]["content"])
                if not isinstance(result, dict):
                    return None
                return result

            notes: list[str] = []
            for index, chunk in enumerate(chunks):
                result = await request_summary(chunk, "transcript_chunk")
                if result is None:
                    return None
                notes.append(json.dumps(result, ensure_ascii=False))
                if progress_callback:
                    progress_callback(0.8 * (index + 1) / len(chunks))

            # Reduce groups repeatedly so no request exceeds the configured input bound.
            initial_note_count = len(notes)
            for level in range(8):
                note_chunks = _chunk_lines(notes, LLM_REDUCTION_CHARS)
                if len(note_chunks) == 1:
                    final = await request_summary(note_chunks[0], "final")
                    if final is None:
                        return None
                    if progress_callback:
                        progress_callback(1.0)
                    final["_analysis"] = {
                        "strategy": "hierarchical_local_llm",
                        "chunk_count": len(chunks),
                        "source_character_count": len(transcript_for_model),
                    }
                    return final

                if len(note_chunks) >= len(notes):
                    log.warning("LLM summaries did not shrink during hierarchical reduction")
                    return None

                reduced: list[str] = []
                for index, note_chunk in enumerate(note_chunks):
                    result = await request_summary(note_chunk, "intermediate")
                    if result is None:
                        return None
                    reduced.append(json.dumps(result, ensure_ascii=False))
                    if progress_callback:
                        shrink = 1 - len(note_chunks) / max(1, initial_note_count)
                        progress_callback(
                            min(0.98, 0.8 + 0.18 * max(shrink, (level + (index + 1) / len(note_chunks)) / 8))
                        )
                notes = reduced
            log.warning("LLM hierarchical reduction exceeded its maximum depth")
    except Exception as e:
        log.warning("Failed to call local LLM", error=str(e))

    return None


async def analyze_space(
    space_id: str,
    cfg: Settings | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> dict:
    """
    Run full Phase 6 analysis on a transcribed space.
    Saves summary.json and updates SQLite topics.
    """
    cfg = cfg or get_settings()

    # Ensure DB is ready
    from xsa.db import init_db

    try:
        from xsa.db.database import _SessionLocal

        if _SessionLocal is None:
            init_db(cfg.db_path)
    except Exception:
        init_db(cfg.db_path)

    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        started_at = (
            space.started_at if space and space.started_at else datetime.now(UTC).replace(tzinfo=None)
        )

    paths = SpacePaths.for_space(cfg.data_dir, space_id, started_at)

    if not paths.transcript_json.exists():
        raise FileNotFoundError(f"transcript.json not found for space {space_id}")

    transcript_data = json.loads(paths.transcript_json.read_text(encoding="utf-8"))
    segments = transcript_data.get("segments", [])
    full_text = transcript_data.get("full_text", "")

    # 1. Speaker Participation Stats
    speaker_stats = calculate_speaker_participation(segments)

    # 2. Extract Questions
    questions = extract_questions_and_quotes(segments)
    topic_evidence = extract_topic_evidence(segments)
    deep_insights = build_deep_insights(segments, speaker_stats, questions, topic_evidence)

    # 3. LLM or Heuristic Summary
    llm_result = await summarize_with_llm(
        full_text, cfg, segments=segments, progress_callback=progress_callback
    )

    if llm_result:
        summary_text = llm_result.get("executive_summary", "")
        key_topics = llm_result.get("key_topics", [])
        takeaways = llm_result.get("key_takeaways", [])
    else:
        # High quality extractive summary fallback
        key_topics = [item["topic"] for item in topic_evidence] or extract_algorithmic_topics(full_text)
        takeaways = [q["question"] for q in questions[:5]]
        # Use first 3 significant segments as summary
        significant_sentences = [s["text"] for s in segments if len(s["text"].split()) > 6][:4]
        summary_text = (
            " ".join(significant_sentences)
            if significant_sentences
            else "لا توجد مقاطع كلام في التفريغ؛ راجع جودة الصوت والتسجيل قبل اعتماد التحليل."
        )

    if llm_result:
        # Keep the dashboard's topic contract stable even if a model returns objects.
        key_topics = [
            str(item.get("topic") or item.get("name") or "") if isinstance(item, dict) else str(item)
            for item in (key_topics or [])
        ]
        key_topics = [item for item in key_topics if item]

    result = {
        "space_id": space_id,
        "executive_summary": summary_text,
        "key_topics": key_topics,
        "key_takeaways": takeaways,
        "questions": questions,
        "speaker_participation": speaker_stats,
        "deep_insights": deep_insights,
        "open_questions": (llm_result or {}).get("open_questions", []),
        "analysis_method": "local_transcript_heuristics+llm" if llm_result else "local_transcript_heuristics",
        "transcript_segment_count": len(segments),
        "transcript_language": transcript_data.get("language") if segments else "unknown",
        "analyzed_at": datetime.now(UTC).isoformat(),
    }

    # Save summary.json
    paths.summary_json.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Save topics in DB
    with get_session() as db:
        space = db.query(Space).filter_by(space_id=space_id).first()
        if space:
            db.query(Topic).filter_by(space_id=space.id).delete()
            for topic_label in key_topics:
                db_topic = Topic(
                    space_id=space.id,
                    label=str(topic_label),
                    source="llm" if llm_result else "heuristic",
                )
                db.add(db_topic)
            space.state = "analyzed"
            db.commit()

    log.info("Analysis completed successfully", space_id=space_id, topics_count=len(key_topics))
    return result
