<?php
/** Versioned booking inventory implementation. Loaded by Twinevia Event Sync. */
if ( ! defined( 'ABSPATH' ) ) { exit; }

function twinevia_event_sync_source( string $provider ): string {
	return (string) apply_filters( 'twinevia_event_sync_source', 'wpforms' === $provider ? 'wordpress_wpforms' : 'wordpress_events_manager', $provider );
}

function twinevia_event_sync_checked_wpforms_map(): array {
	global $wpdb;
	$raw = get_option( TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP, '' );
	if ( '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not read the form event mapping.' ); }
	if ( ! is_scalar( $raw ) || ( '' !== trim( (string) $raw ) && ! is_array( json_decode( (string) $raw, true ) ) ) ) {
		throw new RuntimeException( 'The configured form event mapping must be a valid JSON object.' );
	}
	$map = twinevia_event_sync_wpforms_event_map();
	if ( '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not read the form event mapping.' ); }
	$normalized = [];
	foreach ( $map as $form_id => $post_id ) {
		if ( ! ctype_digit( (string) $form_id ) || (int) $form_id < 1
			|| ! ( is_int( $post_id ) || is_string( $post_id ) ) || ! ctype_digit( (string) $post_id ) || (int) $post_id < 1 ) {
			throw new RuntimeException( 'Configured form and event identifiers must be positive integers.' );
		}
		$normalized[(int) $form_id] = (int) $post_id;
	}
	return $normalized;
}

function twinevia_event_sync_initial_form_event( int $form_id, int $candidate_post_id ): int {
	global $wpdb;
	$map = twinevia_event_sync_checked_wpforms_map();
	if ( isset( $map[$form_id] ) ) {
		$resolved = $map[$form_id];
	} else {
		// Inspect every published/scheduled event, including past dates. A local
		// inventory's embedded form is only a candidate, never a binding proof.
		$rows = $wpdb->get_results( $wpdb->prepare(
			"SELECT ID, post_content FROM {$wpdb->posts} WHERE post_type = %s
			AND post_status IN ('publish', 'future') ORDER BY ID",
			twinevia_event_sync_event_post_type()
		), ARRAY_A );
		if ( ! is_array( $rows ) || '' !== $wpdb->last_error ) {
			throw new RuntimeException( 'Could not inspect the complete set of event form embeddings.' );
		}
		$matches = [];
		foreach ( $rows as $row ) {
			if ( ! isset( $row['ID'], $row['post_content'] ) || ! is_string( $row['post_content'] ) ) {
				throw new RuntimeException( 'An event form embedding could not be inspected.' );
			}
			if ( in_array( $form_id, twinevia_event_sync_wpforms_form_ids_from_content( $row['post_content'] ), true ) ) {
				$matches[] = absint( (string) $row['ID'] );
			}
		}
		$matches = array_values( array_unique( $matches ) );
		if ( 1 !== count( $matches ) ) {
			throw new RuntimeException( 'The form event binding is missing or ambiguous; configure an explicit event mapping.' );
		}
		$resolved = $matches[0];
	}
	if ( $candidate_post_id < 1 || $resolved !== $candidate_post_id ) {
		throw new RuntimeException( 'The form belongs to a different event; retain its configured source binding.' );
	}
	// Validate the selected Events Manager record before any persistent binding.
	twinevia_event_sync_event_payload_from_post_id( $resolved );
	if ( '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not verify the form event metadata.' ); }
	return $resolved;
}

