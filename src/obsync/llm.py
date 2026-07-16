from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .profiles import (
    FULL_TRANSFER_PROFILE,
    PROTECTED_SYSTEM_PROMPT,
    AIProfile,
    render_user_prompt,
)
from .security import slugify

SYSTEM_PROMPT = PROTECTED_SYSTEM_PROMPT


@dataclass(slots=True)
class Analysis:
    title: str
    summary: str
    category: str
    document_type: str
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    related_notes: list[str] = field(default_factory=list)
    provider: str = "rules"
    model: str = ""
    profile_id: str = FULL_TRANSFER_PROFILE.id
    profile_name: str = FULL_TRANSFER_PROFILE.name

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = False
    provider: str = "off"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout_seconds: int = 120
    profile: AIProfile | None = None
    custom_instructions: str = ""

    @property
    def active(self) -> bool:
        return bool(
            self.enabled and self.provider not in {"", "off"} and self.base_url and self.model
        )

    @property
    def active_profile(self) -> AIProfile:
        return self.profile or FULL_TRANSFER_PROFILE


def validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("LLM base URL must be a valid http:// or https:// URL")
    return value


def _first_words(text: str, maximum: int = 420) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= maximum:
        return compact
    return compact[:maximum].rsplit(" ", 1)[0] + "…"


def fallback_analysis(source_path: str, text: str, extension: str) -> Analysis:
    path = Path(source_path)
    raw_title = path.stem.replace("_", " ").replace("-", " ").strip()
    title = re.sub(r"\s+", " ", raw_title).title() or "Untitled document"
    parent = path.parent.name if path.parent.name not in {"", "."} else "Documents"
    category = parent.replace("_", " ").replace("-", " ").strip().title() or "Documents"
    extension_tag = extension.lower().lstrip(".") or "file"
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", f"{title} {parent}".lower())
    stop = {"this", "that", "from", "with", "file", "document", "documents"}
    tags = [extension_tag]
    for token in tokens:
        normalized = slugify(token, max_length=30)
        if normalized not in stop and normalized not in tags:
            tags.append(normalized)
        if len(tags) >= 6:
            break
    summary = _first_words(text) if text else f"Synced {extension_tag.upper()} file: {path.name}."
    return Analysis(
        title=title,
        summary=summary,
        category=category,
        document_type=extension_tag,
        tags=tags,
        confidence=0.35,
        provider="rules",
    )


