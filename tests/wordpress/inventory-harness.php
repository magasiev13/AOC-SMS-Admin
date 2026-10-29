<?php
/** Standalone WordPress API simulation: php tests/wordpress/inventory-harness.php */
define( 'ABSPATH', __DIR__ );
define( 'TWINEVIA_EVENT_SYNC_ENABLED', true );
define( 'TWINEVIA_EVENT_SYNC_WEBHOOK_URL', 'https://example.invalid/events' );
define( 'TWINEVIA_EVENT_SYNC_WEBHOOK_SECRET', 'isolated-test-secret' );
$options = []; $post_meta = []; $deliveries = []; $filter_callbacks = [];
$fixture_posts = []; $fixture_event_rows = []; $fixture_entries = [];
function add_action( ...$args ) {}
function add_filter( $name, $callback, ...$args ) { global $filter_callbacks; $filter_callbacks[$name][] = $callback; }
function apply_filters( $name, $value, ...$args ) { global $filter_callbacks; foreach ( $filter_callbacks[$name] ?? [] as $cb ) { $value = $cb( $value, ...$args ); } return $value; }
function get_option( $key, $default = false ) { global $options, $wpdb; $wpdb->last_error = $wpdb->fail_map_read && 'twinevia_event_sync_wpforms_event_map' === $key ? 'Simulated map read failure.' : ''; return $wpdb->last_error ? $default : ( $options[$key] ?? $default ); }
function add_option( $key, $value, ...$args ) { global $options; if ( isset( $options[$key] ) ) { return false; } $options[$key] = $value; return true; }
function update_option( $key, $value, ...$args ) { global $options; $options[$key] = $value; return true; }
function get_post_meta( $id, $key, ...$args ) { global $post_meta, $wpdb; $wpdb->last_error = ''; return $post_meta[$id][$key] ?? ''; }
function add_post_meta( $id, $key, $value, ...$args ) { global $post_meta; if ( isset( $post_meta[$id][$key] ) ) { return false; } $post_meta[$id][$key] = $value; return true; }
function absint( $value ) { return abs( (int) $value ); }
function wp_json_encode( $value ) { return json_encode( $value ); }
function wp_generate_uuid4() { static $id = 0; return 'test-'.++$id; }
function wp_schedule_single_event( $time, $hook, $args, ...$rest ) { global $deliveries; $deliveries[] = $args[0]; return true; }
function is_wp_error( $result ) { return false; }
function maybe_unserialize( $value ) { return unserialize( $value ); }
class WP_Post {
    public $ID; public $post_content; public $post_type = 'event'; public $post_status = 'publish'; public $post_name;
    function __construct( $id, $content ) { $this->ID = $id; $this->post_content = $content; $this->post_name = 'event-' . $id; }
}
function get_post( $id ) { global $fixture_posts; return $fixture_posts[$id] ?? null; }
function get_the_title( $id ) { return 'Fixture event ' . $id; }
function get_permalink( $id ) { return 'https://example.invalid/event/' . $id; }
function get_post_modified_time( ...$args ) { return '2026-10-29T00:00:00+00:00'; }
function wp_timezone() { return new DateTimeZone( 'UTC' ); }
function wp_timezone_string() { return 'UTC'; }
class InventoryTestDatabase {
    public $options = 'wp_options'; public $posts = 'wp_posts'; public $prefix = 'wp_'; public $last_error = '';
    public $fail_discovery = false; public $fail_map_read = false; public $fail_event_rows = []; private $snapshot = null;
    function esc_like( $value ) { return addcslashes( $value, '_%\\' ); }
    function prepare( $sql, ...$args ) {
        foreach ( $args as $arg ) { $sql = preg_replace_callback( '/%[sd]/', function ( $match ) use ( $arg ) { return '%d' === $match[0] ? (string) (int) $arg : "'" . addslashes( (string) $arg ) . "'"; }, $sql, 1 ); }
        return $sql;
    }
    function query( $sql ) {
        global $options, $post_meta; $this->last_error = '';
        if ( 0 === strpos( $sql, 'START TRANSACTION' ) ) { $this->snapshot = [ $options, $post_meta ]; }
        elseif ( 'ROLLBACK' === $sql && null !== $this->snapshot ) { [ $options, $post_meta ] = $this->snapshot; $this->snapshot = null; }
        elseif ( 'COMMIT' === $sql ) { $this->snapshot = null; }
        return 0;
    }
    function get_var( $sql ) {
        global $options; $this->last_error = '';
        if ( false !== strpos( $sql, 'GET_LOCK' ) || false !== strpos( $sql, 'RELEASE_LOCK' ) ) { return '1'; }
        if ( preg_match( "/option_name = '([^']+)'/", $sql, $matches ) ) { return isset( $options[$matches[1]] ) ? serialize( $options[$matches[1]] ) : null; }
        throw new RuntimeException( 'Unexpected test query.' );
    }
    function get_row( $sql, ...$args ) {
        global $fixture_event_rows; $this->last_error = '';
        if ( preg_match( '/e\.post_id = (\d+)/', $sql, $matches ) ) {
            if ( in_array( (int) $matches[1], $this->fail_event_rows, true ) ) { $this->last_error = 'Simulated event metadata read failure.'; return null; }
            return $fixture_event_rows[(int) $matches[1]] ?? null;
        }
        throw new RuntimeException( 'Unexpected fixture row query.' );
    }
    function get_col( $sql ) { $this->last_error = ''; return []; }
    function get_results( $sql, ...$args ) {
        global $fixture_posts, $fixture_entries; $this->last_error = '';
        if ( false !== strpos( $sql, 'FROM wp_posts' ) ) {
            $rows = array_values( array_map( function ( $post ) { return [ 'ID' => $post->ID, 'post_content' => $post->post_content ]; }, array_filter( $fixture_posts, function ( $post ) { return in_array( $post->post_status, [ 'publish', 'future' ], true ); } ) ) );
            if ( $this->fail_discovery ) { $this->last_error = 'Simulated incomplete event discovery.'; return array_slice( $rows, 0, 1 ); }
            return preg_match( '/LIMIT\s+(\d+)/i', $sql, $matches ) ? array_slice( $rows, 0, (int) $matches[1] ) : $rows;
        }
        if ( preg_match( '/FROM wp_wpforms_entries WHERE form_id = (\d+)/', $sql, $matches ) ) { return $fixture_entries[(int) $matches[1]] ?? []; }
        throw new RuntimeException( 'Unexpected fixture collection query.' );
    }
}
$wpdb = new InventoryTestDatabase();
define( 'ARRAY_A', 'ARRAY_A' );
function fixture_event( $id, $event_id, $forms, $date = '2030-01-01' ) {
    global $fixture_posts, $fixture_event_rows;
    $fixture_posts[$id] = new WP_Post( $id, implode( ' ', array_map( function ( $form ) { return '[wpforms id="' . $form . '"]'; }, $forms ) ) );
    $fixture_event_rows[$id] = [ 'event_id' => $event_id, 'event_start_date' => $date, 'event_start_time' => '10:00:00', 'event_end_date' => $date, 'event_end_time' => '12:00:00' ];
}
fixture_event( 100, 1, [ 2 ] );

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


