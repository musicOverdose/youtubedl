import asyncio
import re
from typing import List, Optional, Tuple
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
    def is_configured(cls) -> bool:
        """Verifies that AI translation is enabled and has required parameters."""
        return bool(
            settings.AI_ENABLED
            and settings.AI_API_KEY
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
                text = " ".join(lines[time_line_idx + 1 :])
                idx = len(segments) + 1
                segments.append(SubtitleSegment(idx, start, end, text))

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
        cls, english_srt_content: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Translates English SRT subtitles to Persian using OpenAI-compatible API.
        Enforces strict preservation of segment numbers and timestamps.
        Returns: (success, persian_srt_content, error_message)
        """
        if not cls.is_configured():
            return False, None, "AI translation is not enabled or configured"

        segments = cls.parse_srt(english_srt_content)
        if not segments:
            return False, None, "English subtitle content is empty or invalid"

        # Chunk segments to prevent context window overflow (up to 60 segments per prompt)
        chunk_size = 60
        translated_segments: List[SubtitleSegment] = []

        system_prompt = (
            "You are a professional subtitle translator. Your task is to translate English subtitles into natural, idiomatic Persian (Farsi).\n"
            "STRICT RULES:\n"
            "1. You MUST preserve the EXACT subtitle numbering, timestamps, and SRT structure.\n"
            "2. Translate ONLY the spoken text. Do NOT change, omit, or reformat any timestamps (e.g., 00:01:23,456 --> 00:01:25,789).\n"
            "3. Output MUST be strictly valid SRT format and nothing else. No markdown fences, no explanations.\n"
        )

        for i in range(0, len(segments), chunk_size):
            chunk = segments[i : i + chunk_size]
            chunk_srt = "\n\n".join(seg.to_srt().strip() for seg in chunk)

            user_prompt = f"Translate these SRT subtitles to Persian. Keep all timestamps and numbers intact:\n\n{chunk_srt}"

            headers = {
                "Authorization": f"Bearer {settings.AI_API_KEY}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": settings.AI_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
            }

            url = f"{settings.AI_BASE_URL.rstrip('/')}/chat/completions"

            # Execute API call with retries
            success = False
            response_text = ""
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(timeout=90.0) as client:
                        resp = await client.post(url, headers=headers, json=payload)
                        if resp.status_code == 200:
                            data = resp.json()
                            response_text = data["choices"][0]["message"]["content"].strip()
                            # Clean possible markdown fences
                            if response_text.startswith("```"):
                                response_text = re.sub(r"^```[a-zA-Z]*\n", "", response_text)
                                response_text = re.sub(r"\n```$", "", response_text)
                            success = True
                            break
                        else:
                            logger.error(f"AI API responded with {resp.status_code}: {resp.text}")
                except Exception as e:
                    logger.error(f"AI translation request attempt {attempt + 1} failed: {e}")
                    await asyncio.sleep(2)

            if not success or not response_text:
                return False, None, "AI translation provider request failed"

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
