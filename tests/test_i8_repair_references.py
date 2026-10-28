"""The R-reference on the repair order links a declaration to its repair
(BE13, 27-Oct-2026): the parser reuse, coverage through it, and precedence."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.assistant import ids
from app.initiatives.i8 import attestation as module
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.repair_references import resolve_repair_references
from app.initiatives.i13.session_link import RequestLine, SessionInfo

CFG = get_i8_settings()
SESSION = ids.mint()
OTHER_SESSION = ids.mint()
REQUEST = ids.mint_request()


def _row(text, *, banfn="1000000001", bnfpo="00010", material="8000005632", plant="1300", bwart=None):
    return {
        "rsnum": "9000001",
        "rspos": "0001",
        "matnr": material,
        "werks": plant,
        "bwart": bwart or CFG.repair_reservation_movement_type,
        "banfn": banfn,
        "bnfpo": bnfpo,
        "aufnr": None,
        "xloek": None,
        "kzear": None,
        "sgtxt": text,
    }


SESSIONS = {
    SESSION: SessionInfo(SESSION, "8000005632", "1300", False),
    OTHER_SESSION: SessionInfo(OTHER_SESSION, "8000009999", "1300", False),
}


class TestResolve:
    def test_a_valid_session_for_the_same_part_links(self) -> None:
        refs = resolve_repair_references([_row(f"for {SESSION} pump")], SESSIONS, {}, CFG)
        assert refs == (("1000000001", "10", SESSION, SESSION),)

    def test_a_mistyped_session_does_not_link(self) -> None:
        typo = SESSION[:-1] + ("A" if SESSION[-1] != "A" else "B")
        assert resolve_repair_references([_row(typo)], SESSIONS, {}, CFG) == ()

    def test_a_session_for_another_part_does_not_link(self) -> None:
        assert resolve_repair_references([_row(OTHER_SESSION)], SESSIONS, {}, CFG) == ()

    def test_an_unknown_session_does_not_link(self) -> None:
        assert resolve_repair_references([_row(SESSION)], {}, {}, CFG) == ()

    def test_a_line_on_another_movement_type_is_not_read(self) -> None:
        assert resolve_repair_references([_row(SESSION, bwart="261")], SESSIONS, {}, CFG) == ()

    def test_a_request_links_through_its_line_for_this_part(self) -> None:
        lines = {
            REQUEST: [
                RequestLine(REQUEST, 1, OTHER_SESSION, "8000009999", "1300"),
                RequestLine(REQUEST, 2, SESSION, "8000005632", "1300"),
            ]
        }
        refs = resolve_repair_references([_row(f"{REQUEST}/1")], SESSIONS, lines, CFG)
        # The /1 does not decide: material and plant pick line 2.
        assert refs == (("1000000001", "10", SESSION, f"{REQUEST}/2"),)

    def test_a_request_with_no_line_for_this_part_does_not_link(self) -> None:
        lines = {REQUEST: [RequestLine(REQUEST, 1, OTHER_SESSION, "8000009999", "1300")]}
        assert resolve_repair_references([_row(REQUEST)], SESSIONS, lines, CFG) == ()


def _declaration(*, session_id=None, material="8000005632", plant="1300", document=None, item=None, kind=None):
    return SimpleNamespace(
        id=f"A-{session_id or document}",
        material_id=material,
        plant=plant,
        attested_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        repair_document=document,
        repair_item=item,
        repair_document_type=kind,
        session_id=session_id,
    )


def _line(document="4500", item="10", *, pr="1000000001", pr_item="10", raised=date(2021, 3, 11)):
    return SimpleNamespace(
        purchasing_document=document,
        item=item,
        key=(document, item),
        material_id="8000005632",
        plant="1300",
        raised_at=raised,
        pr_number=pr,
        pr_item=pr_item,
    )


def _request(banfn="1000000001", item="10", on=date(2021, 3, 11)):
    return SimpleNamespace(
        purchase_requisition=banfn,
        item=item,
        key=(banfn, item),
        material_id="8000005632",
        plant="1300",
        requested_on=on,
    )


REFS = (("1000000001", "10", SESSION, SESSION),)


class TestCoverageByReference:
    def test_the_po_ordered_from_a_referenced_pr_is_covered(self, monkeypatch) -> None:
        declared = _declaration(session_id=SESSION)
        monkeypatch.setattr(module, "find", lambda *a, **k: [declared])
        result = module.coverage(db=None, lines=[_line()], cfg=CFG, references=REFS)
        assert result.covered[("4500", "10")] is declared
        assert result.links[("4500", "10")] == module.LINK_REFERENCE
        # Out of window by years: without the reference it would be uncovered.
        bare = module.coverage(db=None, lines=[_line()], cfg=CFG)
        assert bare.uncovered == {("4500", "10")}

    def test_an_open_pr_is_covered_by_reference(self, monkeypatch) -> None:
        declared = _declaration(session_id=SESSION)
        monkeypatch.setattr(module, "find", lambda *a, **k: [declared])
        result = module.coverage(db=None, lines=[], cfg=CFG, requests=[_request()], references=REFS)
        assert result.requests_covered[("1000000001", "10")] is declared
        assert result.request_links[("1000000001", "10")] == module.LINK_REFERENCE

    def test_a_declaration_for_another_part_in_that_session_does_not_cover(self, monkeypatch) -> None:
        other = _declaration(session_id=SESSION, material="8000009999")
        monkeypatch.setattr(module, "find", lambda *a, **k: [other])
        result = module.coverage(db=None, lines=[_line()], cfg=CFG, references=REFS)
        assert result.uncovered == {("4500", "10")}

    def test_a_reference_to_another_pr_does_not_cover(self, monkeypatch) -> None:
        declared = _declaration(session_id=SESSION)
        monkeypatch.setattr(module, "find", lambda *a, **k: [declared])
        refs = (("1000000002", "10", SESSION, SESSION),)
        result = module.coverage(db=None, lines=[_line()], cfg=CFG, references=refs)
        assert result.uncovered == {("4500", "10")}

    def test_the_reference_comes_before_a_named_declaration(self, monkeypatch) -> None:
        named = _declaration(document="4500", item="10", kind="PO")
        visit = _declaration(session_id=SESSION)
        monkeypatch.setattr(module, "find", lambda *a, **k: [named, visit])
        result = module.coverage(db=None, lines=[_line()], cfg=CFG, references=REFS)
        assert result.covered[("4500", "10")] is visit
        assert result.links[("4500", "10")] == module.LINK_REFERENCE
        assert ("4500", "10") not in result.bound

    def test_with_no_reference_the_named_declaration_still_binds(self, monkeypatch) -> None:
        named = _declaration(document="4500", item="10", kind="PO")
        monkeypatch.setattr(module, "find", lambda *a, **k: [named])
        result = module.coverage(db=None, lines=[_line()], cfg=CFG, references=REFS)
        assert result.links[("4500", "10")] == module.LINK_BOUND
