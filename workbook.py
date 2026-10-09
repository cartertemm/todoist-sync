import datetime
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from mapping import DATE_LENGTH, DONE_SUFFIX, STATUSES, Record, normalize_status, normalize_who

TASKS_SHEET = "Tasks"
PROJECTS_SHEET = "Projects"
HEADERS = ("Task", "Project", "Start date", "Due date", "Status", "Who", "Follow-up on", "Notes", "Repeat", "Priority", "Link", "ID")
PRIORITIES = ("P1", "P2", "P3", "P4")
LOWEST_PRIORITY = 4
FIRST_DATA_ROW = 2
VALIDATION_LAST_ROW = 5000
DATE_FORMAT = "yyyy-mm-dd"
TASK_URL = "https://app.todoist.com/app/task/{}"
LINK_TEXT = "Open"
BACKUP_STAMP = "%Y%m%d-%H%M%S-%f"


@dataclass
class Row:
	id: str | None
	record: Record
	start_date: Any = None


class WorkbookError(Exception):
	pass


def is_open(path):
	path = Path(path)
	lock_names = ("~$" + path.name, "~$" + path.name[2:], f".~lock.{path.name}#")
	if any((path.parent / name).exists() for name in lock_names):
		return True
	if not path.exists():
		return False
	try:
		with open(path, "r+b"):
			return False
	except PermissionError:
		return True


def load_rows(path, owner):
	sheet = load_workbook(path)[TASKS_SHEET]
	columns = _columns(sheet)
	rows = []
	for values in sheet.iter_rows(min_row=FIRST_DATA_ROW, values_only=True):
		if all(value in (None, "") for value in values):
			continue
		cell = {header: values[index] if index < len(values) else None for header, index in columns.items()}
		record = Record(
			content=_text(cell["Task"]),
			project=_text(cell["Project"]),
			due=_to_iso(cell["Due date"]),
			status=normalize_status(cell["Status"]),
			who=normalize_who(_text(cell["Who"]), owner),
			follow_up=_to_iso(cell["Follow-up on"]),
			notes=_text(cell["Notes"]),
			repeat=_text(cell["Repeat"]) or None,
			priority=_priority(cell["Priority"]),
		)
		rows.append(Row(_text(cell["ID"]) or None, record, cell["Start date"]))
	return rows


def save_rows(path, rows, project_names, owner):
	path = Path(path)
	if path.exists():
		book = load_workbook(path)
		sheet = book[TASKS_SHEET]
	else:
		book = Workbook()
		sheet = book.active
		sheet.title = TASKS_SHEET
		sheet.append(HEADERS)
	columns = _columns(sheet)
	if sheet.max_row >= FIRST_DATA_ROW:
		sheet.delete_rows(FIRST_DATA_ROW, sheet.max_row - FIRST_DATA_ROW + 1)
	for row_number, row in enumerate(rows, start=FIRST_DATA_ROW):
		_write_row(sheet, row_number, columns, row, owner)
	_write_projects(book, project_names)
	_add_dropdowns(sheet, columns, len(project_names))
	sheet.column_dimensions[_letter(columns, "ID")].hidden = True
	book.save(path)


def backup(path, backup_dir, keep):
	path = Path(path)
	if not path.exists():
		return
	backup_dir = Path(backup_dir)
	backup_dir.mkdir(exist_ok=True)
	stamp = datetime.datetime.now().strftime(BACKUP_STAMP)
	shutil.copy2(path, backup_dir / f"{path.stem}-{stamp}{path.suffix}")
	for old in sorted(backup_dir.glob(f"{path.stem}-*{path.suffix}"))[:-keep]:
		old.unlink()


def _columns(sheet):
	header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
	columns = {name: index for index, name in enumerate(header_row) if name in HEADERS}
	missing = [header for header in HEADERS if header not in columns]
	if missing:
		raise WorkbookError(f"Missing columns in {TASKS_SHEET}: {', '.join(missing)}")
	return columns


def _letter(columns, header):
	return get_column_letter(columns[header] + 1)


def _write_row(sheet, row_number, columns, row, owner):
	record = row.record
	values = {
		"Task": record.content,
		"Project": record.project,
		"Start date": row.start_date,
		"Due date": _to_date(record.due),
		"Status": record.status,
		"Who": record.who or owner,
		"Follow-up on": _to_date(record.follow_up),
		"Notes": record.notes,
		"Repeat": record.repeat,
		"Priority": f"P{record.priority}",
		"Link": None,
		"ID": row.id,
	}
	for header, value in values.items():
		cell = sheet.cell(row=row_number, column=columns[header] + 1, value=value)
		if isinstance(value, datetime.date):
			cell.number_format = DATE_FORMAT
	if row.id:
		link = sheet.cell(row=row_number, column=columns["Link"] + 1, value=LINK_TEXT)
		link.hyperlink = TASK_URL.format(row.id.removesuffix(DONE_SUFFIX))
		link.style = "Hyperlink"


def _write_projects(book, project_names):
	if PROJECTS_SHEET in book.sheetnames:
		del book[PROJECTS_SHEET]
	sheet = book.create_sheet(PROJECTS_SHEET)
	for name in project_names:
		sheet.append([name])
	sheet.sheet_state = "hidden"


def _add_dropdowns(sheet, columns, project_count):
	sheet.data_validations.dataValidation = []
	lists = {
		"Project": f"{PROJECTS_SHEET}!$A$1:$A${max(project_count, 1)}",
		"Status": '"' + ",".join(STATUSES) + '"',
		"Priority": '"' + ",".join(PRIORITIES) + '"',
	}
	for header, formula in lists.items():
		validation = DataValidation(type="list", formula1=formula, allow_blank=True)
		letter = _letter(columns, header)
		validation.add(f"{letter}{FIRST_DATA_ROW}:{letter}{VALIDATION_LAST_ROW}")
		sheet.add_data_validation(validation)


def _text(value):
	return "" if value is None else str(value).strip()


def _to_iso(value):
	if isinstance(value, datetime.datetime):
		return value.date().isoformat()
	if isinstance(value, datetime.date):
		return value.isoformat()
	return _text(value)[:DATE_LENGTH] or None


def _to_date(iso):
	return datetime.date.fromisoformat(iso) if iso else None


def _priority(value):
	text = _text(value).upper()
	return PRIORITIES.index(text) + 1 if text in PRIORITIES else LOWEST_PRIORITY
