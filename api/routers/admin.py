from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api import auth
from api.admin_store import admin_store

# Public: only the login endpoint. Everything else under /admin requires the admin bearer token.
public_router = APIRouter(prefix="/admin", tags=["admin"])
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(auth.require_admin)])


class LoginIn(BaseModel):
	password: str


@public_router.post("/login")
def login(body: LoginIn, request: Request) -> Dict[str, Any]:
	return {"token": auth.login(body.password, request), "expires_in": auth.TOKEN_TTL_SECONDS}


@router.get("/me")
def me() -> Dict[str, Any]:
	return {"ok": True}


@router.get("/conversations")
def get_conversations() -> List[Dict[str, Any]]:
	return admin_store.get_conversations_summary()

@router.get("/collected_data")
def get_collected_data() -> List[Dict[str, Any]]:
	return admin_store.get_collected_data()

@router.get("/auth_codes")
def get_auth_codes() -> List[Dict[str, Any]]:
	return admin_store.get_auth_codes()

@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
	admin_store.delete_conversation(conversation_id)
	return {"status": "success", "message": f"Conversation {conversation_id} deleted."}

@router.delete("/conversations")
def delete_all_conversations():
	admin_store.delete_all_conversations()
	return {"status": "success", "message": "All conversations deleted."}

@router.get("/cost_summary")
def get_cost_summary() -> Dict[str, Any]:
	"""Legacy shape kept for old clients; billing history can no longer be wiped from the API."""
	from api.routers.billing import summary
	s = summary(days=3650, include_test=True)
	return {"total_input_tokens": s["tokens"]["input_text"] + s["tokens"]["input_audio"] + s["tokens"]["input_image"] + s["tokens"]["input_cached"],
			"total_output_tokens": s["tokens"]["output_text"] + s["tokens"]["output_audio"],
			"total_duration_seconds": s["calls"]["billable_seconds"], "total_cost_usd": s["cost"]["gemini_usd"],
			"cost_per_minute_usd": s["cost"]["per_call_minute_usd"] or 0.0}

_SETTING_KEYS = ("agent_language", "usd_to_bdt", "test_numbers", "test_session_prefixes")
_SETTING_DEFAULTS = {"agent_language": "Bengali", "usd_to_bdt": "123.0", "test_numbers": "01797046851", "test_session_prefixes": "test-,upaytest"}

@router.get("/settings")
def get_settings() -> Dict[str, Any]:
	return {k: admin_store.get_setting(k, _SETTING_DEFAULTS[k]) for k in _SETTING_KEYS}

class SettingUpdate(BaseModel):
	key: str
	value: str

@router.post("/settings")
def update_settings(setting: SettingUpdate):
	if setting.key not in _SETTING_KEYS:
		raise HTTPException(422, "unknown setting")
	if setting.key == "usd_to_bdt":
		try:
			if not 50 <= float(setting.value) <= 500:
				raise ValueError
		except ValueError:
			raise HTTPException(422, "usd_to_bdt must be a number between 50 and 500")
	success = admin_store.update_setting(setting.key, setting.value.strip())
	if success:
		return {"status": "success"}
	return {"status": "error", "message": "Failed to update setting"}
