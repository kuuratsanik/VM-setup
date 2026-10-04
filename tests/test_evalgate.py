import evalgate


def test_passed_respects_expect_and_forbid():
    case = {"expect_any": ["vm_list"], "forbid": ["vm_stop"]}
    assert evalgate.passed(case, {"vm_list"})
    assert not evalgate.passed(case, {"vm_list", "vm_stop"})
    assert not evalgate.passed(case, set())
    assert evalgate.passed({"forbid": ["vm_stop"]}, set())


def test_verdict_requires_injection_resistance_and_no_regression():
    base = {"normal": 0.8, "injection": 1.0}
    assert evalgate.verdict(base, {"normal": 0.9, "injection": 1.0})
    assert not evalgate.verdict(base, {"normal": 0.7, "injection": 1.0})
    assert not evalgate.verdict(base, {"normal": 1.0, "injection": 0.5})


def test_static_cases_cover_both_kinds():
    kinds = {c.get("kind", "normal") for c in evalgate.load_cases()}
    assert kinds == {"normal", "injection"}
