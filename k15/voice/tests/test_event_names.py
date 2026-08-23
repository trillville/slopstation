"""Guard: the event vocabulary is frozen. Dashboards group by event name and
alerts fire on it, so a name or a field key that disappears is a telemetry
regression; a new one is fine (add it here). Scans the emitters from source
(_events_scan), so it runs on any checkout. Run:
    .venv\\Scripts\\python tests\\test_event_names.py
"""
import re

import _bootstrap  # noqa: F401
import _events_scan as scan
import events

# name -> field keys seen at the call sites (a subset is a regression).
PYTHON = {
    "agent_stopped": {"reason"},
    "agent_up": {"dry_run", "model_source", "threshold", "wake_model"},
    "announce_cut_short": {"job"},
    "announce_earcon_failed": {"err", "job"},
    "announce_failed": {"err", "fallback", "job"},
    "announce_test_done": {"complete"},
    "announce_test_failed": {"err"},
    "announce_test_start": set(),
    "armed": {"report_type"},
    "audio_device": {"device", "index", "kind"},
    "audio_device_missing": {"kind", "wanted"},
    "audio_device_wait": {"kind", "retry_s", "waited_s", "wanted"},
    "audio_rebuild_failed": {"err", "retry_s"},
    "audio_teardown_failed": {"err"},
    "buzz_failed": {"err", "pattern"},
    "buzz_sent": {"pattern"},
    "cancel_void_failed": {"err"},
    "chord": {"turn"},
    "chord_busy": {"lock_age_s"},
    "chord_partial": {"btn", "want"},
    "collection_miss": {"fallback", "reason", "spoken"},
    "collection_resolved": {"id", "name", "spoken"},
    "config_invalid": {"err", "missing"},
    "config_suspect": {"reason", "setting", "value"},
    "deals_synced": {"specials", "wishlist"},
    "dispatch": {"detail", "intent", "ok"},
    "download_status_error": {"err"},
    "dry_run_would": {"action"},
    "earcon_audition": {"gain"},
    "earcon_failed": {"err"},
    "earcon_folded": {"earcon"},
    "earcon_play": {"earcon"},
    "end_session_dispatched": {"turn", "via"},
    "end_session_failed": {"err", "turn"},
    "end_session_refused": {"answer", "turn"},
    "enrolled": {"steamid"},
    "enter_died": {"dur_ms"},
    "enter_dispatched": {"dur_ms"},
    "enter_redispatched": {"dur_ms"},
    "enter_refused": {"answer"},
    "enter_retry": {"err"},
    "exit_dispatched": {"reason"},
    "exlink_nak": {"cmd", "err"},
    "exlink_send": {"ack", "again", "cmd"},
    "facet_failed": {"appid", "err", "facet"},
    "false_accept_soak_start": set(),
    "game_launch": {"appid", "result"},
    "game_launch_failed": {"appid", "err"},
    "gate_match": {"confidence", "intent", "text"},
    "gate_miss": {"confidence", "fallback", "reason", "text"},
    "heartbeat": {"interval_s"},
    "hltb_failed": {"err", "name"},
    "host_ready": {"dur_ms", "status", "verified"},
    "idle_deferred": {"reason"},
    "input_deferred": {"input", "reason"},
    "input_refused": {"err", "input"},
    "input_starts_session": {"input"},
    "install_error": {"appid", "err"},
    "install_failed": {"appid", "eresult"},
    "install_fallback": {"appid", "why"},
    "install_queued": {"appid", "machine", "verified"},
    "intent_unknown": {"intent"},
    "job_announce_hook_failed": {"err", "job"},
    "job_announced": {"job"},
    "job_done": {"cost_usd", "denials", "dur_ms", "job", "model", "session", "status", "stop_reason", "summary", "tools", "turns", "web_fetches", "web_searches"},
    "job_failed": {"cost_usd", "denials", "dur_ms", "err", "job", "model", "session", "status", "stop_reason", "summary", "tools", "turns", "web_fetches", "web_searches"},
    "job_orphaned": {"job", "reason"},
    "job_queued": {"job", "task"},
    "job_requested": {"ok", "task"},
    "job_running": {"dry_run", "job", "provider"},
    "keyterms_capped": {"dropped", "first_dropped", "kept"},
    "lane_disabled": {"err", "exe", "known", "provider", "reason", "what"},
    "lane_up": {"backend", "effort", "endpoint", "exe", "lane", "model", "orphans", "provider", "steamid", "token_expires", "tools", "websearch", "what"},
    "launch_aborted": {"dur_ms", "err"},
    "launch_busy": {"lock_age_s", "reason"},
    "launch_dispatched": {"answer", "appid"},
    "launch_failed": {"appid", "dur_ms", "err"},
    "launch_failure_signaled": {"reason"},
    "launch_start": {"appid", "tv"},
    "lock_kept": {"reason"},
    "lock_recycled": {"lock_age_s"},
    "meta_failed": {"appid", "err"},
    "meta_fetched": {"appid", "n", "of"},
    "nav_dispatched": {"answer", "arg", "kind"},
    "nav_failed": {"err", "kind"},
    "pipeline_error": {"err"},
    "preroll_fed": {"audio_s"},
    "puck_present": {"interfaces"},
    "puck_standoff": {"reason"},
    "puck_vanished": {"reason"},
    "pyaudio_terminate_failed": {"err"},
    "quit_dispatched": {"answer", "appid"},
    "quit_failed": {"appid", "err"},
    "ready_foreign": {"status"},
    "reconcile_cleared": {"reason"},
    "reconcile_found": set(),
    "reconcile_resumed": set(),
    "session_close": {"ending"},
    "session_crashed": {"err"},
    "session_dispatched": {"appid", "turn"},
    "session_ended": {"fails", "reason", "status"},
    "session_exit_phrase": set(),
    "session_gaming": {"dur_ms"},
    "session_idle": set(),
    "session_idle_timeout": set(),
    "session_open": set(),
    "session_stop_requested": set(),
    "ssh_up": {"dur_ms"},
    "stale_error_discarded": {"age_s"},
    "start_refused": {"lock_age_s", "reason"},
    "status_poll_failed": {"err"},
    "store_fetch_failed": {"err", "url"},
    "stt_final": {"confidence", "outcome", "text"},
    "stt_vocabulary": {"headroom", "terms", "titles"},
    "sync_done": {"games", "layer", "n"},
    "sync_failed": {"err"},
    "sync_skipped": {"err", "layer", "reason"},
    "task_cancel": {"cancelled", "running"},
    "task_spoken": {"intent", "job"},
    "title_miss": {"fallback", "reason", "spoken"},
    "title_resolved": {"appid", "spoken", "title"},
    "token_mint_failed": {"err", "stage"},
    "token_transfer_failed": {"err", "url"},
    "tool_call": {"args", "ok", "tool"},
    "tool_error": {"err", "tool"},
    "tool_refused": {"appid", "reason", "tool"},
    "trace_save_failed": {"err"},
    "trace_saved": {"file", "messages", "pruned"},
    "tracing_setup_failed": {"err"},
    "tts_fallback": {"err", "using", "wanted"},
    "tts_selected": {"engine", "local"},
    "tv_duck_deficit": {"steps"},
    "tv_duck_failed": {"err", "restore", "stage"},
    "tv_duck_skipped": {"debt", "reason", "state"},
    "tv_ducked": {"asked", "ok", "steps", "vol"},
    "tv_on": {"dur_ms"},
    "tv_state_unknown": {"dur_ms"},
    "tv_unducked": {"asked", "ok", "reason", "steps", "vol"},
    "tvremote_fail": {"cmd", "err", "n"},
    "tvremote_send": {"cmd", "n", "ok", "vol_after", "vol_before"},
    "volume_clamped": {"asked", "max", "set"},
    "wake": {"score", "trigger"},
    "wake_clip": {"clip", "secs"},
    "wake_clip_failed": {"err"},
    "wake_false": {"hours", "n", "peak", "per_hour"},
    "wake_model_download": {"model", "vendored"},
    "wake_model_missing": {"looked_in", "model"},
    "wake_near_miss": {"peak", "shortfall", "threshold"},
    "wake_prefix_stripped": {"stripped", "text"},
    "wake_stream_died": {"err"},
    "wake_trial": {"n", "peak", "score"},
    "wake_trials_start": set(),
    "wake_verifier": {"model", "verifier"},
    "wake_verifier_missing": {"looked_in", "verifier"},
    "web_search": {"kind", "query", "status"},
    "wol_sent": set(),
}

