import datetime
from dataclasses import replace

import pytest

from fakes import FakeTodoist
from mapping import Record
from sync import DeleteGuardError, due_payload, run_sync
from workbook import Row

NOW = datetime.datetime(2026, 10, 9, 12, 0, tzinfo=datetime.timezone.utc)
OWNER = "Carter"
WEEKLY = {"date": "2026-10-09", "string": "every week", "is_recurring": True}


def record(content, **changes):
	base = Record(
		content=content,
		project="Work",
		due=None,
		status="Todo",
		who=None,
		follow_up=None,
		notes="",
		repeat=None,
		priority=4,
	)
	return replace(base, **changes)


def sync(fake, rows=None, state=None, **kwargs):
	return run_sync(rows or [], fake, state or {}, OWNER, NOW, **kwargs)


def edit(result, index=0, **changes):
	rows = list(result.rows)
	rows[index] = Row(rows[index].id, replace(rows[index].record, **changes), rows[index].start_date)
	return rows


def test_first_run_builds_rows_from_open_tasks():
	fake = FakeTodoist()
	task_id = fake.add("Write report")
	result = sync(fake)
	assert result.rows == [Row(task_id, record("Write report"))]
	assert result.state["tasks"][task_id]["content"] == "Write report"
	assert result.state["last_sync"] == NOW.isoformat()
	assert result.project_names == ["Inbox", "Work", "Home"]


def test_row_without_id_creates_task():
	fake = FakeTodoist()
	result = sync(fake, [Row(None, record("New thing", status="Blocked", who="Jane"))])
	task_id = result.rows[0].id
	assert fake.tasks[task_id]["labels"] == ["blocked", "who-Jane"]
	assert fake.tasks[task_id]["project_id"] == "p-work"
	assert task_id in result.state["tasks"]


def test_row_without_project_goes_to_inbox():
	fake = FakeTodoist()
	result = sync(fake, [Row(None, record("Loose end", project=""))])
	assert fake.tasks[result.rows[0].id]["project_id"] == "p-inbox"
	assert result.rows[0].record.project == "Inbox"


def test_new_row_with_unknown_project_is_left_alone():
	fake = FakeTodoist()
	row = Row(None, record("Mystery", project="Nope"))
	result = sync(fake, [row])
	assert result.rows == [row]
	assert fake.tasks == {}


def test_excel_edit_is_pushed_to_todoist():
	fake = FakeTodoist()
	task_id = fake.add("Old name")
	first = sync(fake)
	result = sync(fake, edit(first, content="New name"), first.state)
	assert fake.tasks[task_id]["content"] == "New name"
	assert result.rows[0].record.content == "New name"


def test_todoist_edit_updates_row():
	fake = FakeTodoist()
	task_id = fake.add("Old name")
	first = sync(fake)
	fake.tasks[task_id]["content"] = "Changed"
	result = sync(fake, first.rows, first.state)
	assert result.rows[0].record.content == "Changed"


def test_todoist_wins_when_both_sides_change_same_field():
	fake = FakeTodoist()
	task_id = fake.add("Original")
	first = sync(fake)
	fake.tasks[task_id]["content"] = "From Todoist"
	result = sync(fake, edit(first, content="From Excel"), first.state)
	assert result.rows[0].record.content == "From Todoist"
	assert fake.tasks[task_id]["content"] == "From Todoist"


def test_different_fields_merge_from_both_sides():
	fake = FakeTodoist()
	task_id = fake.add("Original")
	first = sync(fake)
	fake.tasks[task_id]["content"] = "From Todoist"
	result = sync(fake, edit(first, notes="From Excel"), first.state)
	assert result.rows[0].record.content == "From Todoist"
	assert result.rows[0].record.notes == "From Excel"
	assert fake.tasks[task_id]["description"] == "From Excel"


def test_status_and_who_keep_other_labels():
	fake = FakeTodoist()
	task_id = fake.add("Task", labels=["errand"])
	first = sync(fake)
	sync(fake, edit(first, status="Blocked", who="Jane"), first.state)
	assert fake.tasks[task_id]["labels"] == ["errand", "blocked", "who-Jane"]


def test_project_change_moves_task():
	fake = FakeTodoist()
	task_id = fake.add("Task")
	first = sync(fake)
	result = sync(fake, edit(first, project="Home"), first.state)
	assert fake.tasks[task_id]["project_id"] == "p-home"
	assert result.rows[0].record.project == "Home"


def test_unknown_project_change_is_skipped():
	fake = FakeTodoist()
	fake.add("Task")
	first = sync(fake)
	result = sync(fake, edit(first, project="Nope"), first.state)
	assert result.rows[0].record.project == "Work"
	assert not any(call[0] == "move_task" for call in fake.calls)
	assert any("Nope" in line for line in result.log)


def test_repeat_change_wins_over_due_change():
	fake = FakeTodoist()
	task_id = fake.add("Water plants", due=dict(WEEKLY))
	first = sync(fake)
	sync(fake, edit(first, repeat="every day", due="2026-11-01"), first.state)
	assert ("set_due", task_id, {"string": "every day"}) in fake.calls


def test_due_payload_keeps_time_and_recurrence():
	task = {"due": {"date": "2026-10-09T10:00:00", "string": "every friday at 10am", "is_recurring": True}}
	assert due_payload(record("x", due="2026-10-16"), task, repeat_changed=False) == {
		"date": "2026-10-16T10:00:00",
		"string": "every friday at 10am",
	}


