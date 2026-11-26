"""The layered review in the manuscript workbench, through real widgets.

Requests go through the real ApiClient and an httpx MockTransport, so the
report validation, the pairing and the apply paths are the shipped code.
Layer 1 is the deterministic structure check (``/v1/structure-checks``),
Layer 2 the experimental semantic review, Layer 3 a reserved switch; the term
track lists ambiguous one-character names (``ambiguous-mention-v1``) apart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
from test_manuscript_semantic import issue, semantic_report

from wuwaterm_client import markdown_report as mdr
from wuwaterm_client import strings, theme
from wuwaterm_client.api import ApiClient
from wuwaterm_client.manuscript import empty_workfile, serialize_workfile
from wuwaterm_client.ui import review_states
from wuwaterm_client.ui.manuscript_view import TRACK_SEMANTIC, ManuscriptView

AMB = "ambiguous-mention-v1"
STRUCTURE = "structure-check-v1"
CANDIDATE = {"candidate_id": "c" * 64, "zh": "椿", "en": "Camellya", "category": "resonator"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def term_report(source: str, target: str, resolutions=None, *, ambiguous: bool = True) -> dict:
    """Core's review-v2 answer: the first 椿 judged, every later one an
    unsettled one-character name until a resolution names it."""
    findings, resolved = [], {r["mention_id"] for r in resolutions or ()}
    start = source.find("椿")
    first = True
    while start >= 0:
        mention = f"{start}:{start + 1}:椿"
        item = {"id": mention, "rule_id": "review.term_pair", "verdict": "verified_constraint",
                "source_span": {"start": start, "end": start + 1, "text": "椿"}, "target_span": None,
                "candidates": [{**CANDIDATE, "sources": []}], "candidates_truncated": False}
        at = target.find("Camellya")
        if first and at >= 0:
            item["target_span"] = {"start": at, "end": at + 8, "text": "Camellya"}
        elif not first and ambiguous and mention not in resolved:
            item.update(verdict="not_evaluated", reason="ambiguous_mention")
        findings.append(item)
        first = False
        start = source.find("椿", start + 1)
    not_evaluated = sum(1 for f in findings if f["verdict"] == "not_evaluated")
    return {"request_id": "term", "source_revision": digest(source), "target_revision": digest(target),
            "rule_version": "review-v2", "matcher_revision": "a" * 64,
            "dictionary": {"schema_version": "1", "source_commit": "b" * 40, "term_count": 1, "revision": "a" * 64},
            "coverage": {"evaluated": len(findings) - not_evaluated, "not_evaluated": not_evaluated,
                         "rules": ["review.term_pair", "review.sentence_meaning"]},
            "findings": findings, "truncated": False}


def structure_answer(body: dict) -> dict:
    """number_missing for every 100 a source has and its target lacks."""
    segments = []
    for index, seg in enumerate(body["segments"]):
        findings = []
        at = seg["source"].find("100")
        if at >= 0 and "100" not in seg["target"]:
            findings.append({"rule_id": "number_missing", "source_span": {"start": at, "end": at + 3, "text": "100"},
                             "target_span": None})
        segments.append({"index": index, "segment_id": seg["segment_id"], "findings": findings,
                         "omitted_findings": 0,
                         "layout": {"source_paragraphs": 1, "target_paragraphs": 1, "source_lines": 1,
                                    "target_lines": 1}})
    return {"request_id": "l1", "check_version": STRUCTURE, "checks": ["empty_target", "numbers", "placeholders",
                                                                         "paragraphs"], "segments": segments}


class Server:
    def __init__(self, capabilities=(), issues=None):
        self.capabilities = capabilities
        self.issues = issues or (lambda source, target: [])
        self.requests: list[tuple[str, dict | None, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, body, dict(request.headers)))
        path = request.url.path
        if path == "/v1/capabilities":
            return httpx.Response(200, json={"api_version": "v1", "capabilities": list(self.capabilities),
                                             "request_id": "caps"})
        if path == "/v1/reviews":
            features = request.headers.get("x-wuwaterm-review-features", "")
            return httpx.Response(200, json=term_report(body["source"], body["target"], body.get("resolutions"),
                                                        ambiguous=AMB in features))
        if path == "/v1/structure-checks":
            return httpx.Response(200, json=structure_answer(body))
        if path == "/v1/semantic-reviews":
            return httpx.Response(200, json=semantic_report(body["source"], body["target"],
                                                            self.issues(body["source"], body["target"])))
        raise AssertionError(path)

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.requests]

    def sent(self, path: str) -> list[dict]:
        return [body for p, body, _ in self.requests if p == path]


def make_view(server: Server, source: str, target: str) -> ManuscriptView:
    client = ApiClient("https://example.invalid", token_provider=lambda: "test-token",
                       _test_transport=httpx.MockTransport(server))
    view = ManuscriptView(client)
    value = empty_workfile()
    value.update(source=source, target=target)
    view._set_payload(value)
    view._saved_payload = view._payload.copy()
    view._update_controls()
    view._refresh_derived()
    return view


async def settle(view: ManuscriptView) -> None:
    controller = view._controller
    for _ in range(200):
        tasks = [t for t in (view._task, view._semantic_task, view._translate_task, controller.structure_task)
                 if t is not None and not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            return
        await asyncio.wait(tasks)
    raise AssertionError("tasks did not settle")


NUMBER_SOURCE = "价格是 100 元，她不会出发。"
NUMBER_TARGET = "The price is 10 yuan, and she will leave."


def test_layer_one_runs_when_pressed_locates_and_is_never_written(qapp, tmp_path) -> None:
    async def scenario():
        server = Server(capabilities=(STRUCTURE,))
        view = make_view(server, NUMBER_SOURCE, NUMBER_TARGET)
        assert server.requests == [], "nothing is asked for before the owner presses a button"
        assert view.l1_title.text() == strings.MANUSCRIPT_L1_TITLE
        assert view.l1_run_button.text() == strings.MANUSCRIPT_L1_RUN.format(count=1)
        assert view.l1_run_button.isEnabled() and view.l1_list.isHidden()
        before = serialize_workfile(view._payload)
        report_before = view.report_markdown()
        view.l1_run_button.click()
        await settle(view)
        assert server.paths() == ["/v1/capabilities", "/v1/structure-checks"]
        assert view.l1_list.count() == 1
        row = view.l1_list.item(0)
        assert strings.MANUSCRIPT_L1_RULES["number_missing"] in row.text() and "100" in row.text()
        assert row.data(review_states.STATE_ROLE) == review_states.NEEDS_CHECK
        assert strings.MANUSCRIPT_L1_SUMMARY.format(checked=1, segments=1, count=1) in view.l1_status.text()
        assert view.l1_run_button.text() == strings.MANUSCRIPT_L1_RERUN.format(count=1)
        view.l1_list.setCurrentRow(0)
        assert view.source_edit.textCursor().selectedText() == "100"
        # Never written: not dirty, nothing to undo, the same export, the same bytes on save.
        assert not view.document_dirty and not view._controller.can_undo()
        assert view.report_markdown() == report_before
        assert serialize_workfile(view._payload) == before
        path = tmp_path / "draft.wuwaterm"
        view._path = path
        assert view.save_document()
        assert path.read_text(encoding="utf-8") == before
        # An edit of the segment drops its result at once, and it stays gone.
        cursor = view.source_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        view.source_edit.setTextCursor(cursor)
        view.source_edit.insertPlainText("改")
        assert view.l1_list.count() == 0 and view.l1_list.isHidden()
        view._refresh_derived()
        assert view.l1_list.count() == 0
        assert strings.MANUSCRIPT_L1_NOT_RUN in view.l1_status.text()
        # Undo brings the text back, and the result made for it.
        view._undo_replace()
        view._refresh_derived()
        assert view.l1_list.count() == 1
    asyncio.run(scenario())


def test_layer_one_says_when_the_service_does_not_offer_it(qapp) -> None:
    async def scenario():
        server = Server(capabilities=())
        view = make_view(server, NUMBER_SOURCE, NUMBER_TARGET)
        view.l1_run_button.click()
        await settle(view)
        assert server.paths() == ["/v1/capabilities"]
        assert strings.MANUSCRIPT_L1_UNAVAILABLE in view.l1_status.text()
        assert not view.l1_run_button.isEnabled() and view.l1_run_button.isHidden()
        assert view.l1_note.isHidden() and view.l1_list.isHidden()
    asyncio.run(scenario())


def layered_issues(source, target):
    return [
        issue(source, target, category="quantity_change", confidence="high", source_quote="100",
              target_quote="10", seed="1"),
        issue(source, target, category="negation_change", confidence="medium", source_quote="不会出发",
              target_quote="will leave", seed="2"),
        issue(source, target, category="meaning_shift", confidence="low", source_quote="价格",
              target_quote="price", seed="3"),
    ]


def test_layer_two_folds_low_confidence_and_leaves_out_what_layer_one_shows(qapp) -> None:
    async def scenario():
        server = Server(capabilities=("semantic-review-v1", STRUCTURE), issues=layered_issues)
        view = make_view(server, NUMBER_SOURCE, NUMBER_TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        # Before Layer 1: high and medium listed, low folded.
        assert view.semantic_list.count() == 2
        assert view.low_toggle.isVisibleTo(view) and not view.low_toggle.isChecked()
        assert view.low_toggle.text() == strings.MANUSCRIPT_L2_LOW_SHOW.format(count=1)
        assert view.l2_covered.isHidden()
        assert view.l2_title.text() == strings.MANUSCRIPT_L2_TITLE
        # Layer 1 finds the missing number: the quantity issue on it is not raised twice.
        view.l1_run_button.click()
        await settle(view)
        assert view.l1_list.count() == 1
        assert [i.issue["confidence"] for i in view._issue_rows] == ["medium"]
        assert view.l2_covered.text() == strings.MANUSCRIPT_L2_COVERED.format(count=1)
        assert view.semantic_track_button.text() == strings.MANUSCRIPT_TRACK_SEMANTIC.format(count=2)
        assert strings.MANUSCRIPT_L2_OVERVIEW.format(high=0, medium=1, low=1) in view.l2_overview.text()
        # F8 steps over what is listed only: the folded low issue is not reached.
        view._step_issue(1)
        view._step_issue(1)
        assert view._current_issue().issue["confidence"] == "medium"
        # Opening the fold lists the low issue last.
        view.low_toggle.click()
        assert [i.issue["confidence"] for i in view._issue_rows] == ["medium", "low"]
        assert view.low_toggle.text() == strings.MANUSCRIPT_L2_LOW_HIDE.format(count=1)
        # Nothing was decided or saved by leaving an issue out.
        assert view._payload.get("semantic_decisions", []) == []
        assert not view.document_dirty or view._payload["semantic_history"]
    asyncio.run(scenario())


def test_layer_one_leaves_out_a_low_confidence_issue_while_every_issue_is_folded(qapp) -> None:
    """Every semantic issue low confidence, so none is listed until the fold is
    opened: Layer 1 still decides which of them the fold holds and counts."""
    def all_low(source, target):
        return [issue(source, target, category="quantity_change", confidence="low", source_quote="100",
                      target_quote="10", seed="1")]

    async def scenario():
        server = Server(capabilities=("semantic-review-v1", STRUCTURE), issues=all_low)
        view = make_view(server, NUMBER_SOURCE, NUMBER_TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        assert view._issue_rows == [] and len(view._session.issues()) == 1
        assert view.low_toggle.text() == strings.MANUSCRIPT_L2_LOW_SHOW.format(count=1)
        assert view.semantic_track_button.text() == strings.MANUSCRIPT_TRACK_SEMANTIC.format(count=1)
        view.l1_run_button.click()
        await settle(view)
        # The missing number is Layer 1's: the folded issue on it is left out and counted as such.
        assert view.l2_covered.text() == strings.MANUSCRIPT_L2_COVERED.format(count=1)
        assert view.low_toggle.isHidden()
        assert view.semantic_track_button.text() == strings.MANUSCRIPT_TRACK_SEMANTIC.format(count=0)
        assert strings.MANUSCRIPT_L2_OVERVIEW.format(high=0, medium=0, low=0) in view.l2_overview.text()
        view.low_toggle.setChecked(True)
        assert view._issue_rows == []
    asyncio.run(scenario())


def test_closing_the_window_cancels_a_structure_check_in_flight(qapp) -> None:
    async def scenario():
        server = Server(capabilities=(STRUCTURE,))
        gate = asyncio.Event()
        seen: list[str] = []

        async def held(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/v1/structure-checks":
                await gate.wait()
            return server(request)

        client = ApiClient("https://example.invalid", token_provider=lambda: "test-token",
                           _test_transport=httpx.MockTransport(held))
        view = ManuscriptView(client)
        value = empty_workfile()
        value.update(source=NUMBER_SOURCE, target=NUMBER_TARGET)
        view._set_payload(value)
        view._saved_payload = view._payload.copy()
        view._refresh_derived()
        controller = view._controller
        view.l1_run_button.click()
        for _ in range(200):
            await asyncio.sleep(0)
            if "/v1/structure-checks" in seen:
                break
        assert controller.structure_running()
        renders: list[str] = []
        view._render_structure = lambda: renders.append("render")
        view.cancel_background_work()
        assert not controller.structure_running()
        gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
        # Nothing answers into the closed view, and nothing was kept.
        assert renders == []
        assert controller.structure_task is None and controller.structure.results == {}
        assert controller.structure.phase == "idle"
    asyncio.run(scenario())


def test_layer_three_is_off_by_default_and_sends_or_stores_nothing(qapp) -> None:
    server = Server(capabilities=(STRUCTURE,))
    view = make_view(server, NUMBER_SOURCE, NUMBER_TARGET)
    before = serialize_workfile(view._payload)
    assert view.l3_title.text() == strings.MANUSCRIPT_L3_TITLE
    assert view.l3_check.text() == strings.MANUSCRIPT_L3_TOGGLE
    assert not view.l3_check.isChecked() and view.l3_note.text() == strings.MANUSCRIPT_L3_OFF
    view.l3_check.click()
    assert view.l3_check.isChecked() and view.l3_note.text() == strings.MANUSCRIPT_L3_ON
    view.l3_check.click()
    assert server.requests == []
    assert serialize_workfile(view._payload) == before and not view.document_dirty


AMBIGUOUS_SOURCE = "椿和今汐在城楼上。椿看着远方。"
AMBIGUOUS_TARGET = "Camellya and Jinhsi stood on the tower. She looked afar."


def test_ambiguous_names_are_folded_after_the_judged_findings_and_not_counted(qapp) -> None:
    async def scenario():
        server = Server(capabilities=(AMB,))
        view = make_view(server, AMBIGUOUS_SOURCE, AMBIGUOUS_TARGET)
        view._start_review()
        await settle(view)
        _, body, headers = [r for r in server.requests if r[0] == "/v1/reviews"][0]
        assert headers["x-wuwaterm-review-features"] == "dense-128,reason-v1,ambiguous-mention-v1"
        assert body.get("resolutions") is None
        # One judged finding is listed and counted; the ambiguous one is apart.
        assert view.findings.count() == 1
        assert view.terms_track_button.text() == strings.MANUSCRIPT_TRACK_TERMS.format(count=1)
        assert view.finding_count.text() == strings.MANUSCRIPT_FINDING_COUNT.format(current=1, total=1)
        assert view.ambiguous_toggle.isVisibleTo(view) and not view.ambiguous_toggle.isChecked()
        assert view.ambiguous_toggle.text() == strings.MANUSCRIPT_AMBIGUOUS_SHOW.format(count=1)
        assert view.ambiguous_area.isHidden()
        view.ambiguous_toggle.click()
        assert view.ambiguous_area.isVisibleTo(view)
        assert view.ambiguous_list.count() == 1
        second = AMBIGUOUS_SOURCE.index("椿", 1)
        assert view.ambiguous_list.item(0).text() == strings.MANUSCRIPT_AMBIGUOUS_ITEM_DOCUMENT.format(
            text="椿", start=second + 1, end=second + 1)
        adopt = view.ambiguous_adopt_button
        assert view.ambiguous_combo.count() == 1 and view.ambiguous_combo.currentData() == CANDIDATE["candidate_id"]
        assert "椿" in view.ambiguous_combo.currentText() and "Camellya" in view.ambiguous_combo.currentText()
        assert adopt.text() == strings.MANUSCRIPT_AMBIGUOUS_ADOPT_BUTTON
        assert adopt.isEnabled() and view.ambiguous_ignore_button.isEnabled()
        # Adopt: staged in the manuscript, the report stays listed.
        adopt.click()
        [choice] = view._payload["choices"]
        assert (choice["choice"], choice["candidate"]["en"]) == ("official_pair", "Camellya")
        assert view.status_label.text() == strings.MANUSCRIPT_AMBIGUOUS_ADOPTED.format(zh="椿", en="Camellya")
        assert view.findings.count() == 1 and view.ambiguous_list.count() == 1
        assert strings.MANUSCRIPT_AMBIGUOUS_STAGED in view.ambiguous_list.item(0).text()
        # The exported report says the choice goes out, not that a desktop never sends it.
        exported = view.report_markdown()
        assert mdr.CHOICE_REASONS["position_basis_match"] in exported
        assert mdr.CHOICE_REASONS["desktop_not_submitted"] not in exported
        # The next check sends it; Core then judges the name.
        view._start_review()
        await settle(view)
        sent = server.sent("/v1/reviews")[-1]
        assert sent["resolutions"] == [{"mention_id": f"{second}:{second + 1}:椿", "choice": "official_pair",
                                        "candidate_id": CANDIDATE["candidate_id"]}]
        assert set(sent["resolution_context"]) == {"source_revision", "rule_version", "dictionary_revision",
                                                  "matcher_revision"}
        assert view.findings.count() == 2
        assert view.ambiguous_toggle.isHidden() and view.ambiguous_list.count() == 0
    asyncio.run(scenario())


def test_an_ambiguous_name_can_be_marked_an_ordinary_word(qapp) -> None:
    async def scenario():
        server = Server(capabilities=(AMB,))
        view = make_view(server, AMBIGUOUS_SOURCE, AMBIGUOUS_TARGET)
        view._start_review()
        await settle(view)
        view.ambiguous_toggle.click()
        view.ambiguous_ignore_button.click()
        [choice] = view._payload["choices"]
        assert (choice["choice"], choice["candidate"]) == ("not_a_term", None)
        exported = view.report_markdown()
        assert mdr.CHOICE_REASONS["user_not_a_term"] in exported
        assert mdr.CHOICE_REASONS["imported_not_a_term"] not in exported
        view._start_review()
        await settle(view)
        second = AMBIGUOUS_SOURCE.index("椿", 1)
        assert server.sent("/v1/reviews")[-1]["resolutions"] == [
            {"mention_id": f"{second}:{second + 1}:椿", "choice": "not_a_term"}]
    asyncio.run(scenario())


LONG_SOURCE = "\n\n".join(["今汐在城楼远望。" * 90, "椿穿过回廊。椿停下。" + "声骸在远处低鸣。" * 90,
                           "守岸人负责回能。" * 90])
LONG_TARGET = "\n\n".join(["Jinhsi looked afar from the tower. " * 30, "Camellya crossed the corridor. " * 34,
                           "The Shorekeeper restored energy. " * 32])


def test_long_manuscripts_resolve_an_ambiguous_name_as_a_segment_choice(qapp) -> None:
    async def scenario():
        server = Server(capabilities=(AMB,))
        view = make_view(server, LONG_SOURCE, LONG_TARGET)
        view._start_review()
        await settle(view)
        # Every later 椿 of the second segment is ambiguous; none is a term group.
        assert view._ambiguous and all(ref is not None and ref[0] == 1 for _, ref in view._ambiguous)
        assert all("椿" != g["surface"] or g["count"] == 1 for g in view._term_groups_data)
        view.ambiguous_toggle.click()
        view.ambiguous_list.setCurrentRow(0)
        finding, ref = view._ambiguous[0]
        assert view.ambiguous_list.item(0).text().startswith(
            strings.MANUSCRIPT_AMBIGUOUS_ITEM.format(text="椿", number=2))
        view.ambiguous_ignore_button.click()
        record = view._payload["segments"][1]
        [choice] = [c for c in record["choices"]
                    if (c["source_span"]["start"], c["source_span"]["end"]) == ref[1:]]
        assert choice["choice"] == "not_a_term" and choice["confirmed"]
        assert record["status"] == "stale"
        before = len(server.sent("/v1/reviews"))
        view._start_review()
        await settle(view)
        resent = server.sent("/v1/reviews")[before:]
        assert any(r.get("resolutions") and any(item["choice"] == "not_a_term" for item in r["resolutions"])
                   for r in resent)
    asyncio.run(scenario())


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_the_new_text_meets_contrast_in_both_themes(qapp, scheme) -> None:
    """Every new label sits on the inspector (bg-surface) or on the layer card
    (bg-sunken) and uses a style whose text clears 4.5:1 on both."""
    def luminance(color: str) -> float:
        r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    def contrast(a: str, b: str) -> float:
        hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    tokens = theme.TOKENS_BY_SCHEME[scheme]
    view = make_view(Server(), NUMBER_SOURCE, NUMBER_TARGET)
    names = {widget.objectName() for widget in (
        view.layers_title, view.l1_title, view.l1_note, view.l1_status, view.l2_overview, view.l3_title, view.l3_note,
        view.l2_title, view.l2_note, view.l2_covered, view.low_toggle, view.ambiguous_toggle, view.ambiguous_note,
        view.ambiguous_state)}
    colour = {"sectionTitle": "text-secondary", "statusText": "text-primary", "secondaryText": "text-secondary",
              "commandButton": "text-primary"}
    assert names <= set(colour), names - set(colour)
    sheet = theme.load_stylesheet(scheme)
    assert view.layers_card.objectName() == "reviewStatus" and "#reviewStatus" in sheet
    for name in names:
        for background in ("bg-surface", "bg-sunken"):
            assert contrast(tokens[colour[name]], tokens[background]) >= 4.5, (scheme, name, background)


@pytest.mark.parametrize("font_scale", [1.0, 1.25])
@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_every_layer_open_never_widens_the_inspector(qapp, scheme, font_scale) -> None:
    """All three layers filled, both folds open: nothing pushes the inspector
    into a horizontal scroll, with room to spare. Windows' CJK fonts are wider
    than Linux's (they once overflowed this by 32 px), so the test also runs
    with every font a quarter larger and asks for 40 px of headroom."""
    import re

    from PySide6.QtWidgets import QScrollArea

    async def scenario():
        server = Server(capabilities=(AMB, "semantic-review-v1", STRUCTURE), issues=layered_issues)
        source, target = NUMBER_SOURCE + "椿在城楼。椿点头。", NUMBER_TARGET + " Camellya waited. She nodded."
        view = make_view(server, source, target)
        view.resize(1100, 700)
        view.show()
        view._start_review()
        await settle(view)
        view.l1_run_button.click()
        await settle(view)
        view.l3_check.setChecked(True)
        view.ambiguous_toggle.setChecked(True)
        view.ambiguous_list.setCurrentRow(0)
        view.low_toggle.setChecked(True)
        scroll = view.inspector.findChild(QScrollArea)
        for track in (0, TRACK_SEMANTIC, 2):
            view._show_track(track)
            for _ in range(3):
                QApplication.processEvents()
            assert view.l1_list.count() and view.ambiguous_list.count() and view._issue_rows
            assert scroll.horizontalScrollBar().maximum() == 0, (scheme, font_scale, track)
            room = scroll.viewport().width() - scroll.widget().minimumSizeHint().width()
            assert room >= 40, (scheme, font_scale, track, room)
        # Whole rows only: the Layer 1 list is exactly as tall as its first three rows.
        rows = sum(view.l1_list.sizeHintForRow(i) for i in range(min(3, view.l1_list.count())))
        assert view.l1_list.maximumHeight() == max(view.l1_list.minimumHeight(), rows + 2 * view.l1_list.frameWidth())
        # A button short of room keeps its whole text for readers.
        assert view.terms_track_button.accessibleName() == view.terms_track_button.text()
        view._saved_payload = view._payload
        view.close()

    previous_sheet, previous_font = qapp.styleSheet(), qapp.font()
    sheet = re.sub(r"(\d+(?:\.\d+)?)pt", lambda m: f"{float(m.group(1)) * font_scale:.1f}pt",
                   theme.load_stylesheet(scheme))
    font = qapp.font()
    font.setPointSizeF(font.pointSizeF() * font_scale)
    qapp.setFont(font)
    qapp.setStyleSheet(sheet)
    try:
        asyncio.run(scenario())
    finally:
        qapp.setStyleSheet(previous_sheet)
        qapp.setFont(previous_font)


def test_an_eliding_button_shows_its_whole_text_when_there_is_room(qapp) -> None:
    from wuwaterm_client.ui.manuscript_view import _ElidingButton
    button = _ElidingButton("下一处待处理")
    assert button.minimumSizeHint().width() < button.sizeHint().width()
    button.resize(button.sizeHint())
    button.show()
    QApplication.processEvents()
    image = button.grab()  # paints the whole text without eliding
    assert not image.isNull() and button.text() == "下一处待处理"
    button.resize(button.minimumSizeHint().width(), button.sizeHint().height())
    QApplication.processEvents()
    assert not button.grab().isNull() and button.text() == "下一处待处理"
    button.close()
