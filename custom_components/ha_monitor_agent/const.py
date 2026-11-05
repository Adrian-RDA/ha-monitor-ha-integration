"""Constants for HA Monitor's outbound agent."""

DOMAIN = "ha_monitor_agent"
VERSION = "0.1.0"
CONF_SERVER_URL = "server_url"
CONF_PAIRING_CODE = "pairing_code"
CONF_AGENT_TOKEN = "agent_token"
CONF_INSTANCE_ID = "instance_id"
CONF_ENABLED = "enabled"
CONF_HEARTBEAT_INTERVAL = "heartbeat_interval_seconds"
CONF_MAX_BATCH_EVENTS = "max_batch_events"
CONF_MAX_BODY_BYTES = "max_body_bytes"

SCHEMA_VERSION = 1
MAX_BUFFER_EVENTS = 100
MAX_BODY_BYTES = 65_536
REQUEST_TIMEOUT = 10
MAX_RESPONSE_BYTES = 65_536
BACKOFF_BASE = 5
BACKOFF_CAP = 900
MAX_RETRY_AFTER = 86_400
