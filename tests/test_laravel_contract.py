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


async def validate_with_laravel(hass, body):
    def validate():
        return subprocess.run(
            [str(Path(LARAVEL_PATH) / "vendor/bin/sail"), "php", "-r", PHP_VALIDATE],
            input=body,
            capture_output=True,
            cwd=LARAVEL_PATH,
            timeout=20,
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
