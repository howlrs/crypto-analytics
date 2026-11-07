# Sourced by update_japan.sh. A purge cannot be undone (the GCS backup keeps no versions), so a refusal
# has to last before it counts as the end of the subscription. Reasons for a refusal: plan means J-Quants
# itself said the plan does not cover a dataset; other is any other 401/403, which a firewall or a renamed
# endpoint can also produce, so it counts only when it outlasts the paid period by a wide margin.
#   JQ_PURGE_CONFIRM_HOURS (24)  how long a plan refusal must last
#   JQ_PURGE_OTHER_DAYS (7)      how long any other refusal must last

purge_due() {  # purge_due plan|other DENIED_FILE [NOW_EPOCH]: true when the first refusal is old enough
  local reason=$1 first=$2 now=${3:-$(date +%s)} need
  [[ -f "$first" ]] || return 1
  if [[ "$reason" == plan ]]; then
    need=$(( ${JQ_PURGE_CONFIRM_HOURS:-24} * 3600 ))
  else
    need=$(( ${JQ_PURGE_OTHER_DAYS:-7} * 86400 ))
  fi
  (( now - $(stat -c %Y "$first") >= need ))
}
