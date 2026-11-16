from nexus.integrations.wa_links import build, normalize, parse, personalize


def test_normalize_uae_and_international():
    assert normalize("050 123 4567") == "971501234567"
    assert normalize("+971 55 765 4321") == "971557654321"
    assert normalize("00971501112233") == "971501112233"
    assert normalize("501112233") == "971501112233"
    assert normalize("+91 98935 73666") == "919893573666"
    assert normalize("12") == ""


def test_parse_message_and_contacts():
    msg, contacts = parse("Hi {name}, quick one about your listings?\nOmar Khan, 050 123 4567\n+971 55 765 4321\n"
                          "Sara - 971501112233")
    assert msg == "Hi {name}, quick one about your listings?"
    assert [c["name"] for c in contacts] == ["Omar Khan", "", "Sara"]
    assert [c["number"] for c in contacts] == ["971501234567", "971557654321", "971501112233"]


def test_personalize_with_and_without_name():
    assert personalize("Hi {name}, quick one", "omar khan") == "Hi Omar, quick one"
    assert personalize("Hi {name}, quick one", "") == "Hi, quick one"
    assert personalize("Hey {name}!", "") == "Hey!"


def test_build_skips_duplicates_and_dnc():
    out = build("Hi {name}\nOmar, 0501234567\nOmar again, +971501234567\nBlocked, 0509999999",
                is_blocked=lambda n: n == "971509999999")
    assert [l["number"] for l in out["links"]] == ["971501234567"]
    assert out["links"][0]["url"] == "https://wa.me/971501234567?text=Hi%20Omar"
    assert [why for _, why in out["skipped"]] == ["duplicate", "do-not-contact"]
