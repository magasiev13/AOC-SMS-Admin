<?php
/**
 * Plugin Name: Twinevia Event Sync
 * Description: Syncs WordPress Events Manager events, bookings, and WPForms signups to Twinevia.
 * Version: 1.0.0
 * Author: Twinevia
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

const TWINEVIA_EVENT_SYNC_OPTION_ENABLED = 'twinevia_event_sync_enabled';
const TWINEVIA_EVENT_SYNC_OPTION_WEBHOOK_URL = 'twinevia_event_sync_webhook_url';
const TWINEVIA_EVENT_SYNC_OPTION_WEBHOOK_SECRET = 'twinevia_event_sync_webhook_secret';
const TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP = 'twinevia_event_sync_wpforms_event_map';
const TWINEVIA_EVENT_SYNC_CRON_HOOK = 'twinevia_event_sync_reconcile_future_events';

function twinevia_event_sync_enabled(): bool {
	if ( defined( 'TWINEVIA_EVENT_SYNC_ENABLED' ) ) {
		return true === TWINEVIA_EVENT_SYNC_ENABLED;
	}

	return '1' === (string) get_option( TWINEVIA_EVENT_SYNC_OPTION_ENABLED, '0' );
}

function twinevia_event_sync_webhook_url(): string {
	if ( defined( 'TWINEVIA_EVENT_SYNC_WEBHOOK_URL' ) ) {
		return trim( (string) TWINEVIA_EVENT_SYNC_WEBHOOK_URL );
	}

	return trim( (string) get_option( TWINEVIA_EVENT_SYNC_OPTION_WEBHOOK_URL, '' ) );
}

function twinevia_event_sync_webhook_secret(): string {
	if ( defined( 'TWINEVIA_EVENT_SYNC_WEBHOOK_SECRET' ) ) {
		return trim( (string) TWINEVIA_EVENT_SYNC_WEBHOOK_SECRET );
	}

	return trim( (string) get_option( TWINEVIA_EVENT_SYNC_OPTION_WEBHOOK_SECRET, '' ) );
}

function twinevia_event_sync_ready(): bool {
	return twinevia_event_sync_enabled()
		&& '' !== twinevia_event_sync_webhook_url()
		&& '' !== twinevia_event_sync_webhook_secret();
}

function twinevia_event_sync_log_warning( string $message, array $context ): void {
	error_log( 'Twinevia Event Sync warning: ' . $message . ' ' . wp_json_encode( $context ) );
}

function twinevia_event_sync_log_error( string $message, array $context ): void {
	error_log( 'Twinevia Event Sync error: ' . $message . ' ' . wp_json_encode( $context ) );
}

function twinevia_event_sync_send_payload( array $payload ): void {
	if ( ! twinevia_event_sync_ready() ) {
		return;
	}

	$body      = wp_json_encode( $payload );
	if ( ! is_string( $body ) ) {
		throw new RuntimeException( 'Twinevia webhook payload could not be encoded as JSON.' );
	}
	$timestamp = (string) time();
	$signature = hash_hmac( 'sha256', $timestamp . '.' . $body, twinevia_event_sync_webhook_secret() );
	$delivery_id = wp_generate_uuid4();
	$last_error = null;

	for ( $attempt = 1; $attempt <= 3; $attempt++ ) {
		$response  = wp_remote_post(
			twinevia_event_sync_webhook_url(),
			[
				'timeout' => 15,
				'headers' => [
					'Content-Type'             => 'application/json',
					'X-Twinevia-Timestamp'     => $timestamp,
					'X-Twinevia-Signature'     => 'sha256=' . $signature,
					'X-Twinevia-Delivery-ID'   => $delivery_id,
				],
				'body'    => $body,
			]
		);

		if ( is_wp_error( $response ) ) {
			$last_error = new RuntimeException( $response->get_error_message() );
		} else {
			$status_code = (int) wp_remote_retrieve_response_code( $response );
			if ( 200 <= $status_code && 299 >= $status_code ) {
				return;
			}
			$last_error = new RuntimeException( 'Twinevia webhook returned HTTP ' . $status_code . ': ' . wp_remote_retrieve_body( $response ) );
		}

		if ( 3 > $attempt ) {
			twinevia_event_sync_log_warning(
				'Twinevia webhook attempt failed.',
				[
					'attempt'     => $attempt,
					'delivery_id' => $delivery_id,
					'error'       => $last_error->getMessage(),
				]
			);
			usleep( 250000 );
		}
	}

	if ( $last_error instanceof RuntimeException ) {
		throw $last_error;
	}

	throw new RuntimeException( 'Twinevia webhook failed without a response.' );
}

function twinevia_event_sync_events_table(): string {
	global $wpdb;
	return $wpdb->prefix . 'em_events';
}

function twinevia_event_sync_locations_table(): string {
	global $wpdb;
	return $wpdb->prefix . 'em_locations';
}

function twinevia_event_sync_bookings_table(): string {
	global $wpdb;
	return $wpdb->prefix . 'em_bookings';
}

function twinevia_event_sync_wpforms_entries_table(): string {
	global $wpdb;
	return $wpdb->prefix . 'wpforms_entries';
}

function twinevia_event_sync_event_post_type(): string {
	return 'event';
}

function twinevia_event_sync_event_row_for_post_id( int $post_id ): array {
	global $wpdb;

	$row = $wpdb->get_row(
		$wpdb->prepare(
			'SELECT e.*, l.location_name, l.location_address, l.location_town, l.location_state, l.location_postcode, l.location_country
			FROM ' . twinevia_event_sync_events_table() . ' e
			LEFT JOIN ' . twinevia_event_sync_locations_table() . ' l ON l.location_id = e.location_id
			WHERE e.post_id = %d
			ORDER BY e.event_id DESC
			LIMIT 1',
			$post_id
		),
		ARRAY_A
	);

	if ( ! is_array( $row ) ) {
		throw new RuntimeException( 'Events Manager row not found for post ' . $post_id . '.' );
	}

	return $row;
}

function twinevia_event_sync_event_post_id_for_event_id( int $event_id ): int {
	global $wpdb;

	return absint(
		(string) $wpdb->get_var(
			$wpdb->prepare(
				'SELECT post_id FROM ' . twinevia_event_sync_events_table() . ' WHERE event_id = %d LIMIT 1',
				$event_id
			)
		)
	);
}

function twinevia_event_sync_iso_datetime( string $date, string $time ): string {
	$timezone = wp_timezone();
	$datetime = DateTimeImmutable::createFromFormat( 'Y-m-d H:i:s', trim( $date ) . ' ' . trim( $time ), $timezone );
	if ( ! $datetime instanceof DateTimeImmutable ) {
		throw new RuntimeException( 'Invalid Events Manager date/time.' );
	}

	return $datetime->format( DATE_ATOM );
}

function twinevia_event_sync_event_payload_from_post_id( int $post_id ): array {
	$post = get_post( $post_id );
	if ( ! $post instanceof WP_Post || twinevia_event_sync_event_post_type() !== $post->post_type ) {
		throw new RuntimeException( 'Event post not found for post ' . $post_id . '.' );
	}

	$row = twinevia_event_sync_event_row_for_post_id( $post_id );
	return [
		'event_id'     => (string) $row['event_id'],
		'post_id'      => (string) $post_id,
		'title'        => get_the_title( $post_id ),
		'slug'         => $post->post_name,
		'permalink'    => get_permalink( $post_id ),
		'status'       => $post->post_status,
		'start_at'     => twinevia_event_sync_iso_datetime( (string) $row['event_start_date'], (string) $row['event_start_time'] ),
		'end_at'       => twinevia_event_sync_iso_datetime( (string) $row['event_end_date'], (string) $row['event_end_time'] ),
		'timezone'     => wp_timezone_string(),
		'modified_at'  => get_post_modified_time( DATE_ATOM, false, $post_id ),
		'location'     => [
			'name'     => (string) ( $row['location_name'] ?? '' ),
			'address'  => (string) ( $row['location_address'] ?? '' ),
			'town'     => (string) ( $row['location_town'] ?? '' ),
			'state'    => (string) ( $row['location_state'] ?? '' ),
			'postcode' => (string) ( $row['location_postcode'] ?? '' ),
			'country'  => (string) ( $row['location_country'] ?? '' ),
		],
		'rsvp_enabled' => isset( $row['event_rsvp'] ) ? (bool) $row['event_rsvp'] : null,
		'capacity'     => isset( $row['event_spaces'] ) ? (int) $row['event_spaces'] : null,
	];
}

function twinevia_event_sync_event_payload_is_future( array $event_payload ): bool {
	$start_at = DateTimeImmutable::createFromFormat( DATE_ATOM, (string) $event_payload['start_at'] );
	if ( ! $start_at instanceof DateTimeImmutable ) {
		return false;
	}

	return 'publish' === (string) $event_payload['status'] && $start_at > new DateTimeImmutable( 'now', wp_timezone() );
}

function twinevia_event_sync_dispatch_event_post( int $post_id ): void {
	if ( ! twinevia_event_sync_ready() ) {
		return;
	}

	$payload = twinevia_event_sync_event_payload_from_post_id( $post_id );
	twinevia_event_sync_send_payload(
		[
			'action' => 'event_upsert',
			'source' => 'wordpress_events_manager',
			'event'  => $payload,
		]
	);
}

function twinevia_event_sync_save_event_post( int $post_id, WP_Post $post, bool $update ): void {
	if ( wp_is_post_autosave( $post_id ) || wp_is_post_revision( $post_id ) || twinevia_event_sync_event_post_type() !== $post->post_type ) {
		return;
	}

	try {
		twinevia_event_sync_dispatch_event_post( $post_id );
	} catch ( Throwable $exception ) {
		twinevia_event_sync_log_error( 'Could not sync event post.', [ 'post_id' => $post_id, 'error' => $exception->getMessage() ] );
	}
}

add_action( 'save_post_event', 'twinevia_event_sync_save_event_post', 50, 3 );

function twinevia_event_sync_delete_event_post( int $post_id, WP_Post $post ): void {
	if ( twinevia_event_sync_event_post_type() !== $post->post_type || ! twinevia_event_sync_ready() ) {
		return;
	}

	try {
		twinevia_event_sync_send_payload(
			[
				'action' => 'event_deleted',
				'source' => 'wordpress_events_manager',
				'event'  => twinevia_event_sync_event_payload_from_post_id( $post_id ),
			]
		);
	} catch ( Throwable $exception ) {
		twinevia_event_sync_log_error( 'Could not sync deleted event post.', [ 'post_id' => $post_id, 'error' => $exception->getMessage() ] );
	}
}

add_action( 'before_delete_post', 'twinevia_event_sync_delete_event_post', 10, 2 );

function twinevia_event_sync_booking_event_post_id( object $booking ): int {
	if ( method_exists( $booking, 'get_event' ) ) {
		$event = $booking->get_event();
		if ( is_object( $event ) && isset( $event->post_id ) ) {
			return absint( (string) $event->post_id );
		}
	}

	if ( isset( $booking->event_id ) ) {
		return twinevia_event_sync_event_post_id_for_event_id( absint( (string) $booking->event_id ) );
	}

	return 0;
}

function twinevia_event_sync_booking_id( object $booking ): int {
	if ( isset( $booking->booking_id ) ) {
		return absint( (string) $booking->booking_id );
	}
	if ( isset( $booking->id ) ) {
		return absint( (string) $booking->id );
	}
	return 0;
}

function twinevia_event_sync_booking_status_text( int $status_code ): string {
	$status_map = [
		0 => 'pending',
		1 => 'approved',
		2 => 'rejected',
		3 => 'cancelled',
		4 => 'awaiting_online_payment',
		5 => 'awaiting_payment',
	];

	return $status_map[ $status_code ] ?? 'unknown';
}

function twinevia_event_sync_booking_is_active( int $status_code ): bool {
	return in_array( $status_code, [ 0, 1 ], true );
}

function twinevia_event_sync_booking_user_id( object $booking ): int {
	if ( isset( $booking->person_id ) ) {
		return absint( (string) $booking->person_id );
	}
	if ( method_exists( $booking, 'get_person' ) ) {
		$person = $booking->get_person();
		if ( is_object( $person ) && isset( $person->ID ) ) {
			return absint( (string) $person->ID );
		}
	}
	return 0;
}

function twinevia_event_sync_booking_meta_value( object $booking, array $keys ): string {
	$sources = [];
	if ( isset( $booking->booking_meta ) && is_array( $booking->booking_meta ) ) {
		$sources[] = $booking->booking_meta;
	}
	if ( isset( $booking->meta ) && is_array( $booking->meta ) ) {
		$sources[] = $booking->meta;
	}

	foreach ( $sources as $source ) {
		foreach ( $keys as $key ) {
			if ( isset( $source[ $key ] ) && is_scalar( $source[ $key ] ) && '' !== trim( (string) $source[ $key ] ) ) {
				return trim( (string) $source[ $key ] );
			}
		}
	}

	return '';
}

function twinevia_event_sync_booking_phone( object $booking, int $user_id ): string {
	$dbem_phone = twinevia_event_sync_booking_meta_value( $booking, [ 'dbem_phone' ] );
	if ( '' !== $dbem_phone ) {
		return $dbem_phone;
	}

	if ( 0 < $user_id ) {
		$value = get_user_meta( $user_id, 'dbem_phone', true );
		if ( is_scalar( $value ) && '' !== trim( (string) $value ) ) {
			return trim( (string) $value );
		}
	}

	$booking_phone = twinevia_event_sync_booking_meta_value( $booking, [ 'billing_phone', 'phone', 'mobile_phone' ] );
	if ( '' !== $booking_phone ) {
		return $booking_phone;
	}

	foreach ( [ 'billing_phone', 'phone', 'mobile_phone' ] as $key ) {
		if ( 0 < $user_id ) {
			$value = get_user_meta( $user_id, $key, true );
			if ( is_scalar( $value ) && '' !== trim( (string) $value ) ) {
				return trim( (string) $value );
			}
		}
	}

	return '';
}

function twinevia_event_sync_booking_name( object $booking, int $user_id ): string {
	if ( method_exists( $booking, 'get_person' ) ) {
		$person = $booking->get_person();
		if ( is_object( $person ) && isset( $person->display_name ) && '' !== trim( (string) $person->display_name ) ) {
			return trim( (string) $person->display_name );
		}
	}

	if ( 0 < $user_id ) {
		$user = get_user_by( 'id', $user_id );
		if ( $user instanceof WP_User && '' !== trim( $user->display_name ) ) {
			return trim( $user->display_name );
		}
	}

	return isset( $booking->person_name ) ? trim( (string) $booking->person_name ) : '';
}

function twinevia_event_sync_booking_payload( object $booking ): array {
	$post_id = twinevia_event_sync_booking_event_post_id( $booking );
	if ( 0 >= $post_id ) {
		throw new RuntimeException( 'Booking event post not found.' );
	}

	$status_code = isset( $booking->booking_status ) ? (int) $booking->booking_status : 0;
	$user_id = twinevia_event_sync_booking_user_id( $booking );
	$booking_id = twinevia_event_sync_booking_id( $booking );
	if ( 0 >= $booking_id ) {
		throw new RuntimeException( 'Booking ID not found.' );
	}
	return [
		'action'  => 'booking_upsert',
		'source'  => 'wordpress_events_manager',
		'event'   => twinevia_event_sync_event_payload_from_post_id( $post_id ),
		'booking' => [
			'provider'   => 'events_manager',
			'booking_id' => (string) $booking_id,
			'status'     => twinevia_event_sync_booking_status_text( $status_code ),
			'active'     => twinevia_event_sync_booking_is_active( $status_code ),
			'person_id'  => 0 < $user_id ? (string) $user_id : '',
			'name'       => twinevia_event_sync_booking_name( $booking, $user_id ),
			'phone'      => twinevia_event_sync_booking_phone( $booking, $user_id ),
			'spaces'     => isset( $booking->booking_spaces ) ? (int) $booking->booking_spaces : 1,
			'updated_at' => current_time( DATE_ATOM ),
		],
	];
}

function twinevia_event_sync_sync_booking( object $booking, string $action ): void {
	if ( ! twinevia_event_sync_ready() ) {
		return;
	}

	try {
		$payload = twinevia_event_sync_booking_payload( $booking );
		$payload['action'] = $action;
		twinevia_event_sync_send_payload( $payload );
	} catch ( Throwable $exception ) {
		twinevia_event_sync_log_error( 'Could not sync Events Manager booking.', [ 'error' => $exception->getMessage() ] );
	}
}

function twinevia_event_sync_booking_upsert( object $booking ): void {
	twinevia_event_sync_sync_booking( $booking, 'booking_upsert' );
}

function twinevia_event_sync_booking_deleted( object $booking ): void {
	twinevia_event_sync_sync_booking( $booking, 'booking_deleted' );
}

add_action( 'em_booking_added', 'twinevia_event_sync_booking_upsert', 20, 1 );
add_action( 'em_booking_status_changed', 'twinevia_event_sync_booking_upsert', 20, 1 );
add_action( 'em_booking_deleted', 'twinevia_event_sync_booking_deleted', 20, 1 );

function twinevia_event_sync_wpforms_event_map(): array {
	$raw = trim( (string) get_option( TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP, '' ) );
	if ( '' === $raw ) {
		return [];
	}
	$decoded = json_decode( $raw, true );
	return is_array( $decoded ) ? $decoded : [];
}

function twinevia_event_sync_wpforms_event_post_id( int $form_id ): int {
	$map = twinevia_event_sync_wpforms_event_map();
	if ( isset( $map[ (string) $form_id ] ) ) {
		return absint( (string) $map[ (string) $form_id ] );
	}

	$future_map = twinevia_event_sync_future_wpforms_map();
	return isset( $future_map[ $form_id ] ) ? absint( (string) $future_map[ $form_id ] ) : 0;
}

function twinevia_event_sync_wpforms_form_ids_from_content( string $content ): array {
	if ( ! preg_match_all( "/\\[wpforms\\s+[^\\]]*id=[\"']?(\\d+)[\"']?/i", $content, $matches ) ) {
		return [];
	}

	return array_values( array_unique( array_map( 'absint', $matches[1] ) ) );
}

function twinevia_event_sync_future_event_post_ids(): array {
	$query = new WP_Query(
		[
			'post_type'      => twinevia_event_sync_event_post_type(),
			'post_status'    => 'publish',
			'posts_per_page' => -1,
			'fields'         => 'ids',
		]
	);

	$post_ids = [];
	foreach ( $query->posts as $post_id ) {
		try {
			$event_payload = twinevia_event_sync_event_payload_from_post_id( absint( (string) $post_id ) );
			if ( twinevia_event_sync_event_payload_is_future( $event_payload ) ) {
				$post_ids[] = absint( (string) $post_id );
			}
		} catch ( Throwable $exception ) {
			twinevia_event_sync_log_error( 'Could not inspect future event post.', [ 'post_id' => $post_id, 'error' => $exception->getMessage() ] );
		}
	}

	return $post_ids;
}

function twinevia_event_sync_future_wpforms_map(): array {
	$form_event_map = [];
	$embedded_candidates = [];

	foreach ( twinevia_event_sync_wpforms_event_map() as $form_id => $post_id ) {
		$post_id = absint( (string) $post_id );
		if ( 0 >= $post_id ) {
			continue;
		}
		try {
			$event_payload = twinevia_event_sync_event_payload_from_post_id( $post_id );
			if ( twinevia_event_sync_event_payload_is_future( $event_payload ) ) {
				$form_event_map[ absint( (string) $form_id ) ] = $post_id;
			}
		} catch ( Throwable $exception ) {
			twinevia_event_sync_log_error( 'Could not inspect mapped WPForms event.', [ 'form_id' => $form_id, 'post_id' => $post_id, 'error' => $exception->getMessage() ] );
		}
	}

	foreach ( twinevia_event_sync_future_event_post_ids() as $post_id ) {
		$post = get_post( $post_id );
		if ( ! $post instanceof WP_Post ) {
			continue;
		}
		foreach ( twinevia_event_sync_wpforms_form_ids_from_content( (string) $post->post_content ) as $form_id ) {
			if ( isset( $form_event_map[ $form_id ] ) ) {
				continue;
			}
			if ( ! isset( $embedded_candidates[ $form_id ] ) ) {
				$embedded_candidates[ $form_id ] = [];
			}
			$embedded_candidates[ $form_id ][] = $post_id;
		}
	}

	foreach ( $embedded_candidates as $form_id => $post_ids ) {
		$unique_post_ids = array_values( array_unique( array_map( 'absint', $post_ids ) ) );
		if ( 1 === count( $unique_post_ids ) ) {
			$form_event_map[ absint( (string) $form_id ) ] = $unique_post_ids[0];
			continue;
		}
		twinevia_event_sync_log_warning(
			'WPForms auto-mapping skipped because the form appears on multiple future event pages.',
			[
				'form_id'  => $form_id,
				'post_ids' => $unique_post_ids,
			]
		);
	}

	return $form_event_map;
}

function twinevia_event_sync_wpforms_value_to_text( $value ): string {
	if ( is_scalar( $value ) ) {
		return trim( (string) $value );
	}
	if ( is_array( $value ) ) {
		return trim( implode( ' ', array_map( 'twinevia_event_sync_wpforms_value_to_text', $value ) ) );
	}
	return '';
}

function twinevia_event_sync_wpforms_field_value( array $field ): string {
	return twinevia_event_sync_wpforms_value_to_text( $field['value'] ?? '' );
}

function twinevia_event_sync_wpforms_field_label( array $field ): string {
	return isset( $field['name'] ) && is_scalar( $field['name'] ) ? trim( (string) $field['name'] ) : '';
}

function twinevia_event_sync_wpforms_submission_name( array $fields ): string {
	foreach ( $fields as $field ) {
		if ( ! is_array( $field ) ) {
			continue;
		}
		$type = isset( $field['type'] ) && is_scalar( $field['type'] ) ? strtolower( (string) $field['type'] ) : '';
		$label = strtolower( twinevia_event_sync_wpforms_field_label( $field ) );
		if ( 'name' === $type || false !== strpos( $label, 'name' ) ) {
			return twinevia_event_sync_wpforms_field_value( $field );
		}
	}
	return '';
}

function twinevia_event_sync_wpforms_submission_phone( array $fields ): string {
	foreach ( $fields as $field ) {
		if ( ! is_array( $field ) ) {
			continue;
		}
		$type = isset( $field['type'] ) && is_scalar( $field['type'] ) ? strtolower( (string) $field['type'] ) : '';
		$label = strtolower( twinevia_event_sync_wpforms_field_label( $field ) );
		if ( 'phone' === $type || false !== strpos( $label, 'phone' ) || false !== strpos( $label, 'mobile' ) ) {
			return twinevia_event_sync_wpforms_field_value( $field );
		}
	}
	return '';
}

function twinevia_event_sync_wpforms_process_complete( array $fields, array $entry, array $form_data, int $entry_id ): void {
	if ( ! twinevia_event_sync_ready() ) {
		return;
	}

	$form_id = isset( $form_data['id'] ) ? absint( (string) $form_data['id'] ) : 0;
	$post_id = twinevia_event_sync_wpforms_event_post_id( $form_id );
	if ( 0 >= $post_id ) {
		twinevia_event_sync_log_warning( 'WPForms signup skipped because no event mapping was found.', [ 'form_id' => $form_id ] );
		return;
	}

	twinevia_event_sync_sync_wpforms_entry( $entry_id, $form_id, $fields, $entry, $post_id );
}

function twinevia_event_sync_sync_wpforms_entry( int $entry_id, int $form_id, array $fields, array $entry, int $post_id ): void {
	try {
		twinevia_event_sync_send_payload(
			[
				'action'  => 'booking_upsert',
				'source'  => 'wordpress_wpforms',
				'event'   => twinevia_event_sync_event_payload_from_post_id( $post_id ),
				'booking' => [
					'provider'    => 'wpforms',
					'booking_id'  => (string) $entry_id,
					'status'      => (string) ( $entry['status'] ?? 'completed' ),
					'active'      => true,
					'person_id'   => isset( $entry['user_id'] ) ? (string) $entry['user_id'] : '',
					'name'        => twinevia_event_sync_wpforms_submission_name( $fields ),
					'phone'       => twinevia_event_sync_wpforms_submission_phone( $fields ),
					'spaces'      => 1,
					'updated_at'  => current_time( DATE_ATOM ),
					'sms_consent' => true,
				],
			]
		);
	} catch ( Throwable $exception ) {
		twinevia_event_sync_log_error( 'Could not sync WPForms signup.', [ 'form_id' => $form_id, 'entry_id' => $entry_id, 'error' => $exception->getMessage() ] );
	}
}

add_action( 'wpforms_process_complete', 'twinevia_event_sync_wpforms_process_complete', 20, 4 );

function twinevia_event_sync_wpforms_entry_fields( string $fields_json ): array {
	$decoded = json_decode( $fields_json, true );
	if ( ! is_array( $decoded ) ) {
		return [];
	}

	return array_values( array_filter( $decoded, 'is_array' ) );
}

function twinevia_event_sync_reconcile_future_events(): void {
	if ( ! twinevia_event_sync_ready() ) {
		return;
	}

	$query = new WP_Query(
		[
			'post_type'      => twinevia_event_sync_event_post_type(),
			'post_status'    => 'publish',
			'posts_per_page' => -1,
			'fields'         => 'ids',
		]
	);

	foreach ( $query->posts as $post_id ) {
		try {
			$event_payload = twinevia_event_sync_event_payload_from_post_id( absint( (string) $post_id ) );
			if ( ! twinevia_event_sync_event_payload_is_future( $event_payload ) ) {
				continue;
			}
			twinevia_event_sync_send_payload(
				[
					'action' => 'event_upsert',
					'source' => 'wordpress_events_manager',
					'event'  => $event_payload,
				]
			);
		} catch ( Throwable $exception ) {
			twinevia_event_sync_log_error( 'Could not reconcile event.', [ 'post_id' => $post_id, 'error' => $exception->getMessage() ] );
		}
	}

	twinevia_event_sync_reconcile_events_manager_bookings();
	twinevia_event_sync_reconcile_wpforms_entries();
	twinevia_event_sync_send_payload(
		[
			'action' => 'reconcile_complete',
			'source' => 'wordpress_events_manager',
		]
	);
}

function twinevia_event_sync_reconcile_events_manager_bookings(): void {
	global $wpdb;

	if ( ! function_exists( 'em_get_booking' ) ) {
		return;
	}

	$booking_ids = $wpdb->get_col(
		'SELECT b.booking_id
		FROM ' . twinevia_event_sync_bookings_table() . ' b
		INNER JOIN ' . twinevia_event_sync_events_table() . ' e ON e.event_id = b.event_id
		WHERE b.booking_status IN (0, 1)
		AND e.event_start_date >= CURDATE()'
	);

	foreach ( $booking_ids as $booking_id ) {
		$booking = em_get_booking( absint( (string) $booking_id ) );
		if ( is_object( $booking ) ) {
			twinevia_event_sync_booking_upsert( $booking );
		}
	}
}

function twinevia_event_sync_reconcile_wpforms_entries(): void {
	global $wpdb;

	$form_event_map = twinevia_event_sync_future_wpforms_map();
	if ( [] === $form_event_map ) {
		return;
	}

	$table_name = twinevia_event_sync_wpforms_entries_table();
	$table_exists = $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $table_name ) );
	if ( $table_exists !== $table_name ) {
		return;
	}

	$form_ids = array_keys( $form_event_map );
	$placeholders = implode( ',', array_fill( 0, count( $form_ids ), '%d' ) );
	$entries = $wpdb->get_results(
		$wpdb->prepare(
			'SELECT entry_id, form_id, user_id, status, fields, date_modified
			FROM ' . $table_name . '
			WHERE form_id IN (' . $placeholders . ')
			AND (status IS NULL OR status = "" OR status IN ("completed", "publish", "published"))',
			$form_ids
		),
		ARRAY_A
	);

	if ( ! is_array( $entries ) ) {
		return;
	}

	foreach ( $entries as $entry ) {
		$form_id = absint( (string) $entry['form_id'] );
		$post_id = $form_event_map[ $form_id ] ?? 0;
		if ( 0 >= $post_id ) {
			continue;
		}

		twinevia_event_sync_sync_wpforms_entry(
			absint( (string) $entry['entry_id'] ),
			$form_id,
			twinevia_event_sync_wpforms_entry_fields( (string) $entry['fields'] ),
			[
				'status' => (string) $entry['status'],
				'user_id' => (string) $entry['user_id'],
				'date_modified' => (string) $entry['date_modified'],
			],
			$post_id
		);
	}
}

add_action( TWINEVIA_EVENT_SYNC_CRON_HOOK, 'twinevia_event_sync_reconcile_future_events' );

function twinevia_event_sync_schedule_cron(): void {
	if ( ! wp_next_scheduled( TWINEVIA_EVENT_SYNC_CRON_HOOK ) ) {
		wp_schedule_event( time() + HOUR_IN_SECONDS, 'hourly', TWINEVIA_EVENT_SYNC_CRON_HOOK );
	}
}

add_action( 'init', 'twinevia_event_sync_schedule_cron' );

function twinevia_event_sync_admin_menu(): void {
	add_options_page(
		'Twinevia Event Sync',
		'Twinevia Event Sync',
		'manage_options',
		'twinevia-event-sync',
		'twinevia_event_sync_admin_page'
	);
}

add_action( 'admin_menu', 'twinevia_event_sync_admin_menu' );

function twinevia_event_sync_register_settings(): void {
	register_setting( 'twinevia_event_sync', TWINEVIA_EVENT_SYNC_OPTION_ENABLED );
	register_setting( 'twinevia_event_sync', TWINEVIA_EVENT_SYNC_OPTION_WEBHOOK_URL );
	register_setting( 'twinevia_event_sync', TWINEVIA_EVENT_SYNC_OPTION_WEBHOOK_SECRET );
	register_setting( 'twinevia_event_sync', TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP );
}

add_action( 'admin_init', 'twinevia_event_sync_register_settings' );

function twinevia_event_sync_admin_page(): void {
	if ( ! current_user_can( 'manage_options' ) ) {
		return;
	}
	?>
	<div class="wrap">
		<h1>Twinevia Event Sync</h1>
		<form method="post" action="options.php">
			<?php settings_fields( 'twinevia_event_sync' ); ?>
			<table class="form-table" role="presentation">
				<tr>
					<th scope="row">Enabled</th>
					<td><label><input type="checkbox" name="<?php echo esc_attr( TWINEVIA_EVENT_SYNC_OPTION_ENABLED ); ?>" value="1" <?php checked( twinevia_event_sync_enabled() ); ?>> Sync events to Twinevia</label></td>
				</tr>
				<tr>
					<th scope="row"><label for="twinevia_event_sync_webhook_url">Webhook URL</label></th>
					<td><input class="regular-text" id="twinevia_event_sync_webhook_url" name="<?php echo esc_attr( TWINEVIA_EVENT_SYNC_OPTION_WEBHOOK_URL ); ?>" type="url" value="<?php echo esc_attr( twinevia_event_sync_webhook_url() ); ?>"></td>
				</tr>
				<tr>
					<th scope="row"><label for="twinevia_event_sync_webhook_secret">Webhook Secret</label></th>
					<td><input class="regular-text" id="twinevia_event_sync_webhook_secret" name="<?php echo esc_attr( TWINEVIA_EVENT_SYNC_OPTION_WEBHOOK_SECRET ); ?>" type="password" value="<?php echo esc_attr( twinevia_event_sync_webhook_secret() ); ?>" autocomplete="off"></td>
				</tr>
				<tr>
					<th scope="row"><label for="twinevia_event_sync_wpforms_event_map">WPForms Event Map</label></th>
					<td>
						<textarea class="large-text code" id="twinevia_event_sync_wpforms_event_map" name="<?php echo esc_attr( TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP ); ?>" rows="5"><?php echo esc_textarea( (string) get_option( TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP, '' ) ); ?></textarea>
						<p class="description">Optional JSON map of form ID to event post ID, for example {"123":"456"}.</p>
					</td>
				</tr>
			</table>
			<?php submit_button(); ?>
		</form>
	</div>
	<?php
}

if ( defined( 'WP_CLI' ) && WP_CLI ) {
	WP_CLI::add_command(
		'twinevia-event-sync reconcile',
		function (): void {
			twinevia_event_sync_reconcile_future_events();
			WP_CLI::success( 'Twinevia future event reconciliation completed.' );
		}
	);
}
