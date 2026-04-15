from sqlalchemy import inspect, text


ONBOARDING_COLUMNS = (
    ("legal_business_name", "ALTER TABLE organization_a2p_onboardings ADD COLUMN legal_business_name VARCHAR(120)"),
    ("public_brand_name", "ALTER TABLE organization_a2p_onboardings ADD COLUMN public_brand_name VARCHAR(120)"),
    ("has_business_tax_id", "ALTER TABLE organization_a2p_onboardings ADD COLUMN has_business_tax_id BOOLEAN"),
    ("brand_registration_mode", "ALTER TABLE organization_a2p_onboardings ADD COLUMN brand_registration_mode VARCHAR(40)"),
    ("has_public_website", "ALTER TABLE organization_a2p_onboardings ADD COLUMN has_public_website BOOLEAN"),
    ("submission_source_mode", "ALTER TABLE organization_a2p_onboardings ADD COLUMN submission_source_mode VARCHAR(40)"),
    ("submission_source_reason", "ALTER TABLE organization_a2p_onboardings ADD COLUMN submission_source_reason TEXT"),
    ("external_website_url", "ALTER TABLE organization_a2p_onboardings ADD COLUMN external_website_url VARCHAR(255)"),
    (
        "external_privacy_policy_url",
        "ALTER TABLE organization_a2p_onboardings ADD COLUMN external_privacy_policy_url VARCHAR(255)",
    ),
    (
        "external_terms_and_conditions_url",
        "ALTER TABLE organization_a2p_onboardings ADD COLUMN external_terms_and_conditions_url VARCHAR(255)",
    ),
    ("external_cta_proof_url", "ALTER TABLE organization_a2p_onboardings ADD COLUMN external_cta_proof_url VARCHAR(255)"),
    ("external_url_validation_json", "ALTER TABLE organization_a2p_onboardings ADD COLUMN external_url_validation_json TEXT"),
    ("external_urls_last_checked_at", "ALTER TABLE organization_a2p_onboardings ADD COLUMN external_urls_last_checked_at TIMESTAMP"),
    ("upgrade_recommended_reason", "ALTER TABLE organization_a2p_onboardings ADD COLUMN upgrade_recommended_reason TEXT"),
    ("upgrade_recommended_at", "ALTER TABLE organization_a2p_onboardings ADD COLUMN upgrade_recommended_at TIMESTAMP"),
    ("upgrade_requested_at", "ALTER TABLE organization_a2p_onboardings ADD COLUMN upgrade_requested_at TIMESTAMP"),
    ("upgraded_at", "ALTER TABLE organization_a2p_onboardings ADD COLUMN upgraded_at TIMESTAMP"),
)


def apply(connection, logger) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())

    if "organization_a2p_onboardings" not in table_names:
        logger.info("SaaS migration 010: organization_a2p_onboardings missing, skipping source-mode fields.")
        return

    onboarding_columns = {row["name"] for row in inspector.get_columns("organization_a2p_onboardings")}
    for column_name, statement in ONBOARDING_COLUMNS:
        if column_name in onboarding_columns:
            logger.info("SaaS migration 010: organization_a2p_onboardings.%s already exists.", column_name)
            continue
        connection.execute(text(statement))
        logger.info("SaaS migration 010: added organization_a2p_onboardings.%s.", column_name)

    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_org_a2p_onboardings_submission_source_mode
            ON organization_a2p_onboardings (submission_source_mode)
            """
        )
    )
    logger.info("SaaS migration 010: ensured organization_a2p_onboardings.submission_source_mode index.")

    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_org_a2p_onboardings_brand_registration_mode
            ON organization_a2p_onboardings (brand_registration_mode)
            """
        )
    )
    logger.info("SaaS migration 010: ensured organization_a2p_onboardings.brand_registration_mode index.")
