"""Optional read-only checks against the installed Laravel v2 FormRequest.

Run with HA_MONITOR_LARAVEL_PATH=/path/to/ha-monitor. Sail must already be up.
No controller, database write, credential or request to a live endpoint is used.
"""

import os
from pathlib import Path
import subprocess

from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er
import pytest

from .test_reported_updates import reported_updates as reported_updates
from .test_snapshot_v2 import make_v2
from .test_updates import disabled_source, source
from .test_updates import supervisor_updates as supervisor_updates

LARAVEL_PATH = os.environ.get("HA_MONITOR_LARAVEL_PATH")
pytestmark = pytest.mark.skipif(
    not LARAVEL_PATH, reason="Optional Laravel checkout not configured"
)

PHP_VALIDATE = r"""
require 'vendor/autoload.php';
$app = require 'bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$raw = stream_get_contents(STDIN);
$request = App\Http\Requests\Api\MonitoringSnapshotV2Request::create(
    '/api/v2/agents/monitoring', 'POST', [], [], [],
    ['CONTENT_TYPE' => 'application/json', 'HTTP_ACCEPT' => 'application/json'], $raw
);
$request->setContainer($app);
$validator = Illuminate\Support\Facades\Validator::make($request->all(), $request->rules());
foreach ($request->after() as $after) {
    $validator->after($after);
}
$failed = $validator->fails();
echo json_encode(['valid' => !$failed, 'fields' => $validator->errors()->keys()], JSON_THROW_ON_ERROR);
exit($failed ? 1 : 0);
"""


async def validate_with_laravel(hass, body, *, health=False):
    def validate():
        return subprocess.run(
            [
                str(Path(LARAVEL_PATH) / "vendor/bin/sail"),
                "php",
                "-r",
                PHP_VALIDATE.replace(
                    "MonitoringSnapshotV2Request", "HealthReportRequest"
                ).replace("/api/v2/agents/monitoring", "/api/v1/agents/health")
                if health
                else PHP_VALIDATE,
            ],
            input=body,
            capture_output=True,
            cwd=LARAVEL_PATH,
            timeout=45,
            check=False,
        )

    result = await hass.async_add_executor_job(validate)
    assert result.returncode == 0, result.stdout.decode(errors="replace")


@pytest.mark.parametrize("disabled_by", list(er.RegistryEntryDisabler))
async def test_all_native_disabled_reasons_pass_real_laravel_validator(
    hass, backend, entry, disabled_by
):
    source(hass, "update.healthy", latest_version="20261101/v1.15.0-release")
    disabled_source(hass, disabled_by)
    monitor = await make_v2(hass, backend, entry)
    await validate_with_laravel(
        hass, monitor.uploader._envelope(monitor.uploader._latest)
    )


@pytest.mark.parametrize("startup", [False, True])
async def test_startup_and_161_source_partial_inventory_pass_real_laravel_validator(
    hass, backend, entry, reported_updates, startup
):
    source(
        hass,
        "update.fifth_offer",
        latest_version="9.0.3 (17 packages)",
        supported_features=0,
    )
    for i in range(20):
        entity = source(hass, f"update.disabled_{i}")
        er.async_get(hass).async_update_entity(
            entity.entity_id, disabled_by=er.RegistryEntryDisabler.HASS
        )
    for i in range(10):
        source(hass, f"update.failed_{i}", state="unavailable")
    for i in range(125):
        source(
            hass,
            f"update.current_{i}",
            state="off",
            installed_version="firmware/1",
            latest_version="firmware/1",
        )
    if startup:
        hass.set_state(CoreState.starting)
    monitor = await make_v2(hass, backend, entry)
    counts = monitor.data["updates"]["counts"]
    assert counts["total"] == (0 if startup else 161)
    if not startup:
        assert (
            counts["available"] == 5
            and counts["disabled"] == 20
            and counts["unavailable"] == 10
        )
    await validate_with_laravel(
        hass, monitor.uploader._envelope(monitor.uploader._latest)
    )


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "scenario",
    [
        "complete",
        "startup",
        "selected",
        "overflow",
        "backup_running",
        "backup_ok",
        "backup_failed",
        "names",
    ],
)
async def test_health_payloads_pass_real_laravel_validator(
    hass, backend, entry, scenario
):
    from homeassistant.components.backup.const import DATA_MANAGER
    from homeassistant.components.backup.manager import (
        BackupManager,
        CoreBackupReaderWriter,
        CreateBackupState,
        IdleEvent,
    )

    from .test_health import make_health
    from .test_health_collectors import emit, repair

    repair(hass)
    if scenario == "names":
        from homeassistant.helpers import device_registry as dr

        hass.config_entries.async_update_entry(entry, title="Freigegebener Eintrag")
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("test", "local")},
            name="Wohnzimmer",
        )
        entity = er.async_get(hass).async_get_or_create(
            "sensor",
            "test",
            "named",
            config_entry=entry,
            device_id=device.id,
            original_name="Temperatur 🌡️",
        )
        hass.states.async_set(entity.entity_id, "unavailable")
    if scenario == "startup":
        hass.set_state(CoreState.starting)
    if scenario == "overflow":
        for i in range(1001):
            hass.states.async_set(f"sensor.{'x' * 235}{i:04}", "unavailable")
    else:
        hass.states.async_set("sensor.health_problem", "unavailable")
    manager = BackupManager(hass, CoreBackupReaderWriter(hass))
    hass.data[DATA_MANAGER] = manager
    manager.async_on_backup_event(IdleEvent())
    health = await make_health(
        hass,
        backend,
        entry,
        health_all_entities=scenario != "selected",
        health_entities=["sensor.health_problem"],
        health_names=scenario == "names",
        health_devices=scenario == "names",
    )
    if scenario == "startup":
        assert health.data is None and health.uploader._latest is None
        hass.set_state(CoreState.running)
        await health.async_collect()
    if scenario.startswith("backup_"):
        emit(manager, CreateBackupState.IN_PROGRESS)
        if scenario == "backup_ok":
            emit(manager, CreateBackupState.COMPLETED)
        elif scenario == "backup_failed":
            emit(manager, CreateBackupState.FAILED, reason="backup_agent_unreachable")
        await health.async_collect()
    await validate_with_laravel(
        hass, health.uploader._envelope(health.uploader._latest), health=True
    )
