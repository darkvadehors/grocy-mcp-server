import logging

from grocy_mcp.redact import RedactingFilter, install_redaction, redact_text

GROCY_KEY = "a" * 25 + "B" * 25


def test_headers_are_masked():
    out = redact_text(f"Authorization: Bearer gma_abc GROCY-API-KEY: {GROCY_KEY}")
    assert "gma_abc" not in out and GROCY_KEY not in out


def test_query_parameters_are_masked():
    out = redact_text("GET /cb?code=SECRETCODE&state=xyz&refresh_token=RT&code_verifier=V&req=R&cle=K")
    for secret in ("SECRETCODE", "=RT", "=V&", "=R&", "=K"):
        assert secret not in out
    assert "state=xyz" in out


def test_tokens_and_grocy_keys_are_masked():
    out = redact_text(f"jeton gmr_{'x' * 43} et clé {GROCY_KEY}")
    assert "gmr_" not in out and GROCY_KEY not in out


def test_innocent_text_untouched():
    assert redact_text("3 yaourts ajoutés au Frigo") == "3 yaourts ajoutés au Frigo"


def test_filter_rewrites_record():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "clé %s", (GROCY_KEY,), None)
    assert RedactingFilter().filter(record) is True
    assert GROCY_KEY not in record.getMessage()


def test_install_on_root_handlers():
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        install_redaction()
        assert any(isinstance(f, RedactingFilter) for f in handler.filters)
    finally:
        root.removeHandler(handler)
