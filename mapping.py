import re
from dataclasses import dataclass, fields

STATUSES = ("Todo", "Complete", "Blocked")
BLOCKED_LABEL = "blocked"
WHO_PREFIX = "who-"
FOLLOW_UP_PREFIX = "Follow-up on:"
FOLLOW_UP_PATTERN = re.compile(r"^Follow-up on:\s*(\d{4}-\d{2}-\d{2})$")
API_PRIORITY_SUM = 5
DATE_LENGTH = len("YYYY-MM-DD")
DONE_SUFFIX = ":done"


@dataclass(frozen=True)
class Record:
	content: str
	project: str
	due: str | None
	status: str
	who: str | None
	follow_up: str | None
	notes: str
	repeat: str | None
	priority: int


FIELDS = tuple(field.name for field in fields(Record))


def normalize_status(value):
	text = (value or "").strip().lower()
	for status in STATUSES:
		if status.lower() == text:
			return status
	return "Todo"


def normalize_who(name, owner):
	name = (name or "").strip()
	if not name or name.lower() == owner.lower():
		return None
	return name


def parse_description(text):
	follow_up = None
	notes = []
	for line in (text or "").splitlines():
		match = FOLLOW_UP_PATTERN.match(line.strip())
		if match and follow_up is None:
			follow_up = match.group(1)
		else:
			notes.append(line)
	return follow_up, "\n".join(notes).strip()


def build_description(follow_up, notes):
	lines = []
	if follow_up:
		lines.append(f"{FOLLOW_UP_PREFIX} {follow_up}")
	if notes:
		lines.append(notes)
	return "\n".join(lines)


def to_api_priority(priority):
	return API_PRIORITY_SUM - priority


def from_api_priority(api_priority):
	return API_PRIORITY_SUM - api_priority


def task_to_record(task, project_names, owner):
	labels = task.get("labels") or []
	due = task.get("due")
	follow_up, notes = parse_description(task.get("description"))
	if task.get("checked"):
		status = "Complete"
	elif BLOCKED_LABEL in labels:
		status = "Blocked"
	else:
		status = "Todo"
	who = next((label[len(WHO_PREFIX):] for label in labels if label.startswith(WHO_PREFIX)), None)
	return Record(
		content=task["content"],
		project=project_names.get(task["project_id"], ""),
		due=due["date"][:DATE_LENGTH] if due else None,
		status=status,
		who=normalize_who(who, owner),
		follow_up=follow_up,
		notes=notes,
		repeat=due["string"] if due and due.get("is_recurring") else None,
		priority=from_api_priority(task.get("priority", 1)),
	)


def labels_for(record, current_labels):
	labels = [label for label in current_labels if label != BLOCKED_LABEL and not label.startswith(WHO_PREFIX)]
	if record.status == "Blocked":
		labels.append(BLOCKED_LABEL)
	if record.who:
		labels.append(WHO_PREFIX + record.who)
	return labels
