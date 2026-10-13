import json
import os
import stat

from grocy_mcp.audit import AuditLog, verify_chain


def test_chain_and_permissions(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = AuditLog(p, clock=lambda: 0.0)
    log.write("connexion", compte="johnny-ia", client="Claude")
    log.write("outil", compte="johnny-ia", outil="consommer", resultat="ok")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640
    lines = p.read_text().splitlines()
    first, second = (json.loads(line) for line in lines)
    assert first["prev"] == "0" * 64
    assert second["prev"] != first["prev"]
    assert first["ts"] == "1970-01-01T00:00:00Z"
    assert verify_chain(p) is True


def test_chain_continues_after_restart(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).write("a")
    AuditLog(p).write("b")
    assert verify_chain(p) is True


def test_tampering_detected(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = AuditLog(p)
    log.write("a", n=1)
    log.write("b", n=2)
    log.write("c", n=3)
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace('"n":2', '"n":9')
    p.write_text("\n".join(lines) + "\n")
    assert verify_chain(p) is False


def test_fields_are_redacted(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).write("x", detail="code=SECRET&state=1")
    assert "SECRET" not in p.read_text()
