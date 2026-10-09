from mapping import (
	Record,
	build_description,
	from_api_priority,
	labels_for,
	normalize_status,
	normalize_who,
	parse_description,
	task_to_record,
	to_api_priority,
)

OWNER = "Carter"
PROJECTS = {"p-work": "Work"}


def make_task(**changes):
	task = {
		"id": "t1",
		"content": "Write report",
		"description": "",
		"project_id": "p-work",
		"labels": [],
		"priority": 1,
		"due": None,
		"checked": False,
	}
	task.update(changes)
	return task


def test_normalize_status_ignores_case_and_defaults_to_todo():
	assert normalize_status("blocked") == "Blocked"
	assert normalize_status(" COMPLETE ") == "Complete"
	assert normalize_status(None) == "Todo"
	assert normalize_status("whatever") == "Todo"


def test_normalize_who_treats_owner_and_blank_as_none():
	assert normalize_who("carter", OWNER) is None
	assert normalize_who("  ", OWNER) is None
	assert normalize_who(None, OWNER) is None
	assert normalize_who(" Jane ", OWNER) == "Jane"


def test_description_round_trip():
	text = build_description("2026-10-12", "Ask about budget\nSecond line")
	assert text == "Follow-up on: 2026-10-12\nAsk about budget\nSecond line"
	assert parse_description(text) == ("2026-10-12", "Ask about budget\nSecond line")


def test_description_without_follow_up():
	assert build_description(None, "Just notes") == "Just notes"
	assert parse_description("Just notes") == (None, "Just notes")
	assert parse_description(None) == (None, "")


def test_priority_conversion_is_reversed():
	assert to_api_priority(1) == 4
	assert from_api_priority(4) == 1
	assert from_api_priority(1) == 4


def test_task_to_record_plain_task():
	assert task_to_record(make_task(), PROJECTS, OWNER) == Record(
		content="Write report",
		project="Work",
		due=None,
		status="Todo",
		who=None,
		follow_up=None,
		notes="",
		repeat=None,
		priority=4,
	)


def test_task_to_record_reads_labels_due_and_description():
	task = make_task(
		labels=["errand", "blocked", "who-Jane"],
		priority=4,
		description="Follow-up on: 2026-10-12\nAsk about budget",
		due={"date": "2026-10-09T10:00:00", "string": "every friday at 10am", "is_recurring": True},
	)
	record = task_to_record(task, PROJECTS, OWNER)
	assert record.status == "Blocked"
	assert record.who == "Jane"
	assert record.priority == 1
	assert record.due == "2026-10-09"
	assert record.repeat == "every friday at 10am"
	assert record.follow_up == "2026-10-12"
	assert record.notes == "Ask about budget"


def test_task_to_record_checked_is_complete_and_non_recurring_has_no_repeat():
	task = make_task(checked=True, due={"date": "2026-10-09", "string": "Oct 9", "is_recurring": False})
	record = task_to_record(task, PROJECTS, OWNER)
	assert record.status == "Complete"
	assert record.repeat is None


def test_task_to_record_owner_label_counts_as_no_who():
	record = task_to_record(make_task(labels=["who-Carter"]), PROJECTS, OWNER)
	assert record.who is None


def test_labels_for_keeps_other_labels():
	record = task_to_record(make_task(), PROJECTS, OWNER)
	blocked = Record(**{**record.__dict__, "status": "Blocked", "who": "Jane"})
	assert labels_for(blocked, ["errand", "blocked", "who-Bob"]) == ["errand", "blocked", "who-Jane"]
	assert labels_for(record, ["errand", "blocked", "who-Bob"]) == ["errand"]
