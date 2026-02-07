from sqlalchemy import text


def apply(connection, logger) -> None:
    existing_conflicts = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM keyword_automation_rules AS rules
            INNER JOIN survey_flows AS surveys
                ON UPPER(TRIM(rules.keyword)) = UPPER(TRIM(surveys.trigger_keyword))
            """
        )
    ).scalar_one()

    if existing_conflicts:
        logger.warning(
            "Migration 008_enforce_cross_table_keyword_uniqueness: found %s existing keyword conflict(s) "
            "between keyword_automation_rules and survey_flows. New conflicts will be blocked.",
            existing_conflicts,
        )

    connection.execute(
        text(
            """
            CREATE TRIGGER IF NOT EXISTS trg_keyword_rules_no_survey_conflict_insert
            BEFORE INSERT ON keyword_automation_rules
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1
                FROM survey_flows
                WHERE UPPER(TRIM(trigger_keyword)) = UPPER(TRIM(NEW.keyword))
            )
            BEGIN
                SELECT RAISE(ABORT, 'keyword_conflicts_with_survey_trigger');
            END
            """
        )
    )

    connection.execute(
        text(
            """
            CREATE TRIGGER IF NOT EXISTS trg_keyword_rules_no_survey_conflict_update
            BEFORE UPDATE OF keyword ON keyword_automation_rules
            FOR EACH ROW
            WHEN UPPER(TRIM(NEW.keyword)) != UPPER(TRIM(OLD.keyword))
                 AND EXISTS (
                    SELECT 1
                    FROM survey_flows
                    WHERE UPPER(TRIM(trigger_keyword)) = UPPER(TRIM(NEW.keyword))
                 )
            BEGIN
                SELECT RAISE(ABORT, 'keyword_conflicts_with_survey_trigger');
            END
            """
        )
    )

    connection.execute(
        text(
            """
            CREATE TRIGGER IF NOT EXISTS trg_survey_flows_no_keyword_conflict_insert
            BEFORE INSERT ON survey_flows
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1
                FROM keyword_automation_rules
                WHERE UPPER(TRIM(keyword)) = UPPER(TRIM(NEW.trigger_keyword))
            )
            BEGIN
                SELECT RAISE(ABORT, 'survey_trigger_conflicts_with_keyword_rule');
            END
            """
        )
    )

    connection.execute(
        text(
            """
            CREATE TRIGGER IF NOT EXISTS trg_survey_flows_no_keyword_conflict_update
            BEFORE UPDATE OF trigger_keyword ON survey_flows
            FOR EACH ROW
            WHEN UPPER(TRIM(NEW.trigger_keyword)) != UPPER(TRIM(OLD.trigger_keyword))
                 AND EXISTS (
                    SELECT 1
                    FROM keyword_automation_rules
                    WHERE UPPER(TRIM(keyword)) = UPPER(TRIM(NEW.trigger_keyword))
                 )
            BEGIN
                SELECT RAISE(ABORT, 'survey_trigger_conflicts_with_keyword_rule');
            END
            """
        )
    )

    logger.info(
        "Migration 008_enforce_cross_table_keyword_uniqueness: created cross-table keyword conflict triggers."
    )
