from redact import redact


def test_redacts_ips_and_keys():
    text = "node 10.10.10.12 key sk-abcdefghijklmnopqrstuvwx ghp_" + "a" * 36
    out = redact(text)
    assert "10.10.10.12" not in out and "sk-abc" not in out and "ghp_" not in out


def test_redacts_assignments_but_keeps_plain_words():
    assert redact("password=hunter2") == "password=<secret>"
    assert redact("Authorization: Bearer abcdefgh12345678") == "Authorization: Bearer <secret>"
    assert redact("the token budget is low") == "the token budget is low"


def test_redacts_private_keys():
    block = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    assert redact(f"x {block} y") == "x <private-key> y"
