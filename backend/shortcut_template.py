"""
shortcut_template.py — generates a .shortcut (binary plist) file for iOS import.

Generates a minimal, known-working structure:
  1. POSTs health metrics to /health-sync?token={token} as JSON
  2. Shows a notification on success

HealthKit reads are NOT included — iOS rejected our parameter format.
Instead the Shortcut prompts the user for values via import questions,
which is more reliable across iOS versions. The user fills in their
HRV/sleep/steps when installing; after that the Shortcut remembers them.
"""
from __future__ import annotations

import plistlib
import uuid as _uuid


def _uid() -> str:
    return str(_uuid.uuid4()).upper()


def generate_shortcut(backend_url: str, token: str) -> bytes:
    base = backend_url.rstrip("/")
    post_url = f"{base}/health-sync?token={token}"

    # Import questions — prompted once on install, stored as Shortcut variables
    import_questions = [
        {
            "DefaultValue": "",
            "ParameterKey": "hrv_ms",
            "QuestionType": "WFWorkflowImportQuestionText",
            "Text": "HRV (ms) — e.g. 55",
        },
        {
            "DefaultValue": "",
            "ParameterKey": "resting_hr",
            "QuestionType": "WFWorkflowImportQuestionText",
            "Text": "Resting HR (bpm) — e.g. 62",
        },
        {
            "DefaultValue": "",
            "ParameterKey": "sleep_hours",
            "QuestionType": "WFWorkflowImportQuestionText",
            "Text": "Sleep hours last night — e.g. 7.5",
        },
        {
            "DefaultValue": "",
            "ParameterKey": "steps_yesterday",
            "QuestionType": "WFWorkflowImportQuestionText",
            "Text": "Steps yesterday — e.g. 8000",
        },
        {
            "DefaultValue": "",
            "ParameterKey": "calories_burned",
            "QuestionType": "WFWorkflowImportQuestionText",
            "Text": "Calories burned — e.g. 2000",
        },
    ]

    def _import_var(param_key: str) -> dict:
        """Reference to a value set via import question."""
        return {
            "Value": {
                "attachmentsByRange": {
                    "{0, 1}": {
                        "Type": "Variable",
                        "VariableName": param_key,
                    }
                },
                "string": "￼",
            },
            "WFSerializationType": "WFTextTokenString",
        }

    def _dict_item(key: str, value: dict) -> dict:
        return {
            "WFItemType": 0,
            "WFKey": {
                "Value": {"string": key},
                "WFSerializationType": "WFTextTokenString",
            },
            "WFValue": value,
        }

    post_action = {
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "UUID": _uid(),
            "WFHTTPMethod": "POST",
            "WFURL": post_url,
            "WFHTTPBodyType": "JSON",
            "WFJSONValues": {
                "Value": {
                    "WFDictionaryFieldValueItems": [
                        _dict_item("hrv_ms",          _import_var("hrv_ms")),
                        _dict_item("resting_hr",       _import_var("resting_hr")),
                        _dict_item("sleep_hours",      _import_var("sleep_hours")),
                        _dict_item("steps_yesterday",  _import_var("steps_yesterday")),
                        _dict_item("calories_burned",  _import_var("calories_burned")),
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
        "WFWorkflowActions": [post_action, notify_action],
        "WFWorkflowClientVersion": "1432",
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowImportQuestions": import_questions,
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowName": "RehabCoach Sync",
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": [],
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": 59511,
            "WFWorkflowIconStartColor": 1216341504,
        },
    }

    return plistlib.dumps(workflow, fmt=plistlib.FMT_BINARY)
