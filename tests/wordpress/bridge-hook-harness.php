<?php
/** Standalone lifecycle regression: php tests/wordpress/bridge-hook-harness.php */
define('ABSPATH',__DIR__);
define('TWINEVIA_AOC_BRIDGE_ENABLED',true);
$hooks=[];$payloads=[];$saved_metadata='old-start-time';
function hook_key($callback){return is_string($callback)?$callback:spl_object_id($callback);}
function add_filter($hook,$callback,$priority=10,$accepted=1){global $hooks;$hooks[$hook][$priority][hook_key($callback)]=[$callback,$accepted];}
function add_action(...$args){add_filter(...$args);}
function remove_filter($hook,$callback,$priority=10){global $hooks;unset($hooks[$hook][$priority][hook_key($callback)]);}
function remove_action(...$args){remove_filter(...$args);}
function has_filter($hook,$callback){global $hooks;foreach($hooks[$hook]??[] as $priority=>$callbacks){if(isset($callbacks[hook_key($callback)])){return $priority;}}return false;}
function apply_filters($hook,$value,...$args){global $hooks;$callbacks=$hooks[$hook]??[];ksort($callbacks);foreach($callbacks as $items){foreach($items as [$callback,$accepted]){$value=$callback(...array_slice([$value,...$args],0,$accepted));}}return $value;}
function do_action($hook,...$args){global $hooks;$callbacks=$hooks[$hook]??[];ksort($callbacks);foreach($callbacks as $items){foreach($items as [$callback,$accepted]){$callback(...array_slice($args,0,$accepted));}}}
function absint($value){return abs((int)$value);}
function aoc_custom_twinevia_sync_enabled(){return true;}
function twinevia_event_sync_inventory_event(){}
function twinevia_event_sync_dispatch_event_post($post_id){global $payloads,$saved_metadata;$payloads[]=['post_id'=>$post_id,'metadata'=>$saved_metadata];}
function twinevia_event_sync_save_event_post($id,$post,$update){twinevia_event_sync_dispatch_event_post($id);}
function aoc_custom_twinevia_sync_saved_event($result,$event){twinevia_event_sync_dispatch_event_post($event->post_id);return $result;}
function aoc_custom_twinevia_sync_booking_upsert(){}
function aoc_custom_twinevia_sync_booking_deleted(){}
function aoc_custom_twinevia_delete_event_post(){}
function aoc_custom_twinevia_wpforms_stripe_process_complete(){}
function twinevia_event_sync_log_error($message,$context){throw new RuntimeException('Unexpected bridge error.');}
foreach(['em_booking_added','em_booking_status_changed'] as $hook){add_action($hook,'aoc_custom_twinevia_sync_booking_upsert',20,1);}
add_action('em_booking_deleted','aoc_custom_twinevia_sync_booking_deleted',20,1);
add_action('before_delete_post','aoc_custom_twinevia_delete_event_post',10,2);
add_action('wpforms_stripe_process_complete','aoc_custom_twinevia_wpforms_stripe_process_complete',20,4);
add_filter('em_event_save','aoc_custom_twinevia_sync_saved_event',9999,2);
add_action('save_post_event','twinevia_event_sync_save_event_post',50,3);
require __DIR__.'/../../integrations/wordpress/twinevia-aoc-bridge.php';
function verify($condition,$label){if(!$condition){throw new RuntimeException($label);}echo "PASS $label\n";}
do_action('plugins_loaded');
verify(false===has_filter('save_post_event','twinevia_event_sync_save_event_post'),'early post-save hook suppressed');
verify(false===has_filter('em_event_save','aoc_custom_twinevia_sync_saved_event'),'superseded legacy event-save hook suppressed');
verify(9999===has_filter('em_event_save','twinevia_aoc_bridge_saved_event'),'late event-save replacement installed');
verify(false===has_filter('em_booking_added','aoc_custom_twinevia_sync_booking_upsert')&&false===has_filter('em_booking_deleted','aoc_custom_twinevia_sync_booking_deleted'),'legacy booking hooks suppressed');
verify(false===has_filter('before_delete_post','aoc_custom_twinevia_delete_event_post')&&false===has_filter('wpforms_stripe_process_complete','aoc_custom_twinevia_wpforms_stripe_process_complete'),'legacy deletion and payment hooks suppressed');
$event=(object)['post_id'=>123];
do_action('save_post_event',123,$event,true);
verify(0===count($payloads),'post write emits no premature payload');
$saved_metadata='new-start-time-after-custom-table-save';
$result=apply_filters('em_event_save',true,$event);
verify(true===$result&&1===count($payloads)&&$payloads[0]['metadata']===$saved_metadata,'one payload uses committed Events Manager metadata');
verify(false===apply_filters('em_event_save',false,$event)&&1===count($payloads),'failed save preserves result and emits no payload');
verify(true===apply_filters('em_event_save',true,(object)[])&&1===count($payloads),'missing post identity emits no payload');
echo "ALL BRIDGE HOOK CHECKS PASSED\n";
