<?php
/** Standalone WordPress API simulation: php tests/wordpress/inventory-harness.php */
define( 'ABSPATH', __DIR__ );
define( 'TWINEVIA_EVENT_SYNC_ENABLED', true );
define( 'TWINEVIA_EVENT_SYNC_WEBHOOK_URL', 'https://example.invalid/events' );
define( 'TWINEVIA_EVENT_SYNC_WEBHOOK_SECRET', 'isolated-test-secret' );
$options = []; $post_meta = []; $deliveries = []; $filter_callbacks = [];
function add_action( ...$args ) {}
function add_filter( $name, $callback, ...$args ) { global $filter_callbacks; $filter_callbacks[$name][] = $callback; }
function apply_filters( $name, $value, ...$args ) { global $filter_callbacks; foreach ( $filter_callbacks[$name] ?? [] as $cb ) { $value = $cb( $value, ...$args ); } return $value; }
function get_option( $key, $default = false ) { global $options; return $options[$key] ?? $default; }
function add_option( $key, $value, ...$args ) { global $options; if ( isset( $options[$key] ) ) { return false; } $options[$key] = $value; return true; }
function update_option( $key, $value, ...$args ) { global $options; $options[$key] = $value; return true; }
function get_post_meta( $id, $key, ...$args ) { global $post_meta; return $post_meta[$id][$key] ?? ''; }
function add_post_meta( $id, $key, $value, ...$args ) { global $post_meta; if ( isset( $post_meta[$id][$key] ) ) { return false; } $post_meta[$id][$key] = $value; return true; }
function absint( $value ) { return abs( (int) $value ); }
function wp_json_encode( $value ) { return json_encode( $value ); }
function wp_generate_uuid4() { static $id = 0; return 'test-'.++$id; }
function wp_schedule_single_event( $time, $hook, $args, ...$rest ) { global $deliveries; $deliveries[] = $args[0]; return true; }
function is_wp_error( $result ) { return false; }
function maybe_unserialize( $value ) { return unserialize( $value ); }
class InventoryTestDatabase {
    public $options = 'wp_options';
    function prepare( $sql, ...$args ) { foreach ( $args as $arg ) { $sql = preg_replace( '/%s/', "'" . addslashes( (string) $arg ) . "'", $sql, 1 ); } return $sql; }
    function get_var( $sql ) {
        global $options;
        if ( false !== strpos( $sql, 'GET_LOCK' ) || false !== strpos( $sql, 'RELEASE_LOCK' ) ) { return '1'; }
        if ( preg_match( "/option_name = '([^']+)'/", $sql, $matches ) ) { return isset( $options[$matches[1]] ) ? serialize( $options[$matches[1]] ) : null; }
        throw new RuntimeException( 'Unexpected test query.' );
    }
}
$wpdb = new InventoryTestDatabase();

require __DIR__ . '/../../integrations/wordpress/twinevia-event-sync.php';
function verify( $condition, $label ) { if ( ! $condition ) { throw new RuntimeException( $label ); } echo "PASS $label\n"; }
verify( twinevia_event_sync_bound_event( 10, 2, 100 ) === 100, 'first entry binds form and entry' );
verify( twinevia_event_sync_bound_event( 10, 2, 200 ) === 100, 'changed map preserves existing entry' );
verify( twinevia_event_sync_bound_event( 11, 2, 200 ) === 100, 'reused form retains original event' );
$first = twinevia_event_sync_version_booking( [ 'provider'=>'wpforms','source_scope'=>'wpforms:2','booking_id'=>'10','phone'=>'+17205550101' ] );
$replay = twinevia_event_sync_version_booking( [ 'provider'=>'wpforms','source_scope'=>'wpforms:2','booking_id'=>'10','phone'=>'+17205550101' ] );
verify( $first['source_revision'] === $replay['source_revision'] && $first['updated_at'] === $replay['updated_at'], 'replayed source state retains revision' );
$changed = twinevia_event_sync_version_booking( [ 'provider'=>'wpforms','source_scope'=>'wpforms:2','booking_id'=>'10','phone'=>'' ] );
verify( $changed['source_revision'] > $replay['source_revision'], 'clearing phone advances source revision' );
try {
    twinevia_event_sync_version_booking( [ 'provider'=>'wpforms','source_scope'=>'wpforms:2','booking_id'=>'10','phone'=>'+17205550101' ], '2020-01-01T00:00:00Z' );
    throw new RuntimeException( 'old snapshot accepted over a later source change' );
} catch ( RuntimeException $error ) { verify( strpos( $error->getMessage(), 'Source changed after inventory' ) !== false, 'older snapshot cannot overwrite a later hook revision' ); }
$snapshot = twinevia_event_sync_version_booking( [ 'provider'=>'wpforms','source_scope'=>'wpforms:2','booking_id'=>'new-snapshot','phone'=>'' ], '2026-01-01T00:00:00.123456Z' );
verify( $snapshot['updated_at'] === '2026-01-01T00:00:00.123456Z', 'new snapshot identities retain the capture start watermark' );

add_filter( 'twinevia_event_sync_wpforms_party_size_fields', function ( $map ) { return [ 2=>4 ]; } );
verify( twinevia_event_sync_wpforms_party_size( 2, [ ['id'=>4,'value'=>'10'] ] ) === 10, 'configured group field preserves ten places' );
try { twinevia_event_sync_wpforms_party_size( 2, [ ['id'=>4,'value'=>'invalid'] ] ); throw new RuntimeException( 'invalid party size accepted' ); } catch ( RuntimeException $error ) { verify( strpos( $error->getMessage(), 'positive whole number' ) !== false, 'invalid party size stops inventory' ); }
$rows = array_fill( 0, 51, [ 'booking_id'=>'retained' ] );
twinevia_event_sync_queue_inventory( ['event_id'=>'100'], 'wordpress_wpforms', 'wpforms:2', $rows, '2026-10-29T18:00:00Z' );
verify( count( $deliveries ) === 3 && $deliveries[2]['action'] === 'inventory_complete', 'completion queued after every bounded page' );
$hashes = [ hash( 'sha256', $deliveries[0]['records_json'] ), hash( 'sha256', $deliveries[1]['records_json'] ) ];
verify( hash( 'sha256', implode( "\n", $hashes ) ) === $deliveries[2]['completion_proof'], 'cross-page proof binds exact UTF8 payloads' );
echo "ALL WORDPRESS INVENTORY CHECKS PASSED\n";