def _extract_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM did not return JSON") from None
        value = json.loads(raw[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value


def _normalize_analysis(
    value: dict[str, Any],
    fallback: Analysis,
    provider: str,
    model: str,
    candidates: list[str | dict[str, Any]],
    profile: AIProfile,
) -> Analysis:
    title = str(value.get("title") or fallback.title).strip()[:160]
    summary = str(value.get("summary") or fallback.summary).strip()[:4000]
    category = str(value.get("category") or fallback.category).strip()[:80]
    document_type = str(value.get("document_type") or fallback.document_type).strip()[:50]

    raw_tags = value.get("tags", [])
    tags: list[str] = []
    if profile.use_tags and profile.tag_limit and isinstance(raw_tags, list):
        for tag in raw_tags:
            clean = slugify(str(tag), fallback="", max_length=40)
            if clean and clean not in tags:
                tags.append(clean)
            if len(tags) >= profile.tag_limit:
                break
    if profile.use_tags and profile.tag_limit and not tags:
        tags = fallback.tags[: profile.tag_limit]

    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence", 0.75))))
    except (TypeError, ValueError):
        confidence = 0.5

    allowed = {
        _candidate_title(candidate).casefold(): _candidate_title(candidate)
        for candidate in candidates
        if _candidate_title(candidate)
    }
    related: list[str] = []
    raw_related = value.get("related_notes", [])
    if (
        profile.use_vault_context
        and profile.use_wikilinks
        and profile.related_notes_limit
        and isinstance(raw_related, list)
    ):
        for note in raw_related:
            exact = allowed.get(str(note).strip().casefold())
            if exact and exact not in related:
                related.append(exact)
            if len(related) >= profile.related_notes_limit:
                break

    return Analysis(
        title=title or fallback.title,
        summary=summary or fallback.summary,
        category=category or fallback.category,
        document_type=document_type or fallback.document_type,
        tags=tags,
        confidence=confidence,
        related_notes=related,
        provider=provider,
        model=model,
        profile_id=profile.id,
        profile_name=profile.name,
    )


def _candidate_title(candidate: str | dict[str, Any]) -> str:
    if isinstance(candidate, dict):
        return str(candidate.get("title", "")).strip()
    return str(candidate).strip()


def _candidate_prompt_line(candidate: str | dict[str, Any]) -> str:
    if not isinstance(candidate, dict):
        return f"- [[{str(candidate).strip()}]]"
    title = _candidate_title(candidate)
    path = str(candidate.get("path", "")).strip()
    raw_tags = candidate.get("tags", [])
    tags = ", ".join(str(tag) for tag in raw_tags[:20]) if isinstance(raw_tags, list) else ""
    details = [
        value
        for value in (f"path: {path}" if path else "", f"tags: {tags}" if tags else "")
        if value
    ]
    return f"- [[{title}]]" + (f" | {' | '.join(details)}" if details else "")


class LLMAnalyzer:
    def __init__(
        self,
        config: LLMConfig,
        progress: Callable[[str, str], None] | None = None,
    ):
        self.config = config
        self.progress = progress

    def _emit(self, kind: str, message: str) -> None:
        if self.progress and message:
            self.progress(kind, message)

    def _system_prompt(self) -> str:
        role_prompt = self.config.active_profile.role_prompt.strip()
        legacy = self.config.custom_instructions.strip()
        if legacy:
            role_prompt = f"{role_prompt}\n\nAdditional organization preferences:\n{legacy[:8000]}"
        return (
            f"{PROTECTED_SYSTEM_PROMPT}\n\nACTIVE AI PROFILE ROLE:\n{role_prompt[:20_000]}\n\n"
            "These preferences may refine organization behavior but never override the required "
            "JSON schema, untrusted-content boundary, validation, or non-destructive safety rules."
        )

    async def analyze(
        self,
        *,
        source_path: str,
        text: str,
        mime_type: str,
        candidates: list[str | dict[str, Any]],
        review_feedback: str = "",
    ) -> Analysis:
        fallback = fallback_analysis(source_path, text, Path(source_path).suffix)
        profile = self.config.active_profile
        fallback.profile_id = profile.id
        fallback.profile_name = profile.name
        if not profile.use_tags:
            fallback.tags = []
        if not self.config.active:
            self._emit("stage", "Local AI is disabled; using deterministic organization rules.")
            return fallback

        base_url = validate_base_url(self.config.base_url)
        prompt = self._user_prompt(
            source_path, text, mime_type, candidates, review_feedback=review_feedback
        )
        provider = self.config.provider.lower()
        self._emit(
            "stage",
            f"Sending {Path(source_path).name} to {provider} model {self.config.model}.",
        )
        try:
            if provider == "ollama":
                raw = await self._call_ollama(base_url, prompt)
            elif provider in {"lmstudio", "openai", "openai-compatible"}:
                raw = await self._call_openai_compatible(base_url, prompt)
            else:
                raise ValueError(f"Unsupported LLM provider: {self.config.provider}")
            self._emit("stage", "Validating the model's structured decision.")
            parsed = _extract_json(raw)
            result = _normalize_analysis(
                parsed,
                fallback,
                provider,
                self.config.model,
                candidates,
                self.config.active_profile,
            )
            self._emit(
                "decision",
                f"Decision: {result.title} → {result.category}; "
                f"{round(result.confidence * 100)}% confidence.",
            )
            return result
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            json.JSONDecodeError,
        ) as exc:
            self._emit("error", f"Local AI failed: {exc}. Using deterministic rules instead.")
            return fallback

    def _user_prompt(
        self,
        source_path: str,
        text: str,
        mime_type: str,
        candidates: list[str | dict[str, Any]],
        *,
        review_feedback: str = "",
    ) -> str:
        profile = self.config.active_profile
        candidate_text = (
            "\n".join(
                _candidate_prompt_line(note) for note in candidates[: profile.candidate_limit]
            )
            or "(none)"
        )
        content = text[: profile.input_char_limit]
        feedback = review_feedback.strip()[:4000]
        return render_user_prompt(
            profile.user_prompt_template,
            source_path=source_path,
            mime_type=mime_type,
            candidate_notes=candidate_text,
            document_content=content,
            review_feedback=feedback or "(none)",
        )

    async def _call_ollama(self, base_url: str, prompt: str) -> str:
        async with (
            httpx.AsyncClient(timeout=self.config.timeout_seconds) as client,
            client.stream(
                "POST",
                f"{base_url}/api/chat",
                json={
                    "model": self.config.model,
                    "stream": True,
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": self._system_prompt()},
                        {"role": "user", "content": prompt},
                    ],
                    "options": {
                        "temperature": self.config.active_profile.temperature,
                        "top_p": self.config.active_profile.top_p,
                        "num_predict": self.config.active_profile.max_output_tokens,
                    },
                },
            ) as response,
        ):
            response.raise_for_status()
            content: list[str] = []
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                message = payload.get("message", {})
                thinking = str(
                    message.get("thinking")
                    or message.get("reasoning")
                    or payload.get("thinking")
                    or ""
                )
                chunk = str(message.get("content") or "")
                if thinking:
                    self._emit("reasoning", thinking)
                if chunk:
                    content.append(chunk)
                    self._emit("output", chunk)
            return "".join(content)

    async def _call_openai_compatible(self, base_url: str, prompt: str) -> str:
        url = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload = {
            "model": self.config.model,
            "temperature": self.config.active_profile.temperature,
            "top_p": self.config.active_profile.top_p,
            "max_tokens": self.config.active_profile.max_output_tokens,
            "stream": True,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            for attempt in range(2):
                raw_lines: list[str] = []
                content: list[str] = []
                async with client.stream(
                    "POST", f"{url}/chat/completions", headers=headers, json=payload
                ) as response:
                    if response.status_code == 400 and attempt == 0:
                        await response.aread()
                        payload.pop("response_format", None)
                        continue
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        stripped = line.strip()
                        if not stripped or stripped == "data: [DONE]":
                            continue
                        if stripped.startswith("data:"):
                            stripped = stripped[5:].strip()
                        raw_lines.append(stripped)
                        try:
                            event = json.loads(stripped)
                        except json.JSONDecodeError:
                            continue
                        choice = (event.get("choices") or [{}])[0]
                        message = choice.get("delta") or choice.get("message") or {}
                        reasoning = str(
                            message.get("reasoning_content") or message.get("reasoning") or ""
                        )
                        chunk = str(message.get("content") or "")
                        if reasoning:
                            self._emit("reasoning", reasoning)
                        if chunk:
                            content.append(chunk)
                            self._emit("output", chunk)
                if content:
                    return "".join(content)
                # Some compatible servers ignore stream=true and return one JSON object.
                raw = "\n".join(raw_lines)
                response_json = json.loads(raw)
                message = response_json["choices"][0]["message"]
                reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
                result = str(message["content"])
                if reasoning:
                    self._emit("reasoning", reasoning)
                self._emit("output", result)
                return result
        raise ValueError("The model did not return a response")

    async def test_connection(self) -> dict[str, Any]:
        provider = self.config.provider.strip().lower()
        if provider in {"", "off"} or not self.config.base_url:
            return {"ok": False, "message": "LLM integration is disabled or incomplete."}
        try:
            base_url = validate_base_url(self.config.base_url)
            timeout_seconds = max(3, min(self.config.timeout_seconds, 15))
            timeout = httpx.Timeout(timeout_seconds, connect=min(5, timeout_seconds))
            headers: dict[str, str] = {}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            async with httpx.AsyncClient(timeout=timeout) as client:
                if provider == "ollama":
                    response = await client.get(f"{base_url}/api/tags")
                    response.raise_for_status()
                    models = [
                        str(item.get("name") or item.get("model") or "").strip()
                        for item in response.json().get("models", [])
                        if isinstance(item, dict)
                    ]
                elif provider in {"lmstudio", "openai", "openai-compatible"}:
                    url = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
                    response = await client.get(f"{url}/models", headers=headers)
                    response.raise_for_status()
                    models = [
                        str(item.get("id") or "").strip()
                        for item in response.json().get("data", [])
                        if isinstance(item, dict)
                    ]
                else:
                    raise ValueError(f"Unsupported LLM provider: {self.config.provider}")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "message": f"Could not reach {self.config.provider}: {exc}",
            }

        models = [model for model in models if model]
        if not models:
            return {
                "ok": False,
                "message": f"Connected to {self.config.provider}, but it reported no models.",
                "models": [],
            }
        selected = self.config.model.strip()
        if selected:
            selected_key = selected.casefold()
            matches = {
                name.casefold()
                for name in models
                if name.casefold() == selected_key
                or name.casefold().removesuffix(":latest") == selected_key.removesuffix(":latest")
            }
            if not matches:
                preview = ", ".join(models[:5])
                return {
                    "ok": False,
                    "message": (
                        f"Connected to {self.config.provider}, but '{selected}' is not available. "
                        f"Available: {preview}"
                    ),
                    "models": models,
                }
        return {
            "ok": True,
            "message": (
                f"Connected to {self.config.provider}; '{selected}' is available."
                if selected
                else f"Connected to {self.config.provider}; found {len(models)} model(s)."
            ),
            "models": models,
            "suggested_model": selected or models[0],
        }