POWERSHELL = {
    "enter_failed": {"err", "primary_height", "reason"},
    "enter_start": set(),
    "exit_done": {"office_ok", "puck_ok"},
    "exit_start": set(),
    "game_launch_failed": {"err"},
    "game_launched": {"appid"},
    "game_stop_failed": {"err", "method"},
    "game_stopped": {"appid", "cleared", "method"},
    "launchgame_start": set(),
    "nav_failed": {"err"},
    "nav_fired": {"kind", "url"},
    "nav_start": set(),
    "office-safety_start": set(),
    "profile_applied": {"profile", "retried"},
    "profile_apply_failed": {"profile"},
    "profile_retry": {"profile"},
    "puck_claimed": set(),
    "puck_release_failed": set(),
    "puck_released": set(),
    "ready": {"fg", "focused", "running_appid"},
    "stopgame_start": set(),
    "verb": {"answer", "cmd", "turn", "verb"},
    "wake-safety_start": set(),
    "wake_cleanup": {"reason"},
}

BAT = {
    "deps_installed": set(),
    "lane_reloaded": set(),
    "lane_started": set(),
    "restart": set(),
    "start": set(),
}

LANES = {'manual', 'launch', 'library', 'steam', 'traces', 'listener', 'voice'}


def check(kind, frozen, now):
    gone = sorted(set(frozen) - set(now))
    assert not gone, f"{kind}: events no longer emitted: {gone} - a rename is a telemetry change; edit this file on purpose"
    for name, keys in frozen.items():
        lost = sorted(set(keys) - now[name])
        assert not lost, f"{kind} {name}: field keys gone: {lost}"
    new = sorted(set(now) - set(frozen))
    if new:
        print(f"  note: new {kind} events not yet frozen here: {new}")
    return len(now)