function twinevia_event_sync_bound_event( int $entry_id, int $form_id, int $candidate_post_id ): int {
	global $wpdb;
	$entry_key = 'twinevia_entry_event_' . $form_id . '_' . $entry_id;
	$bound_entry = absint( (string) get_option( $entry_key, '0' ) );
	if ( '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not read the retained entry event binding.' ); }
	if ( 0 < $bound_entry ) { return $bound_entry; }
	// A form is one event source. Reusing it requires an explicit migration/new form,
	// never reinterpreting its old entries through a newly changed option map.
	$bound_form = absint( (string) get_post_meta( $form_id, '_twinevia_event_post_id', true ) );
	if ( '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not read the retained form event binding.' ); }
	if ( 0 === $bound_form && 0 < $candidate_post_id ) {
		$verified_post_id = twinevia_event_sync_initial_form_event( $form_id, $candidate_post_id );
		add_post_meta( $form_id, '_twinevia_event_post_id', $verified_post_id, true );
		if ( '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not persist the verified form event binding.' ); }
		$bound_form = absint( (string) get_post_meta( $form_id, '_twinevia_event_post_id', true ) );
	}
	if ( 0 >= $bound_form ) { throw new RuntimeException( 'This form has no verified event binding.' ); }
	if ( 0 < $candidate_post_id && $candidate_post_id !== $bound_form ) {
		twinevia_event_sync_log_warning( 'Mapping change retained the original form event. Use a separate form for the new event.', [ 'form_id' => $form_id, 'bound_post_id' => $bound_form ] );
	}
	if ( 0 < $entry_id ) { add_option( $entry_key, (string) $bound_form, '', false ); }
	return $bound_form;
}

function twinevia_event_sync_utc_revision_time(): string {
	return ( new DateTimeImmutable( 'now', new DateTimeZone( 'UTC' ) ) )->format( 'Y-m-d\TH:i:s.u\Z' );
}

