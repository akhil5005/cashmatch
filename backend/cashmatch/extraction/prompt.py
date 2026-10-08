"""The few-shot prompt.

Three choices worth defending here.

**Amounts are requested as strings.** A model asked for a number will emit
``418500.1`` and ``json.loads`` will hand back a float. Strings keep the
conversion inside :func:`cashmatch.money.rupees_to_paise`, which refuses
anything it cannot represent exactly.

**The examples teach restraint, not just extraction.** One of them has a
field the document does not state, and the expected output omits it rather
than inventing a plausible value. Teaching a model to leave a gap is harder,
and more valuable, than teaching it to fill one.

**The reason code is left in the document's own words.** Normalising "12
cases broken in transit" to ``damage`` is
:mod:`cashmatch.extraction.reasons`' job, and keeping it there means the LLM
path and the regex path produce the same vocabulary.
"""

from __future__ import annotations

import json

from cashmatch.extraction.schema import RemittanceDraft

SYSTEM_INSTRUCTION = """\
You read remittance advice documents from Indian FMCG distributors and return \
structured JSON. You extract only what the document actually states.

Rules you must follow:
1. Return JSON only. No prose, no markdown fences, no commentary.
2. Every monetary value is a STRING containing digits and at most one decimal \
point: "418500.00". Strip currency markers, commas and trailing "/-".
3. Never invent a value. If the document does not state an amount for a line, \
omit that field entirely. A missing field is correct; a guessed one is not.
4. Copy invoice references exactly as written, including any prefix.
5. Put the reason for any deduction in the document's own words. Do not \
translate it into a category.
6. A deduction stated without naming an invoice -- typically a sentence under \
the table -- belongs in "unattributed_deduction". Do not guess which line it \
sits on.
7. If the document states the same deduction twice, once as a column and once \
as a sentence, report it once.
"""

_EXAMPLES: tuple[tuple[str, dict], ...] = (
    (
        """Subject: Payment advice - 06.02.2026

Dear Sir/Madam,

We have remitted Rs. 4,18,500.00 towards the following bills.

  INV-00042    Rs. 3,00,000.00
  INV-00043    Rs. 1,30,500.00

Rs. 12,000.00 deducted - 12 cases damaged in transit.

Regards,
Accounts Department, Shree Balaji Traders Pvt Ltd""",
        {
            # The claim sentence names no invoice, so it is reported
            # unattributed rather than guessed onto INV-00043.
            "lines": [
                {"invoice_reference": "INV-00042", "gross_amount": "300000.00"},
                {"invoice_reference": "INV-00043", "gross_amount": "130500.00"},
            ],
            "total_amount": "418500.00",
            "unattributed_deduction": "12000.00",
            "unattributed_deduction_reason": "12 cases damaged in transit",
            "payer_name": "Shree Balaji Traders Pvt Ltd",
        },
    ),
    (
        """Subject: RTGS done - INR 192,681.43

Invoice        | Amount        | Remarks
----------------------------------------------------
INV-00746      | 2,04,931.43/- | less INR 12,250.00 damage

Total transferred: 192681.43

Thanks,
Arunima Ahuja | Balaji Distributors LLP

Bank reference: UTR2026020500105""",
        {
            "lines": [
                {
                    "invoice_reference": "INV-00746",
                    "gross_amount": "204931.43",
                    "paid_amount": "192681.43",
                    "deduction_amount": "12250.00",
                    "deduction_reason": "damage",
                }
            ],
            "total_amount": "192681.43",
            "payer_name": "Balaji Distributors LLP",
            "bank_reference": "UTR2026020500105",
        },
    ),
    (
        # Teaches restraint: the per-invoice split is not stated, so the model
        # must return the references with no amounts rather than guessing.
        """Subject: payment

pls note we have paid 10,94,691.97/- today against Inv. No. 410, INV 32, INV-00879.

Girik
Om Chatterjee Enterprises""",
        {
            "lines": [
                {"invoice_reference": "Inv. No. 410"},
                {"invoice_reference": "INV 32"},
                {"invoice_reference": "INV-00879"},
            ],
            "total_amount": "1094691.97",
            "payer_name": "Om Chatterjee Enterprises",
        },
    ),
)


def build_prompt(document_text: str, *, previous_error: str | None = None) -> str:
    """Assemble the extraction prompt for one document.

    Args:
        document_text: the raw advice text.
        previous_error: on a retry, the validation failure from the first
            attempt. Feeding the error back is what makes the retry worth
            spending: a blind second call usually reproduces the first.
    """
    parts: list[str] = [SYSTEM_INSTRUCTION, "", "Return JSON matching this schema:", ""]
    parts.append(json.dumps(RemittanceDraft.json_schema_for_prompt(), indent=2))
    parts.append("")

    for index, (example_text, expected) in enumerate(_EXAMPLES, start=1):
        parts += [
            f"### Example {index}",
            "Document:",
            example_text,
            "",
            "JSON:",
            json.dumps(expected, indent=2),
            "",
        ]

    if previous_error:
        parts += [
            "### Correction required",
            "Your previous answer was rejected by the schema validator:",
            previous_error,
            "Return corrected JSON that satisfies the schema. Do not invent values to fill gaps.",
            "",
        ]

    parts += ["### Document to extract", document_text, "", "JSON:"]
    return "\n".join(parts)
