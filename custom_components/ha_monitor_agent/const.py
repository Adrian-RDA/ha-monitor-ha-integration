"""Constants for HA Monitor's outbound agent."""

DOMAIN = "ha_monitor_agent"
VERSION = "0.3.0"
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

CONF_MONITORING_ENABLED = "monitoring_enabled"
CONF_EXPORT_METRICS = "export_metrics"
CONF_SYSTEM_UPDATES = "system_updates"
CONF_UPDATE_ENTITIES = "update_entities"
CONF_DISCOVER_UPDATES = "discover_updates"
CONF_EXPORT_ALL_UPDATES = "export_all_updates"
CONF_MONITORING_SCHEMA_VERSION = "monitoring_schema_version"
MONITORING_SCHEMA_VERSION = 2
MAX_MONITORING_BODY_BYTES = 2_097_152
MONITORING_INTERVAL = 60
METRIC_KEYS = ("cpu_percent", "memory_percent", "storage_percent")
INVENTORY_KEYS = ("entities", "devices", "automations", "scenes", "integrations")
