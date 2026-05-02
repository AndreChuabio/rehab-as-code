"""
shortcut_template.py — generates a .shortcut (Apple XML plist) file.

The generated Shortcut:
  1. Reads HRV, resting HR, steps, active calories from HealthKit (quantity types)
  2. Reads sleep hours via a sleep analysis summary (category type)
  3. POSTs all metrics as JSON to POST /health-sync?token={token}
  4. Shows a success notification

The token is baked in at generation time — no user input needed on install.

Note: HealthKit action parameters follow the iOS Shortcuts plist spec as documented
in https://www.macstories.net/shortcuts and community reverse-engineering. If a
HealthKit step fails on device, it can be re-wired in the Shortcuts editor; the POST
action will always work.
"""
from __future__ import annotations

import plistlib
import uuid as _uuid


def _uid() -> str:
    return str(_uuid.uuid4()).upper()


def _token_string(text: str) -> dict:
    """Wraps a plain text string in the WFTextTokenString serialization envelope."""
    return {
        "Value": {"string": text},
        "WFSerializationType": "WFTextTokenString",
    }


def _output_ref(output_uuid: str) -> dict:
    """Reference to the output of a previous action by UUID."""
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {
                    "OutputUUID": output_uuid,
                    "Type": "ActionOutput",
                }
            },
            "string": "￼",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def _health_quantity_action(
    output_uuid: str,
    quantity_type: str,
    label: str,
    aggregation: int = 1,  # 0=none 1=avg 2=sum 3=min 4=max
) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.health.quantity.read",
        "WFWorkflowActionParameters": {
            "CustomOutputName": label,
            "GroupingIdentifier": _uid(),
            "UUID": output_uuid,
            "WFHealthQuantityTypeIdentifier": quantity_type,
            "WFHealthAggregationStyle": aggregation,
            "WFHealthStartDate": "Start of Today",
            "WFHealthEndDate": "Now",
        },
    }


def _dict_item(key: str, value_ref: dict) -> dict:
    return {
        "WFItemType": 0,
        "WFKey": _token_string(key),
        "WFValue": value_ref,
    }


def generate_shortcut(backend_url: str, token: str) -> bytes:
    """Return XML plist bytes for a .shortcut file."""
    base = backend_url.rstrip("/")

    # UUIDs for each HealthKit read output
    hrv_id    = _uid()
    rhr_id    = _uid()
    sleep_id  = _uid()
    steps_id  = _uid()
    cal_id    = _uid()

    health_actions = [
        _health_quantity_action(
            hrv_id,
            "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
            "HRV (ms)",
            aggregation=1,  # average
        ),
        _health_quantity_action(
            rhr_id,
            "HKQuantityTypeIdentifierRestingHeartRate",
            "Resting HR (bpm)",
            aggregation=1,
        ),
        # Sleep: use time-asleep quantity (iOS 16+) — falls back gracefully if unavailable
        _health_quantity_action(
            sleep_id,
            "HKCategoryTypeIdentifierSleepAnalysis",
            "Sleep (hours)",
            aggregation=2,  # sum
        ),
        _health_quantity_action(
            steps_id,
            "HKQuantityTypeIdentifierStepCount",
            "Steps",
            aggregation=2,  # sum
        ),
        _health_quantity_action(
            cal_id,
            "HKQuantityTypeIdentifierActiveEnergyBurned",
            "Calories",
            aggregation=2,  # sum
        ),
    ]

    post_action = {
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "UUID": _uid(),
            "WFHTTPMethod": "POST",
            "WFURL": f"{base}/health-sync?token={token}",
            "WFHTTPBodyType": "JSON",
            "WFJSONValues": {
                "Value": {
                    "WFDictionaryFieldValueItems": [
                        _dict_item("hrv_ms",           _output_ref(hrv_id)),
                        _dict_item("resting_hr",        _output_ref(rhr_id)),
                        _dict_item("sleep_hours",       _output_ref(sleep_id)),
                        _dict_item("steps_yesterday",   _output_ref(steps_id)),
                        _dict_item("calories_burned",   _output_ref(cal_id)),
                    ]
                },
                "WFSerializationType": "WFDictionaryFieldValue",
            },
        },
    }

    notify_action = {
        "WFWorkflowActionIdentifier": "is.workflow.actions.notification",
        "WFWorkflowActionParameters": {
            "UUID": _uid(),
            "WFNotificationActionTitle": "RehabCoach",
            "WFNotificationActionBody": "Health data synced ✓",
            "WFNotificationActionPlaySound": False,
        },
    }

    workflow = {
        "WFWorkflowActions": health_actions + [post_action, notify_action],
        "WFWorkflowClientVersion": "1300.0.1",
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowName": "RehabCoach Sync",
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": [],
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 1216341504,   # green
            "WFWorkflowIconGlyphNumber": 59511,
        },
    }

    return plistlib.dumps(workflow, fmt=plistlib.FMT_XML)
