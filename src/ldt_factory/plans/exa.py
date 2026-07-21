from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import DevelopmentPlanConfig
from .registry import AdminArea


class ExaError(RuntimeError):
    """Raised when Exa cannot complete a discovery or content request."""


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "jurisdiction": {"type": "string"},
        "jurisdiction_match": {"type": "boolean"},
        "is_development_plan": {"type": "boolean"},
        "is_full_document": {"type": "boolean"},
        "status": {"type": "string"},
        "start_year": {"type": "string"},
        "end_year": {"type": "string"},
        "adoption_date": {"type": "string"},
        "issuing_authority": {"type": "string"},
        "direct_document_url": {"type": "string"},
    },
    "required": [
        "jurisdiction",
        "jurisdiction_match",
        "is_development_plan",
        "is_full_document",
        "status",
        "start_year",
        "end_year",
        "adoption_date",
        "issuing_authority",
        "direct_document_url",
    ],
}


def _quoted_terms(values: list[str]) -> str:
    return " OR ".join(f'"{value}"' for value in values)


def build_search_request(
    config: DevelopmentPlanConfig,
    area: AdminArea,
    pass_number: int,
) -> dict[str, Any]:
    search = config.search
    terms = list(search["document_terms"])
    aliases = [area.name, *area.aliases]
    names = " OR ".join(f'"{name}"' for name in aliases)
    location = f", {area.parent}" if area.parent else ""
    evidence_query = (
        f"Evidence that this is the full formally adopted development plan for "
        f"{area.name}{location}, including jurisdiction, plan period, approval status, "
        "issuing authority, and direct document link"
    )
    summary_query = (
        f"Evaluate this result only as a candidate for the full local development plan for "
        f"{area.name}{location}, {config.country_name}. Extract the named fields. "
        "Use unknown when a string field is not supported by the source. "
        "A draft, consultation, budget, spatial plan, annual report, implementation report, "
        "decision to start preparation, or citizen summary is not a full adopted plan."
    )
    contents = {
        "highlights": {"query": evidence_query, "maxCharacters": 4000},
        "summary": {"query": summary_query, "schema": SUMMARY_SCHEMA},
        "extras": {"links": 20},
    }
    common: dict[str, Any] = {
        "numResults": int(search["num_results"]),
        "contents": contents,
        "systemPrompt": (
            "Prefer official local-government and government repositories. Return candidates, "
            "including uncertain ones, without treating webpage publication dates as plan dates."
        ),
    }
    if pass_number == 1:
        return {
            **common,
            "type": "auto",
            "query": (
                f"Latest adopted full development plan for {names}{location}, "
                f"{config.country_name}. {_quoted_terms(terms)} PDF DOCX"
            ),
        }
    if pass_number == 2:
        additional = [
            f'"{term}" "{name}" {area.parent} PDF'
            for term in terms
            for name in aliases[:2]
        ][:10]
        return {
            **common,
            "type": "deep",
            "query": (
                f"Find all plausible versions of the local development plan for {names}{location}, "
                f"{config.country_name}, and prioritize the latest adopted full document"
            ),
            "additionalQueries": additional,
        }
    if pass_number == 3:
        domains = list(search.get("official_domain_suffixes", ()))
        domain_hint = " OR ".join(f"site:{domain}" for domain in domains)
        final_terms = list(search.get("final_terms", ()))
        return {
            **common,
            "type": "deep",
            "query": (
                f"Targeted recovery search for an official full development plan for "
                f"{names}{location}, {config.country_name}. Search council document libraries, "
                f"gazettes, ministries, and linked PDFs. {domain_hint} {_quoted_terms(final_terms)}"
            ).strip(),
            "additionalQueries": [
                f'"{area.name}" "{term}" official PDF'
                for term in [*terms, *final_terms]
            ][:10],
        }
    raise ValueError("pass_number must be 1, 2, or 3")


@dataclass
class ExaClient:
    api_key: str
    timeout_seconds: int = 60
    max_retries: int = 3
    use_environment_proxy: bool = False

    @classmethod
    def from_config(cls, config: DevelopmentPlanConfig) -> "ExaClient":
        api_key = os.environ.get(config.api_key_env, "").strip()
        if not api_key:
            raise ExaError(f"Missing Exa API key in environment variable {config.api_key_env}")
        return cls(
            api_key,
            timeout_seconds=int(config.search["request_timeout_seconds"]),
            use_environment_proxy=bool(config.search.get("use_environment_proxy", False)),
        )

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        retry_statuses = {429, 500, 502, 503, 504}
        with requests.Session() as session:
            session.trust_env = self.use_environment_proxy
            headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
            for attempt in range(self.max_retries + 1):
                try:
                    response = session.post(
                        f"https://api.exa.ai/{endpoint}",
                        headers=headers,
                        json=payload,
                        timeout=(15, self.timeout_seconds),
                    )
                    if response.status_code in retry_statuses and attempt < self.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                        time.sleep(min(delay, 30))
                        continue
                    response.raise_for_status()
                    result = response.json()
                    if not isinstance(result, dict):
                        raise ExaError(f"Exa {endpoint} response was not a JSON object")
                    return result
                except requests.RequestException as error:
                    if attempt >= self.max_retries:
                        raise ExaError(f"Exa {endpoint} request failed: {error}") from error
                    time.sleep(min(2**attempt, 30))
        raise ExaError(f"Exa {endpoint} retry loop ended unexpectedly")

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("search", payload)

    def contents(self, urls: list[str], *, query: str, fresh: bool = True) -> dict[str, Any]:
        payload = {
            "urls": urls,
            "highlights": {"query": query, "maxCharacters": 5000},
            "summary": {"query": query, "schema": SUMMARY_SCHEMA},
            "extras": {"links": 20},
            "maxAgeHours": 0 if fresh else 24,
            "livecrawlTimeout": min(self.timeout_seconds * 1000, 90000),
        }
        return self._post("contents", payload)
