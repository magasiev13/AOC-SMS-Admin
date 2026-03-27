def _table_exists(connection, table_name: str) -> bool:
    cursor = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def apply(connection, logger):
    if not _table_exists(connection, "organization_a2p_onboardings"):
        connection.exec_driver_sql(
            """
            CREATE TABLE organization_a2p_onboardings (
                id INTEGER PRIMARY KEY,
                organization_id INTEGER NOT NULL UNIQUE,
                registration_path VARCHAR(32) NOT NULL DEFAULT 'standard',
                number_strategy VARCHAR(32) NOT NULL DEFAULT 'auto_buy',
                onboarding_status VARCHAR(20) NOT NULL DEFAULT 'draft',
                brand_status VARCHAR(30),
                campaign_status VARCHAR(30),
                verification_status VARCHAR(30),
                business_name VARCHAR(120),
                business_type VARCHAR(80),
                business_identity VARCHAR(40),
                business_registration_identifier VARCHAR(40),
                business_registration_number_encrypted TEXT,
                website_url VARCHAR(255),
                social_profile_url VARCHAR(255),
                email VARCHAR(255),
                phone_number VARCHAR(20),
                mobile_number VARCHAR(20),
                first_name VARCHAR(80),
                last_name VARCHAR(80),
                job_position VARCHAR(80),
                address_sid VARCHAR(64),
                supporting_document_sid VARCHAR(64),
                customer_profile_sid VARCHAR(64),
                trust_product_sid VARCHAR(64),
                brand_registration_sid VARCHAR(64),
                vetting_sid VARCHAR(64),
                campaign_sid VARCHAR(64),
                campaign_use_case VARCHAR(40) NOT NULL DEFAULT 'MIXED',
                campaign_description TEXT,
                message_flow TEXT,
                message_samples_json TEXT,
                opt_in_message TEXT,
                opt_out_message TEXT,
                help_message TEXT,
                opt_in_keywords_json TEXT,
                opt_out_keywords_json TEXT,
                help_keywords_json TEXT,
                campaign_verify_token_encrypted TEXT,
                desired_phone_number VARCHAR(20),
                desired_phone_number_sid VARCHAR(64),
                raw_submission_json TEXT,
                raw_status_json TEXT,
                last_error TEXT,
                failure_code VARCHAR(80),
                submitted_at TIMESTAMP,
                last_synced_at TIMESTAMP,
                approved_at TIMESTAMP,
                canceled_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES organizations (id)
            )
            """
        )
        logger.info("Migration 020: created organization_a2p_onboardings.")
    else:
        logger.info("Migration 020: organization_a2p_onboardings already exists.")

    index_statements = (
        "CREATE INDEX IF NOT EXISTS ix_org_a2p_onboardings_status ON organization_a2p_onboardings (onboarding_status)",
        "CREATE INDEX IF NOT EXISTS ix_org_a2p_onboardings_brand_status ON organization_a2p_onboardings (brand_status)",
        "CREATE INDEX IF NOT EXISTS ix_org_a2p_onboardings_campaign_status ON organization_a2p_onboardings (campaign_status)",
        "CREATE INDEX IF NOT EXISTS ix_org_a2p_onboardings_verification_status ON organization_a2p_onboardings (verification_status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_a2p_onboardings_customer_profile_sid ON organization_a2p_onboardings (customer_profile_sid)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_a2p_onboardings_trust_product_sid ON organization_a2p_onboardings (trust_product_sid)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_a2p_onboardings_brand_registration_sid ON organization_a2p_onboardings (brand_registration_sid)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_a2p_onboardings_vetting_sid ON organization_a2p_onboardings (vetting_sid)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_org_a2p_onboardings_campaign_sid ON organization_a2p_onboardings (campaign_sid)",
    )
    for statement in index_statements:
        connection.exec_driver_sql(statement)
    logger.info("Migration 020: ensured organization_a2p_onboardings indexes.")
