import pytest

from sentinel.ids import generate_ulid, is_valid_id, new_id

_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_new_id_has_prefix_and_26_char_ulid() -> None:
    iid = new_id("incident")
    prefix, _, ulid = iid.partition("_")
    assert prefix == "inc"
    assert len(ulid) == 26
    assert set(ulid) <= _CROCKFORD


def test_new_id_is_deterministic_with_injected_time_and_randomness() -> None:
    a = new_id("event", _ms=1_700_000_000_000, _rand=b"\x00" * 10)
    b = new_id("event", _ms=1_700_000_000_000, _rand=b"\x00" * 10)
    assert a == b
    assert a.startswith("evt_")


def test_generate_ulid_is_k_sortable_by_time() -> None:
    earlier = generate_ulid(_ms=1_000, _rand=b"\xff" * 10)
    later = generate_ulid(_ms=2_000, _rand=b"\x00" * 10)
    assert earlier < later  # leading 10 chars encode the timestamp


def test_is_valid_id_accepts_and_rejects() -> None:
    good = new_id("tenant")
    assert is_valid_id(good, "tenant")
    assert not is_valid_id(good, "incident")  # wrong prefix
    assert not is_valid_id("ten_short", "tenant")  # wrong length
    assert not is_valid_id("nope", "tenant")  # no ulid


def test_new_id_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        new_id("bogus")
