#!/usr/bin/env bash
# =============================================================================
# test_resume_restart.sh — Systematic pause/resume + restart diagnostic
# for Type 2 (monolith) simulation
#
# Tests every step of product and influencer pipelines for:
#   Phase A: restart-from-step (fast, ~5 min total)
#   Phase B: pause + resume   (slow, ~60-70 min total)
#
# Usage:
#   bash test_resume_restart.sh              # run both phases
#   bash test_resume_restart.sh restart      # Phase A only
#   bash test_resume_restart.sh resume       # Phase B only
# =============================================================================

BASE="http://127.0.0.1:8000"
AUTH="Authorization: Bearer your-internal-token"
CONTENT_TYPE="Content-Type: application/json"

# Timeouts
POLL_INTERVAL=2            # seconds between status polls
COMPLETE_TIMEOUT=180       # max wait for completion after restart (seconds)
RESUME_TIMEOUT=300         # max wait for completion after resume (seconds)
PAUSE_WAIT=60              # seconds to wait between pause and resume
SIM_DURATION="5m"          # simulation_duration for pause/resume tests

# Output files
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AUDIT_DIR="$SCRIPT_DIR/../documents/audit"
RESULTS_FILE="$AUDIT_DIR/results_raw.tsv"
LOG_FILE="$AUDIT_DIR/test_run.log"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# =============================================================================
# Helpers
# =============================================================================

log() {
    local msg="[$(date '+%H:%M:%S')] $*"
    echo -e "$msg"
    echo -e "$msg" >> "$LOG_FILE"
}

# Get steps for a video type (avoids nameref issues)
get_steps() {
    local vtype="$1"
    if [[ "$vtype" == "product video" ]]; then
        echo "step_0 step_1 step_2 step_2.5 step_2.7 step_3 steps_4_7 step_8 step_9"
    else
        echo "step_0 step_1 step_2 step_2.7 step_3 steps_4_7 step_7.5 step_8 step_9"
    fi
}

# Parse JSON field safely. Args: json_string, field_name, default_value
json_field() {
    echo "$1" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    v = d.get('$2', '$3')
    print('' if v is None else v)
except:
    print('$3')
" 2>/dev/null || echo "$3"
}

# Start a job, return job_id. Args: video_type, simulation_duration
start_job() {
    local vtype="$1"
    local sim_dur="${2:-none}"
    local tmpfile="/tmp/tvd_test_resp_$$.json"
    local http_code
    http_code=$(curl -s -o "$tmpfile" -w '%{http_code}' -X POST "$BASE/api/generate" \
        -H "$AUTH" -H "$CONTENT_TYPE" \
        -d "{\"video_type\":\"$vtype\",\"prompt\":\"Test for resume/restart diagnostic\",\"simulation\":true,\"simulation_type\":\"monolith\",\"simulation_duration\":\"$sim_dur\"}")

    local body
    body=$(cat "$tmpfile" 2>/dev/null)
    rm -f "$tmpfile"

    if [[ "$http_code" != "200" ]]; then
        echo "START_FAILED: HTTP $http_code" >&2
        echo ""
        return 1
    fi

    local job_id
    job_id=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id',''))")
    if [[ -z "$job_id" ]]; then
        # Debug: try alternative parsing
        job_id=$(echo "$body" | python3 -c "import sys,json,re; m=re.search(r'\"job_id\":\"([^\"]+)\"', sys.stdin.read()); print(m.group(1) if m else '')")
    fi
    if [[ -z "$job_id" ]]; then
        echo "PARSE_FAILED" >&2
        echo ""
        return 1
    fi
    echo "$job_id"
}

