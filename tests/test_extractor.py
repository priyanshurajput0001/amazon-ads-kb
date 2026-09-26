"""Tests for pipeline.extractor — offline; the LLM seam is faked."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pipeline.extractor import (
    LlmError,
    _parse_json_claims,
    extract_url,
    main,
)
from pipeline.state import SourceState, save_state

T0 = "2026-09-26T12:00:00+00:00"
TEX = "2026-09-26T18:00:00+00:00"
URL = "https://example.com/docs/onboarding"

CONTENT = (
    "# Amazon Ads API Onboarding Overview\n\n"
    "Before using any developer tool, you must complete the onboarding process "
    "and register an application.\n\n"
    "Amazon Marketing Stream delivers near real-time metrics through your AWS account.\n"
)

VALID_CLAIMS = [
    {"claim": "All developer tools require completing onboarding first.",
     "quote": "Before using any developer tool, you must complete the onboarding process",
     "topic_hint": "amazon-ads-api-onboarding", "confidence": "high"},
    {"claim": "Amazon Marketing Stream delivers near real-time metrics via AWS.",
     "quote": "Amazon Marketing Stream delivers near real-time metrics through your AWS account.",
     "topic_hint": "amazon-marketing-stream", "confidence": "medium"},
]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fake_llm(claims):
    """Build an LLM seam that returns `claims` (or raises if it's an Exception)."""

    def _llm(content, url):
        if isinstance(claims, Exception):
            raise claims
        return claims

    return _llm


class ExtractorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "cache"
        self.claims = Path(self._tmp.name) / "claims"
        self.cache.mkdir()
        self.states: dict = {}

    def tearDown(self):
        self._tmp.cleanup()

    def register(self, url=URL, content=CONTENT, write_cache=True, ext="md"):
        digest = sha(content)
        self.states[url] = SourceState(url=url, status="ok", fetched_at=T0,
                                       sha256=digest, strategy="tvly-basic")
        if write_cache:
            (self.cache / f"{digest}.{ext}").write_text(content, encoding="utf-8")
        return digest

    def run_extract(self, llm=None, url=URL):
        return extract_url(url, self.states, self.cache, self.claims,
                           llm=llm if llm is not None else fake_llm(VALID_CLAIMS),
                           now=TEX)


class ExtractionGuardTests(ExtractorTestCase):
    def test_01_url_not_in_state(self):
        result = self.run_extract()  # nothing registered
        self.assertEqual(result["status"], "error")
        self.assertIn("not found in fetch state", result["error"])
        self.assertFalse(self.claims.exists())

    def test_02_missing_cache_file(self):
        self.register(write_cache=False)
        result = self.run_extract()
        self.assertEqual(result["status"], "error")
        self.assertIn("cache file missing", result["error"])

    def test_03_html_cache_rejected(self):
        self.register(ext="html")
        result = self.run_extract()
        self.assertEqual(result["status"], "error")
        self.assertIn("HTML", result["error"])

    def test_04_sha_mismatch_rejected(self):
        self.register(content=CONTENT)
        # Corrupt the cache so its hash no longer matches state.
        digest = sha(CONTENT)
        (self.cache / f"{digest}.md").write_text(CONTENT + "\ntampered", encoding="utf-8")
        result = self.run_extract()
        self.assertEqual(result["status"], "error")
        self.assertIn("integrity check failed", result["error"])
        self.assertFalse(self.claims.exists())

    def test_05_existing_claims_skipped_and_llm_not_called(self):
        digest = self.register()
        self.claims.mkdir()
        (self.claims / f"{digest}.json").write_text('{"status": "ok"}', encoding="utf-8")

        def must_not_be_called(content, url):
            raise AssertionError("LLM must not run when claims already exist")

        result = self.run_extract(llm=must_not_be_called)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "claims already exist")
        self.assertEqual(result["sha256"], digest)

    def test_07_invalid_json_from_llm_rejected(self):
        self.register()
        result = self.run_extract(llm=fake_llm(LlmError("LLM returned invalid JSON")))
        self.assertEqual(result["status"], "error")
        self.assertIn("LLM returned invalid JSON", result["error"])
        self.assertFalse(self.claims.exists())

    def test_08_missing_required_claim_field_rejected(self):
        self.register()
        bad = [{k: v for k, v in VALID_CLAIMS[0].items() if k != "topic_hint"}]
        result = self.run_extract(llm=fake_llm(bad))
        self.assertEqual(result["status"], "error")
        self.assertIn("missing 'topic_hint'", result["error"])
        self.assertFalse(self.claims.exists())

    def test_08b_empty_quote_rejected(self):
        self.register()
        bad = [dict(VALID_CLAIMS[0], quote="   ")]
        result = self.run_extract(llm=fake_llm(bad))
        self.assertEqual(result["status"], "error")
        self.assertIn("non-empty string", result["error"])

    def test_08c_bad_confidence_rejected(self):
        self.register()
        bad = [dict(VALID_CLAIMS[0], confidence="certain")]
        result = self.run_extract(llm=fake_llm(bad))
        self.assertEqual(result["status"], "error")
        self.assertIn("confidence", result["error"])

    def test_09_quote_not_in_source_rejected(self):
        self.register()
        bad = [dict(VALID_CLAIMS[0], quote="this sentence does not appear in the source")]
        result = self.run_extract(llm=fake_llm(bad))
        self.assertEqual(result["status"], "error")
        self.assertIn("quote not found verbatim", result["error"])
        self.assertFalse(self.claims.exists())

    def test_10_valid_claims_written_correctly(self):
        digest = self.register()
        result = self.run_extract()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["skipped"])
        self.assertEqual(result["claim_count"], 2)
        path = Path(result["claims_path"])
        self.assertTrue(path.exists())
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc, {
            "schema_version": 1,
            "source_url": URL,
            "sha256": digest,
            "fetched_at": T0,
            "extracted_at": TEX,
            "status": "ok",
            "claims": VALID_CLAIMS,
        })
        # Grounding: every quote really is in the cached source.
        for claim in doc["claims"]:
            self.assertIn(claim["quote"], CONTENT)


class LlmParseTests(unittest.TestCase):
    def test_bare_json_array(self):
        self.assertEqual(_parse_json_claims('[{"claim": "a"}]'), [{"claim": "a"}])

    def test_fenced_json_array(self):
        text = '```json\n[{"claim": "a"}]\n```'
        self.assertEqual(_parse_json_claims(text), [{"claim": "a"}])

    def test_claims_object_unwrapped(self):
        self.assertEqual(_parse_json_claims('{"claims": [{"claim": "a"}]}'),
                         [{"claim": "a"}])

    def test_garbage_raises_llm_error(self):
        with self.assertRaises(LlmError):
            _parse_json_claims("Sure! Here are the claims: ...")

    def test_non_list_raises_llm_error(self):
        with self.assertRaises(LlmError):
            _parse_json_claims('{"error": "no claims"}')


class CliTests(ExtractorTestCase):
    def test_multiple_urls_one_failure_does_not_stop_others(self):
        self.register()
        save_state(Path(self._tmp.name) / "fetch_state.json", self.states)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["https://never-fetched.example", URL],
                        state_path=Path(self._tmp.name) / "fetch_state.json",
                        cache_path=self.cache, claims_path=self.claims,
                        llm=fake_llm(VALID_CLAIMS))
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["status"], "error")
        self.assertEqual(lines[1]["status"], "ok")
        self.assertEqual(lines[1]["claim_count"], 2)


if __name__ == "__main__":
    unittest.main()
