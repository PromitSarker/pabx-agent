from typing import Any, Dict, List, Union

from langchain_core.tools import tool
from pydantic import BaseModel

from agent.db import get_connection


@tool
def escalate(reason: str) -> str:
	"""
	Use this tool when the user has a complex request, complaint, or wants to talk to a human.
	It will signal the system to transfer the conversation to a human support agent.
	
	Why it's needed: Bots shouldn't handle arguments or disputes! This acts like an "emergency exit"
	button for the LLM to easily hand the interaction over to real customer support.
	"""
	return "A customer service person will contact the customer soon. Tell the customer this; do not give any phone number."


def rewrite_query(original_query: str) -> str:
	"""Fixes typos and contextualizes the query for RAG search."""
	from langchain_google_genai import ChatGoogleGenerativeAI
	from agent.config import GEMINI_API_KEY
	from pydantic import BaseModel

	class RewrittenQuery(BaseModel):
		query: str

	llm = ChatGoogleGenerativeAI(model="gemini-3.8-flash", google_api_key=GEMINI_API_KEY, thinking_budget=0)
	structured_llm = llm.with_structured_output(RewrittenQuery, include_raw=True)
	prompt = (
		"Rewrite the following search query to correct any spelling mistakes "
		"and make it a clear, standalone search query for a knowledge base.\n\n"
		f"Query: {original_query}"
	)
	try:
		result = structured_llm.invoke(prompt)
		response = result.get("parsed") if isinstance(result, dict) else result
		if isinstance(result, dict) and result.get("raw") is not None:
			from agent.usage import record_usage, tokens_from_langchain
			raw = result["raw"]
			record_usage(model=(getattr(raw, "response_metadata", None) or {}).get("model_name") or "gemini-3.8-flash",
						 purpose="query_rewrite", **tokens_from_langchain(getattr(raw, "usage_metadata", None)))
		corrected = response.query.strip() if response and response.query else original_query
		print(f"[QueryRewriter] '{original_query}' -> '{corrected}'")
		return corrected
	except Exception as e:
		print(f"[QueryRewriter] Failed: {e}, using original query")
		return original_query

@tool
def search_knowledge_base(query: str) -> str:
	"""
	Search the knowledge base for general information, policies, or FAQs.
	
	Why it's needed: Use this when the user asks a question about the MTB Islamic DPS on upay: eligibility, plans, deposit amounts, tenure, profit, tax, withdrawal, early encashment, instalments, nominee or charges.
	"""
	from agent.rag import search_documents
	corrected_query = rewrite_query(query)
	result = search_documents(corrected_query)
	return f"<context>\n{result}\n</context>"


class SaveCollectedInformationInput(BaseModel):
	data: Dict[str, str]
	session_id: str = "" # Injected by the system, LLM does not need to provide this.


@tool(args_schema=SaveCollectedInformationInput)
def save_collected_information(data: Dict[str, str], session_id: str = "") -> str:
	"""
	Save pieces of information gathered from the user (e.g., for IT services, lead gen, etc).
	Pass a dictionary mapping the exact requested keys to the user's provided values.
	Do NOT provide session_id, it is injected automatically.
	
	Why it's needed: When the user wants to buy a service or provide their information, use this tool to securely store all the details at once.
	"""
	if not session_id:
		return "ERROR: session_id is missing."
	
	if not data:
		return "ERROR: No data provided to save."
	
	saved_keys = []
	try:
		with get_connection() as conn:
			with conn.cursor() as cur:
				for key, value in data.items():
					cur.execute("UPDATE collected_data SET `value` = %s, created_at = CURRENT_TIMESTAMP(6) WHERE session_id = %s AND `key` = %s", (str(value), session_id, key))
					if cur.rowcount == 0:
						cur.execute("INSERT INTO collected_data (session_id, `key`, `value`) VALUES (%s, %s, %s)", (session_id, key, str(value)))
					saved_keys.append(key)
			conn.commit()
		return f"Successfully saved: {', '.join(saved_keys)}."
	except Exception as e:
		return f"ERROR: Could not save information: {str(e)}"


class NoteCustomerQueryInput(BaseModel):
	query: str
	kind: str = "question"
	session_id: str = "" # Injected by the system, LLM does not need to provide this.


_QUERY_LABELS = {"question": "Unanswered Query", "callback": "Callback Request", "complaint": "Complaint"}


