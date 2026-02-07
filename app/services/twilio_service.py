import time
from typing import Optional

from flask import current_app
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from app.utils import render_message_template


class TwilioTransientError(Exception):
    """Transient Twilio error that should trigger a retry."""

    def __init__(self, message: str, results: Optional[dict] = None, failed_index: Optional[int] = None):
        super().__init__(message)
        self.results = results
        self.failed_index = failed_index


class TwilioService:
    def __init__(self):
        self.account_sid = current_app.config.get('TWILIO_ACCOUNT_SID')
        self.auth_token = current_app.config.get('TWILIO_AUTH_TOKEN')
        self.from_number = current_app.config.get('TWILIO_FROM_NUMBER')
        
        if not all([self.account_sid, self.auth_token, self.from_number]):
            raise ValueError("Twilio credentials not configured. Check environment variables.")
        
        self.client = Client(self.account_sid, self.auth_token)
    
    def _is_transient_error(self, error: TwilioRestException) -> bool:
        status = getattr(error, 'status', None)
        return status in {429} or (isinstance(status, int) and status >= 500)

    def send_message(self, to_number: str, body: str, raise_on_transient: bool = False) -> dict:
        """Send a single SMS message. Returns dict with status and error if any."""
        try:
            message = self.client.messages.create(
                body=body,
                from_=self.from_number,
                to=to_number
            )
            return {
                'success': True,
                'sid': message.sid,
                'status': message.status,
                'error': None
            }
        except TwilioRestException as e:
            if raise_on_transient and self._is_transient_error(e):
                raise TwilioTransientError(str(e)) from e
            return {
                'success': False,
                'sid': None,
                'status': 'failed',
                'error': str(e.msg) if hasattr(e, 'msg') else str(e)
            }
        except Exception as e:
            if raise_on_transient:
                raise
            return {
                'success': False,
                'sid': None,
                'status': 'failed',
                'error': str(e)
            }
    
    def send_bulk(
        self,
        recipients: list,
        body: str,
        delay: float = 0.1,
        raise_on_transient: bool = False
    ) -> dict:
        """
        Send SMS to multiple recipients.
        
        Args:
            recipients: List of dicts with 'phone' and optionally 'name'
            body: Message body
            delay: Delay between sends in seconds (to avoid rate limits)
            raise_on_transient: Raise when Twilio returns a transient error
        
        Returns:
            dict with success_count, failure_count, and details list
        """
        results = {
            'total': len(recipients),
            'success_count': 0,
            'failure_count': 0,
            'details': []
        }
        
        for index, recipient in enumerate(recipients):
            phone = recipient.get('phone')
            name = recipient.get('name', '')
            personalized_body = render_message_template(body, recipient)

            try:
                result = self.send_message(phone, personalized_body, raise_on_transient=raise_on_transient)
            except TwilioTransientError as exc:
                raise TwilioTransientError(
                    str(exc),
                    results=results,
                    failed_index=index
                ) from exc
            
            detail = {
                'phone': phone,
                'name': name,
                'success': result['success'],
                'error': result.get('error')
            }
            results['details'].append(detail)
            
            if result['success']:
                results['success_count'] += 1
            else:
                results['failure_count'] += 1
            
            # Small delay to avoid rate limiting
            if delay > 0:
                time.sleep(delay)
        
        return results


def get_twilio_service() -> TwilioService:
    """Factory function to get TwilioService instance."""
    return TwilioService()


def validate_inbound_signature(url: str, params: dict, signature: Optional[str]) -> bool:
    """Validate Twilio webhook signature for inbound requests."""
    auth_token = current_app.config.get('TWILIO_AUTH_TOKEN')
    if not auth_token or not signature:
        return False

    try:
        validator = RequestValidator(auth_token)
        return validator.validate(url, params, signature)
    except Exception:
        current_app.logger.exception('Failed to validate Twilio inbound signature.')
        return False
