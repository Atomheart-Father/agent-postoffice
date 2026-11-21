import json
from pathlib import Path


MESSAGES = json.loads(Path(__file__).with_name("messages.json").read_text(encoding="utf-8"))


def batch_text(conn, unit: str, refs) -> str:
    """Render the current actionable refs from the same Core snapshot used to select them."""
    refs = [ref for ref in (refs or ()) if isinstance(ref, str) and ref]
    if not refs:
        return MESSAGES["empty_batch"] % unit
    marks = ",".join("?" * len(refs))
    from lib.mail import letter_fields

    rows = {r["letter_id"]: r for r in conn.execute(
        "SELECT l.letter_id, l.from_actor, l.subject, l.need, s.content_override_json FROM letters l"
        " LEFT JOIN letter_states s ON s.letter_id=l.letter_id AND s.box=?"
        " WHERE l.letter_id IN (%s)" % marks, [unit, *refs]).fetchall()}
    lines = [MESSAGES["batch_header"] % (unit, len(refs))]
    for ref in refs:
        row = rows.get(ref)
        if row is None:
            lines.append(MESSAGES["unavailable_letter"] % ref)
            continue
        fields = letter_fields(row, row, ("subject", "need"))
        lines.append(MESSAGES["letter"] % (ref, row["from_actor"], fields["subject"],
                                           fields["need"].strip() or MESSAGES["unspecified_need"]))
    lines.append(MESSAGES["read_instruction"])
    return "\n".join(lines)


ROUND_SETTLE_RESULTS = ("accepted", "rejected", "unknown")