function reset_inventory_fixture() {
    global $options, $post_meta, $deliveries, $fixture_posts, $fixture_event_rows, $fixture_entries, $wpdb;
    $options = []; $post_meta = []; $deliveries = []; $fixture_posts = []; $fixture_event_rows = []; $fixture_entries = [];
    $wpdb->fail_discovery = false; $wpdb->fail_map_read = false; $wpdb->fail_event_rows = []; $wpdb->last_error = '';
}
function rejected_inventory( $post_id, $form_id = 0 ) {
    try { twinevia_event_sync_inventory_event( $post_id, $form_id ); return false; } catch ( RuntimeException $error ) { return true; }
}
reset_inventory_fixture();
fixture_event( 301, 31, [ 7 ] ); fixture_event( 302, 32, [ 7 ] );
verify( rejected_inventory( 301 ) && ! isset( $post_meta[7] ) && [] === $deliveries, 'shared form on two future events cannot be bound or emit inventory completion' );
reset_inventory_fixture();
fixture_event( 303, 33, [ 8 ] );
twinevia_event_sync_inventory_event( 303 );
verify( 303 === (int) get_post_meta( 8, '_twinevia_event_post_id', true ) && count( $deliveries ) === 4, 'unique published future event binds and completes both source scopes' );
reset_inventory_fixture();
fixture_event( 304, 34, [ 9 ] ); fixture_event( 305, 35, [ 9 ] );
$options[TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP] = wp_json_encode( [ 9 => 305 ] );
twinevia_event_sync_inventory_event( 305 );
verify( 305 === (int) get_post_meta( 9, '_twinevia_event_post_id', true ), 'explicit map resolves a shared form to its selected event' );
reset_inventory_fixture();
fixture_event( 306, 36, [ 10 ], '2020-01-01' );
twinevia_event_sync_inventory_event( 306, 10 );
verify( 306 === (int) get_post_meta( 10, '_twinevia_event_post_id', true ) && count( $deliveries ) === 2, 'selected uniquely embedded past event still reconciles' );
reset_inventory_fixture();
fixture_event( 307, 37, [ 11 ], '2020-01-01' ); fixture_event( 308, 38, [ 11 ] );
verify( rejected_inventory( 308, 11 ) && ! isset( $post_meta[11] ) && [] === $deliveries, 'form selection does not override reuse across past and future events' );
reset_inventory_fixture();
fixture_event( 309, 39, [ 12 ], '2020-01-01' ); fixture_event( 310, 40, [ 12 ] );
$post_meta[12]['_twinevia_event_post_id'] = 309;
$options[TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP] = wp_json_encode( [ 12 => 310 ] );
twinevia_event_sync_inventory_event( 309, 12 );
verify( 309 === (int) get_post_meta( 12, '_twinevia_event_post_id', true ), 'retained binding wins over changed mapping and shared embeddings' );
reset_inventory_fixture();
fixture_event( 311, 41, [ 13 ] ); $wpdb->fail_discovery = true;
verify( rejected_inventory( 311 ) && ! isset( $post_meta[13] ) && [] === $deliveries, 'incomplete discovery read cannot bind or emit completion' );
reset_inventory_fixture();
fixture_event( 312, 42, [ 14 ] ); $wpdb->fail_map_read = true;
verify( rejected_inventory( 312 ) && ! isset( $post_meta[14] ) && [] === $deliveries, 'mapping read failure cannot fall back to automatic binding' );
reset_inventory_fixture();
fixture_event( 313, 43, [ 15 ] ); $wpdb->fail_event_rows = [ 313 ];
verify( rejected_inventory( 313 ) && ! isset( $post_meta[15] ) && [] === $deliveries, 'unreadable event metadata cannot persist a form binding' );
reset_inventory_fixture();
fixture_event( 314, 44, [], '2020-01-01' );
$options[TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP] = wp_json_encode( [ 16 => 314 ] );
twinevia_event_sync_inventory_event( 314, 16 );
verify( 314 === (int) get_post_meta( 16, '_twinevia_event_post_id', true ), 'explicit past-event mapping works without an embedded shortcode' );
reset_inventory_fixture();
for ( $index = 0; $index < 22; $index++ ) { fixture_event( 400 + $index, 50 + $index, [ 0 === $index || 21 === $index ? 17 : 100 + $index ] ); }
verify( rejected_inventory( 400 ) && ! isset( $post_meta[17] ) && [] === $deliveries, 'binding discovery checks beyond the old twenty-event candidate window' );
reset_inventory_fixture();
fixture_event( 501, 81, [ 18 ] ); fixture_event( 502, 82, [] );
$options[TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP] = wp_json_encode( [ 19 => 502 ] );
twinevia_event_sync_inventory_event( 501 );
verify( 501 === (int) get_post_meta( 18, '_twinevia_event_post_id', true ) && ! isset( $post_meta[19] ), 'event inventory does not persist unrelated configured form bindings' );
reset_inventory_fixture();
fixture_event( 503, 83, [ 20 ] ); fixture_event( 504, 84, [ 20 ] );
$options['twinevia_entry_event_20_200'] = '503';
verify( 503 === twinevia_event_sync_bound_event( 200, 20, 504 ) && ! isset( $post_meta[20] ), 'retained entry binding stays immutable even when a form is shared' );
reset_inventory_fixture();
fixture_event( 505, 85, [] ); $wpdb->fail_map_read = true;
verify( rejected_inventory( 505 ) && [] === $deliveries, 'mapping read failure emits no partial completion for an empty event' );
reset_inventory_fixture();
fixture_event( 506, 86, [ 21 ] ); $options[TWINEVIA_EVENT_SYNC_OPTION_WPFORMS_EVENT_MAP] = '{invalid json';
verify( rejected_inventory( 506 ) && ! isset( $post_meta[21] ) && [] === $deliveries, 'malformed explicit mapping cannot silently fall back to discovery' );
reset_inventory_fixture();
fixture_event( 100009631, 58, [ 100009626 ], '2026-10-10' );
fixture_event( 100009684, 59, [ 100009682 ], '2026-10-31' );
twinevia_event_sync_inventory_event( 100009631 ); twinevia_event_sync_inventory_event( 100009684 );
verify( 100009631 === (int) get_post_meta( 100009626, '_twinevia_event_post_id', true ) && 100009684 === (int) get_post_meta( 100009682, '_twinevia_event_post_id', true ) && count( $deliveries ) === 8, 'verified AOC past58 and current59 unique form mappings remain compatible' );
reset_inventory_fixture();
fixture_event( 507, 87, [ 22 ] ); $wpdb->fail_event_rows = [ 507 ];
$metadata_rejected = false;
try { twinevia_event_sync_bound_event( 220, 22, 507 ); } catch ( RuntimeException $error ) { $metadata_rejected = true; }
verify( $metadata_rejected && ! isset( $post_meta[22] ) && ! isset( $options['twinevia_entry_event_22_220'] ), 'direct entry binding verifies event metadata before persisting either identity' );
echo "ALL WORDPRESS INVENTORY CHECKS PASSED\n";
