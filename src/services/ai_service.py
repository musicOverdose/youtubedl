import asyncio
from pathlib import Path
import re
from typing import Any, Callable, List, Optional, Tuple
import httpx
from src.core.config import settings
from src.core.logger import setup_logger

logger = setup_logger("ai_service")

# SRT timestamp regex: 00:00:00,000 --> 00:00:00,000
SRT_TIMESTAMP_REGEX = re.compile(
    r"^(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})"
)


class SubtitleSegment:
    def __init__(self, index: int, start: str, end: str, text: str):
        self.index = index
        self.start = start.replace(".", ",")
        self.end = end.replace(".", ",")
        self.text = text.strip()

    def to_srt(self) -> str:
        return f"{self.index}\n{self.start} --> {self.end}\n{self.text}\n"


class AIService:
    @classmethod
    def get_api_key(cls) -> Optional[str]:
        """
        Resolves AI API key strictly enforcing runtime secret isolation:
        1. Check /config/runtime/ai-api-key first (authoritative runtime secret written by Web Admin).
        2. Fall back to settings.AI_API_KEY only when no runtime file exists (e.g. dev/test mode).
        Prevents stale .env keys from overriding the admin-configured runtime secret.
        """
        try:
            runtime_file = Path(getattr(settings, "RUNTIME_BOT_TOKEN_FILE", "/config/runtime/bot-token")).parent / "ai-api-key"
            if runtime_file.is_file():
                k = runtime_file.read_text(encoding="utf-8").strip()
                if k:
                    return k
        except Exception:
            pass

        return settings.AI_API_KEY or None

    @classmethod
    def is_configured(cls) -> bool:
        """Verifies that AI translation is enabled and has required parameters."""
        return bool(
            settings.AI_ENABLED
            and cls.get_api_key()
            and settings.AI_BASE_URL
            and settings.AI_MODEL
        )

    @classmethod
    def parse_srt(cls, srt_content: str) -> List[SubtitleSegment]:
        """Parses raw SRT content into structured segments."""
        blocks = re.split(r"\n\s*\n", srt_content.strip())
        segments: List[SubtitleSegment] = []

        for block in blocks:
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            if len(lines) < 2:
                continue

            # Check if line 0 is index or timestamp
            time_line_idx = 1 if lines[0].isdigit() else 0
            if time_line_idx >= len(lines):
                continue

            match = SRT_TIMESTAMP_REGEX.match(lines[time_line_idx])
            if match:
                start, end = match.group(1), match.group(2)
                raw_text = " ".join(lines[time_line_idx + 1 :])
                clean_text = re.sub(r"<[^>]+>", "", raw_text).strip()
                clean_text = re.sub(r"\s+", " ", clean_text)
                if not clean_text:
                    continue
                idx = len(segments) + 1
                segments.append(SubtitleSegment(idx, start, end, clean_text))

        return segments

    @classmethod
    def validate_srt(cls, srt_content: str) -> Tuple[bool, Optional[str]]:
        """
        Strict SRT validation:
        - Checks numbering
        - Checks timestamp syntax
        - Checks chronological ordering
        """
        segments = cls.parse_srt(srt_content)
        if not segments:
            return False, "No valid subtitle segments found"

        prev_end_secs = -1.0
        for seg in segments:
            # Check timestamps
            if not SRT_TIMESTAMP_REGEX.match(f"{seg.start} --> {seg.end}"):
                return False, f"Invalid timestamp syntax in segment {seg.index}: {seg.start} --> {seg.end}"

            start_secs = cls.timestamp_to_seconds(seg.start)
            end_secs = cls.timestamp_to_seconds(seg.end)

            if end_secs < start_secs:
                return False, f"End time before start time in segment {seg.index}"

            if start_secs < prev_end_secs - 30.0:  # Allow minor overlap but check sequence
                return False, f"Timestamps out of chronological order in segment {seg.index}"

            prev_end_secs = end_secs

        return True, None

    @staticmethod
    def timestamp_to_seconds(ts: str) -> float:
        ts = ts.replace(",", ".")
        parts = ts.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        return 0.0

    @classmethod
    async def translate_english_to_persian(
        cls,
        english_srt_content: str,
        max_chunks: Optional[int] = None,
        on_progress: Optional[Callable[[int, int, int], Any]] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Translates English SRT subtitles to Persian using OpenAI-compatible API.
        Enforces strict preservation of segment numbers and timestamps.
        Returns: (success, persian_srt_content, error_message)
        """
        if not cls.is_configured():
            return False, None, "AI translation is not enabled or configured"

        api_key = cls.get_api_key()
        if not api_key:
            return False, None, "AI translation API key is not configured"

        segments = cls.parse_srt(english_srt_content)
        if not segments:
            return False, None, "English subtitle content is empty or invalid"

        # Chunk segments to prevent timeout/context window overflow
        chunk_size = getattr(settings, "AI_CHUNK_SIZE", 10)
        translated_segments: List[SubtitleSegment] = []

        total_chunks = (len(segments) + chunk_size - 1) // chunk_size
        effective_max_chunks = max_chunks if max_chunks is not None else getattr(settings, "AI_MAX_CHUNKS", 50)

        if effective_max_chunks and effective_max_chunks > 0 and total_chunks > effective_max_chunks:
            max_cues = effective_max_chunks * chunk_size
            logger.warning(
                "Subtitle translation rejected: %d chunks (%d cues) exceeds max allowed %d chunks (%d cues)",
                total_chunks,
                len(segments),
                effective_max_chunks,
                max_cues,
            )
            return (
                False,
                None,
                f"Video subtitle size is too large for AI translation ({total_chunks} chunks / {len(segments)} cues exceeds maximum allowed limit of {effective_max_chunks} chunks / {max_cues} cues). Please download English subtitles instead.",
            )

        system_prompt = (
            "You are a professional subtitle translator. Your task is to translate English subtitles into natural, idiomatic Persian (Farsi).\n"
            "STRICT RULES:\n"
            "1. You MUST preserve the EXACT subtitle numbering, timestamps, and SRT structure.\n"
            "2. Translate ONLY the spoken text. Do NOT change, omit, or reformat any timestamps (e.g., 00:01:23,456 --> 00:01:25,789).\n"
            "3. Output MUST be strictly valid SRT format and nothing else. No markdown fences, no explanations.\n"
        )

        logger.info(
            "Starting Persian subtitle translation: %d segments across %d chunks (chunk_size=%d, max_chunks=%s)",
            len(segments),
            total_chunks,
            chunk_size,
            effective_max_chunks,
        )

        # Normalize Base URL to prevent duplicate /chat/completions
        raw_base = (settings.AI_BASE_URL or "https://api.openai.com/v1").strip().rstrip("/")
        if raw_base.endswith("/chat/completions"):
            url = raw_base
        else:
            url = f"{raw_base}/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            "User-Agent": "YtDlpBot/1.0",
        }
        model_name = (settings.AI_MODEL or "gpt-4o-mini").strip()

        timeout_secs = float(getattr(settings, "AI_TIMEOUT", 120.0))
        req_timeout = httpx.Timeout(connect=20.0, read=timeout_secs, write=20.0, pool=20.0)

        for chunk_idx, i in enumerate(range(0, len(segments), chunk_size), 1):
            chunk = segments[i : i + chunk_size]
            chunk_srt = "\n\n".join(seg.to_srt().strip() for seg in chunk)

            user_prompt = f"Translate these SRT subtitles to Persian. Keep all timestamps and numbers intact:\n\n{chunk_srt}"

            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
            }

            # Execute API call with retries and exponential backoff
            success = False
            response_text = ""
            last_error = ""
            for attempt in range(3):
                if on_progress:
                    try:
                        res = on_progress(chunk_idx, total_chunks, attempt + 1)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as cb_err:
                        logger.debug("AI translation on_progress callback error: %s", cb_err)

                try:
                    async with httpx.AsyncClient(timeout=req_timeout, follow_redirects=True) as client:
                        resp = await client.post(url, headers=headers, json=payload)
                        if resp.status_code == 200:
                            data = resp.json()
                            choices = data.get("choices") or []
                            if not choices:
                                last_error = f"API returned 200 but choices array is empty: {data}"
                                logger.error("AI API returned 200 with empty choices on chunk %d/%d: %s", chunk_idx, total_chunks, data)
                                continue
                            response_text = choices[0].get("message", {}).get("content", "").strip()
                            if not response_text:
                                last_error = "API returned empty message content"
                                logger.warning("AI API returned empty message content on chunk %d/%d", chunk_idx, total_chunks)
                                continue
                            # Clean possible markdown fences
                            if response_text.startswith("```"):
                                response_text = re.sub(r"^```[a-zA-Z]*\n", "", response_text)
                                response_text = re.sub(r"\n```$", "", response_text)
                            success = True
                            logger.info(
                                "Successfully translated chunk %d/%d (attempt %d)",
                                chunk_idx,
                                total_chunks,
                                attempt + 1,
                            )
                            break
                        else:
                            err_body = resp.text.strip()[:300]
                            last_error = f"HTTP {resp.status_code}: {err_body}"
                            logger.error(
                                "AI API responded with %s: %s (chunk %d/%d, attempt %d/3)",
                                resp.status_code,
                                err_body,
                                chunk_idx,
                                total_chunks,
                                attempt + 1,
                            )
                except httpx.ReadTimeout:
                    last_error = f"ReadTimeout (provider read timed out after {int(timeout_secs)}s on chunk {chunk_idx}/{total_chunks})"
                    logger.error(
                        "AI translation read timeout after %0.1fs on chunk %d/%d (attempt %d/3)",
                        timeout_secs,
                        chunk_idx,
                        total_chunks,
                        attempt + 1,
                    )
                except httpx.ConnectTimeout:
                    last_error = f"ConnectTimeout (failed to connect to provider within 20s on chunk {chunk_idx}/{total_chunks})"
                    logger.error(
                        "AI translation connect timeout on chunk %d/%d (attempt %d/3)",
                        chunk_idx,
                        total_chunks,
                        attempt + 1,
                    )
                except httpx.TimeoutException as te:
                    timeout_type = type(te).__name__
                    last_error = f"{timeout_type} (provider request timed out after {int(timeout_secs)}s on chunk {chunk_idx}/{total_chunks})"
                    logger.error(
                        "AI translation timeout (%s) on chunk %d/%d (attempt %d/3)",
                        timeout_type,
                        chunk_idx,
                        total_chunks,
                        attempt + 1,
                    )
                except Exception as e:
                    err_desc = str(e).strip() or "no error message"
                    last_error = f"{type(e).__name__}: {err_desc[:300]}"
                    logger.error(
                        "AI translation request failed with %s: %s (chunk %d/%d, attempt %d/3)",
                        type(e).__name__,
                        e,
                        chunk_idx,
                        total_chunks,
                        attempt + 1,
                    )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

            if not success or not response_text:
                err_msg = f"AI translation provider request failed on chunk {chunk_idx}/{total_chunks}"
                if last_error:
                    err_msg += f" ({last_error})"
                return False, None, err_msg

            # Parse translated chunk
            parsed_chunk = cls.parse_srt(response_text)
            if len(parsed_chunk) != len(chunk):
                # Fallback repair: pair original timestamps with translated texts
                logger.warning(
                    f"Segment count mismatch: expected {len(chunk)}, got {len(parsed_chunk)}. Repairing..."
                )
                repaired_chunk = []
                for idx, orig_seg in enumerate(chunk):
                    translated_text = (
                        parsed_chunk[idx].text if idx < len(parsed_chunk) else orig_seg.text
                    )
                    repaired_chunk.append(
                        SubtitleSegment(
                            orig_seg.index, orig_seg.start, orig_seg.end, translated_text
                        )
                    )
                translated_segments.extend(repaired_chunk)
            else:
                # Ensure timestamps match original perfectly
                for orig_seg, trans_seg in zip(chunk, parsed_chunk):
                    trans_seg.index = orig_seg.index
                    trans_seg.start = orig_seg.start
                    trans_seg.end = orig_seg.end
                    translated_segments.append(trans_seg)

        final_srt = "\n".join(seg.to_srt() for seg in translated_segments)
        is_valid, validation_err = cls.validate_srt(final_srt)
        if not is_valid:
            logger.error(f"SRT validation failed after translation: {validation_err}")
            return False, None, f"SRT validation error: {validation_err}"

        return True, final_srt, None
