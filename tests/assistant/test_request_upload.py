"""A request's materials from an uploaded Excel template (UAT, 22 Oct).

The department is not in the file (TL, 20 Nov): it is picked on the request
itself, and a leftover ``Department`` column is ignored like any other.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app.assistant.request_upload import HEADERS, SHEET, TEMPLATE_COLUMNS, UploadError, build_template, extract
from app.core.config import get_settings

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook(*rows, header=HEADERS, sheet=SHEET, extra_sheet=False) -> bytes:
    book = Workbook()
    ws = book.active
    ws.title = sheet
    if header is not None:
        ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    if extra_sheet:
        book.create_sheet("Notes")
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def errors_of(content: bytes, **kwargs) -> list[str]:
    with pytest.raises(UploadError) as raised:
        extract(content, max_lines=kwargs.pop("max_lines", 20), **kwargs)
    return raised.value.errors


class TestExtract:
    def test_reads_the_materials_of_one_plant(self) -> None:
        got = extract(workbook(("1000000123", "1300"), ("1000000456", "1300")), max_lines=20)
        assert (got.plant, got.materials, got.row_count) == ("1300", ["1000000123", "1000000456"], 2)

    def test_number_cells_read_as_typed(self) -> None:
        got = extract(workbook((1000000123, 1300), (1000000456.0, 1300.0)), max_lines=20)
        assert got.materials == ["1000000123", "1000000456"]
        assert got.plant == "1300"

    def test_skips_blank_rows_and_matches_headers_in_any_case(self) -> None:
        content = workbook(
            ("1000000123", "1300"), (None, None), ("1000000456", "1300"), header=("MATERIAL", " plant "),
        )
        assert extract(content, max_lines=20).row_count == 2

    def test_merges_duplicates_leading_zeros_or_not(self) -> None:
        got = extract(workbook(("1000000123", "1300"), ("0000000001000000123", "1300")), max_lines=20)
        assert got.materials == ["1000000123"]
        assert got.duplicates == ["0000000001000000123"]

    def test_reads_headers_with_or_without_the_required_hint(self) -> None:
        content = workbook(("1000000123", "1300"), header=("Material (required)", "Plant*"))
        assert extract(content, max_lines=20).materials == ["1000000123"]

    def test_names_the_missing_columns(self) -> None:
        assert "Missing: Plant" in errors_of(workbook(("1",), header=("Material",)))[0]

    def test_refuses_an_empty_file_or_one_with_headers_only(self) -> None:
        assert errors_of(workbook(header=None)) == ["The file is empty."]
        assert errors_of(workbook()) == ["The file has the headers but no materials."]

    def test_lists_rows_with_an_empty_cell(self) -> None:
        errors = errors_of(workbook(("1000000123", None), (None, "1300")))
        assert "Material is empty on row 3." in errors
        assert "Plant is empty on row 2." in errors

    def test_refuses_more_than_one_plant(self) -> None:
        errors = errors_of(workbook(("1000000123", "1300"), ("1000000934", "1500")))
        assert errors == [
            "The file has more than one plant (1300, 1500). One request covers one plant: upload a file per plant."
        ]

    def test_refuses_a_plant_out_of_scope(self) -> None:
        assert errors_of(workbook(("1000000123", "9999")))[0].startswith("Plant 9999 is not")

    def test_an_older_template_with_a_department_column_still_uploads(self) -> None:
        """The department is picked on the request now, so the column is ignored
        whatever it holds -- two departments here, one of them not even a code."""
        content = workbook(
            ("1000000123", "1300", "10303"),
            ("1000000456", "1300", "Mill crew"),
            header=("Material", "Plant", "Department"),
        )
        got = extract(content, max_lines=20)
        assert (got.plant, got.materials) == ("1300", ["1000000123", "1000000456"])

    def test_refuses_more_than_the_cap_rather_than_cutting_off(self) -> None:
        rows = [(str(1000000100 + i), "1300") for i in range(21)]
        assert "21 materials: a request holds up to 20" in errors_of(workbook(*rows))[0]

    def test_refuses_a_cell_that_is_not_a_material_number(self) -> None:
        assert errors_of(workbook(("pump seal", "1300"))) == ["Material is not a material number on row 2."]

    def test_reports_every_problem_at_once(self) -> None:
        errors = errors_of(workbook(("1000000123", "1300"), ("", "1500")))
        assert len(errors) == 2  # empty material, two plants

    def test_refuses_a_file_that_is_not_xlsx(self) -> None:
        assert ".xls files cannot be read" in errors_of(b"old", file_name="list.xls")[0]
        assert "is not an .xlsx file" in errors_of(b"a,b", file_name="list.csv")[0]
        assert "could not be read as an Excel workbook" in errors_of(b"not a zip", file_name="list.xlsx")[0]

    def test_needs_the_materials_sheet_when_there_are_several(self) -> None:
        content = workbook(("1000000123", "1300"), sheet="Sheet1", extra_sheet=True)
        assert "no 'Materials' sheet" in errors_of(content)[0]
        # The only sheet, renamed, is still the list.
        assert extract(workbook(("1000000123", "1300"), sheet="Sheet1"), max_lines=20).materials

    def test_refuses_a_file_over_one_megabyte(self) -> None:
        assert "larger than 1 MB" in errors_of(b"x" * (1024 * 1024 + 1))[0]


class TestTemplate:
    def test_has_the_headers_text_columns_and_instructions(self) -> None:
        book = load_workbook(io.BytesIO(build_template(20)))
        sheet = book[SHEET]
        assert [c.value for c in sheet[1]] == [
            "Material (required)", "Material name (optional)", "Plant (required)",
        ]
        assert sheet["A1"].font.bold and not sheet["B1"].font.bold
        assert list(TEMPLATE_COLUMNS) == [c.value for c in sheet[1]]
        assert sheet["A2"].number_format == "@"
        notes = [row[0] or "" for row in book["How to fill"].iter_rows(values_only=True)]
        assert any("Up to 20 materials" in line for line in notes)
        assert any("department is not in the file" in line for line in notes)

    def test_filled_in_it_is_a_valid_upload(self) -> None:
        book = load_workbook(io.BytesIO(build_template(20)))
        book[SHEET].append(["000000001000000123", "BEARING ASSY PUMP HR250", "1300"])
        book[SHEET].append(["1000000456", None, "1300"])
        buffer = io.BytesIO()
        book.save(buffer)
        got = extract(buffer.getvalue(), max_lines=20, file_name="filled.xlsx")
        assert got.materials == ["000000001000000123", "1000000456"]

    def test_other_columns_are_the_requesters_own_and_ignored(self) -> None:
        content = workbook(
            ("Pump, 6/4 Warman", "1000000123", "Mill 3", "1300"),
            header=("Description", "Material", "Notes", "Plant"),
        )
        assert extract(content, max_lines=20).materials == ["1000000123"]


class TestRoutes:
    @pytest.fixture
    def settings(self):
        return get_settings().model_copy(
            update={"assistant_requests_enabled": True, "assistant_request_max_lines": 20}
        )

    @pytest.fixture
    def client(self, settings):
        from app.main import create_app

        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        yield TestClient(app)  # no `with`: the lifespan would reach the real database
        app.dependency_overrides.clear()

    def test_template_downloads(self, client) -> None:
        response = client.get("/api/assistant/requests/upload-template")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == XLSX
        assert "material-request-template.xlsx" in response.headers["content-disposition"]
        assert load_workbook(io.BytesIO(response.content))[SHEET]["A1"].value == "Material (required)"

    def test_upload_returns_the_materials(self, client) -> None:
        content = workbook(("1000000123", "1300"), ("1000000123", "1300"))
        response = client.post(
            "/api/assistant/requests/upload", files={"file": ("list.xlsx", content, XLSX)}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "plant": "1300",
            "materials": ["1000000123"],
            "rowCount": 2,
            "duplicates": ["1000000123"],
            "maxLines": 20,
        }

    def test_upload_lists_every_problem_as_422(self, client) -> None:
        content = workbook(("1000000123", "1300"), ("", "1500"))
        response = client.post(
            "/api/assistant/requests/upload", files={"file": ("list.xlsx", content, XLSX)}
        )
        assert response.status_code == 422
        assert len(response.json()["detail"]["errors"]) == 2

    def test_off_with_the_requests_switch(self, client, settings) -> None:
        client.app.dependency_overrides[get_settings] = lambda: settings.model_copy(
            update={"assistant_requests_enabled": False}
        )
        assert client.get("/api/assistant/requests/upload-template").status_code == 404
        response = client.post(
            "/api/assistant/requests/upload", files={"file": ("list.xlsx", workbook(), XLSX)}
        )
        assert response.status_code == 404
