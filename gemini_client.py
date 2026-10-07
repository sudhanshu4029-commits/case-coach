"""Thin wrapper around the Gemini API.

Handles the failure modes the report asks about:
- invalid key            -> clear message, no retry
- model retired/renamed  -> falls back to the next model in the list
- rate limit / overload  -> waits and retries
- empty or broken JSON   -> raises BadOutput so the app can recover gracefully
"""

import json
import re
import time

from google import genai
from google.genai import types

# Tried in order. The first one that works is remembered for the session.
DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash"]


class GeminiError(Exception):
    """The API could not be reached or refused the request."""

    def __init__(self, user_message, detail=""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


class BadOutput(Exception):
    """The model answered, but not with usable JSON."""


def parse_json(text):
    if not text:
        raise BadOutput("empty response")
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise BadOutput(f"could not parse: {text[:200]}")


class GeminiClient:
    def __init__(self, api_key, preferred_model=None):
        self.client = genai.Client(api_key=api_key)
        models = [preferred_model] if preferred_model else []
        self.models = models + [m for m in DEFAULT_MODELS if m not in models]
        self.active_model = None

    def generate_json(self, system, prompt, temperature=0.7):
        return parse_json(self._generate(system, prompt, temperature))

    def _generate(self, system, prompt, temperature):
        order = ([self.active_model] if self.active_model else []) + [
            m for m in self.models if m != self.active_model
        ]
        errors = []
        for model in order:
            for attempt in range(3):
                try:
                    resp = self.client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system,
                            temperature=temperature,
                            response_mime_type="application/json",
                        ),
                    )
                    text = resp.text
                    if not text:
                        raise BadOutput("empty response (possibly blocked by safety filters)")
                    self.active_model = model
                    return text
                except BadOutput as e:
                    errors.append(f"{model}: {e}")
                    time.sleep(1)
                    continue
                except Exception as e:  # SDK raises several error classes; inspect the message
                    msg = str(e)
                    low = msg.lower()
                    errors.append(f"{model}: {msg[:200]}")
                    if "api key" in low or "api_key" in low or "unauthenticated" in low or "permission_denied" in low:
                        raise GeminiError(
                            "Gemini rejected the API key. Check GEMINI_API_KEY in your Streamlit secrets.",
                            msg,
                        )
                    if "404" in msg or "not found" in low or "not supported" in low:
                        break  # this model name is unavailable; try the next one
                    if "429" in msg or "resource_exhausted" in low or "quota" in low:
                        time.sleep(3 * (attempt + 1))
                        continue
                    time.sleep(1.5 * (attempt + 1))  # 5xx, timeouts, network blips
        raise GeminiError(
            "Couldn't reach Gemini after several tries (likely the free-tier rate limit). "
            "Wait about 30 seconds and send your answer again.",
            "\n".join(errors),
        )