def test_due_payload_clearing_repeat_keeps_date_only():
	task = {"due": {"date": "2026-10-09", "string": "every friday", "is_recurring": True}}
	assert due_payload(record("x", due="2026-10-09"), task, repeat_changed=True) == {"date": "2026-10-09"}


def test_due_payload_clearing_due():
	assert due_payload(record("x"), {"due": None}, repeat_changed=False) is None


def test_row_deleted_in_excel_deletes_task():
	fake = FakeTodoist()
	ids = [fake.add(f"Task {number}") for number in range(4)]
	first = sync(fake)
	result = sync(fake, first.rows[1:], first.state)
	assert fake.deleted == [ids[0]]
	assert ids[0] not in result.state["tasks"]


def test_task_deleted_in_todoist_removes_row():
	fake = FakeTodoist()
	ids = [fake.add(f"Task {number}") for number in range(4)]
	first = sync(fake)
	del fake.tasks[ids[0]]
	result = sync(fake, first.rows, first.state)
	assert [row.id for row in result.rows] == ids[1:]
	assert ids[0] not in result.state["tasks"]


def test_mass_delete_stops_sync():
	fake = FakeTodoist()
	for number in range(4):
		fake.add(f"Task {number}")
	first = sync(fake)
	with pytest.raises(DeleteGuardError) as info:
		sync(fake, first.rows[2:], first.state)
	assert fake.deleted == []
	assert info.value.names == ["Task 0", "Task 1"]


def test_allow_deletes_skips_the_check():
	fake = FakeTodoist()
	for number in range(4):
		fake.add(f"Task {number}")
	first = sync(fake)
	sync(fake, first.rows[2:], first.state, allow_deletes=True)
	assert len(fake.deleted) == 2


def test_completing_in_excel_closes_task():
	fake = FakeTodoist()
	task_id = fake.add("One-off")
	first = sync(fake)
	second = sync(fake, edit(first, status="Complete"), first.state)
	assert fake.tasks[task_id]["checked"]
	assert second.rows[0].record.status == "Complete"
	third = sync(fake, second.rows, second.state)
	assert third.rows == second.rows


def test_completing_recurring_in_excel_adds_history_row():
	fake = FakeTodoist()
	task_id = fake.add("Water plants", due=dict(WEEKLY))
	first = sync(fake)
	result = sync(fake, edit(first, status="Complete"), first.state)
	assert [row.id for row in result.rows] == [task_id, f"{task_id}:done"]
	assert result.rows[0].record.status == "Todo"
	assert result.rows[0].record.due == "2026-10-16"
	assert result.rows[1].record == replace(first.rows[0].record, status="Complete", repeat=None)


def test_recurring_completion_in_todoist_adds_history_row():
	fake = FakeTodoist()
	task_id = fake.add("Water plants", due=dict(WEEKLY))
	first = sync(fake)
	fake.tasks[task_id]["due"]["date"] = "2026-10-16"
	fake.completed.append({"id": task_id})
	result = sync(fake, first.rows, first.state)
	assert [row.id for row in result.rows] == [task_id, f"{task_id}:done"]
	assert result.rows[0].record.due == "2026-10-16"
	assert result.rows[1].record.due == "2026-10-09"
	assert result.rows[1].record.status == "Complete"


def test_completion_in_todoist_marks_row_complete():
	fake = FakeTodoist()
	task_id = fake.add("One-off")
	first = sync(fake)
	fake.tasks[task_id]["checked"] = True
	result = sync(fake, first.rows, first.state)
	assert result.rows[0].record.status == "Complete"


def test_reopening_in_excel_reopens_task():
	fake = FakeTodoist()
	task_id = fake.add("One-off")
	first = sync(fake)
	second = sync(fake, edit(first, status="Complete"), first.state)
	third = sync(fake, edit(second, status="Todo"), second.state)
	assert not fake.tasks[task_id]["checked"]
	assert third.rows[0].record.status == "Todo"


def test_completed_row_removed_from_excel_is_not_deleted():
	fake = FakeTodoist()
	task_id = fake.add("One-off")
	first = sync(fake)
	second = sync(fake, edit(first, status="Complete"), first.state)
	third = sync(fake, [], second.state)
	assert fake.deleted == []
	assert task_id not in third.state["tasks"]


def test_history_rows_pass_through_untouched():
	fake = FakeTodoist()
	fake.add("Task")
	first = sync(fake)
	history = Row("t1:done", record("Old run", status="Complete"))
	result = sync(fake, first.rows + [history], first.state)
	assert result.rows[-1] == history


def test_missing_state_lets_todoist_win():
	fake = FakeTodoist()
	task_id = fake.add("Todoist name")
	result = sync(fake, [Row(task_id, record("Excel name"))], {})
	assert result.rows[0].record.content == "Todoist name"
	assert fake.tasks[task_id]["content"] == "Todoist name"


def test_api_error_keeps_unprocessed_rows():
	fake = FakeTodoist()
	fake.add("A")
	fake.add("B")
	first = sync(fake)
	rows = edit(first, 0, content="A2")
	rows[1] = Row(rows[1].id, replace(rows[1].record, content="B2"))
	fake.fail_on = "update_task"
	result = sync(fake, rows, first.state)
	assert result.error is not None
	assert result.rows == rows
	assert result.state["tasks"] == first.state["tasks"]


def test_api_error_after_create_keeps_new_id():
	fake = FakeTodoist()
	fake.add("A")
	first = sync(fake)
	rows = [Row(None, record("New"))] + edit(first, content="A2")
	fake.fail_on = "update_task"
	result = sync(fake, rows, first.state)
	assert result.error is not None
	assert result.rows[0].id is not None
	assert result.rows[0].id in result.state["tasks"]