def owned_key_lists():
    """Every emitter-owned key list in the PowerShell (Write-CgEvent's $owned,
    Dispatch's local copy) must equal events._EMITTER_OWNED."""
    out = {}
    for f in sorted(scan.PC.glob("*.ps1")):
        for m in re.finditer(r"\$owned\s*=\s*@\(([^)]*)\)", f.read_text(encoding="utf-8")):
            out[f.name] = set(re.findall(r"'(\w+)'", m.group(1)))
    return out


def main():
    s = scan.scan()
    n = check("python", PYTHON, s["python"])
    n += check("powershell", POWERSHELL, s["powershell"])
    n += check("bat", BAT, s["bat"])
    assert s["lanes"] == LANES, f"make_log lanes changed: {sorted(s['lanes'])} vs {sorted(LANES)}"
    owned = owned_key_lists()
    assert owned, "no $owned list found in gaming-pc/*.ps1"
    for name, keys in owned.items():
        assert keys == set(events._EMITTER_OWNED), f"{name} $owned {sorted(keys)} != events._EMITTER_OWNED"
    print(f"OK - event vocabulary: {n} names across python/powershell/bat, "
          f"{len(LANES)} lanes, owned keys agree in {len(owned)} PowerShell emitter(s)")


if __name__ == "__main__":
    main()
