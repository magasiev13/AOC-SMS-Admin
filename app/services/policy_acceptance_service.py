from __future__ import annotations

from flask import current_app

from app import db
from app.models import AppUser, CustomerPolicyAcceptance, Organization


POLICY_CONFIG_KEYS = {
    "terms": "TERMS_POLICY_VERSION",
    "privacy": "PRIVACY_POLICY_VERSION",
    "acceptable_use": "ACCEPTABLE_USE_POLICY_VERSION",
    "sms": "SMS_POLICY_VERSION",
    "billing": "BILLING_POLICY_VERSION",
}


class PolicyAcceptanceError(ValueError):
    """Raised when required policy acceptance is incomplete or invalid."""


def required_policy_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for policy_name, config_key in POLICY_CONFIG_KEYS.items():
        version = str(current_app.config.get(config_key) or "").strip()
        if not version:
            raise PolicyAcceptanceError(f"Required policy version {config_key} is not configured.")
        versions[policy_name] = version
    return versions


def missing_required_policy_acceptances(
    organization: Organization,
    user: AppUser,
) -> dict[str, str]:
    required = required_policy_versions()
    rows = CustomerPolicyAcceptance.query.filter_by(
        organization_id=organization.id,
        user_id=user.id,
    ).all()
    accepted = {(row.policy_name, row.policy_version) for row in rows}
    return {
        policy_name: version
        for policy_name, version in required.items()
        if (policy_name, version) not in accepted
    }


def required_policy_acceptances_complete(
    organization: Organization,
    user: AppUser,
) -> bool:
    return not missing_required_policy_acceptances(organization, user)


def accept_required_policies(
    organization: Organization,
    user: AppUser,
    accepted_policy_names: set[str],
    accepted_ip: str | None,
    accepted_user_agent: str | None,
) -> list[CustomerPolicyAcceptance]:
    required = required_policy_versions()
    missing_names = set(required) - accepted_policy_names
    if missing_names:
        missing_labels = ", ".join(sorted(missing_names))
        raise PolicyAcceptanceError(
            f"Accept every required policy before continuing: {missing_labels}."
        )

    existing_rows = CustomerPolicyAcceptance.query.filter_by(
        organization_id=organization.id,
        user_id=user.id,
    ).all()
    existing = {(row.policy_name, row.policy_version): row for row in existing_rows}
    accepted_rows: list[CustomerPolicyAcceptance] = []
    for policy_name, policy_version in required.items():
        row = existing.get((policy_name, policy_version))
        if row is None:
            row = CustomerPolicyAcceptance(
                organization_id=organization.id,
                user_id=user.id,
                policy_name=policy_name,
                policy_version=policy_version,
                accepted_ip=str(accepted_ip or "")[:45] or None,
                accepted_user_agent=str(accepted_user_agent or "")[:255] or None,
            )
            db.session.add(row)
        accepted_rows.append(row)
    db.session.commit()
    return accepted_rows
