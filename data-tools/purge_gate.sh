# Sourced by update_japan.sh. A purge cannot be undone (the GCS backup keeps no versions), so a refusal
# only counts when it comes after the paid period and has lasted. Reasons for a refusal: plan means
# J-Quants itself said the plan does not cover a dataset; other is any other 401/403, which a firewall or
# a renamed endpoint can also produce.
#   a line "not-before YYYY-MM-DD" in the cancellation marker   no purge before that UTC day (the paid period)
#   JQ_PURGE_CONFIRM_HOURS (24)  how long a plan refusal must last
#   JQ_PURGE_OTHER_DAYS (7)      how long any other refusal must last

purge_due() {  # purge_due plan|other DENIED_FILE MARKER_FILE [NOW_EPOCH]: true when a purge may run
  local reason=$1 first=$2 marker=$3 now=${4:-$(date +%s)} need floor
  [[ -f "$first" && -f "$marker" ]] || return 1
  floor=$(awk '$1 == "not-before" {print $2; exit}' "$marker")
  if [[ -n "$floor" ]]; then
    [[ "$floor" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || return 1  # an unreadable date never allows a purge
    [[ ! "$(date -u -d "@$now" +%F)" < "$floor" ]] || return 1
  fi
  if [[ "$reason" == plan ]]; then
    need=$(( ${JQ_PURGE_CONFIRM_HOURS:-24} * 3600 ))
  else
    need=$(( ${JQ_PURGE_OTHER_DAYS:-7} * 86400 ))
  fi
  (( now - $(stat -c %Y "$first") >= need ))
}
