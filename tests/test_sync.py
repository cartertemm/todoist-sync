import datetime
from dataclasses import replace

import pytest
import requests

from fakes import HOME, WORK, FakeTodoist
from mapping import Record
from sync import DeleteGuardError, due_payload, run_sync
from todoist import TodoistError
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


def test_recurring_completion_from_excel_adds_history_row_once():
	fake = FakeTodoist()
	fake.add("Water plants", due=dict(WEEKLY))
	first = sync(fake)
	second = sync(fake, edit(first, status="Complete"), first.state)
	third = sync(fake, second.rows, second.state)
	assert third.rows == second.rows


def test_failed_run_keeps_last_sync_so_history_row_is_not_lost():
	fake = FakeTodoist()
	fake.add("Other")
	recurring_id = fake.add("Water plants", due=dict(WEEKLY))
	first = sync(fake)
	fake.tasks[recurring_id]["due"]["date"] = "2026-10-16"
	fake.completed.append({"id": recurring_id})
	later = NOW + datetime.timedelta(hours=1)
	fake.fail_on = "update_task"
	failed = run_sync(edit(first, content="Other 2"), fake, first.state, OWNER, later)
	assert failed.error is not None
	assert failed.state["last_sync"] == NOW.isoformat()
	fake.fail_on = None
	retry = run_sync(failed.rows, fake, failed.state, OWNER, later)
	assert retry.error is None
	assert f"{recurring_id}:done" in [row.id for row in retry.rows]


def test_task_gone_after_push_removes_row():
	fake = FakeTodoist()
	fake.add("Task")
	first = sync(fake)
	fake.get_task = lambda task_id: None
	result = sync(fake, edit(first, content="Renamed"), first.state)
	assert result.rows == []
	assert result.state["tasks"] == {}
	assert any("removed row" in line for line in result.log)


def test_failed_push_does_not_mark_completion_handled():
	fake = FakeTodoist()
	task_id = fake.add("Water plants", due=dict(WEEKLY))
	first = sync(fake)
	fake.tasks[task_id]["due"]["date"] = "2026-10-16"
	fake.completed.append({"id": task_id})
	later = NOW + datetime.timedelta(hours=1)
	fake.fail_on = "update_task"
	failed = run_sync(edit(first, content="Renamed"), fake, first.state, OWNER, later)
	assert failed.error is not None
	assert [row.id for row in failed.rows] == [task_id]
	assert failed.state["handled_completions"] == []
	fake.fail_on = None
	retry = run_sync(failed.rows, fake, failed.state, OWNER, later)
	assert [row.id for row in retry.rows] == [task_id, f"{task_id}:done"]


def test_recurring_close_is_not_repeated_after_failed_refetch():
	fake = FakeTodoist()
	task_id = fake.add("Water plants", due=dict(WEEKLY))
	first = sync(fake)
	later = NOW + datetime.timedelta(hours=1)

	def refuse(_):
		raise requests.RequestException("get_task failed")

	fake.get_task = refuse
	failed = run_sync(edit(first, status="Complete"), fake, first.state, OWNER, later)
	assert failed.error is not None
	del fake.get_task
	retry = run_sync(failed.rows, fake, failed.state, OWNER, later)
	assert retry.error is None
	assert [call for call in fake.calls if call[0] == "close_task"] == [("close_task", task_id)]
	assert fake.tasks[task_id]["due"]["date"] == "2026-10-16"


def test_clean_run_saves_only_its_own_handled_completions():
	fake = FakeTodoist()
	fake.add("Task")
	first = sync(fake)
	state = {**first.state, "handled_completions": ["old"]}
	assert sync(fake, first.rows, state).state["handled_completions"] == []


def hide_completed(fake):
	read = fake.get_task
	fake.get_task = lambda task_id: None if fake.tasks.get(task_id, {}).get("checked") else read(task_id)


def test_new_row_links_to_task_with_same_name_and_project():
	fake = FakeTodoist()
	task_id = fake.add("Write report")
	result = sync(fake, [Row(None, record("Write report", notes="From Excel"))])
	assert result.rows == [Row(task_id, record("Write report"))]
	assert not any(call[0] == "create_task" for call in fake.calls)
	assert "linked row to existing task: Write report" in result.log


def test_new_row_is_not_linked_to_task_in_other_project():
	fake = FakeTodoist()
	fake.add("Write report", project_id="p-home")
	result = sync(fake, [Row(None, record("Write report"))])
	assert len(fake.tasks) == 2
	assert len(result.rows) == 2


def test_each_task_is_linked_once():
	fake = FakeTodoist()
	fake.add("Write report")
	sync(fake, [Row(None, record("Write report")), Row(None, record("Write report"))])
	assert len(fake.tasks) == 2


def test_synced_task_is_not_linked_again():
	fake = FakeTodoist()
	fake.add("Write report")
	first = sync(fake)
	result = sync(fake, first.rows + [Row(None, record("Write report"))], first.state)
	assert len(fake.tasks) == 2
	assert len({row.id for row in result.rows}) == 2


