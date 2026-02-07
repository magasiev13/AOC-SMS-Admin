from collections import defaultdict

from sqlalchemy import text


def _normalize_keyword(value: str | None) -> str:
    return " ".join((value or "").upper().strip().split())


def _normalize_column(connection, *, table: str, id_column: str, keyword_column: str) -> tuple[int, set[str]]:
    rows = connection.execute(
        text(
            f"""
            SELECT {id_column} AS row_id, {keyword_column} AS keyword
            FROM {table}
            """
        )
    ).fetchall()

    grouped_ids: dict[str, list[int]] = defaultdict(list)
    updates: list[tuple[int, str]] = []

    for row in rows:
        row_id = int(row._mapping["row_id"])
        raw_keyword = row._mapping["keyword"]
        normalized_keyword = _normalize_keyword(raw_keyword)
        if not normalized_keyword:
            raise RuntimeError(
                f"Cannot normalize {table}.{keyword_column} for id={row_id}: keyword becomes empty."
            )
        grouped_ids[normalized_keyword].append(row_id)
        if (raw_keyword or "") != normalized_keyword:
            updates.append((row_id, normalized_keyword))

    duplicates = {keyword: ids for keyword, ids in grouped_ids.items() if len(ids) > 1}
    if duplicates:
        details = ", ".join(
            f"{keyword} -> ids {','.join(str(row_id) for row_id in ids)}"
            for keyword, ids in sorted(duplicates.items())
        )
        raise RuntimeError(
            f"Cannot normalize {table}.{keyword_column}: duplicate normalized keywords detected ({details})."
        )

    for row_id, normalized_keyword in updates:
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

    return len(updates), set(grouped_ids.keys())


def apply(connection, logger) -> None:
    updated_rules, rule_keywords = _normalize_column(
        connection,
        table="keyword_automation_rules",
        id_column="id",
        keyword_column="keyword",
    )
    updated_surveys, survey_keywords = _normalize_column(
        connection,
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
        "Migration 007_normalize_inbox_keywords: normalized %s keyword rule rows and %s survey rows.",
        updated_rules,
        updated_surveys,
    )