@tool(args_schema=NoteCustomerQueryInput)
def note_customer_query(query: str, kind: str = "question", session_id: str = "") -> str:
	"""
	Note down something a customer service person must follow up on: a question you could not answer
	from the knowledge base, a request to be called back, or a complaint.
	kind is one of: "question" (default), "callback", "complaint".
	Do NOT provide session_id, it is injected automatically.

	Each call adds a NEW numbered entry (Unanswered Query #1, #2, ...) so earlier ones are never overwritten,
	and flags the session as needing follow-up.
	"""
	if not session_id:
		return "ERROR: session_id is missing."
	query = (query or "").strip()
	if not query:
		return "ERROR: query is empty."
	label = _QUERY_LABELS.get((kind or "").strip().lower(), _QUERY_LABELS["question"])
	try:
		with get_connection() as conn:
			with conn.cursor() as cur:
				cur.execute("SELECT id FROM collected_data WHERE session_id = %s AND `key` LIKE %s", (session_id, label + " #%"))
				n = len(cur.fetchall()) + 1
				cur.execute("INSERT INTO collected_data (session_id, `key`, `value`) VALUES (%s, %s, %s)",
							(session_id, f"{label} #{n}", query[:1000]))
				cur.execute("UPDATE collected_data SET `value` = %s, created_at = CURRENT_TIMESTAMP(6) WHERE session_id = %s AND `key` = %s",
							("Yes - customer service to contact", session_id, "Follow-up Needed"))
				if cur.rowcount == 0:
					cur.execute("INSERT INTO collected_data (session_id, `key`, `value`) VALUES (%s, %s, %s)",
								(session_id, "Follow-up Needed", "Yes - customer service to contact"))
			conn.commit()
		return f"Noted as {label} #{n}. If you have not already said so aloud, tell the customer now that you have noted it down and a customer service person will contact them soon. Do not give a phone number."
	except Exception as e:
		return f"ERROR: Could not note the query: {str(e)}"


class SendVerificationEmailInput(BaseModel):
	email: str
	session_id: str = "" # Injected by the system, LLM does not need to provide this.


@tool(args_schema=SendVerificationEmailInput)
def send_verification_email(email: str, session_id: str = "") -> str:
	"""
	Generate a temporary password (verification code) and send it to the user's email.
	
	Why it's needed: When a user provides their email to log in or verify their identity, this tool generates a secure code and emails it to them.
	"""
	import random
	import string
	import resend
	from agent.config import RESEND_API_KEY
	
	# Generate 6-character random code
	code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
	
	# Save to DB (user_auth_codes and collected_data)
	auth_query = "INSERT INTO user_auth_codes (email, code) VALUES (%s, %s)"
	collected_query = "INSERT INTO collected_data (session_id, `key`, `value`) VALUES (%s, %s, %s)"
	
	try:
		with get_connection() as conn:
			with conn.cursor() as cur:
				cur.execute(auth_query, (email, code))
				if session_id:
					cur.execute(collected_query, (session_id, "Temporary Password", code))
			conn.commit()
	except Exception as e:
		return f"ERROR: Could not save verification code: {str(e)}"
	
	# Try sending email
	if RESEND_API_KEY:
		try:
			from agent.config import RESEND_FROM_EMAIL
			resend.api_key = RESEND_API_KEY
			params = {
				"from": f"upay <{RESEND_FROM_EMAIL}>",
				"to": [email],
				"subject": "upay Verification Code",
				"text": f"Hello,\n\nYour temporary password is: {code}\n\nIMPORTANT: This is a temporary password, and you need to change it immediately after you log in.\n\nThank you,\nupay"
			}
			resend.Emails.send(params)
			return "Verification code successfully sent to email."
		except Exception as e:
			print(f"Failed to send email via Resend: {e}")
			return f"Code generated ({code}) but failed to send email due to Resend API error."
	else:
		print(f"MOCK EMAIL SENT TO {email}: Code is {code}")
		return "Verification code successfully generated and mock-sent (Resend API key not configured)."


class WriteToChatInput(BaseModel):
	message: str
	session_id: str = "" # Injected by the system, LLM does not need to provide this.


@tool(args_schema=WriteToChatInput)
def write_to_chat(message: str, session_id: str = "") -> str:
	"""
	Write a message directly to the text chat interface for the user to see.
	
	Why it's needed: Use this when the user asks you to "write it down", "spell it", or provide detailed text (like a price list or link) during a voice call.
	"""
	# The actual broadcasting logic is handled in proxy_gemini_to_client.
	# This function just acts as a stub to return success to the LLM.
	return "Message successfully written to chat."


@tool
def end_call() -> str:
	"""
	End the current voice call with the user.
	
	Why it's needed: Use this when the conversation has naturally concluded or when the user explicitly asks to hang up or end the call. ALWAYS ask for confirmation before calling this tool.
	"""
	return "Call ended successfully."
