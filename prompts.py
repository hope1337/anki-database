#!/usr/bin/env python3
"""
prompts.py

File-backed prompt templates for TEXT enrichment.

Text enrichment prompts live under config/prompts/ so they can be edited
without changing Python. Supported placeholders:

    text_system.txt:
        {language_name}         learner language name from the active profile
        {profile_instruction}   profile llm.instruction text
        {max_sayings}           notable-sayings cap from profile features
        {target_char}           the Hanzi being enriched

    text_user.txt:
        {facts_json}            SOURCE_FACTS JSON (Unicode ground truth)

Rendering is strict: every placeholder above must be present in its file
(config validation checks this), and rendering fails clearly on unknown
placeholders instead of silently emitting them.
"""

from __future__ import annotations

import hashlib
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_SYSTEM_FILE = str(ROOT / "config" / "prompts" / "text_system.txt")
DEFAULT_USER_FILE = str(ROOT / "config" / "prompts" / "text_user.txt")

REQUIRED_SYSTEM_PLACEHOLDERS = (
    "{language_name}",
    "{profile_instruction}",
    "{max_sayings}",
    "{target_char}",
)
REQUIRED_USER_PLACEHOLDERS = ("{facts_json}",)


class PromptError(RuntimeError):
    """A prompt file is missing, unreadable, or fails placeholder checks."""


def _resolve(path_str: str) -> Path:
    path = Path(path_str)
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_text(path_str: str) -> str:
    """Read a prompt file as text. Raises PromptError when missing."""
    path = _resolve(path_str)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(
            f"prompt file not found/unreadable: {path_str} "
            f"(resolved: {path}): {exc}") from exc


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_file_hash(path_str: str) -> str:
    """SHA256 of a prompt file's raw content (for cache identity)."""
    return sha256_text(load_text(path_str))


def check_placeholders(template: str, required: tuple[str, ...],
                        *, name: str) -> None:
    missing = [p for p in required if p not in template]
    if missing:
        raise PromptError(
            f"prompt file {name} lacks required placeholders: "
            f"{', '.join(missing)}")


def validate_text_prompts(system_file: str, user_file: str) -> None:
    """Validate that prompt files exist and carry required placeholders."""
    system = load_text(system_file)
    user = load_text(user_file)
    check_placeholders(system, REQUIRED_SYSTEM_PLACEHOLDERS,
                       name=system_file)
    check_placeholders(user, REQUIRED_USER_PLACEHOLDERS, name=user_file)


def _strict_render(template: str, values: dict[str, str]) -> str:
    """Render {placeholders} strictly: unknown fields raise PromptError."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise PromptError(f"prompt template is malformed: {exc}") from exc
    for _, field_name, _, _ in parsed:
        if field_name is None:
            continue
        # Allow literal {{ }} escapes (field_name None covers them already);
        # any other field must be a known value.
        if field_name not in values:
            raise PromptError(
                f"prompt template uses unknown placeholder "
                f"{{{field_name}}}; known: {sorted(values)}")
    try:
        return template.format(**values)
    except (KeyError, ValueError) as exc:
        raise PromptError(
            f"prompt template render failed: {exc}") from exc


def render_system(system_file: str, *, language_name: str,
                 profile_instruction: str, max_sayings: int,
                 target_char: str) -> str:
    template = load_text(system_file)
    check_placeholders(template, REQUIRED_SYSTEM_PLACEHOLDERS,
                       name=system_file)
    return _strict_render(template, {
        "language_name": language_name,
        "profile_instruction": profile_instruction,
        "max_sayings": str(max_sayings),
        "target_char": target_char,
    }).strip()


def render_user(user_file: str, *, facts_json: str) -> str:
    template = load_text(user_file)
    check_placeholders(template, REQUIRED_USER_PLACEHOLDERS,
                       name=user_file)
    return _strict_render(template, {"facts_json": facts_json}).strip()
