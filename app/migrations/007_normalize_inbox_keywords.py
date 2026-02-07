from collections import defaultdict

from sqlalchemy import text


def _normalize_keyword(value: str | None) -> str:
    return " ".join((value or "").upper().strip().split())


def _normalize_column(
    connection,
    logger,
    *,
    table: str,
    id_column: str,
    keyword_column: str,
) -> tuple[int, set[str], int, int, int]:
    rows = connection.execute(
        text(
            f"""
            SELECT {id_column} AS row_id, {keyword_column} AS keyword
            FROM {table}
            """
        )
    ).fetchall()

    grouped_ids: dict[str, list[int]] = defaultdict(list)
    normalized_by_id: dict[int, str] = {}
    raw_by_id: dict[int, str] = {}
    skipped_empty = 0

    for row in rows:
        row_id = int(row._mapping["row_id"])
        raw_keyword = row._mapping["keyword"]
        normalized_keyword = _normalize_keyword(raw_keyword)
        if not normalized_keyword:
            skipped_empty += 1
            logger.warning(
                "Migration 007_normalize_inbox_keywords: skipped %s.%s id=%s because normalized keyword is empty.",
                table,
                keyword_column,
                row_id,
            )
            continue
        grouped_ids[normalized_keyword].append(row_id)
        normalized_by_id[row_id] = normalized_keyword
        raw_by_id[row_id] = raw_keyword or ""

    conflict_keyword_count = 0
    skipped_conflict_rows = 0
    conflicting_row_ids: set[int] = set()
    for keyword, ids in sorted(grouped_ids.items()):
        if len(ids) <= 1:
            continue
        sorted_ids = sorted(ids)
        keeper_id = sorted_ids[0]
        conflicting_ids = sorted_ids[1:]
        conflict_keyword_count += 1
        skipped_conflict_rows += len(conflicting_ids)
        conflicting_row_ids.update(conflicting_ids)
        logger.warning(
            "Migration 007_normalize_inbox_keywords: keyword %r has normalized duplicates in %s.%s "
            "(keeping id=%s canonical, skipping ids=%s).",
            keyword,
            table,
            keyword_column,
            keeper_id,
            ",".join(str(row_id) for row_id in conflicting_ids),
        )

    updates = 0
    for row_id, normalized_keyword in normalized_by_id.items():
        if row_id in conflicting_row_ids:
            continue
        raw_keyword = raw_by_id[row_id]
        if raw_keyword == normalized_keyword:
            continue
        connection.execute(
            text(
                f"""
                UPDATE {table}
                SET {keyword_column} = :keyword
                WHERE {id_column} = :row_id
                """
            ),
            {"keyword": normalized_keyword, "row_id": row_id},
        )
        updates += 1

    return (
        updates,
        set(grouped_ids.keys()),
        skipped_empty,
        conflict_keyword_count,
        skipped_conflict_rows,
    )


def apply(connection, logger) -> None:
    (
        updated_rules,
        rule_keywords,
        skipped_empty_rules,
        conflict_groups_rules,
        conflict_rows_rules,
    ) = _normalize_column(
        connection,
        logger,
        table="keyword_automation_rules",
        id_column="id",
        keyword_column="keyword",
    )
    (
        updated_surveys,
        survey_keywords,
        skipped_empty_surveys,
        conflict_groups_surveys,
        conflict_rows_surveys,
    ) = _normalize_column(
        connection,
        logger,
        table="survey_flows",
        id_column="id",
        keyword_column="trigger_keyword",
    )

    cross_conflicts = sorted(rule_keywords.intersection(survey_keywords))
    if cross_conflicts:
        preview = ", ".join(cross_conflicts[:10])
        if len(cross_conflicts) > 10:
            preview += ", ..."
        logger.warning(
            "Migration 007_normalize_inbox_keywords: detected %s cross-table keyword conflicts "
            "between keyword_automation_rules and survey_flows (%s).",
            len(cross_conflicts),
            preview,
        )

    logger.info(
        "Migration 007_normalize_inbox_keywords: normalized %s keyword rule rows and %s survey rows "
        "(skipped_empty_rules=%s, skipped_empty_surveys=%s, conflict_groups_rules=%s, "
        "conflict_groups_surveys=%s, skipped_conflict_rows_rules=%s, skipped_conflict_rows_surveys=%s).",
        updated_rules,
        updated_surveys,
        skipped_empty_rules,
        skipped_empty_surveys,
        conflict_groups_rules,
        conflict_groups_surveys,
        conflict_rows_rules,
        conflict_rows_surveys,
    )