def test_client_error_skips_row_and_continues():
	fake = FakeTodoist()
	fake.add("A")
	b_id = fake.add("B")
	first = sync(fake)
	fake.add("C")
	rows = edit(first, 0, content="A2")
	rows[1] = Row(rows[1].id, replace(rows[1].record, project="Home"))
	fake.fail_on = "update_task"
	fake.fail_status = 400
	result = sync(fake, rows, first.state)
	assert result.error is None
	assert result.rows[0] == rows[0]
	assert result.state["tasks"][rows[0].id] == first.state["tasks"][rows[0].id]
	assert fake.tasks[b_id]["project_id"] == "p-home"
	assert [row.record.content for row in result.rows] == ["A2", "B", "C"]
	assert "row skipped, Todoist said 400: A2" in result.log


def test_rate_limit_still_stops_run():
	fake = FakeTodoist()
	fake.add("A")
	first = sync(fake)
	fake.fail_on = "update_task"
	fake.fail_status = 429
	result = sync(fake, edit(first, content="A2"), first.state)
	assert result.error is not None


def test_rejected_due_change_skips_row():
	fake = FakeTodoist()
	fake.add("A")
	fake.add("B")
	first = sync(fake)

	def reject(task_id, due):
		raise TodoistError("Could not set due date")

	fake.set_due = reject
	rows = edit(first, 0, repeat="every blursday")
	rows[1] = Row(rows[1].id, replace(rows[1].record, content="B2"))
	result = sync(fake, rows, first.state)
	assert result.error is None
	assert result.rows[0] == rows[0]
	assert result.rows[1].record.content == "B2"


def test_blank_task_name_with_id_is_skipped():
	fake = FakeTodoist()
	task_id = fake.add("Task")
	first = sync(fake)
	result = sync(fake, edit(first, content=""), first.state)
	assert not any(call[0] == "update_task" for call in fake.calls)
	assert result.rows[0].record.content == ""
	assert result.state["tasks"][task_id] == first.state["tasks"][task_id]
	assert any("blank" in line for line in result.log)


def test_copied_row_becomes_new_task():
	fake = FakeTodoist()
	task_id = fake.add("Task")
	first = sync(fake)
	copy = Row(task_id, record("Task copy"))
	result = sync(fake, first.rows + [copy], first.state)
	assert len(fake.tasks) == 2
	assert fake.tasks[task_id]["content"] == "Task"
	assert result.rows[1].id not in (None, task_id)
	assert any("copied row" in line for line in result.log)


def test_history_rows_with_same_id_are_kept():
	fake = FakeTodoist()
	fake.add("Task")
	first = sync(fake)
	history = [Row("t1:done", record("Run 1", status="Complete")), Row("t1:done", record("Run 2", status="Complete"))]
	result = sync(fake, first.rows + history, first.state)
	assert result.rows[1:] == history
	assert len(fake.tasks) == 1


def test_completed_task_that_cannot_be_read_stays_complete():
	fake = FakeTodoist()
	task_id = fake.add("One-off")
	first = sync(fake)
	fake.tasks[task_id]["checked"] = True
	fake.completed.append({"id": task_id})
	hide_completed(fake)
	result = sync(fake, first.rows, first.state)
	assert result.rows[0].record.status == "Complete"
	assert result.state["tasks"][task_id]["status"] == "Complete"
	assert sync(fake, result.rows, result.state).rows == result.rows


def test_closed_task_that_cannot_be_read_stays_complete():
	fake = FakeTodoist()
	task_id = fake.add("One-off")
	first = sync(fake)
	hide_completed(fake)
	result = sync(fake, edit(first, status="Complete"), first.state)
	assert fake.tasks[task_id]["checked"]
	assert result.rows[0].id == task_id
	assert result.rows[0].record.status == "Complete"
	assert result.state["tasks"][task_id]["status"] == "Complete"


def test_missing_inbox_skips_rows_without_project():
	fake = FakeTodoist()
	fake.project_list = [WORK, HOME]
	row = Row(None, record("Loose end", project=""))
	result = sync(fake, [row])
	assert result.rows == [row]
	assert fake.tasks == {}


def test_extra_columns_follow_the_row():
	fake = FakeTodoist()
	fake.add("Water plants", due=dict(WEEKLY))
	first = sync(fake)
	rows = [replace(row, extra={12: "Garden"}) for row in edit(first, status="Complete")]
	result = sync(fake, rows, first.state)
	assert [row.extra for row in result.rows] == [{12: "Garden"}, {12: "Garden"}]


def test_complete_row_without_id_stays_in_excel_only():
	fake = FakeTodoist()
	fake.add("Old job")
	rows = [Row(None, record("Old job", status="Complete")), Row(None, record("Older job", status="Complete"))]
	result = sync(fake, rows)
	assert result.rows[:2] == rows
	assert len(fake.tasks) == 1
	assert not any(call[0] in ("create_task", "close_task") for call in fake.calls)
	assert set(result.state["tasks"]) == {"t1"}
	assert "kept 2 Complete rows without an ID in Excel only" in result.log


def test_copy_above_original_becomes_the_new_task():
	fake = FakeTodoist()
	task_id = fake.add("Task")
	first = sync(fake)
	copy = Row(task_id, record("Task copy"))
	result = sync(fake, [copy] + first.rows, first.state)
	assert fake.tasks[task_id]["content"] == "Task"
	assert not any(call[0] == "update_task" for call in fake.calls)
	assert result.rows[1] == first.rows[0]
	assert result.rows[0].id not in (None, task_id)
	assert fake.tasks[result.rows[0].id]["content"] == "Task copy"
