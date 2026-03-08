#!/usr/bin/env python3
"""
API enhancement module — send Markdown to LLM for cleanup or rewrite via OpenRouter.
"""

import os
import re

from rich.console import Console

console = Console()

# ── Models ───────────────────────────────────────────────────────

MODELS = {
    "1": {"name": "gpt-4o-mini",           "id": "openai/gpt-4o-mini"},
    "2": {"name": "deepseek-v3.2",         "id": "deepseek/deepseek-chat-v3-0324"},
    "3": {"name": "qwen2.5-72b-instruct",  "id": "qwen/qwen-2.5-72b-instruct"},
}

# ── Prompts ──────────────────────────────────────────────────────

PROMPT_B = """\
You are a Markdown formatting assistant. Clean up the following lecture note Markdown:

Rules:
- Fix any formatting issues (broken tables, misaligned lists, inconsistent spacing)
- Ensure code blocks have correct language tags (```cpp for C++, ```makefile for Makefile)
- Standardize heading levels: # for slide titles, ## for subtitles only
- Keep ALL content exactly as-is — do not add, remove, or rephrase any text
- Keep image references as-is
- Output ONLY the cleaned Markdown, no explanations

Markdown to clean:
"""

PROMPT_C = """\
You are an expert at creating clear, well-organized study notes from lecture slides.
Rewrite the following lecture note Markdown into a polished study guide:

Rules:
- Reorganize content for logical flow (group related topics)
- Use clear heading hierarchy: # for major topics, ## for subtopics, ### for details
- Preserve ALL technical content, code examples, and formulas accurately
- Improve clarity: add brief explanations where slides are terse
- Format code blocks with correct language tags (```cpp for C++, ```makefile for Makefile)
- Use tables, bullet points, and bold for key concepts
- Keep image references as-is
- Output ONLY the rewritten Markdown, no explanations

Lecture notes to rewrite:
"""

# ── Helpers ──────────────────────────────────────────────────────

def empty_usage() -> dict:
    """Return a zeroed token usage dict."""
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ── API Call ─────────────────────────────────────────────────────

def call_api(text: str, mode: str, model_id: str) -> tuple[str, dict]:
    """Send markdown to OpenRouter API. Returns (result_text, usage_dict)."""
    from openai import OpenAI

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    )

    prompt = PROMPT_B if mode == "B" else PROMPT_C

    response = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.1 if mode == "B" else 0.3,
    )

    usage = empty_usage()
    if response.usage:
        usage["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0) or 0
        usage["completion_tokens"] = getattr(response.usage, "completion_tokens", 0) or 0
        usage["total_tokens"] = getattr(response.usage, "total_tokens", 0) or 0

    return response.choices[0].message.content, usage


def call_api_chunked(text: str, mode: str, model_id: str, chunk_limit: int = 60000) -> tuple[str, dict]:
    """For large files, split by # headings and process each chunk. Returns (result, total_usage)."""
    if len(text) <= chunk_limit:
        return call_api(text, mode, model_id)

    sections = re.split(r'(?=^# )', text, flags=re.MULTILINE)
    sections = [s for s in sections if s.strip()]

    chunks = []
    current = ""
    for section in sections:
        if len(current) + len(section) > chunk_limit and current:
            chunks.append(current)
            current = section
        else:
            current += section
    if current:
        chunks.append(current)

    console.print(f"    [dim]split into {len(chunks)} chunks for API[/dim]")

    results = []
    total_usage = empty_usage()
    for i, chunk in enumerate(chunks):
        console.print(f"    [dim]chunk {i+1}/{len(chunks)} ({len(chunk)//1000}K chars)...[/dim]")
        result, usage = call_api(chunk, mode, model_id)
        results.append(result)
        for k in total_usage:
            total_usage[k] += usage[k]

    return "\n\n".join(results), total_usage


def enhance_file(md_path, mode: str, model_id: str) -> dict:
    """Read a markdown file, enhance via API, write back. Returns usage dict."""
    from pathlib import Path
    md_path = Path(md_path)
    text = md_path.read_text(encoding="utf-8")
    est_tokens = max(1, len(text) // 4)

    console.print(f"  ├ [blue]API:[/blue]     calling [cyan]{model_id.split('/')[-1]}[/cyan] "
                  f"(~{est_tokens:,} tokens est.)...")

    text, usage = call_api_chunked(text, mode, model_id)

    console.print(f"  ├ [blue]API:[/blue]     [yellow]{usage['prompt_tokens']:,}[/yellow] prompt + "
                  f"[yellow]{usage['completion_tokens']:,}[/yellow] completion = "
                  f"[yellow bold]{usage['total_tokens']:,}[/yellow bold] tokens")

    md_path.write_text(text, encoding="utf-8")
    return usage
