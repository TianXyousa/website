#!/bin/sh

set -eu

config_path="/rec/config.json"
webhook_url="${BREC_WEBHOOK_URL:-http://app:8000/api/brec/webhook}"

# BililiveRecorder stores WebHookUrlsV2 in its persistent config.  The official
# image has no JSON CLI, so patch only this known top-level field with Perl
# (available in the base image), preserving all room and user settings.
if [ ! -f "$config_path" ]; then
    printf '%s\n' '{"version":3,"global":{},"rooms":[]}' > "$config_path"
fi

WEBHOOK_JSON=$(printf '%s' "$webhook_url" | perl -0pe 's/\\/\\\\/g; s/"/\\"/g; s/\r/\\r/g; s/\n/\\n/g')
export WEBHOOK_JSON

perl -0pi -e '
    my $url = $ENV{"WEBHOOK_JSON"} // "";
    if (m/"WebHookUrlsV2"\s*:/s) {
        s/"WebHookUrlsV2"\s*:\s*\{[^{}]*\}/"WebHookUrlsV2":{"HasValue":true,"Value":"$url"}/s;
    } elsif (m/"global"\s*:\s*\{\s*\}/s) {
        s/"global"\s*:\s*\{\s*\}/"global":{"WebHookUrlsV2":{"HasValue":true,"Value":"$url"}}/s;
    } else {
        s/("global"\s*:\s*\{)/$1"WebHookUrlsV2":{"HasValue":true,"Value":"$url"},/s;
    }
' "$config_path"

echo "Configured BililiveRecorder FileClosed webhook: $webhook_url"
exec /usr/local/bin/docker-entrypoint.sh "$@"
