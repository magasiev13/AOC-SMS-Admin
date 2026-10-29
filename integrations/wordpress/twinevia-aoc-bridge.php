<?php
/**
 * Plugin Name: Twinevia AOC Integration Bridge
 * Description: Explicit opt-in replacement for AOC's existing messaging hooks. Requires Twinevia Event Sync 2.
 * Version: 1.0.0
 */
if ( ! defined( 'ABSPATH' ) ) { exit; }


function twinevia_aoc_bridge_saved_event( bool $result, object $event ): bool {
	if ( ! $result ) { return $result; }
	$post_id = absint( (string) ( $event->post_id ?? 0 ) );
	if ( 0 >= $post_id ) { return $result; }
	try {
		// Events Manager has committed its custom-table metadata at this late
		// filter. The earlier WordPress post-save callback sees the old row.
		twinevia_event_sync_dispatch_event_post( $post_id );
	} catch ( Throwable $exception ) {
		twinevia_event_sync_log_error( 'Could not sync saved Events Manager event.', [ 'post_id' => $post_id, 'error' => $exception->getMessage() ] );
	}
	return $result;
}

add_action( 'plugins_loaded', function (): void {
	if ( ! defined( 'TWINEVIA_AOC_BRIDGE_ENABLED' ) || true !== TWINEVIA_AOC_BRIDGE_ENABLED ) { return; }
	if ( ! function_exists( 'twinevia_event_sync_inventory_event' ) || ! function_exists( 'aoc_custom_twinevia_sync_enabled' ) ) {
		throw new RuntimeException( 'AOC bridge requires both the installed AOC integration and Twinevia Event Sync 2.' );
	}
	add_filter( 'twinevia_event_sync_enabled', function () { return aoc_custom_twinevia_sync_enabled(); } );
	add_filter( 'twinevia_event_sync_webhook_url', function () { return aoc_custom_twinevia_webhook_url(); } );
	add_filter( 'twinevia_event_sync_webhook_secret', function () { return aoc_custom_twinevia_webhook_secret(); } );
	add_filter( 'twinevia_event_sync_source', function () { return 'aoc-wordpress'; } );
	add_filter( 'twinevia_event_sync_wpforms_event_map', function ( $map ) { return $map + aoc_custom_twinevia_wpforms_event_map(); } );
	add_filter( 'twinevia_event_sync_wpforms_party_size_fields', function ( $map ) { return $map + [ 100009682 => 4, 100009626 => 4 ]; } );
	add_filter( 'twinevia_event_sync_wpforms_party_size', function ( $size, $form_id, $fields ) {
		foreach ( $fields as $field ) {
			if ( 'payment-select' === ( $field['type'] ?? '' ) && false !== stripos( (string) ( $field['name'] ?? '' ), 'ticket quantity' ) ) {
				return aoc_custom_twinevia_wpforms_submission_ticket_quantity( $fields );
			}
		}
		return $size;
	}, 10, 3 );
	add_filter( 'twinevia_event_sync_wpforms_sms_consent', function ( $consent, $form_id, $fields ) { return aoc_custom_twinevia_wpforms_sms_consent( $fields ); }, 10, 3 );
	add_filter( 'twinevia_event_sync_booking_payload', function ( $payload, $booking ) {
		$payload['booking']['spaces'] = aoc_custom_twinevia_booking_spaces( $booking );
		$payload['booking']['selections'] = aoc_custom_twinevia_booking_selections( (int) $payload['booking']['booking_id'] );
		$payload['booking']['comment'] = aoc_custom_twinevia_booking_comment( $booking );
		return $payload;
	}, 10, 2 );
	add_filter( 'twinevia_event_sync_wpforms_active', function ( $active, $form_id, $entry_id, $entry ) {
		global $wpdb;
		$form = get_post( $form_id );
		$data = $form instanceof WP_Post ? json_decode( (string) $form->post_content, true ) : [];
		$paid_form = false;
		foreach ( ( $data['fields'] ?? [] ) as $field ) {
			if ( 'payment-total' === ( $field['type'] ?? '' ) ) { $paid_form = true; break; }
		}
		if ( ! $paid_form ) { return $active; }
		$eligible_payment = $wpdb->get_var( $wpdb->prepare( 'SELECT id FROM ' . $wpdb->prefix . 'wpforms_payments WHERE form_id=%d AND entry_id=%d AND mode=%s AND status IN (%s,%s) ORDER BY id DESC LIMIT 1', $form_id, $entry_id, 'live', 'processed', 'completed' ) );
		return $active && 0 < (int) $eligible_payment;
	}, 10, 4 );
	remove_action( 'em_booking_added', 'aoc_custom_twinevia_sync_booking_upsert', 20 );
	remove_action( 'em_booking_status_changed', 'aoc_custom_twinevia_sync_booking_upsert', 20 );
	remove_action( 'em_booking_deleted', 'aoc_custom_twinevia_sync_booking_deleted', 20 );
	remove_filter( 'em_event_save', 'aoc_custom_twinevia_sync_saved_event', 9999 );
	remove_action( 'save_post_event', 'twinevia_event_sync_save_event_post', 50 );
	add_filter( 'em_event_save', 'twinevia_aoc_bridge_saved_event', 9999, 2 );
	remove_action( 'before_delete_post', 'aoc_custom_twinevia_delete_event_post', 10 );
	remove_action( 'wpforms_stripe_process_complete', 'aoc_custom_twinevia_wpforms_stripe_process_complete', 20 );
	add_action( 'wpforms_stripe_process_complete', function ( $fields, $form_data, $payment_id, $stripe_payment ): void {
		if ( 'succeeded' !== (string) ( $stripe_payment->status ?? '' ) ) { return; }
		$payment = aoc_custom_twinevia_wpforms_payment_record( (int) $payment_id );
		$entry_id = (int) ( $payment->entry_id ?? 0 );
		if ( $entry_id < 1 ) { return; }
		$entry = aoc_custom_twinevia_wpforms_entry_record( $entry_id );
		$form_id = (int) ( $form_data['id'] ?? 0 );
		twinevia_event_sync_sync_wpforms_entry( $entry_id, $form_id, $fields, get_object_vars( $entry ), twinevia_event_sync_wpforms_event_post_id( $form_id ) );
	}, 20, 4 );
}, 100 );
