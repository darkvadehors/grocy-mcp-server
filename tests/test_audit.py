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



def test_restart_when_tail_cut_inside_multibyte_character(tmp_path):
    """Au-delà de 64 Ko, la lecture de la fin du journal peut commencer au milieu d'un « é » : le service doit redémarrer."""
    path = tmp_path / "audit.jsonl"
    AuditLog(path, lambda: 0.0).write("action", note="dernier")
    last = path.read_bytes()
    for k in (1, 2):
        data = b"A" * k + "é".encode() * 40_000 + b"\n" + last
        if (len(data) - 65_536 - k) % 2 == 1:  # la coupure tombe sur le second octet d'un « é »
            break
    path.write_bytes(data)
    AuditLog(path, lambda: 0.0).write("action", note="après redémarrage")  # ne doit pas lever UnicodeDecodeError
    assert path.read_text().splitlines()[-1].count("après redémarrage") == 1