# Poll until terminal status. Args: job_id, timeout_seconds
# Sets: LAST_STATUS, LAST_ERROR
LAST_ERROR=""
LAST_STATUS=""
poll_until_done() {
    local job_id="$1"
    local timeout="$2"
    local elapsed=0
    local last_known_status="unknown"

    while (( elapsed < timeout )); do
        local resp
        resp=$(curl -s "$BASE/api/jobs/$job_id" -H "$AUTH" 2>/dev/null || echo '{}')
        local status
        status=$(json_field "$resp" "status" "unknown")

        if [[ -n "$status" && "$status" != "unknown" ]]; then
            last_known_status="$status"
        fi

        if [[ "$status" == "completed" || "$status" == "failed" ]]; then
            LAST_STATUS="$status"
            if [[ "$status" == "failed" ]]; then
                LAST_ERROR=$(echo "$resp" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    err = d.get('error', '') or ''
    step = d.get('failed_at_step', '') or ''
    parts = []
    if step: parts.append(f'step={step}')
    if err: parts.append(err[:200])
    print(' | '.join(parts) if parts else 'unknown error')
except:
    print('parse error')
" 2>/dev/null || echo "parse error")
            else
                LAST_ERROR=""
            fi
            return 0
        fi
        sleep "$POLL_INTERVAL"
        elapsed=$((elapsed + POLL_INTERVAL))
    done

    LAST_STATUS="timeout"
    LAST_ERROR="Timed out after ${timeout}s (last status: $last_known_status)"
    return 0
}

# Wait for job to reach terminal state, then return. Used to clean up between restarts.
# Args: job_id, timeout
wait_terminal() {
    local job_id="$1"
    local timeout="${2:-120}"
    local elapsed=0
    while (( elapsed < timeout )); do
        local status
        status=$(json_field "$(curl -s "$BASE/api/jobs/$job_id" -H "$AUTH" 2>/dev/null || echo '{}')" "status" "unknown")
        if [[ "$status" == "completed" || "$status" == "failed" || "$status" == "paused" ]]; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    # Force abort if stuck
    curl -s -X POST "$BASE/api/jobs/$job_id/abort" -H "$AUTH" > /dev/null 2>&1
    sleep 3
    return 0
}

# Record result. Args: video_type, step_id, operation, result, error
record() {
    local vtype="$1" step="$2" op="$3" result="$4" error="${5:-}"
    printf '%s\t%s\t%s\t%s\t%s\n' "$vtype" "$step" "$op" "$result" "$error" >> "$RESULTS_FILE"
    if [[ "$result" == "PASS" ]]; then
        log "  ${GREEN}PASS${NC} | $vtype | $step | $op"
    elif [[ "$result" == "SKIP" ]]; then
        log "  ${YELLOW}SKIP${NC} | $vtype | $step | $op | $error"
    else
        log "  ${RED}FAIL${NC} | $vtype | $step | $op | $error"
    fi
}

# =============================================================================
# Phase A: Restart-from-step
# =============================================================================

run_restart_tests() {
    log ""
    log "============================================================"
    log "PHASE A: Restart-from-step tests"
    log "============================================================"

    for vtype in "product video" "influencer"; do
        local steps
        steps=$(get_steps "$vtype")

        log ""
        log "--- ${CYAN}$vtype${NC}: Running initial job (instant, simulation_duration=none) ---"
        local job_id
        job_id=$(start_job "$vtype" "none")
        if [[ -z "$job_id" ]]; then
            log "${RED}Could not start initial $vtype job. Skipping.${NC}"
            for s in $steps; do
                record "$vtype" "$s" "restart" "SKIP" "Initial job failed to start"
            done
            continue
        fi
        log "  Job: $job_id"

        # Wait for completion
        poll_until_done "$job_id" 180
        if [[ "$LAST_STATUS" != "completed" ]]; then
            log "${RED}Initial $vtype job did not complete: $LAST_STATUS ($LAST_ERROR)${NC}"
            for s in $steps; do
                record "$vtype" "$s" "restart" "SKIP" "Initial job: $LAST_STATUS - $LAST_ERROR"
            done
            continue
        fi
        log "  Initial job completed."

        # Now restart from each step
        for step in $steps; do
            log "  Restarting from $step ..."

            # Issue restart
            local tmpfile="/tmp/tvd_test_restart_$$.json"
            local http_code
            http_code=$(curl -s -o "$tmpfile" -w '%{http_code}' -X POST \
                "$BASE/api/jobs/$job_id/restart?from_step=$step" \
                -H "$AUTH" 2>/dev/null)
            local body
            body=$(cat "$tmpfile" 2>/dev/null)
            rm -f "$tmpfile"

            if [[ "$http_code" != "200" ]]; then
                local detail
                detail=$(json_field "$body" "detail" "HTTP $http_code")
                record "$vtype" "$step" "restart" "FAIL" "Restart HTTP $http_code: $detail"
                # Still wait in case job started anyway
                wait_terminal "$job_id" 30
                continue
            fi

            # Restart accepted — poll for completion
            poll_until_done "$job_id" "$COMPLETE_TIMEOUT"
            if [[ "$LAST_STATUS" == "completed" ]]; then
                record "$vtype" "$step" "restart" "PASS" ""
            elif [[ "$LAST_STATUS" == "timeout" ]]; then
                record "$vtype" "$step" "restart" "FAIL" "$LAST_ERROR"
                # Abort stuck job so next restart can proceed
                curl -s -X POST "$BASE/api/jobs/$job_id/abort" -H "$AUTH" > /dev/null 2>&1
                sleep 3
                # Check if abort worked
                wait_terminal "$job_id" 30
            else
                record "$vtype" "$step" "restart" "FAIL" "$LAST_ERROR"
                # Wait for terminal state before next restart
                wait_terminal "$job_id" 30
            fi
        done
    done
}

# =============================================================================
# Phase B: Pause + Resume
# =============================================================================

# Time-based pause/resume test. Monolith Type 2 sim runs in ~23s with 5m duration.
# current_step uses monolith names (clean_product_image, vo_generation, etc.)
# not wrapper step IDs (step_0, step_1, etc.), so we use time-based pausing.
#
# Pause delay offsets (seconds after job start):
#   early=3s, mid=8s, late=15s — to catch different pipeline phases
# For each: pause, wait 60s, resume, check completion.

PAUSE_OFFSETS=(3 8 15)

run_pause_resume_tests() {
    log ""
    log "============================================================"
    log "PHASE B: Pause + Resume tests (time-based)"
    log "============================================================"
    log "  Pause offsets: ${PAUSE_OFFSETS[*]}s after job start"
    log "  Wait between pause/resume: ${PAUSE_WAIT}s"
    log ""

    for vtype in "product video" "influencer"; do
        for delay in "${PAUSE_OFFSETS[@]}"; do
            log ""
            log "--- ${CYAN}$vtype${NC} | pause at t+${delay}s | resume ---"

            # Start a fresh job
            local job_id
            job_id=$(start_job "$vtype" "$SIM_DURATION")
            if [[ -z "$job_id" ]]; then
                record "$vtype" "t+${delay}s" "resume" "SKIP" "Failed to start job"
                continue
            fi
            log "  Job: $job_id"

            # Wait the delay
            log "  Waiting ${delay}s before pause ..."
            sleep "$delay"

            # Check if job is still running
            local pre_status
            pre_status=$(json_field "$(curl -s "$BASE/api/jobs/$job_id" -H "$AUTH" 2>/dev/null || echo '{}')" "status" "unknown")
            local pre_step
            pre_step=$(json_field "$(curl -s "$BASE/api/jobs/$job_id" -H "$AUTH" 2>/dev/null || echo '{}')" "current_step" "unknown")

            if [[ "$pre_status" == "completed" ]]; then
                log "  Job already completed (step=$pre_step) — too fast for t+${delay}s pause"
                record "$vtype" "t+${delay}s" "resume" "SKIP" "Completed before pause (step=$pre_step)"
                continue
            elif [[ "$pre_status" == "failed" ]]; then
                log "  Job already failed (step=$pre_step)"
                record "$vtype" "t+${delay}s" "resume" "FAIL" "Failed before pause (step=$pre_step)"
                continue
            fi

            log "  Current step at pause: $pre_step (progress: $(json_field "$(curl -s "$BASE/api/jobs/$job_id" -H "$AUTH" 2>/dev/null || echo '{}')" "progress" "?")%)"

            # Issue pause
            log "  Pausing ..."
            local tmpfile="/tmp/tvd_test_pause_$$.json"
            local pause_http
            pause_http=$(curl -s -o "$tmpfile" -w '%{http_code}' -X POST \
                "$BASE/api/jobs/$job_id/pause" -H "$AUTH" 2>/dev/null)
            rm -f "$tmpfile"

            if [[ "$pause_http" != "200" ]]; then
                # Job might have completed/failed between check and pause
                local cur_status
                cur_status=$(json_field "$(curl -s "$BASE/api/jobs/$job_id" -H "$AUTH" 2>/dev/null || echo '{}')" "status" "unknown")
                if [[ "$cur_status" == "completed" ]]; then
                    record "$vtype" "t+${delay}s" "resume" "SKIP" "Completed before pause took effect"
                    continue
                elif [[ "$cur_status" == "failed" ]]; then
                    poll_until_done "$job_id" 5
                    record "$vtype" "t+${delay}s" "resume" "FAIL" "Failed before pause: $LAST_ERROR"
                    continue
                fi
                record "$vtype" "t+${delay}s" "resume" "FAIL" "Pause HTTP $pause_http"
                continue
            fi

            # Wait for cooperative pause to take effect (monolith checks at step boundaries)
            local pause_elapsed=0
            local actual_status="unknown"
            while (( pause_elapsed < 60 )); do
                actual_status=$(json_field "$(curl -s "$BASE/api/jobs/$job_id" -H "$AUTH" 2>/dev/null || echo '{}')" "status" "unknown")
                if [[ "$actual_status" == "paused" || "$actual_status" == "completed" || "$actual_status" == "failed" ]]; then
                    break
                fi
                sleep 2
                pause_elapsed=$((pause_elapsed + 2))
            done

            local paused_step
            paused_step=$(json_field "$(curl -s "$BASE/api/jobs/$job_id" -H "$AUTH" 2>/dev/null || echo '{}')" "current_step" "unknown")

            if [[ "$actual_status" == "completed" ]]; then
                log "  Job completed before pause boundary (step=$paused_step)"
                record "$vtype" "t+${delay}s" "resume" "SKIP" "Completed before pause boundary (step=$paused_step)"
                continue
            elif [[ "$actual_status" == "failed" ]]; then
                poll_until_done "$job_id" 5
                record "$vtype" "t+${delay}s" "resume" "FAIL" "Failed at pause: $LAST_ERROR (step=$paused_step)"
                continue
            elif [[ "$actual_status" != "paused" ]]; then
                log "  ${YELLOW}Status '$actual_status' after 60s wait, not paused${NC}"
                record "$vtype" "t+${delay}s" "resume" "FAIL" "Never paused: status=$actual_status (step=$paused_step)"
                curl -s -X POST "$BASE/api/jobs/$job_id/abort" -H "$AUTH" > /dev/null 2>&1
                sleep 3
                continue
            fi

            log "  Paused at step=$paused_step. Waiting ${PAUSE_WAIT}s ..."
            sleep "$PAUSE_WAIT"

            # Resume
            log "  Resuming ..."
            local resume_tmpfile="/tmp/tvd_test_resume_$$.json"
            local resume_http
            resume_http=$(curl -s -o "$resume_tmpfile" -w '%{http_code}' -X POST \
                "$BASE/api/jobs/$job_id/resume" -H "$AUTH" 2>/dev/null)
            local resume_body
            resume_body=$(cat "$resume_tmpfile" 2>/dev/null)
            rm -f "$resume_tmpfile"

            if [[ "$resume_http" != "200" ]]; then
                local detail
                detail=$(json_field "$resume_body" "detail" "HTTP $resume_http")
                record "$vtype" "t+${delay}s" "resume" "FAIL" "Resume rejected: $detail (paused at $paused_step)"
                continue
            fi

            # Poll for final result
            poll_until_done "$job_id" "$RESUME_TIMEOUT"
            if [[ "$LAST_STATUS" == "completed" ]]; then
                record "$vtype" "t+${delay}s" "resume" "PASS" "paused_at=$paused_step"
            else
                record "$vtype" "t+${delay}s" "resume" "FAIL" "$LAST_ERROR (paused_at=$paused_step)"
            fi
        done
    done
}

# =============================================================================
# Main
# =============================================================================

# Initialize output
mkdir -p "$AUDIT_DIR"
echo "" > "$LOG_FILE"
printf 'video_type\tstep\toperation\tresult\terror\n' > "$RESULTS_FILE"

log "Resume/Restart Diagnostic — $(date '+%Y-%m-%d %H:%M:%S')"
log "Server: $BASE"
log "Branch: $(cd "$SCRIPT_DIR/../.." && git branch --show-current 2>/dev/null || echo 'unknown')"
log "Commit: $(cd "$SCRIPT_DIR/../.." && git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"

# Check server health
health_code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/health" 2>/dev/null)
if [[ "$health_code" != "200" ]]; then
    log "${RED}Server not reachable at $BASE (HTTP $health_code). Aborting.${NC}"
    exit 1
fi
log "Server health: OK"

# Run requested phase(s)
phase="${1:-all}"
case "$phase" in
    restart)
        run_restart_tests
        ;;
    resume)
        run_pause_resume_tests
        ;;
    all)
        run_restart_tests
        run_pause_resume_tests
        ;;
    *)
        echo "Usage: $0 [restart|resume|all]"
        exit 1
        ;;
esac

log ""
log "============================================================"
log "DONE — Results in: $RESULTS_FILE"
log "============================================================"

# Print summary
echo ""
echo "=== SUMMARY ==="
total=$(tail -n +2 "$RESULTS_FILE" | wc -l | tr -d ' ')
pass=$(tail -n +2 "$RESULTS_FILE" | grep -c "PASS" || true)
fail=$(tail -n +2 "$RESULTS_FILE" | grep -c "FAIL" || true)
skip=$(tail -n +2 "$RESULTS_FILE" | grep -c "SKIP" || true)
echo "Total: $total | PASS: $pass | FAIL: $fail | SKIP: $skip"
echo ""
if [[ "$fail" -gt 0 ]]; then
    echo "Failed tests:"
    tail -n +2 "$RESULTS_FILE" | grep "FAIL" | while IFS=$'\t' read -r vt st op res err; do
        echo "  $vt | $st | $op | $err"
    done
    echo ""
fi
