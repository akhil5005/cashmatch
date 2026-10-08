"""Where the text goes to become JSON.

Three providers behind one protocol, which is what makes the LLM optional
rather than load-bearing:

``MockProvider``
    The default. Produces well-formed JSON deterministically, with no
    network and no API key, so the whole test suite and a cold clone of the
    repo both work. It is **not a model** -- it is a stand-in that exercises
    the plumbing around one: JSON parsing, schema validation, the retry, the
    arithmetic cross-check.

``GeminiProvider``
    The real thing. Schema-constrained JSON output via google-genai.

``ScriptedProvider``
    Returns canned responses in order. Used by tests to drive the malformed
    output, retry and give-up paths deliberately rather than hoping a real
    model misbehaves on cue.
"""

from __future__ import annotations

import json
from typing import Protocol

from cashmatch.extraction.regex_extractor import extract_with_regex
from cashmatch.extraction.schema import RemittanceDraft
from cashmatch.matching.config import ReferenceConfig
from cashmatch.money import paise_to_rupees


class ProviderError(RuntimeError):
    """The provider could not be reached or refused the request.

    Distinct from a malformed *answer*: a network failure is retried
    differently from a model that returned unparseable JSON.
    """


class ExtractionProvider(Protocol):
    """Anything that can turn a prompt into a JSON string."""

    name: str

    def complete(self, prompt: str) -> str: ...


class MockProvider:
    """Deterministic stand-in for a competent model.

    It reads the document with the rule-based extractor and serialises the
    result as the JSON a model would have returned. That means mock mode and
    regex fallback agree on easy documents -- which is honest, and exactly
    what you want for a default that must never surprise anyone -- while
    still driving the real parse, validate and reconcile path.

    It is therefore not a benchmark of LLM quality. It is a guarantee that
    the pipeline around the LLM works without one.
    """

    name = "mock"

    def __init__(self, reference_config: ReferenceConfig) -> None:
        self._reference_config = reference_config

    def complete(self, prompt: str) -> str:
        document = _document_from_prompt(prompt)
        extracted = extract_with_regex(document, self._reference_config)

        draft = {
            "lines": [
                {
                    key: value
                    for key, value in {
                        "invoice_reference": line.invoice_reference,
                        "gross_amount": _rupees(line.gross_amount_paise),
                        "paid_amount": _rupees(line.paid_amount_paise),
                        "deduction_amount": _rupees(line.deduction_amount_paise or None),
                        "deduction_reason": line.deduction_note,
                    }.items()
                    if value is not None
                }
                for line in extracted.lines
            ],
        }
        if extracted.total_paise is not None:
            draft["total_amount"] = _rupees(extracted.total_paise)
        if extracted.bank_reference:
            draft["bank_reference"] = extracted.bank_reference
        if extracted.document_deduction_paise:
            draft["unattributed_deduction"] = _rupees(extracted.document_deduction_paise)
            if extracted.document_deduction_note:
                draft["unattributed_deduction_reason"] = extracted.document_deduction_note

        return json.dumps(draft)


class ScriptedProvider:
    """Returns prepared responses in order, then repeats the last one.

    Lets a test say "first call returns garbage, second returns valid JSON"
    and assert the retry did what it claims to.
    """

    name = "scripted"

    def __init__(self, responses: list[str], *, raise_after: int | None = None) -> None:
        if not responses:
            raise ValueError("ScriptedProvider needs at least one response.")
        self._responses = responses
        self._raise_after = raise_after
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self._raise_after is not None and self.calls > self._raise_after:
            raise ProviderError("scripted provider failure")
        index = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[index]


class GeminiProvider:
    """Google Gemini via the google-genai SDK.

    Requests ``application/json`` with a response schema so the model is
    constrained at generation time rather than only validated afterwards.
    The Pydantic validation still runs: schema-constrained decoding makes
    malformed output rare, not impossible, and "rare" is not a basis for
    trusting a number that will post against a customer's account.

    Verified against the live API: the client, model and request shape all
    work. A full corpus run has not been completed live, because Gemini was
    returning 503 under load at the time -- which exercised the fallback for
    real and is exactly why every failure here is raised as
    :class:`ProviderError` for the pipeline to degrade from rather than
    crash on.
    """

    name = "llm"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ProviderError(
                "LLM_MODE=live needs a GEMINI_API_KEY. Set one in .env, or leave "
                "LLM_MODE=mock to run without a key."
            )
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError(
                "google-genai is not installed. Run `pip install -e .` in backend/, "
                "or set LLM_MODE=mock."
            ) from exc

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def complete(self, prompt: str) -> str:
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": RemittanceDraft.json_schema_for_prompt(),
                    # Extraction is a reading task, not a creative one.
                    "temperature": 0.0,
                },
            )
        except Exception as exc:
            raise ProviderError(f"Gemini request failed: {exc.__class__.__name__}: {exc}") from exc

        text = getattr(response, "text", None)
        if not text:
            raise ProviderError("Gemini returned an empty response.")
        return text


def _rupees(paise: int | None) -> str | None:
    return f"{paise_to_rupees(paise):.2f}" if paise is not None else None


def _document_from_prompt(prompt: str) -> str:
    """Recover the document from an assembled prompt.

    The mock provider receives the same prompt a real model would, so that
    prompt construction is exercised rather than bypassed.
    """
    marker = "### Document to extract"
    if marker not in prompt:
        return prompt
    body = prompt.split(marker, 1)[1]
    return body.rsplit("\nJSON:", 1)[0].strip()