function twinevia_event_sync_version_booking( array $booking, ?string $snapshot_at = null ): array {
	global $wpdb;
	$identity = $booking['provider'] . ':' . $booking['source_scope'] . ':' . $booking['booking_id'];
	$key = 'twinevia_booking_rev_' . hash( 'sha256', $identity );
	$lock = 'twinevia_rev_' . substr( hash( 'sha256', $identity ), 0, 40 );
	if ( '1' !== (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, 5)', $lock ) ) ) {
		throw new RuntimeException( 'Could not serialize booking revision capture; retry this source update.' );
	}
	try {
		$content = $booking;
		unset( $content['updated_at'], $content['source_revision'], $content['sms_consent_captured_at'] );
		$digest = hash( 'sha256', wp_json_encode( $content ) );
		// A locking read bypasses stale WordPress object-cache entries and reads the
		// latest committed revision even inside a consistent inventory transaction.
		$stored = $wpdb->get_var( $wpdb->prepare( "SELECT option_value FROM {$wpdb->options} WHERE option_name = %s FOR UPDATE", $key ) );
		$previous = is_string( $stored ) ? maybe_unserialize( $stored ) : [];
		if ( null !== $snapshot_at && is_array( $previous ) && ! empty( $previous['updated_at'] )
			&& new DateTimeImmutable( (string) $previous['updated_at'] ) > new DateTimeImmutable( $snapshot_at ) ) {
			throw new RuntimeException( 'Source changed after inventory capture began; retry with a newer snapshot.' );
		}
		if ( ! is_array( $previous ) || ( $previous['digest'] ?? '' ) !== $digest ) {
			$previous = [ 'digest' => $digest, 'revision' => max( 1, (int) ( $previous['revision'] ?? 0 ) + 1 ), 'updated_at' => $snapshot_at ?? twinevia_event_sync_utc_revision_time() ];
			update_option( $key, $previous, false );
		}
		$booking['source_revision'] = (int) $previous['revision'];
		$booking['updated_at'] = (string) $previous['updated_at'];
		return $booking;
	} finally {
		$wpdb->get_var( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
	}
}

function twinevia_event_sync_wpforms_party_size( int $form_id, array $fields ): int {
	$map = (array) apply_filters( 'twinevia_event_sync_wpforms_party_size_fields', [] );
	if ( isset( $map[ $form_id ] ) ) {
		foreach ( $fields as $field ) {
			if ( (string) ( $field['id'] ?? '' ) === (string) $map[ $form_id ] ) {
				$value = twinevia_event_sync_wpforms_field_value( $field );
				if ( ! preg_match( '/^\s*(\d+)\s*$/', $value, $matches ) || (int) $matches[1] < 1 || (int) $matches[1] > 10000 ) {
					throw new RuntimeException( 'Configured party-size field must contain a positive whole number.' );
				}
				return (int) $matches[1];
			}
		}
		throw new RuntimeException( 'Configured party-size field was absent; inventory stopped.' );
	}
	return (int) apply_filters( 'twinevia_event_sync_wpforms_party_size', 1, $form_id, $fields );
}

function twinevia_event_sync_wpforms_payload( int $entry_id, int $form_id, array $fields, array $entry, int $candidate_post_id, ?string $snapshot_at = null ): array {
	$post_id = twinevia_event_sync_bound_event( $entry_id, $form_id, $candidate_post_id );
	$status = strtolower( trim( (string) ( $entry['status'] ?? '' ) ) );
	$status = '' === $status ? 'completed' : $status;
	$active = in_array( $status, [ 'completed', 'publish', 'published', 'submitted', 'pending' ], true );
	$active = (bool) apply_filters( 'twinevia_event_sync_wpforms_active', $active, $form_id, $entry_id, $entry );
	$consent_field_id = twinevia_event_sync_wpforms_consent_field_id( $form_id );
	$consent = twinevia_event_sync_wpforms_submission_sms_consent( $fields, $consent_field_id );
	$consent = apply_filters( 'twinevia_event_sync_wpforms_sms_consent', $consent, $form_id, $fields );
	$booking = [ 'provider' => 'wpforms', 'booking_id' => (string) $entry_id,
		'source_scope' => 'wpforms:' . $form_id, 'form_id' => (string) $form_id,
		'status' => $status, 'active' => $active, 'person_id' => (string) ( $entry['user_id'] ?? '' ),
		'name' => twinevia_event_sync_wpforms_submission_name( $fields ),
		'phone' => twinevia_event_sync_wpforms_submission_phone( $fields ),
		'spaces' => twinevia_event_sync_wpforms_party_size( $form_id, $fields ),
		'sms_consent' => $consent, 'sms_consent_source' => 'wpforms:field:' . $consent_field_id,
		'sms_consent_text_version' => twinevia_event_sync_consent_text_version(),
		'sms_consent_captured_at' => (string) ( $entry['date_modified'] ?? gmdate( 'c' ) ),
	];
	return [ 'action' => 'booking_upsert', 'source' => twinevia_event_sync_source( 'wpforms' ),
		'event' => twinevia_event_sync_event_payload_from_post_id( $post_id ),
		'booking' => twinevia_event_sync_version_booking( $booking, $snapshot_at ) ];
}

function twinevia_event_sync_queue_inventory( array $event, string $source, string $scope, array $bookings, string $watermark ): void {
	$pages = array_chunk( $bookings, TWINEVIA_EVENT_SYNC_RECONCILE_BATCH_SIZE );
	if ( [] === $pages ) { $pages = [ [] ]; }
	$manifest = [ 'id' => wp_generate_uuid4(), 'source_scope' => $scope, 'watermark' => $watermark,
		'pages' => count( $pages ), 'records' => count( $bookings ) ];
	$hashes = [];
	foreach ( $pages as $index => $records ) {
		$encoded = wp_json_encode( $records );
		if ( ! is_string( $encoded ) ) { throw new RuntimeException( 'Could not encode inventory records.' ); }
		$hashes[] = hash( 'sha256', $encoded );
		twinevia_event_sync_send_payload( [ 'action' => 'inventory_page', 'protocol_version' => 2,
			'source' => $source, 'event' => $event, 'inventory' => $manifest,
			'page' => $index + 1, 'bookings' => $records, 'records_json' => $encoded ] );
	}
	twinevia_event_sync_send_payload( [ 'action' => 'inventory_complete', 'protocol_version' => 2,
		'source' => $source, 'event' => $event, 'inventory' => $manifest,
		'completion_proof' => hash( 'sha256', implode( "\n", $hashes ) ) ] );
}

function twinevia_event_sync_inventory_event( int $post_id, int $form_id = 0 ): void {
	global $wpdb;
	if ( ! twinevia_event_sync_ready() ) { throw new RuntimeException( 'Twinevia integration is not configured.' ); }
	$event = twinevia_event_sync_event_payload_from_post_id( $post_id );
	$scopes = [];
	// All source rows are read from one database snapshot. Failure queues no completion.
	$wpdb->query( 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ' );
	if ( false === $wpdb->query( 'START TRANSACTION WITH CONSISTENT SNAPSHOT' ) ) { throw new RuntimeException( 'Could not start inventory snapshot.' ); }
	try {
		$watermark = twinevia_event_sync_utc_revision_time();
		if ( 0 === $form_id ) {
			$ids = $wpdb->get_col( $wpdb->prepare( 'SELECT booking_id FROM ' . twinevia_event_sync_bookings_table() . ' WHERE event_id = %d ORDER BY booking_id', (int) $event['event_id'] ) );
			if ( ! is_array( $ids ) || '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not read complete Events Manager roster.' ); }
			$bookings = [];
			foreach ( $ids as $id ) {
				$booking = em_get_booking( (int) $id );
				if ( ! is_object( $booking ) ) { throw new RuntimeException( 'A source booking could not be loaded; inventory stopped.' ); }
				$bookings[] = twinevia_event_sync_booking_payload( $booking, $watermark )['booking'];
			}
			$scopes[] = [ twinevia_event_sync_source( 'events_manager' ), 'events_manager:' . $event['event_id'], $bookings ];
		}
		$map = twinevia_event_sync_checked_wpforms_map();
		$post = get_post( $post_id );
		if ( ! $post instanceof WP_Post || '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not inspect the selected event form content.' ); }
		foreach ( twinevia_event_sync_wpforms_form_ids_from_content( (string) $post->post_content ) as $embedded_form ) {
			if ( ! isset( $map[ $embedded_form ] ) ) { $map[ $embedded_form ] = $post_id; }
		}
		if ( 0 < $form_id ) { $map = [ $form_id => $post_id ]; }
		foreach ( $map as $mapped_form => $mapped_post ) {
			$retained = absint( (string) get_post_meta( (int) $mapped_form, '_twinevia_event_post_id', true ) );
			if ( '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not inspect a retained form binding.' ); }
			if ( ( $retained && $retained !== $post_id ) || ( ! $retained && (int) $mapped_post !== $post_id ) ) { continue; }
			$bound = twinevia_event_sync_bound_event( 0, (int) $mapped_form, (int) $mapped_post );
			if ( $bound !== $post_id ) { continue; }
			$entries = $wpdb->get_results( $wpdb->prepare( 'SELECT entry_id,form_id,user_id,status,fields,date_modified FROM ' . twinevia_event_sync_wpforms_entries_table() . ' WHERE form_id = %d ORDER BY entry_id', (int) $mapped_form ), ARRAY_A );
			if ( ! is_array( $entries ) || '' !== $wpdb->last_error ) { throw new RuntimeException( 'Could not read complete WPForms roster.' ); }
			$bookings = [];
			foreach ( $entries as $entry ) {
				$fields = json_decode( (string) $entry['fields'], true );
				if ( ! is_array( $fields ) ) { throw new RuntimeException( 'A source entry has invalid fields; inventory stopped.' ); }
				$bookings[] = twinevia_event_sync_wpforms_payload( (int) $entry['entry_id'], (int) $mapped_form, array_values( $fields ), $entry, $post_id, $watermark )['booking'];
			}
			$scopes[] = [ twinevia_event_sync_source( 'wpforms' ), 'wpforms:' . $mapped_form, $bookings ];
		}
		if ( false === $wpdb->query( 'COMMIT' ) ) { throw new RuntimeException( 'Inventory snapshot commit failed.' ); }
	} catch ( Throwable $exception ) { $wpdb->query( 'ROLLBACK' ); throw $exception; }
	foreach ( $scopes as $scope ) {
		twinevia_event_sync_queue_inventory( $event, $scope[0], $scope[1], $scope[2], $watermark );
	}
}
