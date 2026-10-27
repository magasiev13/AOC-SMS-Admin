"""Inspect by default; explicitly recover verified or complete known outcomes."""
import argparse
import json
import sys

from app import create_app, db
from app.services.outbound_review_service import (
    OutboundReviewError,
    finalize_known_outbound_request,
    inspect_outbound_request,
    resolve_outbound_attempt,
)


def _positive_id(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError('Use a positive integer id.')
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--organization-id', type=_positive_id, required=True)
    parser.add_argument('--request-id', type=_positive_id, required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--resolve', action='store_true', help='Write one verified resolution; never send or enqueue a message.')
    actions.add_argument('--finalize-known', action='store_true', help='Recover the same request only when its complete frozen audience has known outcomes; no provider or queue access.')
    parser.add_argument('--attempt-id', type=_positive_id)
    parser.add_argument('--message-sid')
    parser.add_argument('--actor-user-id', type=_positive_id)
    parser.add_argument('--evidence-reference')
    args = parser.parse_args(argv)
    evidence_args = (args.attempt_id, args.message_sid, args.actor_user_id, args.evidence_reference)
    if args.resolve and not all(evidence_args):
        parser.error('--resolve requires --attempt-id, --message-sid, --actor-user-id, and --evidence-reference.')
    if args.finalize_known:
        if not args.actor_user_id or not args.evidence_reference:
            parser.error('--finalize-known requires --actor-user-id and --evidence-reference.')
        if args.attempt_id is not None or args.message_sid is not None:
            parser.error('--finalize-known reviews the complete saved audience and does not accept --attempt-id or --message-sid.')
    if not args.resolve and not args.finalize_known and any(value is not None for value in evidence_args):
        parser.error('Evidence/actor arguments require explicit --resolve or --finalize-known; default inspection is read-only.')
    app = create_app(run_startup_tasks=False, start_scheduler=False)
    with app.app_context():
        try:
            if args.resolve:
                result = resolve_outbound_attempt(
                    args.organization_id, args.request_id, attempt_id=args.attempt_id,
                    message_sid=args.message_sid, actor_user_id=args.actor_user_id,
                    evidence_reference=args.evidence_reference,
                )
            elif args.finalize_known:
                result = finalize_known_outbound_request(
                    args.organization_id, args.request_id, actor_user_id=args.actor_user_id,
                    evidence_reference=args.evidence_reference,
                )
            else:
                result = inspect_outbound_request(args.organization_id, args.request_id)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0
        except OutboundReviewError as exc:
            db.session.rollback()
            print(json.dumps({'error': str(exc), 'state_changed': False}), file=sys.stderr)
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
